"""API HTTP + WebSocket.

Deux canaux, volontairement séparés :
  - **REST** pour tout ce qui est rare et structuré : catalogue de blocs,
    lecture/écriture du graphe, démarrage/arrêt.
  - **WebSocket binaire** pour le flux de données, où chaque octet compte.

Format d'une trame WebSocket :

    [ uint32 little-endian : longueur de l'en-tête ][ en-tête JSON ][ charge utile ]

L'en-tête dit à quel afficheur la trame appartient et comment lire la charge
utile. Le JSON seul aurait coûté environ dix fois plus cher pour un spectre —
d'où le binaire, pour un gain qui se voit dès qu'on monte en cadence.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import time
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .blocks import Ctx, catalogue
from .graph import DEFAULT_GRAPH, Flowgraph, GraphError
from .schema import GraphIn, RunIn
from .security import ALLOWED_HOSTS, ALLOWED_ORIGINS, Guard, origin_ok

log = logging.getLogger("blocksdr")

# --- Limites du WebSocket ---------------------------------------------------
# Une poignée d'onglets ouverts, pas plus : chaque client reçoit TOUT le flux.
MAX_WS_CLIENTS = 8
# Trames en attente par client. Au-delà, ce client est trop lent (réseau, onglet
# en arrière-plan) : on jette SES trames, sans ralentir les autres ni le moteur.
CLIENT_QUEUE = 256
# Le client n'a rien à dire (il ne fait qu'écouter) : quelques messages par
# fenêtre de 10 s sont tolérés, au-delà la connexion est fermée.
WS_MSG_PER_10S = 20


def encode_frame(header: dict, payload: bytes) -> bytes:
    h = json.dumps(header, separators=(",", ":")).encode()
    return struct.pack("<I", len(h)) + h + payload


class Client:
    """Un abonné au flux, avec SA file d'envoi.

    ⚠️ Avant, la diffusion attendait chaque `send` l'un après l'autre : un seul
    client lent (téléphone en 4G, onglet gelé) ralentissait la boucle du moteur
    entière, et c'est le dongle qui débordait. Désormais le moteur ne fait que
    DÉPOSER les trames ; une tâche par client les envoie à son rythme.
    """

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.q: asyncio.Queue[bytes | str] = asyncio.Queue(maxsize=CLIENT_QUEUE)
        self.dropped = 0

    def push(self, msg: bytes | str) -> None:
        try:
            self.q.put_nowait(msg)
        except asyncio.QueueFull:
            self.dropped += 1

    async def run(self) -> None:
        try:
            while True:
                m = await self.q.get()
                if isinstance(m, bytes):
                    await self.ws.send_bytes(m)
                else:
                    await self.ws.send_text(m)
        except Exception:
            # Connexion perdue : la boucle de réception de `ws_endpoint` le
            # verra aussi et retirera le client.
            pass


class Engine:
    """Détient le graphe courant et la boucle qui le fait tourner."""

    def __init__(self) -> None:
        self.spec: dict[str, Any] = DEFAULT_GRAPH
        self.fg: Flowgraph | None = None
        self.task: asyncio.Task | None = None
        self.running = False
        self.error: str | None = None
        self.clients: set[Client] = set()
        # Une seule modification à la fois (graphe ou marche/arrêt) : deux
        # requêtes simultanées ouvraient sinon deux fois le dongle.
        self.lock = asyncio.Lock()
        self.ticks = 0
        self.rate = 0.0          # tours par seconde réellement atteints
        self.load = 0.0          # part du temps passée à calculer (0 → 1)

    # -- graphe ------------------------------------------------------------
    def validate(self, spec: dict) -> None:
        """Vérifie qu'un graphe tient debout, puis REND ce qu'il a réservé.

        ⚠️ Construire un graphe n'est pas une opération neutre : la source
        RTL-SDR ouvre le dongle et démarre son fil de lecture dès son
        constructeur. Sans le `close()` ci-dessous, cette instance de
        vérification gardait le matériel, et le déploiement réel qui suit
        échouait sur « dongle déjà pris » — par soi-même, une seconde plus tôt.

        Le symptôme était trompeur au possible : le message accusait un autre
        programme, et un redémarrage du conteneur « corrigeait » le problème
        jusqu'au prochain envoi de graphe.

        Désormais, si un graphe TOURNE, la vérification se fait sans toucher
        au matériel : le dongle est tenu par le graphe en marche, et l'ouvrir
        une seconde fois échouait TOUJOURS sur « dongle déjà pris » — changer
        un réglage en pleine écoute était impossible. Le vrai essai a lieu au
        redémarrage, juste après l'arrêt de l'ancien graphe.
        """
        fg = Flowgraph(spec, hardware=not self.running)
        fg.close()

    async def set_graph(self, spec: dict) -> None:
        was = self.running
        await self.stop()
        self.spec = spec
        self.error = None
        if was:
            await self.start()

    # -- cycle de vie ------------------------------------------------------
    async def start(self) -> None:
        if self.running:
            return
        if self.fg is not None:
            # Graphe arrêté par une erreur d'exécution : il tient encore ses
            # ressources (le dongle). On le rend avant d'en construire un autre.
            await self.stop()
        try:
            self.fg = Flowgraph(self.spec)
        except GraphError as e:
            self.error = str(e)
            raise
        self.error = None
        self.running = True
        self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
            self.task = None
        if self.fg:
            # Rendre le dongle peut prendre jusqu'à quelques dixièmes de
            # seconde (fin de la lecture USB en cours) : hors de la boucle
            # asyncio, pour que le flux des autres requêtes ne gèle pas.
            fg, self.fg = self.fg, None
            await asyncio.to_thread(fg.close)

    async def _loop(self) -> None:
        assert self.fg
        period = self.fg.period()
        pending: list[bytes] = []

        def emit(header: dict, payload: bytes) -> None:
            pending.append(encode_frame(header, payload))

        ctx = Ctx(emit=emit)
        # Le tour de graphe s'exécute HORS de la boucle asyncio.
        #
        # Avec la source simulée, tout est du numpy qui rend la main tout de
        # suite. Avec un vrai dongle, `read_samples()` BLOQUE le temps que l'USB
        # produise le bloc demandé — soit des dizaines de millisecondes pendant
        # lesquelles plus rien d'autre ne tournerait : ni l'envoi des trames, ni
        # l'API, ni même la réponse à un « Arrêter ». Un exécuteur règle ça.
        loop = asyncio.get_running_loop()
        next_at = time.perf_counter()
        window_t0, window_n, busy = next_at, 0, 0.0
        try:
            while self.running:
                t0 = time.perf_counter()
                # ⚠️ La charge se mesure en temps PROCESSEUR, pas en temps écoulé.
                # Avec une source matérielle, le tour passe l'essentiel de sa
                # durée BLOQUÉ dans la lecture USB : compté en temps écoulé, on
                # affichait 98 % de charge alors que le processeur ne faisait
                # rien — une fausse alerte qui donne envie d'optimiser du code
                # qui n'est pas le problème.
                c0 = time.process_time()
                ctx.tick = self.ticks
                ctx.now = t0
                try:
                    await loop.run_in_executor(None, self.fg.tick, ctx)
                except Exception as e:  # un bloc a explosé : on arrête proprement
                    # Le détail (pile d'appels) reste dans les journaux du
                    # conteneur ; le navigateur ne reçoit que le message.
                    log.exception("tour de graphe en échec")
                    self.error = f"Erreur d'exécution : {e}"
                    self.running = False
                    self._broadcast_json({"type": "error", "message": self.error})
                    break
                self.ticks += 1
                busy += time.process_time() - c0

                if pending:
                    frames, pending[:] = list(pending), []
                    self._broadcast(frames)

                # statistiques, une fois par seconde
                window_n += 1
                if t0 - window_t0 >= 1.0:
                    self.rate = window_n / (t0 - window_t0)
                    self.load = min(1.0, busy / (t0 - window_t0))
                    window_t0, window_n, busy = t0, 0, 0.0
                    self._broadcast_json({
                        "type": "stats", "rate": round(self.rate, 1),
                        "load": round(self.load, 3), "ticks": self.ticks,
                    })

                if period <= 0.0:
                    # Source matérielle : c'est elle qui donne le tempo, on ne
                    # fait que rendre la main à la boucle entre deux tours.
                    await asyncio.sleep(0)
                    continue
                next_at += period
                delay = next_at - time.perf_counter()
                if delay < -0.5:      # on a pris trop de retard : on se recale
                    next_at = time.perf_counter()
                    delay = 0
                await asyncio.sleep(max(0.0, delay))
        except asyncio.CancelledError:
            raise

    # -- diffusion ---------------------------------------------------------
    def _broadcast(self, frames: list[bytes]) -> None:
        for c in list(self.clients):
            for f in frames:
                c.push(f)

    def _broadcast_json(self, obj: dict) -> None:
        txt = json.dumps(obj)
        for c in list(self.clients):
            c.push(txt)

    def status(self) -> dict:
        return {"running": self.running, "error": self.error, "ticks": self.ticks,
                "rate": round(self.rate, 1), "load": round(self.load, 3),
                "clients": len(self.clients)}


engine = Engine()

# Pas de documentation interactive (/docs, /redoc, /openapi.json) : personne ne
# s'en sert, et c'est autant de surface en moins. `debug` reste à False : une
# erreur renvoie « Internal Server Error », jamais la pile d'appels.
app = FastAPI(title="BlockSDR", version="0.2.0", debug=False,
              docs_url=None, redoc_url=None, openapi_url=None)

# En production tout passe par la même origine (Traefik) : CORS ne sert qu'à
# REFUSER les autres. Liste fermée, pas d'étoile.
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS,
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type"])
# En-tête Host vérifié : c'est précisément le vecteur de la CVE-2026-48710
# (Starlette reconstruisait l'URL depuis un Host non validé). La mise à jour
# de Starlette corrige la faille ; ce filtre la ferme une seconde fois.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
# Ajouté en dernier = exécuté en premier : taille, Origin, en-têtes.
app.add_middleware(Guard)


@app.exception_handler(RequestValidationError)
async def invalid_request(_req: Request, exc: RequestValidationError):
    """422 SANS recopier la requête reçue (pydantic la renvoie par défaut)."""
    errs = exc.errors()
    first = errs[0] if errs else {}
    where = ".".join(str(x) for x in first.get("loc", ()))
    return JSONResponse(status_code=422, content={
        "ok": False, "error": f"Requête invalide : {where} — {first.get('msg', '?')}"})


@app.get("/api/health")
def health():
    return {"ok": True, "version": app.version}


@app.get("/api/blocks")
def blocks():
    return {"blocks": catalogue()}


@app.get("/api/graph")
def get_graph():
    return {"graph": engine.spec, "status": engine.status()}


@app.post("/api/graph")
async def post_graph(g: GraphIn):
    spec = g.to_spec()
    async with engine.lock:
        try:
            engine.validate(spec)          # on refuse un graphe invalide sans rien casser
            await engine.set_graph(spec)   # peut encore échouer au redémarrage (dongle)
        except GraphError as e:
            return {"ok": False, "error": str(e), "status": engine.status()}
    return {"ok": True, "status": engine.status()}


@app.post("/api/run")
async def run(r: RunIn):
    async with engine.lock:
        try:
            if r.running:
                await engine.start()
            else:
                await engine.stop()
        except GraphError as e:
            return {"ok": False, "error": str(e), "status": engine.status()}
    return {"ok": True, "status": engine.status()}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    # Refus AVANT l'acceptation : le navigateur reçoit un 403 de poignée de main.
    if not origin_ok(ws.headers.get("origin")) or len(engine.clients) >= MAX_WS_CLIENTS:
        await ws.close(code=1008)
        return
    await ws.accept()
    await ws.send_text(json.dumps({"type": "hello", "status": engine.status()}))
    client = Client(ws)
    engine.clients.add(client)
    sender = asyncio.create_task(client.run())
    window_t0, count = time.monotonic(), 0
    try:
        # Le client ne parle pas : la réception sert de détecteur de coupure,
        # et de garde-fou contre un client qui inonderait le serveur.
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            now = time.monotonic()
            if now - window_t0 > 10.0:
                window_t0, count = now, 0
            count += 1
            if count > WS_MSG_PER_10S:
                await ws.close(code=1008)
                break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        engine.clients.discard(client)
        sender.cancel()

# ---------------------------------------------------------------------------
#  Service des fichiers statiques (production)
#
#  En développement, c'est Vite qui sert le front et relaie /api et /ws ici.
#  En production, l'image contient le front déjà buildé et ce même processus
#  sert tout : une seule origine, un seul port, aucun CORS, et le WebSocket
#  passe par le même hôte que la page — donc rien à configurer côté client.
#
#  Ce bloc est enregistré EN DERNIER, volontairement : FastAPI teste les routes
#  dans l'ordre de déclaration, et l'attrape-tout ci-dessous masquerait /api.
# ---------------------------------------------------------------------------
STATIC_DIR = os.environ.get("BLOCKSDR_STATIC") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


def static_files(root: str) -> dict[str, str]:
    """Inventaire des fichiers servables : chemin d'URL -> chemin sur disque.

    ⚠️ On ne construit JAMAIS un chemin disque à partir de l'URL. L'ancien
    garde-fou (`normpath` puis `startswith(STATIC_DIR)`) laissait passer
    `../static-autre/…` — « /app/static-autre » COMMENCE bien par
    « /app/static ». Ici, l'URL n'est qu'une clé cherchée dans un
    dictionnaire dressé au démarrage : ce qui n'y est pas n'existe pas. Le
    front buildé est figé dans l'image, l'inventaire ne bouge donc jamais.
    Les liens symboliques et les fichiers cachés sont ignorés.
    """
    found: dict[str, str] = {}
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if not x.startswith(".")
                   and not os.path.islink(os.path.join(d, x))]
        for f in files:
            full = os.path.join(d, f)
            if f.startswith(".") or os.path.islink(full):
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            found[rel] = full
    return found


if os.path.isdir(STATIC_DIR):
    _FILES = static_files(STATIC_DIR)
    _INDEX = os.path.join(STATIC_DIR, "index.html")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        # Fichier réel demandé (assets/, favicon…) : on le sert tel quel.
        target = _FILES.get(full_path)
        if target is not None:
            return FileResponse(target)
        # Sinon, route côté client (vite-plugin-pages) : on rend l'index et
        # c'est React Router qui décide.
        return FileResponse(_INDEX)

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
import struct
import time
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .blocks import Ctx, catalogue
from .graph import DEFAULT_GRAPH, Flowgraph, GraphError


def encode_frame(header: dict, payload: bytes) -> bytes:
    h = json.dumps(header, separators=(",", ":")).encode()
    return struct.pack("<I", len(h)) + h + payload


class Engine:
    """Détient le graphe courant et la boucle qui le fait tourner."""

    def __init__(self) -> None:
        self.spec: dict[str, Any] = DEFAULT_GRAPH
        self.fg: Flowgraph | None = None
        self.task: asyncio.Task | None = None
        self.running = False
        self.error: str | None = None
        self.clients: set[WebSocket] = set()
        self.ticks = 0
        self.rate = 0.0          # tours par seconde réellement atteints
        self.load = 0.0          # part du temps passée à calculer (0 → 1)

    # -- graphe ------------------------------------------------------------
    def validate(self, spec: dict) -> Flowgraph:
        return Flowgraph(spec)

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
            self.fg.close()
            self.fg = None

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
                ctx.tick = self.ticks
                ctx.now = t0
                try:
                    await loop.run_in_executor(None, self.fg.tick, ctx)
                except Exception as e:  # un bloc a explosé : on arrête proprement
                    self.error = f"Erreur d'exécution : {e}"
                    self.running = False
                    await self._broadcast_json({"type": "error", "message": self.error})
                    break
                self.ticks += 1
                busy += time.perf_counter() - t0

                if pending:
                    frames, pending[:] = list(pending), []
                    await self._broadcast(frames)

                # statistiques, une fois par seconde
                window_n += 1
                if t0 - window_t0 >= 1.0:
                    self.rate = window_n / (t0 - window_t0)
                    self.load = min(1.0, busy / (t0 - window_t0))
                    window_t0, window_n, busy = t0, 0, 0.0
                    await self._broadcast_json({
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
    async def _broadcast(self, frames: list[bytes]) -> None:
        dead = []
        for ws in list(self.clients):
            try:
                for f in frames:
                    await ws.send_bytes(f)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def _broadcast_json(self, obj: dict) -> None:
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(json.dumps(obj))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    def status(self) -> dict:
        return {"running": self.running, "error": self.error, "ticks": self.ticks,
                "rate": round(self.rate, 1), "load": round(self.load, 3),
                "clients": len(self.clients)}


engine = Engine()
app = FastAPI(title="BlockSDR", version="0.1.0")

# En dev, le front tourne sur le port 5173 (Vite) et l'API sur 8000 : sans CORS
# le navigateur bloquerait les appels. En production tout passe par le même
# proxy, donc la liste large ne sert que localement.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


class GraphIn(BaseModel):
    nodes: list[dict]
    edges: list[dict]


class RunIn(BaseModel):
    running: bool


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
    spec = {"nodes": g.nodes, "edges": g.edges}
    try:
        engine.validate(spec)          # on refuse un graphe invalide sans rien casser
    except GraphError as e:
        return {"ok": False, "error": str(e), "status": engine.status()}
    await engine.set_graph(spec)
    return {"ok": True, "status": engine.status()}


@app.post("/api/run")
async def run(r: RunIn):
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
    await ws.accept()
    engine.clients.add(ws)
    await ws.send_text(json.dumps({"type": "hello", "status": engine.status()}))
    try:
        while True:
            await ws.receive_text()   # le client ne parle pas, ça sert de détecteur de coupure
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        engine.clients.discard(ws)

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
import os
from fastapi.responses import FileResponse

STATIC_DIR = os.environ.get("BLOCKSDR_STATIC") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

if os.path.isdir(STATIC_DIR):
    _INDEX = os.path.join(STATIC_DIR, "index.html")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        # Fichier réel demandé (assets/, favicon…) : on le sert tel quel.
        target = os.path.normpath(os.path.join(STATIC_DIR, full_path))
        # Garde-fou anti-remontée de chemin : rester dans STATIC_DIR.
        if target.startswith(os.path.normpath(STATIC_DIR)) and os.path.isfile(target):
            return FileResponse(target)
        # Sinon, route côté client (vite-plugin-pages) : on rend l'index et
        # c'est React Router qui décide.
        return FileResponse(_INDEX)

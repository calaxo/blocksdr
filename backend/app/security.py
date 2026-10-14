"""Garde-fous HTTP et WebSocket, en ASGI pur (aucune bibliothèque de plus).

Ce que l'application accepte est réglé par deux variables d'environnement :

  - `BLOCKSDR_ORIGINS` : origines autorisées à appeler l'API et à ouvrir le
    WebSocket (séparées par des virgules). Par défaut, l'accès local
    `http://localhost:8092` seulement.
  - `BLOCKSDR_HOSTS` : noms d'hôte acceptés dans l'en-tête Host. Par défaut
    `localhost` et `127.0.0.1` (le healthcheck en a besoin).

Derrière un reverse proxy, les deux DOIVENT être réglées sur le nom public,
par exemple `BLOCKSDR_ORIGINS=https://blocksdr.example.org` et
`BLOCKSDR_HOSTS=blocksdr.example.org,localhost,127.0.0.1`.

⚠️ Le contrôle d'Origin du WebSocket n'est PAS redondant avec CORS : les
navigateurs n'appliquent pas CORS aux WebSocket. Sans lui, n'importe quelle
page ouverte dans le navigateur pourrait s'abonner au flux.
"""

from __future__ import annotations

import os

DEFAULT_ORIGINS = "http://localhost:8092,http://127.0.0.1:8092"
DEFAULT_HOSTS = "localhost,127.0.0.1"


def _env_list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


ALLOWED_ORIGINS = _env_list("BLOCKSDR_ORIGINS", DEFAULT_ORIGINS)
ALLOWED_HOSTS = _env_list("BLOCKSDR_HOSTS", DEFAULT_HOSTS)

# Un graphe complet tient en quelques dizaines de Kio : au-delà, ce n'est pas
# le front qui parle.
MAX_BODY = 128 * 1024


def origin_ok(origin: str | None) -> bool:
    """Origin absente = client hors navigateur (curl, sonde) : accepté.

    Un navigateur, lui, envoie TOUJOURS l'en-tête sur un WebSocket et sur un
    POST : c'est donc lui qu'on filtre.
    """
    return origin is None or origin in ALLOWED_ORIGINS


def _csp() -> str:
    """Politique de contenu : tout vient de la même origine.

    `style-src 'unsafe-inline'` est le seul relâchement : React Flow positionne
    les blocs par l'attribut `style`. Les adresses ws(s) sont listées en plus
    de 'self', que certains navigateurs n'étendent pas aux WebSocket.
    """
    ws = " ".join(o.replace("https://", "wss://").replace("http://", "ws://")
                  for o in ALLOWED_ORIGINS)
    return ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            f"img-src 'self' data:; connect-src 'self' {ws}; worker-src 'self'; "
            "object-src 'none'; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'self'")


SECURITY_HEADERS = [
    (b"content-security-policy", _csp().encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"cross-origin-opener-policy", b"same-origin"),
]


class Guard:
    """Middleware ASGI : taille des requêtes, Origin, en-têtes de sécurité.

    Placé en premier, il refuse avant que FastAPI ne lise quoi que ce soit.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)      # WebSocket : voir main.py
            return

        headers = dict(scope.get("headers") or [])
        method = scope.get("method", "GET")

        # Requêtes qui modifient l'état : une Origin étrangère est refusée,
        # même si CORS aurait fini par bloquer la RÉPONSE (la requête, elle,
        # serait déjà passée).
        if method not in ("GET", "HEAD", "OPTIONS"):
            origin = headers.get(b"origin")
            if not origin_ok(origin.decode("latin-1") if origin else None):
                await _plain(send, 403, b"Origine refusee")
                return

        # Taille annoncée obligatoire pour un envoi : un navigateur la donne
        # toujours, et le serveur HTTP (h11) garantit qu'on ne lira pas un
        # octet de plus. Pas d'envoi « par morceaux » à compter au fil de l'eau.
        length = headers.get(b"content-length")
        if method in ("POST", "PUT", "PATCH") and length is None:
            await _plain(send, 411, b"Content-Length obligatoire")
            return
        if length is not None:
            try:
                too_big = int(length) > MAX_BODY
            except ValueError:
                await _plain(send, 400, b"Content-Length invalide")
                return
            if too_big:
                await _plain(send, 413, b"Requete trop grosse")
                return

        async def send_with_headers(msg):
            if msg["type"] == "http.response.start":
                msg = dict(msg)
                msg["headers"] = list(msg.get("headers") or []) + SECURITY_HEADERS
            await send(msg)

        await self.app(scope, receive, send_with_headers)


async def _plain(send, status: int, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})

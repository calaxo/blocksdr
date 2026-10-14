"""L'API de bout en bout : garde-fous HTTP, WebSocket, fichiers statiques."""

from __future__ import annotations

import copy
import os
import tempfile

import pytest

# Un faux front buildé, et À CÔTÉ un dossier dont le nom commence pareil : c'est
# lui que l'ancien garde-fou (`startswith`) laissait lire.
_TMP = tempfile.mkdtemp()
_STATIC = os.path.join(_TMP, "static")
os.makedirs(os.path.join(_STATIC, "assets"))
os.makedirs(os.path.join(_TMP, "static-autre"))
for path, txt in [("static/index.html", "<html>index</html>"),
                  ("static/assets/app.js", "console.log(1)"),
                  ("static/.cache", "cache"),
                  ("static-autre/secret.txt", "SECRET")]:
    with open(os.path.join(_TMP, path), "w") as f:
        f.write(txt)
os.environ["BLOCKSDR_STATIC"] = _STATIC
# Configuration d'un déploiement derrière un reverse proxy, comme en production.
os.environ["BLOCKSDR_ORIGINS"] = "https://blocksdr.example.org"
os.environ["BLOCKSDR_HOSTS"] = "blocksdr.example.org,localhost,127.0.0.1"

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from app import main  # noqa: E402
from app.graph import DEFAULT_GRAPH  # noqa: E402

ORIGIN = "https://blocksdr.example.org"
WS_URL = "wss://blocksdr.example.org/ws"


@pytest.fixture(scope="module")
def _client():
    # UN SEUL client (donc une seule boucle asyncio) pour tout le module : le
    # moteur est global et garde sa tâche d'une requête à l'autre.
    with TestClient(main.app, base_url=ORIGIN) as c:
        yield c


@pytest.fixture
def client(_client):
    yield _client
    # Chaque test repart d'un moteur arrêté, sur le graphe par défaut.
    _client.post("/api/run", json={"running": False})
    main.engine.spec = DEFAULT_GRAPH


# --- HTTP --------------------------------------------------------------------

def test_sante_et_en_tetes(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors" in r.headers["content-security-policy"]


def test_host_inconnu_refuse(client):
    assert client.get("/api/health", headers={"host": "evil.example"}).status_code == 400


def test_post_d_une_origine_etrangere_refuse(client):
    r = client.post("/api/run", json={"running": False},
                    headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_cors_ferme(client):
    r = client.options("/api/graph", headers={
        "origin": "https://evil.example", "access-control-request-method": "POST"})
    assert "access-control-allow-origin" not in r.headers
    r = client.options("/api/graph", headers={
        "origin": ORIGIN, "access-control-request-method": "POST"})
    assert r.headers["access-control-allow-origin"] == ORIGIN


def test_requete_trop_grosse(client):
    r = client.post("/api/graph", content=b"{" + b" " * 200_000 + b"}",
                    headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_422_sans_recopie_de_l_entree(client):
    g = copy.deepcopy(DEFAULT_GRAPH)
    g["nodes"][0]["id"] = "<script>alert(1)</script>"
    r = client.post("/api/graph", json=g)
    assert r.status_code == 422
    assert "script" not in r.text and r.json()["ok"] is False


def test_parametre_hors_bornes_message_au_format_habituel(client):
    g = copy.deepcopy(DEFAULT_GRAPH)
    g["nodes"][0]["params"]["sample_rate"] = 0
    r = client.post("/api/graph", json=g)
    assert r.status_code == 200 and r.json()["ok"] is False
    assert "Cadence" in r.json()["error"]


def test_pas_de_documentation_interactive(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert "index" in client.get(path).text


# --- fichiers statiques ------------------------------------------------------

def test_fichier_statique_servi(client):
    assert client.get("/assets/app.js").text == "console.log(1)"


@pytest.mark.parametrize("path", [
    "/%2e%2e/static-autre/secret.txt",
    "/..%2fstatic-autre%2fsecret.txt",
    "/%2fetc%2fpasswd",
    "/.cache",
])
def test_pas_de_remontee_de_chemin(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert r.text == "<html>index</html>"


# --- moteur et WebSocket -----------------------------------------------------

def test_ws_origine_etrangere_refusee(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(WS_URL, headers={"origin": "https://evil.example"}):
            pass


def test_ws_diffuse_le_spectre(client):
    assert client.post("/api/run", json={"running": True}).json()["ok"]
    with client.websocket_connect(WS_URL, headers={"origin": ORIGIN}) as ws:
        assert ws.receive_json()["type"] == "hello"
        kinds = set()
        for _ in range(40):
            m = ws.receive()
            if m.get("bytes"):
                kinds.add("trame")
            if len(kinds):
                break
        assert "trame" in kinds


def test_ws_nombre_de_clients_borne(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_WS_CLIENTS", 1)
    with client.websocket_connect(WS_URL, headers={"origin": ORIGIN}) as ws1:
        ws1.receive_json()
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(WS_URL, headers={"origin": ORIGIN}) as ws2:
                ws2.receive_json()


def test_ws_client_bavard_deconnecte(client):
    with client.websocket_connect(WS_URL, headers={"origin": ORIGIN}) as ws:
        ws.receive_json()
        for _ in range(main.WS_MSG_PER_10S + 1):
            ws.send_text("x")
        # Le serveur ferme avec 1008 (violation de politique).
        assert ws.receive() == {"type": "websocket.close", "code": 1008, "reason": ""}


def test_reglage_change_en_marche_avec_le_dongle(client, fake_lib):
    """Avant : changer un réglage en pleine écoute échouait (« dongle déjà pris »)."""
    g = {"nodes": [{"id": "src", "type": "rtlsdr_source", "params": {}},
                   {"id": "fft", "type": "fft", "params": {}},
                   {"id": "spec", "type": "spectrum_sink", "params": {}}],
         "edges": [{"from": "src", "fromPort": "out", "to": "fft", "toPort": "in"},
                   {"from": "fft", "fromPort": "out", "to": "spec", "toPort": "in"}]}
    assert client.post("/api/graph", json=g).json()["ok"]
    assert client.post("/api/run", json={"running": True}).json()["ok"]
    g["nodes"][0]["params"]["center_freq"] = 101_100_000
    r = client.post("/api/graph", json=g).json()
    assert r["ok"] and r["status"]["running"]
    assert client.post("/api/run", json={"running": False}).json()["ok"]
    assert fake_lib.opened == fake_lib.closed

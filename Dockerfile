# BlockSDR — image unique : front React buildé + backend Python, un seul port.
# Deux étages : node build le front, puis un runtime python léger qui
# sert à la fois l'API, le WebSocket et les fichiers statiques.
# Construite par la CI du repo (.github/workflows/docker-publish.yml)
# -> ghcr.io/calaxo/blocksdr:latest
#
# Images de base épinglées par DIGEST : le tag seul peut changer sous nos pieds
# d'un build à l'autre. Pour monter de version : relever le digest du nouveau
# tag (`docker buildx imagetools inspect python:3.12.x-slim-trixie`).

# --- Étage 1 : build du front ------------------------------------------------
FROM node:22.23.3-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build          # -> /fe/dist

# --- Étage 2 : runtime python ------------------------------------------------
FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS runtime
WORKDIR /app
# PYTHONDONTWRITEBYTECODE : le système de fichiers est en LECTURE SEULE en
# production ; le bytecode est compilé une fois pour toutes au build (plus bas).
#
# MALLOC_* : à chaque tour, numpy alloue puis libère des tableaux de quelques
# centaines de Kio. Au-dessus de 128 Kio, glibc les obtient par mmap et les
# rend aussitôt au noyau : appels système et défauts de page en boucle. Mesuré
# sur le vrai dongle (FM + FFT à 2,4 MS/s) : 10 % d'un cœur en temps
# « système », ramené à 2 % en gardant ces blocs dans le tas (4 Mio / 8 Mio).
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 \
    BLOCKSDR_STATIC=/app/static \
    MALLOC_MMAP_THRESHOLD_=4194304 MALLOC_TRIM_THRESHOLD_=8388608

# librtlsdr : le pilote du dongle, appelé directement par app/rtl.py (ctypes).
RUN apt-get update \
 && apt-get install -y --no-install-recommends librtlsdr0 \
 && rm -rf /var/lib/apt/lists/*

# --require-hashes : chaque paquet doit correspondre à l'empreinte sha256 du
# fichier verrouillé, dépendances transitives comprises. --only-binary : aucune
# compilation de source, donc aucun script d'installation exécuté.
COPY backend/requirements.txt .
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt

COPY backend/app ./app
COPY --from=frontend /fe/dist ./static
RUN python -m compileall -q /app/app

# Utilisateur sans privilège. L'accès au dongle passe par le GROUPE propriétaire
# des nœuds /dev/bus/usb de l'hôte, ajouté par `group_add` dans le compose.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin blocksdr
USER 10001

EXPOSE 8092
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8092/api/health', timeout=4).status == 200 else 1)"]

# --ws-max-size 4096 : le client n'envoie rien d'utile, un message de 4 Kio
#   est déjà suspect (défaut d'uvicorn : 16 Mio).
# --limit-concurrency : au-delà, 503 plutôt qu'un serveur qui s'écroule.
# --no-server-header : ne pas annoncer la version d'uvicorn.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8092", \
     "--ws", "websockets", "--ws-max-size", "4096", \
     "--limit-concurrency", "64", "--no-server-header"]

# BlockSDR — image unique : front React buildé + backend Python, un seul port.
# Même schéma que surfcast : node build le front, puis runtime python léger qui
# sert à la fois l'API, le WebSocket et les fichiers statiques.
# Construite par la CI du repo (.github/workflows/docker-publish.yml)
# -> ghcr.io/calaxo/blocksdr:latest

# --- Étage 1 : build du front ------------------------------------------------
FROM node:22-alpine AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build          # -> /fe/dist

# --- Étage 2 : runtime python ------------------------------------------------
FROM python:3.12-slim AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 BLOCKSDR_STATIC=/app/static

# librtlsdr : présente dès maintenant pour que le jour où l'on active pyrtlsdr
# et le passage USB, il n'y ait rien à changer à l'image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends librtlsdr0 \
 && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY --from=frontend /fe/dist ./static

EXPOSE 8092
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8092"]

# Image du backend BlockSDR, pour le DÉVELOPPEMENT (docker-compose.yml racine).
# L'image de production est le Dockerfile racine.
# python:3.12-slim et pas alpine : numpy y est fourni en roue précompilée,
# alors qu'alpine (musl) impose une compilation de plusieurs minutes.
FROM python:3.12.15-slim-trixie@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d

# librtlsdr : le pilote du dongle, appelé par app/rtl.py.
RUN apt-get update  && apt-get install -y --no-install-recommends librtlsdr0  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt
COPY app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

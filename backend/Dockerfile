# Image du backend BlockSDR.
# python:3.12-slim et pas alpine : numpy y est fourni en roue précompilée,
# alors qu'alpine (musl) impose une compilation de plusieurs minutes.
FROM python:3.12-slim

# librtlsdr : présente dès maintenant pour que le jour où l'on décommente
# pyrtlsdr, il n'y ait rien à changer à l'image. Sans dongle passé au
# conteneur, elle ne sert simplement à rien.
RUN apt-get update \
 && apt-get install -y --no-install-recommends librtlsdr0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python:3.12-slim

LABEL org.opencontainers.image.title="ReClip Master"
LABEL org.opencontainers.image.description="Self-hosted media downloader"
LABEL org.opencontainers.image.licenses="MIT"

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd -r reclip && useradd -r -g reclip reclip

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p downloads && chown -R reclip:reclip /app

USER reclip

EXPOSE 8899
ENV HOST=0.0.0.0
ENV FLASK_DEBUG=0

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8899/')" || exit 1

CMD ["python", "app.py"]
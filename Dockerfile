FROM jrottenberg/ffmpeg:7.0-nvidia AS ffmpeg
FROM python:3.12-slim

# ffmpeg binaries + its own bundled codec .so files come from the multi-stage base
COPY --from=ffmpeg /usr/local /usr/local
RUN /sbin/ldconfig /usr/local/lib
# System runtime deps only: VAAPI for Intel/AMD hardware accel, and curl for the
# healthcheck. All codec libs (x264/x265/vpx/fdk-aac/etc.) ship inside /usr/local
# from the ffmpeg base, so we do NOT apt-install them here.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libva2 libva-drm2 libva-x11-2 vainfo \
      libnuma1 libass9 libfreetype6 libfontconfig1 \
      curl ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

# DB + persistent state live under /data so they survive container rebuilds.
ENV PYTHONUNBUFFERED=1 \
    TRANSCODER_DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://localhost:8765/api/status > /dev/null || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]

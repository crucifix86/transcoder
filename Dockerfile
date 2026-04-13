FROM jrottenberg/ffmpeg:7.0-nvidia AS ffmpeg
FROM python:3.12-slim

# ffmpeg with GPU support from the multi-stage base
COPY --from=ffmpeg /usr/local /usr/local
# Runtime libs ffmpeg needs (VAAPI for Intel, plus codec libs), curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
      libva2 libva-drm2 libva-x11-2 vainfo intel-media-va-driver-non-free \
      libnuma1 libass9 libvorbisenc2 libvpx7 libx264-164 libx265-199 \
      libfdk-aac2 libmp3lame0 libopus0 libtheora0 libfreetype6 libfontconfig1 \
      curl \
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

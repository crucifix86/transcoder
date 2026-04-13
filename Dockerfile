# Use the nvidia-ffmpeg image directly as the base so ffmpeg + its bundled
# codec libs live in their intended spot without clashing with a second distro's
# glibc (the previous multi-stage COPY /usr/local overwrote libc.so.6).
FROM jrottenberg/ffmpeg:7.0-nvidia

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv \
      libva2 libva-drm2 libva-x11-2 vainfo \
      libnuma1 libass9 libfreetype6 libfontconfig1 \
      curl ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt
COPY app ./app

# DB + persistent state live under /data so they survive container rebuilds.
ENV PYTHONUNBUFFERED=1 \
    TRANSCODER_DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://localhost:8765/api/status > /dev/null || exit 1

# The base image's ENTRYPOINT is ffmpeg itself — override it.
ENTRYPOINT []
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]

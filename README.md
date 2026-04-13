# Simple Transcoder

A dead-simple video library transcoder. Watch folders, scan for non-matching videos, re-encode with your GPU, verify the output, and replace in place — safely.

## Why

Tdarr throws 9 million knobs at you for what should be a 6-option task. This doesn't.

## Safety model (the Tdarr-ate-my-files fix)

1. Transcode output goes to a **sibling temp file** next to the original
2. After ffmpeg exits 0, the output is **re-probed**: must exist, have a video stream, and its duration must match the original within 2%
3. Only then is the original **moved to `.transcoder-trash/`** (not deleted)
4. Then the new file is renamed into place
5. If "Keep originals" is off, trash is emptied only after the successful verified swap

If **anything** fails — ffmpeg error, verification fail, disk issue — the original is untouched.

## Run locally

```bash
cd ~/Desktop/transcoder
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# need ffmpeg + ffprobe on PATH
sudo apt install ffmpeg
uvicorn app.main:app --host 0.0.0.0 --port 8765
```

Open http://localhost:8765

## Run in Docker

```bash
docker build -t transcoder .
# Pass through NVIDIA GPU (requires nvidia-container-toolkit on host):
docker run -d --name transcoder \
  --gpus all \
  --device /dev/dri:/dev/dri \
  -p 8765:8765 \
  -v /path/to/media:/media \
  -v transcoder-data:/app \
  transcoder
```

Then put `/media` (or subfolders) in the Watch Folders box.

## Settings

- **Codec**: H.264, H.265, AV1, VP9
- **Container**: MKV, MP4, WebM
- **Encoder**: Auto (picks NVENC > QSV > VAAPI > CPU), or pin one
- **Quality**: Low / Medium / High (maps to sensible CQ/CRF values per codec)
- **Audio**: Copy (passthrough) or AAC 192k
- **Skip if match**: don't touch files already in target codec+container
- **Keep originals**: keep in `.transcoder-trash/` for manual cleanup, or purge after verify

## DB

SQLite at `transcoder.db` tracks every file seen, its status, and before/after sizes.

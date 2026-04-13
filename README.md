# Simple Transcoder

A dead-simple video library transcoder. Watch folders, scan for non-matching videos, re-encode with your GPU, verify the output, and replace in place — safely.

## Why

Tdarr throws 9 million knobs at you for what should be a 6-option task. This doesn't.

## Safety model

1. Transcode output goes to a **sibling temp file** next to the original
2. After ffmpeg exits 0, the output is **re-probed**: must exist, have a video stream, and duration must match the original within 2%
3. Only then is the original **moved to backup** (either a central `backup_path` on a different disk, or in-folder `.transcoder-trash/`)
4. The new file is renamed into place
5. If "Keep originals" is off, backup is deleted only after the successful verified swap

If **anything** fails — ffmpeg error, verification fail, disk issue — the original is untouched.

## Features

- NVIDIA NVENC, Intel QSV, AMD/Intel VAAPI, and CPU fallback
- Multi-GPU: auto-detects NVIDIA GPUs and spawns one worker per card
- Language filter: keep only the audio/sub tracks you want
- Configurable quality (Original/High/Medium/Low) with bitrate cap so h265 conversion never bloats files
- Max-height downscale with HDR→SDR tonemapping
- Folder monitoring (auto-enqueue new files)
- Skip files below minimum bitrate, or files already in target codec+container
- Schedule window (only run during quiet hours)
- Central backup destination — ideally on a different disk from source
- Crash-resume: lease-based job queue means interrupted work gets picked up automatically
- Per-show savings stats

## Run with Docker (recommended for Unraid)

```bash
docker run -d --name transcoder \
  --gpus all \
  -p 8765:8765 \
  -v /path/to/data:/data \
  -v /path/to/media:/media \
  -v /path/to/backup:/backup \
  ghcr.io/crucifix86/transcoder:latest
```

Or with docker-compose — see `docker-compose.yml` in this repo.

In the UI, add `/media` (or subfolders) as watched folders. Set backup path to `/backup`.

Images are built automatically on every push to `main`. Pull `:latest` to update.

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
sudo apt install ffmpeg  # needs ffmpeg + ffprobe on PATH
uvicorn app.main:app --host 0.0.0.0 --port 8765
```

Open http://localhost:8765

## Settings

- **Codec**: H.264, H.265, AV1, VP9
- **Container**: MKV, MP4, WebM
- **Encoder**: Auto (picks NVENC > QSV > VAAPI > CPU), or pin one
- **Quality**: Original (visually lossless) / High / Medium / Low
- **Language**: keep only a chosen language's audio + subs (plus untagged)
- **Audio**: Copy (passthrough) or AAC 192k
- **Skip if match**: don't touch files already in target codec+container
- **Keep originals**: keep backups for manual cleanup, or purge after verify
- **Backup destination**: central path (ideally different disk) or in-folder
- **Min bitrate**: skip files already below a bitrate threshold
- **Max height**: downscale taller sources (with HDR→SDR tonemap)
- **Schedule**: only run within a time window
- **Monitor interval**: how often to rescan watched folders

## Data persistence

The SQLite DB and state live in `$TRANSCODER_DATA_DIR` (defaults to `/data` in Docker, project root locally). Back this up along with your media if you care about scan history and stats.

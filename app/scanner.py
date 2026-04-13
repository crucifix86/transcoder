import os
from pathlib import Path
from . import db
from .probe import VIDEO_EXTS, probe, normalize_codec

TRASH_DIRNAME = ".transcoder-trash"
TEMP_SUFFIX = ".transcoding"
MAX_ATTEMPTS = 3

def scan_folder(root, target_codec, target_container, skip_if_match=True, min_bitrate_kbps=0):
    """Walk folder, record files, mark which need transcoding.
    Returns list of paths queued as 'pending'."""
    queued = []
    root = Path(root)
    if not root.exists():
        return queued
    for dirpath, dirnames, filenames in os.walk(root):
        # skip our own trash dirs
        dirnames[:] = [d for d in dirnames if d != TRASH_DIRNAME]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() not in VIDEO_EXTS:
                continue
            if TEMP_SUFFIX in p.name:
                continue
            path_str = str(p)
            existing = db.get_file(path_str)
            # skip if we already finished it, or it's already known/queued/in-progress
            if existing and existing["status"] in ("done", "skipped", "working", "pending"):
                continue
            # if we failed it before, leave it as error unless manually reset
            if existing and existing["status"] == "error":
                continue
            # skip files that have exceeded the retry cap
            if existing and (existing.get("attempts") or 0) >= MAX_ATTEMPTS:
                continue
            info = probe(p)
            if not info:
                db.upsert_file(path_str, status="unreadable", error="ffprobe failed",
                               container_in=p.suffix.lower().lstrip("."))
                continue
            codec_in = normalize_codec(info["video_codec"])
            container_in = p.suffix.lower().lstrip(".")
            already_target = (skip_if_match and codec_in == target_codec
                              and container_in == target_container)
            if already_target:
                db.upsert_file(path_str, status="skipped",
                               codec_in=codec_in, container_in=container_in,
                               size_in=info["size"], duration_in=info["duration"])
                continue
            # skip tiny/already-efficient files that won't save space
            if min_bitrate_kbps > 0 and info.get("bit_rate"):
                if info["bit_rate"] < min_bitrate_kbps * 1000:
                    db.upsert_file(path_str, status="skipped",
                                   error=f"below min bitrate ({info['bit_rate']//1000} kbps < {min_bitrate_kbps} kbps)",
                                   codec_in=codec_in, container_in=container_in,
                                   size_in=info["size"], duration_in=info["duration"])
                    continue
            db.upsert_file(path_str, status="pending",
                           codec_in=codec_in, container_in=container_in,
                           size_in=info["size"], duration_in=info["duration"])
            queued.append(path_str)
    return queued

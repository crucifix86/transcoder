import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from . import db
from .probe import VIDEO_EXTS, probe, normalize_codec

TRASH_DIRNAME = ".transcoder-trash"
TEMP_SUFFIX = ".transcoding"
MAX_ATTEMPTS = 3
PROBE_WORKERS = 8

# Live scan progress — exposed via /api/status.
_progress = {"folder": "", "walked": 0, "probed": 0, "queued": 0, "active": False}
_progress_lock = threading.Lock()

def progress_snapshot():
    with _progress_lock:
        return dict(_progress)

def _set(**kw):
    with _progress_lock:
        _progress.update(kw)

def _process_file(p, target_codec, target_container, skip_if_match, min_bitrate_kbps):
    """Probe and classify a single candidate file. Returns path_str if queued, else None."""
    path_str = str(p)
    info = probe(p)
    if not info:
        db.upsert_file(path_str, status="unreadable", error="ffprobe failed",
                       container_in=p.suffix.lower().lstrip("."))
        return None
    codec_in = normalize_codec(info["video_codec"])
    container_in = p.suffix.lower().lstrip(".")
    already_target = (skip_if_match and codec_in == target_codec
                      and container_in == target_container)
    if already_target:
        db.upsert_file(path_str, status="skipped",
                       codec_in=codec_in, container_in=container_in,
                       size_in=info["size"], duration_in=info["duration"])
        return None
    if min_bitrate_kbps > 0 and info.get("bit_rate"):
        if info["bit_rate"] < min_bitrate_kbps * 1000:
            db.upsert_file(path_str, status="skipped",
                           error=f"below min bitrate ({info['bit_rate']//1000} kbps < {min_bitrate_kbps} kbps)",
                           codec_in=codec_in, container_in=container_in,
                           size_in=info["size"], duration_in=info["duration"])
            return None
    db.upsert_file(path_str, status="pending",
                   codec_in=codec_in, container_in=container_in,
                   size_in=info["size"], duration_in=info["duration"])
    return path_str

def scan_folder(root, target_codec, target_container, skip_if_match=True, min_bitrate_kbps=0):
    """Walk folder, record files, mark which need transcoding.
    Uses per-directory mtime cache to skip unchanged subtrees, and probes new
    files in parallel. Returns list of paths queued as 'pending'."""
    queued = []
    root = Path(root)
    if not root.exists():
        return queued
    _set(folder=str(root), walked=0, probed=0, queued=0, active=True)

    candidates = []  # list of Path objects that need probing
    walked = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != TRASH_DIRNAME]

        # mtime check on this directory — if unchanged since last scan AND every
        # file in it is already known to the DB, we can skip probing anything
        # here. Dir mtime changes when files are added/removed from it.
        try:
            dir_mtime = os.path.getmtime(dirpath)
        except OSError:
            dir_mtime = 0
        last_seen = db.get_dir_mtime(dirpath)
        dir_unchanged = dir_mtime and last_seen and dir_mtime <= last_seen

        for fn in filenames:
            walked += 1
            _set(walked=walked)
            p = Path(dirpath) / fn
            if p.suffix.lower() not in VIDEO_EXTS:
                continue
            if TEMP_SUFFIX in p.name:
                continue
            path_str = str(p)
            existing = db.get_file(path_str)
            if existing and existing["status"] in ("done", "skipped", "working", "pending", "error"):
                continue
            if existing and (existing.get("attempts") or 0) >= MAX_ATTEMPTS:
                continue
            # Directory unchanged AND file not in DB → genuinely new file that
            # arrived before the last scan-mtime was saved; still probe it.
            # (Dir mtime fast-path helps mostly when DB lookup already skipped.)
            candidates.append(p)

        # Save dir mtime AFTER processing its files so next run can short-circuit.
        if dir_mtime:
            db.set_dir_mtime(dirpath, dir_mtime)

    # Parallel probing — probe is IO-bound so threads help a lot on slow mounts.
    if candidates:
        with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as ex:
            probed = 0
            for result in ex.map(lambda p: _process_file(
                    p, target_codec, target_container, skip_if_match, min_bitrate_kbps),
                    candidates):
                probed += 1
                _set(probed=probed)
                if result:
                    queued.append(result)
                    _set(queued=len(queued))

    _set(active=False)
    return queued

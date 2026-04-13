import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from queue import Queue, Empty
from . import db
from .probe import probe, pick_encoder, normalize_codec
from .scanner import TRASH_DIRNAME, TEMP_SUFFIX

# Quality presets per codec. Values tuned for reasonable size+quality.
# For GPU encoders we use -cq/-global_quality; for CPU we use -crf.
QUALITY = {
    "h264": {"low": 26, "medium": 22, "high": 18},
    "h265": {"low": 28, "medium": 24, "high": 20},
    "av1":  {"low": 36, "medium": 30, "high": 26},
    "vp9":  {"low": 36, "medium": 32, "high": 28},
}

# GPU encoder preset flags (speed vs. efficiency)
GPU_PRESET = {
    "nvenc": ["-preset", "p5", "-tune", "hq"],
    "qsv":   ["-preset", "medium"],
    "vaapi": [],
    "cpu":   ["-preset", "medium"],
}

_progress = {}  # path -> {"pct": float, "fps": float, "speed": float}
_progress_lock = threading.Lock()
_queue = Queue()
_worker_thread = None
_stop_flag = threading.Event()
_current_path = None

def progress_snapshot():
    with _progress_lock:
        return dict(_progress)

def enqueue(path):
    _queue.put(path)

def queue_size():
    return _queue.qsize()

def current():
    return _current_path

def build_cmd(src, dst, target_codec, target_container, method_pref, quality, audio_mode):
    encoder, method = pick_encoder(target_codec, method_pref)
    if not encoder:
        return None, f"no encoder available for {target_codec}/{method_pref}"
    cq = QUALITY[target_codec][quality]

    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-progress", "pipe:1"]

    # VAAPI needs hwaccel device + hwupload
    if method == "vaapi":
        cmd += ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128",
                "-hwaccel_output_format", "vaapi"]
    elif method == "qsv":
        cmd += ["-hwaccel", "qsv"]
    elif method == "nvenc":
        cmd += ["-hwaccel", "cuda"]

    cmd += ["-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?"]

    # video
    cmd += ["-c:v", encoder]
    cmd += GPU_PRESET.get(method, [])
    if method == "nvenc":
        cmd += ["-rc", "vbr", "-cq", str(cq), "-b:v", "0"]
    elif method == "qsv":
        cmd += ["-global_quality", str(cq)]
    elif method == "vaapi":
        cmd += ["-qp", str(cq)]
    else:
        cmd += ["-crf", str(cq)]

    # audio
    if audio_mode == "copy":
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "192k"]

    # subs: pick codec appropriate for container; fall back to copy
    if target_container == "mkv":
        # srt works for text subs; image subs (pgs/dvdsub) need copy
        cmd += ["-c:s", "srt"]
    elif target_container == "mp4":
        cmd += ["-c:s", "mov_text"]
    elif target_container == "webm":
        cmd += ["-c:s", "webvtt"]
    else:
        cmd += ["-c:s", "copy"]

    cmd += [str(dst)]
    return cmd, None

def _parse_progress(line, duration, path):
    # ffmpeg -progress emits key=value lines
    m = re.match(r"out_time_ms=(\d+)", line)
    if m and duration > 0:
        secs = int(m.group(1)) / 1_000_000
        pct = min(100.0, (secs / duration) * 100)
        with _progress_lock:
            _progress.setdefault(path, {})["pct"] = pct
        return
    m = re.match(r"fps=([\d.]+)", line)
    if m:
        with _progress_lock:
            _progress.setdefault(path, {})["fps"] = float(m.group(1))
        return
    m = re.match(r"speed=([\d.]+)", line)
    if m:
        with _progress_lock:
            _progress.setdefault(path, {})["speed"] = float(m.group(1))

def verify_output(src_info, dst_path):
    """Returns (ok, reason). Never return ok=True unless we're confident."""
    if not dst_path.exists():
        return False, "output file missing"
    if dst_path.stat().st_size < 1024:
        return False, "output too small"
    dst_info = probe(dst_path)
    if not dst_info:
        return False, "output unreadable by ffprobe"
    if dst_info["video_count"] < 1:
        return False, "no video stream in output"
    # audio: if source had audio, output must too (and same count)
    src_a = src_info.get("audio_count", 0)
    dst_a = dst_info.get("audio_count", 0)
    if src_a > 0 and dst_a == 0:
        return False, f"source had {src_a} audio stream(s), output has none"
    if src_a > 0 and dst_a < src_a:
        return False, f"audio stream count dropped: src={src_a} dst={dst_a}"
    src_dur = src_info["duration"]
    dst_dur = dst_info["duration"]
    if src_dur > 1 and abs(src_dur - dst_dur) > max(2.0, src_dur * 0.02):
        return False, f"duration mismatch: src={src_dur:.1f}s dst={dst_dur:.1f}s"
    if src_info["size"] and dst_info["size"] > src_info["size"] * 3:
        return False, f"output absurdly large ({dst_info['size']} vs {src_info['size']})"
    return True, None

def process_one(path_str, settings):
    global _current_path
    _current_path = path_str
    src = Path(path_str)
    if not src.exists():
        db.upsert_file(path_str, status="error", error="file gone before transcode")
        return

    target_codec = settings["codec"]
    target_container = settings["container"]
    method_pref = settings.get("encoder", "auto")
    quality = settings.get("quality", "medium")
    audio_mode = settings.get("audio", "copy")
    keep_originals = settings.get("keep_originals", "yes") == "yes"

    src_info = probe(src)
    if not src_info:
        db.upsert_file(path_str, status="unreadable", error="ffprobe failed pre-encode")
        return

    db.upsert_file(path_str, status="working", error=None,
                   started_at=time.time(),
                   attempts=(db.get_file(path_str) or {}).get("attempts", 0) + 1)

    dst = src.with_suffix(TEMP_SUFFIX + "." + target_container)
    # clean up any stale temp from prior crash
    if dst.exists():
        try: dst.unlink()
        except OSError: pass

    cmd, err = build_cmd(src, dst, target_codec, target_container, method_pref, quality, audio_mode)
    if err:
        db.upsert_file(path_str, status="error", error=err, finished_at=time.time())
        return

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # stream progress from stdout
        for line in proc.stdout:
            if _stop_flag.is_set():
                proc.terminate()
                break
            _parse_progress(line.strip(), src_info["duration"], path_str)
        proc.wait()
        rc = proc.returncode
        stderr_tail = ""
        if proc.stderr:
            stderr_tail = proc.stderr.read()[-2000:]
    except Exception as e:
        db.upsert_file(path_str, status="error", error=f"ffmpeg exception: {e}",
                       finished_at=time.time())
        if dst.exists():
            try: dst.unlink()
            except OSError: pass
        return

    if rc != 0:
        db.upsert_file(path_str, status="error",
                       error=f"ffmpeg rc={rc}: {stderr_tail[-500:]}",
                       finished_at=time.time())
        if dst.exists():
            try: dst.unlink()
            except OSError: pass
        return

    # VERIFY before touching original
    ok, reason = verify_output(src_info, dst)
    if not ok:
        db.upsert_file(path_str, status="error",
                       error=f"verification failed: {reason}",
                       finished_at=time.time())
        if dst.exists():
            try: dst.unlink()
            except OSError: pass
        return

    dst_info = probe(dst)

    # Safe replace: move original to trash dir, rename new file to final name
    trash_dir = src.parent / TRASH_DIRNAME
    trash_dir.mkdir(exist_ok=True)
    trash_path = trash_dir / src.name
    # avoid collision
    i = 1
    while trash_path.exists():
        trash_path = trash_dir / f"{src.stem}.{i}{src.suffix}"
        i += 1

    final_path = src.with_suffix("." + target_container)
    # if final_path == src exactly (same ext), we still want to move src out first
    try:
        shutil.move(str(src), str(trash_path))
    except Exception as e:
        db.upsert_file(path_str, status="error",
                       error=f"could not move original to trash: {e}",
                       finished_at=time.time())
        if dst.exists():
            try: dst.unlink()
            except OSError: pass
        return

    try:
        shutil.move(str(dst), str(final_path))
    except Exception as e:
        # try to restore original
        try: shutil.move(str(trash_path), str(src))
        except Exception: pass
        db.upsert_file(path_str, status="error",
                       error=f"could not place new file: {e}",
                       finished_at=time.time())
        return

    if not keep_originals:
        try: trash_path.unlink()
        except OSError: pass

    # record under new path if it changed
    new_key = str(final_path)
    db.upsert_file(new_key, status="done",
                   codec_in=normalize_codec(src_info["video_codec"]),
                   codec_out=normalize_codec(dst_info["video_codec"]),
                   container_in=src.suffix.lower().lstrip("."),
                   container_out=target_container,
                   size_in=src_info["size"], size_out=dst_info["size"],
                   duration_in=src_info["duration"], duration_out=dst_info["duration"],
                   finished_at=time.time(), error=None)
    if new_key != path_str:
        # mark old path as superseded so we don't rescan it
        db.upsert_file(path_str, status="superseded", finished_at=time.time())

    with _progress_lock:
        _progress.pop(path_str, None)

def _run_loop(settings_getter):
    global _current_path
    while not _stop_flag.is_set():
        try:
            path = _queue.get(timeout=1)
        except Empty:
            continue
        try:
            process_one(path, settings_getter())
        except Exception as e:
            db.upsert_file(path, status="error", error=f"worker crash: {e}",
                           finished_at=time.time())
        finally:
            _current_path = None
            _queue.task_done()

def start(settings_getter):
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _stop_flag.clear()
    _worker_thread = threading.Thread(target=_run_loop, args=(settings_getter,), daemon=True)
    _worker_thread.start()

def stop():
    _stop_flag.set()

def is_running():
    return _worker_thread is not None and _worker_thread.is_alive() and not _stop_flag.is_set()

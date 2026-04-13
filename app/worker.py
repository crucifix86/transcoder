import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from . import db
from .probe import probe, pick_encoder, normalize_codec, nvidia_gpu_count
from .scanner import TRASH_DIRNAME, TEMP_SUFFIX

LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 30
POLL_INTERVAL = 2
NODE_ID = f"{socket.gethostname()}:{os.getpid()}"

# Quality presets per codec. Values tuned for reasonable size+quality.
# For GPU encoders we use -cq/-global_quality; for CPU we use -crf.
QUALITY = {
    "h264": {"low": 26, "medium": 22, "high": 18, "original": 17},
    "h265": {"low": 28, "medium": 24, "high": 20, "original": 20},
    "av1":  {"low": 36, "medium": 30, "high": 26, "original": 24},
    "vp9":  {"low": 36, "medium": 32, "high": 28, "original": 24},
}

# GPU encoder preset flags (speed vs. efficiency)
GPU_PRESET = {
    "nvenc": ["-preset", "p5", "-tune", "hq"],
    "qsv":   ["-preset", "medium"],
    "vaapi": [],
    "cpu":   ["-preset", "medium"],
}

MAX_ATTEMPTS = 3

_progress = {}  # path -> {"pct": float, "fps": float, "speed": float}
_progress_lock = threading.Lock()
_worker_threads = []
_stop_flag = threading.Event()
_pause_flag = threading.Event()
_current_paths = {}  # gpu_index -> path

def progress_snapshot():
    with _progress_lock:
        return dict(_progress)

def enqueue(path):
    """Back-compat shim: scanner now writes 'pending' rows directly to the DB,
    which workers poll. Keeping this no-op so callers don't break."""
    return

def queue_size():
    # Pending rows in DB are the queue.
    counts = db.counts()
    return counts.get("pending", 0)

def current():
    # For backward-compat: first active path, else None
    if not _current_paths:
        return None
    return next(iter(_current_paths.values()), None)

def current_all():
    return dict(_current_paths)

def build_cmd(src, dst, target_codec, target_container, method_pref, quality, audio_mode, src_bitrate=0, gpu_index=0, language="all", max_height=0, is_hdr=False):
    encoder, method = pick_encoder(target_codec, method_pref)
    if not encoder:
        return None, f"no encoder available for {target_codec}/{method_pref}"
    cq = QUALITY[target_codec][quality]
    # Cap output bitrate at 85% of source so h265 conversion never bloats the file.
    # 0 means unknown — skip the cap.
    maxrate = int(src_bitrate * 0.85) if src_bitrate > 0 else 0

    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-progress", "pipe:1"]

    # HDR → SDR tonemapping needs CPU filter chain (zscale+tonemap).
    # When we detect HDR AND the user wants a downscale, decode on CPU so
    # frames can flow through the tonemap filter. Without downscale we keep
    # the HDR untouched (copy color metadata is downstream's problem).
    hdr_tonemap = is_hdr and max_height and max_height > 0

    # VAAPI needs hwaccel device + hwupload
    if method == "vaapi" and not hdr_tonemap:
        cmd += ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128",
                "-hwaccel_output_format", "vaapi"]
    elif method == "qsv" and not hdr_tonemap:
        cmd += ["-hwaccel", "qsv"]
    elif method == "nvenc" and not hdr_tonemap:
        cmd += ["-hwaccel", "cuda", "-hwaccel_device", str(gpu_index)]

    cmd += ["-i", str(src), "-map", "0:v:0"]
    if language == "all":
        cmd += ["-map", "0:a?", "-map", "0:s?"]
    else:
        # Keep tracks matching the chosen language, plus any untagged/undetermined.
        cmd += ["-map", f"0:a:m:language:{language}?",
                "-map", "0:a:m:language:und?",
                "-map", f"0:s:m:language:{language}?",
                "-map", "0:s:m:language:und?"]

    # optional downscale: only kicks in if source is taller than max_height.
    # For HDR sources we run a CPU tonemap chain so the output is proper SDR
    # bt709 — otherwise a scaled HDR file would look washed out on SDR screens.
    if max_height and max_height > 0:
        if hdr_tonemap:
            cmd += ["-vf",
                    f"zscale=t=linear:npl=100,format=gbrpf32le,"
                    f"zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
                    f"zscale=t=bt709:m=bt709:r=tv,format=yuv420p,"
                    f"scale=-2:'min({max_height},ih)'"]
        elif method == "vaapi":
            cmd += ["-vf", f"scale_vaapi=w=-2:h='min({max_height},ih)'"]
        elif method == "nvenc":
            cmd += ["-vf", f"scale_cuda=-2:'min({max_height},ih)'"]
        else:
            cmd += ["-vf", f"scale=-2:'min({max_height},ih)'"]

    # video
    cmd += ["-c:v", encoder]
    cmd += GPU_PRESET.get(method, [])
    if method == "nvenc":
        cmd += ["-rc", "vbr", "-cq", str(cq), "-b:v", "0", "-gpu", str(gpu_index)]
        if maxrate:
            cmd += ["-maxrate", str(maxrate), "-bufsize", str(maxrate * 2)]
    elif method == "qsv":
        cmd += ["-global_quality", str(cq)]
        if maxrate:
            cmd += ["-maxrate", str(maxrate), "-bufsize", str(maxrate * 2)]
    elif method == "vaapi":
        cmd += ["-qp", str(cq)]
        if maxrate:
            cmd += ["-maxrate", str(maxrate), "-bufsize", str(maxrate * 2)]
    else:
        cmd += ["-crf", str(cq)]
        if maxrate:
            cmd += ["-maxrate", str(maxrate), "-bufsize", str(maxrate * 2)]

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

def verify_output(src_info, dst_path, language_filter=False):
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
    # audio: if source had audio, output must too. When a language filter is on,
    # stream count can legitimately drop; we only fail if ALL audio was lost.
    src_a = src_info.get("audio_count", 0)
    dst_a = dst_info.get("audio_count", 0)
    if src_a > 0 and dst_a == 0:
        return False, f"source had {src_a} audio stream(s), output has none"
    if src_a > 0 and dst_a < src_a and not language_filter:
        return False, f"audio stream count dropped: src={src_a} dst={dst_a}"
    src_dur = src_info["duration"]
    dst_dur = dst_info["duration"]
    if src_dur > 1 and abs(src_dur - dst_dur) > max(2.0, src_dur * 0.02):
        return False, f"duration mismatch: src={src_dur:.1f}s dst={dst_dur:.1f}s"
    if src_info["size"] and dst_info["size"] > src_info["size"] * 3:
        return False, f"output absurdly large ({dst_info['size']} vs {src_info['size']})"
    return True, None

def process_one(path_str, settings, gpu_index=0):
    _current_paths[gpu_index] = path_str
    # Start a heartbeat so the lease doesn't expire mid-transcode; signal with
    # a stop event when the job ends, and always release the lease in finally.
    hb_stop = threading.Event()
    def _heartbeat():
        while not hb_stop.wait(HEARTBEAT_SECONDS):
            if not db.extend_lease(path_str, NODE_ID, LEASE_SECONDS):
                # We lost the lease (expired and another worker took over).
                # Bail out of the heartbeat; the outer worker will notice when
                # it finishes and discovers the row no longer belongs to us.
                return
    hb_thread = threading.Thread(target=_heartbeat, daemon=True)
    hb_thread.start()
    try:
        _process_one_body(path_str, settings, gpu_index)
    finally:
        hb_stop.set()
        db.release_lease(path_str, NODE_ID)

def _process_one_body(path_str, settings, gpu_index=0):
    # The lease already moved this row to 'working', so no need to re-check status.
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
    backup_path = (settings.get("backup_path") or "").strip()
    watched_folders = [line.strip() for line in (settings.get("folders") or "").splitlines() if line.strip()]
    language = settings.get("language", "all")
    try:
        max_height = int(settings.get("max_height", 0) or 0)
    except ValueError:
        max_height = 0

    src_info = probe(src)
    if not src_info:
        db.upsert_file(path_str, status="unreadable", error="ffprobe failed pre-encode")
        return

    # Disk-space safety: transcode output lands next to source; original moves
    # to backup. Need ~source_size free in both places (they're the same disk when
    # no central backup is configured).
    src_size = src_info.get("size") or 0
    try:
        free_target = shutil.disk_usage(str(src.parent)).free
        if src_size and free_target < src_size:
            db.upsert_file(path_str, status="error",
                           error=f"insufficient disk space at target: {free_target/1e9:.1f}GB free, need ~{src_size/1e9:.1f}GB",
                           finished_at=time.time())
            return
        if backup_path and keep_originals:
            try:
                free_backup = shutil.disk_usage(backup_path).free
                if src_size and free_backup < src_size:
                    db.upsert_file(path_str, status="error",
                                   error=f"insufficient disk space at backup: {free_backup/1e9:.1f}GB free, need ~{src_size/1e9:.1f}GB",
                                   finished_at=time.time())
                    return
            except OSError as e:
                db.upsert_file(path_str, status="error",
                               error=f"backup path unreachable: {e}",
                               finished_at=time.time())
                return
    except OSError:
        pass

    # status=working, started_at, and attempts already set by claim_next_pending.
    db.upsert_file(path_str, error=None)

    dst = src.with_suffix(TEMP_SUFFIX + "." + target_container)
    # clean up any stale temp from prior crash
    if dst.exists():
        try: dst.unlink()
        except OSError: pass

    cmd, err = build_cmd(src, dst, target_codec, target_container, method_pref, quality, audio_mode,
                         src_bitrate=src_info.get("bit_rate", 0), gpu_index=gpu_index, language=language,
                         max_height=max_height, is_hdr=bool(src_info.get("is_hdr")))
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
    ok, reason = verify_output(src_info, dst, language_filter=(language != "all"))
    if not ok:
        db.upsert_file(path_str, status="error",
                       error=f"verification failed: {reason}",
                       finished_at=time.time())
        if dst.exists():
            try: dst.unlink()
            except OSError: pass
        return

    dst_info = probe(dst)

    # Safe replace: move original to backup, rename new file to final name.
    # Backup destination is either the central backup_path (preserving folder
    # structure relative to the matching watched folder) or the legacy
    # in-folder .transcoder-trash/.
    trash_dir = None
    if backup_path:
        watched_root = None
        for wf in watched_folders:
            try:
                Path(src).resolve().relative_to(Path(wf).resolve())
                watched_root = Path(wf).resolve()
                break
            except ValueError:
                continue
        try:
            rel_parent = src.parent.resolve().relative_to(watched_root) if watched_root else Path(src.parent.name)
        except ValueError:
            rel_parent = Path(src.parent.name)
        trash_dir = Path(backup_path) / rel_parent
    else:
        trash_dir = src.parent / TRASH_DIRNAME
    try:
        trash_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        db.upsert_file(path_str, status="error",
                       error=f"could not create backup dir {trash_dir}: {e}",
                       finished_at=time.time())
        if dst.exists():
            try: dst.unlink()
            except OSError: pass
        return
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

def _in_schedule_window(settings):
    """Return True if we're allowed to run now. Respect optional quiet_start/quiet_end (24h)."""
    try:
        start = int(settings.get("schedule_start", "") or -1)
        end = int(settings.get("schedule_end", "") or -1)
    except ValueError:
        return True
    if start < 0 or end < 0 or start == end:
        return True  # no window set
    import datetime
    h = datetime.datetime.now().hour
    if start < end:
        return start <= h < end
    # wraps midnight, e.g. 22..7
    return h >= start or h < end

def _run_loop(settings_getter, gpu_index):
    while not _stop_flag.is_set():
        if _pause_flag.is_set() or not _in_schedule_window(settings_getter()):
            time.sleep(5)
            continue
        path = db.claim_next_pending(NODE_ID, LEASE_SECONDS)
        if not path:
            time.sleep(POLL_INTERVAL)
            continue
        try:
            process_one(path, settings_getter(), gpu_index=gpu_index)
        except Exception as e:
            db.upsert_file(path, status="error", error=f"worker crash: {e}",
                           finished_at=time.time())
            db.release_lease(path, NODE_ID)
        finally:
            _current_paths.pop(gpu_index, None)

def start(settings_getter):
    global _worker_threads
    if any(t.is_alive() for t in _worker_threads):
        return
    _stop_flag.clear()
    _worker_threads = []
    # Spawn one worker per NVIDIA GPU when auto/nvenc preferred; else single worker.
    method = (settings_getter().get("encoder") or "auto").lower()
    n_workers = 1
    if method in ("auto", "nvenc"):
        n_workers = max(1, nvidia_gpu_count())
    for i in range(n_workers):
        t = threading.Thread(target=_run_loop, args=(settings_getter, i), daemon=True)
        t.start()
        _worker_threads.append(t)

def stop():
    _stop_flag.set()

def pause():
    _pause_flag.set()

def resume():
    _pause_flag.clear()

def is_paused():
    return _pause_flag.is_set()

def is_running():
    return any(t.is_alive() for t in _worker_threads) and not _stop_flag.is_set()

def worker_count():
    return sum(1 for t in _worker_threads if t.is_alive())

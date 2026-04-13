import json
import threading
import time
from pathlib import Path
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from . import db, worker, scanner
from .probe import available_encoders


app = FastAPI(title="Simple Transcoder")
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

DEFAULTS = {
    "folders": "",           # newline separated
    "codec": "h265",
    "container": "mkv",
    "encoder": "auto",       # auto | nvenc | qsv | vaapi | cpu
    "quality": "original",   # original | high | medium | low
    "audio": "copy",         # copy | aac
    "language": "all",       # all | eng | spa | fre | ger | jpn | rus | chi | ita | por
    "keep_originals": "yes", # yes | no
    "skip_if_match": "yes",
    "min_bitrate_kbps": "0",  # 0 = disabled
    "max_height": "0",        # 0 = no cap; else e.g. 1080
    "schedule_start": "",     # hour 0-23, empty disables
    "schedule_end": "",
    "monitor_interval": "60", # seconds
}

_monitor_stop = threading.Event()

def _monitor_loop():
    """Periodically rescan watched folders and auto-enqueue new files."""
    while not _monitor_stop.is_set():
        try:
            s = current_settings()
            folders = [line.strip() for line in s["folders"].splitlines() if line.strip()]
            min_kbps = int(s.get("min_bitrate_kbps", "0") or 0)
            for folder in folders:
                found = scanner.scan_folder(folder, s["codec"], s["container"],
                                            skip_if_match=(s["skip_if_match"] == "yes"),
                                            min_bitrate_kbps=min_kbps)
                for p in found:
                    worker.enqueue(p)
        except Exception as e:
            print(f"monitor error: {e}")
        try:
            interval = max(10, int(current_settings().get("monitor_interval", "60") or 60))
        except ValueError:
            interval = 60
        _monitor_stop.wait(interval)

@app.on_event("startup")
def _startup():
    db.init()
    # Crash resume: anything the worker was mid-transcode on gets requeued.
    requeued = db.requeue_stuck()
    for p in requeued:
        worker.enqueue(p)
    if requeued:
        print(f"crash-resume: requeued {len(requeued)} file(s)")
    worker.start(current_settings)
    threading.Thread(target=_monitor_loop, daemon=True).start()

@app.on_event("shutdown")
def _shutdown():
    _monitor_stop.set()

def current_settings():
    s = dict(DEFAULTS)
    for k in DEFAULTS:
        v = db.get_setting(k)
        if v is not None:
            s[k] = v
    return s

@app.get("/", response_class=HTMLResponse)
def root():
    return FileResponse(STATIC_DIR / "index.html")

@app.get("/api/status")
def status():
    return {
        "counts": db.counts(),
        "queue_size": worker.queue_size(),
        "current": worker.current(),
        "current_all": worker.current_all(),
        "progress": worker.progress_snapshot(),
        "running": worker.is_running(),
        "paused": worker.is_paused(),
        "workers": worker.worker_count(),
        "encoders": available_encoders(),
        "settings": current_settings(),
    }

@app.get("/api/files")
def files(status: str | None = None, limit: int = 300):
    return db.list_files(status=status, limit=limit)

@app.post("/api/settings")
async def save_settings(
    folders: str = Form(""),
    codec: str = Form("h265"),
    container: str = Form("mkv"),
    encoder: str = Form("auto"),
    quality: str = Form("original"),
    audio: str = Form("copy"),
    language: str = Form("all"),
    keep_originals: str = Form("yes"),
    skip_if_match: str = Form("yes"),
    min_bitrate_kbps: str = Form("0"),
    max_height: str = Form("0"),
    schedule_start: str = Form(""),
    schedule_end: str = Form(""),
    monitor_interval: str = Form("60"),
):
    for k, v in dict(folders=folders, codec=codec, container=container,
                     encoder=encoder, quality=quality, audio=audio,
                     language=language,
                     keep_originals=keep_originals, skip_if_match=skip_if_match,
                     min_bitrate_kbps=min_bitrate_kbps, max_height=max_height,
                     schedule_start=schedule_start, schedule_end=schedule_end,
                     monitor_interval=monitor_interval).items():
        db.set_setting(k, v)
    return {"ok": True, "settings": current_settings()}

@app.post("/api/scan")
def scan():
    s = current_settings()
    folders = [line.strip() for line in s["folders"].splitlines() if line.strip()]
    if not folders:
        raise HTTPException(400, "No folders configured")
    min_kbps = int(s.get("min_bitrate_kbps", "0") or 0)
    queued = []
    for folder in folders:
        found = scanner.scan_folder(folder, s["codec"], s["container"],
                                    skip_if_match=(s["skip_if_match"] == "yes"),
                                    min_bitrate_kbps=min_kbps)
        for p in found:
            worker.enqueue(p)
            queued.append(p)
    return {"queued": len(queued), "sample": queued[:10]}

@app.post("/api/worker/pause")
def worker_pause():
    worker.pause()
    return {"paused": worker.is_paused()}

@app.post("/api/worker/resume")
def worker_resume():
    worker.resume()
    return {"paused": worker.is_paused()}

@app.get("/api/stats/shows")
def stats_shows():
    return db.savings_by_show()

@app.post("/api/worker/start")
def worker_start():
    worker.start(current_settings)
    return {"running": worker.is_running()}

@app.post("/api/worker/stop")
def worker_stop():
    worker.stop()
    return {"running": worker.is_running()}

@app.get("/api/browse")
def browse(path: str = ""):
    """List subdirectories of a path. Empty path returns common roots."""
    if not path:
        roots = []
        for p in [Path.home(), Path("/media"), Path("/mnt"), Path("/")]:
            if p.exists():
                roots.append({"path": str(p), "name": str(p)})
        return {"path": "", "parent": None, "dirs": roots}
    p = Path(path).resolve()
    if not p.exists() or not p.is_dir():
        raise HTTPException(400, f"not a directory: {p}")
    try:
        dirs = sorted(
            [{"path": str(d), "name": d.name} for d in p.iterdir()
             if d.is_dir() and not d.name.startswith(".")],
            key=lambda x: x["name"].lower()
        )
    except PermissionError:
        raise HTTPException(403, "permission denied")
    return {
        "path": str(p),
        "parent": str(p.parent) if p.parent != p else None,
        "dirs": dirs,
    }

@app.post("/api/reset/{path:path}")
def reset_file(path: str):
    """Mark an errored/unreadable file as pending again."""
    f = db.get_file(path)
    if not f:
        raise HTTPException(404, "not found")
    db.upsert_file(path, status="pending", error=None)
    worker.enqueue(path)
    return {"ok": True}

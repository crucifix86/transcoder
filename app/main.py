import json
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
    "quality": "medium",     # low | medium | high
    "audio": "copy",         # copy | aac
    "keep_originals": "yes", # yes | no
    "skip_if_match": "yes",
}

@app.on_event("startup")
def _startup():
    db.init()
    worker.start(current_settings)

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
        "progress": worker.progress_snapshot(),
        "running": worker.is_running(),
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
    quality: str = Form("medium"),
    audio: str = Form("copy"),
    keep_originals: str = Form("yes"),
    skip_if_match: str = Form("yes"),
):
    for k, v in dict(folders=folders, codec=codec, container=container,
                     encoder=encoder, quality=quality, audio=audio,
                     keep_originals=keep_originals, skip_if_match=skip_if_match).items():
        db.set_setting(k, v)
    return {"ok": True, "settings": current_settings()}

@app.post("/api/scan")
def scan():
    s = current_settings()
    folders = [line.strip() for line in s["folders"].splitlines() if line.strip()]
    if not folders:
        raise HTTPException(400, "No folders configured")
    queued = []
    for folder in folders:
        found = scanner.scan_folder(folder, s["codec"], s["container"],
                                    skip_if_match=(s["skip_if_match"] == "yes"))
        for p in found:
            worker.enqueue(p)
            queued.append(p)
    return {"queued": len(queued), "sample": queued[:10]}

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

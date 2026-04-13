import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "transcoder.db"
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    size_in INTEGER,
    size_out INTEGER,
    codec_in TEXT,
    codec_out TEXT,
    container_in TEXT,
    container_out TEXT,
    status TEXT NOT NULL,
    error TEXT,
    attempts INTEGER DEFAULT 0,
    started_at REAL,
    finished_at REAL,
    duration_in REAL,
    duration_out REAL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_status ON files(status);
"""

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init():
    with _lock, get_conn() as c:
        c.executescript(SCHEMA)

def upsert_file(path, **fields):
    cols = ["path"] + list(fields.keys())
    vals = [path] + list(fields.values())
    placeholders = ",".join("?" * len(cols))
    updates = ",".join(f"{k}=excluded.{k}" for k in fields.keys())
    sql = f"INSERT INTO files ({','.join(cols)}) VALUES ({placeholders}) ON CONFLICT(path) DO UPDATE SET {updates}"
    with _lock, get_conn() as c:
        c.execute(sql, vals)

def get_file(path):
    with get_conn() as c:
        r = c.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
        return dict(r) if r else None

def list_files(status=None, limit=500):
    with get_conn() as c:
        if status:
            rs = c.execute("SELECT * FROM files WHERE status=? ORDER BY finished_at DESC, path LIMIT ?", (status, limit)).fetchall()
        else:
            rs = c.execute("SELECT * FROM files ORDER BY status, path LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rs]

def counts():
    with get_conn() as c:
        rs = c.execute("SELECT status, COUNT(*) n FROM files GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rs}

def get_setting(key, default=None):
    with get_conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

def set_setting(key, value):
    with _lock, get_conn() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

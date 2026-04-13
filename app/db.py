import os
import sqlite3
import threading
from pathlib import Path

# Respect TRANSCODER_DATA_DIR so Docker can bind-mount /data for persistence.
_data_dir = Path(os.environ.get("TRANSCODER_DATA_DIR") or Path(__file__).parent.parent)
_data_dir.mkdir(parents=True, exist_ok=True)
DB_PATH = _data_dir / "transcoder.db"
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
    duration_out REAL,
    leased_by TEXT,
    lease_expires REAL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS dir_mtimes (
    dir TEXT PRIMARY KEY,
    mtime REAL NOT NULL
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
        # Additive migrations for older DBs — sqlite can't IF NOT EXISTS columns,
        # so catch the duplicate-column error and move on.
        for col, ddl in [("leased_by", "TEXT"), ("lease_expires", "REAL")]:
            try:
                c.execute(f"ALTER TABLE files ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError:
                pass

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

def get_dir_mtime(d):
    with get_conn() as c:
        r = c.execute("SELECT mtime FROM dir_mtimes WHERE dir=?", (d,)).fetchone()
        return r["mtime"] if r else 0.0

def set_dir_mtime(d, mtime):
    with _lock, get_conn() as c:
        c.execute("INSERT INTO dir_mtimes(dir,mtime) VALUES(?,?) ON CONFLICT(dir) DO UPDATE SET mtime=excluded.mtime",
                  (d, mtime))

def savings_by_show():
    """Group done rows by parent folder (show/season) and compute savings."""
    with get_conn() as c:
        rs = c.execute("""
            SELECT path, size_in, size_out FROM files
            WHERE status='done' AND size_in > 0 AND size_out > 0
        """).fetchall()
    groups = {}
    for r in rs:
        # use parent dir as the show key
        parent = str(Path(r["path"]).parent)
        # trim parent to last 2 path components for a friendlier label
        parts = parent.rstrip("/").split("/")
        label = "/".join(parts[-2:]) if len(parts) >= 2 else parent
        g = groups.setdefault(label, {"count": 0, "size_in": 0, "size_out": 0})
        g["count"] += 1
        g["size_in"] += r["size_in"]
        g["size_out"] += r["size_out"]
    return [{"show": k, **v, "saved": v["size_in"] - v["size_out"],
             "pct": (1 - v["size_out"]/v["size_in"]) * 100 if v["size_in"] else 0}
            for k, v in sorted(groups.items())]

def claim_next_pending(node_id, lease_seconds=120):
    """Atomically claim the oldest pending file (or one with an expired lease).
    Returns the path string, or None if nothing to claim.
    Sets status='working', leased_by, lease_expires."""
    import time as _time
    now = _time.time()
    new_expires = now + lease_seconds
    with _lock, get_conn() as c:
        # Eligible: pending, OR working with an expired lease (dead worker).
        row = c.execute("""
            SELECT path FROM files
            WHERE status='pending'
               OR (status='working' AND (lease_expires IS NULL OR lease_expires < ?))
            ORDER BY
              CASE WHEN status='pending' THEN 0 ELSE 1 END,
              COALESCE(started_at, 0),
              path
            LIMIT 1
        """, (now,)).fetchone()
        if not row:
            return None
        path = row["path"]
        # Claim it. If someone else raced us, our UPDATE still wins because
        # we're single-writer under _lock; but we still guard with a WHERE that
        # re-checks the claim predicate in case the row moved between select and update.
        c.execute("""
            UPDATE files
            SET status='working', leased_by=?, lease_expires=?, started_at=?,
                attempts=COALESCE(attempts,0)+1
            WHERE path=? AND (status='pending'
                              OR (status='working' AND (lease_expires IS NULL OR lease_expires < ?)))
        """, (node_id, new_expires, now, path, now))
        if c.total_changes:
            return path
        return None

def extend_lease(path, node_id, seconds=120):
    """Heartbeat — push the lease forward. Returns True if we still hold it."""
    import time as _time
    new_expires = _time.time() + seconds
    with _lock, get_conn() as c:
        cur = c.execute(
            "UPDATE files SET lease_expires=? WHERE path=? AND leased_by=? AND status='working'",
            (new_expires, path, node_id),
        )
        return cur.rowcount > 0

def release_lease(path, node_id):
    """Drop the lease fields without touching status (caller sets done/error)."""
    with _lock, get_conn() as c:
        c.execute(
            "UPDATE files SET leased_by=NULL, lease_expires=NULL WHERE path=? AND leased_by=?",
            (path, node_id),
        )

def requeue_stuck():
    """On startup: convert any orphaned 'working' rows back to 'pending' and drop
    their leases so another worker can pick them up. This covers the case where
    the server itself crashed (not just the ffmpeg process)."""
    with _lock, get_conn() as c:
        c.execute("""UPDATE files SET status='pending', error='requeued after restart',
                     leased_by=NULL, lease_expires=NULL WHERE status='working'""")
        rs = c.execute("SELECT path FROM files WHERE status='pending'").fetchall()
        return [r["path"] for r in rs]

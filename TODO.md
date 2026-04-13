# Transcoder TODO

## Safety rails
- [x] Crash resume: on startup, requeue any rows stuck in "working"
- [x] Disk-space check before transcode (free >= source size, abort if not)
- [x] Per-file retry cap (fail permanently after 3 attempts)
- [x] Configurable backup destination — central backup path preserves folder
      structure and includes free-space check. Ideal when placed on a different
      physical disk than the source.
- [ ] Lock file on watch folder (deferred — not needed for single-instance use)

## Features
- [x] Pause/resume button (graceful: finish current, stop next)
- [x] Schedule window ("only run between H1-H2")
- [x] Min-bitrate skip (auto-skip files below X kbps)
- [x] Max-resolution cap (downscale taller sources)
- [x] Per-show savings stats view
- [x] Configurable monitor interval
- [x] Folder monitoring (auto-enqueue new files)

## Big-library scaling (important for Unraid / 50k+ file libraries)
- [x] mtime cache per directory — stored in dir_mtimes table
- [x] Parallel probing (8 concurrent ffprobes) for first-scan ingest
- [x] Rotating incremental scan — one folder per monitor tick
- [x] Progress indicator for long first-scans (walked/probed/queued live in UI)
- [ ] Lazy probe — enqueue by path, probe inside worker (may not be needed
      now that parallel probing exists; revisit if first-scan is still slow
      on huge libraries)

## Distributed transcoding (Tdarr-style nodes) — future
- [ ] Worker-node mode — deferred until after Docker/Unraid packaging. Groundwork
      already in place: DB-based job lease with heartbeat and auto-reclaim of
      expired leases (see claim_next_pending/extend_lease/release_lease in db.py).
      To finish: extract `do_transcode_job()` as a DB-free helper, add
      /api/node/{claim,heartbeat,complete} endpoints with bearer-token auth,
      write a standalone `app/node.py` process with path-mapping, and surface
      connected nodes in the UI.

## Deferred
- Plex/Jellyfin refresh webhook (outgoing, on completion)
- Sonarr/Radarr import webhook (incoming)

## Done (polish)
- [x] HDR→SDR tonemapping when downscaling — CPU zscale+tonemap chain kicks
      in when source is PQ/HLG and max_height is set; output is bt709 SDR.

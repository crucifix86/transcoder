# Transcoder TODO

## Safety rails
- [x] Crash resume: on startup, requeue any rows stuck in "working"
- [x] Disk-space check before transcode (free >= source size, abort if not)
- [x] Per-file retry cap (fail permanently after 3 attempts)
- [ ] Configurable backup destination — instead of `.transcoder-trash/` inside the
      watched folder, let users set a central backup path, ideally on a DIFFERENT
      physical disk. Protects against cases where the source disk itself goes bad
      mid-run (power loss corrupting the FS, a dying drive, etc.) — if backup lives
      on a separate disk it can't be taken down with the source. Include free-space
      check on the backup path too. Makes one-folder-at-a-time workflow much safer.
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
- [ ] mtime cache per folder — skip files whose mtime predates last scan
- [ ] Parallel probing (e.g. 8 concurrent ffprobes) to speed first-scan ingest
- [ ] Rotating incremental scan — one folder per monitor tick instead of all
- [ ] Lazy probe — enqueue by path, probe inside worker; makes scan non-blocking
- [ ] Progress indicator for long first-scans (UI currently gives no feedback)

## Deferred
- Plex/Jellyfin refresh webhook (outgoing, on completion)
- Sonarr/Radarr import webhook (incoming)
- HDR→SDR tonemapping when downscaling (currently scales HDR without tonemapping)

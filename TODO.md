# Transcoder TODO

## Safety rails
- [x] Crash resume: on startup, requeue any rows stuck in "working"
- [x] Disk-space check before transcode (free >= source size, abort if not)
- [x] Per-file retry cap (fail permanently after 3 attempts)
- [ ] Lock file on watch folder (deferred — not needed for single-instance use)

## Features
- [x] Pause/resume button (graceful: finish current, stop next)
- [x] Schedule window ("only run between H1-H2")
- [x] Min-bitrate skip (auto-skip files below X kbps)
- [x] Max-resolution cap (downscale taller sources)
- [x] Per-show savings stats view
- [x] Configurable monitor interval
- [x] Folder monitoring (auto-enqueue new files)

## Deferred
- Plex/Jellyfin refresh webhook (outgoing, on completion)
- Sonarr/Radarr import webhook (incoming)
- HDR→SDR tonemapping when downscaling (currently scales HDR without tonemapping)

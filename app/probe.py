import json
import subprocess
from functools import lru_cache

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm",
              ".m4v", ".mpg", ".mpeg", ".ts", ".m2ts", ".vob", ".3gp"}

# codec tag -> ffmpeg encoder family
CODEC_ENCODERS = {
    "h264": {"nvenc": "h264_nvenc", "qsv": "h264_qsv", "vaapi": "h264_vaapi", "cpu": "libx264"},
    "h265": {"nvenc": "hevc_nvenc", "qsv": "hevc_qsv", "vaapi": "hevc_vaapi", "cpu": "libx265"},
    "av1":  {"nvenc": "av1_nvenc",  "qsv": "av1_qsv",  "vaapi": "av1_vaapi",  "cpu": "libsvtav1"},
    "vp9":  {"cpu": "libvpx-vp9"},
}

# codec name as reported by ffprobe -> our internal codec key
CODEC_NAME_MAP = {
    "h264": "h264", "avc1": "h264",
    "hevc": "h265", "h265": "h265",
    "av1": "av1",
    "vp9": "vp9",
    "mpeg4": "mpeg4", "mpeg2video": "mpeg2", "msmpeg4v3": "mpeg4",
    "wmv3": "wmv", "vc1": "vc1",
}

def _test_encoder(encoder, hwaccel=None):
    """Actually try encoding 1 frame to prove the GPU backend works at runtime."""
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi",
           "-i", "nullsrc=s=256x256:d=0.1", "-frames:v", "1"]
    if hwaccel == "vaapi":
        cmd = ["ffmpeg", "-hide_banner", "-v", "error",
               "-vaapi_device", "/dev/dri/renderD128",
               "-f", "lavfi", "-i", "nullsrc=s=256x256:d=0.1",
               "-vf", "format=nv12,hwupload", "-frames:v", "1"]
    cmd += ["-c:v", encoder, "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

@lru_cache(maxsize=1)
def nvidia_gpu_count():
    """Return number of NVENC-capable NVIDIA GPUs present, or 0."""
    try:
        r = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return 0
        return sum(1 for line in r.stdout.splitlines() if line.strip().startswith("GPU "))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0

@lru_cache(maxsize=1)
def available_encoders():
    """Return dict of {codec: [methods...]} filtered to encoders that actually work."""
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=10).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    # First pass: find what ffmpeg claims to support
    candidates = {}
    for codec, methods in CODEC_ENCODERS.items():
        candidates[codec] = [(m, enc) for m, enc in methods.items()
                             if f" {enc} " in out]
    # Second pass: runtime-test each GPU method ONCE per family
    # (cpu always works; we assume)
    method_works = {"cpu": True}
    # test each GPU method on the first h264 codec that has it (cheapest)
    for method in ("nvenc", "qsv", "vaapi"):
        enc = CODEC_ENCODERS["h264"].get(method)
        if enc and f" {enc} " in out:
            method_works[method] = _test_encoder(enc, hwaccel=method)
        else:
            method_works[method] = False
    result = {}
    for codec, pairs in candidates.items():
        working = [m for m, _ in pairs if method_works.get(m)]
        if working:
            result[codec] = working
    return result

def probe(path):
    """Return dict with duration, video_codec, audio_codec, or None if unreadable."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=30
        )
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout)
    except Exception:
        return None
    v_codec = None
    a_codec = None
    video_count = 0
    audio_count = 0
    subtitle_count = 0
    for s in data.get("streams", []):
        t = s.get("codec_type")
        if t == "video":
            # skip attached cover art
            if s.get("disposition", {}).get("attached_pic"):
                continue
            video_count += 1
            if v_codec is None:
                v_codec = s.get("codec_name")
        elif t == "audio":
            audio_count += 1
            if a_codec is None:
                a_codec = s.get("codec_name")
        elif t == "subtitle":
            subtitle_count += 1
    try:
        duration = float(data["format"]["duration"])
    except (KeyError, ValueError):
        duration = 0.0
    size = int(data.get("format", {}).get("size", 0))
    try:
        bit_rate = int(data["format"].get("bit_rate") or 0)
    except ValueError:
        bit_rate = 0
    if not bit_rate and duration > 0 and size:
        bit_rate = int(size * 8 / duration)
    return {
        "duration": duration,
        "video_codec": v_codec,
        "audio_codec": a_codec,
        "video_count": video_count,
        "audio_count": audio_count,
        "subtitle_count": subtitle_count,
        "size": size,
        "bit_rate": bit_rate,
    }

def normalize_codec(name):
    if not name:
        return None
    return CODEC_NAME_MAP.get(name.lower(), name.lower())

def pick_encoder(codec, method_pref):
    """method_pref: 'auto' or specific. Returns (ffmpeg_encoder_name, actual_method) or (None, None)."""
    avail = available_encoders().get(codec, [])
    if not avail:
        return None, None
    if method_pref == "auto":
        for m in ("nvenc", "qsv", "vaapi", "cpu"):
            if m in avail:
                return CODEC_ENCODERS[codec][m], m
        return None, None
    if method_pref in avail:
        return CODEC_ENCODERS[codec][method_pref], method_pref
    return None, None

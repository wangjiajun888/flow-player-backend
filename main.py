import os, tempfile, subprocess, shutil, uuid, stat
import urllib.request, tarfile
import glob as _glob
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="Flow Player Backend", version="1.0.0")
origins = os.getenv("ALLOWED_ORIGINS", "*")
app.add_middleware(CORSMiddleware, allow_origins=origins.split(",") if origins != "*" else ["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")
MAX_DURATION = int(os.getenv("MAX_VIDEO_DURATION", "600"))

class TranscribeReq(BaseModel):
    url: str
    api_key: str = ""
    cookies: str = ""  # Netscape cookie format for platforms needing login

class TranscribeResp(BaseModel):
    text: str = ""
    duration: float = 0
    success: bool = True
    error: str = ""


_FFMPEG_CACHED = None

def ensure_ffmpeg():
    """Find or download ffmpeg binary. Returns path or None."""
    global _FFMPEG_CACHED
    if _FFMPEG_CACHED:
        return _FFMPEG_CACHED

    # 1) Already in PATH
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        _FFMPEG_CACHED = ffmpeg
        return ffmpeg

    # 2) Already downloaded locally
    local_bin = os.path.join(tempfile.gettempdir(), "fp_bin")
    ffmpeg = os.path.join(local_bin, "ffmpeg")
    if os.path.isfile(ffmpeg) and os.access(ffmpeg, os.X_OK):
        _FFMPEG_CACHED = ffmpeg
        return ffmpeg

    # 3) Search common paths
    for p in [
        "/nix/var/nix/profiles/default/bin/ffmpeg",
        "/home/railway/.nix-profile/bin/ffmpeg",
        "/root/.nix-profile/bin/ffmpeg",
        "/run/current-system/sw/bin/ffmpeg",
    ]:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            _FFMPEG_CACHED = p
            return p

    for p in _glob.glob("/nix/store/*/bin/ffmpeg"):
        if os.path.isfile(p):
            _FFMPEG_CACHED = p
            return p

    # 4) Download static build
    try:
        os.makedirs(local_bin, exist_ok=True)
        url = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
        tar_path = os.path.join(local_bin, "ffmpeg.tar.xz")
        urllib.request.urlretrieve(url, tar_path)
        with tarfile.open(tar_path, "r:xz") as tar:
            for member in tar.getmembers():
                name = os.path.basename(member.name)
                if name in ("ffmpeg", "ffprobe"):
                    member.name = name
                    tar.extract(member, local_bin)
        os.chmod(ffmpeg, os.stat(ffmpeg).st_mode | stat.S_IEXEC)
        os.unlink(tar_path)
        _FFMPEG_CACHED = ffmpeg
        return ffmpeg
    except Exception:
        return None

def check_tools():
    if not shutil.which("yt-dlp"):
        raise RuntimeError("yt-dlp not found")
    ffmpeg_path = ensure_ffmpeg()
    if not ffmpeg_path:
        raise RuntimeError("ffmpeg not found and download failed")

def download_video(url, outdir, cookies=""):
    """Download video using yt-dlp. Returns (filepath, duration)."""
    tmpl = os.path.join(outdir, "%(title)s.%(ext)s")
    cmd = [
        "yt-dlp", "-f", "best[height<=720]/best[ext=mp4]/best",
        "--no-playlist", "--restrict-filenames",
        "--user-agent", "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "-o", tmpl,
    ]
    # Write cookies to temp file if provided
    cookie_file = None
    if cookies and cookies.strip():
        cookie_file = os.path.join(outdir, "cookies.txt")
        with open(cookie_file, "w", encoding="utf-8") as cf:
            cf.write(cookies.strip())
        cmd.extend(["--cookies", cookie_file])
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=outdir)

    # Check for downloaded files regardless of return code
    video_exts = [".mp4", ".webm", ".mkv", ".mov", ".flv", ".m4a", ".mp3"]
    found = []
    for f in os.listdir(outdir):
        ext = os.path.splitext(f)[1].lower()
        if ext in video_exts:
            found.append(os.path.join(outdir, f))

    if not found:
        detail = (r.stderr + "\n" + r.stdout)[-600:]
        raise RuntimeError("Download failed, no video file produced. Output: " + detail)

    vpath = found[0]
    # Try to get duration from yt-dlp output
    dur = 0.0
    for line in (r.stdout + r.stderr).split("\n"):
        if "duration" in line.lower():
            try:
                import re
                m = re.search(r"duration[:\s]*(\d+\.?\d*)", line, re.IGNORECASE)
                if m:
                    dur = float(m.group(1))
            except Exception:
                pass
    return vpath, dur

def extract_audio(vpath, outdir):
    apath = os.path.join(outdir, "audio.mp3")
    r = subprocess.run([
        ensure_ffmpeg() or "ffmpeg", "-i", vpath, "-vn", "-acodec", "libmp3lame",
        "-ar", "16000", "-ac", "1", "-b:a", "64k", "-y", apath
    ], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + r.stderr[-300:])
    return apath

def transcribe(apath, api_key):
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    with open(apath, "rb") as f:
        t = client.audio.transcriptions.create(
            model=WHISPER_MODEL, file=f,
            response_format="verbose_json",
            timestamp_granularities=["segment"]
        )
    lines = []
    for seg in t.segments:
        s = int(seg.get("start", 0) or 0)
        m, sec = divmod(s, 60)
        lines.append(f"[{m:02d}:{sec:02d}] {seg['text'].strip()}")
    return "\n".join(lines)


@app.get("/api/debug")
async def debug():
    import sys
    return {
        "python": sys.version,
        "yt_dlp": shutil.which("yt-dlp") or "NOT FOUND",
        "ffmpeg_path": shutil.which("ffmpeg") or "NOT IN PATH",
        "ffmpeg_found": ensure_ffmpeg() or "NOT FOUND",
        "PATH_dirs": os.environ.get("PATH", "").split(":")[:5],
        "platform": sys.platform,
    }

@app.get("/api/health")
async def health():
    return {"status": "ok"}

@app.post("/api/transcribe", response_model=TranscribeResp)
async def transcribe_video(req: TranscribeReq):
    if not req.url or not req.url.strip():
        raise HTTPException(400, "Missing video URL")
    # API key is passed by client per request
    tmp = tempfile.mkdtemp(prefix="fp_")
    try:
        check_tools()
        vpath, dur = download_video(req.url.strip(), tmp, req.cookies)
        apath = extract_audio(vpath, tmp)
        text = transcribe(apath, req.api_key or OPENAI_API_KEY)
        return TranscribeResp(text=text, duration=dur, success=True)
    except RuntimeError as e:
        return TranscribeResp(success=False, error=str(e))
    except Exception as e:
        return TranscribeResp(success=False, error="Error: " + str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
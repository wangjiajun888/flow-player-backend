import os, tempfile, subprocess, shutil, uuid
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

class TranscribeResp(BaseModel):
    text: str = ""
    duration: float = 0
    success: bool = True
    error: str = ""

def check_tools():
    if not shutil.which("yt-dlp"):
        raise RuntimeError("yt-dlp not found")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found")

def download_video(url, outdir):
    tmpl = os.path.join(outdir, "%(id)s.%(ext)s")
    r = subprocess.run([
        "yt-dlp", "-f", "best[height<=720]/best",
        "--max-duration", str(MAX_DURATION), "--no-playlist",
        "--restrict-filenames", "-o", tmpl,
        "--print", "filename", "--print", "duration", url
    ], capture_output=True, text=True, timeout=120, cwd=outdir)
    if r.returncode != 0:
        raise RuntimeError("Download failed: " + r.stderr[-300:])
    lines = r.stdout.strip().split("\n")
    if len(lines) < 2:
        raise RuntimeError("No video info")
    vpath = lines[-2].strip()
    dur = float(lines[-1].strip()) if len(lines) >= 2 else 0.0
    if not os.path.exists(vpath):
        for f in os.listdir(outdir):
            if os.path.splitext(f)[1].lower() in [".mp4",".webm",".mkv",".mov",".flv"]:
                vpath = os.path.join(outdir, f)
                break
    if not os.path.exists(vpath):
        raise RuntimeError("Video file not found: " + vpath)
    return vpath, dur

def extract_audio(vpath, outdir):
    apath = os.path.join(outdir, "audio.mp3")
    r = subprocess.run([
        "ffmpeg", "-i", vpath, "-vn", "-acodec", "libmp3lame",
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
        vpath, dur = download_video(req.url.strip(), tmp)
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
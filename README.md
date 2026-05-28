# Flow Player Backend

Video link to transcript service. Powered by yt-dlp + ffmpeg + OpenAI Whisper.

## API

- `POST /api/transcribe` - Accepts `{"url":"..."}`, returns transcript with timestamps
- `GET /api/health` - Health check

## Deploy on Railway

1. Push this repo to GitHub
2. Connect to Railway, set env vars:
   - `OPENAI_API_KEY` - OpenAI API Key for Whisper
   - `ALLOWED_ORIGINS` - CORS origins (default `*`)
3. Deploy

## Local

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```
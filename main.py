import os
import re
import uuid
import base64
import shutil
import tempfile
from typing import Optional, List
from pathlib import Path

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

app = FastAPI(title="VeloMate High-Speed Media Engine", version="1.0.0")

# Allow frontend web app to connect directly
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Length", "Content-Range", "Accept-Ranges"],
)

COOKIES_FILE = Path("cookies.txt")
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "velomate_downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

REALISTIC_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class ExtractRequest(BaseModel):
    url: str
    cookies_b64: Optional[str] = None
    anti_bot_profile: str = "ios_android_web"
    po_token: Optional[str] = None


class CookiesRequest(BaseModel):
    cookies_text: str


def detect_platform(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "facebook.com" in u or "fb.watch" in u or "fb.com" in u:
        return "facebook"
    if "instagram.com" in u or "instagr.am" in u:
        return "instagram"
    if "tiktok.com" in u:
        return "tiktok"
    if "twitter.com" in u or "x.com" in u:
        return "twitter"
    if "vimeo.com" in u:
        return "vimeo"
    return "web"


def write_temp_cookies(cookies_b64: Optional[str]) -> Optional[str]:
    if cookies_b64:
        try:
            decoded = base64.b64decode(cookies_b64).decode("utf-8", errors="ignore")
            if decoded.strip():
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w", encoding="utf-8")
                tmp.write(decoded)
                tmp.close()
                return tmp.name
        except Exception:
            pass
    if COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 10:
        return str(COOKIES_FILE)
    return None


def build_ydl_opts(
    cookie_path: Optional[str],
    anti_bot_profile: str = "ios_android_web",
    po_token: Optional[str] = None,
    turbo: bool = True,
) -> dict:
    # Anti-Bot client fallback strategy so YouTube treats requests like real mobile/browser clients
    if anti_bot_profile == "android_vr":
        clients = ["android_vr", "ios", "web"]
    elif anti_bot_profile == "web_cookies":
        clients = ["web", "mweb", "ios"]
    else:
        clients = ["ios", "android", "mweb", "web"]

    youtube_args = {
        "player_client": clients,
        "player_skip": ["configs"],
    }
    if po_token and po_token.strip():
        youtube_args["po_token"] = [f"web+{po_token.strip()}"]

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "cachedir": False,
        "retries": 6,
        "fragment_retries": 12,
        "socket_timeout": 20,
        "concurrent_fragment_downloads": 16 if turbo else 4,
        "http_headers": {
            "User-Agent": REALISTIC_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Mode": "navigate",
        },
        "extractor_args": {
            "youtube": youtube_args,
        },
    }

    if cookie_path:
        opts["cookiefile"] = cookie_path

    # Use aria2c 16-connection acceleration when available on server
    if turbo and shutil.which("aria2c"):
        opts["external_downloader"] = "aria2c"
        opts["external_downloader_args"] = {
            "default": ["-x", "16", "-s", "16", "-k", "1M", "--file-allocation=none"]
        }

    return opts


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "ytdlp_version": getattr(yt_dlp.version, "__version__", "unknown"),
        "ffmpeg_installed": bool(shutil.which("ffmpeg")),
        "aria2c_installed": bool(shutil.which("aria2c")),
        "cookies_loaded": COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 10,
    }


@app.post("/api/cookies")
def save_cookies(req: CookiesRequest):
    content = req.cookies_text.strip()
    if not content or (
        "youtube.com" not in content
        and "facebook.com" not in content
        and "instagram.com" not in content
        and "#" not in content
    ):
        raise HTTPException(status_code=400, detail="Invalid Netscape cookies.txt content.")
    COOKIES_FILE.write_text(content, encoding="utf-8")
    return {"ok": True, "bytes": len(content)}


@app.post("/api/extract")
def extract_media(
    req: ExtractRequest,
    x_yt_cookies: Optional[str] = Header(default=None),
):
    cookie_path = write_temp_cookies(req.cookies_b64 or x_yt_cookies)
    is_temp = bool(cookie_path and cookie_path != str(COOKIES_FILE))

    try:
        opts = build_ydl_opts(
            cookie_path=cookie_path,
            anti_bot_profile=req.anti_bot_profile,
            po_token=req.po_token,
            turbo=False,
        )
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(req.url, download=False)
            if "entries" in info and info["entries"]:
                info = info["entries"][0]

        platform = detect_platform(req.url)
        raw_formats = info.get("formats") or []
        formats: List[dict] = []
        seen_heights = set()

        # 1. Progressive formats (Direct fast video+audio stream)
        progressive = [
            f for f in raw_formats
            if f.get("vcodec") != "none" and f.get("acodec") != "none" and f.get("url")
        ]
        progressive.sort(key=lambda f: f.get("height") or 0, reverse=True)

        # 2. High-res adaptive video formats (1080p, 1440p, 4K)
        video_only = [
            f for f in raw_formats
            if f.get("vcodec") != "none" and f.get("acodec") == "none" and (f.get("height") or 0) >= 480
        ]
        video_only.sort(key=lambda f: f.get("height") or 0, reverse=True)

        for f in video_only:
            h = f.get("height")
            if not h or h in seen_heights or h > 2160:
                continue
            seen_heights.add(h)
            size = f.get("filesize") or f.get("filesize_approx")
            formats.append({
                "format_id": f"{f['format_id']}+bestaudio/best",
                "label": f"{h}p {'4K' if h >= 2160 else 'HD'} MP4 (Fast Merge)",
                "ext": "mp4",
                "resolution": f"{h}p",
                "filesize_mb": round(size / (1024 * 1024), 1) if size else None,
                "has_audio": True,
                "has_video": True,
                "url": None,
            })

        for f in progressive:
            h = f.get("height") or 720
            size = f.get("filesize") or f.get("filesize_approx")
            formats.append({
                "format_id": str(f["format_id"]),
                "label": f"{h}p MP4 (Direct CDN Stream)",
                "ext": f.get("ext") or "mp4",
                "resolution": f"{h}p",
                "filesize_mb": round(size / (1024 * 1024), 1) if size else None,
                "has_audio": True,
                "has_video": True,
                "url": f.get("url"),
            })

        # 3. Audio-only format (MP3/M4A)
        audio_only = [
            f for f in raw_formats
            if f.get("vcodec") == "none" and f.get("acodec") != "none" and f.get("url")
        ]
        audio_only.sort(key=lambda f: f.get("abr") or 0, reverse=True)
        if audio_only:
            best_a = audio_only[0]
            size = best_a.get("filesize") or best_a.get("filesize_approx")
            formats.append({
                "format_id": "bestaudio/best",
                "label": "High Quality Audio (MP3 / M4A)",
                "ext": "mp3",
                "resolution": "Audio",
                "filesize_mb": round(size / (1024 * 1024), 1) if size else None,
                "has_audio": True,
                "has_video": False,
                "url": best_a.get("url"),
            })

        if not formats and info.get("url"):
            formats.append({
                "format_id": "best",
                "label": "Best Quality MP4",
                "ext": info.get("ext") or "mp4",
                "resolution": "HD",
                "filesize_mb": None,
                "has_audio": True,
                "has_video": True,
                "url": info.get("url"),
            })

        direct_stream = next((f["url"] for f in progressive if f.get("url")), info.get("url"))

        return {
            "id": info.get("id") or str(uuid.uuid4()),
            "url": req.url,
            "platform": platform,
            "title": info.get("title") or "Extracted Media",
            "author": info.get("uploader") or info.get("channel") or platform.capitalize(),
            "thumbnail": info.get("thumbnail") or "",
            "duration": info.get("duration"),
            "directStreamUrl": direct_stream,
            "source": "python-api",
            "formats": formats[:10],
        }
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    finally:
        if is_temp and cookie_path and os.path.exists(cookie_path):
            os.remove(cookie_path)


@app.get("/api/stream")
async def stream_media(
    request: Request,
    url: str = Query(...),
    format_id: str = Query(default="best"),
    cookies_b64: Optional[str] = Query(default=None),
):
    """High-speed proxy stream with HTTP Range seeking support."""
    cookie_path = write_temp_cookies(cookies_b64)
    is_temp = bool(cookie_path and cookie_path != str(COOKIES_FILE))
    try:
        opts = build_ydl_opts(cookie_path=cookie_path, turbo=False)
        opts["format"] = format_id
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            direct_url = info.get("url")
            if not direct_url and info.get("requested_formats"):
                direct_url = info["requested_formats"][0].get("url")
            if not direct_url:
                raise HTTPException(status_code=404, detail="No direct stream URL found.")

        headers = {"User-Agent": REALISTIC_USER_AGENT}
        if "range" in request.headers:
            headers["Range"] = request.headers["range"]

        client = httpx.AsyncClient(http2=True, follow_redirects=True, timeout=None)
        req_upstream = client.build_request("GET", direct_url, headers=headers)
        upstream = await client.send(req_upstream, stream=True)

        resp_headers = {}
        for h in ["content-type", "content-length", "content-range", "accept-ranges"]:
            if h in upstream.headers:
                resp_headers[h] = upstream.headers[h]

        async def iter_chunks():
            try:
                async for chunk in upstream.aiter_bytes(chunk_size=524288):
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            iter_chunks(),
            status_code=upstream.status_code,
            headers=resp_headers,
            media_type=upstream.headers.get("content-type", "video/mp4"),
        )
    finally:
        if is_temp and cookie_path and os.path.exists(cookie_path):
            os.remove(cookie_path)


def cleanup_files(paths: List[str]):
    for p in paths:
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


@app.get("/api/download")
def download_media(
    url: str = Query(...),
    format_id: str = Query(default="bestvideo+bestaudio/best"),
    ext: str = Query(default="mp4"),
    turbo: bool = Query(default=True),
    anti_bot_profile: str = Query(default="ios_android_web"),
    cookies_b64: Optional[str] = Query(default=None),
):
    """Fast multi-threaded download using aria2c + 16 concurrent fragments + ffmpeg faststart."""
    job_id = str(uuid.uuid4())[:8]
    out_template = str(DOWNLOAD_DIR / f"{job_id}_%(title).80s.%(ext)s")
    cookie_path = write_temp_cookies(cookies_b64)
    is_temp = bool(cookie_path and cookie_path != str(COOKIES_FILE))

    opts = build_ydl_opts(cookie_path=cookie_path, anti_bot_profile=anti_bot_profile, turbo=turbo)
    opts["format"] = format_id
    opts["outtmpl"] = out_template

    if ext == "mp3":
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
    else:
        opts["merge_output_format"] = "mp4"
        opts["postprocessor_args"] = {"ffmpeg": ["-movflags", "+faststart"]}

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title") or "velomate-video"

        matched = list(DOWNLOAD_DIR.glob(f"{job_id}_*"))
        if not matched:
            raise HTTPException(status_code=500, detail="Downloaded file not found.")

        file_path = str(matched[0])
        safe_title = re.sub(r"[^\w\-. ()]", "_", title)[:80]
        final_ext = Path(file_path).suffix.lstrip(".") or ext
        cleanup_list = [file_path] + ([cookie_path] if is_temp and cookie_path else [])

        return FileResponse(
            path=file_path,
            filename=f"{safe_title}.{final_ext}",
            media_type="application/octet-stream",
            background=BackgroundTask(cleanup_files, cleanup_list),
        )
    except Exception as exc:
        if is_temp and cookie_path and os.path.exists(cookie_path):
            os.remove(cookie_path)
        raise HTTPException(status_code=500, detail=str(exc))
"""youtube.py — probe & download for the `add <youtube-url>` / `add --youtube` flow.

No Apple Music Premium is assumed on the YouTube side, so the best available
audio-only stream is usually Opus <=160 kbps (itag 251) or AAC 128 kbps
(itag 140); we transcode once to AAC 256 kbps (matching Apple Music's own
format) via yt-dlp's FFmpegExtractAudio postprocessor.
"""
import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}

# Suffixes commonly appended to YouTube video titles that aren't part of the
# song title, e.g. "Song Title (Official Music Video)", "Song [Lyrics]".
_JUNK_SUFFIX_RE = re.compile(
    r"\s*[\(\[][^\)\]]*\b("
    r"official\s*(music\s*)?video|official\s*audio|lyrics?|lyric\s*video|"
    r"mv|visualizer|audio"
    r")\b[^\)\]]*[\)\]]\s*",
    re.IGNORECASE,
)


def is_youtube_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return False
    return host in _YOUTUBE_HOSTS or host in _SHORT_HOSTS


def is_playlist(url: str) -> bool:
    """A /playlist URL, or a watch URL with `list=` but no `v=` (a bare
    playlist link). A watch URL with both `v=` and `list=` is a single video."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = parsed.netloc.lower()
    if host not in _YOUTUBE_HOSTS and host not in _SHORT_HOSTS:
        return False
    if parsed.path.rstrip("/").endswith("/playlist"):
        return True
    qs = parse_qs(parsed.query)
    return "list" in qs and "v" not in qs


@dataclass
class VideoInfo:
    artist: str
    title: str
    album: str
    acodec: str
    abr: float
    url: str


def _clean_title(title: str) -> str:
    return _JUNK_SUFFIX_RE.sub("", title).strip(" -")


def derive_tags(info: dict) -> tuple[str, str, str]:
    """Pure function: derive (artist, title, album) from a yt-dlp info dict.

    Rule, in order:
    1. YT Music `artist`/`track`/`album` fields, when present.
    2. Split the video title on " - " as "Artist - Title", after stripping
       junk suffixes like "(Official Video)"/"[MV]"/"(Lyrics)".
    3. Fall back to the (cleaned) title with the channel name as artist.
    Album falls back to "YouTube" whenever YT Music didn't supply one.
    """
    artist = (info.get("artist") or "").strip()
    track = (info.get("track") or "").strip()
    album = (info.get("album") or "").strip()
    if artist and track:
        return artist, track, album or "YouTube"

    raw_title = info.get("title") or ""
    cleaned = _clean_title(raw_title)
    if " - " in cleaned:
        left, right = cleaned.split(" - ", 1)
        if left.strip() and right.strip():
            return left.strip(), right.strip(), album or "YouTube"

    channel = (info.get("channel") or info.get("uploader") or "").strip()
    return channel, cleaned or raw_title, album or "YouTube"


def _best_audio_format(info: dict) -> dict | None:
    formats = [
        f
        for f in info.get("formats", []) or []
        if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")
    ]
    if not formats:
        return None
    return max(formats, key=lambda f: f.get("abr") or 0)


def _extract_info(url: str) -> dict:
    import yt_dlp

    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        return ydl.extract_info(url, download=False)


async def probe(url: str) -> VideoInfo:
    info = await asyncio.to_thread(_extract_info, url)
    artist, title, album = derive_tags(info)
    best = _best_audio_format(info) or {}
    return VideoInfo(
        artist=artist,
        title=title,
        album=album,
        acodec=str(best.get("acodec") or info.get("acodec") or "?"),
        abr=float(best.get("abr") or info.get("abr") or 0),
        url=url,
    )


def _safe_filename(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|;]', "_", text).strip() or "track"


def _run_download(url: str, out_template: str) -> None:
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "256",
            }
        ],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])


def _tag_file(path: Path, info: VideoInfo) -> None:
    from mutagen.mp4 import MP4

    audio = MP4(str(path))
    audio["\xa9nam"] = [info.title]
    audio["\xa9ART"] = [info.artist]
    audio["\xa9alb"] = [info.album]
    audio.save()


async def download(url: str, dest_dir: Path, info: VideoInfo) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = _safe_filename(f"{info.artist} - {info.title}")
    out_template = str(dest_dir / f"{safe}.%(ext)s")

    await asyncio.to_thread(_run_download, url, out_template)

    final = dest_dir / f"{safe}.m4a"
    if not final.exists():
        candidates = sorted(
            dest_dir.glob(f"{safe}.*"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        if not candidates:
            raise RuntimeError("yt-dlp produced no output file")
        final = candidates[0]

    await asyncio.to_thread(_tag_file, final, info)
    return final

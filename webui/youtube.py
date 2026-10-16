import re
from urllib.parse import parse_qs, urlsplit


_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}


def extract_youtube_video_id(value: str) -> str:
    candidate = value.strip()
    if _VIDEO_ID.fullmatch(candidate):
        return candidate
    try:
        parsed = urlsplit(candidate)
    except ValueError as error:
        raise ValueError("Enter a YouTube video URL or 11-character video ID") from error
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise ValueError("Enter a YouTube video URL or 11-character video ID")
    host = (parsed.hostname or "").lower()
    video_id = ""
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/", 1)[0]
    elif host in _YOUTUBE_HOSTS:
        path_parts = parsed.path.strip("/").split("/")
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live"}:
            video_id = path_parts[1]
    if not _VIDEO_ID.fullmatch(video_id):
        raise ValueError("Enter a valid YouTube video URL or 11-character video ID")
    return video_id

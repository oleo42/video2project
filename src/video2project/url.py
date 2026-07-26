"""URL parsing — currently YouTube only. Bilibili arrives in v2."""

from __future__ import annotations

import re
from dataclasses import dataclass


class URLParseError(ValueError):
    """Loud-fail: input doesn't look like a supported video URL."""


@dataclass(frozen=True)
class ParsedURL:
    platform: str  # "youtube" | "bilibili" (bilibili in v2)
    video_id: str
    original: str


# YouTube: youtu.be/ID, youtube.com/watch?v=ID, youtube.com/shorts/ID, youtube.com/embed/ID
_YT_PATTERNS = [
    re.compile(r"^https?://youtu\.be/([A-Za-z0-9_-]{6,})"),
    re.compile(
        r"^https?://(?:www\.|m\.)?youtube\.com/watch\?(?:[^#]*&)*v=([A-Za-z0-9_-]{6,})"
    ),
    re.compile(r"^https?://(?:www\.)?youtube\.com/shorts/([A-Za-z0-9_-]{6,})"),
    re.compile(r"^https?://(?:www\.)?youtube\.com/embed/([A-Za-z0-9_-]{6,})"),
]


def parse_url(url: str) -> ParsedURL:
    """Parse a video URL into (platform, video_id). Loud-fail if unsupported."""
    url = url.strip()
    for pat in _YT_PATTERNS:
        m = pat.match(url)
        if m:
            return ParsedURL(platform="youtube", video_id=m.group(1), original=url)
    if "bilibili.com" in url or "b23.tv" in url:
        raise URLParseError(
            "Bilibili support is deferred to v2. YouTube URLs only for v1."
        )
    raise URLParseError(
        f"Cannot parse URL: {url!r}. v1 supports YouTube only "
        f"(youtu.be, youtube.com/watch, /shorts/, /embed/)."
    )


__all__ = ["ParsedURL", "URLParseError", "parse_url"]

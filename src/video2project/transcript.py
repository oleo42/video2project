"""Stage 1a: download metadata + auto-captions for a video.

Wraps yt-dlp. Two outputs:
- transcript.json: {segments: [{start, end, text}], language, source}
- top-level metadata: title, channel, duration, etc. (saved into state.json)

Soft-fail (Q12): if captions are unavailable, we still save metadata and
write an empty transcript with source="missing". The pipeline can continue
without frames-against-transcript alignment, but everything else works.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import ytdlp
from .url import ParsedURL

# yt-dlp exit codes: 0 ok; non-zero are errors.
_YT_DLP_TIMEOUT_S = 120


def _run_ytdlp_dumpjson(url: str) -> dict[str, Any]:
    """Run yt-dlp to dump metadata as JSON. Raises on failure."""
    tail = [
        "--skip-download",
        "--dump-json",
        url,
    ]
    proc, _via = ytdlp.run_chain(tail, timeout=_YT_DLP_TIMEOUT_S)
    if not proc.stdout.strip():
        raise RuntimeError("yt-dlp returned empty output")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"yt-dlp output is not valid JSON: {exc}") from exc


def _ts_to_s(ts: str) -> float:
    parts = ts.split(":")
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = "0", parts[0], parts[1]
    else:
        return 0.0
    return int(h) * 3600 + int(m) * 60 + float(s)


_TIME_LINE_RE = re.compile(
    r"(\d{1,2}:\d{2}:\d{2}\.\d{1,3})\s+-->\s+(\d{1,2}:\d{2}:\d{2}\.\d{1,3})"
)
_TAG_RE = re.compile(r"<[^>]+>")


def _build_transcript_from_subs(sub_path: Path) -> list[dict[str, Any]]:
    """Parse a downloaded subtitle file into [{start, end, text}] segments.

    Minimal VTT parser; avoids the webvtt-py dep for v1.
    """
    if not sub_path.exists():
        return []

    text = sub_path.read_text(encoding="utf-8", errors="replace")
    segments: list[dict[str, Any]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _TIME_LINE_RE.search(lines[i])
        if not m:
            i += 1
            continue
        start = _ts_to_s(m.group(1))
        end = _ts_to_s(m.group(2))
        i += 1
        cue_lines: list[str] = []
        while i < len(lines) and lines[i].strip():
            cleaned = _TAG_RE.sub("", lines[i]).strip()
            cue_lines.append(cleaned)
            i += 1
        if cue_lines:
            segments.append(
                {
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "text": " ".join(cue_lines).strip(),
                }
            )
    return segments


def _download_subs(
    url: str, out_dir: Path, languages: list[str] | None = None
) -> tuple[Path | None, str]:
    """Download subs for the URL. Returns (path, language) or (None, "")."""
    out_dir.mkdir(parents=True, exist_ok=True)
    languages = languages or ["en", "en-US", "en-GB", "zh-Hans", "zh"]
    tail = [
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs",
        ",".join(languages),
        "--sub-format",
        "vtt",
        "-o",
        str(out_dir / "%(id)s.%(ext)s"),
        url,
    ]
    try:
        _proc, _via = ytdlp.run_chain(tail, timeout=_YT_DLP_TIMEOUT_S)
    except RuntimeError:
        return None, ""

    vtts = sorted(out_dir.glob("*.vtt"))
    if not vtts:
        return None, ""
    # Prefer a file whose name contains one of the requested language codes
    for lang in languages:
        for v in vtts:
            if f".{lang}." in v.name or v.stem.endswith(f".{lang}"):
                return v, lang
    # Fall back to the first; derive a hint from the file stem
    first = vtts[0]
    stem_parts = first.stem.split(".")
    lang = stem_parts[-1] if len(stem_parts) > 1 else ""
    return first, lang


def fetch_transcript_via_api(parsed: ParsedURL) -> dict[str, Any]:
    """Fetch transcript via youtube-transcript-api (no yt-dlp, no video download).

    Bypasses YouTube's 2026-Q3 format-manifest lockdown by hitting the
    timedtext endpoint directly. Returns the same shape as fetch_transcript().
    Soft-fails to empty segments if captions are unavailable.
    """
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import (
        TranscriptsDisabled,
        NoTranscriptFound,
        VideoUnavailable,
    )

    try:
        # New API (v1.x): use instance.list() / .fetch()
        api = YouTubeTranscriptApi()
        try:
            transcript_list = api.list(parsed.video_id)
        except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable) as exc:
            return _empty_transcript(parsed, reason=f"transcript api: {exc}")

        # Prefer manually-created English, then auto-generated English,
        # then any language in the user's preferred order.
        chosen = None
        for pref in ("en", "en-US", "en-GB", "zh-Hans", "zh-CN", "zh"):
            try:
                chosen = transcript_list.find_manually_created_transcript([pref])
                break
            except NoTranscriptFound:
                pass
        if chosen is None:
            for pref in ("en", "en-US", "en-GB", "zh-Hans", "zh-CN", "zh"):
                try:
                    chosen = transcript_list.find_generated_transcript([pref])
                    break
                except NoTranscriptFound:
                    pass
        if chosen is None:
            # Fall back to the first available transcript in any language
            for t in transcript_list:
                chosen = t
                break
        if chosen is None:
            return _empty_transcript(parsed, reason="no transcripts available")

        fetched = chosen.fetch()
        segments = [
            {
                "start": round(s.start, 3),
                "end": round(s.start + s.duration, 3),
                "text": s.text,
            }
            for s in fetched
        ]
        source = "manual" if chosen.is_manually_created else "youtube-auto"
        language = chosen.language_code or ""
        # Duration from last segment end
        duration_s = segments[-1]["end"] if segments else 0.0
        return {
            "platform": parsed.platform,
            "video_id": parsed.video_id,
            "url": parsed.original,
            "title": "",
            "channel": "",
            "duration_s": duration_s,
            "upload_date": "",
            "language": language,
            "source": source,
            "segments": segments,
        }
    except Exception as exc:  # noqa: BLE001
        return _empty_transcript(parsed, reason=f"transcript api: {exc}")


def _empty_transcript(parsed: ParsedURL, *, reason: str = "") -> dict[str, Any]:
    return {
        "platform": parsed.platform,
        "video_id": parsed.video_id,
        "url": parsed.original,
        "title": "",
        "channel": "",
        "duration_s": 0,
        "upload_date": "",
        "language": "",
        "source": "missing",
        "segments": [],
        "missing_reason": reason,
    }


def fetch_transcript(parsed: ParsedURL, out_dir: Path) -> dict[str, Any]:
    """Fetch metadata + captions. Soft-fails on missing captions."""
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = _run_ytdlp_dumpjson(parsed.original)

    sub_path, sub_lang = _download_subs(parsed.original, out_dir)
    if sub_path is None:
        segments: list[dict[str, Any]] = []
        source = "missing"
        language = ""
    else:
        segments = _build_transcript_from_subs(sub_path)
        source = "youtube-auto" if "auto" in sub_path.name else "manual"
        language = sub_lang

    return {
        "platform": parsed.platform,
        "video_id": parsed.video_id,
        "url": parsed.original,
        "title": meta.get("title") or "",
        "channel": meta.get("channel") or meta.get("uploader") or "",
        "duration_s": meta.get("duration") or 0,
        "upload_date": meta.get("upload_date") or "",
        "language": language,
        "source": source,
        "segments": segments,
    }


def write_transcript(parsed: ParsedURL, out_dir: Path) -> dict[str, Any]:
    """Fetch + write transcript.json. Cleans up raw .vtt files."""
    transcript = fetch_transcript(parsed, out_dir)
    out_path = out_dir / "transcript.json"
    out_path.write_text(
        json.dumps(transcript, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    for vtt in out_dir.glob("*.vtt"):
        try:
            vtt.unlink()
        except OSError:
            pass
    return transcript


__all__ = ["fetch_transcript", "write_transcript"]

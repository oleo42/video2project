"""Stage 3: render the human-readable index.md and machine-readable index.json.

Inputs (per video dir):
- transcript.json
- candidates.json
- claims.json
- (frames/ filled by stage 1b)

Q8-C: one markdown + one JSON sidecar. JSON is for future tooling (search,
RAG, etc.); markdown is what the user actually reads.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any] | list[dict[str, Any]] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _format_ts(seconds: float) -> str:
    if seconds is None or seconds < 0:
        return "00:00"
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


def _format_youtube_ts(seconds: float) -> str:
    """YouTube URL t-param expects integer seconds."""
    return str(int(seconds or 0))


def render_markdown(
    transcript: dict[str, Any],
    candidates: list[dict[str, Any]],
    claims: list[dict[str, Any]],
) -> str:
    """Render the deliverable markdown."""
    lines: list[str] = []
    title = transcript.get("title") or "(untitled)"
    channel = transcript.get("channel") or ""
    duration_s = float(transcript.get("duration_s") or 0)
    url = transcript.get("url") or ""
    upload_date = transcript.get("upload_date") or ""
    language = transcript.get("language") or ""
    source = transcript.get("source") or ""

    lines.append(f"# {title}")
    lines.append("")
    meta: list[str] = []
    if channel:
        meta.append(f"**Channel:** {channel}")
    if upload_date:
        meta.append(f"**Uploaded:** {upload_date}")
    if duration_s:
        meta.append(f"**Duration:** {_format_ts(duration_s)}")
    if language:
        meta.append(f"**Captions:** {language} ({source})")
    if url:
        meta.append(f"**URL:** <{url}>")
    if meta:
        lines.append("  \n".join(meta))
        lines.append("")

    # Frames
    accepted = [c for c in candidates if c.get("accepted") and c.get("extracted")]
    lines.append(f"## Key frames ({len(accepted)} of {len(candidates)} accepted)")
    lines.append("")
    if not accepted:
        lines.append("_No frames extracted._")
        lines.append("")
    else:
        for c in accepted:
            ts = float(c.get("timestamp_s", 0))
            frame_rel = c.get("frame_path", "")
            frame_abs = f"frames/{Path(frame_rel).name}" if frame_rel else ""
            yt_t = _format_youtube_ts(ts)
            if url:
                link = f"{url}&t={yt_t}s" if "?" in url else f"{url}?t={yt_t}s"
            else:
                link = ""
            stamp = f"`[{_format_ts(ts)}]`"
            if link:
                lines.append(f"- {stamp} [open in YouTube]({link}) — `{frame_abs}`")
            else:
                lines.append(f"- {stamp} `{frame_abs}`")
        lines.append("")

    # Claims
    lines.append(f"## Verified claims ({len(claims)})")
    lines.append("")
    if not claims:
        lines.append("_No claims extracted._")
        lines.append("")
    else:
        for c in claims:
            text = c.get("text", "")
            ctype = c.get("claim_type", "")
            ts_s = float(c.get("timestamp_start", 0))
            ts_e = float(c.get("timestamp_end", 0))
            why = c.get("why_check", "")
            conf = c.get("confidence", "low")
            caveat = c.get("caveat") or ""
            sources = c.get("sources") or []

            lines.append(f"### {c.get('id','?')}. {text}")
            lines.append("")
            meta_bits = [f"**Type:** `{ctype}`"]
            if ts_s or ts_e:
                meta_bits.append(f"**Time:** `{_format_ts(ts_s)}–{_format_ts(ts_e)}`")
            if conf:
                meta_bits.append(f"**Confidence:** {conf}")
            lines.append("  \n".join(meta_bits))
            lines.append("")
            if why:
                lines.append(f"_Why check:_ {why}")
                lines.append("")
            if caveat:
                lines.append(f"> {caveat}")
                lines.append("")
            if sources:
                lines.append("**Sources:**")
                lines.append("")
                for s in sources:
                    agree = s.get("agree", "unrelated")
                    agree_icon = {
                        "true": "✓ agrees",
                        "false": "✗ disagrees",
                        "unrelated": "? unrelated",
                    }.get(agree, agree)
                    one_line = s.get("one_line", "")
                    url = s.get("url", "")
                    title = s.get("title", "")
                    if url:
                        lines.append(
                            f"- [{title or url}]({url}) — {agree_icon}"
                            + (f" — {one_line}" if one_line else "")
                        )
                    else:
                        lines.append(f"- {title or '(no url)'} — {agree_icon}")
                lines.append("")
            else:
                lines.append("_No sources._")
                lines.append("")

    # Transcript
    segs = transcript.get("segments") or []
    lines.append("## Transcript")
    lines.append("")
    if not segs:
        lines.append("_No transcript available._")
    else:
        for s in segs:
            start = float(s.get("start", 0))
            text = (s.get("text") or "").strip()
            if not text:
                continue
            lines.append(f"`[{_format_ts(start)}]` {text}")
        lines.append("")

    lines.append("---")
    lines.append(
        f"_Generated by video2project at "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}._"
    )
    return "\n".join(lines) + "\n"


def render_index_json(
    transcript: dict[str, Any],
    candidates: list[dict[str, Any]],
    claims: list[dict[str, Any]],
) -> dict[str, Any]:
    """Render the machine-readable twin."""
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "video": {
            "platform": transcript.get("platform", ""),
            "id": transcript.get("video_id", ""),
            "url": transcript.get("url", ""),
            "title": transcript.get("title", ""),
            "channel": transcript.get("channel", ""),
            "upload_date": transcript.get("upload_date", ""),
            "duration_s": transcript.get("duration_s", 0),
            "language": transcript.get("language", ""),
            "captions_source": transcript.get("source", ""),
        },
        "frames": [
            {
                "index": c.get("index"),
                "timestamp_s": c.get("timestamp_s"),
                "path": c.get("frame_path"),
                "accepted": c.get("accepted", False),
                "extracted": c.get("extracted", False),
            }
            for c in candidates
        ],
        "claims": [
            {
                "id": c.get("id"),
                "text": c.get("text"),
                "claim_type": c.get("claim_type"),
                "timestamp_start": c.get("timestamp_start"),
                "timestamp_end": c.get("timestamp_end"),
                "why_check": c.get("why_check"),
                "confidence": c.get("confidence"),
                "caveat": c.get("caveat"),
                "sources": c.get("sources", []),
            }
            for c in claims
        ],
        "transcript": transcript.get("segments", []),
    }


def finalize(video_dir: Path) -> tuple[Path, Path]:
    """Load artifacts, render index.md and index.json. Returns their paths."""
    transcript_data = _load(video_dir / "transcript.json")
    transcript: dict[str, Any] = (
        transcript_data if isinstance(transcript_data, dict) else {}
    )
    candidates_data = _load(video_dir / "candidates.json")
    claims_data = _load(video_dir / "claims.json")
    candidates: list[dict[str, Any]] = (
        candidates_data if isinstance(candidates_data, list) else []
    )
    claims: list[dict[str, Any]] = claims_data if isinstance(claims_data, list) else []

    md = render_markdown(transcript, candidates, claims)
    js = render_index_json(transcript, candidates, claims)

    md_path = video_dir / "index.md"
    js_path = video_dir / "index.json"
    md_path.write_text(md, encoding="utf-8")
    js_path.write_text(
        json.dumps(js, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return md_path, js_path


__all__ = ["finalize", "render_markdown", "render_index_json"]

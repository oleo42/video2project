"""Stage 2: extract verifiable claims from a transcript + LLM-cite each one.

Q3-B: extract factual claims only (named entities, numbers, dates, etc.).
Q6-C: cite via the LLM's own knowledge (no live search at v1). Mark every
     citation as ⚠️ unverified since we have no way to confirm the URLs exist.

Outputs claims.json:
[
  {
    "id": "c1",
    "text": "...",
    "timestamp_start": 12.3,
    "timestamp_end": 18.7,
    "claim_type": "factual" | "mathematical" | ...,
    "why_check": "...",
    "sources": [
      {"url", "title", "agree": true|false|"unrelated", "one_line"}
    ],
    "confidence": "high" | "medium" | "low",
    "caveat": "..." | null
  }
]

Soft-fail: if LLM call fails for one claim, we record it with empty sources
and continue. The pipeline still produces a usable index.md.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import client
from .paths import PROMPTS_DIR


def _read_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _format_transcript_for_prompt(
    transcript: dict[str, Any], max_chars: int = 60_000
) -> str:
    """Plain-text transcript with [mm:ss] timestamps. Truncated to fit budget."""
    lines: list[str] = []
    for seg in transcript.get("segments") or []:
        start = float(seg.get("start", 0))
        mm = int(start // 60)
        ss = start - mm * 60
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"[{mm:02d}:{ss:05.2f}] {text}")
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars] + "\n…(truncated)"
    return out


def _empty_citation(exc: Exception) -> dict[str, Any]:
    """A citation record for when the LLM call failed."""
    return {
        "sources": [],
        "confidence": "low",
        "caveat": f"⚠️ unverified — LLM call failed: {exc}",
    }


def _context_excerpt(transcript: dict[str, Any], start: float, end: float) -> str:
    """Snippet of the transcript around [start, end] for the cite prompt."""
    segs = transcript.get("segments") or []
    pad = 5.0
    in_range = [
        s
        for s in segs
        if float(s.get("start", 0)) <= end + pad
        and float(s.get("end", 0)) >= start - pad
    ]
    return " ".join((s.get("text") or "").strip() for s in in_range).strip()[:1500]


def extract_claims(
    transcript: dict[str, Any], *, max_claims: int = 50
) -> list[dict[str, Any]]:
    """Run the claim-extraction prompt over the transcript."""
    if not transcript.get("segments"):
        return []
    system = _read_prompt("extract_claims.md")
    user = (
        "VIDEO METADATA\n"
        f"Title: {transcript.get('title','')}\n"
        f"Channel: {transcript.get('channel','')}\n"
        f"Duration: {transcript.get('duration_s',0):.0f}s\n\n"
        "TRANSCRIPT (with [mm:ss] timestamps):\n"
        f"{_format_transcript_for_prompt(transcript)}\n\n"
        f"Extract up to {max_claims} verifiable claims. Output the JSON object only."
    )
    try:
        data = client.chat_json(system, user, max_tokens=4096)
    except Exception as exc:  # noqa: BLE001
        # Loud-fail: no claims = no citations = no useful index.md.
        # The caller decides whether to abort or continue with empty claims.
        raise RuntimeError(f"Claim extraction failed: {exc}") from exc

    claims = data.get("claims") if isinstance(data, dict) else None
    if not isinstance(claims, list):
        raise RuntimeError(f"Unexpected claim-extraction shape: {data!r}")
    # Normalize: ensure each has at least the required fields
    out: list[dict[str, Any]] = []
    for i, c in enumerate(claims, start=1):
        if not isinstance(c, dict):
            continue
        out.append(
            {
                "id": c.get("id") or f"c{i}",
                "text": c.get("text") or "",
                "timestamp_start": float(c.get("timestamp_start") or 0),
                "timestamp_end": float(c.get("timestamp_end") or 0),
                "claim_type": c.get("claim_type") or "factual",
                "why_check": c.get("why_check") or "",
                "sources": [],
                "confidence": "low",
                "caveat": "⚠️ unverified — pending citation pass",
            }
        )
    return out


def cite_claim(claim: dict[str, Any], transcript: dict[str, Any]) -> dict[str, Any]:
    """Run the cite_sources prompt for one claim. Returns updated claim dict."""
    system = _read_prompt("cite_sources.md")
    excerpt = _context_excerpt(
        transcript,
        float(claim.get("timestamp_start", 0)),
        float(claim.get("timestamp_end", 0)),
    )
    user = (
        f"CLAIM (type: {claim.get('claim_type','factual')}):\n"
        f"{claim.get('text','')}\n\n"
        f"CONTEXT (from transcript, may be a paraphrase):\n"
        f"{excerpt}\n\n"
        "Produce the JSON object with 2-3 sources only."
    )
    try:
        data = client.chat_json(system, user, max_tokens=1500)
    except Exception as exc:  # noqa: BLE001
        # Soft-fail: record the failure, keep the claim
        return {**claim, **_empty_citation(exc)}

    if not isinstance(data, dict):
        return {**claim, **_empty_citation(ValueError("non-dict response"))}

    sources = data.get("sources") or []
    if not isinstance(sources, list):
        sources = []

    # Defensive normalization
    norm_sources: list[dict[str, Any]] = []
    for s in sources:
        if not isinstance(s, dict):
            continue
        url = (s.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            # Skip junk URLs the model sometimes produces
            continue
        norm_sources.append(
            {
                "url": url,
                "title": (s.get("title") or "").strip(),
                "agree": s.get("agree", "unrelated"),
                "one_line": (s.get("one_line") or "").strip(),
            }
        )

    confidence = data.get("confidence") or "low"
    if confidence not in ("high", "medium", "low"):
        confidence = "low"
    caveat = data.get("caveat")
    if not norm_sources:
        # Treat empty source list as an automatic unverified tag
        if not caveat:
            caveat = "⚠️ unverified — no defensible sources"
        elif "⚠️" not in caveat:
            caveat = f"⚠️ unverified — {caveat}"
        confidence = "low"
    else:
        # At v1 every source is LLM-cited → unverified
        prefix = "⚠️ unverified — LLM-cited (no live search at v1)"
        caveat = f"{prefix}; {caveat}" if caveat else prefix

    return {
        **claim,
        "sources": norm_sources,
        "confidence": confidence,
        "caveat": caveat,
    }


def extract_and_cite(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    """Convenience: extract + cite every claim. Returns the claims list."""
    claims = extract_claims(transcript)
    out: list[dict[str, Any]] = []
    for c in claims:
        out.append(cite_claim(c, transcript))
    return out


def write_claims(transcript: dict[str, Any], out_path: Path) -> list[dict[str, Any]]:
    """Run the full extract+cite pass and write to claims.json."""
    claims = extract_and_cite(transcript)
    out_path.write_text(
        json.dumps(claims, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return claims


__all__ = ["extract_claims", "cite_claim", "extract_and_cite", "write_claims"]

"""Browser-capture pipeline: server-side orchestrator.

The Chrome extension (browser-first design) captures audio + frames on the
YouTube watch page and POSTs them to the local server. This module coordinates
the local half:

    audio POST  →  transcribe (Whisper)  →  pick frame timestamps (Rule A)
                                             ↓
    frames POST (extension seeks+captures at those timestamps)
                                             ↓
                       OCR (PaddleOCR, confidence → human-check flag)
                                             ↓
                       finalize (index.md / index.json)

It reuses the existing artifacts and contracts (transcript.json,
candidates.json, state.json, finalize) so the review UI and renderer work
unchanged. Network invariant: the local machine never touches YouTube — only
the browser does; we only ever call the existing Ark text-LLM during extract.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from . import finalize as finalize_mod
from . import paths
from .frames import _pick_candidate_timestamps
from .ocr import ocr_frames
from .transcribe import transcribe_audio

_AUDIO_NAME = "audio.webm"
_PENDING_TS = "pending_timestamps.json"


# ── payload helpers ─────────────────────────────────────────────────


def _b64_to_bytes(data: str) -> bytes:
    """Decode a base64 payload, tolerating a data-URL prefix."""
    if "," in data and data.split(",", 1)[0].startswith("data:"):
        data = data.split(",", 1)[1]
    return base64.b64decode(data)


def _save_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


# ── public API (called by the ingest HTTP endpoint) ─────────────────


def start_job(metadata: dict[str, Any]) -> dict[str, Any]:
    """Register a capture job from the extension's metadata. Returns job info."""
    platform = metadata.get("platform", "youtube")
    video_id = metadata["video_id"]
    paths.ensure_dirs(platform, video_id)
    video_dir = paths.video_dir(platform, video_id)
    _save_json(video_dir / "capture_metadata.json", metadata)
    return {"video_dir": str(video_dir), "platform": platform, "video_id": video_id}


def ingest_audio(metadata: dict[str, Any], audio_b64: str) -> dict[str, Any]:
    """Save audio, transcribe, pick frame timestamps. Returns timestamps.

    The extension calls this after Pass-1 audio capture; the returned
    ``timestamps`` drive Pass-2 frame capture.
    """
    platform = metadata.get("platform", "youtube")
    video_id = metadata["video_id"]
    paths.ensure_dirs(platform, video_id)
    video_dir = paths.video_dir(platform, video_id)

    audio_path = video_dir / _AUDIO_NAME
    audio_path.write_bytes(_b64_to_bytes(audio_b64))

    transcript = transcribe_audio(audio_path, metadata=metadata)
    _save_json(paths.transcript_json(platform, video_id), transcript)

    duration_s = float(metadata.get("duration_s") or transcript.get("duration_s") or 0)
    timestamps = _pick_candidate_timestamps(transcript, duration_s)
    _save_json(video_dir / _PENDING_TS, {"timestamps": timestamps})

    return {
        "video_id": video_id,
        "n_segments": len(transcript.get("segments", [])),
        "timestamps": timestamps,
    }


def ingest_frames(
    metadata: dict[str, Any], frames: list[dict[str, Any]]
) -> dict[str, Any]:
    """Save captured frames, OCR them, write candidates.json, finalize.

    ``frames`` is a list of {timestamp_s, image_b64}. Returns a summary with
    the OCR results (including which frames need human check).
    """
    platform = metadata.get("platform", "youtube")
    video_id = metadata["video_id"]
    paths.ensure_dirs(platform, video_id)
    video_dir = paths.video_dir(platform, video_id)
    frames_dir = paths.frames_dir(platform, video_id)
    frames_dir.mkdir(parents=True, exist_ok=True)

    saved: list[Path] = []
    candidates: list[dict[str, Any]] = []
    for i, fr in enumerate(frames, start=1):
        ts = float(fr.get("timestamp_s", 0))
        img = _b64_to_bytes(fr["image_b64"])
        out = frames_dir / f"frame_{i:04d}.png"
        out.write_bytes(img)
        saved.append(out)
        candidates.append(
            {
                "index": i,
                "timestamp_s": ts,
                "frame_path": f"frames/{out.name}",
                "extracted": True,
                "accepted": True,
            }
        )

    # OCR each frame; attach text + human-check flag to its candidate.
    ocr_results = ocr_frames(saved)
    by_name = {r["frame_path"]: r for r in ocr_results}
    for cand in candidates:
        r = by_name.get(Path(cand["frame_path"]).name)
        if r:
            cand["ocr_text"] = r["text"]
            cand["ocr_confidence"] = r["confidence"]
            cand["needs_human"] = r["needs_human"]

    _save_json(paths.candidates_json(platform, video_id), candidates)
    _save_json(video_dir / "ocr_results.json", ocr_results)

    # Render the deliverable (claims.json may not exist yet → finalize tolerates).
    claims_path = paths.claims_json(platform, video_id)
    if not claims_path.exists():
        _save_json(claims_path, [])
    md_path, js_path = finalize_mod.finalize(video_dir)

    return {
        "video_id": video_id,
        "n_frames": len(candidates),
        "n_needs_human": sum(1 for c in candidates if c.get("needs_human")),
        "index_md": str(md_path),
        "index_json": str(js_path),
    }


__all__ = ["start_job", "ingest_audio", "ingest_frames"]

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
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import finalize as finalize_mod
from . import paths
from .frames import _pick_candidate_timestamps
from .transcribe import transcribe_audio

_AUDIO_NAME = "audio.webm"
_PENDING_TS = "pending_timestamps.json"
_STATE_NAME = "state.json"
_RUN_SUMMARY = "run_summary.json"

# Per-frame OCR timeout when running in an isolated subprocess. Frames that
# take longer than this (or crash PaddleOCR entirely) are skipped, not fatal.
_OCR_SUBPROC_TIMEOUT_S = 60.0

# ── payload helpers ─────────────────────────────────────────────────


def _b64_to_bytes(data: str) -> bytes:
    """Decode a base64 payload, tolerating a data-URL prefix."""
    if "," in data and data.split(",", 1)[0].startswith("data:"):
        data = data.split(",", 1)[1]
    return base64.b64decode(data)


def _save_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_state(video_dir: Path) -> dict[str, Any]:
    p = video_dir / _STATE_NAME
    if not p.exists():
        return {"stages": {}}
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
        s.setdefault("stages", {})
        return s
    except (json.JSONDecodeError, OSError):
        return {"stages": {}}


def _save_state(video_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = _now()
    _save_json(video_dir / _STATE_NAME, state)


def _stage_start(video_dir: Path, name: str, **extra: Any) -> dict[str, Any]:
    """Mark a stage as started on disk. Returns the fresh state dict.

    ``extra`` lets the caller stamp inputs (e.g. audio_sha256) that let a later
    resume decide whether the prior run's outputs are still valid.
    """
    state = _load_state(video_dir)
    stages = state.setdefault("stages", {})
    stages[name] = {"started_at": _now(), "ok": False, **extra}
    _save_state(video_dir, state)
    return state


def _stage_done(video_dir: Path, name: str, **fields: Any) -> None:
    state = _load_state(video_dir)
    stage = state.setdefault("stages", {}).setdefault(name, {})
    stage.update(fields)
    stage["done_at"] = _now()
    stage["ok"] = True
    _save_state(video_dir, state)


def _stage_fail(video_dir: Path, name: str, err: str) -> None:
    state = _load_state(video_dir)
    stage = state.setdefault("stages", {}).setdefault(name, {})
    stage["failed_at"] = _now()
    stage["ok"] = False
    stage["error"] = err
    _save_state(video_dir, state)


# ── subprocess-isolated OCR (survives PaddleOCR SIGTERMs) ───────────


def _ocr_frames_isolated(frame_paths: list[Path]) -> list[dict[str, Any]]:
    """OCR frames in an isolated subprocess. Batch first; per-frame on crash.

    Fast path: one subprocess handles the whole batch — PaddleOCR loads once.
    If it crashes (SIGTERM, OneDNN abort, etc.) each frame is retried in its
    own subprocess so a single bad frame can't take down the rest. Failed
    frames land with ``ocr_failed=True`` and empty text.
    """
    if not frame_paths:
        return []
    batch_timeout = max(_OCR_SUBPROC_TIMEOUT_S, 15.0 * len(frame_paths))
    batch = _run_ocr_subproc([str(p) for p in frame_paths], timeout_s=batch_timeout)
    if batch is not None:
        return batch
    # Batch failed → per-frame retry so one bad frame doesn't kill the rest.
    results: list[dict[str, Any]] = []
    for p in frame_paths:
        one = _run_ocr_subproc([str(p)], timeout_s=_OCR_SUBPROC_TIMEOUT_S)
        if one:
            results.append(one[0])
        else:
            results.append(
                {
                    "frame_path": p.name,
                    "text": "",
                    "confidence": 0.0,
                    "needs_human": True,
                    "ocr_failed": True,
                    "ocr_error": "subprocess crashed",
                }
            )
    return results


def _run_ocr_subproc(
    frame_path_strs: list[str], *, timeout_s: float
) -> list[dict[str, Any]] | None:
    """Run one subprocess that OCRs a list of frames. None on failure."""
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json,sys;"
                    "from pathlib import Path;"
                    "from video2project.ocr import ocr_frame;"
                    "print(json.dumps([ocr_frame(Path(a)) for a in sys.argv[1:]]))"
                ),
                *frame_path_strs,
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    # PaddleOCR spams stdout with progress lines; find the JSON array.
    for ln in reversed(proc.stdout.splitlines()):
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                continue
    return None


def _write_run_summary(video_dir: Path, metadata: dict[str, Any]) -> Path:
    """Write a small canonical summary of what the run produced.

    Kept separate from ``state.json`` so downstream consumers (Obsidian vault
    mirror, future AI agents) have one flat file to read: what stages ran, how
    much audio was captured, how many frames, what needs human review.
    """
    state = _load_state(video_dir)
    stages = state.get("stages", {})
    audio = stages.get("ingest_audio", {})
    frames_stage = stages.get("ingest_frames", {})
    warnings: list[str] = []
    errors: list[str] = []
    if audio.get("partial"):
        vd = audio.get("video_duration_s") or 1
        ae = audio.get("audio_end_s") or 0
        warnings.append(
            f"audio captured only {ae:.0f}s of {vd:.0f}s ({ae / vd * 100:.0f}%)"
        )
    if frames_stage.get("n_ocr_failed", 0):
        warnings.append(f"{frames_stage['n_ocr_failed']} frame(s) failed OCR")
    for name, s in stages.items():
        if s.get("error"):
            errors.append(f"{name}: {s['error']}")
    summary = {
        "video_id": metadata.get("video_id"),
        "title": metadata.get("title"),
        "url": metadata.get("url"),
        "video_duration_s": metadata.get("duration_s"),
        "audio_end_s": audio.get("audio_end_s"),
        "audio_partial": audio.get("partial", False),
        "n_segments": audio.get("n_segments"),
        "n_frames": frames_stage.get("n_frames"),
        "n_needs_human": frames_stage.get("n_needs_human"),
        "n_ocr_failed": frames_stage.get("n_ocr_failed"),
        "stages_ok": {k: s.get("ok", False) for k, s in stages.items()},
        "warnings": warnings,
        "errors": errors,
        "written_at": _now(),
    }
    out = video_dir / _RUN_SUMMARY
    _save_json(out, summary)
    return out


def read_state(platform: str, video_id: str) -> dict[str, Any]:
    """Public accessor for the /api/state endpoint.

    Enriches the raw ``state.json`` with the current ``pending_timestamps.json``
    and any ``run_summary.json`` so the extension can decide whether to skip
    Pass 1 without extra HTTP round-trips.
    """
    video_dir = paths.video_dir(platform, video_id)
    state = _load_state(video_dir)
    pending_ts_path = video_dir / _PENDING_TS
    if pending_ts_path.exists():
        try:
            state.setdefault("stages", {}).setdefault("ingest_audio", {})[
                "timestamps"
            ] = json.loads(pending_ts_path.read_text(encoding="utf-8")).get(
                "timestamps", []
            )
        except (json.JSONDecodeError, OSError):
            pass
    summary_path = video_dir / _RUN_SUMMARY
    if summary_path.exists():
        try:
            state["run_summary"] = json.loads(summary_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return state


# ── public API (called by the ingest HTTP endpoint) ─────────────────


def start_job(metadata: dict[str, Any]) -> dict[str, Any]:
    """Register a capture job from the extension's metadata. Returns job info."""
    platform = metadata.get("platform", "youtube")
    video_id = metadata["video_id"]
    paths.ensure_dirs(platform, video_id)
    video_dir = paths.video_dir(platform, video_id)
    _save_json(video_dir / "capture_metadata.json", metadata)
    _stage_done(video_dir, "start_job", video_duration_s=metadata.get("duration_s"))
    return {"video_dir": str(video_dir), "platform": platform, "video_id": video_id}


def ingest_audio(metadata: dict[str, Any], audio_b64: str) -> dict[str, Any]:
    """Save audio, transcribe, pick frame timestamps. Returns timestamps.

    The extension calls this after Pass-1 audio capture; the returned
    ``timestamps`` drive Pass-2 frame capture.

    **Idempotent**: if the same audio (by sha256) was already transcribed for
    this video, returns the cached timestamps without re-running Whisper.
    """
    platform = metadata.get("platform", "youtube")
    video_id = metadata["video_id"]
    paths.ensure_dirs(platform, video_id)
    video_dir = paths.video_dir(platform, video_id)

    audio_bytes = _b64_to_bytes(audio_b64)
    audio_sha = hashlib.sha256(audio_bytes).hexdigest()

    transcript_path = paths.transcript_json(platform, video_id)
    pending_ts_path = video_dir / _PENDING_TS
    if transcript_path.exists() and pending_ts_path.exists():
        try:
            cached = json.loads(transcript_path.read_text(encoding="utf-8"))
            if cached.get("audio_sha256") == audio_sha:
                timestamps = json.loads(
                    pending_ts_path.read_text(encoding="utf-8")
                ).get("timestamps", [])
                cached_segments = cached.get("segments", [])
                cached_audio_end = (
                    float(cached_segments[-1]["end"]) if cached_segments else 0.0
                )
                cached_duration_s = float(
                    metadata.get("duration_s") or cached.get("duration_s") or 0
                )
                cached_partial = (
                    cached_duration_s > 0 and cached_audio_end < cached_duration_s * 0.5
                )
                _stage_done(
                    video_dir,
                    "ingest_audio",
                    audio_sha256=audio_sha,
                    n_segments=len(cached_segments),
                    audio_end_s=cached_audio_end,
                    video_duration_s=cached_duration_s,
                    partial=cached_partial,
                    cached=True,
                )
                return {
                    "video_id": video_id,
                    "n_segments": len(cached_segments),
                    "timestamps": timestamps,
                    "cached": True,
                    "partial": cached_partial,
                    "audio_end_s": cached_audio_end,
                    "video_duration_s": cached_duration_s,
                }
        except (json.JSONDecodeError, OSError):
            pass  # fall through to re-transcribe
    _stage_start(
        video_dir, "ingest_audio", audio_sha256=audio_sha, audio_bytes=len(audio_bytes)
    )
    audio_path = video_dir / _AUDIO_NAME
    audio_path.write_bytes(audio_bytes)

    try:
        transcript = transcribe_audio(audio_path, metadata=metadata)
    except Exception as exc:
        _stage_fail(video_dir, "ingest_audio", f"transcribe: {exc}")
        raise
    transcript["audio_sha256"] = audio_sha
    _save_json(transcript_path, transcript)

    duration_s = float(metadata.get("duration_s") or transcript.get("duration_s") or 0)
    # Audio-vs-video sanity: last transcript end timestamp compared to metadata.
    segments = transcript.get("segments", [])
    audio_end = float(segments[-1]["end"]) if segments else 0.0
    partial = duration_s > 0 and audio_end < duration_s * 0.5
    timestamps = _pick_candidate_timestamps(transcript, duration_s)
    _save_json(pending_ts_path, {"timestamps": timestamps})
    _stage_done(
        video_dir,
        "ingest_audio",
        audio_sha256=audio_sha,
        n_segments=len(segments),
        audio_end_s=audio_end,
        video_duration_s=duration_s,
        partial=partial,
    )

    return {
        "video_id": video_id,
        "n_segments": len(segments),
        "timestamps": timestamps,
        "cached": False,
        "partial": partial,
        "audio_end_s": audio_end,
        "video_duration_s": duration_s,
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
    # Track (rounded ts, path) to dedupe against manual marks below.
    seen_ts: set[int] = set()
    for i, fr in enumerate(frames, start=1):
        ts = float(fr.get("timestamp_s", 0))
        img = _b64_to_bytes(fr["image_b64"])
        out = frames_dir / f"frame_{i:04d}.png"
        out.write_bytes(img)
        saved.append(out)
        seen_ts.add(round(ts * 10))
        candidates.append(
            {
                "index": i,
                "timestamp_s": ts,
                "frame_path": f"frames/{out.name}",
                "extracted": True,
                "accepted": True,
                "manual": False,
            }
        )

    # Merge previously-saved manual marks (already on disk as mark_XXXX.png).
    marks_path = video_dir / "pending_marks.json"
    if marks_path.exists():
        try:
            for mark in json.loads(marks_path.read_text(encoding="utf-8")) or []:
                ts = float(mark.get("timestamp_s", 0))
                key = round(ts * 10)
                if key in seen_ts:
                    continue  # deduped against auto-selected
                seen_ts.add(key)
                # The PNG is already at frames/mark_XXXX.png; just reference it.
                mark_png = frames_dir / Path(mark["frame_path"]).name
                if mark_png.exists():
                    saved.append(mark_png)
                    candidates.append(
                        {
                            "index": len(candidates) + 1,
                            "timestamp_s": ts,
                            "frame_path": f"frames/{mark_png.name}",
                            "extracted": True,
                            "accepted": True,
                            "manual": True,
                        }
                    )
        except (json.JSONDecodeError, OSError):
            pass  # bad marks file — keep going without it
    _stage_start(video_dir, "ingest_frames", n_input_frames=len(frames))
    # OCR each frame in isolated subprocesses. A crash or timeout on one frame
    # is captured as ocr_failed=True on that candidate; the run continues.
    try:
        ocr_results = _ocr_frames_isolated(saved)
    except Exception as exc:
        _stage_fail(video_dir, "ingest_frames", f"ocr: {exc}")
        raise
    by_name = {r["frame_path"]: r for r in ocr_results}
    for cand in candidates:
        r = by_name.get(Path(cand["frame_path"]).name)
        if r:
            cand["ocr_text"] = r.get("text", "")
            cand["ocr_confidence"] = r.get("confidence", 0.0)
            cand["needs_human"] = r.get("needs_human", True)
            if r.get("ocr_failed"):
                cand["ocr_failed"] = True
                cand["ocr_error"] = r.get("ocr_error", "")

    _save_json(paths.candidates_json(platform, video_id), candidates)
    _save_json(video_dir / "ocr_results.json", ocr_results)

    # Render the deliverable (claims.json may not exist yet → finalize tolerates).
    claims_path = paths.claims_json(platform, video_id)
    if not claims_path.exists():
        _save_json(claims_path, [])
    md_path, js_path = finalize_mod.finalize(video_dir)

    n_ocr_failed = sum(1 for c in candidates if c.get("ocr_failed"))
    n_needs_human = sum(1 for c in candidates if c.get("needs_human"))
    _stage_done(
        video_dir,
        "ingest_frames",
        n_frames=len(candidates),
        n_needs_human=n_needs_human,
        n_ocr_failed=n_ocr_failed,
    )
    _write_run_summary(video_dir, metadata)

    return {
        "video_id": video_id,
        "n_frames": len(candidates),
        "n_needs_human": n_needs_human,
        "n_ocr_failed": n_ocr_failed,
        "index_md": str(md_path),
        "index_json": str(js_path),
    }


def ingest_mark(
    metadata: dict[str, Any], timestamp_s: float, image_b64: str
) -> dict[str, Any]:
    """Save ONE manually-marked frame right now.

    Called by the extension's mark button each time the user clicks it. The
    PNG lands in ``frames/mark_<n>.png`` immediately, and a stub entry is
    appended to ``pending_marks.json``. If Pass 2 (``ingest_frames``) runs
    later, it merges these marks with the auto-selected timestamps. If it
    never runs, the marks are still on disk for review.
    """
    platform = metadata.get("platform", "youtube")
    video_id = metadata["video_id"]
    paths.ensure_dirs(platform, video_id)
    video_dir = paths.video_dir(platform, video_id)
    frames_dir = paths.frames_dir(platform, video_id)
    frames_dir.mkdir(parents=True, exist_ok=True)

    marks_path = video_dir / "pending_marks.json"
    marks: list[dict[str, Any]] = []
    if marks_path.exists():
        try:
            marks = json.loads(marks_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            marks = []

    idx = len(marks) + 1
    out = frames_dir / f"mark_{idx:04d}.png"
    out.write_bytes(_b64_to_bytes(image_b64))

    marks.append(
        {
            "index": idx,
            "timestamp_s": float(timestamp_s),
            "frame_path": f"frames/{out.name}",
        }
    )
    _save_json(marks_path, marks)

    return {
        "video_id": video_id,
        "index": idx,
        "frame_path": str(out),
        "total_marks": len(marks),
    }


__all__ = [
    "start_job",
    "ingest_audio",
    "ingest_frames",
    "ingest_mark",
    "read_state",
]

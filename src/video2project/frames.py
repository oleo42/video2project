"""Stage 1b: extract candidate key-frames from a video.

Q2-C choice: AI proposes candidates, you tweak via the review page.

Candidate generation (pure text heuristics, no LLM call here):
- Topic-shift points in the transcript (large gap between consecutive segments)
- Evenly spaced anchors (every ~60s) so we always have a floor
- Deduplicated, capped at MAX_CANDIDATES

Frame extraction:
- We download the video to a temp file via yt-dlp (not just audio — we need
  visual frames). Format chosen for "smallest file with reasonable quality".
- ffmpeg extracts each timestamp at 720p height, preserving aspect ratio.

Outputs:
- candidates.json: [{index, timestamp_s, frame_path, accepted: true|false}]
- frames/frame_NNNN.png
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import ytdlp
from .url import ParsedURL

MAX_CANDIDATES = 30
ANCHOR_INTERVAL_S = 60.0
MIN_GAP_BETWEEN_S = 5.0  # dedupe candidates closer than this
TOPIC_SHIFT_PAUSE_S = 1.5  # gaps >= this count as topic shifts
FRAME_HEIGHT = 720
DOWNLOAD_TIMEOUT_S = 300


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _check_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg not found on PATH. Install ffmpeg: "
            "  apt-get install ffmpeg  /  brew install ffmpeg"
        )


def _download_video(parsed: ParsedURL, dest_dir: Path) -> Path:
    """Download the video to <dest_dir>/video.mp4. Returns the path.

    Picks the smallest format with both video and audio (or video-only if
    we just need frames). mp4 preferred.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(dest_dir / "video.%(ext)s")
    tail = [
        "-f",
        "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4] / wv*+ba / b",
        "--merge-output-format",
        "mp4",
        "-o",
        out_template,
        parsed.original,
    ]
    try:
        _proc, _via = ytdlp.run_chain(tail, timeout=DOWNLOAD_TIMEOUT_S)
    except RuntimeError as exc:
        raise RuntimeError(f"Video download failed: {exc}") from exc

    # Find the actual file (could be video.mp4 or video.webm etc.)
    candidates = sorted(dest_dir.glob("video.*"))
    real = [
        c for c in candidates if c.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")
    ]
    if not real:
        raise RuntimeError("yt-dlp download produced no recognizable video file")
    return real[0]


def _pick_candidate_timestamps(
    transcript: dict[str, Any], duration_s: float
) -> list[float]:
    """Pure-text heuristic: topic shifts + evenly-spaced anchors.

    Returns sorted list of timestamps (seconds), capped at MAX_CANDIDATES.
    """
    if duration_s <= 0:
        duration_s = 0.0
    timestamps: list[float] = []
    seen: set[float] = set()

    def add(ts: float) -> None:
        # Clamp to valid range
        if duration_s > 0 and ts >= duration_s:
            return
        # Dedupe by quantizing to 0.5s buckets
        key = round(ts * 2) / 2
        if key in seen:
            return
        # Apply min-gap filter
        if any(abs(ts - t) < MIN_GAP_BETWEEN_S for t in timestamps):
            return
        seen.add(key)
        timestamps.append(ts)

    # 1. Topic shifts: gaps between segments >= TOPIC_SHIFT_PAUSE_S,
    #    take the END of the preceding segment (where the visual changed)
    segments = transcript.get("segments") or []
    for prev, curr in zip(segments, segments[1:]):
        gap = curr.get("start", 0) - prev.get("end", 0)
        if gap >= TOPIC_SHIFT_PAUSE_S:
            add(prev.get("end", 0) + 0.1)

    # 2. Evenly-spaced anchors across the duration
    if duration_s > 0:
        n_anchors = max(3, min(10, int(duration_s / ANCHOR_INTERVAL_S) + 1))
        step = duration_s / (n_anchors + 1)
        for i in range(1, n_anchors + 1):
            add(i * step)
    elif segments:
        # Fallback: use last segment end
        add(segments[-1].get("end", 0) * 0.5)

    # 3. Always include a frame near the start (intro slide)
    add(1.0)

    timestamps.sort()
    # Cap
    return timestamps[:MAX_CANDIDATES]


def _extract_frame(video: Path, ts: float, out_path: Path) -> bool:
    """Extract a single frame at given timestamp. Returns True on success."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{ts:.3f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-vf",
        f"scale=-2:{FRAME_HEIGHT}",
        "-q:v",
        "2",  # high quality JPEG-like
        str(out_path),
    ]
    try:
        proc = _run(cmd, timeout=30)
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0


def extract_frames(
    parsed: ParsedURL,
    transcript: dict[str, Any],
    frames_dir: Path,
    work_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Download video, pick candidates, extract PNGs. Returns candidates list."""
    _check_ffmpeg()
    frames_dir.mkdir(parents=True, exist_ok=True)

    # Use a temp dir for the video download (videos can be large; we clean up)
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="v2p_"))
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        video = _download_video(parsed, work_dir)
        duration_s = float(transcript.get("duration_s") or 0)
        timestamps = _pick_candidate_timestamps(transcript, duration_s)

        candidates: list[dict[str, Any]] = []
        for idx, ts in enumerate(timestamps, start=1):
            frame_path = frames_dir / f"frame_{idx:04d}.png"
            ok = _extract_frame(video, ts, frame_path)
            if not ok:
                # Try once more with a slight time nudge (some players report
                # duration slightly off; this catches near-the-end frames)
                ok = _extract_frame(video, max(0, ts - 0.5), frame_path)
            candidates.append(
                {
                    "index": idx,
                    "timestamp_s": round(ts, 3),
                    "frame_path": str(frame_path.relative_to(frames_dir.parent)),
                    "extracted": ok,
                    "accepted": ok,  # default accepted; user can flip in review
                }
            )
        return candidates
    finally:
        # Clean up the downloaded video — we only wanted the frames
        shutil.rmtree(work_dir, ignore_errors=True)


def write_candidates(
    parsed: ParsedURL,
    transcript: dict[str, Any],
    frames_dir: Path,
) -> list[dict[str, Any]]:
    """Run extraction + write candidates.json. Returns the list."""
    candidates = extract_frames(parsed, transcript, frames_dir)
    out = frames_dir.parent / "candidates.json"
    out.write_text(
        json.dumps(candidates, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return candidates


__all__ = ["extract_frames", "write_candidates", "MAX_CANDIDATES"]

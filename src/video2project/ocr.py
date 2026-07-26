"""Browser-capture pipeline: local OCR on captured frames.

Free frame-understanding path (no vision-LLM in the free version). Each frame
is OCR'd locally; the mean confidence of the detected text boxes decides
whether the frame's text is trusted or flagged for HUMAN check in the review
UI. Chinese-primary: PaddleOCR handles mixed CN/EN far better than Tesseract.

Config (env):
- ``V2P_OCR_LANG``        PaddleOCR lang, default "ch" (Chinese+English)
- ``V2P_OCR_CONF_MIN``    float 0..1, default 0.6 — below this → human check
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_OCR_LANG = os.environ.get("V2P_OCR_LANG", "ch")
_CONF_MIN = float(os.environ.get("V2P_OCR_CONF_MIN", "0.6"))

_ocr_cache: dict[str, Any] = {}


def _load_engine():
    """Lazy-load a PaddleOCR engine. Raises if paddleocr missing."""
    if "engine" in _ocr_cache:
        return _ocr_cache["engine"]
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise RuntimeError(
            "paddleocr not installed. Run: pip install paddleocr paddlepaddle"
        ) from exc
    # PaddleOCR 3.x: constructor takes lang; angle classification is on by default.
    engine = PaddleOCR(lang=_OCR_LANG)
    _ocr_cache["engine"] = engine
    return engine


def ocr_frame(frame_path: Path) -> dict[str, Any]:
    """OCR one frame. Returns text, confidence, and a needs_human flag.

    Confidence is the mean of per-box scores (0 if no text detected). A frame
    with no detectable text is NOT auto-flagged — it may be a pure-visual frame
    (a person, a scene) with nothing to read; only *low-confidence detected
    text* needs human eyes.
    """
    frame_path = Path(frame_path)
    if not frame_path.is_file():
        raise RuntimeError(f"frame not found: {frame_path}")

    engine = _load_engine()
    # PaddleOCR 3.x .ocr(): no cls kwarg; angle classification is built in.
    result = engine.ocr(str(frame_path))
    texts: list[str] = []
    scores: list[float] = []
    # PaddleOCR 3.x returns a list of result dicts with rec_texts / rec_scores.
    # (Older 2.x returned nested [box, (text, score)] lists; we target 3.x.)
    for res in result or []:
        if hasattr(res, "get"):
            for text, score in zip(
                res.get("rec_texts") or [], res.get("rec_scores") or []
            ):
                text = (text or "").strip()
                if not text:
                    continue
                texts.append(text)
                scores.append(float(score))
        else:
            # Defensive: tolerate the legacy nested-box shape if encountered.
            for line in res or []:
                try:
                    _box, (text, score) = line
                except (ValueError, TypeError):
                    continue
                text = (text or "").strip()
                if text:
                    texts.append(text)
                    scores.append(float(score))

    confidence = round(sum(scores) / len(scores), 4) if scores else 0.0
    has_text = bool(texts)
    needs_human = has_text and confidence < _CONF_MIN

    return {
        "frame_path": frame_path.name,
        "text": "\n".join(texts),
        "confidence": confidence,
        "n_boxes": len(texts),
        "needs_human": needs_human,
    }


def ocr_frames(frame_paths: list[Path]) -> list[dict[str, Any]]:
    """OCR many frames. One engine load, reused across all frames."""
    return [ocr_frame(p) for p in frame_paths]


__all__ = ["ocr_frame", "ocr_frames"]

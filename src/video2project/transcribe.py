"""Browser-capture pipeline: local Whisper transcription.

Primary transcript source for Chinese YouTube content, which mostly lacks
caption tracks. The browser extension captures the playing audio (captureStream
→ MediaRecorder) and POSTs it here; this module runs faster-whisper locally so
no audio ever leaves the machine.

Model choice (per confirmed design): ``medium`` (int8) is the default — the
smallest size that produces *usable* Mandarin. ``small`` is the documented
fallback if ``medium`` is too slow on the host CPU. Override via env:

- ``V2P_WHISPER_MODEL``   e.g. "medium" (default), "small", "base"
- ``V2P_WHISPER_DEVICE``  "cpu" (default) or "cuda"
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_DEFAULT_MODEL = os.environ.get("V2P_WHISPER_MODEL", "medium")
_FALLBACK_MODEL = "small"
_DEVICE = os.environ.get("V2P_WHISPER_DEVICE", "cpu")
_COMPUTE = "int8"  # CPU-friendly quantization

# Language hint. Default = auto-detect (empty) so English and other videos work;
# a hard "zh" hint silently yields 0 segments on non-Chinese audio. Set
# V2P_WHISPER_LANGUAGE=zh to force Chinese when you know the content is Chinese.
_DEFAULT_LANGUAGE = os.environ.get("V2P_WHISPER_LANGUAGE", "")

_model_cache: dict[str, Any] = {}


def _load_model(name: str):
    """Lazy-load and cache a WhisperModel. Raises if faster_whisper missing."""
    if name in _model_cache:
        return _model_cache[name]
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper not installed. Run: pip install faster-whisper"
        ) from exc
    model = WhisperModel(name, device=_DEVICE, compute_type=_COMPUTE)
    _model_cache[name] = model
    return model


def _transcribe_with(model_name: str, audio_path: Path) -> tuple[list[dict], str]:
    """Run one model over the audio. Returns (segments, detected_language)."""
    model = _load_model(model_name)
    segments_iter, info = model.transcribe(
        str(audio_path),
        language=_DEFAULT_LANGUAGE or None,
        # VAD off: it discards music/quiet-speech segments entirely (0 segments on
        # songs). For videos with mixed content, let Whisper see everything.
        vad_filter=False,
        beam_size=5,
    )
    segments: list[dict[str, Any]] = []
    for seg in segments_iter:
        text = seg.text.strip()
        if not text:
            continue
        segments.append(
            {
                "start": round(float(seg.start), 3),
                "end": round(float(seg.end), 3),
                "text": text,
            }
        )
    return segments, (info.language or _DEFAULT_LANGUAGE or "")


def transcribe_audio(
    audio_path: Path,
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Transcribe an audio file to the project's transcript.json shape.

    Tries the configured model (default ``medium``); on failure falls back to
    ``small``. Returns the same dict shape as transcript.py so finalize and the
    review UI consume it unchanged.
    """
    audio_path = Path(audio_path)
    if not audio_path.is_file():
        raise RuntimeError(f"audio file not found: {audio_path}")

    metadata = metadata or {}
    order = [_DEFAULT_MODEL]
    if _DEFAULT_MODEL != _FALLBACK_MODEL:
        order.append(_FALLBACK_MODEL)

    segments: list[dict] = []
    language = ""
    used_model = order[0]
    last_exc: Exception | None = None
    for name in order:
        try:
            segments, language = _transcribe_with(name, audio_path)
            used_model = name
            break
        except Exception as exc:  # noqa: BLE001 — fall back to next model
            last_exc = exc
            continue
    if not segments and last_exc is not None:
        raise RuntimeError(f"whisper transcription failed: {last_exc}") from last_exc

    return {
        "platform": metadata.get("platform", "youtube"),
        "video_id": metadata.get("video_id", ""),
        "url": metadata.get("url", ""),
        "title": metadata.get("title", ""),
        "channel": metadata.get("channel", ""),
        "duration_s": float(metadata.get("duration_s") or 0),
        "upload_date": metadata.get("upload_date", ""),
        "language": language,
        "source": f"whisper-{used_model}",
        "segments": segments,
    }


__all__ = ["transcribe_audio"]

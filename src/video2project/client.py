"""LLM client seam — wraps Volcengine Ark (Coding Plan) via the OpenAI SDK.

Why this file exists (per Q7 from the grilling session):
> A clean client.py seam means when you add local inference (privacy, cost,
> latency), no other file changes.

CRITICAL (from user memory): the Volcengine Ark Coding Plan base URL is
`/api/coding/v1`. NEVER `/api/v3` — that bills against the ordinary Ark API
and incurs extra charges. The `ark-code-latest` model only works under
/api/coding; /api/v3 returns 404 for it.

Retry policy (Q12):
- Network/transient errors: retry 3x with exponential backoff, then soft-fail.
- Missing inputs: not handled here — that's the caller's job.
- Structural errors (bad config, missing key): loud-fail with stack trace.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError

DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/coding/v1"
DEFAULT_MODEL = os.environ.get("VOLCENGINE_MODEL", "doubao-lite")
ENV_KEY = "VOLCENGINE_API_KEY"
ENV_BASE_URL = "VOLCENGINE_BASE_URL"

MAX_RETRIES = 3
INITIAL_BACKOFF_S = 1.0
BACKOFF_FACTOR = 2.0


class LLMConfigError(RuntimeError):
    """Loud-fail: missing or invalid LLM config. Caller should not catch."""


@dataclass
class LLMResult:
    text: str
    raw: dict[str, Any]
    attempts: int
    elapsed_s: float


def _load_dotenv_if_present() -> None:
    """Minimal .env loader so we don't depend on python-dotenv at v1."""
    env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Don't override real env vars
        os.environ.setdefault(key, value)


def _client() -> OpenAI:
    _load_dotenv_if_present()
    api_key = os.environ.get(ENV_KEY)
    if not api_key or api_key == "your-volcengine-ark-key":
        raise LLMConfigError(
            f"{ENV_KEY} not set. Copy .env.example to .env and fill it in."
        )
    base_url = os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL)
    if "/v3" in base_url and "/coding" not in base_url:
        raise LLMConfigError(
            f"Refusing to use {base_url}: Volcengine Ark Coding Plan requires "
            f"/api/coding/v1. Per project rule, never use /api/v3."
        )
    return OpenAI(api_key=api_key, base_url=base_url)


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, (APIConnectionError, APITimeoutError, RateLimitError))


def chat(
    system: str,
    user: str,
    *,
    model: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 2048,
    response_format_json: bool = False,
) -> LLMResult:
    """Single-turn chat. Returns text (and raw). Retries transient errors."""
    client = _client()
    use_model = model or DEFAULT_MODEL

    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})

    kwargs: dict[str, Any] = {
        "model": use_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format_json:
        kwargs["response_format"] = {"type": "json_object"}

    backoff = INITIAL_BACKOFF_S
    start = time.monotonic()
    last_exc: BaseException | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(**kwargs)
            text = resp.choices[0].message.content or ""
            return LLMResult(
                text=text,
                raw=resp.model_dump() if hasattr(resp, "model_dump") else {},
                attempts=attempt,
                elapsed_s=time.monotonic() - start,
            )
        except Exception as exc:  # noqa: BLE001 — we re-raise non-transients
            last_exc = exc
            if not _is_transient(exc):
                # Loud-fail on structural errors
                raise
            if attempt == MAX_RETRIES:
                # Soft-fail: surface as RuntimeError so caller can decide
                raise RuntimeError(
                    f"LLM call failed after {attempt} attempts: {exc}"
                ) from exc
            time.sleep(backoff)
            backoff *= BACKOFF_FACTOR
    # Unreachable, but keep type-checkers happy
    raise RuntimeError(f"LLM call failed: {last_exc}")


def chat_json(
    system: str,
    user: str,
    *,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 2048,
) -> Any:
    """Convenience: chat() + parse JSON. Raises if response isn't valid JSON."""
    result = chat(
        system,
        user,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format_json=True,
    )
    text = result.text.strip()
    if not text:
        raise RuntimeError("LLM returned empty JSON response")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        # Some models wrap JSON in code fences despite json_object format
        cleaned = text.strip("`").strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            raise RuntimeError(
                f"LLM returned non-JSON response (attempt {result.attempts}): {text[:500]}"
            ) from exc


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "LLMConfigError",
    "LLMResult",
    "chat",
    "chat_json",
]

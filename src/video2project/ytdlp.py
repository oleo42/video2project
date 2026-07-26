"""Shared yt-dlp invocation with a resilience fallback chain.

Why this exists (P-problem: YouTube bot wall + proxy throttling):

1. Per-stream throttling — the proxy whitelists googlevideo.com but throttles
   any single long-lived connection. Fixed by forcing a fresh TCP connection
   every 1MB via ``--http-chunk-size 1M`` (each chunk reconnects at full speed).

2. Bot wall ("Sign in to confirm you're not a bot") — the proxy's exit IP is
   flagged by YouTube. Anonymous requests are rejected on some videos regardless
   of player_client. PO-token providers don't help a *flagged IP*; only an
   authenticated session (cookies) does.

So every yt-dlp call runs through :func:`attempt_chain`, which tries a sequence
of argument profiles in order and returns the first that succeeds:

    anonymous  →  cookies.txt (if YTDLP_COOKIES set)  →  PO-token script

The chain is cheap: profiles that aren't configured are skipped, and the first
success short-circuits. Errors from the final attempt are surfaced to the caller.

Config (all optional, env-driven):
- ``YTDLP_COOKIES``     path to a Netscape cookies.txt exported from a browser
                        where you're logged into YouTube.
- ``YTDLP_POT_SCRIPT``  path to the bgutil generate_once.ts server dir
                        (default ~/bgutil-ytdlp-pot-provider/server).
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Fresh connection every 1MB: defeats per-stream proxy throttling.
_CHUNK_ARGS = ["--http-chunk-size", "1M"]

_DEFAULT_POT_DIR = Path.home() / "bgutil-ytdlp-pot-provider" / "server"


@dataclass
class Attempt:
    """One yt-dlp invocation profile."""

    name: str
    extra_args: list[str]


def _cookies_path() -> Path | None:
    p = os.environ.get("YTDLP_COOKIES", "").strip()
    if not p:
        return None
    path = Path(p).expanduser()
    return path if path.is_file() else None


def _pot_script_dir() -> Path | None:
    p = os.environ.get("YTDLP_POT_SCRIPT", "").strip()
    d = Path(p).expanduser() if p else _DEFAULT_POT_DIR
    return d if (d / "src" / "generate_once.ts").is_file() else None


def build_attempts() -> list[Attempt]:
    """Order the fallback chain, skipping profiles that aren't configured."""
    attempts = [Attempt("anonymous", [])]

    cookies = _cookies_path()
    if cookies is not None:
        attempts.append(Attempt("cookies", ["--cookies", str(cookies)]))

    pot = _pot_script_dir()
    if pot is not None:
        attempts.append(
            Attempt(
                "pot",
                [
                    "--extractor-args",
                    f"youtube:pot-bgutil-script-deno=1;pot_bgutil_script_path={pot}",
                ],
            )
        )
    return attempts


def _is_bot_wall(stderr: str) -> bool:
    s = stderr.lower()
    return "not a bot" in s or "sign in to confirm" in s


def run_chain(
    base_cmd_tail: list[str],
    *,
    timeout: int,
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run yt-dlp through the fallback chain.

    ``base_cmd_tail`` is everything after ``python -m yt_dlp`` (format, output,
    url, etc.). Chunk args and each attempt's extra args are inserted for it.

    Returns ``(proc, attempt_name)`` of the first attempt with returncode 0.
    Raises RuntimeError with the last attempt's stderr if all fail.
    """
    attempts = build_attempts()
    last: subprocess.CompletedProcess[str] | None = None
    last_name = "anonymous"

    for attempt in attempts:
        # The pot script's network fetch stalls on a flagged IP; cap its budget
        # so a doomed attempt can't burn the whole timeout before the next one.
        budget = min(timeout, 45) if attempt.name == "pot" else timeout
        cmd = (
            [sys.executable, "-m", "yt_dlp", "--no-update", "--no-warnings"]
            + _CHUNK_ARGS
            + attempt.extra_args
            + base_cmd_tail
        )
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=budget)
        except subprocess.TimeoutExpired:
            # A hung attempt must not abort the chain — record and try the next.
            last, last_name = None, attempt.name
            continue
        except FileNotFoundError as exc:
            raise RuntimeError("yt-dlp not installed. Run: pip install yt-dlp") from exc

        if proc.returncode == 0:
            return proc, attempt.name

        last, last_name = proc, attempt.name
        # Only keep falling back on a bot wall; other errors are deterministic.
        if not _is_bot_wall(proc.stderr or ""):
            break

    stderr = (last.stderr if last else "").strip() or "(no stderr)"
    first_err = stderr.splitlines()[0] if stderr else "unknown"
    raise RuntimeError(f"yt-dlp failed ({last_name}): {first_err}")


__all__ = ["Attempt", "build_attempts", "run_chain"]

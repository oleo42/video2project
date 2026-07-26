"""video2project CLI — entry point for the `video2project` console script.

Subcommands:
  (no args) or <url>  Auto-chain all three stages (ingest → extract → finalize)
  ingest <url>        Download transcript + frame candidates
  extract <video_id>  LLM claim extraction + cited fallback
  finalize <video_id> Render index.md + index.json
  review <video_id>   Open the localhost review page
  list                List all videos in the project home
  doctor              Sanity-check ffmpeg, env, output dir

State + resume (Q9):
  Every per-video dir has a state.json. Each stage checks: is the artifact
  newer than its inputs? If yes, skip. --force bypasses.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__, extract, finalize, frames, paths, review, transcript
from .url import URLParseError, parse_url

# ── state.json helpers ──────────────────────────────────────────────


def _load_state(video_dir: Path) -> dict[str, Any]:
    p = video_dir / "state.json"
    if not p.exists():
        return {"stages": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"stages": {}}


def _save_state(video_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    (video_dir / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _stage_done(state: dict[str, Any], name: str) -> bool:
    return bool(state.get("stages", {}).get(name, {}).get("done_at"))


def _mark_stage(state: dict[str, Any], name: str, **fields: Any) -> None:
    state.setdefault("stages", {}).setdefault(name, {})
    state["stages"][name].update(fields)
    state["stages"][name]["done_at"] = datetime.now(timezone.utc).isoformat()


def _log_line(video_dir: Path, msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    log_path = video_dir / "run.log"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _write_missing_transcript(video_dir: Path, parsed_video: Any) -> None:
    """Write a 'no captions' transcript so downstream stages can still run."""
    out = {
        "platform": parsed_video.platform,
        "video_id": parsed_video.video_id,
        "url": parsed_video.original,
        "title": "",
        "channel": "",
        "duration_s": 0,
        "upload_date": "",
        "language": "",
        "source": "missing",
        "segments": [],
    }
    (video_dir / "transcript.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── stage runners ──────────────────────────────────────────────────


def _run_ingest(parsed_video: Any, *, force: bool) -> Path:
    """Run ingest stage. Returns the per-video dir."""
    paths.ensure_dirs(parsed_video.platform, parsed_video.video_id)
    video_dir = paths.video_dir(parsed_video.platform, parsed_video.video_id)
    state = _load_state(video_dir)

    if _stage_done(state, "ingest") and not force:
        _log_line(video_dir, "ingest: already done, skipping (use --force to rerun)")
        return video_dir

    _log_line(video_dir, f"ingest: starting (url={parsed_video.original})")

    # Stage 1a: transcript
    try:
        transcript.write_transcript(parsed_video, video_dir)
    except Exception as exc:  # noqa: BLE001 — soft-fail per Q12
        _log_line(video_dir, f"ingest: transcript fetch failed: {exc}")
        _log_line(
            video_dir,
            "  (continuing; frames will be picked without transcript alignment)",
        )
        # Write a missing-source transcript so downstream stages can proceed.
        _write_missing_transcript(video_dir, parsed_video)

    # Stage 1b: frames
    try:
        # Re-load transcript (may be empty if 1a failed)
        tr: dict[str, Any] = {}
        tr_path = video_dir / "transcript.json"
        if tr_path.exists():
            tr = json.loads(tr_path.read_text(encoding="utf-8"))
        frames.write_candidates(
            parsed_video,
            tr,
            paths.frames_dir(parsed_video.platform, parsed_video.video_id),
        )
    except Exception as exc:  # noqa: BLE001
        _log_line(video_dir, f"ingest: frame extraction failed: {exc}")
        # Write an empty candidates.json so the next stage doesn't crash
        (video_dir / "candidates.json").write_text("[]", encoding="utf-8")

    state = _load_state(video_dir)
    _mark_stage(state, "ingest")
    _save_state(video_dir, state)
    _log_line(video_dir, "ingest: done")
    return video_dir


def _run_extract(parsed_video: Any, *, force: bool) -> Path:
    video_dir = paths.video_dir(parsed_video.platform, parsed_video.video_id)
    paths.ensure_dirs(parsed_video.platform, parsed_video.video_id)
    state = _load_state(video_dir)

    if _stage_done(state, "extract") and not force:
        _log_line(video_dir, "extract: already done, skipping (use --force to rerun)")
        return video_dir

    tr_path = video_dir / "transcript.json"
    if not tr_path.exists():
        _log_line(video_dir, "extract: no transcript.json — run `ingest` first")
        sys.exit(2)
    transcript_data: dict[str, Any] = json.loads(tr_path.read_text(encoding="utf-8"))

    if not transcript_data.get("segments"):
        _log_line(
            video_dir, "extract: transcript is empty (no captions) — nothing to extract"
        )
        # Still write an empty claims.json so finalize works
        (video_dir / "claims.json").write_text("[]", encoding="utf-8")
    else:
        _log_line(
            video_dir,
            f"extract: starting ({len(transcript_data['segments'])} segments)",
        )
        try:
            claims = extract.write_claims(transcript_data, video_dir / "claims.json")
            _log_line(video_dir, f"extract: done ({len(claims)} claims)")
        except Exception as exc:  # noqa: BLE001
            _log_line(video_dir, f"extract: failed: {exc}")
            (video_dir / "claims.json").write_text("[]", encoding="utf-8")
            _log_line(video_dir, "  (continuing with empty claims)")

    state = _load_state(video_dir)
    _mark_stage(state, "extract")
    _save_state(video_dir, state)
    return video_dir


def _run_finalize(parsed_video: Any, *, force: bool) -> Path:
    video_dir = paths.video_dir(parsed_video.platform, parsed_video.video_id)
    paths.ensure_dirs(parsed_video.platform, parsed_video.video_id)
    state = _load_state(video_dir)

    if _stage_done(state, "finalize") and not force:
        _log_line(video_dir, "finalize: already done, skipping (use --force to rerun)")
        return video_dir

    if not (video_dir / "transcript.json").exists():
        _log_line(video_dir, "finalize: no transcript.json — run `ingest` first")
        sys.exit(2)

    _log_line(video_dir, "finalize: rendering index.md + index.json")
    md_path, js_path = finalize.finalize(video_dir)
    _log_line(video_dir, f"finalize: done ({md_path.name}, {js_path.name})")

    state = _load_state(video_dir)
    _mark_stage(state, "finalize")
    _save_state(video_dir, state)
    return video_dir


# ── subcommand implementations ─────────────────────────────────────


def cmd_ingest(args: argparse.Namespace) -> int:
    try:
        parsed = parse_url(args.url)
    except URLParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _run_ingest(parsed, force=args.force)
    print(f"video dir: {paths.video_dir(parsed.platform, parsed.video_id)}")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    platform, video_id = _split_video_id(args.video_id)
    from .url import ParsedURL

    parsed = ParsedURL(platform=platform, video_id=video_id, original=args.video_id)
    _run_extract(parsed, force=args.force)
    return 0


def cmd_finalize(args: argparse.Namespace) -> int:
    platform, video_id = _split_video_id(args.video_id)
    from .url import ParsedURL

    parsed = ParsedURL(platform=platform, video_id=video_id, original=args.video_id)
    _run_finalize(parsed, force=args.force)
    return 0


def cmd_auto(args: argparse.Namespace) -> int:
    """Auto-chain: ingest → extract → finalize."""
    try:
        parsed = parse_url(args.url)
    except URLParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    started = time.monotonic()
    _run_ingest(parsed, force=args.force)
    _run_extract(parsed, force=args.force)
    _run_finalize(parsed, force=args.force)
    elapsed = time.monotonic() - started
    video_dir = paths.video_dir(parsed.platform, parsed.video_id)
    print(f"\nDone in {elapsed:.1f}s")
    print(f"  index.md:    {video_dir / 'index.md'}")
    print(f"  index.json:  {video_dir / 'index.json'}")
    print(f"  Next: video2project review {parsed.platform}__{parsed.video_id}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    platform, video_id = _split_video_id(args.video_id)
    video_dir = paths.video_dir(platform, video_id)
    if not video_dir.exists():
        print(
            f"error: {video_dir} does not exist. Run `ingest` first.", file=sys.stderr
        )
        return 2
    review.serve(video_dir, open_browser=not args.no_browser, port=args.port)
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    videos_root = paths.VIDEOS_DIR
    if not videos_root.exists():
        print(f"No videos yet. Output root: {videos_root}")
        return 0
    entries = sorted(videos_root.iterdir())
    if not entries:
        print("(no videos)")
        return 0
    for entry in entries:
        if not entry.is_dir():
            continue
        has_md = (entry / "index.md").exists()
        marker = "✓" if has_md else "·"
        print(f"  {marker} {entry.name}")
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    print(f"video2project v{__version__}")
    print(f"  python:   {sys.version.split()[0]}")
    print(f"  ffmpeg:   {shutil.which('ffmpeg') or 'NOT FOUND'}")
    print(f"  yt-dlp:   {shutil.which('yt-dlp') or '(use python -m yt_dlp)'}")
    print(f"  home:     {paths.HOME}")
    print(f"  videos:   {paths.VIDEOS_DIR}")
    # Quick env check (without exposing the key)
    import os

    api_key = os.environ.get("VOLCENGINE_API_KEY", "")
    if not api_key or api_key == "your-volcengine-ark-key":
        print("  api key:  NOT SET (copy .env.example to .env and fill)")
    else:
        print(f"  api key:  set ({api_key[:4]}…{api_key[-2:]})")
    base_url = os.environ.get("VOLCENGINE_BASE_URL", "")
    if base_url and "/v3" in base_url and "/coding" not in base_url:
        print(f"  WARNING:  base URL {base_url} is wrong (must be /api/coding/v1)")
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    """Start the browser-capture ingest server (extension POSTs audio/frames)."""
    from . import ingest_server

    try:
        ingest_server.serve(port=args.port)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


# ── helpers ────────────────────────────────────────────────────────


def _split_video_id(arg: str) -> tuple[str, str]:
    """Accept 'youtube__ID' or bare 'ID' (defaults to youtube)."""
    if "__" in arg:
        platform, _, vid = arg.partition("__")
        return platform, vid
    return "youtube", arg


# ── argparse ───────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="video2project",
        description="Paste a YouTube URL, get a verified markdown + JSON knowledge artifact.",
    )
    sub = p.add_subparsers(dest="cmd")

    # Default (no subcommand): treat argv[1] as a URL
    p.set_defaults(func=None)

    # `ingest`
    sp = sub.add_parser("ingest", help="Download transcript + frame candidates")
    sp.add_argument("url")
    sp.add_argument("--force", action="store_true", help="Re-run even if stage is done")
    sp.set_defaults(func=cmd_ingest)

    # `extract`
    sp = sub.add_parser("extract", help="LLM claim extraction + cited fallback")
    sp.add_argument("video_id", help="e.g. youtube__GYMyDFwNULk")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_extract)

    # `finalize`
    sp = sub.add_parser("finalize", help="Render index.md + index.json")
    sp.add_argument("video_id")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_finalize)

    # `review`
    sp = sub.add_parser("review", help="Open the review page on localhost")
    sp.add_argument("video_id")
    sp.add_argument("--port", type=int, default=None)
    sp.add_argument("--no-browser", action="store_true")
    sp.set_defaults(func=cmd_review)

    # `list`
    sp = sub.add_parser("list", help="List all videos in the project home")
    sp.set_defaults(func=cmd_list)

    # `doctor`
    sp = sub.add_parser("doctor", help="Sanity-check ffmpeg, env, output dir")
    sp.set_defaults(func=cmd_doctor)

    # `capture` — browser-capture ingest server
    sp = sub.add_parser("capture", help="Start ingest server for the browser extension")
    sp.add_argument("--port", type=int, default=8765)
    sp.set_defaults(func=cmd_capture)

    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()

    # No subcommand: treat first arg as a URL → auto-chain
    if argv and argv[0] not in {"-h", "--help"} and not argv[0].startswith("-"):
        # If it looks like a subcommand, defer to argparse; else treat as URL
        if argv[0] in {
            "ingest",
            "extract",
            "finalize",
            "review",
            "list",
            "doctor",
            "capture",
        }:
            pass
        else:
            ns = argparse.Namespace(url=argv[0], force=False)
            return cmd_auto(ns)

    ns = parser.parse_args(argv)
    if ns.func is None:
        parser.print_help()
        return 0
    return ns.func(ns)


if __name__ == "__main__":
    sys.exit(main())

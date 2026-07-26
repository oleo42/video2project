"""Central path registry — single source of truth for filesystem layout.

All modules resolve inputs/outputs through these constants. Never hard-code
`Path(__file__).parent` for shared data; use `paths.VIDEOS_DIR / ...` etc.

Pattern borrowed from fantasyXclosest/src/fantasyx/paths.py.
"""

from __future__ import annotations

import os
from pathlib import Path

# ── Package / repo roots ─────────────────────────────────────────────
PACKAGE_ROOT = Path(__file__).resolve().parent  # src/video2project
SRC_ROOT = PACKAGE_ROOT.parent  # src
REPO_ROOT = SRC_ROOT.parent  # repo root
PROMPTS_DIR = PACKAGE_ROOT / "prompts"

# ── Project home (output location) ──────────────────────────────────
# Default: ~/Documents/video2project/. Override via VIDEO2PROJECT_HOME env var.
_DEFAULT_HOME = Path.home() / "Documents" / "video2project"
HOME = Path(os.environ.get("VIDEO2PROJECT_HOME", str(_DEFAULT_HOME))).resolve()

# ── Per-video layout ────────────────────────────────────────────────
VIDEOS_DIR = HOME / "videos"


def video_dir(platform: str, video_id: str) -> Path:
    """Per-video artifact directory. Platform + id disambiguates YouTube vs Bilibili."""
    return VIDEOS_DIR / f"{platform}__{video_id}"


def state_json(platform: str, video_id: str) -> Path:
    return video_dir(platform, video_id) / "state.json"


def transcript_json(platform: str, video_id: str) -> Path:
    return video_dir(platform, video_id) / "transcript.json"


def candidates_json(platform: str, video_id: str) -> Path:
    return video_dir(platform, video_id) / "candidates.json"


def claims_json(platform: str, video_id: str) -> Path:
    return video_dir(platform, video_id) / "claims.json"


def index_md(platform: str, video_id: str) -> Path:
    return video_dir(platform, video_id) / "index.md"


def index_json(platform: str, video_id: str) -> Path:
    return video_dir(platform, video_id) / "index.json"


def frames_dir(platform: str, video_id: str) -> Path:
    return video_dir(platform, video_id) / "frames"


def run_log(platform: str, video_id: str) -> Path:
    return video_dir(platform, video_id) / "run.log"


def ensure_dirs(platform: str, video_id: str) -> None:
    """Create all per-video dirs on demand. Safe to call repeatedly."""
    video_dir(platform, video_id).mkdir(parents=True, exist_ok=True)
    frames_dir(platform, video_id).mkdir(parents=True, exist_ok=True)
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    HOME.mkdir(parents=True, exist_ok=True)


# ── Review server ───────────────────────────────────────────────────
REVIEW_HOST = "localhost"
REVIEW_PORT = 8765

__all__ = [
    "PACKAGE_ROOT",
    "SRC_ROOT",
    "REPO_ROOT",
    "PROMPTS_DIR",
    "HOME",
    "VIDEOS_DIR",
    "video_dir",
    "state_json",
    "transcript_json",
    "candidates_json",
    "claims_json",
    "index_md",
    "index_json",
    "frames_dir",
    "run_log",
    "ensure_dirs",
    "REVIEW_HOST",
    "REVIEW_PORT",
]

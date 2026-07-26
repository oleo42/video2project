"""Smoke test for video2project.

Mocks the LLM client (and the URL→yt-dlp path is never exercised in tests).
Verifies the full pipeline produces the expected artifacts
(transcript.json, candidates.json, claims.json, index.md, index.json)
without hitting the network.

Run:  pytest tests/test_smoke.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make src/ importable without installing
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video2project import cli, paths  # noqa: E402
from video2project.url import ParsedURL, URLParseError, parse_url  # noqa: E402

FIXTURE_TRANSCRIPT = json.loads(
    (ROOT / "tests" / "fixtures" / "transcript.json").read_text(encoding="utf-8")
)


# ── mock helpers ───────────────────────────────────────────────────


def _make_claims_stub() -> list[dict]:
    return [
        {
            "id": "c1",
            "text": "Photosynthesis converts light energy into chemical energy.",
            "timestamp_start": 4.5,
            "timestamp_end": 9.0,
            "claim_type": "factual",
            "why_check": "Core claim of the video; foundational biology fact.",
        },
        {
            "id": "c2",
            "text": "The photosynthesis equation is 6 CO2 + 6 H2O + light -> C6H12O6 + 6 O2.",
            "timestamp_start": 9.0,
            "timestamp_end": 14.0,
            "claim_type": "mathematical",
            "why_check": "Specific stoichiometric coefficients.",
        },
        {
            "id": "c3",
            "text": "Jan Ingenhousz demonstrated photosynthesis in 1779.",
            "timestamp_start": 14.0,
            "timestamp_end": 22.0,
            "claim_type": "historical",
            "why_check": "Attribution and date.",
        },
    ]


def _make_citation_stub() -> dict:
    return {
        "sources": [
            {
                "url": "https://en.wikipedia.org/wiki/Photosynthesis",
                "title": "Photosynthesis - Wikipedia",
                "agree": True,
                "one_line": "Encyclopedic confirmation.",
            }
        ],
        "confidence": "high",
        "caveat": None,
    }


# ── fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Point VIDEO2PROJECT_HOME at a temp dir for isolation."""
    home = tmp_path / "v2p_home"
    home.mkdir()
    monkeypatch.setenv("VIDEO2PROJECT_HOME", str(home))
    import importlib

    importlib.reload(paths)
    yield home


def _seed_video(video_dir: Path, *, claims: bool = True) -> None:
    """Write the standard set of artifacts for a video."""
    video_dir.mkdir(parents=True, exist_ok=True)
    (video_dir / "transcript.json").write_text(
        json.dumps(FIXTURE_TRANSCRIPT), encoding="utf-8"
    )
    (video_dir / "candidates.json").write_text(
        json.dumps(
            [
                {
                    "index": 1,
                    "timestamp_s": 4.5,
                    "frame_path": "frames/frame_0001.png",
                    "extracted": True,
                    "accepted": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    if claims:
        (video_dir / "claims.json").write_text(
            json.dumps(
                [
                    {
                        "id": "c1",
                        "text": "Photosynthesis converts light energy into chemical energy.",
                        "claim_type": "factual",
                        "timestamp_start": 4.5,
                        "timestamp_end": 9.0,
                        "why_check": "Core biology fact.",
                        "sources": [
                            {
                                "url": "https://en.wikipedia.org/wiki/Photosynthesis",
                                "title": "Wikipedia",
                                "agree": True,
                                "one_line": "Encyclopedic source.",
                            }
                        ],
                        "confidence": "high",
                        "caveat": "⚠️ unverified — LLM-cited (no live search at v1)",
                    }
                ]
            ),
            encoding="utf-8",
        )


# ── tests ──────────────────────────────────────────────────────────


def test_url_parser_yt_variants():
    cases = [
        ("https://youtu.be/abc123XYZ", "abc123XYZ"),
        ("https://www.youtube.com/watch?v=abc123XYZ", "abc123XYZ"),
        ("https://youtube.com/watch?v=abc123XYZ&t=42s", "abc123XYZ"),
        ("https://www.youtube.com/shorts/abc123XYZ", "abc123XYZ"),
        ("https://www.youtube.com/embed/abc123XYZ", "abc123XYZ"),
    ]
    for url, want_id in cases:
        p = parse_url(url)
        assert p.platform == "youtube"
        assert p.video_id == want_id


def test_url_parser_rejects_garbage():
    for bad in ["", "not a url", "https://example.com/foo", "https://vimeo.com/123"]:
        with pytest.raises(URLParseError):
            parse_url(bad)


def test_url_parser_rejects_bilibili_at_v1():
    with pytest.raises(URLParseError, match="Bilibili.*v2"):
        parse_url("https://www.bilibili.com/video/BV1xx")


def test_pick_candidate_timestamps_anchors():
    """Without gaps in the fixture, only anchors are returned."""
    from video2project.frames import _pick_candidate_timestamps

    timestamps = _pick_candidate_timestamps(FIXTURE_TRANSCRIPT, duration_s=180)
    assert 3 <= len(timestamps) <= 12
    assert all(0 <= t <= 180 for t in timestamps)
    assert timestamps == sorted(timestamps)
    assert timestamps[0] < 5
    # The last anchor is at (n_anchors / (n_anchors+1)) * duration; not necessarily 180.
    assert timestamps[-1] > 0.6 * 180
    diffs = [timestamps[i + 1] - timestamps[i] for i in range(len(timestamps) - 1)]
    avg = sum(diffs) / len(diffs)
    assert 10 <= avg <= 90


def test_pick_candidate_timestamps_picks_topic_shifts():
    """With explicit gaps, those gaps should be picked as topic shifts."""
    from video2project.frames import _pick_candidate_timestamps

    tr = {
        "segments": [
            {"start": 0.0, "end": 5.0, "text": "a"},
            {"start": 5.0, "end": 10.0, "text": "b"},
            # 3.0s gap here — should be a topic shift
            {"start": 13.0, "end": 18.0, "text": "c"},
        ]
    }
    timestamps = _pick_candidate_timestamps(tr, duration_s=30)
    # The end of segment 2 (~10.1) should be picked
    assert any(9 <= t <= 12 for t in timestamps), timestamps


def test_vtt_parser_smoke():
    from video2project.transcript import _build_transcript_from_subs
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".vtt", delete=False) as f:
        f.write("""WEBVTT

00:00:00.000 --> 00:00:02.000
Hello world.

00:00:02.000 --> 00:00:05.000
This is a <i>test</i>.
""")
        path = Path(f.name)
    try:
        segs = _build_transcript_from_subs(path)
        assert len(segs) == 2
        assert segs[0]["text"] == "Hello world."
        assert segs[1]["text"] == "This is a test."
    finally:
        path.unlink()


def test_finalize_renders_outputs(tmp_home):
    platform, video_id = "youtube", "RENDERTEST"
    video_dir = paths.video_dir(platform, video_id)
    _seed_video(video_dir)

    video_dir = cli._run_finalize(
        ParsedURL(platform=platform, video_id=video_id, original=""), force=True
    )
    md_path = video_dir / "index.md"
    js_path = video_dir / "index.json"
    assert md_path.exists()
    assert js_path.exists()

    md = md_path.read_text(encoding="utf-8")
    assert "Test Video: Why Photosynthesis Matters" in md
    assert "Photosynthesis converts light energy" in md
    assert "https://en.wikipedia.org/wiki/Photosynthesis" in md
    assert "Key frames" in md
    assert "Verified claims" in md
    assert "Transcript" in md
    assert "⚠️" in md

    js = json.loads(js_path.read_text(encoding="utf-8"))
    # The transcript.json was seeded from the fixture (id = SMOKETEST01).
    # We assert finalize is faithful to its inputs.
    assert js["video"]["id"] == "SMOKETEST01"
    assert js["video"]["title"] == "Test Video: Why Photosynthesis Matters"
    assert len(js["frames"]) == 1
    assert len(js["claims"]) == 1
    assert js["claims"][0]["sources"][0]["url"].startswith("https://")


def test_extract_with_mocked_llm(tmp_home):
    from video2project import extract

    platform, video_id = "youtube", "EXTTEST"
    video_dir = paths.video_dir(platform, video_id)
    video_dir.mkdir(parents=True)
    (video_dir / "transcript.json").write_text(
        json.dumps(FIXTURE_TRANSCRIPT), encoding="utf-8"
    )

    with patch.object(extract.client, "chat_json") as mock_chat:
        mock_chat.side_effect = [
            {"claims": _make_claims_stub()},
            _make_citation_stub(),
            _make_citation_stub(),
            _make_citation_stub(),
        ]
        claims = extract.write_claims(FIXTURE_TRANSCRIPT, video_dir / "claims.json")

    assert len(claims) == 3
    assert all(c.get("sources") for c in claims)
    assert all("unverified" in (c.get("caveat") or "") for c in claims)
    valid_types = {
        "factual",
        "causal",
        "definition",
        "procedural",
        "mathematical",
        "historical",
        "statistical",
    }
    for c in claims:
        assert c["id"]
        assert c["text"]
        assert c["claim_type"] in valid_types


def test_state_json_resume(tmp_home):
    """Re-running a stage that already has state.json should be a no-op."""
    platform, video_id = "youtube", "RESUMETEST"
    video_dir = paths.video_dir(platform, video_id)
    video_dir.mkdir(parents=True)

    # Seed state.json with finalize already done
    (video_dir / "state.json").write_text(
        json.dumps({"stages": {"finalize": {"done_at": "2026-01-01T00:00:00Z"}}}),
        encoding="utf-8",
    )
    (video_dir / "index.md").write_text("PREEXISTING\n", encoding="utf-8")

    # Without --force, should NOT re-render
    video_dir = cli._run_finalize(
        ParsedURL(platform=platform, video_id=video_id, original=""),
        force=False,
    )
    assert (video_dir / "index.md").read_text(encoding="utf-8") == "PREEXISTING\n"

    # With --force and proper inputs, should re-render
    (video_dir / "transcript.json").write_text(
        json.dumps(FIXTURE_TRANSCRIPT), encoding="utf-8"
    )
    (video_dir / "candidates.json").write_text("[]", encoding="utf-8")
    (video_dir / "claims.json").write_text("[]", encoding="utf-8")
    video_dir = cli._run_finalize(
        ParsedURL(platform=platform, video_id=video_id, original=""),
        force=True,
    )
    assert "Test Video" in (video_dir / "index.md").read_text(encoding="utf-8")


def test_cli_help_runs(tmp_home):
    """CLI help doesn't crash; doctor runs without network."""
    from video2project.cli import _build_parser, main

    p = _build_parser()
    with pytest.raises(SystemExit) as exc:
        p.parse_args(["--help"])
    assert exc.value.code == 0
    rc = main(["doctor"])
    assert rc == 0


def test_cli_list_empty(tmp_home):
    """`list` on an empty home returns 0 without crashing."""
    rc = cli.main(["list"])
    assert rc == 0


def test_review_template_renders(tmp_home):
    """Render the review HTML template and check the substitution."""
    from video2project.review import _REVIEW_HTML

    platform, video_id = "youtube", "REVIEWTEST"
    video_dir = paths.video_dir(platform, video_id)
    _seed_video(video_dir, claims=False)

    candidates_data: list = []
    claims_data: list = []
    html = _REVIEW_HTML.format(
        video_id="REVIEWTEST",
        title="Test Video",
        video_dir=str(video_dir),
        n_candidates=len(candidates_data),
        n_claims=len(claims_data),
        candidates_json=json.dumps(candidates_data),
        claims_json=json.dumps(claims_data),
        transcript_json=json.dumps(FIXTURE_TRANSCRIPT),
    )
    assert "video2project review" in html
    assert "REVIEWTEST" in html
    assert "Test Video" in html
    assert "renderFrames" in html
    assert "renderClaims" in html
    assert "saveAll" in html


def test_review_serve_smoke(tmp_home):
    """Start the review server on a free port, hit /api/save, stop."""
    import socket
    import time
    import urllib.request
    from video2project import review as review_mod

    platform, video_id = "youtube", "SERVETEST"
    video_dir = paths.video_dir(platform, video_id)
    _seed_video(video_dir, claims=True)

    # Pick a free port
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    review_mod.serve(
        video_dir, open_browser=False, host="127.0.0.1", port=port, block=False
    )

    # Wait for the server to bind
    for _ in range(20):
        with socket.socket() as s:
            s.settimeout(0.2)
            try:
                s.connect(("127.0.0.1", port))
                break
            except OSError:
                time.sleep(0.05)
    else:
        pytest.fail("review server did not bind")

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
        body = r.read().decode("utf-8")
    assert "video2project review" in body

    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/save",
        data=json.dumps({"kind": "candidates", "data": []}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        resp = json.loads(r.read().decode("utf-8"))
    assert resp == {"ok": True}
    assert json.loads((video_dir / "candidates.json").read_text(encoding="utf-8")) == []


def test_base_url_guard(tmp_home, monkeypatch):
    """The /v3 base URL must be rejected loudly."""
    monkeypatch.setenv("VOLCENGINE_API_KEY", "sk-test")
    monkeypatch.setenv(
        "VOLCENGINE_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"
    )
    from video2project import client

    with pytest.raises(client.LLMConfigError, match="/api/coding/v1"):
        client._client()


def test_missing_key_raises(tmp_home, monkeypatch):
    """No API key → LLMConfigError."""
    monkeypatch.delenv("VOLCENGINE_API_KEY", raising=False)
    from video2project import client
    import importlib

    importlib.reload(client)
    with pytest.raises(client.LLMConfigError, match="not set"):
        client._client()

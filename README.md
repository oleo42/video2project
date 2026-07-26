# video2project

Paste a YouTube URL, get back a verified markdown + JSON knowledge artifact per video: timestamped key-frame notes, transcript, and LLM-cited claims (3 sources each, ⚠️ unverified tagged where the model had to fall back to its own training data).

## Install

```bash
pip install -e .
cp .env.example .env  # fill in VOLCENGINE_API_KEY
```

Requires: Python ≥3.10, `ffmpeg` on PATH.

## Quick start

```bash
video2project "https://www.youtube.com/watch?v=GYMyDFwNULk"
# → runs all three stages: ingest → extract → finalize
# → opens ~/Documents/video2project/videos/youtube__GYMyDFwNULk/
#   index.md        # the deliverable
#   index.json      # machine-readable twin
#   transcript.json
#   frames/         # 720p PNGs at candidate timestamps
#   candidates.json # frame candidates (editable)
#   claims.json     # extracted claims + sources (editable)
#   run.log
```

## Subcommands

| Command | What it does |
|---|---|
| `video2project <url>` | Auto-chains all three stages. Resume via `state.json`. |
| `video2project ingest <url>` | Downloads captions + frame candidates. Idempotent. |
| `video2project extract <video_id>` | LLM claim extraction + cited fallback. |
| `video2project finalize <video_id>` | Renders `index.md` + `index.json`. |
| `video2project review <video_id>` | Opens `localhost:8765` review page (candidates + claims). |
| `video2project list` | Lists all videos in the project home. |
| `video2project doctor` | Checks ffmpeg, env, output dir; prints version. |

`--force` on any stage re-runs even if the artifact is newer than the source.

## Output location

Default: `~/Documents/video2project/`. Override with `VIDEO2PROJECT_HOME`.

## Project layout (per video)

```
<VIDEO2PROJECT_HOME>/videos/<platform>__<id>/
├── index.md          # the deliverable (read this)
├── index.json        # machine-readable twin (consume this)
├── transcript.json   # raw transcript with timestamps
├── frames/           # 720p PNGs at candidate timestamps
├── candidates.json   # frame candidates (editable)
├── claims.json       # extracted claims + sources (editable)
├── state.json        # per-stage completion + run metadata
└── run.log           # this run's log
```

## Scope (v1)

- YouTube only (Bilibili in v2)
- Captions only (Whisper fallback in v2)
- LLM-cited fallback with ⚠️ unverified (live search in v2)
- Source check only (logic/math check in v2)
- Smoke test uses fixtures, not live network

See `docs/PLAN.md` (from the grilling session) for the full decision log.

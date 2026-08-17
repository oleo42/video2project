# Contributing

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # fill in VOLCENGINE_API_KEY
```

Requires Python >= 3.10 and `ffmpeg` on PATH. Verify with:

```bash
video2project doctor
```

The browser-capture pipeline (Whisper + PaddleOCR) is an optional extra:

```bash
pip install -e ".[capture,dev]"
```

## Tests

```bash
pytest -q
```

Tests must not hit the network or call a real LLM. The existing suite stubs
the client via `monkeypatch`; follow that pattern. Anything that needs
`VOLCENGINE_API_KEY` to be real belongs in a manual check, not in `pytest`.

## Architecture

Read `docs/ARCHITECTURE.md` first. The short version: the pipeline is three
resumable stages — `ingest` -> `extract` -> `finalize` — and each stage
writes its output to disk so a later stage can be re-run without redoing
the earlier work. `state.json` tracks progress per video.

Two consequences for contributions:

- **Stages stay idempotent.** Re-running a stage on the same input must not
  duplicate or corrupt prior output. Cache keys are content hashes, not
  timestamps.
- **Artifacts are the interface.** If you change the shape of
  `candidates.json`, `claims.json`, or `transcript.json`, update the stage
  that reads it and the tests that assert on it.

## Claim extraction

Claims carry sources. When the model falls back to its own training data
instead of the transcript, the claim is tagged unverified — do not remove
that distinction, it is the point of the tool.

## Secrets

Never commit `.env`. Only `.env.example` is tracked. API keys are read from
the environment at call time via `client.py`; do not thread them through
function signatures or log them.

## Browser extension

`extension/` is a Manifest V3 extension that POSTs to the local ingest
server on `localhost:8765`. After editing it, mirror it with
`scripts/mirror_extension.sh` and reload the unpacked extension in Chrome.

"""Review server: serve the per-video review HTML on localhost.

The HTML page shows:
- Accepted/rejected frame candidates (thumbs) — toggle accepted inline
- Extracted claims with sources — toggle accepted/edited
- The transcript

Edits are POSTed back to the server, which writes them to the JSON files
on disk. No external JS deps; vanilla DOM only.
"""

from __future__ import annotations

import http.server
import json
import socket
import socketserver
import sys
import threading
from urllib.parse import urlparse
from typing import Any
from pathlib import Path

from .paths import REVIEW_HOST, REVIEW_PORT

# ── manual-mark queue (review page ⇄ extension hand-off) ───────────
# The review page and the extension are different documents, so they can't talk
# directly. The review page queues a mark here; the extension polls for pending
# marks, captures the frame on its YouTube tab, and completes it.
_PENDING_LOCK = threading.Lock()
_PENDING_MARKS: list[dict[str, Any]] = []
_MARK_SEQ = [0]


def _push_pending_mark(timestamp_s: float) -> dict[str, Any]:
    with _PENDING_LOCK:
        _MARK_SEQ[0] += 1
        mark = {"id": _MARK_SEQ[0], "timestamp_s": timestamp_s}
        _PENDING_MARKS.append(mark)
        return mark


def _pop_pending_marks(*, peek: bool = False, mark_id: Any = None) -> list[dict]:
    """List pending marks (peek) or remove one by id. Returns the list."""
    with _PENDING_LOCK:
        if mark_id is not None:
            removed = [m for m in _PENDING_MARKS if m["id"] == mark_id]
            _PENDING_MARKS[:] = [m for m in _PENDING_MARKS if m["id"] != mark_id]
            return removed
        return list(_PENDING_MARKS)


# ── HTML template ───────────────────────────────────────────────────
_REVIEW_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>video2project review — {video_id}</title>
<style>
  :root {{
    --bg: #0f1115; --fg: #e6e6e6; --muted: #888;
    --accent: #6aa9ff; --warn: #ffb454; --bad: #ff6b6b;
    --card: #181b22; --border: #2a2f3a;
  }}
  * {{ box-sizing: border-box; }}
  body {{ font: 14px/1.5 system-ui, sans-serif; margin: 0; background: var(--bg); color: var(--fg); }}
  header {{ padding: 16px 24px; border-bottom: 1px solid var(--border); display: flex; align-items: baseline; gap: 16px; }}
  header h1 {{ margin: 0; font-size: 18px; }}
  header .meta {{ color: var(--muted); font-size: 12px; }}
  main {{ padding: 24px; max-width: 1200px; margin: 0 auto; }}
  section {{ margin-bottom: 32px; }}
  h2 {{ font-size: 16px; margin: 0 0 12px; padding-bottom: 8px; border-bottom: 1px solid var(--border); }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }}
  .card.rejected {{ opacity: 0.4; }}
  .card img {{ display: block; width: 100%; height: auto; background: #000; }}
  .card .meta {{ padding: 8px 10px; font-size: 12px; color: var(--muted); display: flex; justify-content: space-between; }}
  .card .actions {{ padding: 6px 10px; border-top: 1px solid var(--border); display: flex; gap: 6px; }}
  .card .actions button {{ background: transparent; color: var(--fg); border: 1px solid var(--border); border-radius: 4px; padding: 4px 8px; cursor: pointer; font-size: 11px; }}
  .card .actions button:hover {{ border-color: var(--accent); }}
  .card.accepted .actions button.toggle {{ background: var(--accent); color: #000; border-color: var(--accent); }}
  .claim {{ background: var(--card); border: 1px solid var(--border); border-radius: 6px; padding: 14px 16px; margin-bottom: 12px; }}
  .claim.rejected {{ opacity: 0.5; }}
  .claim h3 {{ margin: 0 0 6px; font-size: 14px; }}
  .claim .meta {{ font-size: 12px; color: var(--muted); margin-bottom: 8px; }}
  .claim .caveat {{ color: var(--warn); font-size: 12px; margin: 6px 0; }}
  .claim ul {{ margin: 6px 0; padding-left: 20px; }}
  .claim li {{ margin: 3px 0; font-size: 13px; }}
  .claim .agree-true {{ color: #5edda0; }}
  .claim .agree-false {{ color: var(--bad); }}
  .claim .agree-unrelated {{ color: var(--muted); }}
  .toolbar {{ position: sticky; top: 0; background: var(--bg); padding: 12px 0; border-bottom: 1px solid var(--border); margin-bottom: 16px; display: flex; gap: 8px; align-items: center; }}
  .toolbar button {{ background: var(--accent); color: #000; border: 0; padding: 6px 14px; border-radius: 4px; cursor: pointer; font-weight: 600; }}
  .toolbar button.secondary {{ background: transparent; color: var(--fg); border: 1px solid var(--border); }}
  .status {{ font-size: 12px; color: var(--muted); margin-left: 8px; }}
  .ts {{ color: var(--muted); font-family: monospace; }}
  a {{ color: var(--accent); }}
</style>
</head>
<body>
<header>
  <h1>video2project review</h1>
  <span class="meta">{title}</span>
  <span class="meta">— {video_dir}</span>
</header>
<main>
  <div class="toolbar">
    <button onclick="saveAll()">Save changes</button>
    <button class="secondary" onclick="location.reload()">Reload</button>
    <span class="status" id="status"></span>
  </div>

  <section>
    <h2>Frame candidates ({n_candidates})</h2>
    <div style="margin-bottom:12px;display:flex;gap:8px;align-items:center">
      <input id="markTs" type="number" step="0.1" min="0" placeholder="seconds (e.g. 83.5)"
        style="background:var(--card);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:6px 8px;width:160px" />
      <button class="secondary" onclick="addMark()"
        style="background:transparent;color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:6px 12px;cursor:pointer">+ Mark timestamp</button>
      <span class="status" id="markStatus"></span>
    </div>
    <div class="grid" id="frames"></div>
  </section>

  <section>
    <h2>Claims ({n_claims})</h2>
    <div id="claims"></div>
  </section>

  <section>
    <h2>Transcript</h2>
    <div id="transcript" style="max-height: 400px; overflow-y: auto; padding: 8px; background: var(--card); border-radius: 6px;"></div>
  </section>
</main>

<script>
const candidates = {candidates_json};
const claims = {claims_json};
const transcript = {transcript_json};

function renderFrames() {{
  const el = document.getElementById('frames');
  el.innerHTML = '';
  candidates.forEach((c, i) => {{
    const card = document.createElement('div');
    card.className = 'card' + (c.accepted ? ' accepted' : ' rejected');
    const ts = c.timestamp_s || 0;
    const mm = Math.floor(ts/60), ss = Math.floor(ts%60);
    const imgPath = c.frame_path || ('frames/frame_' + String(c.index).padStart(4,'0') + '.png');
    const needsHuman = c.needs_human ? ' <span style="color:var(--warn)">⚠ check OCR</span>' : '';
    const ocrBlock = (c.ocr_text && c.ocr_text.trim())
      ? `<div style="padding:6px 10px;font-size:11px;color:var(--muted);border-top:1px solid var(--border);white-space:pre-wrap;max-height:80px;overflow-y:auto">${{escape(c.ocr_text)}}</div>`
      : '';
    card.innerHTML = `
      <img src="${{imgPath}}" onerror="this.style.background='#333';this.alt='(missing)'" />
      <div class="meta"><span class="ts">${{String(mm).padStart(2,'0')}}:${{String(ss).padStart(2,'0')}}</span><span>frame #${{c.index}}${{needsHuman}}</span></div>
      ${{ocrBlock}}
      <div class="actions">
        <button class="toggle" onclick="toggleFrame(${{i}})">${{c.accepted ? 'accepted' : 'rejected'}}</button>
      </div>`;
    el.appendChild(card);
  }});
}}

function renderClaims() {{
  const el = document.getElementById('claims');
  el.innerHTML = '';
  claims.forEach((c, i) => {{
    const div = document.createElement('div');
    div.className = 'claim' + (c.accepted === false ? ' rejected' : '');
    const meta = [];
    if (c.claim_type) meta.push('type: ' + c.claim_type);
    if (c.timestamp_start != null) {{
      const ts = c.timestamp_start;
      const mm = Math.floor(ts/60), ss = Math.floor(ts%60);
      meta.push('<span class="ts">' + String(mm).padStart(2,'0') + ':' + String(ss).padStart(2,'0') + '</span>');
    }}
    if (c.confidence) meta.push('confidence: ' + c.confidence);
    const sourcesHtml = (c.sources || []).map(s => {{
      const cls = 'agree-' + (s.agree === true ? 'true' : s.agree === false ? 'false' : 'unrelated');
      return `<li class="${{cls}}"><a href="${{s.url}}">${{s.title || s.url}}</a> — ${{s.agree}}${{s.one_line ? ' — ' + s.one_line : ''}}</li>`;
    }}).join('');
    div.innerHTML = `
      <h3>${{c.id}}. ${{escape(c.text)}}</h3>
      <div class="meta">${{meta.join(' · ')}}</div>
      ${{c.why_check ? '<div class="meta"><em>why check:</em> ' + escape(c.why_check) + '</div>' : ''}}
      ${{c.caveat ? '<div class="caveat">' + escape(c.caveat) + '</div>' : ''}}
      ${{sourcesHtml ? '<ul>' + sourcesHtml + '</ul>' : '<div class="meta"><em>no sources</em></div>'}}
      <div class="actions" style="margin-top:8px">
        <button onclick="toggleClaim(${{i}})" style="background:transparent;color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:4px 8px;cursor:pointer">
          ${{c.accepted === false ? 'mark ok' : 'mark rejected'}}
        </button>
      </div>`;
    el.appendChild(div);
  }});
}}

function renderTranscript() {{
  const el = document.getElementById('transcript');
  el.innerHTML = (transcript.segments || []).map(s => {{
    const mm = Math.floor(s.start/60), ss = Math.floor(s.start%60);
    return `<div><span class="ts">${{String(mm).padStart(2,'0')}}:${{String(ss).padStart(2,'0')}}</span> ${{escape(s.text || '')}}</div>`;
  }}).join('');
}}

function toggleFrame(i) {{ candidates[i].accepted = !candidates[i].accepted; renderFrames(); }}
function toggleClaim(i) {{
  const cur = claims[i].accepted;
  claims[i].accepted = (cur === false) ? true : false;
  renderClaims();
}}
function escape(s) {{ return String(s).replace(/[&<>"]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c])); }}

async function addMark() {{
  const inp = document.getElementById('markTs');
  const st = document.getElementById('markStatus');
  const ts = parseFloat(inp.value);
  if (isNaN(ts) || ts < 0) {{ st.textContent = 'enter a valid second'; return; }}
  st.textContent = 'queuing mark — extension will capture on the YouTube tab…';
  // Queue the mark on the server; the extension polls /api/mark/pending on its
  // YouTube tab, captures the frame, and POSTs it back via /api/mark/complete.
  try {{
    await fetch('/api/mark', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{timestamp_s: ts}}) }});
  }} catch (e) {{ st.textContent = 'queue failed: ' + e; return; }}
  const before = candidates.length;
  let tries = 0;
  const poll = setInterval(async () => {{
    tries++;
    try {{
      const r = await fetch('/api/candidates');
      const data = await r.json().catch(() => ({{}}));
      const fresh = (data && data.candidates) || [];
      if (fresh.length > before) {{
        clearInterval(poll);
        candidates.length = 0;
        fresh.forEach(c => candidates.push(c));
        renderFrames();
        st.textContent = 'added ✓ (save to persist)';
        inp.value = '';
      }} else if (tries > 90) {{
        clearInterval(poll);
        st.textContent = 'no frame yet — open the YouTube tab with the extension, then it captures within ~2s.';
      }}
    }} catch (e) {{ /* keep polling */ }}
  }}, 1000);
}}

async function saveAll() {{
  const status = document.getElementById('status');
  status.textContent = 'saving…';
  try {{
    const r1 = await fetch('/api/save', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{kind:'candidates', data: candidates}}) }});
    const r2 = await fetch('/api/save', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{kind:'claims', data: claims}}) }});
    if (r1.ok && r2.ok) status.textContent = 'saved ✓';
    else status.textContent = 'save failed: ' + r1.status + '/' + r2.status;
  }} catch (e) {{ status.textContent = 'save error: ' + e; }}
}}

renderFrames();
renderClaims();
renderTranscript();
</script>
</body>
</html>
"""


class _ReviewHandler(http.server.BaseHTTPRequestHandler):
    server_version = "video2project-review/0.1"
    candidates_path: Path = Path()  # set by factory
    claims_path: Path = Path()  # set by factory
    title: str = ""  # set by factory
    video_dir_label: str = ""  # set by factory

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Silence default per-request logging; run.log captures everything
        pass

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._send_html()
        elif url.path == "/api/mark/pending":
            self._json_ok({"pending": _pop_pending_marks(peek=True)})
        elif url.path == "/api/candidates":
            # Review page polls this to learn when a mark completed.
            try:
                data = json.loads(self.candidates_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = []
            self._json_ok({"candidates": data})
        elif url.path.startswith("/frames/"):
            # Serve frame PNGs
            rel = url.path[len("/frames/") :]
            target = (self.candidates_path.parent / "frames" / rel).resolve()
            if not target.exists() or not str(target).startswith(
                str(self.candidates_path.parent.resolve())
            ):
                self.send_error(404)
                return
            self._send_file(target)
        else:
            self.send_error(404)

    def _json_ok(self, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/api/mark":
            self._handle_mark_request()
            return
        if url.path == "/api/mark/complete":
            self._handle_mark_complete()
            return
        if url.path != "/api/save":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or "0")
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_error(400, f"bad json: {exc}")
            return
        kind = body.get("kind")
        data = body.get("data")
        if kind == "candidates":
            target = self.candidates_path
        elif kind == "claims":
            target = self.claims_path
        else:
            self.send_error(400, f"unknown kind: {kind!r}")
            return
        target.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw.strip() else {}

    def _handle_mark_request(self) -> None:
        """Queue a manual-mark request from the review page.

        The review page POSTs {timestamp_s}; we hold it in a pending queue that
        the extension polls (it owns the <video> and does the actual capture).
        """
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_error(400, f"bad json: {exc}")
            return
        ts = body.get("timestamp_s")
        if not isinstance(ts, (int, float)):
            self.send_error(400, "timestamp_s required")
            return
        mark = _push_pending_mark(float(ts))
        self._json_ok({"ok": True, "queued": mark})

    def _handle_mark_complete(self) -> None:
        """Save the extension's captured frame for a pending mark.

        The extension POSTs {id, timestamp_s, image_b64} after seeking+capturing
        on the YouTube tab. We persist the PNG and append the candidate.
        """
        import base64

        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_error(400, f"bad json: {exc}")
            return
        mark_id = body.get("id")
        ts = float(body.get("timestamp_s") or 0)
        image_b64 = body.get("image_b64") or ""
        if not image_b64:
            self.send_error(400, "image_b64 required")
            return
        _pop_pending_marks(mark_id=mark_id)

        if "," in image_b64 and image_b64.split(",", 1)[0].startswith("data:"):
            image_b64 = image_b64.split(",", 1)[1]
        try:
            png = base64.b64decode(image_b64)
        except (ValueError, TypeError) as exc:
            self.send_error(400, f"bad base64: {exc}")
            return

        video_dir = self.candidates_path.parent
        frames_dir = video_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        candidates = json.loads(self.candidates_path.read_text(encoding="utf-8"))
        next_index = max([c.get("index", 0) for c in candidates] or [0]) + 1
        fname = f"frame_{next_index:04d}.png"
        (frames_dir / fname).write_bytes(png)

        candidate = {
            "index": next_index,
            "timestamp_s": ts,
            "frame_path": f"frames/{fname}",
            "extracted": True,
            "accepted": True,
            "manual": True,
        }
        candidates.append(candidate)
        self.candidates_path.write_text(
            json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._json_ok({"ok": True, "candidate": candidate})

    def _send_html(self) -> None:
        candidates = json.loads(self.candidates_path.read_text(encoding="utf-8"))
        claims = json.loads(self.claims_path.read_text(encoding="utf-8"))
        transcript = json.loads(
            (self.candidates_path.parent / "transcript.json").read_text(
                encoding="utf-8"
            )
        )
        html = _REVIEW_HTML.format(
            video_id=self.candidates_path.parent.name,
            title=self.title,
            video_dir=self.video_dir_label,
            n_candidates=len(candidates),
            n_claims=len(claims),
            candidates_json=json.dumps(candidates),
            claims_json=json.dumps(claims),
            transcript_json=json.dumps(transcript),
        )
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        body = path.read_bytes()
        ctype = (
            "image/png" if path.suffix.lower() == ".png" else "application/octet-stream"
        )
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        try:
            s.connect((host, port))
            return True
        except (ConnectionRefusedError, OSError):
            return False


def serve(
    video_dir: Path,
    *,
    open_browser: bool = True,
    host: str = REVIEW_HOST,
    port: int | None = None,
    block: bool = True,
) -> None:
    """Start the review server. If `block` is False, runs in a daemon thread."""
    candidates_path = video_dir / "candidates.json"
    claims_path = video_dir / "claims.json"
    if not candidates_path.exists():
        raise FileNotFoundError(f"Missing {candidates_path}. Run `ingest` first.")
    if not claims_path.exists():
        raise FileNotFoundError(f"Missing {claims_path}. Run `extract` first.")

    transcript_path = video_dir / "transcript.json"
    title = ""
    if transcript_path.exists():
        try:
            t = json.loads(transcript_path.read_text(encoding="utf-8"))
            title = t.get("title") or ""
        except json.JSONDecodeError:
            pass

    # Pick a port: requested, then REVIEW_PORT, then 8765+ until one is free
    candidates_ports = [port] if port else [REVIEW_PORT, 8766, 8767, 8768]
    chosen: int | None = None
    for p in candidates_ports:
        if p is None:
            continue
        if not _port_in_use(host, p):
            chosen = p
            break
    if chosen is None:
        raise RuntimeError(f"No free port in {candidates_ports}")

    def handler_factory(*args: Any, **kwargs: Any) -> _ReviewHandler:
        # Build a handler subclass with bound paths
        class BoundHandler(_ReviewHandler):
            pass

        BoundHandler.candidates_path = candidates_path
        BoundHandler.claims_path = claims_path
        BoundHandler.title = title
        BoundHandler.video_dir_label = str(video_dir)
        return BoundHandler(*args, **kwargs)

    server = socketserver.TCPServer((host, chosen), handler_factory)
    server.allow_reuse_address = True

    url = f"http://{host}:{chosen}/"
    print(f"video2project review: {url}")
    print(f"  video dir: {video_dir}")
    print("  Ctrl-C to stop.")

    if open_browser:
        # Defer to let the server bind first
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    if not block:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m video2project.review <video_dir>", file=sys.stderr)
        sys.exit(2)
    serve(Path(sys.argv[1]).resolve())


__all__ = ["serve"]

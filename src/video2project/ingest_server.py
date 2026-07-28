"""Browser-capture ingest server: receive extension POSTs on localhost.

Separate from review.py: the review server serves one finished video's HTML;
this server is the capture front-door for the browser extension. It stays up
and routes each POST to capture.py. Standalone stdlib HTTP, no framework.

Endpoints (all JSON POST unless noted):
- ``/api/start``   body: metadata            → registers the job
- ``/api/audio``   body: {metadata, audio}   → transcribe, returns timestamps
- ``/api/frames``  body: {metadata, frames}  → OCR + finalize
- ``/api/health``  GET                        → liveness for the extension

CORS is permissive (localhost-only server, called from a chrome-extension://
origin), so the extension's fetch isn't blocked.
"""

from __future__ import annotations

import http.server
import json
import socket
import socketserver
import threading
from typing import Any
from urllib.parse import urlparse

from . import capture
from .paths import REVIEW_HOST

DEFAULT_PORT = 8765


def _cors(handler: http.server.BaseHTTPRequestHandler) -> None:
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")


class _IngestHandler(http.server.BaseHTTPRequestHandler):
    server_version = "video2project-ingest/0.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # silence per-request logging

    def _json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        _cors(self)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw.strip() else {}

    def do_OPTIONS(self) -> None:  # noqa: N802 — CORS preflight
        self.send_response(204)
        _cors(self)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/api/health":
            self._json({"ok": True, "service": "video2project-ingest"})
        elif url.path == "/api/state":
            # Snapshot of a video's pipeline state, for the extension to decide
            # whether to resume from a checkpoint or start fresh.
            from urllib.parse import parse_qs

            qs = parse_qs(url.query)
            video_id = (qs.get("video_id") or [""])[0]
            platform = (qs.get("platform") or ["youtube"])[0]
            if not video_id:
                self._json({"ok": False, "error": "video_id required"}, status=400)
                return
            try:
                state = capture.read_state(platform, video_id)
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": str(exc)}, status=500)
                return
            self._json({"ok": True, "video_id": video_id, **state})
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        try:
            body = self._read_body()
        except (ValueError, json.JSONDecodeError) as exc:
            self._json({"ok": False, "error": f"bad json: {exc}"}, status=400)
            return

        try:
            if url.path == "/api/start":
                result = capture.start_job(body)
            elif url.path == "/api/audio":
                result = capture.ingest_audio(
                    body.get("metadata") or {}, body.get("audio") or ""
                )
            elif url.path == "/api/frames":
                result = capture.ingest_frames(
                    body.get("metadata") or {}, body.get("frames") or []
                )
            elif url.path == "/api/mark":
                # Save ONE marked frame immediately — the extension calls this
                # each time the user clicks the mark button. Persists to disk
                # right away so a later crash doesn't lose the mark.
                result = capture.ingest_mark(
                    body.get("metadata") or {},
                    float(body.get("timestamp_s") or 0),
                    body.get("image_b64") or "",
                )
            else:
                self.send_error(404)
                return
        except Exception as exc:  # noqa: BLE001 — surface pipeline errors as JSON
            self._json({"ok": False, "error": str(exc)}, status=500)
            return

        self._json({"ok": True, **result})


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        try:
            s.connect((host, port))
            return True
        except (ConnectionRefusedError, OSError):
            return False


def serve(
    *,
    host: str = REVIEW_HOST,
    port: int = DEFAULT_PORT,
    block: bool = True,
) -> socketserver.ThreadingTCPServer | None:
    """Start the ingest server. If ``block`` is False, runs in a daemon thread."""

    # ThreadingTCPServer sets allow_reuse_address as a class attribute; override
    # BEFORE constructing so SO_REUSEADDR takes effect at bind time. Without this,
    # a crashed/restarted server leaves the port in TIME_WAIT and rebind fails.
    class _ReusableServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    try:
        server = _ReusableServer((host, port), _IngestHandler)
    except OSError as exc:
        if exc.errno == 98:  # EADDRINUSE — a live server is holding the port
            raise RuntimeError(
                f"port {port} in use by a live process; run "
                f"`pkill -f 'video2project capture'` then retry"
            ) from exc
        raise

    url = f"http://{host}:{port}/"
    print(f"video2project ingest server: {url}")
    print("  waiting for the browser extension (analyze on a YouTube page).")
    print("  Ctrl-C to stop.")

    if not block:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


__all__ = ["serve", "DEFAULT_PORT"]

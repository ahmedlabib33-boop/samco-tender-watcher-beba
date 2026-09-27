"""Local dashboard server for the SAMCO Construction Opportunity Watcher.

Serves only engine-generated files over an explicit route whitelist on
127.0.0.1:4207 (no directory listing, no user-controlled paths).
Every response is no-store; the API reflects the real on-disk snapshot.
"""
import argparse
import json
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
DASHBOARD = ROOT / "dashboard.html"
LATEST = ROOT / "vercel" / "data" / "latest.json"
TENDERS = ROOT / "tenders.json"
STATE = ROOT / "state" / "state.json"
LOGFILE = ROOT / "logs" / "server.log"

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": ("default-src 'none'; script-src 'unsafe-inline'; "
                                "style-src 'unsafe-inline'; img-src 'self' data:; "
                                "connect-src 'self'; base-uri 'none'; form-action 'none'; "
                                "frame-ancestors 'none'"),
}

NO_SNAPSHOT_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>SAMCO Watcher - first run in progress</title>
<style>body{background:#08101f;color:#e8eefb;font-family:system-ui,Segoe UI,Arial;padding:48px;max-width:720px;margin:auto}
code{background:#0f1c33;padding:2px 6px;border-radius:6px}h1{font-size:22px}</style></head><body>
<h1>No snapshot on disk yet</h1>
<p>The watcher has not written a dashboard snapshot into this folder yet.</p>
<p>Start it with <code>run.bat</code> (or <code>python Watcher.py --once</code>) and reload this page.
Nothing is shown until a real check cycle has completed - this page never invents data.</p>
</body></html>"""


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _health_payload():
    """Real status derived from on-disk state; everything is null if unknown."""
    state = _read_json(STATE) or {}
    tenders = _read_json(TENDERS) or {}
    last_run = state.get("last_run") or {}
    sources = state.get("sources") or {}
    watched_ok = sum(1 for s in sources.values() if s.get("health_state") in ("watched_ok", "watched_no_rows"))
    generated = tenders.get("generated_at")
    age_seconds = None
    if generated:
        try:
            gen = datetime.fromisoformat(generated)
            age_seconds = max(0, int((datetime.now(timezone.utc) - gen).total_seconds()))
        except ValueError:
            pass
    interval_min = (last_run.get("interval_minutes") or 60)
    stale = True if age_seconds is None else age_seconds > interval_min * 60 * 2.2
    return {
        "status": "stale_snapshot" if stale else ("ok" if last_run else "no_runs_yet"),
        "server_time": datetime.now(timezone.utc).isoformat(),
        "snapshot_generated_at": generated,
        "snapshot_age_seconds": age_seconds,
        "stale": stale,
        "last_run": last_run or None,
        "sources_total": len(sources),
        "sources_watched_ok": watched_ok,
        "sources_failed": max(0, len(sources) - watched_ok) if sources else None,
        "tenders_in_snapshot": len(tenders.get("tenders") or []),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "SAMCOWatcher"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30

    def log_message(self, fmt, *args):
        line = "%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), fmt % args)
        try:
            with open(LOGFILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
        print(line)

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _text(self, code, text, ctype="text/plain; charset=utf-8"):
        self._send(code, text.encode("utf-8"), ctype)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/index.html", "/dashboard.html"):
            if DASHBOARD.exists():
                self._send(200, DASHBOARD.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(200, NO_SNAPSHOT_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/data":
            if LATEST.exists():
                self._send(200, LATEST.read_bytes(), "application/json; charset=utf-8")
            else:
                self._json(404, {"error": "no snapshot yet - run Watcher.py first"})
        elif path == "/api/health":
            self._json(200, _health_payload())
        elif path == "/tenders.json":
            if TENDERS.exists():
                self._send(200, TENDERS.read_bytes(), "application/json; charset=utf-8")
            else:
                self._json(404, {"error": "no tenders.json yet"})
        elif path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        self._json(405, {"error": "method not allowed"})

    do_PUT = do_DELETE = do_PATCH = do_POST


def main():
    ap = argparse.ArgumentParser(description="SAMCO Watcher local dashboard server (real data only)")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=4207, help="port (default 4207)")
    ap.add_argument("--open", action="store_true", help="open the dashboard in the default browser")
    args = ap.parse_args()

    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        print("Cannot bind %s:%d - %s" % (args.host, args.port, e))
        print("Is the port already in use? Close the other server or pass --port <n>.")
        sys.exit(1)
    httpd.daemon_threads = True
    url = "http://%s:%d/" % ("127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host, args.port)
    print("SAMCO Watcher dashboard: %s" % url)
    print("Serving real engine output only: dashboard.html + /api/data + /api/health")
    print("Ctrl+C to stop.")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: bound to %s - the dashboard is reachable from other machines." % args.host)
    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nserver stopped")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()

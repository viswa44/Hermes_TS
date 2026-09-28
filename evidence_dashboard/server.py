"""Loopback-only evidence explorer with explicit, bounded research controls."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
from urllib.parse import parse_qs, urlsplit

from .jobs import JobConflict, JobController, RUN_ID
from .store import EvidenceStore

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"


class EvidenceServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store=None, controller=None):
        if address[0] != "127.0.0.1":
            raise ValueError("The evidence dashboard binds to 127.0.0.1 only.")
        self.store = store or EvidenceStore(ROOT)
        self.controller = controller or JobController(ROOT)
        self.csrf_token = secrets.token_urlsafe(32)
        super().__init__(address, Handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(30)
        return connection, address


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _allowed(self, *, write=False):
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "")
        if host not in hosts or self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != "http://" + host:
            return False
        if write:
            token = self.headers.get("X-CSRF-Token", "")
            return origin == "http://" + host and token.isascii() and secrets.compare_digest(token, self.server.csrf_token)
        return True

    def send_value(self, value, status=200, *, mime="application/json; charset=utf-8", head=False):
        content = value if isinstance(value, bytes) else json.dumps(value, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        if not head:
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _query(self, raw, allowed):
        values = parse_qs(raw, keep_blank_values=True, max_num_fields=20)
        if set(values) - set(allowed) or any(len(v) != 1 for v in values.values()):
            raise ValueError("Invalid or repeated query fields.")
        return {k: v[0] for k, v in values.items()}

    def do_GET(self, head=False):
        if not self._allowed():
            return self.send_value({"error": "Local same-origin access is required."}, 403, head=head)
        target = urlsplit(self.path)
        try:
            assets = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js",
                      "/style.css": "style.css", "/static/app.js": "app.js", "/static/style.css": "style.css"}
            if target.path in assets:
                name = assets[target.path]
                mime = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}[name]
                return self.send_value((STATIC / name).read_bytes(), mime=mime + "; charset=utf-8", head=head)
            if target.path == "/health":
                result = {"status": "ok", "service": "evidence-dashboard"}
            elif target.path == "/api/status":
                result = {**self.server.store.status(), "active_job": self.server.controller.active(),
                          "csrf_token": self.server.csrf_token,
                          "defaults": {"min_support": 5, "max_conditions": 6, "discovery_end": None, "publish_to_s3": False}}
            else:
                parts = target.path.strip("/").split("/")
                if len(parts) not in (3, 4) or parts[:2] != ["api", "runs"] or not RUN_ID.fullmatch(parts[2]):
                    return self.send_value({"error": "Not found."}, 404, head=head)
                run_id = parts[2]
                if len(parts) == 3:
                    self._query(target.query, [])
                    result = self.server.store.detail(run_id)
                elif parts[3] == "observations":
                    query = self._query(target.query, ("kind", "date", "side", "label", "condition_id", "q", "offset", "limit"))
                    for name in ("offset", "limit"):
                        if name in query:
                            query[name] = int(query[name])
                    result = self.server.store.table(run_id, **query)
                elif parts[3] == "series":
                    result = self.server.store.series(run_id, **self._query(target.query, ("date",)))
                else:
                    return self.send_value({"error": "Not found."}, 404, head=head)
            self.send_value(result, head=head)
        except FileNotFoundError:
            self.send_value({"error": "The requested evidence is not available yet."}, 404, head=head)
        except (ValueError, TypeError):
            self.send_value({"error": "Invalid filters or unavailable evidence. Refresh and check the run status."}, 400, head=head)
        except Exception:
            self.send_value({"error": "Evidence could not be read. Check the local dashboard service."}, 500, head=head)

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_POST(self):
        if not self._allowed(write=True):
            return self.send_value({"error": "Refresh the dashboard before starting research."}, 403)
        try:
            if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
                return self.send_value({"error": "Use a JSON research request."}, 415)
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not 1 <= int(lengths[0]) <= 4096:
                return self.send_value({"error": "Invalid request size."}, 413)
            data = json.loads(self.rfile.read(int(lengths[0])))
            target = urlsplit(self.path)
            if target.query:
                raise ValueError("Run controls do not accept query parameters.")
            if target.path == "/api/runs":
                result = self.server.controller.start(data)
            else:
                parts = target.path.strip("/").split("/")
                if len(parts) != 4 or parts[:2] != ["api", "runs"] or parts[3] != "resume":
                    return self.send_value({"error": "Not found."}, 404)
                if data != {}:
                    raise ValueError("Resume uses the saved run settings.")
                result = self.server.controller.resume(parts[2])
            self.send_value(result, 202)
        except JobConflict as exc:
            self.send_value({"error": str(exc)}, 409)
        except (ValueError, TypeError, UnicodeError) as exc:
            # Controller validation messages contain only fixed public settings.
            message = str(exc) if type(exc) is ValueError else "Invalid research request."
            self.send_value({"error": message[:250]}, 400)
        except Exception:
            self.send_value({"error": "Research could not start. Check the local dashboard service."}, 500)

    def unsupported(self):
        self.send_value({"error": "Method not allowed."}, 405)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = unsupported


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    server = EvidenceServer(("127.0.0.1", args.port))
    print(f"Hermes evidence dashboard: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

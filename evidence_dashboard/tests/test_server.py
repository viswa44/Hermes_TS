"""Exercise the real loopback HTTP boundary without running research."""
from __future__ import annotations

import http.client
import json
from types import SimpleNamespace
import threading

import pytest

from evidence_dashboard.jobs import JobConflict
from evidence_dashboard.server import EvidenceServer


class FixtureStore:
    def __init__(self):
        self.calls = []
        self.failure = None

    def status(self):
        if self.failure:
            raise self.failure
        return {"source": {"available": True}, "runs": [], "selected_run_id": None}

    def detail(self, run_id):
        self.calls.append(("detail", run_id))
        return {"run_id": run_id, "status": "REGISTERED"}

    def table(self, run_id, **filters):
        self.calls.append(("table", run_id, filters))
        return {"rows": [], "total": 0}

    def series(self, run_id, **filters):
        self.calls.append(("series", run_id, filters))
        return {"points": []}


class FixtureController:
    def __init__(self):
        self.calls = []
        self.failure = None

    def active(self):
        return None

    def start(self, options):
        if self.failure:
            raise self.failure
        self.calls.append(("start", options))
        return {"run_id": "inert-run", "job": {"status": "RUNNING"}}

    def resume(self, run_id):
        if self.failure:
            raise self.failure
        self.calls.append(("resume", run_id))
        return {"run_id": run_id, "job": {"status": "RUNNING"}}


@pytest.fixture
def dashboard():
    store, controller = FixtureStore(), FixtureController()
    server = EvidenceServer(("127.0.0.1", 0), store=store, controller=controller)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    host = f"127.0.0.1:{server.server_address[1]}"

    def request(path="/api/status", *, method="GET", body=None, headers=None):
        body_bytes = body if isinstance(body, bytes) else None if body is None else json.dumps(body).encode()
        supplied = [] if headers is None else list(headers.items()) if isinstance(headers, dict) else list(headers)
        names = {key.lower() for key, _ in supplied}
        if "host" not in names:
            supplied.append(("Host", host))
        if body_bytes is not None:
            if "content-length" not in names:
                supplied.append(("Content-Length", str(len(body_bytes))))
            if "content-type" not in names:
                supplied.append(("Content-Type", "application/json"))
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
        try:
            connection.putrequest(method, path, skip_host=True)
            for name, value in supplied:
                connection.putheader(name, value)
            connection.endheaders(body_bytes)
            response = connection.getresponse()
            payload = response.read()
            return SimpleNamespace(status=response.status, headers=dict(response.getheaders()),
                                   data=json.loads(payload) if payload and "application/json" in response.getheader("Content-Type", "") else payload)
        finally:
            connection.close()

    instance = SimpleNamespace(server=server, store=store, controller=controller, request=request, host=host,
                               authorized={"Origin": f"http://{host}", "X-CSRF-Token": server.csrf_token})
    try:
        yield instance
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("address", ["0.0.0.0", "localhost", "::1", "192.168.1.5"])
def test_server_refuses_non_loopback_binding(address):
    with pytest.raises(ValueError, match="127.0.0.1 only"):
        EvidenceServer((address, 0))


def test_status_has_per_server_token_and_local_security_headers(dashboard):
    response = dashboard.request()
    assert response.status == 200
    assert response.data["csrf_token"] == dashboard.server.csrf_token
    assert response.data["defaults"]["publish_to_s3"] is False
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "Access-Control-Allow-Origin" not in response.headers


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.example"}, {"Host": "127.0.0.1"},
    {"Origin": "https://attacker.example"}, {"Sec-Fetch-Site": "cross-site"},
    {"Origin": "null"},
])
def test_read_endpoints_reject_foreign_browser_context(dashboard, headers):
    result = dashboard.request(headers=headers)
    assert result.status == 403
    assert "csrf_token" not in result.data


@pytest.mark.parametrize("kind", ["missing", "token_only", "origin_only", "wrong_token", "wrong_origin", "cross_site", "foreign_host"])
def test_mutations_require_matching_host_origin_and_csrf_token(dashboard, kind):
    headers = dict(dashboard.authorized)
    if kind == "missing":
        headers = {}
    elif kind == "token_only":
        headers.pop("Origin")
    elif kind == "origin_only":
        headers.pop("X-CSRF-Token")
    elif kind == "wrong_token":
        headers["X-CSRF-Token"] = "invalid"
    elif kind == "wrong_origin":
        headers["Origin"] = "http://attacker.example"
    elif kind == "cross_site":
        headers["Sec-Fetch-Site"] = "cross-site"
    elif kind == "foreign_host":
        headers["Host"] = "attacker.example"
        headers["Origin"] = "http://attacker.example"
    response = dashboard.request("/api/runs", method="POST", body={}, headers=headers)
    assert response.status == 403
    assert dashboard.controller.calls == []


def test_authorized_start_and_resume_reach_bounded_controller(dashboard):
    options = {"min_support": 9, "max_conditions": 4, "publish_to_s3": False}
    result = dashboard.request("/api/runs", method="POST", body=options, headers=dashboard.authorized)
    assert result.status == 202
    assert dashboard.controller.calls == [("start", options)]
    resumed = dashboard.request("/api/runs/saved-run/resume", method="POST", body={}, headers=dashboard.authorized)
    assert resumed.status == 202
    assert dashboard.controller.calls[-1] == ("resume", "saved-run")


def test_localhost_origin_is_accepted_only_with_matching_host(dashboard):
    host = f"localhost:{dashboard.server.server_address[1]}"
    headers = {**dashboard.authorized, "Host": host, "Origin": f"http://{host}"}
    assert dashboard.request("/api/runs", method="POST", body={}, headers=headers).status == 202


@pytest.mark.parametrize("path,body", [
    ("/api/runs?publish_to_s3=true", {}),
    ("/api/runs/saved-run/resume", {"publish_to_s3": True}),
    ("/api/runs/saved-run/resume", []),
    ("/api/runs", b"{not-json"),
])
def test_invalid_control_requests_never_launch(dashboard, path, body):
    result = dashboard.request(path, method="POST", body=body, headers=dashboard.authorized)
    assert result.status == 400
    assert dashboard.controller.calls == []


@pytest.mark.parametrize("extra,body,status", [
    ([('Content-Type', 'text/plain')], b'{}', 415),
    ([('Transfer-Encoding', 'chunked')], b'{}', 415),
    ([('Content-Length', '0')], b'', 413),
    ([('Content-Length', '5000')], b'{}', 413),
    ([('Content-Length', '2'), ('Content-Length', '2')], b'{}', 413),
])
def test_request_framing_and_size_are_bounded(dashboard, extra, body, status):
    headers = list(dashboard.authorized.items()) + extra
    result = dashboard.request("/api/runs", method="POST", body=body, headers=headers)
    assert result.status == status
    assert dashboard.controller.calls == []


def test_filter_query_is_decoded_and_numeric_page_values_converted(dashboard):
    result = dashboard.request("/api/runs/saved-run/observations?kind=observations&side=CE&offset=10&limit=25&q=NIFTY%202026")
    assert result.status == 200
    assert dashboard.store.calls == [("table", "saved-run", {
        "kind": "observations", "side": "CE", "offset": 10, "limit": 25, "q": "NIFTY 2026",
    })]


@pytest.mark.parametrize("path", [
    "/api/runs/saved-run/observations?limit=1&limit=5",
    "/api/runs/saved-run/observations?path=/private/file",
    "/api/runs/saved-run/observations?limit=NaN",
    "/api/runs/saved-run?unknown=1",
    "/api/runs/saved-run/series?symbol=NIFTY",
])
def test_invalid_or_ambiguous_filters_do_not_reach_store(dashboard, path):
    assert dashboard.request(path).status == 400
    assert dashboard.store.calls == []


@pytest.mark.parametrize("path", ["/../private", "/api/runs/../observations", "/api/runs/a%2Fb", "/api/runs/saved-run/files"])
def test_unknown_and_unsafe_paths_are_not_read(dashboard, path):
    assert dashboard.request(path).status == 404
    assert dashboard.store.calls == []


@pytest.mark.parametrize("failure,status,expected", [
    (JobConflict("A research run is already active."), 409, "already active"),
    (ValueError("Choose a cutoff leaving sessions for both discovery and evaluation."), 400, "cutoff"),
    (RuntimeError("private-password-should-not-appear"), 500, "could not start"),
])
def test_start_errors_have_actionable_bounded_responses(dashboard, failure, status, expected):
    dashboard.controller.failure = failure
    response = dashboard.request("/api/runs", method="POST", body={}, headers=dashboard.authorized)
    assert response.status == status
    assert expected in response.data["error"]
    assert "private-password" not in json.dumps(response.data)


def test_unexpected_store_failure_does_not_expose_details(dashboard):
    dashboard.store.failure = RuntimeError("private-filesystem-path-or-secret")
    response = dashboard.request()
    assert response.status == 500
    assert "private-filesystem" not in json.dumps(response.data)


def test_head_and_unsupported_method_do_not_activate_research(dashboard):
    response = dashboard.request("/", method="HEAD")
    assert response.status == 200
    assert response.data == b""
    assert int(response.headers["Content-Length"]) > 0
    assert dashboard.request("/api/runs", method="DELETE").status == 405
    assert dashboard.controller.calls == []

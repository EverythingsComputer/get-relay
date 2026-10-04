"""Tests for get-relay. Uses a local thread-based upstream stub server."""
import io
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import get_relay


class UpstreamHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parts = urllib.parse.urlsplit(self.path)
        if parts.path.startswith("/echo"):
            payload = {
                "method": self.command,
                "path": urllib.parse.unquote(parts.path),
                "query": {k: v for k, v in urllib.parse.parse_qsl(parts.query)},
            }
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parts.path.startswith("/status/"):
            code = int(parts.path.rsplit("/", 1)[1])
            body = b"upstream says %d" % code
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parts.path == "/big":
            body = b"A" * (40 * 1024)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parts.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/echo")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif parts.path == "/slow":
            time.sleep(1.5)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, fmt, *args):
        pass


@pytest.fixture()
def upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture()
def config():
    return get_relay.Config(frozenset({"127.0.0.1", "localhost"}), 10 * 1024, 5.0)


def q(**params):
    """Build parse_qs-style params from keyword args."""
    return {k: [str(v)] for k, v in params.items()}


def call_relay(target, config, **extra):
    params = {"target": [target]}
    params.update(q(**extra))
    return get_relay.relay(params, config)


class TestRelayCore:
    def test_forwards_params_and_returns_status_body(self, upstream, config):
        result = call_relay(f"{upstream}/echo", config, msg="hello", n="1 2")
        assert result.status == 200
        echo = json.loads(result.body)
        assert echo["query"] == {"msg": "hello", "n": "1 2"}

    def test_target_not_forwarded(self, upstream, config):
        result = call_relay(f"{upstream}/echo", config, a="1")
        assert "target" not in json.loads(result.body)["query"]

    def test_missing_target_is_400(self, config):
        result = get_relay.relay({}, config)
        assert result.status == 400
        assert b"target" in result.body

    def test_blank_target_is_400(self, config):
        result = call_relay("   ", config)
        assert result.status == 400

    def test_non_http_scheme_blocked(self, config):
        for bad in ("ftp://example.com/", "file:///etc/passwd", "gopher://x/"):
            result = call_relay(bad, config)
            assert result.status == 403, bad

    def test_host_not_in_allowlist(self, upstream, config):
        config.allowed_hosts = frozenset({"127.0.0.1"})
        result = call_relay(f"http://localhost:{upstream.split(':')[2]}/echo", config)
        assert result.status == 403

    def test_wildcard_allowlist(self, config):
        config.allowed_hosts = "*"
        host, url = get_relay.build_target_url("https://example.com/x?a=1", q(b="2"), config)
        assert host == "example.com"
        assert url == "https://example.com/x?a=1&b=2"

    def test_template_substitution_consumes_param(self, upstream, config):
        result = call_relay(f"{upstream}/echo/{{msg}}/end", config, msg="hi there")
        echo = json.loads(result.body)
        assert echo["path"] == "/echo/hi there/end"
        assert "msg" not in echo["query"]

    def test_unknown_placeholder_left_intact(self, upstream, config):
        result = call_relay(f"{upstream}/echo/{{nope}}", config)
        assert json.loads(result.body)["path"] == "/echo/{nope}"

    def test_substituted_value_cannot_escape_url(self, config):
        _, url = get_relay.build_target_url(
            "http://127.0.0.1:9/x/{v}", q(v="?next=http://evil/"), config
        )
        parsed = urllib.parse.urlsplit(url)
        assert parsed.hostname == "127.0.0.1"
        assert parsed.query == ""
        assert parsed.path == "/x/%3Fnext%3Dhttp%3A%2F%2Fevil%2F"

    def test_merges_with_existing_query(self, upstream, config):
        result = call_relay(f"{upstream}/echo?keep=1", config, extra="x")
        assert json.loads(result.body)["query"] == {"keep": "1", "extra": "x"}

    def test_upstream_status_passthrough(self, upstream, config):
        result = call_relay(f"{upstream}/status/418", config)
        assert result.status == 418
        assert b"418" in result.body

    def test_redirects_not_followed(self, upstream, config):
        result = call_relay(f"{upstream}/redirect", config)
        assert result.status == 302

    def test_response_truncated(self, upstream):
        cfg = get_relay.Config(frozenset({"127.0.0.1"}), 1024, 5.0)
        result = call_relay(f"{upstream}/big", cfg)
        assert result.status == 200
        assert len(result.body) == 1024
        assert ("X-Relay-Truncated", "true") in result.headers

    def test_upstream_unreachable_is_502(self, config):
        result = call_relay("http://127.0.0.1:1/none", config)
        assert result.status == 502

    def test_upstream_timeout_is_504(self, upstream):
        cfg = get_relay.Config(frozenset({"127.0.0.1"}), 1024, 0.2)
        result = call_relay(f"{upstream}/slow", cfg)
        assert result.status == 504


class TestConfig:
    def test_defaults(self, monkeypatch):
        for var in ("ALLOWED_HOSTS", "MAX_BODY_BYTES", "TIMEOUT"):
            monkeypatch.delenv(var, raising=False)
        cfg = get_relay.Config.from_env()
        assert cfg.allowed_hosts == frozenset({"localhost", "127.0.0.1"})
        assert cfg.max_body_bytes == 10 * 1024
        assert cfg.timeout == 10.0

    def test_parsing(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_HOSTS", "API.Example.COM, other.example")
        monkeypatch.setenv("MAX_BODY_BYTES", "2048")
        monkeypatch.setenv("TIMEOUT", "2.5")
        cfg = get_relay.Config.from_env()
        assert cfg.allowed_hosts == frozenset({"api.example.com", "other.example"})
        assert cfg.max_body_bytes == 2048
        assert cfg.timeout == 2.5

    def test_invalid_values_raise(self, monkeypatch):
        monkeypatch.setenv("MAX_BODY_BYTES", "nope")
        with pytest.raises(ValueError):
            get_relay.Config.from_env()
        monkeypatch.setenv("MAX_BODY_BYTES", "-1")
        with pytest.raises(ValueError):
            get_relay.Config.from_env()


class TestWSGI:
    def make_environ(self, method="GET", query="", body=b"", ctype=""):
        return {
            "REQUEST_METHOD": method,
            "QUERY_STRING": query,
            "CONTENT_TYPE": ctype,
            "CONTENT_LENGTH": str(len(body)) if body else "",
            "wsgi.input": io.BytesIO(body),
        }

    def start_response(self, status, headers):
        self.status = status
        self.headers = dict(headers)

    def run(self, app, environ):
        chunks = app(environ, self.start_response)
        return self.status, self.headers, b"".join(chunks)

    def test_get_relay(self, upstream):
        get_relay.wsgi_app.config = get_relay.Config(frozenset({"127.0.0.1"}), 10240, 5.0)
        status, _, body = self.run(
            get_relay.wsgi_app,
            self.make_environ(query=urllib.parse.urlencode({"target": f"{upstream}/echo", "msg": "w"})),
        )
        assert status.startswith("200")
        assert json.loads(body)["query"]["msg"] == "w"

    def test_post_form_merges_and_overrides_query(self, upstream):
        get_relay.wsgi_app.config = get_relay.Config(frozenset({"127.0.0.1"}), 10240, 5.0)
        status, _, body = self.run(
            get_relay.wsgi_app,
            self.make_environ(
                method="POST",
                query=urllib.parse.urlencode({"target": f"{upstream}/echo", "a": "query"}),
                body=b"a=form&b=2",
                ctype="application/x-www-form-urlencoded",
            ),
        )
        assert status.startswith("200")
        assert json.loads(body)["query"] == {"a": "form", "b": "2"}

    def test_body_too_large_is_413(self):
        get_relay.wsgi_app.config = get_relay.Config(frozenset({"127.0.0.1"}), 16, 5.0)
        status, _, body = self.run(
            get_relay.wsgi_app,
            self.make_environ(method="POST", query="target=http://x/", body=b"x" * 32,
                              ctype="application/x-www-form-urlencoded"),
        )
        assert status.startswith("413")

    def test_method_not_allowed(self):
        get_relay.wsgi_app.config = get_relay.Config(frozenset({"127.0.0.1"}), 10240, 5.0)
        status, _, _ = self.run(get_relay.wsgi_app, self.make_environ(method="PUT"))
        assert status.startswith("405")

    def teardown_method(self):
        get_relay.wsgi_app.config = None


class TestServerEndToEnd:
    def test_live_server_roundtrip(self, upstream):
        cfg = get_relay.Config(frozenset({"127.0.0.1"}), 10240, 5.0)
        server = get_relay.make_server("127.0.0.1", 0, cfg)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = (f"http://127.0.0.1:{server.server_address[1]}/relay"
                   f"?target={urllib.parse.quote(upstream + '/echo', safe='')}&msg=e2e")
            with urllib.request.urlopen(url) as resp:
                assert resp.status == 200
                assert json.loads(resp.read())["query"]["msg"] == "e2e"
        finally:
            server.shutdown()
            server.server_close()

    def test_live_server_blocked_host(self):
        cfg = get_relay.Config(frozenset(), 10240, 5.0)
        server = get_relay.make_server("127.0.0.1", 0, cfg)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/relay?target=http://example.com/"
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen(url)
            assert excinfo.value.code == 403
        finally:
            server.shutdown()
            server.server_close()

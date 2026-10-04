#!/usr/bin/env python3
"""get-relay -- tiny stateless HTTP relay for GET-only clients.

Lets systems that can only issue GET requests (simple AI agents, embedded
firmware, IoT buttons, mail-merge links) submit data to endpoints that
expect query parameters, by proxying the request on their behalf.

    GET /relay?target=https://example.com/submit&msg=hello

fetches https://example.com/submit?msg=hello and returns the upstream
status code and body (truncated). No storage, no state, no secrets in logs.

Env config:
  ALLOWED_HOSTS   comma-separated allowlist of target hosts, or "*"
                  (default: localhost,127.0.0.1)
  MAX_BODY_BYTES  max POST form body AND max upstream response bytes
                  returned (default: 10240 = 10 KB)
  TIMEOUT         upstream timeout in seconds (default: 10)

Run:  python get_relay.py [--host HOST] [--port PORT]
WSGI: from get_relay import wsgi_app  (gunicorn "get_relay:wsgi_app")
"""
from __future__ import annotations

import argparse
import http.client
import logging
import os
import re
import string
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__version__ = "1.0.0"
USER_AGENT = f"get-relay/{__version__}"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_ALLOWED_HOSTS = "localhost,127.0.0.1"
DEFAULT_MAX_BODY_BYTES = 10 * 1024
DEFAULT_TIMEOUT_SECONDS = 10.0

TEXT_PLAIN = "text/plain; charset=utf-8"

logger = logging.getLogger("get-relay")


class RelayError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _env_int(env, name, default):
    raw = env.get(name, "")
    if not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}")
    if value <= 0:
        raise ValueError(f"{name} must be > 0")
    return value


def _env_float(env, name, default):
    raw = env.get(name, "")
    if not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}")
    if value <= 0:
        raise ValueError(f"{name} must be > 0")
    return value


class Config:
    def __init__(self, allowed_hosts, max_body_bytes, timeout):
        self.allowed_hosts = allowed_hosts
        self.max_body_bytes = int(max_body_bytes)
        self.timeout = float(timeout)

    def host_allowed(self, host: str) -> bool:
        return self.allowed_hosts == "*" or host in self.allowed_hosts

    @classmethod
    def from_env(cls, env=None) -> "Config":
        env = os.environ if env is None else env
        raw_hosts = env.get("ALLOWED_HOSTS")
        if raw_hosts is None:
            allowed = frozenset(
                h.strip().lower() for h in DEFAULT_ALLOWED_HOSTS.split(",") if h.strip()
            )
        elif raw_hosts.strip() == "*":
            allowed = "*"
        else:
            allowed = frozenset(h.strip().lower() for h in raw_hosts.split(",") if h.strip())
        max_body = _env_int(env, "MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES)
        timeout = _env_float(env, "TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
        return cls(allowed, max_body, timeout)


_FORMATTER = string.Formatter()
_ROOT_FIELD_RE = re.compile(r"[A-Za-z0-9_]+")


def _field_names(template: str) -> list:
    names = []
    for _, name, _, _ in _FORMATTER.parse(template):
        if not name:
            continue
        match = _ROOT_FIELD_RE.match(name)
        root = match.group(0) if match else ""
        if root and root not in names:
            names.append(root)
    return names


def build_target_url(target: str, params: dict, config: Config):
    """Resolve {placeholders} in *target* from *params* and append the rest.

    Returns (host, url). Substituted values are percent-encoded so they can
    never alter the URL structure. Params consumed by a placeholder are not
    appended again to the query string.
    """
    try:
        probe = urllib.parse.urlsplit(target)
    except ValueError as exc:
        raise RelayError(400, f"invalid target URL: {exc}")
    scheme = probe.scheme.lower()
    if scheme not in ("http", "https"):
        raise RelayError(403, f"scheme not allowed: {scheme or '(none)'}")
    host = (probe.hostname or "").lower()
    if not host:
        raise RelayError(400, "target has no hostname")
    if not config.host_allowed(host):
        raise RelayError(403, f"host not allowed: {host}")

    names = _field_names(target)
    fmt_kwargs = {name: "{" + name + "}" for name in names}
    consumed = set()
    for name in names:
        if name != "target" and name in params and params[name]:
            fmt_kwargs[name] = urllib.parse.quote(str(params[name][0]), safe="")
            consumed.add(name)
    try:
        url = _FORMATTER.vformat(target, (), fmt_kwargs)
    except (ValueError, KeyError, IndexError) as exc:
        raise RelayError(400, f"invalid target template: {exc}")

    forwarded = {k: v for k, v in params.items() if k != "target" and k not in consumed}
    query = urllib.parse.urlencode(forwarded, doseq=True)
    if query:
        url += ("&" if urllib.parse.urlsplit(url).query else "?") + query
    return host, url


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


_OPENER = urllib.request.build_opener(_NoRedirects)


def fetch(url: str, config: Config):
    """GET *url* without following redirects. Returns (status, body, truncated)."""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}, method="GET"
    )
    limit = config.max_body_bytes
    try:
        with _OPENER.open(request, timeout=config.timeout) as resp:
            raw = resp.read(limit + 1)
            return getattr(resp, "status", resp.getcode()), raw[:limit], len(raw) > limit
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(limit + 1)
        except Exception:
            raw = b""
        return exc.code, raw[:limit], len(raw) > limit
    except (TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        if isinstance(reason, TimeoutError):
            raise RelayError(504, "upstream timed out")
        raise RelayError(502, f"upstream unreachable: {reason}")
    except http.client.HTTPException as exc:
        raise RelayError(502, f"upstream error: {exc.__class__.__name__}")


RelayResult = namedtuple("RelayResult", "status headers body host")


def _error(status: int, message: str, host: str = "-") -> RelayResult:
    return RelayResult(status, [("Content-Type", TEXT_PLAIN)], (message + "\n").encode(), host)


def peek_host(params: dict) -> str:
    raw = (params.get("target") or [""])[0]
    try:
        return (urllib.parse.urlsplit(raw).hostname or "-").lower()
    except ValueError:
        return "-"


def merge_params(query_string: str, form: dict | None) -> dict:
    params = urllib.parse.parse_qs(query_string, keep_blank_values=True)
    if form:
        params.update(form)
    return params


def relay(params: dict, config: Config) -> RelayResult:
    targets = params.get("target") or [""]
    target = (targets[0] or "").strip()
    if not target:
        return _error(400, "missing required parameter: target")
    try:
        host, url = build_target_url(target, params, config)
        status, body, truncated = fetch(url, config)
    except RelayError as exc:
        return _error(exc.status, exc.message, peek_host(params))
    headers = [("Content-Type", TEXT_PLAIN)]
    if truncated:
        headers.append(("X-Relay-Truncated", "true"))
    return RelayResult(status, headers, body, host)


class RelayHandler(BaseHTTPRequestHandler):
    config: Config = None  # bound by make_server()
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        self._dispatch({})

    def do_POST(self):  # noqa: N802
        try:
            form = self._read_form()
        except RelayError as exc:
            self._respond(_error(exc.status, exc.message))
            return
        self._dispatch(form)

    def _read_form(self) -> dict:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length else 0
        except ValueError:
            raise RelayError(400, "invalid Content-Length")
        if length < 0:
            raise RelayError(400, "invalid Content-Length")
        if length > self.config.max_body_bytes:
            raise RelayError(413, f"request body exceeds {self.config.max_body_bytes} bytes")
        body = self.rfile.read(length) if length else b""
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if not body or ctype != "application/x-www-form-urlencoded":
            return {}
        return urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)

    def _dispatch(self, form: dict):
        params = merge_params(urllib.parse.urlsplit(self.path).query, form)
        try:
            result = relay(params, self.config)
        except Exception:
            logger.error("internal error for %s", urllib.parse.urlsplit(self.path).path)
            result = _error(500, "internal relay error", peek_host(params))
        self._respond(result)

    def _respond(self, result: RelayResult):
        self.target_host = result.host
        self.send_response(result.status)
        for key, value in result.headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(result.body)))
        self.end_headers()
        self.wfile.write(result.body)

    def log_message(self, fmt, *args):  # default includes full request line (query = secrets)
        pass

    def log_request(self, code="-", size="-"):
        path = urllib.parse.urlsplit(self.path).path
        logger.info("%s %s -> %s %s", self.client_address[0], path,
                    getattr(self, "target_host", "-"), code)


def make_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                config: Config | None = None) -> ThreadingHTTPServer:
    config = config or Config.from_env()
    handler = type("BoundRelayHandler", (RelayHandler,), {"config": config})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def wsgi_app(environ, start_response):
    """WSGI entrypoint for PaaS deployments: gunicorn 'get_relay:wsgi_app'."""
    config = getattr(wsgi_app, "config", None) or Config.from_env()
    method = environ.get("REQUEST_METHOD", "GET").upper()
    if method not in ("GET", "POST"):
        result = _error(405, "method not allowed: use GET or POST")
    else:
        try:
            form = {}
            if method == "POST":
                raw_length = environ.get("CONTENT_LENGTH")
                try:
                    length = int(raw_length) if raw_length else 0
                except ValueError:
                    raise RelayError(400, "invalid CONTENT_LENGTH")
                if length < 0:
                    raise RelayError(400, "invalid CONTENT_LENGTH")
                if length > config.max_body_bytes:
                    raise RelayError(413, f"request body exceeds {config.max_body_bytes} bytes")
                ctype = (environ.get("CONTENT_TYPE") or "").split(";", 1)[0].strip().lower()
                if length and ctype == "application/x-www-form-urlencoded":
                    raw = environ["wsgi.input"].read(length)
                    form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"),
                                                 keep_blank_values=True)
            params = merge_params(environ.get("QUERY_STRING", ""), form)
            result = relay(params, config)
        except RelayError as exc:
            result = _error(exc.status, exc.message, peek_host({}))
        except Exception:
            logger.error("internal wsgi error")
            result = _error(500, "internal relay error")
    reason = http.client.responses.get(result.status, "OK")
    headers = list(result.headers) + [("Content-Length", str(len(result.body)))]
    start_response(f"{result.status} {reason}", headers)
    return [result.body]


application = wsgi_app


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="get-relay: stateless GET relay")
    parser.add_argument("--host", default=os.environ.get("HOST", DEFAULT_HOST),
                        help=f"bind address (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT", DEFAULT_PORT)),
                        help="bind port (default: $PORT or 8000)")
    args = parser.parse_args(argv)

    try:
        config = Config.from_env()
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    httpd = make_server(args.host, args.port, config)
    print(f"get-relay {__version__} listening on http://{args.host}:{httpd.server_address[1]}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

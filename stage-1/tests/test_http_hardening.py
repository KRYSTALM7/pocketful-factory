"""Every method gets a JSON answer, HEAD mirrors GET, and responses carry security headers."""
import http.client
import json
import socket
from urllib.parse import urlsplit

import pytest

DOCUMENTED_CODES = {"malformed_request", "missing_idempotency_key", "unauthenticated",
                    "forbidden", "not_found", "idempotency_key_reuse", "validation_failed"}


def raw(base_url, method, path, headers=None):
    parts = urlsplit(base_url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=8)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def assert_security_headers(headers):
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("Referrer-Policy") == "no-referrer"


def test_head_mirrors_get_without_a_body(base_url):
    get_status, get_headers, get_body = raw(base_url, "GET", "/health")
    status, headers, body = raw(base_url, "HEAD", "/health")
    assert (status, body) == (get_status, b"")
    assert headers["Content-Type"] == get_headers["Content-Type"]
    assert headers["Content-Length"] == str(len(get_body))
    assert_security_headers(headers)


@pytest.mark.parametrize("method", ["OPTIONS", "TRACE", "CONNECT", "PROPFIND"])
@pytest.mark.parametrize("path", ["/me", "/health", "/no-such-route"])
def test_unsupported_methods_get_a_documented_json_error(base_url, method, path):
    status, headers, body = raw(base_url, method, path)
    assert 400 <= status < 500
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body)["error"]["code"] in DOCUMENTED_CODES
    assert_security_headers(headers)


def test_unparseable_request_line_is_a_json_400(base_url):
    parts = urlsplit(base_url)
    with socket.create_connection((parts.hostname, parts.port), timeout=8) as sock:
        sock.sendall(b"GET /health HTTP/9.9\r\nHost: x\r\n\r\n")
        response = b""
        while chunk := sock.recv(4096):
            response += chunk
    head, _, body = response.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 400")
    assert b"application/json" in head
    assert json.loads(body)["error"]["code"] == "malformed_request"


def test_json_responses_carry_security_headers_but_no_cors(base_url):
    for method, path in (("GET", "/health"), ("GET", "/me"), ("POST", "/payments")):
        status, headers, _ = raw(base_url, method, path)
        assert_security_headers(headers)
        assert not any(name.lower().startswith("access-control-") for name in headers)


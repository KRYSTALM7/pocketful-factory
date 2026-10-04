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


def raw_exchange(base_url, request):
    parts = urlsplit(base_url)
    with socket.create_connection((parts.hostname, parts.port), timeout=8) as sock:
        sock.sendall(request)
        response = b""
        while chunk := sock.recv(65536):
            response += chunk
    head, _, body = response.partition(b"\r\n\r\n")
    return head, body


@pytest.mark.parametrize("length", [b"\xb2", b"9" * 5000, b"0" * 4400, b"abc"],
                         ids=["latin1-superscript-two", "5000-digits", "4400-zeros", "letters"])
def test_malformed_content_length_is_a_json_400(base_url, length):
    head, body = raw_exchange(base_url, b"POST /auth/login HTTP/1.1\r\nHost: x\r\n"
                              b"Content-Type: application/json\r\nContent-Length: " + length
                              + b"\r\nConnection: close\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 400")
    assert json.loads(body)["error"]["code"] == "malformed_request"


@pytest.mark.parametrize("length_header", [b"Content-Length: 0\r\n", b""], ids=["zero", "missing"])
def test_empty_or_missing_content_length_still_works(base_url, length_header):
    head, _ = raw_exchange(base_url, b"GET /health HTTP/1.1\r\nHost: x\r\n" + length_header
                           + b"Connection: close\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200")


def test_deeply_nested_json_body_is_a_json_400(base_url):
    payload = b"[" * 200_000 + b"]" * 200_000
    head, body = raw_exchange(base_url, b"POST /auth/login HTTP/1.1\r\nHost: x\r\n"
                              b"Content-Type: application/json\r\nContent-Length: "
                              + str(len(payload)).encode() + b"\r\nConnection: close\r\n\r\n"
                              + payload)
    assert head.startswith(b"HTTP/1.1 400")
    assert json.loads(body)["error"]["code"] == "malformed_request"


def test_json_nested_past_the_depth_limit_is_a_json_400_on_a_keyed_write(base_url, world):
    # Shallow enough for json.loads, deep enough to break the recursive idempotency fingerprint.
    payload = b'{"to_handle":"bob","amount":1,"x":' + b"[" * 900 + b"]" * 900 + b"}"
    head, body = raw_exchange(base_url, b"POST /payments HTTP/1.1\r\nHost: x\r\n"
                              b"Authorization: Bearer " + world.ada.token.encode()
                              + b"\r\nIdempotency-Key: depth-900\r\n"
                              b"Content-Type: application/json\r\nContent-Length: "
                              + str(len(payload)).encode() + b"\r\nConnection: close\r\n\r\n"
                              + payload)
    assert head.startswith(b"HTTP/1.1 400")
    assert json.loads(body)["error"]["code"] == "malformed_request"

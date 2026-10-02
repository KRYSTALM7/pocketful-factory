"""Transport limits: a 50-request burst and an export larger than the ordinary body cap."""
import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from harness.http import Api, assert_status, new_key

USERS = [
    {"id": "u_ada", "email": "ada@example.com", "password": "correct horse",
     "display_name": "Ada", "handle": "ada", "balance": 100_000},
    {"id": "u_bob", "email": "bob@example.com", "password": "correct horse",
     "display_name": "Bob", "handle": "bob", "balance": 100_000},
]


def test_fifty_simultaneous_requests_all_complete(reset, base_url):
    reset({"currency": "EUR", "minor_units": 2, "users": USERS, "payments": [], "requests": []})
    ada = Api(base_url).authenticate("ada@example.com", "correct horse")
    bob = Api(base_url).authenticate("bob@example.com", "correct horse")
    rounds = 5

    def send(gate, index):
        gate.wait()
        if index % 2:
            return Api(base_url).get("/health")
        return ada.post("/payments", json={"to_handle": "bob", "amount": 1},
                        idempotency_key=new_key())

    # A short listen backlog drops part of a burst only some of the time, so repeat it.
    # Connection resets or refusals raise here and fail the test.
    for _ in range(rounds):
        gate = threading.Barrier(50)
        with ThreadPoolExecutor(max_workers=50) as pool:
            responses = list(pool.map(lambda index: send(gate, index), range(50)))
        assert sorted(r.status_code for r in responses) == [200] * 25 + [201] * 25
    balances = [assert_status(c.get("/me"), 200).json()["balance"] for c in (ada, bob)]
    assert balances == [100_000 - 25 * rounds, 100_000 + 25 * rounds]


def test_export_larger_than_the_body_cap_imports_unchanged(reset, base_url):
    note = "n" * 200
    payments = [{"id": f"p_{i:05d}", "from_user_id": "u_ada" if i % 2 else "u_bob",
                 "to_user_id": "u_bob" if i % 2 else "u_ada", "amount": 1 + i % 7,
                 "note": note, "visibility": "public"} for i in range(6000)]
    # Alternate directions with matching amounts so seeded balances stay consistent.
    for i in range(0, 6000, 2):
        payments[i]["amount"] = payments[i + 1]["amount"]
    reset({"currency": "EUR", "minor_units": 2, "users": USERS, "payments": payments,
           "requests": []})
    client = Api(base_url)
    export = assert_status(client.get("/_test/export"), 200)
    raw = export._body
    assert len(raw) > 2_000_000

    # Anything else on the service is cleared, then the exact bytes come back in.
    reset({"currency": "EUR", "minor_units": 2, "users": USERS, "payments": [], "requests": []})
    assert_status(client.post("/_test/import", content=raw), 204)
    assert json.loads(client.get("/_test/export")._body) == json.loads(raw)
    ada = Api(base_url).authenticate("ada@example.com", "correct horse")
    assert ada.get("/me").json()["balance"] == 100_000
    assert ada.get("/activity", params={"limit": 200}).json()["has_more"] is True
    # Other endpoints keep the ordinary cap; the declared length alone is rejected.
    host, port = urlsplit(base_url).hostname, urlsplit(base_url).port
    with socket.create_connection((host, port), timeout=8) as sock:
        sock.sendall(b"POST /_test/reset HTTP/1.1\r\nHost: x\r\n"
                     b"Content-Type: application/json\r\nContent-Length: 2000001\r\n\r\n")
        assert sock.recv(64).startswith(b"HTTP/1.1 400")

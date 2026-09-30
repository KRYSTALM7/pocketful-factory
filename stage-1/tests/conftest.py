import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harness.http import Api, assert_status, new_key
import fixtures as fx

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def base_url():
    external_url = os.environ.get("POCKETFUL_BASE_URL")
    if external_url:
        yield external_url.rstrip("/")
        return
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    env["PORT"] = str(port)
    proc = subprocess.Popen([sys.executable, str(ROOT / "app.py")], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("Pocketful service exited during startup")
            try:
                if Api(url).get("/health").status_code == 200:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("Pocketful service did not become healthy")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.fixture
def api(base_url):
    return lambda token=None: Api(base_url, token)


@pytest.fixture
def reset(base_url):
    def do_reset(fixture=None, raw=False):
        client = Api(base_url)
        if raw:
            response = client.post("/_test/reset", content=__import__("json").dumps(fixture))
        else:
            response = client.post("/_test/reset", json=fixture or fx.fixture())
            assert_status(response, 204)
        return response
    do_reset()
    return do_reset


@pytest.fixture
def world(reset, api):
    fixture = fx.fixture()
    reset(fixture)
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    bob = api().authenticate(fx.BOB["email"], fx.BOB["password"])
    cy = api().authenticate(fx.CY["email"], fx.CY["password"])
    return type("World", (), {"ada": ada, "bob": bob, "cy": cy,
                              "fixture": fixture, "currency": "EUR", "minor_units": 2})()


@pytest.fixture
def pay(world):
    unset = object()
    def do_pay(client=None, to_handle="bob", amount=100, note=None,
               visibility=unset, key=None, **extra):
        client = client or world.ada
        body = {"to_handle": to_handle, "amount": amount, **extra}
        if note is not None:
            body["note"] = note
        if visibility is not unset:
            body["visibility"] = visibility
        return client.post("/payments", json=body,
                           idempotency_key=key if key is not None else new_key())
    return do_pay


@pytest.fixture
def ask(world):
    def do_ask(client=None, payer_handle="ada", amount=100, note=None, key=None):
        client = client or world.bob
        body = {"payer_handle": payer_handle, "amount": amount}
        if note is not None:
            body["note"] = note
        return client.post("/requests", json=body,
                           idempotency_key=key if key is not None else new_key())
    return do_ask


@pytest.fixture
def balance():
    return lambda client: client.get("/me").json()["balance"]


@pytest.fixture
def conservation(world, balance):
    expected = sum(u["balance"] for u in world.fixture["users"])
    return lambda _world: (_ for _ in ()).throw(AssertionError("money was not conserved")) \
        if sum(balance(c) for c in (world.ada, world.bob, world.cy)) != expected else None

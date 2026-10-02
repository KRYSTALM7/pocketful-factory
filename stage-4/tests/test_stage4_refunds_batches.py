import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

import fixtures as fx
from harness.http import assert_error, assert_status, new_key

pytestmark = pytest.mark.stage(4)


def refund(client, payment_id, amount, key=None):
    return client.post(f"/payments/{payment_id}/refunds", json={"amount": amount},
                       idempotency_key=key or new_key())


def correct(client, payment_id, expected, amount, effective_at, key=None, reason="fix"):
    return client.post(f"/payments/{payment_id}/corrections", json={
        "expected_revision": expected, "amount": amount,
        "effective_at": effective_at, "reason": reason}, idempotency_key=key or new_key())


def item(payment, amount, expected=1, effective_at=None, reason="batch fix", **extra):
    return {"payment_id": payment["payment_id"], "expected_revision": expected,
            "amount": amount, "effective_at": effective_at or payment["created_at"],
            "reason": reason, **extra}


def batch(client, items, key=None):
    return client.post("/correction-batches", json={"corrections": items},
                       idempotency_key=key or new_key())


def login(api, handle):
    return api().authenticate(f"{handle}@example.com", "correct horse")


@pytest.fixture
def ops(reset, api):
    """Ada (operator, 10000), Bob (2500), Cy (5000); Ada may settle and batch-correct."""
    fixture = fx.fixture()
    fixture["settlement_operator_ids"] = ["u_ada"]
    reset(fixture)
    return type("Ops", (), {"ada": login(api, "ada"), "bob": login(api, "bob"),
                            "cy": login(api, "cy")})()


def settle(client, transfers):
    return assert_status(client.post("/settlements", json={"transfers": transfers},
                                      idempotency_key=new_key()), 201).json()


def money(*clients):
    return sum(c.get("/me").json()["balance"] for c in clients)


# Refunds ------------------------------------------------------------------------------

def test_refund_is_a_reverse_payment_and_replays(world, pay):
    original = assert_status(pay(amount=1000, note="dinner", visibility="private"), 201).json()
    assert original["refund_of"] is None
    key = new_key()
    created = assert_status(refund(world.bob, original["payment_id"], 300, key), 201).json()
    assert (created["from_user_id"], created["to_user_id"]) == ("u_bob", "u_ada")
    assert created["refund_of"] == original["payment_id"]
    assert created["request_id"] is None and created["authorization_id"] is None
    assert (created["note"], created["visibility"], created["amount"]) == ("dinner", "private", 300)
    replay = refund(world.bob, original["payment_id"], 300, key)
    assert replay.status_code == 200 and replay.json() == created
    assert_error(refund(world.bob, original["payment_id"], 301, key), 409, "idempotency_key_reuse")
    assert world.ada.get("/me").json()["balance"] == fx.ADA["balance"] - 700
    assert world.bob.get("/me").json()["balance"] == fx.BOB["balance"] + 700
    activity = world.ada.get("/activity").json()["payments"]
    assert [p["payment_id"] for p in activity][:2] == [created["payment_id"], original["payment_id"]]
    assert next(p for p in activity if p["payment_id"] == original["payment_id"])["amount"] == 1000


def test_refund_authorization_and_validation(world, pay):
    original = assert_status(pay(amount=500), 201).json()
    pid = original["payment_id"]
    assert_error(refund(world.ada, pid, 100), 403, "forbidden")
    assert_error(refund(world.cy, pid, 100), 403, "forbidden")
    assert_error(refund(world.bob, "p_missing", 100), 404, "not_found")
    for amount in (0, -1, 1.5, "100", None, 1_000_000_001):
        assert_error(refund(world.bob, pid, amount), 422, "validation_failed")
    assert_status(world.bob.post(f"/payments/{pid}/refunds", json={"amount": 1}), 400)
    assert_status(refund(world.bob, pid, 200), 201)
    assert_status(refund(world.bob, pid, 300), 201)
    assert_error(refund(world.bob, pid, 1), 422, "refund_exceeds_payment")
    assert world.bob.get("/me").json()["balance"] == fx.BOB["balance"]


def test_refund_of_refund_is_rejected(world, pay):
    original = assert_status(pay(amount=500), 201).json()
    back = assert_status(refund(world.bob, original["payment_id"], 100), 201).json()
    assert_error(refund(world.ada, back["payment_id"], 50), 422, "invalid_refund_target")


def test_refund_uses_available_funds_atomically(world, pay):
    original = assert_status(pay(amount=1000), 201).json()
    assert_status(world.bob.post("/authorizations", json={"to_handle": "cy",
        "amount": fx.BOB["balance"] + 500}, idempotency_key=new_key()), 201)
    before = world.bob.get("/_test/export").json()
    assert_error(refund(world.bob, original["payment_id"], 600), 409, "insufficient_funds")
    assert world.bob.get("/_test/export").json() == before
    assert_status(refund(world.bob, original["payment_id"], 500), 201)


def test_refund_caps_follow_the_corrected_amount(world, pay):
    original = assert_status(pay(amount=1000), 201).json()
    pid = original["payment_id"]
    assert_status(correct(world.ada, pid, 1, 400, original["created_at"]), 201)
    assert_error(refund(world.bob, pid, 401), 422, "refund_exceeds_payment")
    assert_status(refund(world.bob, pid, 300), 201)
    assert_error(correct(world.ada, pid, 2, 299, original["created_at"]),
                 422, "refund_exceeds_payment")
    assert_status(correct(world.ada, pid, 2, 300, original["created_at"]), 201)
    assert_error(refund(world.bob, pid, 1), 422, "refund_exceeds_payment")


def test_refunds_and_captures_cannot_be_corrected(world, pay):
    original = assert_status(pay(amount=1000), 201).json()
    back = assert_status(refund(world.bob, original["payment_id"], 100), 201).json()
    assert_error(correct(world.bob, back["payment_id"], 1, 50, back["created_at"]),
                 422, "linked_payment_immutable")
    auth = assert_status(world.ada.post("/authorizations", json={"to_handle": "bob",
        "amount": 300}, idempotency_key=new_key()), 201).json()
    capture = assert_status(world.bob.post(f"/authorizations/{auth['authorization_id']}/capture",
        json={}, idempotency_key=new_key()), 201).json()
    assert_error(correct(world.ada, capture["payment_id"], 1, 100, capture["created_at"]),
                 422, "linked_payment_immutable")


def test_refunding_a_capture_or_request_payment_reopens_nothing(world, ask):
    auth = assert_status(world.ada.post("/authorizations", json={"to_handle": "bob",
        "amount": 1000}, idempotency_key=new_key()), 201).json()
    capture = assert_status(world.bob.post(f"/authorizations/{auth['authorization_id']}/capture",
        json={"amount": 400, "final": True}, idempotency_key=new_key()), 201).json()
    back = assert_status(refund(world.bob, capture["payment_id"], 400), 201).json()
    assert back["authorization_id"] is None and back["refund_of"] == capture["payment_id"]
    me = world.ada.get("/me").json()
    assert (me["balance"], me["held"], me["available"]) == (fx.ADA["balance"], 0, fx.ADA["balance"])
    state = world.ada.get(f"/authorizations").json()["authorizations"][0]
    assert state["status"] == "captured" and state["remaining_amount"] == 0

    request = assert_status(ask(amount=250), 201).json()
    paid = assert_status(world.ada.post(f"/requests/{request['request_id']}/pay", json={},
                                        idempotency_key=new_key()), 201).json()
    assert_status(refund(world.bob, paid["payment_id"], 250), 201)
    stored = world.bob.get("/requests").json()["requests"][0]
    assert stored["status"] == "paid" and stored["payment_id"] == paid["payment_id"]


def test_settlement_member_refund_keeps_membership(ops):
    receipt = settle(ops.ada, [{"from_handle": "ada", "to_handle": "bob", "amount": 400}])
    member = receipt["payments"][0]
    back = assert_status(refund(ops.bob, member["payment_id"], 150), 201).json()
    assert back["settlement_id"] is None
    activity = ops.bob.get("/activity").json()["payments"]
    assert next(p for p in activity if p["payment_id"] == member["payment_id"])["settlement_id"] \
        == receipt["settlement_id"]


# Batch corrections --------------------------------------------------------------------

def test_batch_requires_operator_key_and_valid_shape(ops, api):
    payment = assert_status(ops.ada.post("/payments", json={"to_handle": "bob", "amount": 10},
                                         idempotency_key=new_key()), 201).json()
    assert_status(api().post("/correction-batches", json={"corrections": [item(payment, 5)]},
                             idempotency_key=new_key()), 401)
    assert_error(batch(ops.bob, [item(payment, 5)]), 403, "forbidden")
    assert_status(ops.ada.post("/correction-batches", json={"corrections": [item(payment, 5)]}), 400)
    for corrections in ([], [item(payment, 5)] * 2, [1], "x", [item(payment, 5)] * 33):
        assert_error(ops.ada.post("/correction-batches", json={"corrections": corrections},
                                  idempotency_key=new_key()), 422, "validation_failed")
    assert_error(batch(ops.ada, [item(payment, 5, reason="")]), 422, "validation_failed")
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert_error(batch(ops.ada, [item(payment, 5, effective_at=future)]), 422, "validation_failed")
    assert_error(batch(ops.ada, [{**item(payment, 5), "payment_id": "p_nope"}]), 404, "not_found")
    assert_error(batch(ops.ada, [item(payment, 5, expected=2)]), 409, "stale_revision")


def test_batch_corrects_whole_settlement_with_shared_recorded_at(ops):
    receipt = settle(ops.ada, [{"from_handle": "ada", "to_handle": "bob", "amount": 400},
                               {"from_handle": "bob", "to_handle": "cy", "amount": 100}])
    first, second = receipt["payments"]
    other = assert_status(ops.cy.post("/payments", json={"to_handle": "ada", "amount": 70},
                                      idempotency_key=new_key()), 201).json()
    effective = datetime.fromisoformat(first["created_at"])
    # Same instant, different offset spelling.
    shifted = effective.astimezone(timezone(timedelta(hours=2))).isoformat()
    before = money(ops.ada, ops.bob, ops.cy)
    key = new_key()
    body = [item(second, 0, effective_at=shifted, ignored=True), item(other, 50),
            item(first, 300, effective_at=first["created_at"])]
    created = assert_status(batch(ops.ada, body, key), 201).json()
    assert [r["payment_id"] for r in created["revisions"]] == [
        second["payment_id"], other["payment_id"], first["payment_id"]]
    assert all(r["recorded_at"] == created["recorded_at"] and r["revision"] == 2
               and r["correction_batch_id"] == created["correction_batch_id"]
               for r in created["revisions"])
    assert money(ops.ada, ops.bob, ops.cy) == before
    assert ops.ada.get("/me").json()["balance"] == fx.ADA["balance"] - 300 + 50
    replay = batch(ops.ada, body, key)
    assert replay.status_code == 200 and replay.json() == created
    revisions = ops.ada.get(f"/payments/{first['payment_id']}/revisions").json()["revisions"]
    assert [r["correction_batch_id"] for r in revisions] == [None, created["correction_batch_id"]]
    assert all(datetime.fromisoformat(created["recorded_at"]) >
               datetime.fromisoformat(r["recorded_at"]) for r in revisions[:1])
    # Single corrections still refuse settlement members.
    assert_error(correct(ops.ada, first["payment_id"], 2, 200, first["created_at"]),
                 422, "linked_payment_immutable")


def test_batch_settlement_rules_and_precedence(ops):
    receipt = settle(ops.ada, [{"from_handle": "ada", "to_handle": "bob", "amount": 400},
                               {"from_handle": "ada", "to_handle": "cy", "amount": 100}])
    first, second = receipt["payments"]
    assert_error(batch(ops.ada, [item(first, 0)]), 422, "incomplete_settlement")
    # Item errors come first, in input order.
    assert_error(batch(ops.ada, [item(first, 0), {**item(first, 0), "payment_id": "p_x"}]),
                 404, "not_found")
    assert_error(batch(ops.ada, [item(first, 0, reason=""), {**item(first, 0), "payment_id": "p_x"}]),
                 422, "validation_failed")
    later = (datetime.fromisoformat(first["created_at"]) - timedelta(seconds=1)).isoformat()
    assert_error(batch(ops.ada, [item(first, 0), item(second, 0, effective_at=later)]),
                 422, "validation_failed")
    auth = assert_status(ops.ada.post("/authorizations", json={"to_handle": "bob",
        "amount": 300}, idempotency_key=new_key()), 201).json()
    capture = assert_status(ops.bob.post(f"/authorizations/{auth['authorization_id']}/capture",
        json={}, idempotency_key=new_key()), 201).json()
    assert_error(batch(ops.ada, [item(first, 0), item(capture, 0)]), 422, "linked_payment_immutable")
    # Settlement completeness precedes funds.
    assert_error(batch(ops.ada, [item(first, 10**9)]), 422, "incomplete_settlement")
    # The complete settlement at one instant is accepted.
    assert_status(batch(ops.ada, [item(first, 0), item(second, 0)]), 201)


def test_batch_affordability_uses_the_combined_effect(reset, api):
    fixture = fx.fixture(users=[fx.user("ada", 1000), fx.user("bob", 0), fx.user("cy", 1000)])
    fixture["settlement_operator_ids"] = ["u_ada"]
    reset(fixture)
    ada, bob, cy = login(api, "ada"), login(api, "bob"), login(api, "cy")
    p1 = assert_status(ada.post("/payments", json={"to_handle": "bob", "amount": 100},
                                idempotency_key=new_key()), 201).json()
    p2 = assert_status(cy.post("/payments", json={"to_handle": "bob", "amount": 50},
                               idempotency_key=new_key()), 201).json()
    assert_status(bob.post("/payments", json={"to_handle": "cy", "amount": 150},
                           idempotency_key=new_key()), 201)
    # Alone, reducing p1 would debit Bob, who has nothing.
    assert_error(correct(ada, p1["payment_id"], 1, 0, p1["created_at"]), 409, "insufficient_funds")
    assert_error(batch(ada, [item(p1, 0)]), 409, "insufficient_funds")
    # Together, Bob's credit from p2 covers the debit; history stays nonnegative too.
    assert_status(batch(ada, [item(p1, 0), item(p2, 150)]), 201)
    assert (bob.get("/me").json()["balance"], money(ada, bob, cy)) == (0, 2000)


def test_rejected_batch_leaves_everything_unchanged(reset, api):
    earlier = "2020-01-01T00:00:00+00:00"
    later = "2020-01-01T00:00:01+00:00"
    fixture = fx.fixture(users=[fx.user("ada", 100), fx.user("bob", 100)], payments=[
        {"id": "p_out", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 100,
         "created_at": earlier},
        {"id": "p_back", "from_user_id": "u_bob", "to_user_id": "u_ada", "amount": 100,
         "created_at": later}])
    fixture["settlement_operator_ids"] = ["u_ada"]
    reset(fixture)
    ada = login(api, "ada")
    before = ada.get("/_test/export").json()
    key = new_key()
    body = [{"payment_id": "p_out", "expected_revision": 1, "amount": 150,
             "effective_at": earlier, "reason": "raise"}]
    assert_error(batch(ada, body, key), 409, "historical_overdraft")
    assert ada.get("/_test/export").json() == before
    # The key was not consumed: a different, valid body may now use it.
    body[0]["amount"] = 50
    assert_status(batch(ada, body, key), 201)


def test_batch_keeps_snapshots_and_receipts(ops):
    receipt_key = new_key()
    receipt = assert_status(ops.ada.post("/settlements", json={"transfers": [
        {"from_handle": "ada", "to_handle": "bob", "amount": 400}]},
        idempotency_key=receipt_key), 201).json()
    member = receipt["payments"][0]
    snapshot = ops.bob.get("/statement").json()
    assert_status(batch(ops.ada, [item(member, 100)]), 201)
    frozen = ops.bob.get("/statement", params={"snapshot": snapshot["snapshot"]}).json()
    assert frozen["entries"] == snapshot["entries"]
    fresh = ops.bob.get("/statement").json()
    assert fresh["entries"][-1]["payment"]["amount"] == 100 and fresh["entries"][-1]["revision"] == 2
    retry = ops.ada.post("/settlements", json={"transfers": [
        {"from_handle": "ada", "to_handle": "bob", "amount": 400}]}, idempotency_key=receipt_key)
    assert retry.status_code == 200 and retry.json() == receipt


def test_concurrent_batch_and_single_correction_commit_once(ops):
    payment = assert_status(ops.ada.post("/payments", json={"to_handle": "bob", "amount": 100},
                                         idempotency_key=new_key()), 201).json()

    def run(kind):
        if kind == "batch":
            return batch(ops.ada, [item(payment, 80)])
        return correct(ops.ada, payment["payment_id"], 1, 70, payment["created_at"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(run, ["batch", "single", "batch", "single"]))
    assert sorted(r.status_code for r in responses) == [201, 409, 409, 409]
    assert len(ops.ada.get(f"/payments/{payment['payment_id']}/revisions").json()["revisions"]) == 2
    assert money(ops.ada, ops.bob, ops.cy) == fx.ADA["balance"] + fx.BOB["balance"] + fx.CY["balance"]


# Import and export --------------------------------------------------------------------

def test_stage4_export_round_trips_refunds_and_batches(ops):
    receipt = settle(ops.ada, [{"from_handle": "ada", "to_handle": "bob", "amount": 400}])
    member = receipt["payments"][0]
    assert_status(batch(ops.ada, [item(member, 300)]), 201)
    back = assert_status(refund(ops.bob, member["payment_id"], 100), 201).json()
    snapshot = ops.bob.get("/statement").json()
    exported = ops.ada.get("/_test/export").json()
    assert_status(ops.ada.post("/_test/import", json=copy.deepcopy(exported)), 204)
    assert ops.ada.get("/_test/export").json() == exported
    assert_error(refund(ops.bob, member["payment_id"], 201), 422, "refund_exceeds_payment")
    page = ops.bob.get("/statement", params={"snapshot": snapshot["snapshot"]}).json()
    assert page["entries"] == snapshot["entries"]
    assert any(e["payment"]["payment_id"] == back["payment_id"] for e in page["entries"])


@pytest.fixture(scope="module")
def stage3_api():
    """Run the Stage 3 service beside this one for cross-version export checks."""
    import os, socket, subprocess, sys, time
    from pathlib import Path
    from harness.http import Api
    root = Path(__file__).resolve().parents[2] / "stage-3"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen([sys.executable, str(root / "app.py")], cwd=root,
                            env={**os.environ, "PORT": str(port)},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                if Api(url).get("/health").status_code == 200:
                    break
            except Exception:
                if time.monotonic() > deadline or proc.poll() is not None:
                    raise RuntimeError("Stage 3 service did not start")
                time.sleep(0.1)
        yield lambda: Api(url)
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_stage3_snapshot_pages_after_import_into_stage4(stage3_api, reset, api):
    assert_status(stage3_api().post("/_test/reset", json=fx.fixture()), 204)
    ada3 = stage3_api().authenticate(fx.ADA["email"], fx.ADA["password"])
    payment = assert_status(ada3.post("/payments", json={"to_handle": "bob", "amount": 100},
                                      idempotency_key=new_key()), 201).json()
    assert_status(correct(ada3, payment["payment_id"], 1, 70, payment["created_at"]), 201)
    statement = assert_status(ada3.get("/statement"), 200).json()
    document = ada3.get("/_test/export").json()
    ada4 = login(api, "ada")
    assert_status(ada4.post("/_test/import", json=document), 204)
    ada4 = login(api, "ada")
    page = assert_status(ada4.get("/statement", params={"snapshot": statement["snapshot"]}), 200).json()
    assert page == statement
    assert_error(login(api, "bob").get("/statement", params={"snapshot": statement["snapshot"]}),
                 404, "not_found")


def test_import_replaces_snapshots_and_rejects_malformed_ones(world, pay):
    assert_status(pay(amount=100), 201)
    statement = world.ada.get("/statement").json()
    document = world.ada.get("/_test/export").json()
    for broken in ([], {statement["snapshot"]: {"user_id": "u_nobody", "result": {}}},
                   {statement["snapshot"]: 5}):
        assert_error(world.ada.post("/_test/import", json={**document, "statement_snapshots": broken}),
                     422, "validation_failed")
    assert world.ada.get("/statement", params={"snapshot": statement["snapshot"]}).status_code == 200
    without = {key: value for key, value in document.items() if key != "statement_snapshots"}
    assert_status(world.ada.post("/_test/import", json=without), 204)
    assert_error(world.ada.get("/statement", params={"snapshot": statement["snapshot"]}),
                 404, "not_found")


def test_stage3_export_without_refund_fields_imports(world, pay):
    payment = assert_status(pay(amount=100), 201).json()
    assert_status(correct(world.ada, payment["payment_id"], 1, 60, payment["created_at"]), 201)
    document = world.ada.get("/_test/export").json()
    for stored in document["state"]["payments"]:
        stored.pop("refund_of")
    assert_status(world.ada.post("/_test/import", json=document), 204)
    assert_status(refund(world.bob, payment["payment_id"], 60), 201)
    assert_error(refund(world.bob, payment["payment_id"], 1), 422, "refund_exceeds_payment")


def test_import_rejects_inconsistent_refunds(world, pay):
    payment = assert_status(pay(amount=100), 201).json()
    assert_status(refund(world.bob, payment["payment_id"], 40), 201)
    document = world.ada.get("/_test/export").json()
    for stored in document["state"]["payments"]:
        if stored.get("refund_of"):
            stored["refund_of"] = stored["id"]  # a refund of a refund
    assert_error(world.ada.post("/_test/import", json=document), 422, "validation_failed")

"""Adversarial regressions found while reviewing the Stage 1 contract."""

import copy
from concurrent.futures import ThreadPoolExecutor

import fixtures as fx
from harness.http import assert_error, assert_status, new_key


def test_large_exponent_amount_is_validation_not_server_error(world):
    response = world.ada.post(
        "/payments", content='{"to_handle":"bob","amount":1e999}',
        idempotency_key=new_key())
    assert_error(response, 422, "validation_failed")


def test_extreme_exponent_amount_does_not_escape_json_parser(world):
    response = world.ada.post(
        "/payments", content='{"to_handle":"bob","amount":1e999999999999999999999999}',
        idempotency_key=new_key())
    assert_error(response, 422, "validation_failed")


def test_precise_fractional_amount_is_not_rounded_to_an_integer(world):
    response = world.ada.post(
        "/payments", content='{"to_handle":"bob","amount":1.000000000000000001}',
        idempotency_key=new_key())
    assert_error(response, 422, "validation_failed")


def test_idempotency_canonicalizes_unrepresentably_large_unknown_numbers(world):
    first = world.ada.post(
        "/payments", content='{"to_handle":"bob","amount":10,"ignored":1e999999999999999999999999}',
        idempotency_key="large-number-canonicalization")
    assert_status(first, 201)
    replay = world.ada.post(
        "/payments", content='{"amount":10,"ignored":10e999999999999999999999998,"to_handle":"bob"}',
        idempotency_key="large-number-canonicalization")
    assert_status(replay, 200)
    assert replay.json() == first.json()


def test_unknown_large_exponent_field_is_ignored(world):
    response = world.ada.post(
        "/payments", content='{"to_handle":"bob","amount":10,"ignored":1e999}',
        idempotency_key=new_key())
    assert_status(response, 201)


def test_wrong_type_required_handle_is_malformed_request(world):
    response = world.ada.post("/payments", json={"to_handle": None, "amount": 10},
                              idempotency_key=new_key())
    assert_error(response, 400, "malformed_request")


def test_wrong_type_settlement_handle_is_malformed_request(reset, api):
    fixture = fx.fixture()
    fixture["settlement_operator_ids"] = ["u_ada"]
    reset(fixture)
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    response = ada.post("/settlements", json={"transfers": [
        {"from_handle": None, "to_handle": "bob", "amount": 10},
    ]}, idempotency_key=new_key())
    assert_error(response, 400, "malformed_request")


def test_unhashable_seeded_payment_party_rejects_fixture_atomically(world):
    fixture = fx.fixture()
    fixture["payments"] = [{"id": "p_bad", "from_user_id": [],
                            "to_user_id": "u_bob", "amount": 10}]
    before = world.ada.get("/me").json()
    assert_error(world.ada.post("/_test/reset", json=fixture), 422, "validation_failed")
    assert world.ada.get("/me").json() == before


def test_import_rejects_paid_request_without_payment_link_atomically(world, ask):
    request = assert_status(ask(amount=10), 201).json()
    snapshot = assert_status(world.ada.get("/_test/export"), 200).json()
    seeded = next(r for r in snapshot["state"]["requests"]
                  if r["id"] == request["request_id"])
    seeded["status"] = "paid"
    seeded["payment_id"] = None

    assert_error(world.ada.post("/_test/import", json=snapshot), 422, "validation_failed")
    current = next(r for r in world.ada.get("/requests").json()["requests"]
                   if r["request_id"] == request["request_id"])
    assert current["status"] == "pending" and current["payment_id"] is None


def test_import_rejects_malformed_password_hash_without_replacing_state(world):
    snapshot = assert_status(world.ada.get("/_test/export"), 200).json()
    snapshot["state"]["users"]["u_ada"]["password_hash"] = "bad:hash"
    assert_error(world.ada.post("/_test/import", json=snapshot), 422, "validation_failed")
    assert_status(world.ada.get("/me"), 200)


def test_import_rejects_boolean_version_and_malformed_idempotency_entry(world):
    assert_status(world.ada.post(
        "/payments", json={"to_handle": "bob", "amount": 10},
        idempotency_key="import-shape-validation"), 201)
    baseline = assert_status(world.ada.get("/_test/export"), 200).json()

    boolean_version = copy.deepcopy(baseline)
    boolean_version["format_version"] = True
    assert_error(world.ada.post("/_test/import", json=boolean_version),
                 422, "validation_failed")
    assert world.ada.get("/_test/export").json() == baseline

    malformed_entry = copy.deepcopy(baseline)
    slot = next(iter(malformed_entry["state"]["idempotency"]))
    malformed_entry["state"]["idempotency"][slot].pop("body")
    assert_error(world.ada.post("/_test/import", json=malformed_entry),
                 422, "validation_failed")
    assert world.ada.get("/_test/export").json() == baseline


def test_feed_sorts_seeded_offset_timestamps_by_instant(reset, api):
    fixture = fx.fixture(payments=[
        {"id": "p_older", "from_user_id": "u_ada", "to_user_id": "u_bob",
         "amount": 1, "created_at": "2026-09-24T12:00:00+02:00"},
        {"id": "p_newer", "from_user_id": "u_ada", "to_user_id": "u_bob",
         "amount": 1, "created_at": "2026-09-24T10:30:00+00:00"},
    ])
    reset(fixture)
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    payments = ada.get("/activity").json()["payments"]
    assert [p["payment_id"] for p in payments] == ["p_newer", "p_older"]


def test_concurrent_payment_of_one_request_moves_money_once(world, ask):
    request = assert_status(ask(amount=500), 201).json()
    payer_before = world.ada.get("/me").json()["balance"]
    recipient_before = world.bob.get("/me").json()["balance"]

    def pay_once(_):
        return world.ada.post(
            f"/requests/{request['request_id']}/pay", json={}, idempotency_key=new_key())

    with ThreadPoolExecutor(max_workers=20) as pool:
        responses = list(pool.map(pay_once, range(20)))
    assert sum(response.status_code == 201 for response in responses) == 1
    for response in responses:
        if response.status_code != 201:
            assert_error(response, 409, "request_not_pending")
    assert world.ada.get("/me").json()["balance"] == payer_before - 500
    assert world.bob.get("/me").json()["balance"] == recipient_before + 500
    matches = [p for p in world.ada.get("/activity", params={"limit": 200}).json()["payments"]
               if p["request_id"] == request["request_id"]]
    assert len(matches) == 1


def test_seeded_zero_share_request_can_be_paid(reset, api):
    fixture = fx.fixture(requests=[{
        "id": "rq_zero", "requester_id": "u_bob", "payer_id": "u_ada",
        "amount": 0, "note": "split remainder", "status": "pending",
    }])
    reset(fixture)
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    payment = assert_status(ada.post("/requests/rq_zero/pay", json={},
                                     idempotency_key=new_key()), 201).json()
    assert payment["amount"] == 0 and payment["request_id"] == "rq_zero"


def test_insufficient_settlement_retry_reuses_key_after_funding(reset, api):
    fixture = fx.fixture(users=[fx.user("ada", 0), fx.user("bob", 100), fx.user("cy", 0)])
    fixture["settlement_operator_ids"] = ["u_ada"]
    reset(fixture)
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    bob = api().authenticate(fx.BOB["email"], fx.BOB["password"])
    cy = api().authenticate(fx.CY["email"], fx.CY["password"])
    body = {"transfers": [{"from_handle": "ada", "to_handle": "cy", "amount": 50}]}
    key = "retry-after-funding"

    assert_error(ada.post("/settlements", json=body, idempotency_key=key),
                 409, "insufficient_funds")
    assert_status(bob.post("/payments", json={"to_handle": "ada", "amount": 50},
                           idempotency_key=new_key()), 201)
    settled = assert_status(ada.post("/settlements", json=body,
                                     idempotency_key=key), 201).json()
    assert len(settled["payments"]) == 1
    assert ada.get("/me").json()["balance"] == 0
    assert bob.get("/me").json()["balance"] == 50
    assert cy.get("/me").json()["balance"] == 50
    assert sum(client.get("/me").json()["balance"] for client in (ada, bob, cy)) == 100

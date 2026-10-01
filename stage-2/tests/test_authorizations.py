import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import fixtures as fx
from harness.http import assert_error, assert_status, new_key


def test_authorization_holds_available_without_moving_total(world):
    key = new_key()
    first = assert_status(world.ada.post(
        "/authorizations", json={"to_handle": "bob", "amount": 2000},
        idempotency_key=key), 201).json()
    assert first["status"] == "open"
    me = assert_status(world.ada.get("/me"), 200).json()
    assert me["balance"] == me["total"] == fx.ADA["balance"]
    assert me["held"] == 2000 and me["available"] == 8000
    replay = assert_status(world.ada.post(
        "/authorizations", json={"amount": 2000, "to_handle": "bob"},
        idempotency_key=key), 200).json()
    assert replay == first
    assert world.ada.get("/me").json()["held"] == 2000
    assert not any(p.get("authorization_id") == first["authorization_id"]
                   for p in world.ada.get("/activity").json()["payments"])


def test_holds_reduce_affordability_for_payment_request_pay_and_settlement(reset, api):
    fixture = fx.fixture()
    fixture["settlement_operator_ids"] = ["u_ada"]
    reset(fixture)
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    bob = api().authenticate(fx.BOB["email"], fx.BOB["password"])
    assert_status(ada.post("/authorizations", json={"to_handle": "cy", "amount": 6000},
                           idempotency_key=new_key()), 201)
    assert_error(ada.post("/payments", json={"to_handle": "bob", "amount": 5000},
                          idempotency_key=new_key()), 409, "insufficient_funds")
    request = assert_status(bob.post("/requests", json={"payer_handle": "ada", "amount": 5000},
                                     idempotency_key=new_key()), 201).json()
    assert_error(ada.post(f"/requests/{request['request_id']}/pay", json={},
                          idempotency_key=new_key()), 409, "insufficient_funds")
    assert_error(ada.post("/settlements", json={"transfers": [
        {"from_handle": "ada", "to_handle": "cy", "amount": 5000}]},
        idempotency_key=new_key()), 409, "insufficient_funds")
    assert ada.get("/me").json()["balance"] == 10000


def test_final_partial_capture_moves_only_captured_amount_and_releases_remainder(world):
    authorization = assert_status(world.ada.post(
        "/authorizations", json={"to_handle": "bob", "amount": 2000},
        idempotency_key=new_key()), 201).json()
    aid = authorization["authorization_id"]
    payment = assert_status(world.bob.post(
        f"/authorizations/{aid}/capture", json={"amount": 1500},
        idempotency_key=new_key()), 201).json()
    assert payment["amount"] == 1500 and payment["authorization_id"] == aid
    assert payment["request_id"] is None
    assert world.ada.get("/me").json()["total"] == 8500
    assert world.ada.get("/me").json()["available"] == 8500
    assert world.ada.get("/me").json()["held"] == 0
    item = world.ada.get("/authorizations").json()["authorizations"][0]
    assert item["status"] == "captured" and item["captured_amount"] == 1500
    assert item["remaining_amount"] == 0 and item["payment_ids"] == [payment["payment_id"]]
    assert_error(world.bob.post(f"/authorizations/{aid}/capture", json={},
                                idempotency_key=new_key()), 409, "authorization_not_open")


def test_capture_above_remaining_uses_authorization_specific_error(world):
    authorization = assert_status(world.ada.post(
        "/authorizations", json={"to_handle": "bob", "amount": 2000},
        idempotency_key=new_key()), 201).json()
    aid = authorization["authorization_id"]
    assert_error(world.bob.post(
        f"/authorizations/{aid}/capture", json={"amount": 2000.5},
        idempotency_key=new_key()), 422, "validation_failed")
    assert_error(world.bob.post(
        f"/authorizations/{aid}/capture", json={"amount": 1_000_000_001},
        idempotency_key=new_key()), 422, "capture_exceeds_authorization")
    current = world.ada.get("/authorizations").json()["authorizations"][0]
    assert current["status"] == "open" and current["captured_amount"] == 0
    assert world.ada.get("/me").json()["held"] == 2000


def test_nonfinal_capture_keeps_only_remainder_held_until_final(world):
    authorization = assert_status(world.ada.post(
        "/authorizations", json={"to_handle": "bob", "amount": 2000},
        idempotency_key=new_key()), 201).json()
    aid = authorization["authorization_id"]
    first = assert_status(world.bob.post(
        f"/authorizations/{aid}/capture", json={"amount": 700, "final": False},
        idempotency_key="capture-part-one"), 201).json()
    assert first["amount"] == 700
    me = world.ada.get("/me").json()
    assert (me["total"], me["held"], me["available"]) == (9300, 1300, 8000)
    item = world.ada.get("/authorizations").json()["authorizations"][0]
    assert item["status"] == "open" and item["captured_amount"] == 700
    assert item["payment_id"] == first["payment_id"]
    assert item["payment_ids"] == [first["payment_id"]]
    replay = assert_status(world.bob.post(
        f"/authorizations/{aid}/capture", json={"final": False, "amount": 700},
        idempotency_key="capture-part-one"), 200).json()
    assert replay == first
    assert_error(world.bob.post(
        f"/authorizations/{aid}/capture", json={"amount": 700},
        idempotency_key="capture-part-one"), 409, "idempotency_key_reuse")
    second = assert_status(world.bob.post(
        f"/authorizations/{aid}/capture", json={"amount": 1300, "final": False},
        idempotency_key="capture-part-two"), 201).json()
    item = world.ada.get("/authorizations").json()["authorizations"][0]
    assert item["status"] == "captured" and item["captured_amount"] == 2000
    assert item["payment_id"] == second["payment_id"]
    assert item["payment_ids"] == [first["payment_id"], second["payment_id"]]
    assert world.ada.get("/me").json()["total"] == 8000


def test_import_requires_payment_id_to_match_latest_capture_and_null_when_empty(world):
    authorization = assert_status(world.ada.post(
        "/authorizations", json={"to_handle": "bob", "amount": 2000},
        idempotency_key=new_key()), 201).json()
    aid = authorization["authorization_id"]

    snapshot = assert_status(world.ada.get("/_test/export"), 200).json()
    raw = snapshot["state"]["authorizations"][0]
    raw["payment_id"] = "p_does_not_exist"
    assert_error(world.ada.post("/_test/import", json=snapshot), 422, "validation_failed")
    assert world.ada.get("/authorizations").json()["authorizations"][0]["payment_id"] is None

    first = assert_status(world.bob.post(
        f"/authorizations/{aid}/capture", json={"amount": 700, "final": False},
        idempotency_key=new_key()), 201).json()
    second = assert_status(world.bob.post(
        f"/authorizations/{aid}/capture", json={"amount": 500, "final": False},
        idempotency_key=new_key()), 201).json()
    assert first["payment_id"] != second["payment_id"]

    snapshot = assert_status(world.ada.get("/_test/export"), 200).json()
    raw = snapshot["state"]["authorizations"][0]
    assert raw["payment_ids"] == [first["payment_id"], second["payment_id"]]
    raw["payment_id"] = first["payment_id"]
    assert_error(world.ada.post("/_test/import", json=snapshot), 422, "validation_failed")
    current = world.ada.get("/authorizations").json()["authorizations"][0]
    assert current["payment_id"] == second["payment_id"]


def test_void_permissions_repeat_and_available_funds(world):
    authorization = assert_status(world.ada.post(
        "/authorizations", json={"to_handle": "bob", "amount": 2000},
        idempotency_key=new_key()), 201).json()
    aid = authorization["authorization_id"]
    assert_error(world.cy.post(f"/authorizations/{aid}/void", json={}), 403, "forbidden")
    voided = assert_status(world.ada.post(f"/authorizations/{aid}/void", json={}), 200).json()
    assert voided["status"] == "voided" and voided["remaining_amount"] == 0
    assert_status(world.ada.post(f"/authorizations/{aid}/void", json={}), 200)
    me = world.ada.get("/me").json()
    assert (me["total"], me["held"], me["available"]) == (10000, 0, 10000)


def test_expired_seeded_authorization_releases_funds_and_filters(reset, api):
    fixture = fx.fixture()
    fixture["authorizations"] = [{
        "id": "a_expired", "from_user_id": "u_ada", "to_user_id": "u_bob",
        "amount": 1200, "note": "old hold", "visibility": "private", "status": "open",
        "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}]
    reset(fixture)
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    assert ada.get("/me").json()["available"] == fx.ADA["balance"]
    rows = ada.get("/authorizations", params={"status": "expired"}).json()["authorizations"]
    assert len(rows) == 1 and rows[0]["status"] == "expired"
    assert ada.get("/authorizations", params={"status": "open"}).json()["authorizations"] == []


def test_reset_rejects_seeded_holds_above_balance_without_replacing_state(reset, api):
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    before = ada.get("/me").json()
    fixture = fx.fixture()
    fixture["authorizations"] = [{
        "id": "a_too_large", "from_user_id": "u_ada", "to_user_id": "u_bob",
        "amount": 10001, "status": "open",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()}]
    assert_error(reset(fixture, raw=True), 422, "validation_failed")
    assert ada.get("/me").json() == before


def test_authorization_expiry_void_and_capture_permissions(world):
    authorization = assert_status(world.ada.post(
        "/authorizations", json={"to_handle": "bob", "amount": 2000},
        idempotency_key=new_key()), 201).json()
    aid = authorization["authorization_id"]
    assert_error(world.ada.post(f"/authorizations/{aid}/capture", json={},
                                idempotency_key=new_key()), 403, "forbidden")
    assert_error(world.cy.post(f"/authorizations/{aid}/capture", json={},
                               idempotency_key=new_key()), 403, "forbidden")
    assert_error(world.bob.post(f"/authorizations/no-such-auth/capture", json={},
                                idempotency_key=new_key()), 404, "not_found")
    assert_error(world.bob.post(f"/authorizations/{aid}/capture", json={"amount": 2001},
                                idempotency_key=new_key()), 422, "capture_exceeds_authorization")
    assert_error(world.bob.post(f"/authorizations/{aid}/capture", json={"amount": 1, "final": 0},
                                idempotency_key=new_key()), 400, "malformed_request")


def test_import_accepts_stage1_state_shape_and_preserves_token(world):
    snapshot = assert_status(world.ada.get("/_test/export"), 200).json()
    legacy = copy.deepcopy(snapshot)
    legacy["state"].pop("authorization_ttl_seconds")
    legacy["state"].pop("authorizations")
    for payment in legacy["state"]["payments"]:
        payment.pop("authorization_id", None)
    assert_status(world.ada.post("/_test/import", json=legacy), 204)
    assert world.ada.get("/me").json()["user_id"] == "u_ada"
    assert world.ada.get("/me").json()["available"] == 10000


def test_concurrent_authorizations_do_not_reserve_more_than_available(world):
    def authorize(index):
        return world.ada.post("/authorizations", json={
            "to_handle": "bob", "amount": 6000,
        }, idempotency_key=f"hold-{index}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(authorize, range(2)))
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert world.ada.get("/me").json()["available"] == 4000
    assert world.ada.get("/me").json()["held"] == 6000


def test_simultaneous_same_key_authorization_is_one_hold(world):
    def authorize(_):
        return world.ada.post("/authorizations", json={"to_handle": "bob", "amount": 2000},
                              idempotency_key="same-hold-key")

    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = list(pool.map(authorize, range(12)))
    assert sorted(response.status_code for response in responses) == [200] * 11 + [201]
    assert world.ada.get("/me").json()["held"] == 2000
    assert len(world.ada.get("/authorizations").json()["authorizations"]) == 1


def test_expired_capture_is_refused_with_expired_code(reset, api):
    fixture = fx.fixture()
    fixture["authorizations"] = [{
        "id": "a_expired", "from_user_id": "u_ada", "to_user_id": "u_bob",
        "amount": 1200, "status": "open",
        "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}]
    reset(fixture)
    bob = api().authenticate(fx.BOB["email"], fx.BOB["password"])
    assert_error(bob.post("/authorizations/a_expired/capture", json={},
                          idempotency_key=new_key()), 409, "authorization_expired")

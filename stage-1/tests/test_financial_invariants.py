from concurrent.futures import ThreadPoolExecutor

import fixtures as fx
from harness.http import Api, assert_error, assert_status, new_key
import pytest


def test_concurrent_same_wallet_requests_do_not_double_spend(reset, api):
    reset(fx.fixture(users=[fx.user("ada", 1000), fx.user("bob", 0)]))
    ada = api().authenticate("ada@example.com", "correct horse")
    clients = [ada] * 20
    def send(i):
        return clients[i].post("/payments", json={"to_handle": "bob", "amount": 1000},
                               idempotency_key=new_key()).status_code
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(send, range(len(clients))))
    assert results.count(201) == 1
    assert results.count(409) == 19
    assert ada.get("/me").json()["balance"] == 0
    assert ada.get("/activity").json()["payments"][0]["amount"] == 1000


def test_idempotency_is_checked_before_validation_and_resource_state(world, ask):
    rid = ask(amount=100).json()["request_id"]
    key = new_key()
    first = world.ada.post(f"/requests/{rid}/pay", json={}, idempotency_key=key)
    assert_status(first, 201)
    replay = world.ada.post(f"/requests/{rid}/pay", json={"visibility": "bogus"},
                            idempotency_key=key)
    assert_error(replay, 409, "idempotency_key_reuse")


def test_concurrent_identical_idempotent_writes_commit_once(world):
    key = new_key()
    def send(_):
        return world.ada.post("/payments", json={"to_handle": "bob", "amount": 50},
                              idempotency_key=key)
    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = list(pool.map(send, range(12)))
    assert sorted(r.status_code for r in responses) == [200] * 11 + [201]
    assert all(r.json() == responses[0].json() for r in responses)
    assert world.ada.get("/me").json()["balance"] == 10000 - 50


def test_fifty_wallet_cycle_conserves_total_under_concurrent_transfers(reset, api):
    users = [fx.user(f"n{i}", 100) for i in range(50)]
    reset(fx.fixture(users=users))
    clients = [api().authenticate(user["email"], user["password"]) for user in users]
    def send(i):
        receiver = users[(i + 1) % len(users)]["handle"]
        return clients[i].post("/payments", json={"to_handle": receiver, "amount": 100},
                               idempotency_key=new_key())
    with ThreadPoolExecutor(max_workers=50) as pool:
        responses = list(pool.map(send, range(50)))
    assert all(response.status_code == 201 for response in responses)
    balances = [client.get("/me").json()["balance"] for client in clients]
    assert balances == [100] * 50
    assert sum(balances) == sum(user["balance"] for user in users)


def test_money_decimals_are_compared_exactly_and_never_raise_5xx(world):
    fractional = world.ada.post("/payments", content='{"to_handle":"bob","amount":1.000000000000000001}',
                                idempotency_key=new_key())
    assert_error(fractional, 422, "validation_failed")
    huge_exponent = world.ada.post("/payments", content='{"to_handle":"bob","amount":1e999999999999999999999999}',
                                   idempotency_key=new_key())
    assert_error(huge_exponent, 422, "validation_failed")
    ignored = world.ada.post("/payments", content='{"to_handle":"bob","amount":1,"future_field":1e999999999999999999999999}',
                             idempotency_key=new_key())
    assert_status(ignored, 201)


def test_export_import_preserves_token_receipt_and_idempotency(world, api):
    key = new_key()
    body = {"to_handle": "bob", "amount": 250, "note": "preserved 🧀"}
    receipt = assert_status(world.ada.post("/payments", json=body,
                                           idempotency_key=key), 201).json()
    snapshot = assert_status(world.ada.get("/_test/export"), 200).json()
    assert_status(world.ada.post("/_test/reset", json=fx.fixture(users=[fx.user("temp", 1)])), 204)
    assert_status(world.ada.post("/_test/import", json=snapshot), 204)
    assert world.ada.get("/me").json()["balance"] == 9750
    replay = assert_status(world.ada.post("/payments", json=body,
                                          idempotency_key=key), 200).json()
    assert replay == receipt
    assert world.ada.get("/activity").json()["payments"][0] == receipt
    assert_status(world.ada.get("/me"), 200)


def test_failed_import_is_atomic(world):
    before = world.ada.get("/me").json()
    response = world.ada.post("/_test/import", json={"track": "wrong", "format_version": 1,
                                                      "state": {}})
    assert_error(response, 422, "validation_failed")
    assert world.ada.get("/me").json() == before


def test_import_rejects_unbalanced_snapshot_without_replacing_state(world):
    before = world.ada.get("/me").json()
    snapshot = world.ada.get("/_test/export").json()
    snapshot["state"]["users"]["u_ada"]["balance"] += 1
    response = world.ada.post("/_test/import", json=snapshot)
    assert_error(response, 422, "validation_failed")
    assert world.ada.get("/me").json() == before


def test_import_rejects_boolean_version_and_malformed_idempotency_entry(world):
    before = world.ada.get("/me").json()
    snapshot = world.ada.get("/_test/export").json()
    snapshot["format_version"] = True
    assert_error(world.ada.post("/_test/import", json=snapshot), 422, "validation_failed")
    snapshot["format_version"] = 1
    snapshot["state"]["idempotency"]["u_ada\u0000bad\u0000POST\u0000/payments"] = {
        "method": "POST", "path": "/payments", "body": "{}", "response": []}
    assert_error(world.ada.post("/_test/import", json=snapshot), 422, "validation_failed")
    assert world.ada.get("/me").json() == before


@pytest.mark.parametrize("mutate", [
    lambda state: state["payments"][0].update(created_at="not-a-timestamp"),
    lambda state: state["requests"][0].update(created_at="not-a-timestamp"),
    lambda state: state["payments"][0].update(settlement_id=[]),
    lambda state: state["idempotency"].update({"u_ada\u0000key\u0000POST\u0000/payments": []}),
])
def test_import_rejects_malformed_stored_timestamps_and_links_atomically(world, mutate):
    # Supply records of each kind so every mutation targets a real persisted row.
    assert_status(world.ada.post("/payments", json={"to_handle": "bob", "amount": 5},
                                 idempotency_key=new_key()), 201)
    assert_status(world.bob.post("/requests", json={"payer_handle": "ada", "amount": 5},
                                 idempotency_key=new_key()), 201)
    snapshot = world.ada.get("/_test/export").json()
    before = world.ada.get("/me").json()
    assert snapshot["state"]["payments"] and snapshot["state"]["requests"]
    mutate(snapshot["state"])

    assert_error(world.ada.post("/_test/import", json=snapshot), 422, "validation_failed")
    assert world.ada.get("/me").json() == before


def test_zero_share_request_can_be_paid_and_survives_export_import(world):
    split = assert_status(world.ada.post("/splits", json={
        "amount": 1, "participant_handles": ["ada", "bob", "cy"]},
        idempotency_key=new_key()), 201).json()
    zero_request = next(r for r in split["requests"] if r["payer_handle"] == "bob")
    assert zero_request["amount"] == 0
    before = world.bob.get("/me").json()["balance"]
    payment = assert_status(world.bob.post(
        f"/requests/{zero_request['request_id']}/pay", json={},
        idempotency_key=new_key()), 201).json()
    assert payment["amount"] == 0
    assert world.bob.get("/me").json()["balance"] == before
    snapshot = assert_status(world.ada.get("/_test/export"), 200).json()
    assert_status(world.ada.post("/_test/reset", json=world.fixture), 204)
    assert_status(world.ada.post("/_test/import", json=snapshot), 204)
    paid = next(r for r in world.bob.get("/requests").json()["requests"]
                if r["request_id"] == zero_request["request_id"])
    assert paid["status"] == "paid" and paid["payment_id"] == payment["payment_id"]


def test_net_settlement_is_atomic_and_idempotent(reset, api):
    fixture = fx.fixture()
    fixture["settlement_operator_ids"] = ["u_ada"]
    reset(fixture)
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    key = new_key()
    transfers = {"transfers": [
        {"from_handle": "ada", "to_handle": "bob", "amount": 500},
        {"from_handle": "bob", "to_handle": "ada", "amount": 500},
    ]}
    first = assert_status(ada.post("/settlements", json=transfers,
                                   idempotency_key=key), 201)
    assert first.json()["payments"][0]["created_at"] == first.json()["committed_at"]
    assert first.json()["payments"][1]["created_at"] == first.json()["committed_at"]
    replay = assert_status(ada.post("/settlements", json=transfers,
                                    idempotency_key=key), 200)
    assert replay.json() == first.json()
    assert ada.get("/me").json()["balance"] == 10000


def test_net_settlement_rejects_insufficient_group_without_partial_commit(reset, api):
    fixture = fx.fixture(users=[fx.user("ada", 10), fx.user("bob", 0), fx.user("cy", 0)])
    fixture["settlement_operator_ids"] = ["u_ada"]
    reset(fixture)
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    before = ada.get("/me").json()["balance"]
    response = ada.post("/settlements", json={"transfers": [
        {"from_handle": "ada", "to_handle": "bob", "amount": 9},
        {"from_handle": "ada", "to_handle": "cy", "amount": 9},
    ]}, idempotency_key=new_key())
    assert_error(response, 409, "insufficient_funds")
    assert ada.get("/me").json()["balance"] == before
    assert ada.get("/activity").json()["payments"] == []

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest

import fixtures as fx
from harness.http import assert_error, assert_status, new_key

pytestmark = pytest.mark.stage(3)


def instant(year=2020, month=1, day=1, second=0):
    return datetime(year, month, day, tzinfo=timezone.utc).replace(
        second=second).isoformat()


def correct(client, payment_id, expected, amount, effective_at, key=None, reason="fix"):
    return client.post(f"/payments/{payment_id}/corrections", json={
        "expected_revision": expected, "amount": amount,
        "effective_at": effective_at, "reason": reason},
        idempotency_key=key or new_key())


def test_as_of_includes_boundary_and_uses_seeded_opening(reset, api):
    timestamp = instant()
    fixture = fx.fixture(payments=[{
        "id": "p_seed", "from_user_id": "u_ada", "to_user_id": "u_bob",
        "amount": 300, "created_at": timestamp}])
    reset(fixture)
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    before = ada.get("/me", params={"as_of": instant(2019, 12, 31)}).json()
    exact = ada.get("/me", params={"as_of": timestamp}).json()
    assert before["balance"] == fx.ADA["balance"] + 300
    assert exact["balance"] == fx.ADA["balance"]
    assert exact["as_of"] == timestamp


def test_seeded_future_payment_reset_is_rejected_without_state_change(reset, api):
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    before = ada.get("/me").json()
    fixture = fx.fixture(payments=[{
        "id": "p_future", "from_user_id": "u_ada", "to_user_id": "u_bob",
        "amount": 100,
        "created_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}])
    assert_error(ada.post("/_test/reset", json=fixture), 422, "validation_failed")
    assert ada.get("/me").json() == before


def test_statement_window_pagination_balance_and_snapshot_stability(world, pay):
    start = instant(2019, 12, 31)
    end = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    first = assert_status(pay(amount=100), 201).json()
    second = assert_status(pay(amount=200), 201).json()
    page1 = assert_status(world.ada.get("/statement", params={
        "from": start, "to": end, "limit": 1}), 200).json()
    assert len(page1["entries"]) == 1 and page1["has_more"]
    assert page1["entries"][0]["payment"]["payment_id"] == first["payment_id"]
    assert correct(world.ada, first["payment_id"], 1, 80,
                   first["created_at"]).status_code == 201
    assert_status(pay(amount=50), 201)
    page2 = assert_status(world.ada.get("/statement", params={
        "snapshot": page1["snapshot"], "limit": 1, "offset": 1}), 200).json()
    assert [page1["entries"][0]["payment"]["payment_id"],
            page2["entries"][0]["payment"]["payment_id"]] == [
                first["payment_id"], second["payment_id"]]
    assert page2["opening_balance"] == page1["opening_balance"]
    assert page2["closing_balance"] == page1["closing_balance"]
    assert page2["entries"][0]["balance_after"] == page1["opening_balance"] - 300
    beyond = world.ada.get("/statement", params={
        "snapshot": page1["snapshot"], "limit": 1, "offset": 20}).json()
    assert beyond["entries"] == [] and not beyond["has_more"]
    assert_error(world.ada.get("/statement", params={
        "snapshot": page1["snapshot"], "from": start}), 422, "validation_failed")


def test_statement_filters_private_visibility_not_parties(world, pay):
    payment = assert_status(pay(client=world.cy, to_handle="bob", amount=10,
                                visibility="private"), 201).json()
    statement = world.ada.get("/statement").json()
    assert payment["payment_id"] not in [
        entry["payment"]["payment_id"] for entry in statement["entries"]]
    assert payment["payment_id"] in [
        entry["payment"]["payment_id"] for entry in world.cy.get("/statement").json()["entries"]]


def test_known_at_selects_revisions_and_statement_echoes_exact_value(world, pay):
    payment = assert_status(pay(amount=100), 201).json()
    corrected = assert_status(correct(world.ada, payment["payment_id"], 1, 70,
                                      payment["created_at"]), 201).json()
    original_view = world.ada.get("/me", params={
        "as_of": payment["created_at"], "known_at": payment["created_at"]}).json()
    corrected_view = world.ada.get("/me", params={
        "as_of": payment["created_at"], "known_at": corrected["recorded_at"]}).json()
    assert original_view["balance"] == fx.ADA["balance"] - 100
    assert corrected_view["balance"] == fx.ADA["balance"] - 70
    exact_known = corrected["recorded_at"]
    statement = world.ada.get("/statement", params={"known_at": exact_known}).json()
    assert statement["known_at"] == exact_known
    assert statement["entries"][-1]["payment"]["amount"] == 70


@pytest.mark.parametrize("param", ["as_of", "known_at"])
@pytest.mark.parametrize("value", ["", "2026-09-24", "2026-09-24T13:20:00"])
def test_historical_query_requires_rfc3339_offset(world, param, value):
    assert_error(world.ada.get("/me", params={param: value}), 422, "validation_failed")


def test_correction_replay_stale_revision_and_immutable_original(world, pay):
    payment = assert_status(pay(amount=100), 201).json()
    key = new_key()
    first = correct(world.ada, payment["payment_id"], 1, 80,
                    payment["created_at"], key=key)
    assert first.status_code == 201
    second = correct(world.ada, payment["payment_id"], 2, 60,
                     payment["created_at"])
    assert second.status_code == 201
    replay = correct(world.ada, payment["payment_id"], 1, 80,
                     payment["created_at"], key=key)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert_error(correct(world.ada, payment["payment_id"], 1, 70,
                         payment["created_at"], key=key), 409, "idempotency_key_reuse")
    assert_error(correct(world.ada, payment["payment_id"], 1, 50,
                         payment["created_at"]), 409, "stale_revision")
    activity = world.ada.get("/activity").json()["payments"]
    original = next(p for p in activity if p["payment_id"] == payment["payment_id"])
    assert original["amount"] == 100
    revisions = world.ada.get(f"/payments/{payment['payment_id']}/revisions").json()["revisions"]
    assert [revision["revision"] for revision in revisions] == [1, 2, 3]
    assert revisions[0]["effective_at"] == revisions[0]["recorded_at"] == payment["created_at"]


def test_revision_history_is_private_and_missing_payment_is_hidden(world, pay):
    payment = assert_status(pay(amount=10), 201).json()
    assert_error(world.cy.get(f"/payments/{payment['payment_id']}/revisions"),
                 404, "not_found")
    assert_error(world.ada.get("/payments/unknown/revisions"), 404, "not_found")
    assert world.ada.get(f"/payments/{payment['payment_id']}/revisions",
                         params=None).status_code == 200


def test_historical_overdraft_rolls_back_everything(reset, api):
    earlier, later = instant(), instant(2020, 1, 1, 1)
    fixture = fx.fixture(users=[fx.user("ada", 100), fx.user("bob", 100)],
        payments=[
            {"id": "p_out", "from_user_id": "u_ada", "to_user_id": "u_bob",
             "amount": 100, "created_at": earlier},
            {"id": "p_back", "from_user_id": "u_bob", "to_user_id": "u_ada",
             "amount": 100, "created_at": later}])
    reset(fixture)
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    before = ada.get("/_test/export").json()
    assert_error(correct(ada, "p_out", 1, 150, earlier), 409, "historical_overdraft")
    after = ada.get("/_test/export").json()
    assert after == before
    assert ada.get("/me").json()["balance"] == 100


def test_same_instant_movements_are_checked_as_a_combined_boundary(reset, api):
    timestamp = instant()
    reset(fx.fixture(users=[fx.user("ada"), fx.user("bob")], payments=[
        {"id": "p_1", "from_user_id": "u_ada", "to_user_id": "u_bob",
         "amount": 50, "created_at": timestamp},
        {"id": "p_2", "from_user_id": "u_bob", "to_user_id": "u_ada",
         "amount": 50, "created_at": timestamp}]))
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    assert ada.get("/me", params={"as_of": timestamp}).json()["balance"] == 0


def test_authorization_capture_and_expiry_are_historical_events(world):
    auth = assert_status(world.ada.post("/authorizations", json={
        "to_handle": "bob", "amount": 1000}, idempotency_key=new_key()), 201).json()
    assert auth["closed_at"] is None
    created_view = world.ada.get("/me", params={
        "as_of": auth["created_at"], "known_at": auth["created_at"]}).json()
    assert (created_view["total"], created_view["held"], created_view["available"]) == (
        fx.ADA["balance"], 1000, fx.ADA["balance"] - 1000)
    capture = assert_status(world.bob.post(
        f"/authorizations/{auth['authorization_id']}/capture",
        json={"amount": 200, "final": False}, idempotency_key=new_key()), 201).json()
    captured = world.ada.get("/me", params={
        "as_of": capture["created_at"], "known_at": capture["created_at"]}).json()
    assert (captured["total"], captured["held"], captured["available"]) == (
        fx.ADA["balance"] - 200, 800, fx.ADA["balance"] - 1000)
    voided = assert_status(world.ada.post(
        f"/authorizations/{auth['authorization_id']}/void", json={}), 200).json()
    after_void = world.ada.get("/me", params={
        "as_of": voided["closed_at"], "known_at": voided["closed_at"]}).json()
    assert after_void["held"] == 0


def test_settlement_and_capture_members_cannot_be_corrected(world, reset, api):
    fixture = fx.fixture()
    fixture["settlement_operator_ids"] = ["u_ada"]
    reset(fixture)
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    settlement = assert_status(ada.post("/settlements", json={"transfers": [{
        "from_handle": "ada", "to_handle": "bob", "amount": 100}]},
        idempotency_key=new_key()), 201).json()
    member = settlement["payments"][0]
    assert_error(correct(ada, member["payment_id"], 1, 50,
                         member["created_at"]), 422, "linked_payment_immutable")


def test_stage3_export_import_preserves_revisions_and_snapshot(world, pay):
    payment = assert_status(pay(amount=100), 201).json()
    correct(world.ada, payment["payment_id"], 1, 75, payment["created_at"])
    snapshot = world.ada.get("/statement").json()
    exported = world.ada.get("/_test/export").json()
    assert_status(world.ada.post("/_test/import", json=copy.deepcopy(exported)), 204)
    revisions = world.ada.get(f"/payments/{payment['payment_id']}/revisions").json()["revisions"]
    assert revisions[-1]["amount"] == 75
    stable_page = world.ada.get("/statement", params={
        "snapshot": snapshot["snapshot"], "offset": 0, "limit": 1}).json()
    assert stable_page["closing_balance"] == snapshot["closing_balance"]


def test_correction_rejected_when_past_available_would_be_negative(reset, api):
    reset(fx.fixture(users=[fx.user("ada", 1000), fx.user("bob", 1000)]))
    ada = api().authenticate("ada@example.com", "correct horse")
    bob = api().authenticate("bob@example.com", "correct horse")
    payment = assert_status(ada.post("/payments", json={"to_handle": "bob", "amount": 100},
                                     idempotency_key=new_key()), 201).json()
    auth = assert_status(ada.post("/authorizations", json={"to_handle": "bob", "amount": 900},
                                  idempotency_key=new_key()), 201).json()
    assert_status(bob.post("/payments", json={"to_handle": "ada", "amount": 500},
                           idempotency_key=new_key()), 201)
    assert_status(ada.post(f"/authorizations/{auth['authorization_id']}/void", json={}), 200)
    before = ada.get("/_test/export").json()
    # Totals stay nonnegative, but while the hold was open ada's available would be -200.
    assert_error(correct(ada, payment["payment_id"], 1, 300, payment["created_at"]),
                 409, "historical_overdraft")
    assert ada.get("/_test/export").json() == before


def test_stage2_export_with_voided_hold_imports(reset, api):
    reset(fx.fixture(users=[fx.user("ada", 1000), fx.user("bob", 0)]))
    ada = api().authenticate("ada@example.com", "correct horse")
    auth = assert_status(ada.post("/authorizations", json={"to_handle": "bob", "amount": 1000},
                                  idempotency_key=new_key()), 201).json()
    assert_status(ada.post(f"/authorizations/{auth['authorization_id']}/void", json={}), 200)
    assert_status(ada.post("/payments", json={"to_handle": "bob", "amount": 1000},
                           idempotency_key=new_key()), 201)
    document = ada.get("/_test/export").json()
    # Reduce the document to the shape a stage-2 service exports.
    state = document["state"]
    del state["opening_balances"]
    for payment in state["payments"]:
        del payment["revisions"]
    for authorization in state["authorizations"]:
        del authorization["closed_at"]
        del authorization["capture_events"]
    assert_status(ada.post("/_test/import", json=document), 204)
    me = assert_status(ada.get("/me"), 200).json()
    assert (me["balance"], me["held"], me["available"]) == (0, 0, 0)
    imported = ada.get("/authorizations").json()["authorizations"][0]
    assert imported["status"] == "voided" and imported["closed_at"] is not None


@pytest.mark.parametrize("closed", [
    {"status": "expired"},
    {"status": "voided", "closed_at": "2020-01-01T00:00:00+00:00"}])
def test_seeded_closed_hold_without_created_at_stays_released(reset, api, closed):
    hold = {"id": "a_old", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 9000,
            "expires_at": "2020-01-01T00:00:00+00:00", **closed}
    fixture = fx.fixture()
    fixture["authorizations"] = [hold]
    reset(fixture)
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    later = ada.get("/me", params={"as_of": tomorrow}).json()
    assert (later["held"], later["available"]) == (0, fx.ADA["balance"])
    assert_status(ada.post("/payments", json={"to_handle": "bob", "amount": 5000},
                           idempotency_key=new_key()), 201)
    payment = assert_status(ada.post("/payments", json={"to_handle": "cy", "amount": 10},
                                     idempotency_key=new_key()), 201).json()
    assert_status(correct(ada, payment["payment_id"], 1, 5, payment["created_at"]), 201)


def test_seeded_closed_hold_larger_than_balance_resets(reset):
    fixture = fx.fixture(users=[fx.user("ada", 5000), fx.user("bob")])
    fixture["authorizations"] = [{
        "id": "a_old", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 9000,
        "status": "expired", "expires_at": "2020-01-01T00:00:00+00:00"}]
    assert_status(reset(fixture, raw=True), 204)


def test_concurrent_corrections_from_one_revision_only_commit_once(world, pay):
    payment = assert_status(pay(amount=100), 201).json()
    def revise(amount):
        return correct(world.ada, payment["payment_id"], 1, amount,
                       payment["created_at"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(revise, (80, 70)))
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert len(world.ada.get(
        f"/payments/{payment['payment_id']}/revisions").json()["revisions"]) == 2

"""Requests, splits, the activity feed and idempotency (§7, §8)."""
import pytest

import fixtures as fx
from harness.http import assert_error, assert_status, new_key

pytestmark = pytest.mark.stage(1)


# ---- requests ------------------------------------------------------------


def test_request_above_the_payers_balance_is_created_normally(world, ask, balance):
    """The payer's balance is not checked at creation time."""
    over = balance(world.ada) + 10_000
    assert assert_status(ask(amount=over), 201).json()["status"] == "pending"


def test_requesting_from_yourself_is_rejected(world, ask):
    assert_error(ask(world.bob, payer_handle="bob"), 422, "self_request")


def test_request_unknown_handle_is_404(world, ask):
    assert_error(ask(payer_handle="nobody"), 404, "not_found")


def test_pay_defaults_to_public_and_honours_private(world, ask):
    rq1 = ask(amount=100).json()
    assert world.ada.post(f"/requests/{rq1['request_id']}/pay", json={},
                          idempotency_key=new_key()).json()["visibility"] == "public"
    rq2 = ask(amount=100).json()
    body = world.ada.post(f"/requests/{rq2['request_id']}/pay",
                          json={"visibility": "private"},
                          idempotency_key=new_key()).json()
    assert body["visibility"] == "private"


def test_paying_without_funds_changes_nothing(world, ask, balance, conservation):
    rq = assert_status(ask(world.ada, payer_handle="cy",
                           amount=balance(world.cy) + 1), 201).json()
    before = balance(world.cy)
    assert_error(world.cy.post(f"/requests/{rq['request_id']}/pay", json={},
                               idempotency_key=new_key()), 409, "insufficient_funds")
    assert balance(world.cy) == before
    listed = world.cy.get("/requests").json()["requests"]
    assert next(r for r in listed
                if r["request_id"] == rq["request_id"])["status"] == "pending"
    conservation(world)


def test_only_the_payer_may_pay(world, ask):
    rq = ask(amount=100).json()
    assert_error(world.cy.post(f"/requests/{rq['request_id']}/pay", json={},
                               idempotency_key=new_key()), 403, "forbidden")


def test_paying_a_non_pending_request_is_409(world, ask):
    rq = ask(amount=100).json()
    assert_status(world.ada.post(f"/requests/{rq['request_id']}/decline"), 200)
    assert_error(world.ada.post(f"/requests/{rq['request_id']}/pay", json={},
                                idempotency_key=new_key()), 409, "request_not_pending")


def test_unknown_request_is_404(world):
    assert_error(world.ada.post("/requests/rq_nope/pay", json={},
                                idempotency_key=new_key()), 404, "not_found")


def test_decline_needs_the_payer_and_cancel_needs_the_requester(world, ask):
    rq = ask(amount=100).json()["request_id"]
    assert_error(world.bob.post(f"/requests/{rq}/decline"), 403, "forbidden")
    assert_error(world.ada.post(f"/requests/{rq}/cancel"), 403, "forbidden")


def test_cancel_a_paid_request_is_409(world, ask):
    rq = ask(amount=100).json()["request_id"]
    assert_status(world.ada.post(f"/requests/{rq}/pay", json={},
                                 idempotency_key=new_key()), 201)
    assert_error(world.bob.post(f"/requests/{rq}/cancel"), 409, "request_not_pending")


# ---- listing -------------------------------------------------------------

def test_requests_are_scoped_to_the_two_parties(world, ask):
    ask(world.bob, payer_handle="ada", amount=100)
    assert world.cy.get("/requests").json()["requests"] == []
    assert len(world.ada.get("/requests").json()["requests"]) == 1
    assert len(world.bob.get("/requests").json()["requests"]) == 1


def test_direction_and_status_filters(world, ask):
    rq = ask(world.bob, payer_handle="ada", amount=100).json()["request_id"]
    ask(world.ada, payer_handle="bob", amount=200)
    incoming = world.ada.get("/requests", params={"direction": "incoming"}).json()
    assert all(r["payer_handle"] == "ada" for r in incoming["requests"])
    outgoing = world.ada.get("/requests", params={"direction": "outgoing"}).json()
    assert all(r["requester_handle"] == "ada" for r in outgoing["requests"])

    assert_status(world.ada.post(f"/requests/{rq}/decline"), 200)
    declined = world.ada.get("/requests", params={"status": "declined"}).json()
    assert [r["request_id"] for r in declined["requests"]] == [rq]


@pytest.mark.parametrize("params", [
    {"direction": "both"}, {"status": "open"}, {"limit": 0},
    {"limit": 201}, {"limit": -5}, {"offset": -1}, {"offset": "abc"},
])
def test_bad_list_parameters_are_422(world, params):
    assert_error(world.ada.get("/requests", params=params), 422, "validation_failed")


def test_has_more_and_paging(world, ask):
    for i in range(3):
        assert_status(ask(amount=100 + i), 201)
    page = world.ada.get("/requests", params={"limit": 2, "offset": 0}).json()
    assert len(page["requests"]) == 2 and page["has_more"] is True
    page = world.ada.get("/requests", params={"limit": 2, "offset": 2}).json()
    assert len(page["requests"]) == 1 and page["has_more"] is False


# ---- splits --------------------------------------------------------------


def test_remainder_follows_handle_order(world):
    """The same amount among the same people in a different order moves the extra unit."""
    first = world.ada.post("/splits", json={
        "amount": 1000, "participant_handles": ["ada", "bob", "cy"]},
        idempotency_key=new_key()).json()
    second = world.ada.post("/splits", json={
        "amount": 1000, "participant_handles": ["cy", "bob", "ada"]},
        idempotency_key=new_key()).json()
    assert first["shares"][0] == {"handle": "ada", "amount": 334}
    assert second["shares"][0] == {"handle": "cy", "amount": 334}


def test_split_with_only_the_caller_creates_no_requests(world):
    body = assert_status(world.ada.post("/splits", json={
        "amount": 999, "participant_handles": ["ada"]},
        idempotency_key=new_key()), 201).json()
    assert body["requests"] == []
    assert body["shares"] == [{"handle": "ada", "amount": 999}]


def test_a_zero_share_still_produces_a_request(world):
    """1 split three ways gives 1, 0, 0 -- and a 0 share is legal."""
    body = world.ada.post("/splits", json={
        "amount": 1, "participant_handles": ["ada", "bob", "cy"]},
        idempotency_key=new_key()).json()
    assert [s["amount"] for s in body["shares"]] == [1, 0, 0]
    assert len(body["requests"]) == 2


def test_split_checks_nobody_balance(world):
    """Nothing about a split checks anyone's balance."""
    assert_status(world.cy.post("/splits", json={
        "amount": 1_000_000, "participant_handles": ["cy", "ada"]},
        idempotency_key=new_key()), 201)


@pytest.mark.parametrize("handles,status,code", [
    ([], 422, "validation_failed"),
    (["ada", "ada"], 422, "validation_failed"),
    (["ada", "nobody"], 404, "not_found"),
])
def test_split_participant_validation(world, handles, status, code):
    assert_error(world.ada.post("/splits", json={
        "amount": 100, "participant_handles": handles},
        idempotency_key=new_key()), status, code)


def test_split_with_a_thousand_participants_is_validated_not_crashed(world):
    resp = world.ada.post("/splits", json={
        "amount": 100, "participant_handles": [f"h{i}" for i in range(1000)]},
        idempotency_key=new_key())
    assert resp.status_code in (404, 422), f"got {resp.status_code}"


# ---- feed ----------------------------------------------------------------


def test_requests_never_appear_in_the_feed(world, ask):
    assert_status(ask(amount=100), 201)
    assert world.ada.get("/activity").json()["payments"] == []
    assert world.bob.get("/activity").json()["payments"] == []


def test_feed_is_newest_first_and_pages(world, pay):
    for i in range(3):
        assert_status(pay(amount=10 + i), 201)
    page = world.ada.get("/activity", params={"limit": 2, "offset": 0}).json()
    assert len(page["payments"]) == 2 and page["has_more"] is True
    times = [p["created_at"] for p in world.ada.get("/activity").json()["payments"]]
    assert times == sorted(times, reverse=True)


def test_seeded_payments_respect_the_feed_contract(reset, api):
    """A seeded private payment is hidden from a third party too."""
    reset(fx.fixture(payments=[
        {"id": "p_1", "from_user_id": "u_ada", "to_user_id": "u_bob",
         "amount": 500, "note": "coffee", "visibility": "private"}]))
    cy = api().authenticate(fx.CY["email"], fx.CY["password"])
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    assert cy.get("/activity").json()["payments"] == []
    assert len(ada.get("/activity").json()["payments"]) == 1


# ---- idempotency ---------------------------------------------------------

def test_four_write_paths_require_a_key(world, ask):
    rq = ask(amount=100).json()["request_id"]
    for path, body in (("/payments", {"to_handle": "bob", "amount": 10}),
                       ("/requests", {"payer_handle": "bob", "amount": 10}),
                       (f"/requests/{rq}/pay", {}),
                       ("/splits", {"amount": 10, "participant_handles": ["ada", "bob"]})):
        assert_error(world.ada.post(path, json=body), 400, "missing_idempotency_key")


def test_decline_and_cancel_need_no_key(world, ask):
    rq = ask(amount=100).json()["request_id"]
    assert_status(world.ada.post(f"/requests/{rq}/decline"), 200)
    rq2 = ask(amount=100).json()["request_id"]
    assert_status(world.bob.post(f"/requests/{rq2}/cancel"), 200)


def test_the_key_outranks_the_status_on_pay(world, ask):
    """Replaying a successful pay returns the original 201 body with 200, not 409."""
    rq = ask(amount=100).json()["request_id"]
    key = new_key()
    first = assert_status(world.ada.post(f"/requests/{rq}/pay", json={},
                                         idempotency_key=key), 201).json()
    replay = assert_status(world.ada.post(f"/requests/{rq}/pay", json={},
                                          idempotency_key=key), 200).json()
    assert replay == first, "an implementation that checks status first fails here"


def test_empty_body_and_explicit_public_are_different_bodies(world, ask):
    """`{}` and `{"visibility": "public"}` are different JSON values."""
    rq = ask(amount=100).json()["request_id"]
    key = new_key()
    assert_status(world.ada.post(f"/requests/{rq}/pay", json={},
                                 idempotency_key=key), 201)
    assert_error(world.ada.post(f"/requests/{rq}/pay", json={"visibility": "public"},
                                idempotency_key=key), 409, "idempotency_key_reuse")


def test_same_key_on_a_different_path_is_not_a_replay(world):
    key = "shared-across-paths"
    assert_status(world.ada.post("/payments", json={"to_handle": "bob", "amount": 10},
                                 idempotency_key=key), 201)
    assert_status(world.ada.post("/requests", json={"payer_handle": "bob", "amount": 10},
                                 idempotency_key=key), 201)


def test_keys_are_scoped_per_user(world):
    key = "same-string"
    assert_status(world.ada.post("/payments", json={"to_handle": "bob", "amount": 10},
                                 idempotency_key=key), 201)
    assert_status(world.bob.post("/payments", json={"to_handle": "cy", "amount": 10},
                                 idempotency_key=key), 201)


def test_key_reused_after_insufficient_funds_is_a_first_use(world, pay, balance):
    key = new_key()
    assert_error(pay(world.cy, amount=balance(world.cy) + 1, key=key),
                 409, "insufficient_funds")
    assert_status(pay(world.cy, amount=10, key=key), 201)


def test_ten_kilobyte_key_is_422(world, pay):
    assert_error(pay(amount=10, key="k" * 10_000), 422, "validation_failed")




"""Stage 1 — idempotent replay (§7), split rounding (§9), visibility (§4), validation (§5)."""
import pytest

import fixtures as fx
from harness.http import assert_error, assert_status, new_key

pytestmark = pytest.mark.stage(1)


# ---- retries -------------------------------------------------------------


def test_changed_amount_on_the_same_key_is_reuse(world):
    key = new_key()
    assert_status(world.ada.post("/payments", json={"to_handle": "bob", "amount": 100},
                                 idempotency_key=key), 201)
    assert_error(world.ada.post("/payments", json={"to_handle": "bob", "amount": 200},
                                idempotency_key=key), 409, "idempotency_key_reuse")


def test_changed_visibility_on_pay_is_reuse(world, ask):
    rq = ask(amount=100).json()["request_id"]
    key = new_key()
    assert_status(world.ada.post(f"/requests/{rq}/pay", json={"visibility": "public"},
                                 idempotency_key=key), 201)
    assert_error(world.ada.post(f"/requests/{rq}/pay", json={"visibility": "private"},
                                idempotency_key=key), 409, "idempotency_key_reuse")


def test_key_reused_after_insufficient_funds_succeeds_once_funded(
        world, ask, balance, conservation):
    """Pay while short, receive money, retry the same key: one payment, not two."""
    short = balance(world.cy) + 1000
    rq = assert_status(ask(world.ada, payer_handle="cy", amount=short),
                       201).json()["request_id"]
    key = new_key()
    assert_error(world.cy.post(f"/requests/{rq}/pay", json={}, idempotency_key=key),
                 409, "insufficient_funds")
    assert_status(world.ada.post("/payments", json={"to_handle": "cy", "amount": 1000},
                                 idempotency_key=new_key()), 201)
    assert_status(world.cy.post(f"/requests/{rq}/pay", json={}, idempotency_key=key), 201)
    paid = [p for p in world.cy.get("/activity", params={"limit": 200}).json()["payments"]
            if p["request_id"] == rq]
    assert len(paid) == 1, "exactly one payment for that request"
    conservation(world)


# ---- splits and rounding -------------------------------------------------


def test_minor_units_zero_service(reset, api):
    """10 split 3 ways in a JPY service is 4, 3, 3 -- the rule is unit-agnostic."""
    reset(fx.fixture(currency="JPY"))
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    assert ada.get("/me").json()["minor_units"] == 0
    body = ada.post("/splits", json={
        "amount": 10, "participant_handles": ["ada", "bob", "cy"]},
        idempotency_key=new_key()).json()
    assert [s["amount"] for s in body["shares"]] == [4, 3, 3]


def test_minor_units_three_service(reset, api):
    reset(fx.fixture(currency="BHD"))
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    assert ada.get("/me").json()["minor_units"] == 3
    body = ada.post("/splits", json={
        "amount": 1000, "participant_handles": ["ada", "bob", "cy"]},
        idempotency_key=new_key()).json()
    assert [s["amount"] for s in body["shares"]] == [334, 333, 333]


# ---- visibility ----------------------------------------------------------


def test_public_payment_between_two_others_is_visible(world):
    assert_status(world.bob.post("/payments", json={
        "to_handle": "cy", "amount": 10, "visibility": "public"},
        idempotency_key=new_key()), 201)
    assert len(world.ada.get("/activity").json()["payments"]) == 1


@pytest.mark.parametrize("params", [
    {}, {"direction": "incoming"}, {"direction": "outgoing"},
    {"status": "pending"}, {"status": "paid"}, {"limit": 200},
])
def test_requests_never_leak_to_a_third_party_under_any_filter(world, ask, params):
    assert_status(ask(world.bob, payer_handle="ada", amount=100), 201)
    assert world.cy.get("/requests", params=params).json()["requests"] == []


# ---- awkward input -------------------------------------------------------


@pytest.mark.parametrize("amount", [0, -1, "1000", 1.5, 10_000_000_001])
def test_amount_out_of_range_never_500(world, amount):
    resp = world.ada.post("/payments", json={"to_handle": "bob", "amount": amount},
                          idempotency_key=new_key())
    assert resp.status_code == 422, f"expected 422, got {resp.status_code}"
    assert resp.json()["error"]["code"] == "validation_failed"


@pytest.mark.parametrize("handle", ["", "ADA", "@ada", "nobody"])
def test_bad_to_handle_never_500(world, handle):
    resp = world.ada.post("/payments", json={"to_handle": handle, "amount": 10},
                          idempotency_key=new_key())
    assert resp.status_code in (404, 422), f"got {resp.status_code}"


def test_paying_your_own_handle_is_self_payment(world):
    assert_error(world.ada.post("/payments", json={"to_handle": "ada", "amount": 10},
                                idempotency_key=new_key()), 422, "self_payment")


@pytest.mark.parametrize("note", [None, "x" * 201, "\U0001F600" * 201])
def test_bad_notes_are_422(world, note):
    assert_error(world.ada.post("/payments",
                                json={"to_handle": "bob", "amount": 10, "note": note},
                                idempotency_key=new_key()), 422, "validation_failed")


def test_two_hundred_emoji_note_is_accepted(world):
    assert_status(world.ada.post(
        "/payments",
        json={"to_handle": "bob", "amount": 10, "note": "\U0001F600" * 200},
        idempotency_key=new_key()), 201)


@pytest.mark.parametrize("visibility", ["Public", "", None])
def test_bad_visibility_is_422(world, visibility):
    assert_error(world.ada.post(
        "/payments", json={"to_handle": "bob", "amount": 10, "visibility": visibility},
        idempotency_key=new_key()), 422, "validation_failed")


def test_empty_body_replayed_against_a_visibility_key_is_reuse(world, ask):
    rq = ask(amount=100).json()["request_id"]
    key = new_key()
    assert_status(world.ada.post(f"/requests/{rq}/pay", json={"visibility": "public"},
                                 idempotency_key=key), 201)
    assert_error(world.ada.post(f"/requests/{rq}/pay", json={}, idempotency_key=key),
                 409, "idempotency_key_reuse")


@pytest.mark.parametrize("handles", [[], ["ada", "ada"], [f"h{i}" for i in range(1000)]])
def test_bad_participant_lists_never_500(world, handles):
    resp = world.ada.post("/splits", json={"amount": 100,
                                           "participant_handles": handles},
                          idempotency_key=new_key())
    assert resp.status_code in (404, 422), f"got {resp.status_code}"


def test_ten_kilobyte_key_is_422(world):
    assert_error(world.ada.post("/payments", json={"to_handle": "bob", "amount": 10},
                                idempotency_key="k" * 10_000), 422, "validation_failed")


def test_a_request_belonging_to_two_other_people_is_not_actionable(world, ask):
    rq = assert_status(ask(world.bob, payer_handle="cy", amount=10),
                       201).json()["request_id"]
    for call in ("pay", "decline", "cancel"):
        body = {"json": {}, "idempotency_key": new_key()} if call == "pay" else {}
        resp = world.ada.post(f"/requests/{rq}/{call}", **body)
        assert resp.status_code in (403, 404), f"{call} gave {resp.status_code}"


@pytest.mark.parametrize("params", [
    {"direction": "both"}, {"status": "open"}, {"limit": 0}, {"limit": 201},
    {"limit": -5}, {"offset": -1}, {"offset": "abc"},
])
def test_bad_request_list_parameters_are_422(world, params):
    assert_error(world.ada.get("/requests", params=params), 422, "validation_failed")


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 201}, {"limit": -5}, {"offset": -1}, {"offset": "abc"},
])
def test_bad_activity_paging_parameters_are_422(world, params):
    assert_error(world.ada.get("/activity", params=params), 422, "validation_failed")


@pytest.mark.parametrize("params", [{"direction": "both"}, {"status": "open"}])
def test_activity_ignores_request_only_parameters(world, params):
    """Unknown query parameters are ignored, never an error (§3.4)."""
    assert_status(world.ada.get("/activity", params=params), 200)


@pytest.mark.parametrize("amount", [1e9, 1000000000])
def test_integral_json_number_at_maximum_is_valid(world, amount):
    assert_error(world.ada.post("/payments", json={"to_handle":"bob", "amount":amount},
                                idempotency_key=new_key()), 409, "insufficient_funds")




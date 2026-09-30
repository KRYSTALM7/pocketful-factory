"""Stage 1 sample — the shipped smoke checks.

This is a sample, not the graded suite. It shows the shape of each surface once so a
service can be wired up against it; the spec is the contract and the graded checks are
the spec's. Passing everything here means the wiring is right, not that the stage holds.
"""
import pytest

import fixtures as fx
from harness.concurrent import burst, no_5xx, tally
from harness.http import Api, assert_error, assert_status, new_key

pytestmark = pytest.mark.stage(1)


def test_me_shape(world):
    body = assert_status(world.ada.get("/me"), 200).json()
    assert body["handle"] == "ada"
    assert body["display_name"] == "Ada"
    assert body["balance"] == fx.ADA["balance"]
    assert body["currency"] == world.currency
    assert body["minor_units"] == world.minor_units


def test_payment_moves_money_both_ways(world, pay, balance, conservation):
    before_ada, before_bob = balance(world.ada), balance(world.bob)
    assert_status(pay(amount=1500), 201)
    assert balance(world.ada) == before_ada - 1500
    assert balance(world.bob) == before_bob + 1500
    conservation(world)


def test_payment_response_shape(world, pay):
    body = assert_status(pay(amount=1500, note="dinner", visibility="public"),
                         201).json()
    assert body["from_handle"] == "ada" and body["to_handle"] == "bob"
    assert body["from_user_id"] and body["to_user_id"]
    assert body["amount"] == 1500
    assert body["currency"] == world.currency
    assert body["note"] == "dinner"
    assert body["visibility"] == "public"
    assert body["request_id"] is None
    assert body["payment_id"] and body["created_at"]


def test_note_and_visibility_default(world, pay):
    body = assert_status(pay(amount=100), 201).json()
    assert body["note"] == ""
    assert body["visibility"] == "public"


def test_insufficient_funds_moves_nothing(world, pay, balance, conservation):
    before = balance(world.cy)
    assert_error(pay(world.cy, to_handle="bob", amount=before + 1),
                 409, "insufficient_funds")
    assert balance(world.cy) == before, "a failed payment leaves no trace"
    conservation(world)


def test_paying_yourself_is_rejected(world, pay):
    assert_error(pay(to_handle="ada"), 422, "self_payment")


def test_a_new_user_starts_at_zero_and_can_receive(world, api, balance):
    resp = assert_status(world.ada.signup("dee@example.com", "correct horse", "Dee"), 201)
    dee = api(resp.json()["token"])
    assert balance(dee) == 0
    assert dee.get("/me").json()["handle"] == "dee"
    assert_status(world.ada.post("/payments", json={"to_handle": "dee", "amount": 250},
                                 idempotency_key=new_key()), 201)
    assert balance(dee) == 250


def test_handle_is_derived_from_the_email_local_part(world, api):
    resp = assert_status(
        world.ada.signup("Dee.Ann+tag@example.com", "correct horse", "Dee"), 201)
    handle = api(resp.json()["token"]).get("/me").json()["handle"]
    assert handle == "dee_ann_tag", f"lowercase, non [a-z0-9_] to _, got {handle!r}"


def test_request_shape_and_pending_status(world, ask):
    body = assert_status(ask(amount=1200, note="taxi"), 201).json()
    assert body["requester_handle"] == "bob" and body["payer_handle"] == "ada"
    assert body["amount"] == 1200 and body["note"] == "taxi"
    assert body["status"] == "pending" and body["payment_id"] is None
    assert body["currency"] == world.currency and body["request_id"]


def test_pay_a_request_moves_money_and_marks_it_paid(world, ask, balance, conservation):
    rq = assert_status(ask(amount=1200), 201).json()
    before_ada, before_bob = balance(world.ada), balance(world.bob)
    payment = assert_status(world.ada.post(f"/requests/{rq['request_id']}/pay",
                                           json={}, idempotency_key=new_key()),
                            201).json()
    assert payment["request_id"] == rq["request_id"]
    assert payment["amount"] == 1200
    assert balance(world.ada) == before_ada - 1200
    assert balance(world.bob) == before_bob + 1200
    listed = world.ada.get("/requests").json()["requests"]
    paid = next(r for r in listed if r["request_id"] == rq["request_id"])
    assert paid["status"] == "paid" and paid["payment_id"] == payment["payment_id"]
    conservation(world)


def test_decline_and_cancel(world, ask):
    rq = ask(amount=100).json()["request_id"]
    assert assert_status(world.ada.post(f"/requests/{rq}/decline"),
                         200).json()["status"] == "declined"
    # Declining twice is not an error.
    assert_status(world.ada.post(f"/requests/{rq}/decline"), 200)

    rq2 = ask(amount=100).json()["request_id"]
    assert assert_status(world.bob.post(f"/requests/{rq2}/cancel"),
                         200).json()["status"] == "cancelled"
    assert_status(world.bob.post(f"/requests/{rq2}/cancel"), 200)


def test_split_shares_follow_the_equal_split_rule(world):
    body = assert_status(world.ada.post("/splits", json={
        "amount": 1000, "participant_handles": ["ada", "bob", "cy"], "note": "dinner"},
        idempotency_key=new_key()), 201).json()
    assert [s["amount"] for s in body["shares"]] == fx.equal_split(1000, 3)
    assert [s["handle"] for s in body["shares"]] == ["ada", "bob", "cy"]
    assert sum(s["amount"] for s in body["shares"]) == 1000


def test_split_creates_one_request_per_participant_except_the_caller(world):
    body = world.ada.post("/splits", json={
        "amount": 1000, "participant_handles": ["ada", "bob", "cy"]},
        idempotency_key=new_key()).json()
    assert [r["payer_handle"] for r in body["requests"]] == ["bob", "cy"]
    assert all(r["requester_handle"] == "ada" for r in body["requests"])
    assert all(r["status"] == "pending" for r in body["requests"])


def test_public_payments_are_visible_to_third_parties(world, pay):
    assert_status(pay(world.ada, to_handle="bob", amount=100, visibility="public"), 201)
    feed = world.cy.get("/activity").json()["payments"]
    assert len(feed) == 1 and feed[0]["visibility"] == "public"


def test_private_payments_are_hidden_from_third_parties_only(world, pay):
    assert_status(pay(world.ada, to_handle="bob", amount=100, visibility="private"), 201)
    assert world.cy.get("/activity").json()["payments"] == []
    for party in (world.ada, world.bob):
        assert len(party.get("/activity").json()["payments"]) == 1, \
            "private hides from third parties, not from the two parties"


def test_payment_replay_is_200_and_moves_money_once(world, pay, balance, conservation):
    key = new_key()
    before = balance(world.ada)
    first = assert_status(pay(amount=300, key=key), 201).json()
    replay = assert_status(pay(amount=300, key=key), 200).json()
    assert replay == first
    assert balance(world.ada) == before - 300, "a replay must not move money twice"
    conservation(world)


# ---- under load ------------------------------------------------------------

def test_one_wallet_under_ten_clients(reset, base_url):
    """Ada holds 1000 and ten clients each try to send all of it: one wins.

    The shape once. The graded run uses the §2 budget of 50 in flight, drains a
    wallet in parts, rings three wallets around a cycle, and checks conservation
    across fifty of them.
    """
    reset(fx.fixture(users=[fx.user("ada", 1000), fx.user("bob", 0)]))
    adas = [Api(base_url).authenticate("ada@example.com", "correct horse")
            for _ in range(10)]
    try:
        out = burst(lambda i: adas[i].post(
            "/payments", json={"to_handle": "bob", "amount": 1000},
            idempotency_key=new_key()), 10)
        no_5xx(out)
        counts = tally(out)
        assert counts.get(201) == 1, f"exactly one payment may succeed: {counts}"
        assert counts.get(409) == 9, f"the other nine must be 409: {counts}"
        assert adas[0].get("/me").json()["balance"] == 0
    finally:
        for client in adas:
            client.close()


def test_export_can_restore_a_payment(world, pay, reset):
    receipt = pay().json()
    snapshot = world.ada.get('/_test/export')
    assert snapshot.status_code == 200
    reset(world.fixture)
    assert world.ada.post('/_test/import', json=snapshot.json()).status_code == 204
    assert world.ada.get('/activity').json()['payments'][0] == receipt

def test_operator_can_settle_two_transfers(reset, api):
    import fixtures as fx
    from harness.http import new_key
    fixture = fx.fixture()
    fixture['settlement_operator_ids'] = ['u_ada']
    reset(fixture)
    ada = api().authenticate(fx.ADA['email'], fx.ADA['password'])
    response = ada.post('/settlements', json={'transfers': [
        dict(from_handle='ada', to_handle='bob', amount=100),
        dict(from_handle='bob', to_handle='ada', amount=100)]}, idempotency_key=new_key())
    assert response.status_code == 201
    assert len(response.json()['payments']) == 2
    assert ada.get('/me').json()['balance'] == 10000




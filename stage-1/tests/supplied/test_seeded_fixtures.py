"""Seeded requests, a rejected fixture, and the amount range on every endpoint (§4, §5).

All three are in the spec and none of them is in the sample: `requests` is part of the
documented fixture, a negative seeded balance is a stated reset error, and §5 says the
shared ranges are "enforced on every endpoint that takes them".
"""
import pytest

import fixtures as fx
from harness.http import assert_error, assert_status, new_key

pytestmark = pytest.mark.stage(1)


def seeded_request(**over):
    body = {"id": "rq_seed", "requester_id": "u_bob", "payer_id": "u_ada",
            "amount": 1200, "note": "taxi", "status": "pending"}
    body.update(over)
    return body


def test_a_seeded_request_is_visible_to_both_parties_and_nobody_else(reset, api):
    reset(fx.fixture(requests=[seeded_request()]))
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    bob = api().authenticate(fx.BOB["email"], fx.BOB["password"])
    cy = api().authenticate(fx.CY["email"], fx.CY["password"])
    mine = ada.get("/requests").json()["requests"]
    assert len(mine) == 1 and mine[0]["amount"] == 1200
    assert mine[0]["status"] == "pending" and mine[0]["payer_handle"] == "ada"
    assert len(bob.get("/requests").json()["requests"]) == 1
    assert cy.get("/requests").json()["requests"] == []


def test_a_seeded_request_can_be_paid(reset, api):
    reset(fx.fixture(requests=[seeded_request()]))
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    bob = api().authenticate(fx.BOB["email"], fx.BOB["password"])
    rid = ada.get("/requests").json()["requests"][0]["request_id"]
    payment = assert_status(ada.post(f"/requests/{rid}/pay", json={},
                                     idempotency_key=new_key()), 201).json()
    assert payment["amount"] == 1200
    assert ada.get("/me").json()["balance"] == fx.ADA["balance"] - 1200
    assert bob.get("/me").json()["balance"] == fx.BOB["balance"] + 1200


def test_a_seeded_non_pending_request_cannot_be_paid(reset, api):
    reset(fx.fixture(requests=[seeded_request(status="declined")]))
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    rid = ada.get("/requests").json()["requests"][0]["request_id"]
    assert_error(ada.post(f"/requests/{rid}/pay", json={},
                          idempotency_key=new_key()), 409, "request_not_pending")


def test_a_negative_seeded_balance_is_a_reset_error_that_changes_nothing(reset, api):
    """§4: a balance below zero in a fixture is `422 validation_failed` from reset."""
    reset(fx.fixture())
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    before = ada.get("/me").json()["balance"]
    broken = fx.fixture(users=[fx.user("ada", -1), fx.user("bob", 100)])
    assert_error(reset(broken, raw=True), 422, "validation_failed")
    assert ada.get("/me").json()["balance"] == before, \
        "a rejected fixture leaves the previous state untouched"


@pytest.mark.parametrize("amount", [0, -1, 1_000_000_001, "100", 1.5])
def test_the_amount_range_is_enforced_on_requests(world, amount):
    assert_error(world.bob.post("/requests", json={
        "payer_handle": "ada", "amount": amount},
        idempotency_key=new_key()), 422, "validation_failed")


@pytest.mark.parametrize("amount", [0, -1, 1_000_000_001, "100", 1.5])
def test_the_amount_range_is_enforced_on_splits(world, amount):
    assert_error(world.ada.post("/splits", json={
        "amount": amount, "participant_handles": ["ada", "bob"]},
        idempotency_key=new_key()), 422, "validation_failed")

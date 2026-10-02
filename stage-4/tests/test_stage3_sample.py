import datetime as dt

import pytest

import fixtures as fx
from harness.http import assert_status, new_key

pytestmark = pytest.mark.stage(3)


def test_a_payment_carries_an_instant(world, pay):
    body = assert_status(pay(amount=100), 201).json()
    assert dt.datetime.fromisoformat(body["created_at"]).tzinfo is not None


def test_me_is_unchanged_without_as_of(world):
    assert "as_of" not in assert_status(world.ada.get("/me"), 200).json()


def test_as_of_in_the_future_is_the_current_balance(world, balance):
    now = dt.datetime.now(dt.timezone.utc)
    later = (now + dt.timedelta(hours=1)).isoformat()
    body = assert_status(world.ada.get("/me", params={"as_of": later}), 200).json()
    assert body["balance"] == balance(world.ada)
    assert body["as_of"] == later


def test_a_statement_closes_its_arithmetic(world, pay):
    assert_status(pay(to_handle="bob", amount=300), 201)
    body = assert_status(world.ada.get("/statement"), 200).json()
    assert body["opening_balance"] + sum(e["delta"] for e in body["entries"]) \
        == body["closing_balance"]


def test_a_statement_walks_the_balance_forward(world, pay):
    for amount in (300, 200):
        assert_status(pay(to_handle="bob", amount=amount), 201)
    body = assert_status(world.ada.get("/statement"), 200).json()
    running = body["opening_balance"]
    for entry in body["entries"]:
        running += entry["delta"]
        assert entry["balance_after"] == running, entry
    assert [e["delta"] for e in body["entries"]] == [-300, -200]


def test_a_correction_changes_the_current_balance(world, pay):
    payment = assert_status(pay(amount=100), 201).json()
    correction = assert_status(world.ada.post(
        "/payments/" + payment["payment_id"] + "/corrections",
        json={"expected_revision": 1, "amount": 60,
              "effective_at": payment["created_at"], "reason": "correct amount"},
        idempotency_key=new_key()), 201).json()
    assert correction["revision"] == 2
    assert assert_status(world.ada.get("/me"), 200).json()["balance"] == fx.ADA["balance"] - 60

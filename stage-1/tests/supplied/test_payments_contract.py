"""`GET /me` and `POST /payments` (§8)."""
import pytest

from harness.http import assert_error, assert_status, new_key

pytestmark = pytest.mark.stage(1)


def test_note_survives_a_round_trip_byte_for_byte(world, pay):
    """Stored verbatim: no trimming, escaping or normalisation."""
    note = "  café \U0001F600 <b>&amp;</b> ñ  "
    assert assert_status(pay(amount=100, note=note), 201).json()["note"] == note


def test_note_of_exactly_200_characters_is_accepted(world, pay):
    assert_status(pay(amount=100, note="x" * 200), 201)


def test_note_of_200_emoji_is_accepted(world, pay):
    """200 characters, not 200 bytes -- a 4-byte emoji still counts as one."""
    assert_status(pay(amount=100, note="\U0001F600" * 200), 201)


def test_note_over_200_characters_is_rejected(world, pay):
    assert_error(pay(amount=100, note="x" * 201), 422, "validation_failed")


def test_paying_exactly_the_balance_succeeds(world, pay, balance):
    exact = balance(world.cy)
    assert_status(pay(world.cy, to_handle="bob", amount=exact), 201)
    assert balance(world.cy) == 0


@pytest.mark.parametrize("amount", [0, -1, "1000", 1.5, 1_000_000_001])
def test_amount_out_of_range_or_wrong_type(world, pay, amount):
    assert_error(pay(amount=amount), 422, "validation_failed")


def test_amount_at_the_maximum_is_in_range(world, pay, balance):
    """1000000000 is allowed; this one fails on funds, not on range."""
    assert_error(pay(amount=1_000_000_000), 409, "insufficient_funds")


@pytest.mark.parametrize("visibility", ["Public", "", None, "secret"])
def test_bad_visibility_is_rejected(world, pay, visibility):
    assert_error(pay(amount=100, visibility=visibility), 422, "validation_failed")


@pytest.mark.parametrize("handle", ["nobody", "ADA", "@ada", ""])
def test_unknown_handle_is_404(world, pay, handle):
    """`ADA` and `@ada` do not match ^[a-z0-9_]{1,20}$, so no user has them."""
    resp = pay(to_handle=handle)
    assert resp.status_code in (404, 422), f"got {resp.status_code}"
    assert resp.json()["error"]["code"] in ("not_found", "validation_failed")


def test_missing_required_field_is_422(world):
    assert_error(world.ada.post("/payments", json={"amount": 100},
                                idempotency_key=new_key()), 422, "validation_failed")


def test_malformed_json_is_400(world):
    assert_error(world.ada.post("/payments", content="{nope",
                                idempotency_key=new_key()), 400, "malformed_request")


def test_unknown_fields_are_ignored(world, pay):
    assert_status(pay(amount=100, colour="blue"), 201)


def test_derived_handle_is_truncated_to_20_characters(world, api):
    resp = assert_status(
        world.ada.signup("a" * 30 + "@example.com", "correct horse", "Long"), 201)
    assert api(resp.json()["token"]).get("/me").json()["handle"] == "a" * 20


def test_taken_derived_handle_fails_and_creates_no_account(world, api):
    """ada@other.example derives `ada`, which the fixture already holds."""
    assert_error(world.ada.signup("ada@other.example", "correct horse", "Other"),
                 409, "handle_taken")
    assert_error(world.ada.login("ada@other.example", "correct horse"),
                 401, "unauthenticated")





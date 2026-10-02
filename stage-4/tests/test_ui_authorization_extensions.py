import pytest

import fixtures as fx
from harness.http import assert_status, new_key


pytestmark = pytest.mark.stage(2)


def sel(test_id):
    return f"[data-testid='{test_id}']"


def login(page, email):
    page.goto("/login")
    page.fill(sel("login-email"), email)
    page.fill(sel("login-password"), fx.ADA["password"])
    page.click(sel("login-submit"))
    page.wait_for_selector(sel("current-user"))


def test_authorization_capture_can_leave_remainder_open(reset, page, api):
    reset(fx.fixture())
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    authorization = assert_status(ada.post(
        "/authorizations", json={"to_handle": "bob", "amount": 2000},
        idempotency_key=new_key()), 201).json()
    auth_id = authorization["authorization_id"]

    login(page, fx.BOB["email"])
    page.goto("/authorizations")
    input_id = f"authorization-capture-amount-{auth_id}"
    page.wait_for_selector(sel(input_id))
    page.fill(sel(input_id), "3.00")
    final_id = f"authorization-capture-final-{auth_id}"
    assert page.is_checked(sel(final_id))
    page.uncheck(sel(final_id))
    page.click(sel(f"authorization-capture-{auth_id}"))

    for _ in range(30):
        item = ada.get("/authorizations").json()["authorizations"][0]
        if item["captured_amount"] == 300:
            break
        page.wait_for_timeout(100)
    assert item["status"] == "open"
    assert item["captured_amount"] == 300
    assert item["remaining_amount"] == 1700
    assert ada.get("/me").json()["held"] == 1700


def test_204_payment_response_is_reported_as_uncertain(reset, page):
    reset(fx.fixture())
    def return_empty_success(route):
        if route.request.method == "POST":
            route.fulfill(status=204)
        else:
            route.continue_()

    page.route("**/payments", return_empty_success)
    login(page, fx.ADA["email"])
    page.goto("/")
    page.fill(sel("pay-handle"), "bob")
    page.fill(sel("pay-amount"), "3.00")
    page.click(sel("pay-submit"))
    page.wait_for_selector(sel("pay-uncertain"))
    assert page.query_selector(sel("pay-error")) is None

"""A request that never reaches the service shows a readable message, not the browser's."""
import pytest

import fixtures as fx

pytestmark = pytest.mark.stage(2)

READABLE = "Couldn’t reach Pocketful. Check your connection and try again."


def sel(test_id):
    return f"[data-testid='{test_id}']"


def login(page, email):
    page.goto("/login")
    page.fill(sel("login-email"), email)
    page.fill(sel("login-password"), fx.ADA["password"])
    page.click(sel("login-submit"))
    page.wait_for_selector(sel("current-user"))


def test_unreachable_activity_shows_a_readable_message(reset, page):
    reset(fx.fixture())
    login(page, fx.ADA["email"])
    page.route("**/activity", lambda route: route.abort())
    page.goto("/")
    error = page.wait_for_selector(sel("home-error"))
    assert error.inner_text() == READABLE
    assert "Failed to fetch" not in page.inner_text("body")


def test_unreachable_payment_stays_uncertain(reset, page):
    reset(fx.fixture())
    login(page, fx.ADA["email"])
    page.goto("/")
    page.route("**/payments", lambda route: route.abort()
               if route.request.method == "POST" else route.continue_())
    page.fill(sel("pay-handle"), "bob")
    page.fill(sel("pay-amount"), "3.00")
    page.click(sel("pay-submit"))
    page.wait_for_selector(sel("pay-uncertain"))
    assert page.query_selector(sel("pay-error")) is None

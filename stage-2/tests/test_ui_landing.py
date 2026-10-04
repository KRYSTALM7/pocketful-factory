"""The signed-out home page is a landing page: sign-in links instead of the app navigation."""
import pytest

import fixtures as fx

pytestmark = pytest.mark.stage(2)


def sel(test_id):
    return f"[data-testid='{test_id}']"


@pytest.mark.parametrize("width", [375, 1280])
def test_signed_out_home_is_a_landing_page_without_app_nav(reset, page, width):
    reset(fx.fixture())
    page.set_viewport_size({"width": width, "height": 900})
    page.goto("/")
    page.wait_for_selector(sel("landing-hero"))
    assert page.is_hidden(".main-nav")
    assert page.get_attribute(sel("landing-signup"), "href") == "/signup"
    assert page.get_attribute(sel("landing-login"), "href") == "/login"
    assert page.get_attribute(sel("header-login"), "href") == "/login"
    assert page.get_attribute(sel("header-signup"), "href") == "/signup"
    for placeholder in page.query_selector_all(".image-placeholder"):
        assert placeholder.get_attribute("aria-hidden") == "true"
    assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")


def test_signed_in_home_keeps_the_app_nav(reset, page):
    reset(fx.fixture())
    page.goto("/login")
    page.fill(sel("login-email"), fx.ADA["email"])
    page.fill(sel("login-password"), fx.ADA["password"])
    page.click(sel("login-submit"))
    page.wait_for_selector(sel("wallet-card"))
    assert page.is_visible(".main-nav")
    assert page.query_selector(sel("landing-hero")) is None
    assert page.query_selector(sel("header-signup")) is None

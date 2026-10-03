"""The UI works under its Content-Security-Policy and stays usable on small screens."""
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


def test_ui_routes_raise_no_csp_violations(reset, page):
    reset(fx.fixture())
    violations = []
    page.on("console", lambda message: violations.append(message.text)
            if "Content Security Policy" in message.text else None)
    login(page, fx.ADA["email"])
    for route in ("/", "/requests", "/split", "/authorizations"):
        page.goto(route)
        page.wait_for_load_state("networkidle")
    assert violations == []


@pytest.mark.parametrize("width", [375, 760])  # the nav scrolls, and clips, at 760px and below
def test_nav_focus_ring_is_not_clipped_on_small_screens(reset, page, width):
    reset(fx.fixture())
    page.set_viewport_size({"width": width, "height": 800})
    login(page, fx.ADA["email"])
    link = page.locator(".main-nav a", has_text="Requests")
    link.focus()
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    ring = link.evaluate("""el => {
        const s = getComputedStyle(el), r = el.getBoundingClientRect();
        const n = el.closest('.main-nav').getBoundingClientRect();
        const reach = parseFloat(s.outlineWidth) + parseFloat(s.outlineOffset);
        return {style: s.outlineStyle, top: r.top - reach, bottom: r.bottom + reach,
                navTop: n.top, navBottom: n.bottom};
    }""")
    assert ring["style"] != "none"
    assert ring["top"] >= ring["navTop"] - 0.5 and ring["bottom"] <= ring["navBottom"] + 0.5, ring


def test_hold_expiry_is_labelled_and_keeps_its_exact_value(reset, page, api):
    reset(fx.fixture())
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    authorization = assert_status(ada.post(
        "/authorizations", json={"to_handle": "bob", "amount": 500},
        idempotency_key=new_key()), 201).json()
    login(page, fx.ADA["email"])
    page.goto("/authorizations")
    expiry = page.locator(sel(f"authorization-expires-{authorization['authorization_id']}"))
    assert expiry.inner_text() == authorization["expires_at"]
    assert expiry.evaluate("el => el.parentElement.textContent").startswith("Expires ")

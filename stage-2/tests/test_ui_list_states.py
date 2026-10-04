"""Lists show an error, a loading state, or the empty state, never two at once; login honours a safe ?next=."""
import re
from urllib.parse import urlsplit

import pytest

import fixtures as fx

pytestmark = pytest.mark.stage(2)

REQUESTS_READ = re.compile(r"/requests\?limit=")
AUTHORIZATIONS_READ = re.compile(r"/authorizations\?limit=")


def sel(test_id):
    return f"[data-testid='{test_id}']"


def login(page, email, path="/login"):
    page.goto(path)
    page.fill(sel("login-email"), email)
    page.fill(sel("login-password"), fx.ADA["password"])
    page.click(sel("login-submit"))
    page.wait_for_selector(sel("current-user"))


def test_aborted_requests_fetch_shows_only_the_error(reset, page):
    reset(fx.fixture())
    login(page, fx.ADA["email"])
    page.route(REQUESTS_READ, lambda route: route.abort())
    page.goto("/requests")
    page.wait_for_selector(sel("request-error"))
    assert page.is_hidden(sel("empty-requests"))
    assert page.is_hidden(sel("requests-loading"))


def test_failed_holds_fetch_shows_only_the_error(reset, page):
    reset(fx.fixture())
    login(page, fx.ADA["email"])
    page.route(AUTHORIZATIONS_READ, lambda route: route.fulfill(
        status=503, content_type="application/json",
        body='{"error": {"code": "unavailable", "message": "Try again soon."}}'))
    page.goto("/authorizations")
    page.wait_for_selector(sel("authorization-error"))
    assert page.is_hidden(sel("empty-authorizations"))
    assert page.is_hidden(sel("authorizations-loading"))


@pytest.mark.parametrize("path, read, loading_id, list_id, empty_id", [
    ("/requests", REQUESTS_READ, "requests-loading", "request-lists", "empty-requests"),
    ("/authorizations", AUTHORIZATIONS_READ, "authorizations-loading", "authorization-list",
     "empty-authorizations"),
])
def test_slow_list_shows_loading_not_the_empty_state(reset, page, path, read, loading_id,
                                                     list_id, empty_id):
    reset(fx.fixture())
    login(page, fx.ADA["email"])
    held = []
    page.route(read, lambda route: held.append(route))
    page.goto(path)
    page.wait_for_selector(sel(loading_id))
    assert page.get_attribute(sel(list_id), "aria-busy") == "true"
    assert page.is_hidden(sel(empty_id))
    while not held:
        page.wait_for_timeout(50)
    held[0].continue_()
    page.wait_for_selector(sel(loading_id), state="hidden")
    assert page.get_attribute(sel(list_id), "aria-busy") == "false"


def test_login_follows_an_allowed_next_route(reset, page):
    reset(fx.fixture())
    page.goto("/requests")
    assert page.get_attribute("a.primary-button", "href") == "/login?next=requests"
    page.click("a.primary-button")
    page.fill(sel("login-email"), fx.ADA["email"])
    page.fill(sel("login-password"), fx.ADA["password"])
    page.click(sel("login-submit"))
    page.wait_for_url(re.compile(r"/requests$"))
    page.wait_for_selector(sel("incoming-list"), state="attached")


@pytest.mark.parametrize("next_value", ["%2F%2Fevil.example", "//evil.example",
                                        "https%3A%2F%2Fevil.example", "nowhere"])
def test_login_ignores_an_unknown_or_offsite_next(reset, page, base_url, next_value):
    reset(fx.fixture())
    login(page, fx.ADA["email"], f"/login?next={next_value}")
    current = urlsplit(page.url)
    assert f"{current.scheme}://{current.netloc}" == base_url
    assert current.path == "/"

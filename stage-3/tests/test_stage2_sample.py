"""Stage 2 sample — the shipped smoke checks.

This is a sample, not the graded suite. It shows the shape of each surface once so a
service can be wired up against it; the spec is the contract and the graded checks are
the spec's. Passing everything here means the wiring is right, not that the stage holds.
"""
import pytest

import fixtures as fx
from playwright.sync_api import expect
from harness.http import assert_status, new_key

pytestmark = pytest.mark.stage(2)


def sel(name: str) -> str:
    return f"[data-testid='{name}']"


def money(minor: int, minor_units: int = 2, currency: str = "EUR") -> str:
    """`100.00 EUR`; with minor_units 0 there is no decimal point at all."""
    if minor_units == 0:
        return f"{minor} {currency}"
    text = str(minor).rjust(minor_units + 1, "0")
    return f"{text[:-minor_units]}.{text[-minor_units:]} {currency}"


@pytest.fixture
def seeded(reset):
    fixture = fx.fixture()
    reset(fixture)
    return fixture


def log_in(page, email=None, password="correct horse", expect_success=True):
    page.goto("/login")
    page.fill(sel("login-email"), email or fx.ADA["email"])
    page.fill(sel("login-password"), password)
    page.click(sel("login-submit"))
    if expect_success:
        page.wait_for_selector(sel("current-user"))


def _pay(page, handle="bob", amount="15.00", note="", visibility=None):
    page.goto("/")
    page.wait_for_selector(sel("pay-submit"))
    page.fill(sel("pay-handle"), handle)
    page.fill(sel("pay-amount"), amount)
    if note:
        page.fill(sel("pay-note"), note)
    if visibility:
        page.select_option(sel("pay-visibility"), visibility)
    page.click(sel("pay-submit"))


@pytest.mark.parametrize("route,anchor", [
    ("/", "pay-submit"), ("/requests", "incoming-list"), ("/split", "split-submit"),
    ("/signup", "signup-submit"), ("/login", "login-submit"),
])
def test_routes_are_directly_navigable(seeded, page, route, anchor):
    log_in(page)
    page.goto(route)
    page.wait_for_selector(sel(anchor), state="attached")


def test_balance_is_formatted_and_carries_minor_units(seeded, page):
    log_in(page)
    page.goto("/")
    page.wait_for_selector(sel("wallet-balance"))
    assert page.text_content(sel("wallet-balance")).strip() == money(fx.ADA["balance"])
    assert page.get_attribute(sel("wallet-balance"), "data-amount") == str(fx.ADA["balance"])


def test_refresh_observes_another_clients_payment(seeded, page, api):
    log_in(page)
    page.goto('/')
    expect(page.get_by_test_id('wallet-balance')).to_have_attribute('data-amount', '10000')
    ada = api().authenticate(fx.ADA['email'], fx.ADA['password'])
    payment = assert_status(ada.post('/payments', json={'to_handle': 'bob', 'amount': 300},
                                     idempotency_key=new_key()), 201).json()
    page.click(sel('wallet-refresh'))
    expect(page.get_by_test_id('wallet-balance')).to_have_attribute('data-amount', '9700')
    expect(page.get_by_test_id('activity-item-' + payment['payment_id'])).to_be_visible()


def test_paying_updates_the_balance_without_a_manual_reload(seeded, page):
    log_in(page)
    _pay(page, amount="15.00")
    page.wait_for_selector(f"{sel('wallet-balance')}[data-amount='{10_000 - 1500}']")
    assert page.text_content(sel("wallet-balance")).strip() == money(8500)


def test_preceding_stage_accounts_survive_import(previous_api, api):
    import fixtures as fx
    assert previous_api.post('/_test/reset', json=fx.fixture()).status_code == 204
    previous_api.authenticate(fx.ADA['email'], fx.ADA['password'])
    snapshot = previous_api.get('/_test/export')
    assert snapshot.status_code == 200
    target = api(token=previous_api.token)
    assert target.post('/_test/import', json=snapshot.json()).status_code == 204
    assert target.get('/me').json()['balance'] == 10000


def test_a_hold_reserves_funds_without_moving_them(reset, api):
    reset(fx.fixture())
    ada = api().authenticate(fx.ADA['email'], fx.ADA['password'])
    held = assert_status(ada.post('/authorizations', json={'to_handle': 'bob', 'amount': 2000},
                                  idempotency_key=new_key()), 201).json()
    assert held['status'] == 'open'
    me = assert_status(ada.get('/me'), 200).json()
    assert me['total'] == fx.ADA['balance']
    assert me['held'] == 2000 and me['available'] == me['total'] - 2000





"""Stage 2 — the UI over the stage-1 API (§9).

Every element is found by its exact `data-testid` and by nothing else.
"""
import pytest

import fixtures as fx
from harness.http import new_key

pytestmark = pytest.mark.stage(2)


def sel(name: str) -> str:
    return f"[data-testid='{name}']"


def money(minor: int, minor_units: int = 2, currency: str = "EUR") -> str:
    """`100.00 EUR`; with minor_units 0 there is no decimal point at all."""
    if minor_units == 0:
        return f"{minor} {currency}"
    text = str(minor).rjust(minor_units + 1, "0")
    return f"{text[:-minor_units]}.{text[-minor_units:]} {currency}"


@pytest.fixture
def seeded(reset):
    fixture = fx.fixture()
    reset(fixture)
    return fixture


def log_in(page, email=None, password="correct horse", expect_success=True):
    page.goto("/login")
    page.fill(sel("login-email"), email or fx.ADA["email"])
    page.fill(sel("login-password"), password)
    page.click(sel("login-submit"))
    if expect_success:
        page.wait_for_selector(sel("current-user"))


# ---- routes and auth -----------------------------------------------------


def test_signup_then_current_user_and_handle(seeded, page):
    page.goto("/signup")
    page.fill(sel("signup-email"), "dee.ann@example.com")
    page.fill(sel("signup-password"), "correct horse")
    page.fill(sel("signup-display-name"), "Dee")
    page.click(sel("signup-submit"))
    page.wait_for_selector(sel("current-user"))
    assert "Dee" in page.text_content(sel("current-user"))
    assert page.text_content(sel("current-handle")).strip() == "dee_ann", \
        "current-handle is exactly the handle, no @ and no surrounding words"


def test_bad_login_shows_auth_error(seeded, page):
    log_in(page, password="nope nope nope", expect_success=False)
    page.wait_for_selector(sel("auth-error"))


def test_logout(seeded, page):
    log_in(page)
    page.click(sel("logout-button"))
    page.wait_for_selector(sel("current-user"), state="detached")


# ---- balance and pay -----------------------------------------------------


def test_minor_units_zero_has_no_decimal_point(reset, page):
    reset(fx.fixture(currency="JPY"))
    log_in(page)
    page.goto("/")
    page.wait_for_selector(sel("wallet-balance"))
    text = page.text_content(sel("wallet-balance")).strip()
    assert text == money(fx.ADA["balance"], 0, "JPY"), text
    assert "." not in text


def _pay(page, handle="bob", amount="15.00", note="", visibility=None):
    page.goto("/")
    page.wait_for_selector(sel("pay-submit"))
    page.fill(sel("pay-handle"), handle)
    page.fill(sel("pay-amount"), amount)
    if note:
        page.fill(sel("pay-note"), note)
    if visibility:
        page.select_option(sel("pay-visibility"), visibility)
    page.click(sel("pay-submit"))


def test_a_typed_decimal_becomes_minor_units(seeded, page):
    """15 is 1500, 15.5 is 1550, 15.00 is 1500 in a minor_units 2 service."""
    log_in(page)
    _pay(page, amount="15.5")
    page.wait_for_selector(f"{sel('wallet-balance')}[data-amount='{10_000 - 1550}']")


def test_too_many_decimal_places_is_refused_in_the_form(seeded, page):
    """15.005 has more places than the currency allows: refused, not rounded."""
    log_in(page)
    _pay(page, amount="15.005")
    page.wait_for_selector(sel("pay-error"))
    assert page.get_attribute(sel("wallet-balance"), "data-amount") == str(fx.ADA["balance"]), \
        "nothing may move when the form refuses the input"


def test_insufficient_funds_shows_pay_error(seeded, page):
    log_in(page, email=fx.CY["email"])       # Cy holds 500
    _pay(page, amount="99.00")
    page.wait_for_selector(sel("pay-error"))


def test_paying_an_unknown_handle_shows_pay_error(seeded, page):
    log_in(page)
    _pay(page, handle="nobody", amount="1.00")
    page.wait_for_selector(sel("pay-error"))


# ---- activity feed -------------------------------------------------------

def test_feed_shows_a_payment_with_its_parts(seeded, page, api):
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    made = ada.post("/payments", json={
        "to_handle": "bob", "amount": 2500, "note": "dinner", "visibility": "public"},
        idempotency_key=new_key()).json()
    pid = made["payment_id"]

    log_in(page)
    page.goto("/")
    page.wait_for_selector(sel(f"activity-item-{pid}"))
    assert page.get_attribute(sel(f"activity-item-{pid}"), "data-visibility") == "public"
    parties = page.text_content(sel(f"activity-parties-{pid}"))
    assert "ada" in parties and "bob" in parties
    assert page.text_content(sel(f"activity-amount-{pid}")).strip() == money(2500)
    assert page.text_content(sel(f"activity-note-{pid}")).strip() == "dinner"


def test_empty_note_element_is_still_present(seeded, page, api):
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    pid = ada.post("/payments", json={"to_handle": "bob", "amount": 100},
                   idempotency_key=new_key()).json()["payment_id"]
    log_in(page)
    page.goto("/")
    page.wait_for_selector(sel(f"activity-note-{pid}"), state="attached")
    assert page.text_content(sel(f"activity-note-{pid}")).strip() == ""


def test_empty_activity_is_shown_when_nothing_is_visible(seeded, page):
    log_in(page, email=fx.CY["email"])
    page.goto("/")
    page.wait_for_selector(sel("empty-activity"))


def test_private_payment_is_hidden_from_a_third_party(seeded, page, api):
    bob = api().authenticate(fx.BOB["email"], fx.BOB["password"])
    pid = bob.post("/payments", json={
        "to_handle": "cy", "amount": 100, "visibility": "private"},
        idempotency_key=new_key()).json()["payment_id"]
    log_in(page)                              # Ada is neither party
    page.goto("/")
    page.wait_for_selector(sel("empty-activity"))
    assert page.query_selector(sel(f"activity-item-{pid}")) is None


def test_feed_is_newest_first_in_the_dom(seeded, page, api):
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    made = [ada.post("/payments", json={"to_handle": "bob", "amount": 10 + i},
                    idempotency_key=new_key()).json() for i in range(3)]
    log_in(page)
    page.goto("/")
    page.wait_for_selector(sel("activity-list"))
    order = page.eval_on_selector_all(
        f"{sel('activity-list')} > *",
        "els => els.map(e => e.getAttribute('data-testid'))")
    rendered = [o for o in order if o and o.startswith("activity-item-")]
    by_id = {f"activity-item-{p['payment_id']}": p for p in made}
    assert set(rendered) == set(by_id)
    times = [by_id[item]["created_at"][:19] for item in rendered]
    assert times == sorted(times, reverse=True)


# ---- requests ------------------------------------------------------------

def test_incoming_request_can_be_paid(seeded, page, api):
    bob = api().authenticate(fx.BOB["email"], fx.BOB["password"])
    rid = bob.post("/requests", json={"payer_handle": "ada", "amount": 1200},
                   idempotency_key=new_key()).json()["request_id"]
    log_in(page)
    page.goto("/requests")
    page.wait_for_selector(sel(f"request-item-{rid}"))
    assert page.get_attribute(sel(f"request-item-{rid}"), "data-status") == "pending"
    assert page.text_content(sel(f"request-amount-{rid}")).strip() == money(1200)
    page.click(sel(f"request-pay-{rid}"))
    page.wait_for_selector(f"{sel('request-item-' + rid)}[data-status='paid']")
    assert page.query_selector(sel(f"request-pay-{rid}")) is None, \
        "pay is present only on a pending incoming request"


def test_incoming_request_can_be_declined(seeded, page, api):
    bob = api().authenticate(fx.BOB["email"], fx.BOB["password"])
    rid = bob.post("/requests", json={"payer_handle": "ada", "amount": 100},
                   idempotency_key=new_key()).json()["request_id"]
    log_in(page)
    page.goto("/requests")
    page.click(sel(f"request-decline-{rid}"))
    page.wait_for_selector(f"{sel('request-item-' + rid)}[data-status='declined']")


def test_outgoing_request_can_be_cancelled(seeded, page, api):
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    rid = ada.post("/requests", json={"payer_handle": "bob", "amount": 100},
                   idempotency_key=new_key()).json()["request_id"]
    log_in(page)
    page.goto("/requests")
    page.wait_for_selector(sel(f"request-item-{rid}"))
    assert page.query_selector(sel(f"request-pay-{rid}")) is None, \
        "an outgoing request has no pay button"
    page.click(sel(f"request-cancel-{rid}"))
    page.wait_for_selector(f"{sel('request-item-' + rid)}[data-status='cancelled']")


def test_paying_a_request_without_funds_shows_request_error(seeded, page, api):
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    rid = ada.post("/requests", json={"payer_handle": "cy", "amount": 90_000},
                   idempotency_key=new_key()).json()["request_id"]
    log_in(page, email=fx.CY["email"])
    page.goto("/requests")
    page.click(sel(f"request-pay-{rid}"))
    page.wait_for_selector(sel("request-error"))


def test_empty_requests_is_shown_when_both_lists_are_empty(seeded, page):
    log_in(page)
    page.goto("/requests")
    page.wait_for_selector(sel("empty-requests"))


# ---- split ---------------------------------------------------------------

def test_split_preview_matches_the_server_rule(seeded, page):
    """10.00 among three is 334, 333, 333 -- computed before anything is posted."""
    log_in(page)
    page.goto("/split")
    page.fill(sel("split-amount"), "10.00")
    page.fill(sel("split-handles"), "ada,bob,cy")
    page.wait_for_selector(sel("split-preview"))
    shares = [page.text_content(sel(f"split-share-{h}")).strip()
              for h in ("ada", "bob", "cy")]
    assert shares == [money(334), money(333), money(333)], shares


def test_split_preview_follows_handle_order(seeded, page):
    log_in(page)
    page.goto("/split")
    page.fill(sel("split-amount"), "10.00")
    page.fill(sel("split-handles"), "cy,bob,ada")
    page.wait_for_selector(sel("split-share-cy"))
    assert page.text_content(sel("split-share-cy")).strip() == money(334)
    assert page.text_content(sel("split-share-ada")).strip() == money(333)


def test_split_submits_and_creates_requests(seeded, page, api):
    log_in(page)
    page.goto("/split")
    page.fill(sel("split-amount"), "10.00")
    page.fill(sel("split-handles"), "ada,bob,cy")
    page.wait_for_selector(sel("split-preview"))
    page.click(sel("split-submit"))
    # Poll the API rather than the network: a server-rendered form never sends POST /splits.
    bob = api().authenticate(fx.BOB["email"], fx.BOB["password"])
    for _ in range(100):
        theirs = bob.get("/requests").json()["requests"]
        if theirs:
            break
        page.wait_for_timeout(100)
    assert len(theirs) == 1 and theirs[0]["amount"] == 333


def test_split_error_on_an_unknown_handle(seeded, page):
    log_in(page)
    page.goto("/split")
    page.fill(sel("split-amount"), "10.00")
    page.fill(sel("split-handles"), "ada,nobody")
    page.click(sel("split-submit"))
    page.wait_for_selector(sel("split-error"))


# ---- a double submit is a replay ------------------------------------------

def test_submitting_the_pay_form_twice_moves_the_money_once(seeded, page):
    """The form still holds its values after a successful pay, so a second click
    sends the same body. A form that mints its idempotency key at submit time
    sends a new one and pays twice."""
    log_in(page)
    _pay(page, amount="15.00")
    after = fx.ADA["balance"] - 1500
    page.wait_for_selector(f"{sel('wallet-balance')}[data-amount='{after}']")

    page.click(sel("pay-submit"))
    page.wait_for_timeout(500)
    assert page.query_selector(sel("pay-error")) is None, \
        "the second submit was refused instead of replayed"
    assert page.get_attribute(sel("wallet-balance"), "data-amount") == str(after), \
        "the money moved twice"


def test_a_double_submit_leaves_one_payment(seeded, page, api):
    log_in(page)
    _pay(page, amount="15.00")
    page.wait_for_selector(
        f"{sel('wallet-balance')}[data-amount='{fx.ADA['balance'] - 1500}']")
    page.click(sel("pay-submit"))
    page.wait_for_timeout(500)

    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    feed = ada.get("/activity").json()["payments"]
    assert len([p for p in feed if p["amount"] == 1500]) == 1, \
        f"paid {len([p for p in feed if p['amount'] == 1500])} times"


def test_changing_the_form_first_is_a_different_payment(seeded, page):
    """Editing a field starts a new payment, so it must not replay the old one."""
    log_in(page)
    _pay(page, amount="15.00")
    page.wait_for_selector(
        f"{sel('wallet-balance')}[data-amount='{fx.ADA['balance'] - 1500}']")
    page.fill(sel("pay-amount"), "20.00")
    page.click(sel("pay-submit"))
    page.wait_for_selector(
        f"{sel('wallet-balance')}[data-amount='{fx.ADA['balance'] - 1500 - 2000}']")
    assert page.query_selector(sel("pay-error")) is None

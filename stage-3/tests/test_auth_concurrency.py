"""Password hashing runs outside the service lock: logins don't stall other requests."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from harness.http import Api, assert_status

USERS = [{"id": "u_ada", "email": "ada@example.com", "password": "correct horse",
          "display_name": "Ada", "handle": "ada", "balance": 1000}]
# Jerry's baseline with scrypt under the lock was 3.9 s for a request queued behind 50 logins.
OTHER_REQUEST_BOUND_SECONDS = 1.0  # measured 0.00-0.03 s in a 2-CPU container after the fix


def test_unrelated_request_is_not_blocked_by_a_login_burst(reset, base_url):
    reset({"currency": "EUR", "minor_units": 2, "users": USERS, "payments": [], "requests": []})
    ada = Api(base_url).authenticate("ada@example.com", "correct horse")
    gate = threading.Barrier(51)

    def log_in(_):
        gate.wait()
        return Api(base_url).login("ada@example.com", "correct horse").status_code

    def read_me():
        gate.wait()
        time.sleep(0.2)  # let the logins reach the server first
        started = time.perf_counter()
        response = ada.get("/me")
        return response.status_code, time.perf_counter() - started

    with ThreadPoolExecutor(max_workers=51) as pool:
        logins = [pool.submit(log_in, i) for i in range(50)]
        me = pool.submit(read_me)
        statuses = [future.result() for future in logins]
        me_status, me_seconds = me.result()
    assert statuses == [200] * 50
    assert me_status == 200
    assert me_seconds < OTHER_REQUEST_BOUND_SECONDS, f"GET /me took {me_seconds:.2f}s"


def test_concurrent_duplicate_signups_create_one_account(reset, base_url):
    reset({"currency": "EUR", "minor_units": 2, "users": USERS, "payments": [], "requests": []})
    gate = threading.Barrier(20)

    def sign_up(_):
        gate.wait()
        response = Api(base_url).signup("same@example.com", "long enough", "Same")
        return response.status_code, (response.json() or {}).get("error", {}).get("code")

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(sign_up, range(20)))
    assert sorted(results, key=str) == sorted([(201, None)] + [(409, "email_taken")] * 19, key=str)
    assert_status(Api(base_url).login("same@example.com", "long enough"), 200)

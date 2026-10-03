"""A large fixture reset completes within the spec's 10 s reset timeout."""
import json
import time
import urllib.request

RESET_TIMEOUT_SECONDS = 10  # spec: per-request timeout is 10 s for POST /_test/reset


def test_hundred_user_fixture_resets_within_the_reset_timeout(base_url):
    users = [{"id": f"u_{i}", "email": f"user{i}@example.com", "password": f"password-{i}",
              "display_name": f"User {i}", "handle": f"user{i}", "balance": 100}
             for i in range(100)]
    body = json.dumps({"currency": "EUR", "minor_units": 2, "users": users,
                       "payments": [], "requests": []}).encode("utf-8")
    request = urllib.request.Request(base_url + "/_test/reset", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    started = time.perf_counter()
    # The client waits longer than the budget so a slow reset fails on time, not on a socket timeout.
    with urllib.request.urlopen(request, timeout=60) as response:
        status = response.status
    elapsed = time.perf_counter() - started
    assert status == 204
    assert elapsed <= RESET_TIMEOUT_SECONDS, f"reset took {elapsed:.2f}s"

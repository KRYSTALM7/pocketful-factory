from concurrent.futures import ThreadPoolExecutor
from collections import Counter


def burst(call, count):
    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(call, range(count)))


def no_5xx(responses):
    failures = [r.status_code for r in responses if r.status_code >= 500]
    assert not failures, f"server errors under concurrent load: {failures}"


def tally(responses):
    return dict(Counter(r.status_code for r in responses))

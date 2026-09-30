"""Fixture helpers matching the inputs used by the supplied Stage 1 tests."""

ADA = {"id": "u_ada", "email": "ada@example.com", "password": "correct horse",
       "display_name": "Ada", "handle": "ada", "balance": 10000}
BOB = {"id": "u_bob", "email": "bob@example.com", "password": "correct horse",
       "display_name": "Bob", "handle": "bob", "balance": 2500}
CY = {"id": "u_cy", "email": "cy@example.com", "password": "correct horse",
      "display_name": "Cy", "handle": "cy", "balance": 5000}


def user(handle, balance=0):
    return {"id": f"u_{handle}", "email": f"{handle}@example.com",
            "password": "correct horse", "display_name": handle.title(),
            "handle": handle, "balance": balance}


def fixture(users=None, payments=None, requests=None, currency="EUR"):
    minor = {"EUR": 2, "JPY": 0, "BHD": 3}[currency]
    return {"currency": currency, "minor_units": minor,
            "users": [dict(u) for u in (users if users is not None else (ADA, BOB, CY))],
            "payments": [dict(p) for p in (payments or [])],
            "requests": [dict(r) for r in (requests or [])]}


def equal_split(amount, count):
    quotient, remainder = divmod(amount, count)
    return [quotient + (1 if i < remainder else 0) for i in range(count)]

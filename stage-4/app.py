"""Pocketful Stage 4 HTTP service. Standard-library only."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import mimetypes
import os
import re
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


MAX_AMOUNT = 1_000_000_000
MAX_BODY_BYTES = 2_000_000
# An unchanged export must always import, so the import endpoint gets a far larger cap.
MAX_IMPORT_BODY_BYTES = 512 * 1024 * 1024
# The UI loads only its own script and stylesheet, so everything else stays blocked.
CONTENT_SECURITY_POLICY = ("default-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                           "form-action 'self'")
MAX_BALANCE = 2**53
HANDLE_RE = re.compile(r"^[a-z0-9_]{1,20}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+$")
PASSWORD_HASH_RE = re.compile(r"^[0-9a-f]{32}:[0-9a-f]{64}$")
STATUSES = {"pending", "paid", "declined", "cancelled"}
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = "Request could not be completed"):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class RawJsonNumber:
    """Keeps a syntactically valid number when Python cannot represent its magnitude."""
    def __init__(self, token):
        self.token = token


def parse_json_integer(token):
    try:
        return int(token)
    except ValueError:
        return RawJsonNumber(token)


def parse_json_decimal(token):
    try:
        return Decimal(token)
    except (InvalidOperation, ValueError):
        return RawJsonNumber(token)


def add_small_to_decimal_integer(text, delta):
    """Add a bounded digit count to an arbitrarily long signed decimal integer."""
    sign = -1 if text.startswith("-") else 1
    digits = text[1:] if text[:1] in ("+", "-") else text
    digits = digits.lstrip("0") or "0"
    if delta == 0:
        return ("-" if sign < 0 and digits != "0" else "") + digits
    other_sign = -1 if delta < 0 else 1
    other = str(abs(delta))

    def add_abs(left, right):
        out, carry = [], 0
        i, j = len(left) - 1, len(right) - 1
        while i >= 0 or j >= 0 or carry:
            total = carry + (ord(left[i]) - 48 if i >= 0 else 0) + (ord(right[j]) - 48 if j >= 0 else 0)
            out.append(chr(48 + total % 10))
            carry = total // 10
            i, j = i - 1, j - 1
        return "".join(reversed(out))

    def subtract_abs(left, right):
        out, borrow = [], 0
        i, j = len(left) - 1, len(right) - 1
        while i >= 0:
            digit = ord(left[i]) - 48 - borrow - (ord(right[j]) - 48 if j >= 0 else 0)
            borrow = 1 if digit < 0 else 0
            out.append(chr(48 + digit + (10 if digit < 0 else 0)))
            i, j = i - 1, j - 1
        return "".join(reversed(out)).lstrip("0") or "0"

    if sign == other_sign:
        result, result_sign = add_abs(digits, other), sign
    else:
        ordering = (len(digits) > len(other)) - (len(digits) < len(other))
        if ordering == 0:
            ordering = (digits > other) - (digits < other)
        if ordering == 0:
            return "0"
        if ordering > 0:
            result, result_sign = subtract_abs(digits, other), sign
        else:
            result, result_sign = subtract_abs(other, digits), other_sign
    return ("-" if result_sign < 0 and result != "0" else "") + result


def normalize_raw_number(token):
    match = re.fullmatch(r"(-?)(0|[1-9][0-9]*)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?", token)
    if match is None:
        raise ValueError("Invalid JSON number")
    sign, whole, fraction, exponent = match.groups()
    fraction = fraction or ""
    digits = (whole + fraction).lstrip("0")
    if not digits:
        return ("number", (0, "0", "0"))
    trailing = len(digits) - len(digits.rstrip("0"))
    if trailing:
        digits = digits[:-trailing]
    exponent = add_small_to_decimal_integer(exponent or "0", trailing - len(fraction))
    return ("number", (-1 if sign else 1, digits, exponent))


def fail(status: int, code: str, message: str = "Request could not be completed"):
    raise ApiError(status, code, message)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_timestamp(value):
    if not isinstance(value, str) or not RFC3339_RE.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def timestamp_param(query, name):
    if name not in query:
        return None
    raw = query[name][0]
    parsed = parse_timestamp(raw)
    if parsed is None:
        fail(422, "validation_failed", f"{name} must be an RFC 3339 instant with an offset")
    return raw, parsed


def instant_key(value):
    return parse_timestamp(value).astimezone(timezone.utc)


def new_id(prefix: str) -> str:
    return prefix + "_" + secrets.token_hex(12)


def canonical(value) -> str:
    def normalize(item):
        if isinstance(item, RawJsonNumber):
            return normalize_raw_number(item.token)
        if item is None:
            return ("null",)
        if isinstance(item, bool):
            return ("boolean", item)
        if isinstance(item, (int, float, Decimal)):
            number = item if isinstance(item, Decimal) else Decimal(str(item))
            if not number.is_finite():
                return ("non-finite-number", str(number))
            sign, digits, exponent = number.as_tuple()
            digits = list(digits)
            if all(digit == 0 for digit in digits):
                return ("number", (0, "0", "0"))
            while len(digits) > 1 and digits[-1] == 0:
                digits.pop()
                exponent += 1
            return ("number", (-1 if sign else 1,
                               "".join(str(digit) for digit in digits), str(exponent)))
        if isinstance(item, str):
            return ("string", item)
        if isinstance(item, list):
            return ("array", tuple(normalize(v) for v in item))
        if isinstance(item, dict):
            return ("object", tuple(sorted((k, normalize(v)) for k, v in item.items())))
        raise ValueError("Request body contains a non-JSON value")
    return repr(normalize(value))


def integer_in_range(value, minimum, maximum):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if minimum <= value <= maximum else None
    if isinstance(value, Decimal):
        if (not value.is_finite() or value != value.to_integral_value()
                or value < minimum or value > maximum):
            return None
        return int(value)
    return None


def validate_financial_links(payments, requests):
    payment_by_id = {payment["id"]: payment for payment in payments}
    request_by_id = {request["id"]: request for request in requests}
    for payment in payments:
        request_id = payment.get("request_id")
        settlement_id = payment.get("settlement_id")
        if settlement_id is not None and (not isinstance(settlement_id, str) or
                                           not settlement_id or len(settlement_id) > 64):
            raise ValueError("invalid settlement link")
        if request_id is None:
            continue
        if (not isinstance(request_id, str) or not request_id or len(request_id) > 64 or
                settlement_id is not None):
            raise ValueError("invalid request payment link")
        request = request_by_id.get(request_id)
        if (request is None or request["status"] != "paid" or
                request.get("payment_id") != payment["id"] or
                request["payer_id"] != payment["from_user_id"] or
                request["requester_id"] != payment["to_user_id"] or
                request["amount"] != payment["amount"]):
            raise ValueError("inconsistent request payment link")
    for request in requests:
        payment_id = request.get("payment_id")
        if request["status"] == "paid":
            if not isinstance(payment_id, str) or payment_id not in payment_by_id:
                raise ValueError("paid request has no payment")
            payment = payment_by_id[payment_id]
            if payment.get("request_id") != request["id"]:
                raise ValueError("paid request has the wrong payment")
        elif payment_id is not None:
            raise ValueError("unpaid request has a payment")


def validate_refund_links(payments):
    """Refunds reverse a non-refund payment between the same wallets, within its amount."""
    payment_by_id = {payment["id"]: payment for payment in payments}
    refunded = {}
    for payment in payments:
        target_id = payment.get("refund_of")
        if target_id is None:
            continue
        target = payment_by_id.get(target_id) if isinstance(target_id, str) else None
        if (target is None or target.get("refund_of") is not None
                or payment["from_user_id"] != target["to_user_id"]
                or payment["to_user_id"] != target["from_user_id"]
                or payment["amount"] < 1
                or payment.get("request_id") is not None
                or payment.get("settlement_id") is not None
                or payment.get("authorization_id") is not None):
            raise ValueError("invalid refund link")
        refunded[target_id] = refunded.get(target_id, 0) + payment["amount"]
    for target_id, amount in refunded.items():
        revisions = payment_by_id[target_id].get("revisions") or []
        current = revisions[-1]["amount"] if revisions else payment_by_id[target_id]["amount"]
        if amount > current:
            raise ValueError("refunds exceed payment")


def amount_value(value, *, required=True) -> int:
    if value is None and not required:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        fail(422, "validation_failed", "amount must be an integer minor-unit value")
    if isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            fail(422, "validation_failed", "amount must be an integer minor-unit value")
        if value < 1 or value > MAX_AMOUNT:
            fail(422, "validation_failed", "amount is outside the allowed range")
        value = int(value)
    elif isinstance(value, float):
        if not value.is_integer():
            fail(422, "validation_failed", "amount must be an integer minor-unit value")
        value = int(value)
    if value < 1 or value > MAX_AMOUNT:
        fail(422, "validation_failed", "amount is outside the allowed range")
    return value


def required_string(body, name: str) -> str:
    if name not in body:
        fail(422, "validation_failed", f"{name} is required")
    if not isinstance(body[name], str):
        fail(400, "malformed_request", f"{name} must be a string")
    return body[name]


def optional_note(body) -> str:
    if "note" not in body:
        return ""
    value = body["note"]
    if not isinstance(value, str):
        fail(422, "validation_failed", "note must be a string")
    if len(value) > 200:
        fail(422, "validation_failed", "note exceeds 200 characters")
    return value


def visibility(body) -> str:
    value = body.get("visibility", "public")
    if value not in ("public", "private") or not isinstance(value, str):
        fail(422, "validation_failed", "visibility must be public or private")
    return value


def page_value(query, name, default, minimum, maximum=None):
    if name not in query:
        return default
    raw = query[name][0]
    if not re.fullmatch(r"[0-9]+", raw):
        fail(422, "validation_failed", f"{name} must be decimal digits")
    try:
        value = int(raw)
    except ValueError:
        fail(422, "validation_failed", f"{name} is outside the supported range")
    if value < minimum or (maximum is not None and value > maximum):
        fail(422, "validation_failed", f"{name} is outside the allowed range")
    return value


# Each scrypt call takes ~16 MiB and a full CPU for tens of milliseconds. Capping concurrent
# login/signup hashes keeps a burst of logins from starving every other request of CPU.
AUTH_HASH_SLOTS = threading.BoundedSemaphore(2)


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return salt.hex() + ":" + digest.hex()


def password_matches(password: str, encoded: str) -> bool:
    try:
        salt_hex, digest_hex = encoded.split(":", 1)
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def empty_state():
    return {"currency": "EUR", "minor_units": 2, "users": {}, "emails": {}, "handles": {},
            "tokens": {}, "payments": [], "requests": [], "operators": [],
            "idempotency": {}, "seeded_total": 0, "sequence": 0,
            "authorization_ttl_seconds": 600, "authorizations": [],
            "opening_balances": {}}


class Service:
    def __init__(self):
        self.lock = threading.RLock()
        self.state = empty_state()
        self.statement_snapshots = {}

    def _idempotency_slot(self, user_id, key, method, path):
        # The client key is scoped by caller and operation path; reuse on a different
        # endpoint is an independent request under the Stage 1 contract.
        return "\0".join((user_id, key, method, path))

    def _get_user(self, uid):
        return self.state["users"].get(uid)

    def _resolve(self, handle):
        if not HANDLE_RE.fullmatch(handle):
            fail(404, "not_found", "No user has that handle")
        uid = self.state["handles"].get(handle)
        if uid is None:
            fail(404, "not_found", "No user has that handle")
        return self.state["users"][uid]

    def _request_obj(self, item):
        return {"request_id": item["id"], "requester_id": item["requester_id"],
                "requester_handle": self.state["users"][item["requester_id"]]["handle"],
                "payer_id": item["payer_id"],
                "payer_handle": self.state["users"][item["payer_id"]]["handle"],
                "amount": item["amount"], "currency": self.state["currency"],
                "note": item["note"], "status": item["status"],
                "payment_id": item.get("payment_id"), "created_at": item["created_at"]}

    def _payment_obj(self, item):
        return {"payment_id": item["id"], "from_user_id": item["from_user_id"],
                "from_handle": self.state["users"][item["from_user_id"]]["handle"],
                "to_user_id": item["to_user_id"],
                "to_handle": self.state["users"][item["to_user_id"]]["handle"],
                "amount": item["amount"], "currency": self.state["currency"],
                "note": item["note"], "visibility": item["visibility"],
                "request_id": item.get("request_id"), "settlement_id": item.get("settlement_id"),
                "authorization_id": item.get("authorization_id"),
                "refund_of": item.get("refund_of"),
                "created_at": item["created_at"]}

    def _initial_revision(self, amount, created_at):
        return {"revision": 1, "amount": amount, "effective_at": created_at,
                "recorded_at": created_at, "reason": ""}

    def _payment_revisions(self, item, known_at=None):
        revisions = item.get("revisions") or [self._initial_revision(item["amount"], item["created_at"])]
        if known_at is None:
            return revisions
        known = parse_timestamp(known_at)
        return [revision for revision in revisions
                if parse_timestamp(revision["recorded_at"]) <= known]

    def _selected_revision(self, item, known_at=None, revision_overrides=None):
        revisions = (revision_overrides or {}).get(item["id"])
        if revisions is None:
            revisions = self._payment_revisions(item, known_at)
        elif known_at is not None:
            known = parse_timestamp(known_at)
            revisions = [revision for revision in revisions
                         if parse_timestamp(revision["recorded_at"]) <= known]
        return revisions[-1] if revisions else None

    def _opening_balance(self, uid):
        return self.state["opening_balances"].get(uid, 0)

    def _historical_total(self, uid, at, known_at=None, *, inclusive=True,
                          revision_overrides=None):
        balance = self._opening_balance(uid)
        at_dt = parse_timestamp(at) if isinstance(at, str) else at
        for payment in self.state["payments"]:
            revision = self._selected_revision(payment, known_at, revision_overrides)
            effective = parse_timestamp(revision["effective_at"]) if revision else None
            if (revision is None or effective > at_dt
                    or (not inclusive and effective == at_dt)):
                continue
            if payment["from_user_id"] == uid:
                balance -= revision["amount"]
            elif payment["to_user_id"] == uid:
                balance += revision["amount"]
        return balance

    def _hold_steps(self, authorization, payments_by_id):
        """Lifecycle steps of one hold as (event_at, known_at, kind, amount), oldest first.

        Creation comes first so a same-instant release of a seeded closed hold applies
        after it; the stable sort keeps insertion order for ties.
        """
        created = parse_timestamp(authorization["created_at"])
        steps = [(created, created, "create", authorization["amount"])]
        events = authorization.get("capture_events")
        if events is None:
            linked = [p for p in payments_by_id.values()
                      if p.get("authorization_id") == authorization["id"]]
            linked.sort(key=lambda p: (parse_timestamp(p["created_at"]), p["id"]))
            events = [{"payment_id": p["id"], "final":
                       authorization.get("status") == "captured" and p["id"] == authorization.get("payment_id")}
                      for p in linked]
        for event in events:
            payment = payments_by_id.get(event.get("payment_id"))
            if payment is None:
                continue
            event_at = parse_timestamp(payment["created_at"])
            steps.append((event_at, event_at, "release" if event.get("final") else "capture",
                          payment["amount"]))
        status = authorization.get("status")
        closed_at = parse_timestamp(authorization.get("closed_at"))
        if status == "voided" and closed_at is not None:
            steps.append((closed_at, closed_at, "release", 0))
        elif status in ("captured", "voided") and closed_at is None:
            steps.append((created, created, "release", 0))
        # The expiry deadline is known as soon as the creation is known.
        steps.append((parse_timestamp(authorization["expires_at"]), created, "release", 0))
        # A seeded closed hold without created_at is created at reset, possibly after its
        # recorded close. Nothing happens to a hold before it exists, so such steps take
        # effect at creation; the hold then releases there instead of staying open forever.
        steps = [steps[0]] + [(max(event_at, created), max(known_at, created), kind, amount)
                              for event_at, known_at, kind, amount in steps[1:]]
        steps.sort(key=lambda step: step[0])
        return steps

    @staticmethod
    def _apply_hold_step(held, kind, amount):
        if kind == "create":
            return amount
        if kind == "capture":
            return max(0, held - amount)
        return 0

    def _authorization_hold_at(self, authorization, at, known_at, payments_by_id):
        held = 0
        for event_at, step_known_at, kind, amount in self._hold_steps(authorization, payments_by_id):
            if event_at > at or step_known_at > known_at:
                continue
            held = self._apply_hold_step(held, kind, amount)
        return held

    def _historical_held(self, uid, at, known_at=None):
        known = parse_timestamp(known_at) if known_at else datetime.max.replace(tzinfo=timezone.utc)
        payments_by_id = {p["id"]: p for p in self.state["payments"]}
        return sum(self._authorization_hold_at(a, at, known, payments_by_id)
                   for a in self.state["authorizations"] if a["from_user_id"] == uid)

    def _record_historical_state_is_solvent(self, revision_overrides=None, *, include_holds=True):
        """Sweep every effective/event boundary under the latest revisions.

        Balances are checked after all movements at one instant have been combined.
        """
        changes = {}

        def move(at, uid, total=0, held=0):
            slot = changes.setdefault(at, {})
            prior_total, prior_held = slot.get(uid, (0, 0))
            slot[uid] = (prior_total + total, prior_held + held)

        for payment in self.state["payments"]:
            revisions = (revision_overrides or {}).get(payment["id"])
            if revisions is None:
                revisions = self._payment_revisions(payment)
            revision = revisions[-1]
            if revision["amount"]:
                at = parse_timestamp(revision["effective_at"])
                move(at, payment["from_user_id"], total=-revision["amount"])
                move(at, payment["to_user_id"], total=revision["amount"])
        if include_holds:
            payments_by_id = {p["id"]: p for p in self.state["payments"]}
            for authorization in self.state["authorizations"]:
                held = 0
                for event_at, _, kind, amount in self._hold_steps(authorization, payments_by_id):
                    updated = self._apply_hold_step(held, kind, amount)
                    if updated != held:
                        move(event_at, authorization["from_user_id"], held=updated - held)
                        held = updated
        totals = {uid: self._opening_balance(uid) for uid in self.state["users"]}
        holds = {uid: 0 for uid in self.state["users"]}
        for at in sorted(changes):
            for uid, (total, held) in changes[at].items():
                totals[uid] += total
                holds[uid] += held
            for uid in changes[at]:
                if totals[uid] < 0 or totals[uid] - holds[uid] < 0:
                    return False
        return True

    def _append_payment(self, payment):
        payment["revisions"] = [self._initial_revision(payment["amount"], payment["created_at"])]
        self.state["payments"].append(payment)
        return payment

    def _correct_payment(self, uid, payment_id, body, headers):
        payment = next((p for p in self.state["payments"] if p["id"] == payment_id), None)
        if payment is None:
            fail(404, "not_found", "Payment not found")
        if payment["from_user_id"] != uid:
            fail(403, "forbidden", "Only the original sender may correct this payment")
        self._check_correctable(payment, allow_settlement=False)

        status, replay, signature = self._key_and_replay(
            headers, uid, "POST",
            f"/payments/{payment_id}/corrections", body)
        if status is not None:
            return status, replay

        plan = self._plan_correction(payment, body, datetime.now(timezone.utc))
        response = self._commit_corrections([plan])["revisions"][0]
        self._save_idempotency(signature, response)
        return 201, response

    def _find_payment(self, payment_id):
        payment = next((p for p in self.state["payments"] if p["id"] == payment_id), None)
        if payment is None:
            fail(404, "not_found", "Payment not found")
        return payment

    def _refunded_amount(self, payment_id):
        return sum(p["amount"] for p in self.state["payments"] if p.get("refund_of") == payment_id)

    def _check_correctable(self, payment, *, allow_settlement):
        if (payment.get("authorization_id") is not None or payment.get("refund_of") is not None
                or (payment.get("settlement_id") is not None and not allow_settlement)):
            fail(422, "linked_payment_immutable", "Linked payments cannot be corrected")

    def _plan_correction(self, payment, body, request_started):
        """Validate one correction against the payment's current revision."""
        expected_revision = body.get("expected_revision")
        amount = integer_in_range(body.get("amount"), 0, MAX_AMOUNT)
        effective_at = body.get("effective_at")
        reason = body.get("reason")
        effective = parse_timestamp(effective_at)
        if (isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
                or expected_revision < 1 or amount is None
                or effective is None or effective > request_started
                or not isinstance(reason, str) or not 1 <= len(reason) <= 200):
            fail(422, "validation_failed", "Invalid payment correction")
        revisions = self._payment_revisions(payment)
        if expected_revision != len(revisions):
            fail(409, "stale_revision", "Payment revision is stale")
        if amount < self._refunded_amount(payment["id"]):
            fail(422, "refund_exceeds_payment", "Payment would be smaller than its refunds")
        return {"payment": payment, "revisions": revisions, "amount": amount,
                "effective_at": effective_at, "reason": reason}

    def _commit_corrections(self, plans, batch_id=None):
        """Check the combined effect of validated corrections, then apply them atomically.

        Current available funds are checked before historical balances. Every new revision
        shares one recorded_at, later than the latest recorded revision of every payment.
        """
        changes = {}
        for plan in plans:
            payment = plan["payment"]
            delta = plan["amount"] - plan["revisions"][-1]["amount"]
            changes[payment["from_user_id"]] = changes.get(payment["from_user_id"], 0) - delta
            changes[payment["to_user_id"]] = changes.get(payment["to_user_id"], 0) + delta
        balances = {uid: self.state["users"][uid]["balance"] + change
                    for uid, change in changes.items()}
        if any(change < 0 and balances[uid] - self._held(uid) < 0
               for uid, change in changes.items()):
            fail(409, "insufficient_funds", "Insufficient available funds")
        if any(value > MAX_BALANCE for value in balances.values()):
            fail(422, "validation_failed", "Resulting balance exceeds the supported range")

        recorded_dt = datetime.now(timezone.utc)
        for plan in plans:
            last_recorded = parse_timestamp(plan["revisions"][-1]["recorded_at"])
            if recorded_dt <= last_recorded:
                recorded_dt = last_recorded + timedelta(microseconds=1)
        recorded_at = recorded_dt.isoformat()
        overrides, created = {}, []
        for plan in plans:
            revision = {"revision": len(plan["revisions"]) + 1, "amount": plan["amount"],
                        "effective_at": plan["effective_at"], "recorded_at": recorded_at,
                        "reason": plan["reason"]}
            if batch_id is not None:
                revision["correction_batch_id"] = batch_id
            overrides[plan["payment"]["id"]] = plan["revisions"] + [revision]
            created.append(revision)
        if not self._record_historical_state_is_solvent(overrides):
            fail(409, "historical_overdraft", "Correction would make a historical balance negative")

        for uid, value in balances.items():
            self.state["users"][uid]["balance"] = value
        revisions = []
        for plan, revision in zip(plans, created):
            plan["payment"]["revisions"] = overrides[plan["payment"]["id"]]
            revisions.append({"payment_id": plan["payment"]["id"], **revision})
        return {"recorded_at": recorded_at, "revisions": revisions}

    def _correct_batch(self, uid, body, signature):
        if uid not in self.state["operators"]:
            fail(403, "forbidden", "Settlement operator permission required")
        items = body.get("corrections")
        if (not isinstance(items, list) or not 1 <= len(items) <= 32
                or any(not isinstance(item, dict) for item in items)):
            fail(422, "validation_failed", "corrections must contain 1 to 32 objects")
        payment_ids = [item.get("payment_id") for item in items]
        if (any(not isinstance(pid, str) for pid in payment_ids)
                or len(set(payment_ids)) != len(payment_ids)):
            fail(422, "validation_failed", "corrections need distinct payment_id strings")

        request_started = datetime.now(timezone.utc)
        plans = []
        for item in items:
            payment = self._find_payment(item["payment_id"])
            self._check_correctable(payment, allow_settlement=True)
            plans.append(self._plan_correction(payment, item, request_started))

        members = {}
        for plan in plans:
            settlement_id = plan["payment"].get("settlement_id")
            if settlement_id is not None:
                members.setdefault(settlement_id, []).append(plan)
        included = set(payment_ids)
        for settlement_id in members:
            if any(p["id"] not in included for p in self.state["payments"]
                   if p.get("settlement_id") == settlement_id):
                fail(422, "incomplete_settlement", "Every settlement member must be corrected together")
        for plans_in_settlement in members.values():
            if len({parse_timestamp(plan["effective_at"]) for plan in plans_in_settlement}) != 1:
                fail(422, "validation_failed", "Settlement members need one effective instant")

        batch_id = new_id("cb")
        committed = self._commit_corrections(plans, batch_id)
        response = {"correction_batch_id": batch_id, "recorded_at": committed["recorded_at"],
                    "revisions": committed["revisions"]}
        self._save_idempotency(signature, response)
        return 201, response

    def _refund_payment(self, uid, payment_id, body, signature):
        payment = self._find_payment(payment_id)
        if payment["to_user_id"] != uid:
            fail(403, "forbidden", "Only the original receiver may refund this payment")
        if payment.get("refund_of") is not None:
            fail(422, "invalid_refund_target", "A refund cannot be refunded")
        amount = amount_value(body.get("amount"))
        if self._refunded_amount(payment_id) + amount > self._payment_revisions(payment)[-1]["amount"]:
            fail(422, "refund_exceeds_payment", "Refunds would exceed the payment amount")
        refund = self._make_payment(uid, payment["from_user_id"], amount, payment["note"],
                                    payment["visibility"], refund_of=payment_id)
        response = self._payment_obj(refund)
        self._save_idempotency(signature, response)
        return 201, response

    def _me_at(self, uid, query, read_at):
        as_of = timestamp_param(query, "as_of")
        known_at = timestamp_param(query, "known_at")
        user = self.state["users"][uid]
        if as_of is None and known_at is None:
            # Without temporal parameters the live ledger already holds the current
            # corrected values, exactly as stage 2 reported them.
            total, held = user["balance"], self._held(uid)
        else:
            known = min(known_at[1], read_at) if known_at else read_at
            at = as_of[1] if as_of else read_at
            known_filter = known.isoformat()
            total = self._historical_total(uid, at, known_filter)
            held = self._historical_held(uid, at, known_filter)
        result = {"user_id": uid, "display_name": user["display_name"],
                  "handle": user["handle"], "balance": total, "total": total,
                  "available": total - held, "held": held,
                  "currency": self.state["currency"], "minor_units": self.state["minor_units"]}
        if as_of:
            result["as_of"] = as_of[0]
        if known_at:
            result["known_at"] = known_at[0]
        return result

    def _statement(self, uid, query, read_at):
        limit = page_value(query, "limit", 50, 1, 200)
        offset = page_value(query, "offset", 0, 0)
        snapshot_tokens = query.get("snapshot")
        if snapshot_tokens:
            if any(name in query for name in ("from", "to", "known_at")):
                fail(422, "validation_failed", "A snapshot cannot be combined with a window or known_at")
            snapshot = self.statement_snapshots.get(snapshot_tokens[0])
            if snapshot is None or snapshot["user_id"] != uid:
                fail(404, "not_found", "Statement snapshot not found")
            return self._statement_page(snapshot["result"], limit, offset)

        from_value = timestamp_param(query, "from")
        to_value = timestamp_param(query, "to")
        known_value = timestamp_param(query, "known_at")
        lower = from_value[1] if from_value else None
        upper_raw = to_value[0] if to_value else read_at.isoformat()
        upper = parse_timestamp(upper_raw)
        known = min(known_value[1], read_at) if known_value else read_at
        known_filter = known.isoformat()
        if lower is not None and lower > upper:
            fail(422, "validation_failed", "from must be earlier than to")

        selected = []
        for payment in self.state["payments"]:
            if uid not in (payment["from_user_id"], payment["to_user_id"]):
                continue
            revision = self._selected_revision(payment, known_filter)
            if revision is None:
                continue
            effective = parse_timestamp(revision["effective_at"])
            if ((lower is not None and effective < lower) or effective >= upper):
                continue
            payment_obj = self._payment_obj(payment)
            payment_obj["amount"] = revision["amount"]
            delta = revision["amount"] * (
                1 if payment["to_user_id"] == uid else -1)
            selected.append((effective, payment["id"], payment_obj, delta, revision))
        selected.sort(key=lambda item: (item[0], item[1]))

        opening = (self._opening_balance(uid) if lower is None else
                   self._historical_total(uid, lower, known_filter,
                                          inclusive=False))
        closing = self._historical_total(uid, upper, known_filter,
                                         inclusive=False)
        running = opening
        entries = []
        for effective, payment_id, payment_obj, delta, revision in selected:
            running += delta
            entries.append({"payment": payment_obj, "delta": delta,
                            "balance_after": running, "revision": revision["revision"],
                            "effective_at": revision["effective_at"],
                            "recorded_at": revision["recorded_at"]})
        result = {"opening_balance": opening, "entries": entries,
                  "closing_balance": closing, "has_more": False}
        if known_value:
            result["known_at"] = known_value[0]
        if from_value:
            result["from"] = from_value[0]
        result["to"] = upper_raw
        snapshot_token = secrets.token_urlsafe(32)
        result["snapshot"] = snapshot_token
        self.statement_snapshots[snapshot_token] = {
            "user_id": uid, "result": copy.deepcopy(result)}
        return self._statement_page(result, limit, offset)

    def _statement_page(self, source, limit, offset):
        result = copy.deepcopy(source)
        entries = result["entries"]
        result["entries"] = entries[offset:offset + limit]
        result["has_more"] = offset + limit < len(entries)
        return result

    def _authorization_remaining(self, item):
        return item["amount"] - item.get("captured_amount", 0)

    def _held(self, uid):
        return sum(self._authorization_remaining(item) for item in self.state["authorizations"]
                   if item["from_user_id"] == uid and item["status"] == "open")

    def _available(self, uid):
        return self.state["users"][uid]["balance"] - self._held(uid)

    def _expire_authorizations(self, at=None):
        instant = at or datetime.now(timezone.utc)
        for item in self.state["authorizations"]:
            if (item["status"] == "open" and
                    (parse_timestamp(item["expires_at"]) or instant) <= instant):
                item["status"] = "expired"
                item["closed_at"] = item["expires_at"]

    def _authorization_obj(self, item):
        return {"authorization_id": item["id"],
                "from_user_id": item["from_user_id"],
                "from_handle": self.state["users"][item["from_user_id"]]["handle"],
                "to_user_id": item["to_user_id"],
                "to_handle": self.state["users"][item["to_user_id"]]["handle"],
                "amount": item["amount"],
                "captured_amount": item.get("captured_amount", 0),
                "currency": self.state["currency"], "note": item["note"],
                "visibility": item["visibility"], "status": item["status"],
                "expires_at": item["expires_at"],
                "payment_id": item.get("payment_id"),
                "payment_ids": list(item.get("payment_ids", [])),
                "remaining_amount": (self._authorization_remaining(item)
                                      if item["status"] == "open" else 0),
                "created_at": item["created_at"],
                "closed_at": item.get("closed_at")}

    def _make_request(self, requester_id, payer_id, amount, note, created=None, rid=None):
        item = {"id": rid or new_id("rq"), "requester_id": requester_id,
                "payer_id": payer_id, "amount": amount, "note": note,
                "status": "pending", "payment_id": None, "created_at": created or now()}
        self.state["requests"].append(item)
        return item

    def _make_payment(self, from_id, to_id, amount, note, vis, request_id=None,
                      settlement_id=None, authorization_id=None, created=None,
                      reserved_authorization=None, refund_of=None):
        sender, receiver = self.state["users"][from_id], self.state["users"][to_id]
        if reserved_authorization is None and self._available(from_id) < amount:
            fail(409, "insufficient_funds", "Insufficient funds")
        if sender["balance"] < amount:
            fail(409, "insufficient_funds", "Insufficient funds")
        if receiver["balance"] + amount > MAX_BALANCE:
            fail(422, "validation_failed", "Resulting balance exceeds the supported range")
        # Check both sides before either write so a rejected transfer is mutation-free.
        sender["balance"] -= amount
        receiver["balance"] += amount
        item = {"id": new_id("p"), "from_user_id": from_id, "to_user_id": to_id,
                "amount": amount, "note": note, "visibility": vis,
                "request_id": request_id, "settlement_id": settlement_id,
                "authorization_id": authorization_id, "refund_of": refund_of,
                "created_at": created or now()}
        return self._append_payment(item)

    def _key_and_replay(self, headers, uid, method, path, body):
        key = headers.get("Idempotency-Key", "")
        if not key:
            fail(400, "missing_idempotency_key", "Idempotency-Key is required")
        if len(key) > 255:
            fail(422, "validation_failed", "Idempotency-Key exceeds 255 characters")
        slot = self._idempotency_slot(uid, key, method, path)
        prior = self.state["idempotency"].get(slot)
        request_body = canonical(body)
        if prior is not None:
            if prior["method"] != method or prior["path"] != path or prior["body"] != request_body:
                fail(409, "idempotency_key_reuse", "Idempotency-Key was already used for another request")
            return 200, copy.deepcopy(prior["response"]), None
        return None, None, (slot, method, path, request_body)

    def _save_idempotency(self, signature, response):
        slot, method, path, request_body = signature
        self.state["idempotency"][slot] = {"method": method, "path": path,
                                            "body": request_body,
                                            "response": copy.deepcopy(response)}

    def _create_payment(self, uid, body, signature):
        amount = amount_value(body.get("amount"))
        handle = required_string(body, "to_handle")
        note, vis = optional_note(body), visibility(body)
        if handle == self.state["users"][uid]["handle"]:
            fail(422, "self_payment", "A wallet cannot pay itself")
        receiver = self._resolve(handle)
        payment = self._make_payment(uid, receiver["id"], amount, note, vis)
        response = self._payment_obj(payment)
        self._save_idempotency(signature, response)
        return 201, response

    def _create_authorization(self, uid, body, signature):
        amount = amount_value(body.get("amount"))
        handle = required_string(body, "to_handle")
        note, vis = optional_note(body), visibility(body)
        if handle == self.state["users"][uid]["handle"]:
            fail(422, "self_payment", "A wallet cannot authorize itself")
        receiver = self._resolve(handle)
        if self._available(uid) < amount:
            fail(409, "insufficient_funds", "Insufficient available funds")
        created_dt = datetime.now(timezone.utc)
        try:
            expires_dt = created_dt + timedelta(seconds=self.state["authorization_ttl_seconds"])
        except OverflowError:
            fail(422, "validation_failed", "Authorization lifetime is outside the supported range")
        item = {"id": new_id("a"), "from_user_id": uid, "to_user_id": receiver["id"],
                "amount": amount, "captured_amount": 0, "note": note,
                "visibility": vis, "status": "open", "expires_at": expires_dt.isoformat(),
                "payment_id": None, "payment_ids": [], "created_at": created_dt.isoformat(),
                "closed_at": None, "capture_events": []}
        self.state["authorizations"].append(item)
        response = self._authorization_obj(item)
        self._save_idempotency(signature, response)
        return 201, response

    def _capture_authorization(self, uid, authorization_id, body, signature):
        item = next((a for a in self.state["authorizations"] if a["id"] == authorization_id), None)
        if item is None:
            fail(404, "not_found", "Authorization not found")
        if item["to_user_id"] != uid:
            fail(403, "forbidden", "Only the recipient may capture this authorization")
        if item["status"] == "expired" or (parse_timestamp(item["expires_at"]) or datetime.min.replace(
                tzinfo=timezone.utc)) <= datetime.now(timezone.utc):
            item["status"] = "expired"
            fail(409, "authorization_expired", "Authorization has expired")
        if item["status"] != "open":
            fail(409, "authorization_not_open", "Authorization is not open")
        remaining = self._authorization_remaining(item)
        if "amount" in body:
            requested = body["amount"]
            # Establish that the supplied amount is a positive integer first.
            # Only well-formed minor-unit amounts get the authorization-specific
            # over-capture error; e.g. 2000.5 is a malformed amount even when it
            # is also greater than a 2000-unit remainder.
            numeric = None
            if isinstance(requested, int) and not isinstance(requested, bool):
                numeric = requested
            elif isinstance(requested, float):
                if math.isfinite(requested) and requested.is_integer():
                    numeric = int(requested)
            elif isinstance(requested, Decimal):
                if requested.is_finite() and requested == requested.to_integral_value():
                    numeric = requested
            elif isinstance(requested, RawJsonNumber):
                try:
                    parsed = Decimal(requested.token)
                    if parsed.is_finite() and parsed == parsed.to_integral_value():
                        numeric = parsed
                except (InvalidOperation, ValueError):
                    pass
            if numeric is None or numeric < 1:
                fail(422, "validation_failed", "amount must be a positive integer minor-unit value")
            if numeric > remaining:
                fail(422, "capture_exceeds_authorization", "Capture exceeds the remaining authorization")
            amount = int(numeric)
        else:
            amount = remaining
        if amount > remaining:
            fail(422, "capture_exceeds_authorization", "Capture exceeds the remaining authorization")
        final = body.get("final", True)
        if not isinstance(final, bool):
            fail(400, "malformed_request", "final must be a boolean")
        payment = self._make_payment(
            item["from_user_id"], item["to_user_id"], amount, item["note"], item["visibility"],
            authorization_id=item["id"], reserved_authorization=item)
        item["captured_amount"] = item.get("captured_amount", 0) + amount
        item.setdefault("payment_ids", []).append(payment["id"])
        item["payment_id"] = payment["id"]
        closed = final or self._authorization_remaining(item) == 0
        item.setdefault("capture_events", []).append(
            {"payment_id": payment["id"], "final": closed})
        if closed:
            item["status"] = "captured"
            item["closed_at"] = payment["created_at"]
        response = self._payment_obj(payment)
        self._save_idempotency(signature, response)
        return 201, response

    def _void_authorization(self, uid, authorization_id):
        item = next((a for a in self.state["authorizations"] if a["id"] == authorization_id), None)
        if item is None:
            fail(404, "not_found", "Authorization not found")
        if item["from_user_id"] != uid:
            fail(403, "forbidden", "Only the payer may void this authorization")
        if item["status"] == "voided":
            return 200, self._authorization_obj(item)
        if item["status"] != "open":
            fail(409, "authorization_not_open", "Authorization is not open")
        item["status"] = "voided"
        item["closed_at"] = now()
        return 200, self._authorization_obj(item)

    def _create_request(self, uid, body, signature):
        amount = amount_value(body.get("amount"))
        handle = required_string(body, "payer_handle")
        note = optional_note(body)
        if handle == self.state["users"][uid]["handle"]:
            fail(422, "self_request", "A user cannot request money from themselves")
        payer = self._resolve(handle)
        item = self._make_request(uid, payer["id"], amount, note)
        response = self._request_obj(item)
        self._save_idempotency(signature, response)
        return 201, response

    def _pay_request(self, uid, rid, body, signature):
        vis = visibility(body)
        item = next((r for r in self.state["requests"] if r["id"] == rid), None)
        if item is None:
            fail(404, "not_found", "Request not found")
        if item["payer_id"] != uid:
            fail(403, "forbidden", "Only the payer may pay this request")
        if item["status"] != "pending":
            fail(409, "request_not_pending", "Request is not pending")
        payment = self._make_payment(uid, item["requester_id"], item["amount"], item["note"], vis,
                                     request_id=rid)
        item["status"], item["payment_id"] = "paid", payment["id"]
        response = self._payment_obj(payment)
        self._save_idempotency(signature, response)
        return 201, response

    def _create_split(self, uid, body, signature):
        amount = amount_value(body.get("amount"))
        raw_handles = body.get("participant_handles")
        if not isinstance(raw_handles, list) or not raw_handles:
            fail(422, "validation_failed", "participant_handles must be a non-empty array")
        if any(not isinstance(h, str) for h in raw_handles):
            fail(422, "validation_failed", "participant handles must be strings")
        if len(set(raw_handles)) != len(raw_handles):
            fail(422, "validation_failed", "participant handles must be unique")
        note = optional_note(body)
        people = [self._resolve(h) for h in raw_handles]
        quotient, remainder = divmod(amount, len(people))
        shares = [{"handle": person["handle"], "amount": quotient + (1 if i < remainder else 0)}
                  for i, person in enumerate(people)]
        ridx = uid
        requests = []
        for person, share in zip(people, shares):
            if person["id"] != ridx:
                requests.append(self._request_obj(self._make_request(uid, person["id"],
                                                                      share["amount"], note)))
        response = {"split_id": new_id("sp"), "amount": amount,
                    "currency": self.state["currency"], "note": note,
                    "shares": shares, "requests": requests, "created_at": now()}
        self._save_idempotency(signature, response)
        return 201, response

    def _settle(self, uid, body, signature):
        if uid not in self.state["operators"]:
            fail(403, "forbidden", "Settlement operator permission required")
        transfers = body.get("transfers")
        if not isinstance(transfers, list) or not 1 <= len(transfers) <= 32:
            fail(422, "validation_failed", "transfers must contain 1 to 32 entries")
        entries = []
        for transfer in transfers:
            if not isinstance(transfer, dict):
                fail(422, "validation_failed", "Each transfer must be an object")
            amount = amount_value(transfer.get("amount"))
            if "from_handle" not in transfer or "to_handle" not in transfer:
                fail(422, "validation_failed", "Each transfer requires from_handle and to_handle")
            from_handle, to_handle = transfer["from_handle"], transfer["to_handle"]
            if not isinstance(from_handle, str) or not isinstance(to_handle, str):
                fail(400, "malformed_request", "Transfer handles must be strings")
            note, vis = optional_note(transfer), visibility(transfer)
            if from_handle == to_handle:
                fail(422, "self_payment", "A wallet cannot pay itself")
            sender, receiver = self._resolve(from_handle), self._resolve(to_handle)
            entries.append((sender["id"], receiver["id"], amount, note, vis))
        net = {u["id"]: 0 for u in self.state["users"].values()}
        for sender, receiver, amount, _, _ in entries:
            net[sender] -= amount
            net[receiver] += amount
        totals = {u["id"]: u["balance"] + net[u["id"]]
                  for u in self.state["users"].values()}
        if any(totals[uid] - self._held(uid) < 0 for uid in totals):
            fail(409, "insufficient_funds", "Settlement is not affordable")
        if any(v < 0 for v in totals.values()):
            fail(409, "insufficient_funds", "Settlement is not affordable")
        if any(v > MAX_BALANCE for v in totals.values()):
            fail(422, "validation_failed", "Settlement balance exceeds the supported range")
        settlement_id, committed_at = new_id("st"), now()
        payments = []
        for sender, receiver, amount, note, vis in entries:
            item = {"id": new_id("p"), "from_user_id": sender, "to_user_id": receiver,
                    "amount": amount, "note": note, "visibility": vis,
                    "request_id": None, "settlement_id": settlement_id,
                    "authorization_id": None, "refund_of": None,
                    "created_at": committed_at}
            item["revisions"] = [self._initial_revision(amount, committed_at)]
            payments.append(item)
        # Commit only after every entry and every net balance has passed validation.
        for user in self.state["users"].values():
            user["balance"] = totals[user["id"]]
        self.state["payments"].extend(payments)
        response = {"settlement_id": settlement_id, "committed_at": committed_at,
                    "payments": [self._payment_obj(p) for p in payments]}
        self._save_idempotency(signature, response)
        return 201, response

    def _serialize_state(self):
        return copy.deepcopy(self.state)

    def _validate_import(self, document):
        version = document.get("format_version") if isinstance(document, dict) else None
        if (not isinstance(document, dict) or document.get("track") != "pocketful"
                or not isinstance(version, int) or isinstance(version, bool) or version != 1):
            fail(422, "validation_failed", "Invalid export document")
        source_state = document.get("state")
        if not isinstance(source_state, dict):
            fail(422, "validation_failed", "Invalid export state")
        legacy_keys = {"currency", "minor_units", "users", "emails", "handles", "tokens",
                       "payments", "requests", "operators", "idempotency", "seeded_total", "sequence"}
        stage2_keys = legacy_keys | {"authorization_ttl_seconds", "authorizations"}
        stage3_keys = stage2_keys | {"opening_balances"}
        legacy_stage3_keys = legacy_keys | {"opening_balances"}
        if set(source_state) not in (legacy_keys, stage2_keys, stage3_keys, legacy_stage3_keys):
            fail(422, "validation_failed", "Invalid export state")
        state = copy.deepcopy(source_state)
        if "authorization_ttl_seconds" not in source_state:
            state["authorization_ttl_seconds"] = 600
        if "authorizations" not in source_state:
            state["authorizations"] = []
        stage3_export = "opening_balances" in source_state
        # Validate referential and arithmetic integrity while preserving byte-for-byte values.
        try:
            if not isinstance(state["users"], dict) or not isinstance(state["payments"], list) or not isinstance(state["requests"], list):
                raise ValueError()
            if (not isinstance(state["tokens"], dict) or not isinstance(state["idempotency"], dict)
                    or not isinstance(state["emails"], dict) or not isinstance(state["handles"], dict)):
                raise ValueError()
            ttl = state["authorization_ttl_seconds"]
            if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 1:
                raise ValueError()
            if not isinstance(state["authorizations"], list):
                raise ValueError()
            if (not isinstance(state["minor_units"], int) or isinstance(state["minor_units"], bool)
                    or (state["currency"], state["minor_units"]) not in (("EUR", 2), ("JPY", 0), ("BHD", 3))):
                raise ValueError()
            balances = 0
            for uid, user in state["users"].items():
                if (not isinstance(uid, str) or not uid or len(uid) > 64 or not isinstance(user, dict)
                        or user.get("id") != uid or not isinstance(user.get("email"), str)
                        or not isinstance(user.get("display_name"), str)
                        or not isinstance(user.get("handle"), str)
                        or not HANDLE_RE.fullmatch(user["handle"])):
                    raise ValueError()
                if not isinstance(user.get("balance"), int) or isinstance(user["balance"], bool) or not 0 <= user["balance"] <= MAX_BALANCE:
                    raise ValueError()
                if (not isinstance(user.get("password_hash"), str)
                        or not PASSWORD_HASH_RE.fullmatch(user["password_hash"])):
                    raise ValueError()
                balances += user["balance"]
            if len(state["emails"]) != len(state["users"]) or len(state["handles"]) != len(state["users"]):
                raise ValueError()
            for email, uid in state["emails"].items():
                if uid not in state["users"] or state["users"][uid].get("email") != email:
                    raise ValueError()
            for handle, uid in state["handles"].items():
                if uid not in state["users"] or state["users"][uid].get("handle") != handle:
                    raise ValueError()
            for token, uid in state["tokens"].items():
                if not token or uid not in state["users"]:
                    raise ValueError()
            payment_ids = set()
            payment_by_id = {}
            for p in state["payments"]:
                if (not isinstance(p, dict) or not isinstance(p.get("id"), str) or not p["id"]
                        or len(p["id"]) > 64 or p["id"] in payment_ids
                        or not isinstance(p.get("from_user_id"), str)
                        or not isinstance(p.get("to_user_id"), str)
                        or p["from_user_id"] not in state["users"]
                        or p["to_user_id"] not in state["users"]
                        or p["from_user_id"] == p["to_user_id"]
                        or not isinstance(p.get("amount"), int) or isinstance(p["amount"], bool)
                        or not 0 <= p["amount"] <= MAX_AMOUNT
                        or (p["amount"] == 0 and not isinstance(p.get("request_id"), str))
                        or not isinstance(p.get("note"), str)
                        or len(p["note"]) > 200 or p.get("visibility") not in ("public", "private")
                        or parse_timestamp(p.get("created_at")) is None):
                    raise ValueError()
                revisions = p.get("revisions")
                if revisions is None:
                    revisions = [self._initial_revision(p["amount"], p["created_at"])]
                    p["revisions"] = revisions
                if not isinstance(revisions, list) or not revisions:
                    raise ValueError()
                previous_recorded = None
                for index, revision in enumerate(revisions, 1):
                    if (not isinstance(revision, dict)
                            or revision.get("revision") != index
                            or isinstance(revision.get("revision"), bool)
                            or not isinstance(revision.get("amount"), int)
                            or isinstance(revision.get("amount"), bool)
                            or not 0 <= revision["amount"] <= MAX_AMOUNT
                            or parse_timestamp(revision.get("effective_at")) is None
                            or parse_timestamp(revision.get("recorded_at")) is None
                            or not isinstance(revision.get("reason"), str)
                            or len(revision["reason"]) > 200):
                        raise ValueError()
                    batch_id = revision.get("correction_batch_id")
                    if batch_id is not None and (index == 1 or not isinstance(batch_id, str)
                                                 or not batch_id or len(batch_id) > 64):
                        raise ValueError()
                    if index == 1:
                        if (revision["amount"] != p["amount"]
                                or revision["effective_at"] != p["created_at"]
                                or revision["recorded_at"] != p["created_at"]
                                or revision["reason"] != ""):
                            raise ValueError()
                    elif (len(revision["reason"]) < 1
                          or parse_timestamp(revision["recorded_at"]) <= previous_recorded
                          or parse_timestamp(revision["effective_at"])
                             > parse_timestamp(revision["recorded_at"])):
                        raise ValueError()
                    previous_recorded = parse_timestamp(revision["recorded_at"])
                # Settlement members may carry batch revisions; captures and refunds never change.
                if ((p.get("authorization_id") is not None or p.get("refund_of") is not None)
                        and len(revisions) != 1):
                    raise ValueError()
                payment_ids.add(p["id"])
                payment_by_id[p["id"]] = p
            validate_refund_links(state["payments"])
            request_ids = set()
            for r in state["requests"]:
                if (not isinstance(r, dict) or not isinstance(r.get("id"), str) or not r["id"]
                        or len(r["id"]) > 64 or r["id"] in request_ids
                        or not isinstance(r.get("requester_id"), str)
                        or not isinstance(r.get("payer_id"), str)
                        or r["requester_id"] not in state["users"]
                        or r["payer_id"] not in state["users"]
                        or r["requester_id"] == r["payer_id"]
                        or not isinstance(r.get("amount"), int) or isinstance(r["amount"], bool)
                        or not 0 <= r["amount"] <= MAX_AMOUNT
                        or not isinstance(r.get("status"), str) or r["status"] not in STATUSES
                        or not isinstance(r.get("note"), str) or len(r["note"]) > 200
                        or parse_timestamp(r.get("created_at")) is None):
                    raise ValueError()
                request_ids.add(r["id"])
            validate_financial_links(state["payments"], state["requests"])
            authorization_ids = set()
            authorization_by_id = {}
            held_by_user = {uid: 0 for uid in state["users"]}
            for a in state["authorizations"]:
                if (not isinstance(a, dict) or not isinstance(a.get("id"), str) or not a["id"]
                        or len(a["id"]) > 64 or a["id"] in authorization_ids
                        or not isinstance(a.get("from_user_id"), str)
                        or not isinstance(a.get("to_user_id"), str)
                        or a["from_user_id"] not in state["users"]
                        or a["to_user_id"] not in state["users"]
                        or a["from_user_id"] == a["to_user_id"]
                        or not isinstance(a.get("amount"), int) or isinstance(a["amount"], bool)
                        or not 1 <= a["amount"] <= MAX_AMOUNT
                        or not isinstance(a.get("captured_amount", 0), int)
                        or isinstance(a.get("captured_amount", 0), bool)
                        or not 0 <= a.get("captured_amount", 0) <= a["amount"]
                        or a.get("status") not in ("open", "captured", "voided", "expired")
                        or not isinstance(a.get("note", ""), str)
                        or len(a.get("note", "")) > 200
                        or a.get("visibility", "public") not in ("public", "private")
                        or not isinstance(a.get("visibility", "public"), str)
                        or parse_timestamp(a.get("expires_at")) is None
                        or ("created_at" in a and parse_timestamp(a.get("created_at")) is None)):
                    raise ValueError()
                authorization_ids.add(a["id"])
                authorization_by_id[a["id"]] = a
                a.setdefault("captured_amount", 0)
                a.setdefault("note", "")
                a.setdefault("visibility", "public")
                a.setdefault("created_at", now())
                a.setdefault("payment_ids", [a["payment_id"]] if a.get("payment_id") else [])
                a.setdefault("payment_id", a["payment_ids"][-1] if a["payment_ids"] else None)
                if (not isinstance(a["payment_ids"], list)
                        or any(not isinstance(pid, str) or not pid for pid in a["payment_ids"])
                        or len(set(a["payment_ids"])) != len(a["payment_ids"])
                        or (a["payment_id"] is not None and not isinstance(a["payment_id"], str))
                        or (a["payment_ids"] and a["payment_id"] != a["payment_ids"][-1])
                        or (not a["payment_ids"] and a["payment_id"] is not None)):
                    raise ValueError()
                for pid in a["payment_ids"]:
                    payment = payment_by_id.get(pid)
                    if (payment is None or payment.get("authorization_id") != a["id"]
                            or payment["from_user_id"] != a["from_user_id"]
                            or payment["to_user_id"] != a["to_user_id"]):
                        raise ValueError()
                linked_payments = [p for p in state["payments"]
                                   if p.get("authorization_id") == a["id"]]
                if linked_payments:
                    if ([p["id"] for p in linked_payments] != a["payment_ids"]
                            or sum(p["amount"] for p in linked_payments) != a["captured_amount"]):
                        raise ValueError()
                elif a["payment_ids"]:
                    raise ValueError()
                if "closed_at" not in a:
                    if a["status"] == "expired":
                        a["closed_at"] = a["expires_at"]
                    elif a["status"] == "captured" and linked_payments:
                        a["closed_at"] = linked_payments[-1]["created_at"]
                    elif a["status"] in ("captured", "voided"):
                        # Older exports did not record when a hold closed. A closed hold
                        # need not reconstruct its lifecycle, so it releases at creation
                        # rather than appearing held until the import.
                        a["closed_at"] = a["created_at"]
                    else:
                        a["closed_at"] = None
                if (a["closed_at"] is not None
                        and parse_timestamp(a["closed_at"]) is None):
                    raise ValueError()
                if a["status"] == "open" and a["closed_at"] is not None:
                    raise ValueError()
                expected_events = [{"payment_id": p["id"],
                    "final": a["status"] == "captured" and p["id"] == a["payment_id"]}
                    for p in linked_payments]
                if "capture_events" not in a:
                    a["capture_events"] = expected_events
                if (not isinstance(a["capture_events"], list)
                        or a["capture_events"] != expected_events):
                    raise ValueError()
                if a["status"] == "open":
                    if a["captured_amount"] >= a["amount"]:
                        raise ValueError()
                    expiry = parse_timestamp(a["expires_at"])
                    if expiry <= datetime.now(timezone.utc):
                        a["status"] = "expired"
                        a["closed_at"] = a["expires_at"]
                    else:
                        held_by_user[a["from_user_id"]] += a["amount"] - a["captured_amount"]
            if any(held_by_user[uid] > state["users"][uid]["balance"] for uid in held_by_user):
                raise ValueError()
            for p in state["payments"]:
                aid = p.get("authorization_id")
                if aid is not None:
                    a = authorization_by_id.get(aid)
                    if (a is None or p.get("request_id") is not None
                            or p.get("settlement_id") is not None or p["id"] not in a["payment_ids"]):
                        raise ValueError()
            if not isinstance(state["operators"], list) or any(uid not in state["users"] for uid in state["operators"]):
                raise ValueError()
            payments_by_id = {p["id"]: p for p in state["payments"]}
            requests_by_id = {r["id"]: r for r in state["requests"]}
            for slot, entry in state["idempotency"].items():
                if (not isinstance(slot, str) or not isinstance(entry, dict)
                        or set(entry) != {"method", "path", "body", "response"}):
                    raise ValueError()
                method, path = entry["method"], entry["path"]
                if (method != "POST" or not isinstance(path, str)
                        or not isinstance(entry["body"], str)
                        or not isinstance(entry["response"], dict)):
                    raise ValueError()
                pay_match = re.fullmatch(r"/requests/([^/]+)/pay", path)
                capture_match = re.fullmatch(r"/authorizations/([^/]+)/capture", path)
                correction_match = re.fullmatch(r"/payments/([^/]+)/corrections", path)
                refund_match = re.fullmatch(r"/payments/([^/]+)/refunds", path)
                if (path not in ("/payments", "/requests", "/splits", "/settlements",
                                 "/authorizations", "/correction-batches")
                        and not pay_match and not capture_match
                        and not correction_match and not refund_match):
                    raise ValueError()
                suffix = "\0" + method + "\0" + path
                if not slot.endswith(suffix):
                    raise ValueError()
                prefix = slot[:-len(suffix)]
                if not any(prefix.startswith(uid + "\0")
                           and 1 <= len(prefix[len(uid) + 1:]) <= 255
                           for uid in state["users"]):
                    raise ValueError()
                response = entry["response"]
                if path == "/payments":
                    payment = payments_by_id.get(response.get("payment_id"))
                    if payment is None or payment.get("request_id") is not None or payment.get("settlement_id") is not None:
                        raise ValueError()
                elif path == "/requests":
                    if response.get("request_id") not in requests_by_id:
                        raise ValueError()
                elif pay_match:
                    payment = payments_by_id.get(response.get("payment_id"))
                    if (payment is None or payment.get("request_id") != pay_match.group(1)
                            or response.get("request_id") != pay_match.group(1)):
                        raise ValueError()
                elif path == "/authorizations":
                    if response.get("authorization_id") not in authorization_by_id:
                        raise ValueError()
                elif capture_match:
                    payment = payments_by_id.get(response.get("payment_id"))
                    if (payment is None or payment.get("authorization_id") != capture_match.group(1)
                            or response.get("authorization_id") != capture_match.group(1)):
                        raise ValueError()
                elif correction_match:
                    payment = payments_by_id.get(response.get("payment_id"))
                    if (payment is None or payment["id"] != correction_match.group(1)
                            or not isinstance(response.get("revision"), int)
                            or isinstance(response.get("revision"), bool)
                            or not 2 <= response["revision"] <= len(payment["revisions"])
                            or payment["revisions"][response["revision"] - 1]["amount"]
                               != response.get("amount")):
                        raise ValueError()
                elif refund_match:
                    payment = payments_by_id.get(response.get("payment_id"))
                    if payment is None or payment.get("refund_of") != refund_match.group(1):
                        raise ValueError()
                elif path == "/correction-batches":
                    batch_id = response.get("correction_batch_id")
                    revisions = response.get("revisions")
                    if (not isinstance(batch_id, str) or not batch_id
                            or not isinstance(revisions, list) or not revisions):
                        raise ValueError()
                    for item in revisions:
                        payment = payments_by_id.get(item.get("payment_id") if isinstance(item, dict) else None)
                        number = item.get("revision") if payment is not None else None
                        if (not isinstance(number, int) or isinstance(number, bool)
                                or not 2 <= number <= len(payment["revisions"])
                                or payment["revisions"][number - 1].get("correction_batch_id") != batch_id):
                            raise ValueError()
                elif path == "/splits":
                    if (not isinstance(response.get("split_id"), str) or not response["split_id"]
                            or not isinstance(response.get("requests"), list)
                            or any(not isinstance(r, dict) or r.get("request_id") not in requests_by_id
                                   for r in response["requests"])):
                        raise ValueError()
                else:
                    settlement_id = response.get("settlement_id")
                    members = response.get("payments")
                    if (not isinstance(settlement_id, str) or not settlement_id
                            or not isinstance(members, list) or not members):
                        raise ValueError()
                    for member in members:
                        payment = payments_by_id.get(member.get("payment_id") if isinstance(member, dict) else None)
                        if payment is None or payment.get("settlement_id") != settlement_id:
                            raise ValueError()
            if (not isinstance(state["sequence"], int) or isinstance(state["sequence"], bool)
                    or state["sequence"] < 0
                    or not isinstance(state["seeded_total"], int) or isinstance(state["seeded_total"], bool)
                    or state["seeded_total"] != balances):
                raise ValueError()
            if stage3_export:
                openings = state["opening_balances"]
                if (not isinstance(openings, dict) or set(openings) != set(state["users"])
                        or any(not isinstance(value, int) or isinstance(value, bool) or value < 0
                               for value in openings.values())):
                    raise ValueError()
            else:
                net = {uid: 0 for uid in state["users"]}
                for payment in state["payments"]:
                    net[payment["from_user_id"]] -= payment["amount"]
                    net[payment["to_user_id"]] += payment["amount"]
                state["opening_balances"] = {
                    uid: user["balance"] - net[uid]
                    for uid, user in state["users"].items()}
                if any(value < 0 for value in state["opening_balances"].values()):
                    raise ValueError()
        except (KeyError, TypeError, ValueError):
            fail(422, "validation_failed", "Invalid export state")
        prior_state = self.state
        self.state = state
        try:
            current_at = datetime.now(timezone.utc)
            if any(self._historical_total(uid, current_at) != user["balance"]
                   for uid, user in state["users"].items()):
                fail(422, "validation_failed", "Export balances do not match payment history")
            # Earlier stages did not record full hold lifecycles, so their exports are
            # checked for historical totals only; reconstructed holds must not reject them.
            if not self._record_historical_state_is_solvent(include_holds=stage3_export):
                fail(422, "validation_failed", "Export contains a historical overdraft")
        finally:
            self.state = prior_state
        return copy.deepcopy(state)

    def _validate_snapshots(self, document, state):
        """Statement snapshots travel beside the state; exports from earlier stages have none.

        An import replaces every existing snapshot, so tokens from the replaced state stop
        resolving and tokens from the imported service keep paging their frozen result.
        """
        raw = document.get("statement_snapshots", {})
        if not isinstance(raw, dict):
            fail(422, "validation_failed", "Invalid statement snapshots")
        snapshots = {}
        for token, snapshot in raw.items():
            result = snapshot.get("result") if isinstance(snapshot, dict) else None
            if (not isinstance(token, str) or not token or len(token) > 255
                    or not isinstance(snapshot, dict)
                    or set(snapshot) != {"user_id", "result"}
                    or not isinstance(snapshot["user_id"], str)
                    or snapshot["user_id"] not in state["users"]
                    or not isinstance(result, dict) or result.get("snapshot") != token
                    or not isinstance(result.get("entries"), list)
                    or any(not isinstance(result.get(name), int) or isinstance(result.get(name), bool)
                           for name in ("opening_balance", "closing_balance"))):
                fail(422, "validation_failed", "Invalid statement snapshots")
            snapshots[token] = copy.deepcopy(snapshot)
        return snapshots

    def _reset(self, fixture):
        if not isinstance(fixture, dict):
            fail(422, "validation_failed", "Fixture must be an object")
        temp = empty_state()
        reset_at = now()
        reset_instant = parse_timestamp(reset_at)
        currency = fixture.get("currency")
        minor = integer_in_range(fixture.get("minor_units"), 0, 3)
        if not isinstance(currency, str) or minor not in (0, 2, 3):
            fail(422, "validation_failed", "Invalid currency configuration")
        if (currency, minor) not in (("EUR", 2), ("JPY", 0), ("BHD", 3)):
            fail(422, "validation_failed", "Unsupported currency configuration")
        temp["currency"], temp["minor_units"] = currency, minor
        ttl = fixture.get("authorization_ttl_seconds", 600)
        if isinstance(ttl, bool) or not isinstance(ttl, (int, Decimal)):
            fail(422, "validation_failed", "authorization_ttl_seconds must be a positive integer")
        if isinstance(ttl, Decimal):
            if not ttl.is_finite() or ttl != ttl.to_integral_value():
                fail(422, "validation_failed", "authorization_ttl_seconds must be a positive integer")
            ttl = int(ttl)
        if ttl < 1:
            fail(422, "validation_failed", "authorization_ttl_seconds must be a positive integer")
        temp["authorization_ttl_seconds"] = ttl
        users = fixture.get("users", [])
        if not isinstance(users, list):
            fail(422, "validation_failed", "users must be an array")
        pending_user_hashes = []
        for raw in users:
            if not isinstance(raw, dict):
                fail(422, "validation_failed", "Invalid user fixture")
            uid, email, password = raw.get("id"), raw.get("email"), raw.get("password")
            display, handle = raw.get("display_name"), raw.get("handle")
            balance = integer_in_range(raw.get("balance"), 0, MAX_BALANCE)
            if (not isinstance(uid, str) or not uid or len(uid) > 64 or
                not isinstance(email, str) or not isinstance(password, str) or
                not isinstance(display, str) or not isinstance(handle, str) or
                not HANDLE_RE.fullmatch(handle) or balance is None or uid in temp["users"] or
                email in temp["emails"] or handle in temp["handles"]):
                fail(422, "validation_failed", "Invalid or duplicate user fixture")
            user = {"id": uid, "email": email, "password_hash": "",
                    "display_name": display, "handle": handle, "balance": balance}
            temp["users"][uid], temp["emails"][email], temp["handles"][handle] = user, uid, uid
            pending_user_hashes.append((uid, password))
        if pending_user_hashes:
            # Fixture reset is test-only and can contain dozens of users. Hash distinct
            # credentials concurrently so large invariant fixtures fit the HTTP timeout.
            with ThreadPoolExecutor(max_workers=min(4, len(pending_user_hashes))) as pool:
                hashes = pool.map(password_hash, (password for _, password in pending_user_hashes))
                for (uid, _), hashed in zip(pending_user_hashes, hashes):
                    temp["users"][uid]["password_hash"] = hashed
        temp["seeded_total"] = sum(user["balance"] for user in temp["users"].values())
        payments = fixture.get("payments", [])
        if not isinstance(payments, list):
            fail(422, "validation_failed", "payments must be an array")
        payment_ids = set()
        for raw in payments:
            if not isinstance(raw, dict):
                fail(422, "validation_failed", "Invalid payment fixture")
            pid, sender, receiver = raw.get("id"), raw.get("from_user_id"), raw.get("to_user_id")
            amt = integer_in_range(raw.get("amount"), 0, MAX_AMOUNT)
            note, vis = raw.get("note", ""), raw.get("visibility", "public")
            created_at = raw.get("created_at", reset_at)
            request_id = raw.get("request_id")
            if (not isinstance(pid, str) or not pid or len(pid) > 64 or
                pid in payment_ids or not isinstance(sender, str) or not isinstance(receiver, str) or
                sender not in temp["users"] or receiver not in temp["users"] or sender == receiver or
                amt is None or (amt == 0 and not isinstance(request_id, str)) or
                not isinstance(note, str) or len(note) > 200 or
                vis not in ("public", "private") or parse_timestamp(created_at) is None
                    or parse_timestamp(created_at) > reset_instant):
                fail(422, "validation_failed", "Invalid payment fixture")
            payment_ids.add(pid)
            item = {"id": pid, "from_user_id": sender, "to_user_id": receiver,
                "amount": amt, "note": note, "visibility": vis, "request_id": request_id,
                "settlement_id": raw.get("settlement_id"),
                "authorization_id": raw.get("authorization_id"),
                "refund_of": raw.get("refund_of"), "created_at": created_at}
            item["revisions"] = [self._initial_revision(amt, created_at)]
            temp["payments"].append(item)
        requests = fixture.get("requests", [])
        if not isinstance(requests, list):
            fail(422, "validation_failed", "requests must be an array")
        request_ids = set()
        for raw in requests:
            if not isinstance(raw, dict):
                fail(422, "validation_failed", "Invalid request fixture")
            rid, requester, payer = raw.get("id"), raw.get("requester_id"), raw.get("payer_id")
            amt = integer_in_range(raw.get("amount"), 0, MAX_AMOUNT)
            note, status = raw.get("note", ""), raw.get("status", "pending")
            created_at = raw.get("created_at", reset_at)
            if (not isinstance(rid, str) or not rid or len(rid) > 64 or
                rid in request_ids or not isinstance(requester, str) or not isinstance(payer, str) or
                requester not in temp["users"] or payer not in temp["users"] or requester == payer or
                amt is None or not isinstance(note, str) or len(note) > 200 or
                not isinstance(status, str) or status not in STATUSES or
                parse_timestamp(created_at) is None):
                fail(422, "validation_failed", "Invalid request fixture")
            request_ids.add(rid)
            temp["requests"].append({"id": rid, "requester_id": requester, "payer_id": payer,
                "amount": amt, "note": note, "status": status,
                "payment_id": raw.get("payment_id"), "created_at": created_at})
        authorizations = fixture.get("authorizations", [])
        if not isinstance(authorizations, list):
            fail(422, "validation_failed", "authorizations must be an array")
        authorization_ids = set()
        for raw in authorizations:
            if not isinstance(raw, dict):
                fail(422, "validation_failed", "Invalid authorization fixture")
            aid = raw.get("id")
            sender, receiver = raw.get("from_user_id"), raw.get("to_user_id")
            amount = integer_in_range(raw.get("amount"), 1, MAX_AMOUNT)
            note, vis, status = raw.get("note", ""), raw.get("visibility", "public"), raw.get("status")
            captured_default = amount if status == "captured" and amount is not None else 0
            captured = integer_in_range(raw.get("captured_amount", captured_default), 0, MAX_AMOUNT)
            expires_at = raw.get("expires_at")
            created_at = raw.get("created_at", reset_at)
            if (not isinstance(aid, str) or not aid or len(aid) > 64 or aid in authorization_ids
                    or not isinstance(sender, str) or sender not in temp["users"]
                    or not isinstance(receiver, str) or receiver not in temp["users"] or sender == receiver
                    or amount is None or captured is None or captured > amount
                    or not isinstance(note, str) or len(note) > 200
                    or not isinstance(vis, str) or vis not in ("public", "private")
                    or status not in ("open", "captured", "voided", "expired")
                    or parse_timestamp(expires_at) is None or parse_timestamp(created_at) is None):
                fail(422, "validation_failed", "Invalid authorization fixture")
            payment_ids = raw.get("payment_ids")
            if payment_ids is None:
                payment_ids = [raw["payment_id"]] if raw.get("payment_id") is not None else []
            payment_id = raw.get("payment_id", payment_ids[-1] if payment_ids else None)
            if (not isinstance(payment_ids, list)
                    or any(not isinstance(pid, str) or not pid for pid in payment_ids)
                    or len(set(payment_ids)) != len(payment_ids)
                    or (payment_id is not None and not isinstance(payment_id, str))
                    or (payment_ids and payment_id != payment_ids[-1])
                    or (not payment_ids and payment_id is not None)):
                fail(422, "validation_failed", "Invalid authorization payment links")
            if status == "open" and captured >= amount:
                fail(422, "validation_failed", "An open authorization must have a remainder")
            if status == "open" and parse_timestamp(expires_at) <= datetime.now(timezone.utc):
                status = "expired"
            authorization_ids.add(aid)
            closed_at = raw.get("closed_at")
            if closed_at is not None and parse_timestamp(closed_at) is None:
                fail(422, "validation_failed", "Invalid authorization fixture")
            if status == "open" and closed_at is not None:
                fail(422, "validation_failed", "An open authorization cannot have closed_at")
            if closed_at is None and status == "expired":
                closed_at = expires_at
            elif closed_at is None and status == "captured" and payment_ids:
                captured_payment = next(
                    (p for p in temp["payments"] if p["id"] == payment_ids[-1]), None)
                closed_at = captured_payment["created_at"] if captured_payment else created_at
            elif closed_at is None and status in ("captured", "voided"):
                closed_at = created_at
            item = {
                "id": aid, "from_user_id": sender, "to_user_id": receiver,
                "amount": amount, "captured_amount": captured, "note": note,
                "visibility": vis, "status": status, "expires_at": expires_at,
                "payment_id": payment_id, "payment_ids": list(payment_ids),
                "created_at": created_at, "closed_at": closed_at}
            item["capture_events"] = []
            for index, linked_id in enumerate(payment_ids):
                item["capture_events"].append({"payment_id": linked_id,
                    "final": status == "captured" and index == len(payment_ids) - 1})
            temp["authorizations"].append(item)
        auth_by_id = {a["id"]: a for a in temp["authorizations"]}
        payments_by_id = {p["id"]: p for p in temp["payments"]}
        held_by_user = {uid: 0 for uid in temp["users"]}
        for auth in temp["authorizations"]:
            linked = [p for p in temp["payments"] if p.get("authorization_id") == auth["id"]]
            if linked:
                if ([p["id"] for p in linked] != auth["payment_ids"]
                        or sum(p["amount"] for p in linked) != auth["captured_amount"]
                        or any(p["from_user_id"] != auth["from_user_id"]
                               or p["to_user_id"] != auth["to_user_id"] for p in linked)):
                    fail(422, "validation_failed", "Invalid authorization payment links")
            elif auth["payment_ids"]:
                fail(422, "validation_failed", "Invalid authorization payment links")
            if auth["status"] == "open":
                held_by_user[auth["from_user_id"]] += auth["amount"] - auth["captured_amount"]
        for payment in temp["payments"]:
            aid = payment.get("authorization_id")
            if aid is not None:
                if not isinstance(aid, str):
                    fail(422, "validation_failed", "Invalid authorization payment links")
                auth = auth_by_id.get(aid)
                if (auth is None or payment.get("request_id") is not None
                        or payment.get("settlement_id") is not None
                        or payment["id"] not in auth["payment_ids"]):
                    fail(422, "validation_failed", "Invalid authorization payment links")
        if any(held_by_user[uid] > temp["users"][uid]["balance"] for uid in held_by_user):
            fail(422, "validation_failed", "Seeded authorizations exceed available wallet funds")
        ops = fixture.get("settlement_operator_ids", [])
        if not isinstance(ops, list) or any(not isinstance(uid, str) or uid not in temp["users"] for uid in ops):
            fail(422, "validation_failed", "Invalid settlement operator list")
        temp["operators"] = list(dict.fromkeys(ops))
        try:
            validate_financial_links(temp["payments"], temp["requests"])
            validate_refund_links(temp["payments"])
        except ValueError:
            fail(422, "validation_failed", "Invalid financial links in fixture")
        net = {uid: 0 for uid in temp["users"]}
        for payment in temp["payments"]:
            net[payment["from_user_id"]] -= payment["amount"]
            net[payment["to_user_id"]] += payment["amount"]
        temp["opening_balances"] = {
            uid: user["balance"] - net[uid] for uid, user in temp["users"].items()}
        if any(balance < 0 for balance in temp["opening_balances"].values()):
            fail(422, "validation_failed", "Seeded payment history has a negative opening balance")
        for authorization in temp["authorizations"]:
            if authorization["status"] == "open" and parse_timestamp(authorization["expires_at"]) <= reset_instant:
                authorization["status"] = "expired"
                authorization["closed_at"] = authorization["expires_at"]
        prior_state = self.state
        self.state = temp
        try:
            if any(self._historical_total(uid, reset_instant) != user["balance"]
                   for uid, user in temp["users"].items()):
                fail(422, "validation_failed", "Seeded balances do not match payment history")
            if not self._record_historical_state_is_solvent():
                fail(422, "validation_failed", "Seeded history contains an overdraft")
        finally:
            self.state = prior_state
        return temp

    def _check_signup_available(self, email, handle):
        if email in self.state["emails"]:
            fail(409, "email_taken", "Email is already registered")
        if handle in self.state["handles"]:
            fail(409, "handle_taken", "Derived handle is already taken")

    def _signup(self, body):
        email = required_string(body, "email")
        password = required_string(body, "password")
        display = required_string(body, "display_name")
        if not EMAIL_RE.fullmatch(email) or email.count("@") != 1:
            fail(422, "validation_failed", "Invalid email address")
        if len(password) < 8:
            fail(422, "validation_failed", "Password must contain at least 8 characters")
        local = email.split("@", 1)[0].lower()
        handle = re.sub(r"[^a-z0-9_]", "_", local)[:20]
        if not handle:
            handle = "_"
        with self.lock:
            self._check_signup_available(email, handle)
        with AUTH_HASH_SLOTS:
            hashed = password_hash(password)
        with self.lock:
            # Another signup may have taken the email or handle while this one hashed.
            self._check_signup_available(email, handle)
            uid, token = new_id("u"), secrets.token_urlsafe(32)
            self.state["users"][uid] = {"id": uid, "email": email, "password_hash": hashed,
                                        "display_name": display, "handle": handle, "balance": 0}
            self.state["emails"][email] = uid
            self.state["handles"][handle] = uid
            self.state["tokens"][token] = uid
            self.state["opening_balances"][uid] = 0
            return 201, {"user_id": uid, "display_name": display, "token": token}

    def _login(self, body):
        email = required_string(body, "email")
        password = required_string(body, "password")
        with self.lock:
            state = self.state
            uid = state["emails"].get(email)
            user = state["users"].get(uid) if uid else None
            encoded = user["password_hash"] if user else None
        if encoded is None:
            fail(401, "unauthenticated", "Email or password is incorrect")
        with AUTH_HASH_SLOTS:
            matches = password_matches(password, encoded)
        if not matches:
            fail(401, "unauthenticated", "Email or password is incorrect")
        with self.lock:
            # A reset or import may have replaced this user while the password was checked.
            if self.state is not state or state["users"].get(uid) is not user:
                fail(401, "unauthenticated", "Email or password is incorrect")
            token = secrets.token_urlsafe(32)
            self.state["tokens"][token] = uid
            return 200, {"user_id": uid, "display_name": user["display_name"], "token": token}

    def dispatch(self, method, target, headers, raw_body):
        read_at = datetime.now(timezone.utc)
        parsed = urlsplit(target)
        path = parsed.path
        try:
            body = json.loads(raw_body.decode("utf-8"), parse_int=parse_json_integer,
                              parse_float=parse_json_decimal,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError())) if raw_body else {}
        except (UnicodeDecodeError, json.JSONDecodeError, InvalidOperation, ValueError, RecursionError):
            fail(400, "malformed_request", "Request body is not valid JSON")
        if not isinstance(body, dict):
            fail(400, "malformed_request", "Request body must be a JSON object")
        query = parse_qs(parsed.query, keep_blank_values=True)
        if method == "POST" and path in ("/auth/signup", "/auth/login"):
            # scrypt is deliberately slow, so it runs outside the service lock; the lock
            # only guards the lookups and the final write.
            return self._signup(body) if path.endswith("signup") else self._login(body)
        with self.lock:
            if method == "GET" and path == "/health":
                return 200, {"status": "ok"}
            if method == "POST" and path == "/_test/reset":
                replacement = self._reset(body)
                self.state = replacement
                self.statement_snapshots.clear()
                return 204, None
            if method == "GET" and path == "/_test/export":
                self._expire_authorizations()
                return 200, {"track": "pocketful", "format_version": 1,
                             "state": self._serialize_state(),
                             "statement_snapshots": copy.deepcopy(self.statement_snapshots)}
            if method == "POST" and path == "/_test/import":
                replacement = self._validate_import(body)
                snapshots = self._validate_snapshots(body, replacement)
                self.state = replacement
                self.statement_snapshots = snapshots
                return 204, None
            token_header = headers.get("Authorization", "")
            if not token_header.startswith("Bearer ") or not token_header[7:]:
                fail(401, "unauthenticated", "A bearer token is required")
            uid = self.state["tokens"].get(token_header[7:])
            if uid is None:
                fail(401, "unauthenticated", "Bearer token is unknown")
            self._expire_authorizations()

            if method == "GET" and path == "/me":
                return 200, self._me_at(uid, query, read_at)
            if method == "POST":
                correction_match = re.fullmatch(r"/payments/([^/]+)/corrections", path)
                if correction_match:
                    return self._correct_payment(uid, correction_match.group(1), body, headers)
                refund_match = re.fullmatch(r"/payments/([^/]+)/refunds", path)
                if refund_match or path == "/correction-batches":
                    status, replay, signature = self._key_and_replay(headers, uid, method, path, body)
                    if status is not None:
                        return status, replay
                    if refund_match:
                        return self._refund_payment(uid, refund_match.group(1), body, signature)
                    return self._correct_batch(uid, body, signature)
            if method == "GET":
                revisions_match = re.fullmatch(r"/payments/([^/]+)/revisions", path)
                if revisions_match:
                    payment = next((p for p in self.state["payments"]
                                    if p["id"] == revisions_match.group(1)), None)
                    if payment is None or uid not in (
                            payment["from_user_id"], payment["to_user_id"]):
                        fail(404, "not_found", "Payment not found")
                    revisions = copy.deepcopy(self._payment_revisions(payment))
                    for revision in revisions:
                        revision.setdefault("correction_batch_id", None)
                    return 200, {"revisions": revisions}
            if method == "POST" and path in ("/payments", "/requests", "/splits", "/settlements",
                                               "/authorizations"):
                status, replay, signature = self._key_and_replay(headers, uid, method, path, body)
                if status is not None:
                    return status, replay
                if path == "/payments":
                    return self._create_payment(uid, body, signature)
                if path == "/requests":
                    return self._create_request(uid, body, signature)
                if path == "/splits":
                    return self._create_split(uid, body, signature)
                if path == "/authorizations":
                    return self._create_authorization(uid, body, signature)
                return self._settle(uid, body, signature)

            m = re.fullmatch(r"/requests/([^/]+)/(pay|decline|cancel)", path)
            if method == "POST" and m:
                rid, action = m.group(1), m.group(2)
                if action == "pay":
                    status, replay, signature = self._key_and_replay(headers, uid, method, path, body)
                    if status is not None:
                        return status, replay
                    return self._pay_request(uid, rid, body, signature)
                item = next((r for r in self.state["requests"] if r["id"] == rid), None)
                if item is None:
                    fail(404, "not_found", "Request not found")
                owner = item["payer_id"] if action == "decline" else item["requester_id"]
                if owner != uid:
                    fail(403, "forbidden", "You may not change this request")
                desired = "declined" if action == "decline" else "cancelled"
                if item["status"] == desired:
                    return 200, self._request_obj(item)
                if item["status"] != "pending":
                    fail(409, "request_not_pending", "Request is not pending")
                item["status"] = desired
                return 200, self._request_obj(item)

            authorization_match = re.fullmatch(r"/authorizations/([^/]+)/(capture|void)", path)
            if method == "POST" and authorization_match:
                authorization_id, action = authorization_match.groups()
                if action == "capture":
                    status, replay, signature = self._key_and_replay(headers, uid, method, path, body)
                    if status is not None:
                        return status, replay
                    return self._capture_authorization(uid, authorization_id, body, signature)
                return self._void_authorization(uid, authorization_id)

            if method == "GET" and path == "/requests":
                direction = query.get("direction", [None])[0]
                status_filter = query.get("status", [None])[0]
                if direction not in (None, "incoming", "outgoing"):
                    fail(422, "validation_failed", "Invalid direction")
                if status_filter not in (None, *STATUSES):
                    fail(422, "validation_failed", "Invalid status")
                limit = page_value(query, "limit", 50, 1, 200)
                offset = page_value(query, "offset", 0, 0)
                rows = [r for r in self.state["requests"] if uid in (r["requester_id"], r["payer_id"])]
                if direction == "incoming":
                    rows = [r for r in rows if r["payer_id"] == uid]
                elif direction == "outgoing":
                    rows = [r for r in rows if r["requester_id"] == uid]
                if status_filter is not None:
                    rows = [r for r in rows if r["status"] == status_filter]
                rows.sort(key=lambda r: (parse_timestamp(r["created_at"]), r["id"]), reverse=True)
                page = rows[offset:offset + limit]
                return 200, {"requests": [self._request_obj(r) for r in page],
                             "has_more": offset + limit < len(rows)}

            if method == "GET" and path == "/activity":
                limit = page_value(query, "limit", 50, 1, 200)
                offset = page_value(query, "offset", 0, 0)
                rows = [p for p in self.state["payments"] if
                        p["visibility"] == "public" or uid in (p["from_user_id"], p["to_user_id"])]
                rows.sort(key=lambda p: (parse_timestamp(p["created_at"]), p["id"]), reverse=True)
                page = rows[offset:offset + limit]
                return 200, {"payments": [self._payment_obj(p) for p in page],
                             "has_more": offset + limit < len(rows)}

            if method == "GET" and path == "/statement":
                return 200, self._statement(uid, query, read_at)

            if method == "GET" and path == "/authorizations":
                direction = query.get("direction", [None])[0]
                status_filter = query.get("status", [None])[0]
                authorization_statuses = ("open", "captured", "voided", "expired")
                if direction not in (None, "incoming", "outgoing"):
                    fail(422, "validation_failed", "Invalid direction")
                if status_filter not in (None, *authorization_statuses):
                    fail(422, "validation_failed", "Invalid status")
                limit = page_value(query, "limit", 50, 1, 200)
                offset = page_value(query, "offset", 0, 0)
                rows = [a for a in self.state["authorizations"]
                        if uid in (a["from_user_id"], a["to_user_id"])]
                if direction == "outgoing":
                    rows = [a for a in rows if a["from_user_id"] == uid]
                elif direction == "incoming":
                    rows = [a for a in rows if a["to_user_id"] == uid]
                if status_filter is not None:
                    rows = [a for a in rows if a["status"] == status_filter]
                rows.sort(key=lambda a: (parse_timestamp(a["created_at"]), a["id"]), reverse=True)
                page = rows[offset:offset + limit]
                return 200, {"authorizations": [self._authorization_obj(a) for a in page],
                             "has_more": offset + limit < len(rows)}

            fail(404, "not_found", "Endpoint not found")


SERVICE = Service()
WEB_ROOT = Path(__file__).resolve().parent / "web"
UI_ROUTES = {"/", "/requests", "/split", "/signup", "/login", "/authorizations"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def _handle(self):
        try:
            length_raw = self.headers.get("Content-Length", "0")
            limit = (MAX_IMPORT_BODY_BYTES if urlsplit(self.path).path == "/_test/import"
                     else MAX_BODY_BYTES)
            # ASCII digits only, and no more of them than the limit has, so int() cannot fail.
            if (not (length_raw.isascii() and length_raw.isdigit())
                    or len(length_raw) > len(str(limit)) or int(length_raw) > limit):
                fail(400, "malformed_request", "Invalid request body length")
            raw_body = self.rfile.read(int(length_raw)) if int(length_raw) else b""
            parsed = urlsplit(self.path)
            accept = self.headers.get("Accept", "")
            if self.command in ("GET", "HEAD") and "text/html" in accept and parsed.path in UI_ROUTES:
                content = (WEB_ROOT / "index.html").read_bytes()
                self._send_bytes(200, content, "text/html; charset=utf-8")
                return
            if self.command in ("GET", "HEAD"):
                static_path = parsed.path
                if static_path.startswith("/assets/"):
                    static_path = static_path[len("/assets/"):]
                elif static_path.startswith("/"):
                    static_path = static_path[1:]
                if static_path and static_path not in ("index.html",):
                    root = WEB_ROOT.resolve()
                    asset = (root / static_path).resolve()
                    if asset.is_relative_to(root) and asset.is_file():
                        content_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
                        self._send_bytes(200, asset.read_bytes(), content_type + "; charset=utf-8")
                        return
            # HEAD is answered exactly like GET; _send_bytes omits the body.
            method = "GET" if self.command == "HEAD" else self.command
            status, payload = SERVICE.dispatch(method, self.path, self.headers, raw_body)
        except ApiError as exc:
            status = exc.status
            payload = {"error": {"code": exc.code, "message": exc.message}}
        except Exception:
            status = 500
            payload = {"error": {"code": "internal_error", "message": "Internal server error"}}
        if payload is None:
            encoded = b""
        else:
            encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, encoded, "application/json; charset=utf-8")

    def _send_bytes(self, status, encoded, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if content_type.startswith("text/html"):
            self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        self.send_header("Connection", "close")
        self.end_headers()
        if encoded and self.command != "HEAD":
            self.wfile.write(encoded)
        self.close_connection = True

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

    def __getattr__(self, name):
        # Every other method (OPTIONS, TRACE, CONNECT, ...) gets the same JSON error handling
        # instead of http.server's HTML 501 page.
        if name.startswith("do_"):
            return self._handle
        raise AttributeError(name)

    def send_error(self, code, message=None, explain=None):
        """Unparseable request lines get the documented JSON error body, never HTML or a 5xx."""
        # Some Python versions reject the request line before recording its version, which
        # would suppress the status line and headers entirely.
        if self.request_version == "HTTP/0.9":
            self.request_version = self.protocol_version
        status = code if 400 <= code < 500 else 400
        error = "not_found" if status == 404 else "malformed_request"
        body = json.dumps({"error": {"code": error,
                                     "message": message or "Request could not be completed"}},
                          ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")


class Server(ThreadingHTTPServer):
    # The listen backlog must stay well above the 50 requests served in flight;
    # the default of 5 resets connections during a burst.
    request_queue_size = 128
    daemon_threads = True


def main():
    port = int(os.environ.get("PORT", "8080"))
    server = Server(("0.0.0.0", port), Handler)
    print(f"Pocketful Stage 4 listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

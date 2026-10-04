"""Pocketful Stage 1 HTTP service. Standard-library only."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
            "idempotency": {}, "seeded_total": 0, "sequence": 0}


class Service:
    def __init__(self):
        self.lock = threading.RLock()
        self.state = empty_state()

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
                "created_at": item["created_at"]}

    def _make_request(self, requester_id, payer_id, amount, note, created=None, rid=None):
        item = {"id": rid or new_id("rq"), "requester_id": requester_id,
                "payer_id": payer_id, "amount": amount, "note": note,
                "status": "pending", "payment_id": None, "created_at": created or now()}
        self.state["requests"].append(item)
        return item

    def _make_payment(self, from_id, to_id, amount, note, vis, request_id=None,
                      settlement_id=None, created=None):
        sender, receiver = self.state["users"][from_id], self.state["users"][to_id]
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
                "created_at": created or now()}
        self.state["payments"].append(item)
        return item

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
        net = {u["id"]: u["balance"] for u in self.state["users"].values()}
        for sender, receiver, amount, _, _ in entries:
            net[sender] -= amount
            net[receiver] += amount
        if any(v < 0 for v in net.values()):
            fail(409, "insufficient_funds", "Settlement is not affordable")
        if any(v > MAX_BALANCE for v in net.values()):
            fail(422, "validation_failed", "Settlement balance exceeds the supported range")
        settlement_id, committed_at = new_id("st"), now()
        payments = []
        for sender, receiver, amount, note, vis in entries:
            item = {"id": new_id("p"), "from_user_id": sender, "to_user_id": receiver,
                    "amount": amount, "note": note, "visibility": vis,
                    "request_id": None, "settlement_id": settlement_id,
                    "created_at": committed_at}
            payments.append(item)
        # Commit only after every entry and every net balance has passed validation.
        for user in self.state["users"].values():
            user["balance"] = net[user["id"]]
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
        state = document.get("state")
        if not isinstance(state, dict):
            fail(422, "validation_failed", "Invalid export state")
        required = {"currency", "minor_units", "users", "emails", "handles", "tokens",
                    "payments", "requests", "operators", "idempotency", "seeded_total", "sequence"}
        if set(state) != required:
            fail(422, "validation_failed", "Invalid export state")
        # Validate referential and arithmetic integrity while preserving byte-for-byte values.
        try:
            if not isinstance(state["users"], dict) or not isinstance(state["payments"], list) or not isinstance(state["requests"], list):
                raise ValueError()
            if (not isinstance(state["tokens"], dict) or not isinstance(state["idempotency"], dict)
                    or not isinstance(state["emails"], dict) or not isinstance(state["handles"], dict)):
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
                payment_ids.add(p["id"])
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
                if path not in ("/payments", "/requests", "/splits", "/settlements") and not pay_match:
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
        except (KeyError, TypeError, ValueError):
            fail(422, "validation_failed", "Invalid export state")
        return copy.deepcopy(state)

    def _reset(self, fixture):
        if not isinstance(fixture, dict):
            fail(422, "validation_failed", "Fixture must be an object")
        temp = empty_state()
        currency = fixture.get("currency")
        minor = integer_in_range(fixture.get("minor_units"), 0, 3)
        if not isinstance(currency, str) or minor not in (0, 2, 3):
            fail(422, "validation_failed", "Invalid currency configuration")
        if (currency, minor) not in (("EUR", 2), ("JPY", 0), ("BHD", 3)):
            fail(422, "validation_failed", "Unsupported currency configuration")
        temp["currency"], temp["minor_units"] = currency, minor
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
            created_at = raw.get("created_at", now())
            request_id = raw.get("request_id")
            if (not isinstance(pid, str) or not pid or len(pid) > 64 or
                pid in payment_ids or not isinstance(sender, str) or not isinstance(receiver, str) or
                sender not in temp["users"] or receiver not in temp["users"] or sender == receiver or
                amt is None or (amt == 0 and not isinstance(request_id, str)) or
                not isinstance(note, str) or len(note) > 200 or
                vis not in ("public", "private") or parse_timestamp(created_at) is None):
                fail(422, "validation_failed", "Invalid payment fixture")
            payment_ids.add(pid)
            temp["payments"].append({"id": pid, "from_user_id": sender, "to_user_id": receiver,
                "amount": amt, "note": note, "visibility": vis, "request_id": request_id,
                "settlement_id": raw.get("settlement_id"), "created_at": created_at})
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
            created_at = raw.get("created_at", now())
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
        ops = fixture.get("settlement_operator_ids", [])
        if not isinstance(ops, list) or any(not isinstance(uid, str) or uid not in temp["users"] for uid in ops):
            fail(422, "validation_failed", "Invalid settlement operator list")
        temp["operators"] = list(dict.fromkeys(ops))
        try:
            validate_financial_links(temp["payments"], temp["requests"])
        except ValueError:
            fail(422, "validation_failed", "Invalid financial links in fixture")
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
                self.state = self._reset(body)
                return 204, None
            if method == "GET" and path == "/_test/export":
                return 200, {"track": "pocketful", "format_version": 1,
                             "state": self._serialize_state()}
            if method == "POST" and path == "/_test/import":
                replacement = self._validate_import(body)
                self.state = replacement
                return 204, None
            token_header = headers.get("Authorization", "")
            if not token_header.startswith("Bearer ") or not token_header[7:]:
                fail(401, "unauthenticated", "A bearer token is required")
            uid = self.state["tokens"].get(token_header[7:])
            if uid is None:
                fail(401, "unauthenticated", "Bearer token is unknown")

            if method == "GET" and path == "/me":
                user = self.state["users"][uid]
                return 200, {"user_id": uid, "display_name": user["display_name"],
                    "handle": user["handle"], "balance": user["balance"],
                    "currency": self.state["currency"], "minor_units": self.state["minor_units"]}
            if method == "POST" and path in ("/payments", "/requests", "/splits", "/settlements"):
                status, replay, signature = self._key_and_replay(headers, uid, method, path, body)
                if status is not None:
                    return status, replay
                if path == "/payments":
                    return self._create_payment(uid, body, signature)
                if path == "/requests":
                    return self._create_request(uid, body, signature)
                if path == "/splits":
                    return self._create_split(uid, body, signature)
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

            fail(404, "not_found", "Endpoint not found")


SERVICE = Service()


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
    print(f"Pocketful Stage 1 listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

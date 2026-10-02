"""Small stdlib HTTP client adapter for the supplied pytest contract files."""
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request


class Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return json.loads(self._body.decode("utf-8")) if self._body else None


def new_key():
    return secrets.token_hex(12)


def assert_status(response, status):
    assert response.status_code == status, f"expected {status}, got {response.status_code}: {response._body!r}"
    return response


def assert_error(response, status, code):
    assert_status(response, status)
    body = response.json()
    assert body["error"]["code"] == code, body
    return response


class Api:
    def __init__(self, base_url, token=None):
        self.base_url, self.token = base_url.rstrip("/"), token

    def request(self, method, path, *, json_body=None, content=None, params=None,
                idempotency_key=None):
        if params:
            path += "?" + urllib.parse.urlencode(params)
        headers = {}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        if content is not None:
            data = content.encode("utf-8") if isinstance(content, str) else content
            headers["Content-Type"] = "application/json; charset=utf-8"
        elif json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        else:
            data = None
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=8) as response:
                return Response(response.status, response.read())
        except urllib.error.HTTPError as exc:
            return Response(exc.code, exc.read())

    def get(self, path, params=None):
        return self.request("GET", path, params=params)

    def post(self, path, *, json=None, content=None, idempotency_key=None):
        return self.request("POST", path, json_body=json, content=content,
                            idempotency_key=idempotency_key)

    def signup(self, email, password, display_name):
        return self.post("/auth/signup", json={"email": email, "password": password,
                                                "display_name": display_name})

    def login(self, email, password):
        return self.post("/auth/login", json={"email": email, "password": password})

    def authenticate(self, email, password):
        response = assert_status(self.login(email, password), 200)
        self.token = response.json()["token"]
        return self

    def close(self):
        pass

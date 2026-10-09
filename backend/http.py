"""Tiny JSON-over-HTTP helper (stdlib). Tests swap `transport` for a fake."""
import json
import urllib.error
import urllib.request


class HttpError(RuntimeError):
    def __init__(self, status: int, body, url: str):
        super().__init__(f"HTTP {status} from {url}: {str(body)[:300]}")
        self.status, self.body, self.url = status, body, url


def _urllib_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


transport = _urllib_transport


USER_AGENT = "AanganStudioAgent/1.0 (+phone enquiry agent)"   # Cloudflare (e.g. api.cal.com) blocks Python's default UA


def request_json(method: str, url: str, headers: dict | None = None, payload=None, timeout: float = 20.0):
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    status, raw = transport(method, url, headers, body, timeout)
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else (raw or "")
    try:
        data = json.loads(text) if text else None
    except json.JSONDecodeError:
        data = text
    if status >= 400:
        raise HttpError(status, data, url)
    return data

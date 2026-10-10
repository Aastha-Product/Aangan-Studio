"""Dashboard sign-in: the shared studio password, and personal accounts (email + password).

- Passwords: PBKDF2-SHA256 with a random salt (standard library, no extra packages).
- Sessions: a random cookie value; the database keeps only its SHA-256, so a leaked table can't sign anyone in.
- The studio password's hash lives in app_settings (set with scripts/set_studio_password.py), never in the code.
- Creating an account needs the studio password too: the dashboard holds callers' details, and the site is public.
- Guessing is slowed down: 8 failed tries per address or email in 15 minutes, then a 15-minute wait.
"""
import base64
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

ITERATIONS = 310_000
SESSION_DAYS = 30
MAX_FAILURES, FAILURE_WINDOW = 8, timedelta(minutes=15)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
COOKIE = "aangan_session"


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def check_password(password: str, stored: str | None) -> bool:
    try:
        algo, iters, salt, digest = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iters))
        return hmac.compare_digest(got, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(store, user_id: int | None) -> tuple[str, str]:
    """-> (cookie value, Set-Cookie header value)."""
    token = secrets.token_urlsafe(32)
    store.create_session(_token_hash(token), user_id, datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS))
    return token, f"{COOKIE}={token}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age={SESSION_DAYS * 86400}"


def clear_cookie() -> str:
    return f"{COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0"


def viewer(store, token: str | None) -> dict | None:
    """Who a session cookie belongs to: {"kind": "account", "name", "email"} or {"kind": "studio"}; None if invalid."""
    if not token:
        return None
    s = store.get_session(_token_hash(token))
    if not s:
        return None
    exp = s.get("expires_at")
    exp = datetime.fromisoformat(str(exp).replace("Z", "+00:00")) if exp else None
    if not exp or exp < datetime.now(timezone.utc):
        return None
    if s.get("user_id") is None:
        return {"kind": "studio", "name": None, "email": None}
    u = store.get_user_by_id(s["user_id"])
    return {"kind": "account", "name": u.get("name"), "email": u.get("email"), "id": u.get("id")} if u else None


def end_session(store, token: str | None) -> None:
    if token:
        store.delete_session(_token_hash(token))


# --- guessing protection ----------------------------------------------------------------------------

def too_many_failures(store, keys: list[str]) -> bool:
    since = (datetime.now(timezone.utc) - FAILURE_WINDOW).isoformat(timespec="seconds")
    return any(store.count_auth_failures(k, since) >= MAX_FAILURES for k in keys if k)


def record_failure(store, keys: list[str]) -> None:
    for k in keys:
        if k:
            store.record_auth_failure(k)


# --- the two ways in --------------------------------------------------------------------------------

def studio_password_ok(store, password: str) -> bool:
    return bool(password) and check_password(password, store.get_setting("studio_password_hash"))


def sign_in(store, email: str, password: str) -> dict | None:
    u = store.get_user_by_email((email or "").strip().lower())
    if not u or not check_password(password or "", u.get("password_hash")):
        return None
    store.touch_user_login(u["id"])
    return u


def sign_up(store, name: str, email: str, password: str, studio_password: str) -> tuple[dict | None, str]:
    """-> (user, "") or (None, a plain-words reason)."""
    email = (email or "").strip().lower()
    name = (name or "").strip()[:80]
    if not studio_password_ok(store, studio_password):
        return None, "The studio password is not right. Ask Aastha for it."
    if not EMAIL_RE.match(email):
        return None, "That email address doesn't look right."
    if len(password or "") < 8:
        return None, "Choose a password of at least 8 characters."
    if store.get_user_by_email(email):
        return None, "There's already an account with this email. Sign in instead."
    return store.create_user(email, name or None, hash_password(password)), ""

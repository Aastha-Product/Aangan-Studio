"""HTTP entry point (WSGI, stdlib). Deployed on Vercel via api/index.py; run locally with scripts/dev_server.py.

POST /api/calendly/webhook   Calendly invitee.created / canceled / no-show   (signature verified)
POST /api/calcom/webhook     Cal.com BOOKING_CREATED / RESCHEDULED / CANCELLED / NO_SHOW_UPDATED (signature verified)
POST /api/vaani/webhook      Vaani call_started / call_ended / call_postprocessing (?key= verified)
GET  /api/cron/digest        7pm digest (Vercel cron, Bearer CRON_SECRET)
GET  /api/reask              designer's "I had to re-ask the basics" tick    (signed link)
GET  /dashboard              Nikhil's dashboard                             (?token=DASHBOARD_TOKEN)
GET  /dashboard/call         one call: checks, score, quotes, transcript
POST /api/admin/migrate     create/update the Neon tables once (Bearer CRON_SECRET)
GET  /api/health
"""
import hmac
import json
import os
import traceback
import urllib.parse
from http.cookies import SimpleCookie

from . import actions, calcom, calendly, config, dashboard, digest, emails, vaani
from .store import default_store, utcnow

_store = None


def get_store():
    global _store
    if _store is None:
        _store = default_store()
    return _store


def _resp(start_response, status: str, body, ctype="application/json", headers=None):
    if not isinstance(body, (bytes, str)):
        body = json.dumps(body, default=str)
    data = body.encode("utf-8") if isinstance(body, str) else body
    start_response(status, [("Content-Type", f"{ctype}; charset=utf-8"), ("Content-Length", str(len(data))),
                            ("Cache-Control", "no-store"), *(headers or [])])
    return [data]


def _dashboard_allowed(environ, query) -> tuple[bool, list]:
    token = config.DASHBOARD_TOKEN
    if not token:
        return (not os.environ.get("VERCEL")), []          # no token set: local only
    given = (query.get("token") or [""])[0]
    cookie = SimpleCookie(environ.get("HTTP_COOKIE", "")).get("dash_token")
    if given and hmac.compare_digest(given, token):
        return True, [("Set-Cookie", f"dash_token={token}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=2592000")]
    return bool(cookie and hmac.compare_digest(cookie.value, token)), []


def app(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    query = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
    # On Vercel every route is rewritten to /api/index, which hides the requested path; vercel.json passes
    # the original as ?__path=… . Locally (dev server, tests) PATH_INFO already holds it.
    raw_path = (query.pop("__path", [None])[0]) or environ.get("PATH_INFO", "/")
    path = "/" + raw_path.strip("/") if raw_path.strip("/") else "/"
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    raw = environ["wsgi.input"].read(length) if length else b""
    store = get_store()

    try:
        if path == "/api/health":
            return _resp(start_response, "200 OK", {"ok": True, "configured": configured()})

        if path == "/api/calendly/webhook" and method == "POST":
            if not calendly.verify_signature(raw, environ.get("HTTP_CALENDLY_WEBHOOK_SIGNATURE")):
                return _resp(start_response, "401 Unauthorized", {"error": "bad signature"})
            result = actions.handle_calendly_event(store, json.loads(raw or b"{}"))
            return _resp(start_response, "200 OK", {"ok": True, "result": result})

        if path == "/api/calcom/webhook" and method == "POST":
            if not calcom.verify_signature(raw, environ.get("HTTP_X_CAL_SIGNATURE_256")):
                return _resp(start_response, "401 Unauthorized", {"error": "bad signature"})
            result = actions.handle_calcom_event(store, json.loads(raw or b"{}"))
            return _resp(start_response, "200 OK", {"ok": True, "result": result})

        if path == "/api/vaani/webhook" and method == "POST":
            if not vaani.verify(raw, environ.get("HTTP_X_VAANIVOICE_SIGNATURE"), (query.get("key") or [None])[0]):
                return _resp(start_response, "401 Unauthorized", {"error": "bad signature"})
            body = json.loads(raw or b"{}")
            return _resp(start_response, "200 OK", {"ok": True, "result": handle_vaani(store, body)})

        if path == "/api/admin/migrate" and method == "POST":
            # One-off DB setup on Vercel (Neon's DATABASE_URL never has to leave Vercel). Same secret as the cron.
            auth = environ.get("HTTP_AUTHORIZATION", "")
            if not config.CRON_SECRET or not hmac.compare_digest(auth, f"Bearer {config.CRON_SECRET}"):
                return _resp(start_response, "401 Unauthorized", {"error": "unauthorised"})
            return _resp(start_response, "200 OK", migrate())

        if path == "/api/cron/digest":
            auth = environ.get("HTTP_AUTHORIZATION", "")
            if not config.CRON_SECRET or not hmac.compare_digest(auth, f"Bearer {config.CRON_SECRET}"):
                return _resp(start_response, "401 Unauthorized", {"error": "unauthorised"})
            return _resp(start_response, "200 OK", digest.run(store))

        if path == "/api/reask":
            call_id, sig = (query.get("call_id") or [""])[0], (query.get("sig") or [""])[0]
            if not call_id or not hmac.compare_digest(sig, emails.reask_sig(call_id)):
                return _resp(start_response, "403 Forbidden", "Invalid link.", "text/plain")
            store.update_call(call_id, {"had_to_reask": True})
            return _resp(start_response, "200 OK", "Thanks, noted. This feeds the dashboard so we can fix what the agent missed.",
                         "text/plain")

        if path in ("/", "/dashboard", "/dashboard/call"):
            ok, cookies = _dashboard_allowed(environ, query)
            if not ok:
                return _resp(start_response, "401 Unauthorized", "Add ?token=… to the URL.", "text/plain")
            if path == "/dashboard/call":
                c = store.get_call((query.get("call_id") or [""])[0])
                if not c:
                    return _resp(start_response, "404 Not Found", "No such call.", "text/plain")
                return _resp(start_response, "200 OK", dashboard.render_call(c), "text/html", cookies)
            period = (query.get("period") or ["30"])[0]
            days = None if period == "mtd" else int(period) if period.isdigit() else 30
            since = None
            m = dashboard.compute_metrics(store.list_calls(since), store.list_events("speech_guard_block"),
                                          config.now_ist(), days)
            return _resp(start_response, "200 OK",
                         dashboard.render_dashboard(m, period, (query.get("token") or [""])[0]), "text/html", cookies)

        return _resp(start_response, "404 Not Found", {"error": "not found", "path": path})
    except Exception as exc:  # noqa: BLE001
        store.log_event(None, "error", {"path": path, "error": str(exc)[:300], "trace": traceback.format_exc()[-1500:]})
        return _resp(start_response, "500 Internal Server Error", {"error": "internal error"})


def _fingerprint(secret: str) -> str | None:
    """First 6 hex chars of sha256 — lets you check Vercel holds the same secret as .env without revealing it."""
    import hashlib
    return hashlib.sha256(secret.encode()).hexdigest()[:6] if secret else None


def configured() -> dict:
    """Which settings this deployment has (yes/no only; secrets as fingerprints). Never returns values."""
    return {
        "database": bool(config.DATABASE_URL), "public_base_url": config.PUBLIC_BASE_URL or None,
        "dashboard_token": _fingerprint(config.DASHBOARD_TOKEN), "cron_secret": _fingerprint(config.CRON_SECRET),
        "app_secret": bool(config.APP_SECRET), "gemini": bool(config.env("GEMINI_API_KEY")),
        "resend": bool(config.RESEND_API_KEY), "hubspot": bool(config.HUBSPOT_TOKEN),
        "booking_provider": config.BOOKING_PROVIDER, "calcom": bool(config.CALCOM_API_KEY),
        "calcom_event_type": bool(config.CALCOM_EVENT_TYPE_ID), "vaani_webhook": bool(config.VAANI_WEBHOOK_SECRET),
    }


def migrate() -> dict:
    """Apply db/schema.sql to DATABASE_URL (idempotent) and report table sizes."""
    if not config.DATABASE_URL:
        return {"ok": False, "error": "DATABASE_URL is not set: connect the Neon database in Vercel → Storage"}
    import psycopg
    sql = (config.ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    with psycopg.connect(config.DATABASE_URL, autocommit=True, connect_timeout=15) as conn:
        conn.execute(sql)
        counts = {t: conn.execute(f"select count(*) from {t}").fetchone()[0] for t in ("calls", "call_events")}
    return {"ok": True, "tables": counts}


def handle_vaani(store, body: dict) -> str:
    """Vaani lifecycle webhooks. With the BYOL bridge the call is already logged and qualified by the
    voice server; these events add the caller's number, the real duration and the recording link.
    Without the bridge (Vaani's own LLM), call_postprocessing's transcript is qualified here."""
    ev = vaani.parse_event(body)
    call_id = ev["call_id"]
    store.log_event(call_id or None, "vaani_webhook", {"event": ev["event"], "raw": body})
    if not call_id:
        return "no call id"
    row = store.get_call(call_id) or {}
    now = utcnow()
    base = {"call_id": call_id, "caller_number": row.get("caller_number") or ev["caller_number"]}

    if ev["event"] == "call_started":
        store.upsert_call({**base, "started_at": row.get("started_at") or now,
                           "answered_at": row.get("answered_at") or now, "answered": True})
        return "started"
    if ev["event"] in ("call_failed", "call_rejected", "call_no_answer"):
        store.upsert_call({**base, "answered": False, "status": "missed"})
        return "missed"
    if ev["event"] == "call_ended":
        store.upsert_call({**base, "ended_at": row.get("ended_at") or now,
                           "duration_sec": ev["duration_sec"] or row.get("duration_sec")})
        return "ended"
    if ev["event"] == "call_postprocessing":
        store.upsert_call({**base, "duration_sec": ev["duration_sec"] or row.get("duration_sec"),
                           "started_at": row.get("started_at") or now})
        if ev["recording_url"]:
            store.log_event(call_id, "recording", {"url": ev["recording_url"], "summary": ev["summary"]})
        # Already handled by the BYOL bridge, or a retry of this webhook: don't process twice.
        if row.get("processed_at") or row.get("processing_started_at") or not ev["transcript"]:
            return "already processed" if (row.get("processed_at") or row.get("processing_started_at")) else "updated"
        store.update_call(call_id, {"processing_started_at": now})
        started = store.get_call(call_id).get("started_at")
        actions.process_completed_call(store, call_id, ev["transcript"], calendly.parse_time(started) if started else None)
        return "processed"
    return f"ignored {ev['event']}"

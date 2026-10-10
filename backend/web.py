"""HTTP entry point (WSGI, stdlib). Deployed on Vercel via api/index.py; run locally with scripts/dev_server.py.

POST /api/calendly/webhook   Calendly invitee.created / canceled / no-show   (signature verified)
POST /api/calcom/webhook     Cal.com BOOKING_CREATED / RESCHEDULED / CANCELLED / NO_SHOW_UPDATED (signature verified)
POST /api/vaani/webhook      Vaani call_started / call_ended / call_postprocessing (?key= verified)
GET  /api/cron/digest        7pm digest (Vercel cron, Bearer CRON_SECRET)
GET  /api/reask              designer's "I had to re-ask the basics" tick    (signed link)
GET  /dashboard              Aangan Studio dashboard (public while DASHBOARD_PUBLIC=true)
GET/POST /dashboard/login    sign in: own account (email + password) or the studio password
GET/POST /dashboard/signup   create an account (needs the studio password, so only the team can join)
POST /dashboard/logout       sign out
GET  /dashboard/call         one call: checks, score, quotes, transcript, activity
POST /dashboard/call/action  mark a follow-up done / reopen it / overturn a rejection (dashboard token)
GET  /dashboard/export.csv   the period's calls as a spreadsheet
POST /api/admin/migrate     create/update the Neon tables once (Bearer CRON_SECRET)
GET  /call                   public "Talk to Aangan Studio" page: web call with the agent (no phone number)
POST /api/webcall/start|end  GET /api/webcall/status   (backend/webcall.py)
POST /api/tools/slots|book|pending   booking tools the agent calls mid-call, keyed by the call's one-time ref (webtools.py)
GET  /api/health
"""
import hmac
import json
import os
import traceback
import urllib.parse
from datetime import timedelta
from http.cookies import SimpleCookie

from . import actions, auth, calcom, calendly, config, dashboard, digest, emails, vaani, webcall, webtools
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


def _cookie(environ, name) -> str | None:
    c = SimpleCookie(environ.get("HTTP_COOKIE", "")).get(name)
    return c.value if c else None


def _viewer(environ, query, store) -> tuple[dict | None, list]:
    """Who is looking: public dashboard, a signed-in account, studio password, or older ?token= link."""
    v = auth.viewer(store, _cookie(environ, auth.COOKIE))
    if v:
        return v, []
    if config.DASHBOARD_PUBLIC:
        return {"kind": "public", "name": "Aangan Studio"}, []
    token = config.DASHBOARD_TOKEN
    if not token:
        return ({"kind": "studio"} if not os.environ.get("VERCEL") else None), []   # no token set: local only
    given = (query.get("token") or [""])[0]
    if given and hmac.compare_digest(given, token):
        return {"kind": "studio"}, [_auth_cookie()]
    old = _cookie(environ, "dash_token")
    return ({"kind": "studio"} if old and hmac.compare_digest(old, token) else None), []


def _dashboard_allowed(environ, query) -> tuple[bool, list]:   # kept for older callers
    v, cookies = _viewer(environ, query, get_store())
    return bool(v), cookies


def _auth_cookie() -> tuple[str, str]:
    """The ?token= link: the browser remembers the dashboard for 30 days (HttpOnly: page scripts can't read it)."""
    return ("Set-Cookie", f"dash_token={config.DASHBOARD_TOKEN}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=2592000")


def _client_ip(environ) -> str:
    return (environ.get("HTTP_X_FORWARDED_FOR") or environ.get("REMOTE_ADDR") or "").split(",")[0].strip()


def _form(raw: bytes) -> dict:
    f = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
    return {k: v[0] for k, v in f.items()}


def _safe_next(nxt: str) -> str:
    """Only ever send people back into the dashboard after signing in."""
    return nxt if nxt.startswith("/dashboard") and not nxt.startswith("//") else "/dashboard"


def _here(path: str, query: dict) -> str:
    """The page that was asked for, without the token, to return to after signing in."""
    q = urllib.parse.urlencode({k: v[0] for k, v in query.items() if k not in ("token", "__path")})
    return path + (f"?{q}" if q else "")


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
        if path == "/favicon.ico":
            return _resp(start_response, "200 OK", dashboard.FAVICON_SVG, "image/svg+xml")

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

        if path in ("/dashboard/login", "/dashboard/signup", "/dashboard/logout"):
            return _auth_route(start_response, store, environ, path, method, query, raw)

        # --- booking tools the Vaani agent calls during a web call (guarded by the call's own one-time reference) ---
        if path.startswith("/api/tools/") and method in ("POST", "GET"):
            status, out = webtools.handle(store, path.rsplit("/", 1)[-1], webtools.parse_params(raw, query))
            return _resp(start_response, status, out)

        # --- web calls (public: this is where callers talk to the agent) ---
        if path == "/call":
            return _resp(start_response, "200 OK", webcall.render_page(), "text/html")
        if path in ("/api/webcall/start", "/api/webcall/end") and method == "POST":
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = {}
            if path == "/api/webcall/start":
                status, out = webcall.start(store, _client_ip(environ), str(body.get("name") or ""))
            else:
                status, out = webcall.end(store, str(body.get("room") or ""), body.get("seconds"))
            return _resp(start_response, status, out)
        if path == "/api/webcall/status":
            status, out = webcall.status(store, (query.get("room") or [""])[0])
            return _resp(start_response, status, out)

        if path in ("/", "/dashboard", "/dashboard/calls", "/dashboard/reports", "/dashboard/setup",
                    "/dashboard/call", "/dashboard/call/action", "/dashboard/export.csv"):
            if method == "POST":   # dashboard forms send the token in the body
                query = {**query, **urllib.parse.parse_qs(raw.decode("utf-8", "replace"))}
            viewer, cookies = _viewer(environ, query, store)
            if not viewer:
                if method == "GET":   # the sign-in page, then straight back to the page that was asked for
                    nxt = _safe_next(_here("/dashboard" if path == "/" else path, query))
                    return _resp(start_response, "401 Unauthorized", dashboard.render_login(nxt), "text/html")
                return _resp(start_response, "401 Unauthorized", "Sign in again: open the dashboard and sign in.", "text/plain")
            dashboard.VIEWER.set(viewer)               # the top bar shows who is signed in
            token = (query.get("token") or [""])[0]
            if path == "/dashboard/call/action":
                if method != "POST":
                    return _resp(start_response, "405 Method Not Allowed", "Use the buttons on the call page.", "text/plain")
                call_id = (query.get("call_id") or [""])[0]
                if not store.get_call(call_id):
                    return _resp(start_response, "404 Not Found", "No such call.", "text/plain")
                by = (query.get("by") or [""])[0] or viewer.get("name") or viewer.get("email") or ""
                done = call_action(store, call_id, (query.get("action") or [""])[0], by, (query.get("note") or [""])[0])
                if not done:
                    return _resp(start_response, "400 Bad Request", "Unknown action.", "text/plain")
                back = (query.get("back") or [""])[0]
                if not back.startswith("/dashboard"):     # only ever redirect within the dashboard
                    back = dashboard._q("/dashboard/call", token, call_id=call_id)
                back += ("&" if "?" in back else "?") + "msg=" + (query.get("action") or [""])[0]
                return _resp(start_response, "303 See Other", "", "text/plain", [("Location", back), *cookies])
            flash = (query.get("msg") or [None])[0]
            if path == "/dashboard/call":
                c = store.get_call((query.get("call_id") or [""])[0])
                if not c:
                    return _resp(start_response, "404 Not Found", "No such call.", "text/plain")
                return _resp(start_response, "200 OK",
                             dashboard.render_call(c, store.list_call_events(c["call_id"]), token, flash), "text/html", cookies)
            if path == "/dashboard/setup":
                calls, now = store.list_calls(), config.now_ist()
                todo = len(dashboard.compute_metrics(calls, [], now, 7)["attention"])
                return _resp(start_response, "200 OK", dashboard.render_setup(dashboard.setup_status(store), token, todo),
                             "text/html", cookies)
            period = (query.get("period") or ["30"])[0]
            if period not in ("1", "7", "30", "mtd"):
                period = "30"
            days = None if period == "mtd" else int(period)
            if path in ("/", "/dashboard", "/dashboard/calls"):
                webcall.sweep(store, limit=2)       # finish web calls whose page closed before the transcript was ready
            calls, blocks, now = store.list_calls(), store.list_events("speech_guard_block"), config.now_ist()
            m = dashboard.compute_metrics(calls, blocks, now, days)
            if path == "/dashboard/export.csv":
                name = f"aangan-calls-{m['start']:%Y%m%d}-{now:%Y%m%d}.csv"
                return _resp(start_response, "200 OK", dashboard.render_csv(m["rows"], now), "text/csv",
                             [("Content-Disposition", f'attachment; filename="{name}"'), *cookies])
            # the same-length window just before this one, for "vs previous period"
            prev_days = days or (now.date() - m["start"].date()).days + 1
            m["prev"] = dashboard.compute_metrics(calls, blocks, m["start"] - timedelta(microseconds=1), prev_days)
            setup = dashboard.setup_status(store)
            setup_open = any(s["state"] != "ok" for s in setup)
            if path == "/dashboard/calls":
                page = dashboard.render_calls(m, period, token, (query.get("group") or ["all"])[0], setup_open)
            elif path == "/dashboard/reports":
                page = dashboard.render_reports(m, period, token, setup_open)
            else:
                page = dashboard.render_home(m, period, token, setup, flash)
            return _resp(start_response, "200 OK", page, "text/html", cookies)

        return _resp(start_response, "404 Not Found", {"error": "not found", "path": path})
    except Exception as exc:  # noqa: BLE001
        store.log_event(None, "error", {"path": path, "error": str(exc)[:300], "trace": traceback.format_exc()[-1500:]})
        return _resp(start_response, "500 Internal Server Error", {"error": "internal error"})


def _auth_route(start_response, store, environ, path, method, query, raw):
    """Sign in (studio password or own account), create an account, sign out."""
    html = lambda status, page, headers=None: _resp(start_response, status, page, "text/html", headers)  # noqa: E731
    go = lambda where, cookies: _resp(start_response, "303 See Other", "", "text/plain",  # noqa: E731
                                      [("Location", where), *[("Set-Cookie", c) for c in cookies]])
    ip_key = f"ip:{_client_ip(environ)}"

    if path == "/dashboard/logout":
        auth.end_session(store, _cookie(environ, auth.COOKIE))
        old = "dash_token=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0"
        return go("/dashboard/login?msg=out", [auth.clear_cookie(), old])

    if method != "POST":
        nxt = _safe_next((query.get("next") or [""])[0])
        msg = (query.get("msg") or [None])[0]
        if path == "/dashboard/signup":
            return html("200 OK", dashboard.render_signup(nxt))
        return html("200 OK", dashboard.render_login(nxt, mode=(query.get("mode") or ["account"])[0], flash=msg))

    f = _form(raw)
    nxt = _safe_next(f.get("next", ""))
    if path == "/dashboard/signup":
        if auth.too_many_failures(store, [ip_key]):
            return html("429 Too Many Requests", dashboard.render_signup(nxt, f, "Too many tries. Wait 15 minutes and try again."))
        user, why = auth.sign_up(store, f.get("name", ""), f.get("email", ""), f.get("password", ""), f.get("studio_password", ""))
        if not user:
            if "studio password" in why:
                auth.record_failure(store, [ip_key])
            return html("400 Bad Request", dashboard.render_signup(nxt, f, why))
        _, cookie = auth.new_session(store, user["id"])
        return go(nxt + ("&" if "?" in nxt else "?") + "msg=welcome", [cookie])

    mode = f.get("mode", "account")
    email_key = f"email:{f.get('email', '').strip().lower()}" if mode == "account" else None
    if auth.too_many_failures(store, [ip_key, email_key]):
        return html("429 Too Many Requests",
                    dashboard.render_login(nxt, mode=mode, error="Too many tries. Wait 15 minutes and try again.", email=f.get("email")))
    if mode == "studio":
        if auth.studio_password_ok(store, f.get("password", "")):
            _, cookie = auth.new_session(store, None)
            return go(nxt, [cookie])
        auth.record_failure(store, [ip_key])
        return html("401 Unauthorized", dashboard.render_login(nxt, mode="studio", error="That password didn't work."))
    user = auth.sign_in(store, f.get("email", ""), f.get("password", ""))
    if user:
        _, cookie = auth.new_session(store, user["id"])
        return go(nxt, [cookie])
    auth.record_failure(store, [ip_key, email_key])
    return html("401 Unauthorized", dashboard.render_login(nxt, mode="account", email=f.get("email"),
                                                           error="That email and password don't match an account."))


def call_action(store, call_id: str, action: str, by: str, note: str) -> bool:
    """Dashboard buttons. done: follow-up handled · reopen: undo that · overturn: a rejected caller gets a callback."""
    by, note = by.strip()[:80] or None, note.strip()[:500] or None
    if action == "done":
        store.update_call(call_id, {"handled_at": utcnow(), "handled_by": by, "handled_note": note})
    elif action == "reopen":
        store.update_call(call_id, {"handled_at": None, "handled_by": None, "handled_note": None})
    elif action == "overturn":
        store.update_call(call_id, {"overturned_at": utcnow(), "overturned_by": by,
                                    "handled_at": None, "handled_by": None, "handled_note": None})
    else:
        return False
    store.log_event(call_id, f"dashboard_{action}", {"by": by, "note": note})
    return True


def _fingerprint(secret: str) -> str | None:
    """First 6 hex chars of sha256 — lets you check Vercel holds the same secret as .env without revealing it."""
    import hashlib
    return hashlib.sha256(secret.encode()).hexdigest()[:6] if secret else None


def configured() -> dict:
    """Which settings this deployment has (yes/no only; secrets as fingerprints). Never returns values."""
    return {
        "database": bool(config.DATABASE_URL), "public_base_url": config.PUBLIC_BASE_URL or None,
        "dashboard_token": _fingerprint(config.DASHBOARD_TOKEN), "cron_secret": _fingerprint(config.CRON_SECRET),
        "dashboard_public": config.DASHBOARD_PUBLIC, "app_secret": bool(config.APP_SECRET),
        "gemini": bool(config.env("GEMINI_API_KEY")),
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
        counts = {t: conn.execute(f"select count(*) from {t}").fetchone()[0]
                  for t in ("calls", "call_events", "users", "sessions", "app_settings")}
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
        if ev["recording_url"] or ev["summary"]:
            store.update_call(call_id, {"recording_url": ev["recording_url"] or row.get("recording_url"),
                                        "summary": ev["summary"] or row.get("summary")})
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

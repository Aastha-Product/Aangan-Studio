"""Call log. SupabaseStore in production (PostgREST); LocalStore (JSON file) for local runs and tests.

Both expose the same methods. Schema: db/schema.sql.
"""
import copy
import json
import threading
import urllib.parse
from datetime import datetime, timezone

from . import config
from .http import request_json


TEST_PREFIX = "E2E-TEST"   # calls made by the live end-to-end test; ignored by the setup checks


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _event_matches(e: dict, kind: str, since_iso, subject_prefix) -> bool:
    p = e.get("payload") or {}
    return (e.get("kind") == kind and not str(e.get("call_id") or "").startswith(TEST_PREFIX)
            and (not since_iso or str(e.get("created_at") or "") >= since_iso)
            and (not subject_prefix or str(p.get("subject") or "").startswith(subject_prefix)))


class SupabaseStore:
    def __init__(self, url: str | None = None, key: str | None = None):
        self.url = (url or config.SUPABASE_URL) + "/rest/v1"
        self.key = key or config.SUPABASE_KEY

    def _h(self, prefer: str | None = None):
        h = {"apikey": self.key, "Authorization": f"Bearer {self.key}"}
        if prefer:
            h["Prefer"] = prefer
        return h

    def get_call(self, call_id: str) -> dict | None:
        rows = request_json("GET", f"{self.url}/calls?select=*&call_id=eq.{urllib.parse.quote(call_id)}", self._h())
        return rows[0] if rows else None

    def upsert_call(self, row: dict) -> dict:
        row = {**row, "updated_at": utcnow()}
        rows = request_json("POST", f"{self.url}/calls?on_conflict=call_id",
                            self._h("resolution=merge-duplicates,return=representation"), [row])
        return rows[0] if rows else row

    def update_call(self, call_id: str, changes: dict) -> dict | None:
        changes = {**changes, "updated_at": utcnow()}
        rows = request_json("PATCH", f"{self.url}/calls?call_id=eq.{urllib.parse.quote(call_id)}",
                            self._h("return=representation"), changes)
        return rows[0] if rows else None

    def delete_call(self, call_id: str) -> None:
        request_json("DELETE", f"{self.url}/calls?call_id=eq.{urllib.parse.quote(call_id)}", self._h("return=minimal"))

    def find_call(self, field: str, value: str) -> dict | None:
        rows = request_json("GET", f"{self.url}/calls?select=*&{field}=eq.{urllib.parse.quote(str(value))}"
                                   f"&order=created_at.desc&limit=1", self._h())
        return rows[0] if rows else None

    def list_calls(self, since_iso: str | None = None) -> list[dict]:
        q = "select=*&order=created_at.asc"
        if since_iso:
            q += f"&created_at=gte.{urllib.parse.quote(since_iso)}"
        return request_json("GET", f"{self.url}/calls?{q}", self._h()) or []

    def log_event(self, call_id: str | None, kind: str, payload: dict | None = None) -> None:
        request_json("POST", f"{self.url}/call_events", self._h("return=minimal"),
                     [{"call_id": call_id, "kind": kind, "payload": payload or {}}])

    def list_events(self, kind: str | None = None, since_iso: str | None = None) -> list[dict]:
        q = "select=*&order=created_at.asc"
        if kind:
            q += f"&kind=eq.{urllib.parse.quote(kind)}"
        if since_iso:
            q += f"&created_at=gte.{urllib.parse.quote(since_iso)}"
        return request_json("GET", f"{self.url}/call_events?{q}", self._h()) or []

    def list_call_events(self, call_id: str) -> list[dict]:
        q = f"select=*&call_id=eq.{urllib.parse.quote(call_id)}&order=created_at.asc"
        return request_json("GET", f"{self.url}/call_events?{q}", self._h()) or []

    def event_stats(self, kind, since_iso=None, subject_prefix=None) -> tuple[int, str | None]:
        hits = [e for e in self.list_events(kind, since_iso) if _event_matches(e, kind, since_iso, subject_prefix)]
        return len(hits), max((e["created_at"] for e in hits), default=None)


class LocalStore:
    """Same interface, kept in memory and optionally mirrored to a JSON file."""

    def __init__(self, path=None):
        self.path = path
        self._lock = threading.Lock()
        self.calls: dict[str, dict] = {}
        self.events: list[dict] = []
        if path and path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.calls, self.events = data.get("calls", {}), data.get("events", [])

    def _save(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"calls": self.calls, "events": self.events}, indent=1,
                                            ensure_ascii=False, default=str), encoding="utf-8")

    def get_call(self, call_id):
        c = self.calls.get(call_id)
        return copy.deepcopy(c) if c else None

    def upsert_call(self, row):
        with self._lock:
            cur = self.calls.get(row["call_id"], {"created_at": utcnow()})
            cur.update(row)
            cur["updated_at"] = utcnow()
            self.calls[row["call_id"]] = cur
            self._save()
            return copy.deepcopy(cur)

    def update_call(self, call_id, changes):
        if call_id not in self.calls:
            return None
        return self.upsert_call({"call_id": call_id, **changes})

    def delete_call(self, call_id):
        with self._lock:
            self.calls.pop(call_id, None)
            self._save()

    def find_call(self, field, value):
        hits = [c for c in self.calls.values() if c.get(field) == value]
        return copy.deepcopy(sorted(hits, key=lambda c: c.get("created_at", ""))[-1]) if hits else None

    def list_calls(self, since_iso=None):
        rows = sorted(self.calls.values(), key=lambda c: c.get("created_at", ""))
        return [copy.deepcopy(c) for c in rows if not since_iso or c.get("created_at", "") >= since_iso]

    def log_event(self, call_id, kind, payload=None):
        with self._lock:
            self.events.append({"call_id": call_id, "kind": kind, "payload": payload or {}, "created_at": utcnow()})
            self._save()

    def list_events(self, kind=None, since_iso=None):
        return [e for e in self.events
                if (not kind or e["kind"] == kind) and (not since_iso or e["created_at"] >= since_iso)]

    def list_call_events(self, call_id):
        return [copy.deepcopy(e) for e in self.events if e.get("call_id") == call_id]

    def claim_processing(self, call_id, stale_after_sec=180):
        with self._lock:
            c = self.calls.get(call_id)
            if not c or c.get("processed_at"):
                return False
            started = c.get("processing_started_at")
            if started and (datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds() < stale_after_sec:
                return False
            c["processing_started_at"] = utcnow()
            self._save()
            return True

    def event_stats(self, kind, since_iso=None, subject_prefix=None):
        hits = [e for e in self.events if _event_matches(e, kind, since_iso, subject_prefix)]
        return len(hits), max((e["created_at"] for e in hits), default=None)

    # --- sign-in (backend/auth.py) — kept in memory only, never in the JSON file ---
    def _auth(self):
        if not hasattr(self, "_a"):
            self._a = {"users": {}, "sessions": {}, "fails": [], "settings": {}}
        return self._a

    def get_setting(self, key):
        return self._auth()["settings"].get(key)

    def set_setting(self, key, value):
        self._auth()["settings"][key] = value

    def create_user(self, email, name, password_hash):
        users = self._auth()["users"]
        u = {"id": len(users) + 1, "email": email, "name": name, "password_hash": password_hash, "created_at": utcnow()}
        users[u["id"]] = u
        return dict(u)

    def get_user_by_email(self, email):
        return next((dict(u) for u in self._auth()["users"].values() if u["email"] == email), None)

    def get_user_by_id(self, user_id):
        u = self._auth()["users"].get(user_id)
        return dict(u) if u else None

    def touch_user_login(self, user_id):
        if user_id in self._auth()["users"]:
            self._auth()["users"][user_id]["last_login_at"] = utcnow()

    def create_session(self, token_hash, user_id, expires_at):
        self._auth()["sessions"][token_hash] = {"user_id": user_id, "expires_at": expires_at.isoformat()}

    def get_session(self, token_hash):
        s = self._auth()["sessions"].get(token_hash)
        return dict(s) if s else None

    def delete_session(self, token_hash):
        self._auth()["sessions"].pop(token_hash, None)

    def record_auth_failure(self, key):
        self._auth()["fails"].append((key, utcnow()))

    def count_auth_failures(self, key, since_iso):
        return sum(1 for k, t in self._auth()["fails"] if k == key and t >= since_iso)


class PostgresStore:
    """Neon (or any Postgres) via DATABASE_URL. Same interface as the other stores.

    - Columns are read from the table once, so unknown keys are ignored instead of failing a call.
    - jsonb columns get dicts/lists, text[] columns get lists; timestamps come back as ISO strings
      (the rest of the backend compares and parses ISO strings).
    - One connection per warm serverless instance, reconnected once if Neon dropped it.
    """

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or config.DATABASE_URL
        self._conn = None
        self._cols: dict[str, dict[str, str]] = {}
        self._unknown: dict[str, set] = {}

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row
        self._conn = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row, connect_timeout=10)
        return self._conn

    def _run(self, sql: str, params=(), fetch: str | None = "all"):
        import psycopg
        for attempt in (1, 2):
            try:
                conn = self._conn if self._conn and not self._conn.closed else self._connect()
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    if fetch == "all":
                        return [_from_db(r) for r in cur.fetchall()]
                    if fetch == "one":
                        r = cur.fetchone()
                        return _from_db(r) if r else None
                    return None
            except psycopg.OperationalError:
                self._conn = None
                if attempt == 2:
                    raise

    def columns(self, table: str) -> dict[str, str]:
        if table not in self._cols:
            rows = self._run("select column_name, data_type from information_schema.columns "
                             "where table_name = %s and table_schema = current_schema()", (table,))
            self._cols[table] = {r["column_name"]: r["data_type"] for r in rows}
        return self._cols[table]

    def _prepare(self, table: str, row: dict) -> dict:
        from psycopg.types.json import Jsonb
        cols = self.columns(table)
        # A migration may have added columns since this instance read them: re-read once per new unknown key.
        seen = self._unknown.setdefault(table, set())
        unknown = {k for k in row if k not in cols} - seen
        if unknown:
            seen |= unknown
            rows = self._run("select column_name, data_type from information_schema.columns "
                             "where table_name = %s and table_schema = current_schema()", (table,))
            if rows:
                cols = self._cols[table] = {r["column_name"]: r["data_type"] for r in rows}
        out = {}
        for k, v in row.items():
            if k not in cols:
                continue
            out[k] = Jsonb(v) if cols[k] == "jsonb" and v is not None else v
        return out

    def get_call(self, call_id):
        return self._run("select * from calls where call_id = %s", (call_id,), "one")

    def upsert_call(self, row):
        data = self._prepare("calls", {**row, "updated_at": utcnow()})
        names = list(data)
        updates = ", ".join(f"{n} = excluded.{n}" for n in names if n != "call_id")
        sql = (f"insert into calls ({', '.join(names)}) values ({', '.join(['%s'] * len(names))}) "
               f"on conflict (call_id) do update set {updates} returning *")
        return self._run(sql, tuple(data[n] for n in names), "one")

    def update_call(self, call_id, changes):
        data = self._prepare("calls", {**changes, "updated_at": utcnow()})
        data.pop("call_id", None)
        sets = ", ".join(f"{n} = %s" for n in data)
        return self._run(f"update calls set {sets} where call_id = %s returning *", (*data.values(), call_id), "one")

    def delete_call(self, call_id):
        self._run("delete from calls where call_id = %s", (call_id,), fetch=None)

    def find_call(self, field, value):
        if field not in self.columns("calls"):
            raise ValueError(f"unknown column {field}")
        return self._run(f"select * from calls where {field} = %s order by created_at desc limit 1", (value,), "one")

    def list_calls(self, since_iso=None):
        if since_iso:
            return self._run("select * from calls where created_at >= %s order by created_at", (since_iso,))
        return self._run("select * from calls order by created_at")

    def log_event(self, call_id, kind, payload=None):
        from psycopg.types.json import Jsonb
        self._run("insert into call_events (call_id, kind, payload) values (%s, %s, %s)",
                  (call_id, kind, Jsonb(payload or {})), fetch=None)

    def list_events(self, kind=None, since_iso=None):
        where, params = [], []
        if kind:
            where.append("kind = %s"); params.append(kind)
        if since_iso:
            where.append("created_at >= %s"); params.append(since_iso)
        clause = f" where {' and '.join(where)}" if where else ""
        return self._run(f"select * from call_events{clause} order by created_at", tuple(params))

    def list_call_events(self, call_id):
        return self._run("select * from call_events where call_id = %s order by created_at", (call_id,))

    def claim_processing(self, call_id, stale_after_sec=180):
        """Atomically mark a call as being processed; False if it's done or someone else is on it (and not stuck)."""
        r = self._run("update calls set processing_started_at = now() where call_id = %s and processed_at is null "
                      "and (processing_started_at is null or processing_started_at < now() - make_interval(secs => %s)) "
                      "returning call_id", (call_id, stale_after_sec), "one")
        return bool(r)

    def event_stats(self, kind, since_iso=None, subject_prefix=None):
        """(count, latest created_at) for one kind of event, without loading payloads."""
        sql = ("select count(*) as n, max(created_at) as t from call_events "
               "where kind = %s and coalesce(call_id, '') not like %s")
        params = [kind, TEST_PREFIX + "%"]
        if since_iso:
            sql += " and created_at >= %s"
            params.append(since_iso)
        if subject_prefix:
            sql += " and payload->>'subject' like %s"
            params.append(subject_prefix.replace("%", r"\%") + "%")
        r = self._run(sql, tuple(params), "one") or {}
        return int(r.get("n") or 0), r.get("t")

    # --- sign-in (backend/auth.py) ---
    def get_setting(self, key):
        r = self._run("select value from app_settings where key = %s", (key,), "one")
        return r["value"] if r else None

    def set_setting(self, key, value):
        self._run("insert into app_settings (key, value) values (%s, %s) "
                  "on conflict (key) do update set value = excluded.value", (key, value), fetch=None)

    def create_user(self, email, name, password_hash):
        return self._run("insert into users (email, name, password_hash) values (%s, %s, %s) returning *",
                         (email, name, password_hash), "one")

    def get_user_by_email(self, email):
        return self._run("select * from users where email = %s", (email,), "one")

    def get_user_by_id(self, user_id):
        return self._run("select * from users where id = %s", (user_id,), "one")

    def touch_user_login(self, user_id):
        self._run("update users set last_login_at = now() where id = %s", (user_id,), fetch=None)

    def create_session(self, token_hash, user_id, expires_at):
        self._run("insert into sessions (token_hash, user_id, expires_at) values (%s, %s, %s)",
                  (token_hash, user_id, expires_at), fetch=None)

    def get_session(self, token_hash):
        return self._run("select * from sessions where token_hash = %s", (token_hash,), "one")

    def delete_session(self, token_hash):
        self._run("delete from sessions where token_hash = %s", (token_hash,), fetch=None)

    def record_auth_failure(self, key):
        self._run("insert into auth_attempts (key) values (%s)", (key,), fetch=None)

    def count_auth_failures(self, key, since_iso):
        r = self._run("select count(*) as n from auth_attempts where key = %s and created_at >= %s", (key, since_iso), "one")
        return int((r or {}).get("n") or 0)


def _from_db(row: dict) -> dict:
    """datetime/date -> ISO strings; Decimal -> float (the backend works with plain JSON types)."""
    import datetime as _dt
    from decimal import Decimal
    out = {}
    for k, v in row.items():
        if isinstance(v, (_dt.datetime, _dt.date)):
            v = v.isoformat()
        elif isinstance(v, Decimal):
            v = float(v)
        out[k] = v
    return out


def default_store():
    if config.DATABASE_URL:
        return PostgresStore()
    if config.SUPABASE_URL and config.SUPABASE_KEY:
        return SupabaseStore()
    return LocalStore(config.LOCAL_STORE_PATH)

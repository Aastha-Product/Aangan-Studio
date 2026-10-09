"""Create this project's own Neon database and wire it in (NEON_API_KEY in .env).

    python scripts/setup_neon.py

- Creates (or reuses) a NEW Neon project "aangan-studio" in Singapore (aws-ap-southeast-1) — never an
  existing project's database (class guardrail: don't reuse DBs other projects use).
- Builds the pooled connection string (right for serverless on Vercel), saves DATABASE_URL to .env and
  .env.vercel (both git-ignored), then creates the tables (db/schema.sql). Prints no secrets.
API: https://api-docs.neon.tech/reference/createproject
"""
import re
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config  # noqa: E402
from backend.http import HttpError, request_json  # noqa: E402

API = "https://console.neon.tech/api/v2"
NAME, REGION = "aangan-studio", "aws-ap-southeast-1"


def h():
    return {"Authorization": f"Bearer {config.env('NEON_API_KEY')}"}


def set_env(path: Path, key: str, value: str):
    s = path.read_text(encoding="utf-8") if path.exists() else ""
    s = re.sub(rf"^{key}=.*$", f"{key}={value}", s, flags=re.M) if re.search(rf"^{key}=", s, re.M) \
        else s.rstrip("\n") + f"\n{key}={value}\n"
    path.write_text(s.lstrip("\n"), encoding="utf-8")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not config.env("NEON_API_KEY"):
        sys.exit("NEON_API_KEY is empty in .env (Neon console → Account settings → API keys → Create).")

    org = ""
    try:
        orgs = request_json("GET", f"{API}/users/me/organizations", h()).get("organizations") or []
        org = orgs[0]["id"] if orgs else ""
    except HttpError:
        pass                                    # org API keys don't need (or allow) this lookup
    q = {"search": NAME, **({"org_id": org} if org else {})}
    existing = [p for p in request_json("GET", f"{API}/projects?{urllib.parse.urlencode(q)}", h()).get("projects", [])
                if p.get("name") == NAME]

    if existing:
        pid = existing[0]["id"]
        print(f"Reusing existing Neon project {NAME} ({pid})")
        role = request_json("GET", f"{API}/projects/{pid}/branches", h())["branches"][0]
        dbs = request_json("GET", f"{API}/projects/{pid}/branches/{role['id']}/databases", h())["databases"]
        db = dbs[0]
        uri = request_json("GET", f"{API}/projects/{pid}/connection_uri?" + urllib.parse.urlencode(
            {"database_name": db["name"], "role_name": db["owner_name"], "pooled": "true"}), h())["uri"]
    else:
        body = {"project": {"name": NAME, "region_id": REGION, **({"org_id": org} if org else {})}}
        res = request_json("POST", f"{API}/projects", h(), body)
        pid = res["project"]["id"]
        p = res["connection_uris"][0]["connection_parameters"]
        host = p.get("pooler_host") or p["host"]
        uri = (f"postgresql://{urllib.parse.quote(p['role'])}:{urllib.parse.quote(p['password'])}"
               f"@{host}/{p['database']}?sslmode=require")
        print(f"Created Neon project {NAME} ({pid}) in {REGION}")

    root = config.ROOT
    set_env(root / ".env", "DATABASE_URL", uri)
    set_env(root / ".env.vercel", "DATABASE_URL", uri)
    print("Saved DATABASE_URL to .env and .env.vercel (pooled connection; not printed)")

    import psycopg
    sql = (root / "db" / "schema.sql").read_text(encoding="utf-8")
    for attempt in range(12):                   # a brand-new project needs a few seconds to come up
        try:
            with psycopg.connect(uri, autocommit=True, connect_timeout=15) as conn:
                conn.execute(sql)
                for t in ("calls", "call_events"):
                    n = conn.execute(f"select count(*) from {t}").fetchone()[0]
                    c = conn.execute("select count(*) from information_schema.columns where table_name = %s", (t,)).fetchone()[0]
                    print(f"OK table {t}: {c} columns, {n} rows")
            return
        except psycopg.OperationalError as e:
            if attempt == 11:
                sys.exit(f"Could not connect yet: {str(e)[:160]}. Re-run this script in a minute.")
            time.sleep(5)


if __name__ == "__main__":
    main()

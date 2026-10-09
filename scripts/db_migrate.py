"""Create / update the call-log tables in Neon (DATABASE_URL). Safe to re-run.

    python scripts/db_migrate.py          # apply db/schema.sql, then show the tables and row counts
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config  # noqa: E402


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not config.DATABASE_URL:
        sys.exit("DATABASE_URL is empty: create the Neon database (Vercel → Storage → Neon) and pull the env first.")
    import psycopg
    sql = (config.ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    with psycopg.connect(config.DATABASE_URL, autocommit=True, connect_timeout=15) as conn:
        conn.execute(sql)
        for table in ("calls", "call_events"):
            n = conn.execute(f"select count(*) from {table}").fetchone()[0]
            cols = conn.execute("select count(*) from information_schema.columns where table_name = %s", (table,)).fetchone()[0]
            print(f"OK {table}: {cols} columns, {n} rows")


if __name__ == "__main__":
    main()

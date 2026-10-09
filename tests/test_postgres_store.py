"""PostgresStore (Neon): SQL shape, column filtering, JSON/array handling — no database needed."""
import datetime as dt
import unittest
from decimal import Decimal

from backend.store import PostgresStore, _from_db

COLUMNS = {"call_id": "text", "status": "text", "fields": "jsonb", "flags": "ARRAY", "score": "integer",
           "updated_at": "timestamp with time zone", "created_at": "timestamp with time zone",
           "invitee_uri": "text", "kind": "text", "payload": "jsonb"}


class Captured(PostgresStore):
    def __init__(self):
        super().__init__("postgres://unused")
        self.sql = []
        self._cols = {"calls": COLUMNS, "call_events": COLUMNS}

    def _run(self, sql, params=(), fetch="all"):
        self.sql.append((sql, params))
        return {"call_id": params[0] if params else None} if fetch == "one" else []


class PostgresStoreTests(unittest.TestCase):
    def test_upsert_filters_unknown_keys_and_wraps_json(self):
        s = Captured()
        s.upsert_call({"call_id": "c1", "status": "booked", "fields": {"a": 1}, "flags": ["x"], "not_a_column": 5})
        sql, params = s.sql[-1]
        self.assertIn("insert into calls (call_id, status, fields, flags, updated_at)", sql)
        self.assertIn("on conflict (call_id) do update set status = excluded.status", sql)
        self.assertNotIn("not_a_column", sql)
        self.assertEqual(type(params[2]).__name__, "Jsonb")                  # jsonb column
        self.assertEqual(params[3], ["x"])                                    # text[] stays a list

    def test_update_and_find_are_parameterised(self):
        s = Captured()
        s.update_call("c1", {"status": "cancelled", "call_id": "ignored"})
        sql, params = s.sql[-1]
        self.assertEqual(sql, "update calls set status = %s, updated_at = %s where call_id = %s returning *")
        self.assertEqual(params[0], "cancelled")
        self.assertEqual(params[-1], "c1")
        s.find_call("invitee_uri", "calcom:bk1")
        self.assertIn("where invitee_uri = %s", s.sql[-1][0])
        with self.assertRaises(ValueError):                                   # no SQL injection via field name
            s.find_call("call_id; drop table calls", "x")

    def test_rows_come_back_as_json_types(self):
        row = _from_db({"t": dt.datetime(2026, 10, 9, 10, 0, tzinfo=dt.timezone.utc), "d": dt.date(2026, 11, 12),
                        "n": Decimal("850000"), "s": "x"})
        self.assertEqual(row, {"t": "2026-10-09T10:00:00+00:00", "d": "2026-11-12", "n": 850000.0, "s": "x"})


if __name__ == "__main__":
    unittest.main()

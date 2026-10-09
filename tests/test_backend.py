"""End-to-end backend tests with fake HTTP + a scripted fake Claude. No keys, no network.

    .venv/Scripts/python -m unittest discover -s tests -t .
"""
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest import mock

from backend import actions, agent, calendly, config, dashboard, digest, http, vaani, web
from backend.speech_guard import DEFLECTION_LINE
from backend.store import LocalStore
from tests.test_rules import t01_fields

IST = config.IST


class FakeHTTP:
    """Records every request; answers by URL."""

    def __init__(self):
        self.calls = []
        self.slot_times = [(datetime.now(timezone.utc) + timedelta(days=d)).replace(hour=5, minute=30, second=0, microsecond=0)
                           for d in (2, 3, 4)]
        self.book_status = 201

    def __call__(self, method, url, headers, body, timeout):
        payload = json.loads(body) if body else None
        self.calls.append((method, url, payload))
        if "event_type_available_times" in url:
            return 200, json.dumps({"collection": [
                {"status": "available", "start_time": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "invitees_remaining": 1}
                for t in self.slot_times]}).encode()
        if url.endswith("/invitees"):
            if self.book_status >= 400:
                return self.book_status, b'{"title":"Invalid Argument","message":"time not available"}'
            return 201, json.dumps({"resource": {"uri": "https://api.calendly.com/invitees/INV1",
                                                 "event": "https://api.calendly.com/scheduled_events/EV1",
                                                 "cancel_url": "https://calendly.com/cancellations/INV1",
                                                 "reschedule_url": "https://calendly.com/reschedulings/INV1"}}).encode()
        if "api.resend.com" in url:
            return 200, b'{"id":"email_1"}'
        if "contacts/search" in url:
            return 200, b'{"results":[]}'
        if url.endswith("/crm/v3/objects/contacts"):
            return 201, b'{"id":"C1"}'
        if url.endswith("/crm/v3/objects/deals"):
            return 201, b'{"id":"D1"}'
        if "deals/batch/read" in url:
            return 200, b'{"results":[{"id":"D1","properties":{"hs_is_closed_won":"true","hs_is_closed":"true"}}]}'
        return 404, b'{"error":"unexpected url in test"}'

    def emails(self):
        return [p for m, u, p in self.calls if "api.resend.com" in u]


class Base(unittest.TestCase):
    def setUp(self):
        self.http = FakeHTTP()
        self.patches = [mock.patch.object(http, "transport", self.http)]
        for name, value in {
            "CALENDLY_TOKEN": "cal_test", "CALENDLY_EVENT_TYPE_URI": "https://api.calendly.com/event_types/ET1",
            "CALENDLY_SIGNING_KEY": "whsec_test", "RESEND_API_KEY": "re_test", "EMAIL_FROM": "agent@aangan.test",
            "DESIGNER_EMAILS": ["designer@aangan.test"], "STUDIO_HEAD_ALERT_EMAIL": "nikhil@aangan.test",
            "FRONT_DESK_EMAIL": "desk@aangan.test", "HUBSPOT_TOKEN": "hs_test", "APP_SECRET": "s3cret",
            "VAANI_WEBHOOK_SECRET": "vaani_test", "CRON_SECRET": "cron_test", "DASHBOARD_TOKEN": "dash_test",
            "PUBLIC_BASE_URL": "https://agent.aangan.test",
            "BOOKING_PROVIDER": "calendly", "CALCOM_API_KEY": "cal_test", "CALCOM_EVENT_TYPE_ID": "999",
            "CALCOM_WEBHOOK_SECRET": "calsec_test",
        }.items():
            self.patches.append(mock.patch.object(config, name, value))
        for p in self.patches:
            p.start()
        self.store = LocalStore()
        web._store = self.store

    def tearDown(self):
        for p in self.patches:
            p.stop()
        web._store = None


# --- fake Claude -----------------------------------------------------------------------------------

def text(t):
    return NS(type="text", text=t)


def tool(name, args, id_="tu_1"):
    return NS(type="tool_use", name=name, input=args, id=id_)


def reply(*blocks, stop=None):
    stop = stop or ("tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn")
    return NS(content=list(blocks), stop_reason=stop,
              usage=NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=3000, cache_creation_input_tokens=0))


class FakeClaude:
    def __init__(self, script):
        self.script, self.requests = list(script), []
        self.messages = NS(create=self._create)
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})  # snapshot, like a real request
        return self.script.pop(0)


class AgentEngine(Base):
    def session(self, script):
        self.claude = FakeClaude(script)
        now = datetime(2026, 10, 8, 21, 30, tzinfo=IST)   # Thursday night, after hours
        return agent.CallSession("call-1", "+919811111111", store=self.store, client=self.claude, now=now)

    def test_after_hours_details_and_caching(self):
        s = self.session([reply(text("Lovely. Where is the property?"))])
        self.assertTrue(s.opening.startswith("Hello, Aangan Studio"))
        self.assertNotIn("[greeting]", agent.SYSTEM_PROMPT)
        s.caller_says("Hi, I want to redo my flat.")
        req = self.claude.requests[0]
        self.assertEqual(req["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("{{", req["system"][0]["text"])
        self.assertIn("first thing tomorrow morning", req["system"][1]["text"])
        self.assertTrue(self.store.get_call("call-1")["after_hours"])

    def test_price_is_blocked_logged_and_model_told(self):
        s = self.session([reply(text("For a 2BHK it's typically around 12 lakh.")),
                          reply(text("Where is the property?"))])
        said = s.caller_says("How much for a 2BHK?")
        self.assertEqual(said, DEFLECTION_LINE)
        self.assertEqual(s.blocks, 1)
        self.assertEqual(len(self.store.list_events("speech_guard_block")), 1)
        s.caller_says("Okay. It's in Wakad.")
        last_msgs = self.claude.requests[1]["messages"]
        self.assertEqual(last_msgs[-1]["role"], "system")
        self.assertIn("price filter", last_msgs[-1]["content"])

    def test_books_on_the_call(self):
        slot = self.http.slot_times[0].strftime("%Y-%m-%dT%H:%M:%SZ")
        s = self.session([
            reply(tool("get_open_slots", {})),
            reply(text("I have two options. Which suits you?")),
            reply(tool("book_consultation", {"slot_id": slot, "full_name": "Priya Kulkarni",
                                             "email": "priya.k@gmail.com", "visit_type": "site_visit"}, "tu_2")),
            reply(text("You're booked. You'll get a confirmation email shortly.")),
        ])
        self.assertIn("two options", s.caller_says("Yes please, book me."))
        self.assertIn("booked", s.caller_says("The first one. priya.k@gmail.com, yes that's right."))
        row = self.store.get_call("call-1")
        self.assertEqual((row["status"], row["booked_on_call"], row["visit_type"]), ("booked", True, "site_visit"))
        post = next(p for m, u, p in self.http.calls if u.endswith("/invitees"))
        self.assertEqual(post["tracking"]["utm_content"], "call-1")      # webhook matching key
        self.assertEqual(post["invitee"]["timezone"], "Asia/Kolkata")

    def test_slot_taken_is_reported_not_raised(self):
        self.http.book_status = 400
        slot = self.http.slot_times[0].strftime("%Y-%m-%dT%H:%M:%SZ")
        s = self.session([reply(tool("book_consultation", {"slot_id": slot, "full_name": "A", "email": "a@b.co",
                                                           "visit_type": "studio"})),
                          reply(text("Sorry, that slot just went. How about the next one?"))])
        s.caller_says("Book it.")
        tool_result = json.loads(self.claude.requests[1]["messages"][-1]["content"][0]["content"])
        self.assertEqual(tool_result["reason"], "slot_taken")

    def test_escalation_sends_instant_alert(self):
        s = self.session([reply(tool("escalate_to_studio_head", {"caller_name": "Sheetal", "project": "2BHK Viman Nagar",
                                                                  "issue_one_line": "designer silent 5 days"})),
                          reply(text("I've flagged this. A senior person will call you back first thing tomorrow morning."))])
        s.caller_says("My designer Aryan hasn't replied in five days!")
        self.assertEqual(self.http.emails()[0]["to"], ["nikhil@aangan.test"])
        self.assertEqual(self.store.get_call("call-1")["status"], "escalated")

    def test_invalid_email_rejected_before_calendly(self):
        s = self.session([reply(tool("book_consultation", {"slot_id": "x", "full_name": "A", "email": "priya at gmail",
                                                           "visit_type": "studio"})),
                          reply(text("Could you spell the email again?"))])
        s.caller_says("priya at gmail")
        self.assertFalse(any(u.endswith("/invitees") for _, u, _ in self.http.calls))


class PostCall(Base):
    def test_qualified_but_not_booked_alerts_front_desk(self):
        self.store.upsert_call({"call_id": "c2", "started_at": "2026-10-08T10:00:00+05:30", "caller_number": "+91"})
        fake = {"fields": t01_fields(), "usage": {"input_tokens": 1000, "output_tokens": 300, "thinking_tokens": 0}}
        with mock.patch.object(actions, "extract_fields", return_value=fake):
            from tests.test_rules import T01
            row = actions.process_completed_call(self.store, "c2", T01)
        self.assertEqual((row["decision"], row["priority"], row["status"]), ("Qualified", "P2", "booking_pending"))
        self.assertEqual(self.http.emails()[0]["to"], ["desk@aangan.test"])

    def test_nurture_gets_follow_up_date(self):
        self.store.upsert_call({"call_id": "c3", "started_at": "2026-10-08T10:00:00+05:30"})
        fake = {"fields": t01_fields(weeks_until_needed=3, open_to_later_start=True), "usage": {}}
        with mock.patch.object(actions, "extract_fields", return_value=fake):
            row = actions.process_completed_call(self.store, "c3", "Caller: before Diwali")
        self.assertEqual((row["status"], row["follow_up_on"]), ("nurture", "2026-11-12"))


def wsgi(method, path, body=b"", headers=None):
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path.split("?")[0],
               "QUERY_STRING": path.split("?", 1)[1] if "?" in path else "",
               "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body), **(headers or {})}
    out = {}
    data = b"".join(web.app(environ, lambda s, h: out.update(status=s, headers=h)))
    return out["status"], data.decode()


class CalendlyWebhooks(Base):
    def _post(self, event, payload, key="whsec_test"):
        body = json.dumps({"event": event, "payload": payload}).encode()
        return wsgi("POST", "/api/calendly/webhook", body, {"HTTP_CALENDLY_WEBHOOK_SIGNATURE": calendly.sign(body, key)})

    def _invitee(self, **extra):
        return {"uri": "https://api.calendly.com/invitees/INV1", "email": "priya.k@gmail.com", "name": "Priya Kulkarni",
                "tracking": {"utm_content": "c9"}, "cancel_url": "cx", "reschedule_url": "rx", "rescheduled": False,
                "scheduled_event": {"uri": "https://api.calendly.com/scheduled_events/EV1",
                                    "start_time": "2026-10-10T05:30:00.000000Z",
                                    "event_memberships": [{"user_email": "ritika@aangan.test", "user_name": "Ritika"}]},
                **extra}

    def _qualified_row(self):
        from qualify.rules import evaluate
        from tests.test_rules import T01
        from dataclasses import asdict
        r = evaluate("c9", t01_fields(), T01)
        self.store.upsert_call({"call_id": "c9", "caller_number": "+919811111111", "started_at": "2026-10-08T10:00:00+05:30",
                                "fields": t01_fields(), "decision": r.decision, "priority": r.priority, "score": r.score,
                                "score_lines": [asdict(l) for l in r.score_lines], "gates": [asdict(g) for g in r.gates],
                                "flags": r.flags, "status": "booked", "booked_on_call": True})

    def test_rejects_bad_signature(self):
        status, _ = self._post("invitee.created", self._invitee(), key="wrong")
        self.assertTrue(status.startswith("401"))

    def test_created_sends_report_card_and_hubspot_deal(self):
        self._qualified_row()
        status, _ = self._post("invitee.created", self._invitee())
        self.assertTrue(status.startswith("200"))
        row = self.store.get_call("c9")
        self.assertEqual((row["designer_email"], row["hubspot_deal_id"]), ("ritika@aangan.test", "D1"))
        card = self.http.emails()[0]
        self.assertEqual(card["to"], ["ritika@aangan.test"])
        self.assertIn("[P2] Consultation", card["subject"])
        self.assertIn("Interest 100/100", card["subject"])
        self.assertIn("Shruti Joshi", card["text"])
        self.assertIn("/api/reask?", card["text"])
        deal = next(p for m, u, p in self.http.calls if u.endswith("/crm/v3/objects/deals"))
        self.assertNotIn("amount", deal["properties"])              # THE CUT
        # second delivery of the same webhook must not send a second card
        self._post("invitee.created", self._invitee())
        self.assertEqual(len([e for e in self.http.emails() if "Consultation" in e["subject"]]), 1)

    def test_cancel_and_no_show(self):
        self._qualified_row()
        self._post("invitee.created", self._invitee())
        self._post("invitee.canceled", self._invitee(status="canceled"))
        self.assertEqual(self.store.get_call("c9")["status"], "cancelled")
        subjects = [e["subject"] for e in self.http.emails()]
        self.assertTrue(any(s.startswith("Cancelled") for s in subjects))
        self.assertTrue(any("call back" in s for s in subjects))
        self._post("invitee_no_show.created", {"invitee": "https://api.calendly.com/invitees/INV1"})
        self.assertEqual(self.store.get_call("c9")["status"], "no_show")

    def test_reschedule_cancel_is_ignored(self):
        self._qualified_row()
        self._post("invitee.created", self._invitee())
        self._post("invitee.canceled", self._invitee(rescheduled=True))
        self.assertEqual(self.store.get_call("c9")["status"], "booked")


class OtherEndpoints(Base):
    # Payload shapes copied from docs.vaanivoice.ai/guides/webhook-setup (Oct 2026)
    POSTPROC = {"event": "call_postprocessing", "call_id": "inbound-1784899978-36cec598",
                "timestamp": "2026-10-09T13:34:22+00:00",
                "data": {"room_name": "inbound-1784899978-36cec598", "call_id": "inbound-1784899978-36cec598",
                         "call_duration": 55150.02, "end_reason": "Call ended", "summary": "…",
                         "recording_url": "https://example/rec",
                         "transcript": "[13:33:14] AGENT: Hello, Aangan Studio.\n\n[13:33:19] USER: Hi, 3BHK in Baner."}}

    def test_vaani_webhook_auth_and_native_mode(self):
        body = json.dumps(self.POSTPROC).encode()
        self.assertTrue(wsgi("POST", "/api/vaani/webhook?key=wrong", body)[0].startswith("401"))
        self.assertTrue(wsgi("POST", "/api/vaani/webhook", body)[0].startswith("401"))
        with mock.patch.object(actions, "process_completed_call") as proc:
            status, out = wsgi("POST", "/api/vaani/webhook?key=vaani_test", body)
        self.assertIn("processed", out)
        row = self.store.get_call("inbound-1784899978-36cec598")
        self.assertEqual(row["duration_sec"], 55)                          # milliseconds -> seconds
        self.assertEqual(proc.call_args[0][2], "Agent: Hello, Aangan Studio.\nCaller: Hi, 3BHK in Baner.")

    def test_lifecycle_events(self):
        for ev in ({"event": "call_started", "room_name": "r1", "status": "active", "phone_number": "+919812345678"},
                   {"event": "call_ended", "room_name": "r1", "call_duration": 42.5, "end_reason": "x"}):
            wsgi("POST", "/api/vaani/webhook?key=vaani_test", json.dumps(ev).encode())
        row = self.store.get_call("r1")
        self.assertEqual((row["caller_number"], row["duration_sec"]), ("+919812345678", 42))   # seconds here

    def test_vaani_retry_does_not_process_twice(self):
        body = json.dumps(self.POSTPROC).encode()
        with mock.patch.object(actions, "process_completed_call") as proc:
            wsgi("POST", "/api/vaani/webhook?key=vaani_test", body)
            status, out = wsgi("POST", "/api/vaani/webhook?key=vaani_test", body)   # a retry
        self.assertEqual(proc.call_count, 1)
        self.assertIn("already processed", out)

    def test_byol_call_is_not_reprocessed_by_webhook(self):
        self.store.upsert_call({"call_id": "inbound-1784899978-36cec598", "processing_started_at": "2026-10-09T13:34:00+00:00"})
        with mock.patch.object(actions, "process_completed_call") as proc:
            wsgi("POST", "/api/vaani/webhook?key=vaani_test", json.dumps(self.POSTPROC).encode())
        proc.assert_not_called()

    def test_unknown_event_is_acknowledged(self):
        body = json.dumps({"event": "human_transfer_initiated", "room_name": "r2"}).encode()
        self.assertTrue(wsgi("POST", "/api/vaani/webhook?key=vaani_test", body)[0].startswith("200"))

    def test_price_spoken_by_native_agent_is_caught(self):
        self.store.upsert_call({"call_id": "v10", "started_at": "2026-10-08T10:00:00+05:30"})
        fake = {"fields": t01_fields(), "usage": {}}
        transcript = "Agent: Hello\nCaller: How much?\nAgent: Usually around 12 lakh for a 2BHK.\nCaller: ok"
        with mock.patch.object(actions, "extract_fields", return_value=fake):
            row = actions.process_completed_call(self.store, "v10", transcript)
        self.assertTrue(row["flags"][0].startswith("⚠ AGENT SPOKE A PRICE"))
        self.assertTrue(any("spoke a price" in e["subject"] and e["to"] == ["nikhil@aangan.test"] for e in self.http.emails()))

    def test_reask_link(self):
        self.store.upsert_call({"call_id": "c5"})
        from backend.emails import reask_sig
        self.assertTrue(wsgi("GET", "/api/reask?call_id=c5&sig=nope")[0].startswith("403"))
        self.assertTrue(wsgi("GET", f"/api/reask?call_id=c5&sig={reask_sig('c5')}")[0].startswith("200"))
        self.assertTrue(self.store.get_call("c5")["had_to_reask"])

    def test_digest_cron(self):
        now = config.now_ist()
        self.store.upsert_call({"call_id": "r1", "started_at": now.isoformat(), "status": "not_qualified",
                                "decision": "Not qualified", "reason_code": "BUDGET_MISALIGNED", "caller_name": "Kharadi caller",
                                "fields": {"location_text": "Kharadi", "rooms": ["kitchen", "bedroom"],
                                           "budget_quote": "My budget is 1 to 1.5 lakh maximum for everything."},
                                "gates": [{"number": 4, "name": "Budget", "status": "fail", "reason_code": "BUDGET_MISALIGNED",
                                           "note": "volunteered budget clearly below scope"}]})
        self.assertTrue(wsgi("GET", "/api/cron/digest")[0].startswith("401"))
        status, out = wsgi("GET", "/api/cron/digest", headers={"HTTP_AUTHORIZATION": "Bearer cron_test"})
        self.assertEqual(json.loads(out)["rows"], 1)
        mail = self.http.emails()[0]
        self.assertEqual(mail["to"], ["designer@aangan.test"])
        self.assertIn("BUDGET_MISALIGNED", mail["text"])
        self.assertIn("1 to 1.5 lakh", mail["text"])
        self.assertTrue(self.store.get_call("r1")["digest_sent_at"])

    def test_dashboard_auth_and_render(self):
        now = config.now_ist()
        self.store.upsert_call({"call_id": "d1", "started_at": now.isoformat(), "answered_at": now.isoformat(),
                                "decision": "Qualified", "status": "booked", "booked_on_call": True,
                                "booked_at": now.isoformat(), "duration_sec": 300,
                                "usage": {"claude": {"model": "claude-opus-5-5", "input": 20000, "output": 2000,
                                                     "cache_read": 60000, "cache_write": 4000}}})
        self.assertTrue(wsgi("GET", "/dashboard")[0].startswith("401"))
        status, page = wsgi("GET", "/dashboard?token=dash_test")
        self.assertTrue(status.startswith("200"))
        self.assertIn("Consultations booked on the call", page)
        self.assertIn("COST_VAANI_INR_PER_MIN", page)        # missing rate is surfaced, not hidden
        status, page = wsgi("GET", "/dashboard/call?call_id=d1&token=dash_test")
        self.assertTrue(status.startswith("200"))

    def test_cost_maths(self):
        c = {"duration_sec": 120, "usage": {"claude": {"model": "claude-opus-5-5", "input": 1_000_000, "output": 0,
                                                       "cache_read": 0, "cache_write": 0}, "emails": 2}}
        with mock.patch.object(config, "VAANI_INR_PER_MIN", 5.0), mock.patch.object(config, "USD_INR_RATE", 80.0), \
                mock.patch.object(config, "EMAIL_INR_EACH", 0.5):
            cost = dashboard.call_cost(c)
            self.assertAlmostEqual(cost["usd"], 4.0)
            self.assertAlmostEqual(dashboard.to_inr(cost), 4.0 * 80 + 2 * 5 + 1.0)


class Helpers(unittest.TestCase):
    def test_next_working_morning(self):
        fri_night = datetime(2026, 10, 9, 22, 0, tzinfo=IST)
        mon_early = datetime(2026, 10, 12, 8, 0, tzinfo=IST)
        self.assertEqual(agent.next_working_morning(fri_night), "on Monday morning")
        self.assertEqual(agent.next_working_morning(mon_early), "this morning")
        self.assertEqual(agent.call_details(datetime(2026, 10, 8, 11, 0, tzinfo=IST), None)["senior_callback_promise"],
                         "within 15 minutes")

    def test_spoken_label_and_filter(self):
        t = datetime(2026, 10, 10, 5, 30, tzinfo=timezone.utc)   # Sat 11:00 IST
        self.assertEqual(calendly.spoken_label(t), "Saturday 10 October, 11 am")
        slots = [{"start_time": "2026-10-10T05:30:00Z"}, {"start_time": "2026-10-12T10:30:00Z"}]
        self.assertEqual(len(calendly.filter_slots(slots, "Saturday morning")), 1)

    def test_vaani_transcript_formats(self):
        self.assertEqual(vaani.normalise_transcript([{"role": "assistant", "content": "Hi"}, {"role": "user", "content": "Yo"}]),
                         "Agent: Hi\nCaller: Yo")

    def test_calendly_signature_tolerance(self):
        body = b'{"a":1}'
        old = calendly.sign(body, "k", t=1_000_000)
        self.assertFalse(calendly.verify_signature(body, old, "k", now=1_000_000 + 600))
        self.assertTrue(calendly.verify_signature(body, old, "k", now=1_000_000 + 60))


if __name__ == "__main__":
    unittest.main()

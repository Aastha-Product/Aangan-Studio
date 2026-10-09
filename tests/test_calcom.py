"""Cal.com booking provider: slots, booking on the call, webhooks — offline, payloads from Cal.com's docs."""
import json
import urllib.parse
from datetime import datetime, timezone
from unittest import mock

from backend import agent, calcom, config
from tests.test_backend import Base, FakeClaude, reply, text, tool, wsgi
from tests.test_rules import T01, t01_fields

IST = config.IST


class FakeCal:
    """Answers api.cal.com like the v2 docs; records method, url, headers, body."""

    def __init__(self, fallback):
        self.fallback, self.calls, self.book_status = fallback, [], 201

    def __call__(self, method, url, headers, body, timeout):
        payload = json.loads(body) if body else None
        self.calls.append((method, url, headers, payload))
        if "api.cal.com/v2/slots" in url:
            return 200, json.dumps({"status": "success", "data": {
                "2026-10-12": [{"start": "2026-10-12T11:00:00.000+05:30"}, {"start": "2026-10-12T16:00:00.000+05:30"}],
                "2026-10-13": [{"start": "2026-10-13T10:00:00.000+05:30"}]}}).encode()
        if url.endswith("api.cal.com/v2/bookings"):
            if self.book_status >= 400:
                return self.book_status, b'{"status":"error","error":{"message":"slot no longer available"}}'
            return 201, json.dumps({"status": "success", "data": {
                "id": 77, "uid": "bk_abc123", "start": payload["start"], "status": "accepted",
                "hosts": [{"id": 1, "name": "Ritika", "email": "ritika@aangan.test"}],
                "attendees": [payload["attendee"]]}}).encode()
        return self.fallback(method, url, headers, body, timeout)

    def cal(self, suffix):
        return [c for c in self.calls if c[1].split("?")[0].endswith(suffix)]


class CalcomBase(Base):
    def setUp(self):
        super().setUp()
        self.cal = FakeCal(self.http)
        from backend import http
        for p in (mock.patch.object(http, "transport", self.cal), mock.patch.object(config, "BOOKING_PROVIDER", "calcom")):
            p.start()
            self.patches.append(p)


class Slots(CalcomBase):
    def test_open_slots_parse_and_headers(self):
        slots = calcom.open_slots(now=datetime(2026, 10, 9, 6, 0, tzinfo=timezone.utc))
        self.assertEqual([s["label"] for s in slots],
                         ["Monday 12 October, 11 am", "Monday 12 October, 4 pm", "Tuesday 13 October, 10 am"])
        self.assertEqual(slots[0]["id"], "2026-10-12T05:30:00Z")
        method, url, headers, _ = self.cal.cal("/v2/slots")[0]
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual((q["eventTypeId"], q["timeZone"]), (["999"], ["Asia/Kolkata"]))
        self.assertEqual(headers["cal-api-version"], "2024-09-04")
        self.assertEqual(headers["Authorization"], "Bearer cal_test")
        self.assertIn("AanganStudioAgent", headers["User-Agent"])          # Cloudflare blocks Python's default

    def test_preference_filter(self):
        slots = calcom.open_slots("afternoon", now=datetime(2026, 10, 9, 6, 0, tzinfo=timezone.utc))
        self.assertEqual([s["label"] for s in slots], ["Monday 12 October, 4 pm"])


class BookingOnTheCall(CalcomBase):
    def session(self, script):
        self.claude = FakeClaude(script)
        return agent.CallSession("call-cal", "+919811111111", store=self.store, client=self.claude,
                                 now=datetime(2026, 10, 9, 20, 0, tzinfo=IST))

    def test_site_visit_booking(self):
        s = self.session([reply(tool("get_open_slots", {})), reply(text("Two options: Monday 11 am or 4 pm.")),
                          reply(tool("book_consultation", {"slot_id": "2026-10-12T05:30:00Z", "full_name": "Priya Kulkarni",
                                                           "email": "priya.k@gmail.com", "visit_type": "site_visit",
                                                           "site_address": "Flat 302, Dahanukar Colony, Kothrud"}, "tu_2")),
                          reply(text("You're booked for Monday 12 October at 11 am, a site visit."))])
        s.caller_says("Yes please.")
        s.caller_says("Monday 11. priya.k@gmail.com. Flat 302, Dahanukar Colony, Kothrud.")
        _, _, headers, body = self.cal.cal("/v2/bookings")[0]
        self.assertEqual(headers["cal-api-version"], "2026-02-25")
        self.assertEqual(body["eventTypeId"], 999)
        self.assertEqual(body["start"], "2026-10-12T05:30:00Z")
        self.assertEqual(body["attendee"], {"name": "Priya Kulkarni", "email": "priya.k@gmail.com",
                                            "timeZone": "Asia/Kolkata", "language": "en", "phoneNumber": "+919811111111"})
        self.assertEqual(body["location"], {"type": "attendeeAddress", "address": "Flat 302, Dahanukar Colony, Kothrud"})
        self.assertEqual(body["metadata"]["call_id"], "call-cal")              # webhook matching key
        row = self.store.get_call("call-cal")
        self.assertEqual((row["status"], row["invitee_uri"], row["designer_email"], row["booking_provider"]),
                         ("booked", "calcom:bk_abc123", "ritika@aangan.test", "calcom"))
        self.assertEqual(row["site_address"], "Flat 302, Dahanukar Colony, Kothrud")

    def test_studio_booking_uses_event_type_address(self):
        s = self.session([reply(tool("book_consultation", {"slot_id": "2026-10-12T05:30:00Z", "full_name": "A B",
                                                           "email": "a@b.co", "visit_type": "studio"})),
                          reply(text("Booked at the studio."))])
        s.caller_says("Studio please, a@b.co")
        self.assertEqual(self.cal.cal("/v2/bookings")[0][3]["location"], {"type": "address"})

    def test_slot_taken(self):
        self.cal.book_status = 400
        s = self.session([reply(tool("book_consultation", {"slot_id": "2026-10-12T05:30:00Z", "full_name": "A B",
                                                           "email": "a@b.co", "visit_type": "studio"})),
                          reply(text("Sorry, that one just went."))])
        s.caller_says("Book it")
        result = json.loads(self.claude.requests[1]["messages"][-1]["content"][0]["content"])
        self.assertEqual(result["reason"], "slot_taken")


class Webhooks(CalcomBase):
    def _post(self, trigger, payload, secret="calsec_test"):
        body = json.dumps({"triggerEvent": trigger, "createdAt": "2026-10-09T14:00:00Z", "payload": payload}).encode()
        return wsgi("POST", "/api/calcom/webhook", body, {"HTTP_X_CAL_SIGNATURE_256": calcom.sign(body, secret)})

    def _booking(self, **extra):
        return {"uid": "bk_abc123", "startTime": "2026-10-12T05:30:00Z", "endTime": "2026-10-12T06:30:00Z",
                "organizer": {"name": "Ritika", "email": "ritika@aangan.test", "timeZone": "Asia/Kolkata"},
                "attendees": [{"name": "Priya Kulkarni", "email": "priya.k@gmail.com", "timeZone": "Asia/Kolkata"}],
                "metadata": {"call_id": "c9"}, "status": "ACCEPTED", **extra}

    def _qualified_row(self):
        from dataclasses import asdict
        from qualify.rules import evaluate
        r = evaluate("c9", t01_fields(), T01)
        self.store.upsert_call({"call_id": "c9", "caller_number": "+919811111111", "started_at": "2026-10-09T13:00:00+05:30",
                                "fields": t01_fields(), "decision": r.decision, "priority": r.priority, "score": r.score,
                                "score_lines": [asdict(l) for l in r.score_lines], "gates": [asdict(g) for g in r.gates],
                                "flags": r.flags, "status": "booked", "booked_on_call": True,
                                "visit_type": "site_visit", "site_address": "Flat 302, Dahanukar Colony, Kothrud",
                                "booking_provider": "calcom"})

    def test_rejects_bad_signature(self):
        self.assertTrue(self._post("BOOKING_CREATED", self._booking(), secret="wrong")[0].startswith("401"))

    def test_created_sends_report_card_once(self):
        self._qualified_row()
        status, out = self._post("BOOKING_CREATED", self._booking())
        self.assertIn('"booked"', out)
        row = self.store.get_call("c9")
        self.assertEqual((row["invitee_uri"], row["designer_email"], row["hubspot_deal_id"]),
                         ("calcom:bk_abc123", "ritika@aangan.test", "D1"))
        card = self.http.emails()[0]
        self.assertEqual(card["to"], ["ritika@aangan.test"])
        self.assertIn("Site visit — Flat 302, Dahanukar Colony, Kothrud", card["text"])
        self.assertIn("(calcom)", card["text"])
        self._post("BOOKING_CREATED", self._booking())                       # redelivery
        self.assertEqual(len([e for e in self.http.emails() if "Consultation" in e["subject"]]), 1)

    def test_reschedule_cancel_no_show(self):
        self._qualified_row()
        self._post("BOOKING_CREATED", self._booking())
        new = self._booking(uid="bk_new456", startTime="2026-10-14T10:30:00Z", rescheduleUid="bk_abc123", metadata={})
        self.assertIn("rescheduled", self._post("BOOKING_RESCHEDULED", new)[1])
        row = self.store.get_call("c9")
        self.assertEqual((row["invitee_uri"], row["slot_start"]), ("calcom:bk_new456", "2026-10-14T10:30:00Z"))
        self.assertTrue(any(e["subject"].startswith("Time changed") for e in self.http.emails()))
        self._post("BOOKING_NO_SHOW_UPDATED", {"bookingUid": "bk_new456", "attendees": [{"email": "priya.k@gmail.com", "noShow": True}]})
        self.assertEqual(self.store.get_call("c9")["status"], "no_show")
        self._post("BOOKING_NO_SHOW_UPDATED", {"bookingUid": "bk_new456", "attendees": [{"email": "priya.k@gmail.com", "noShow": False}]})
        self.assertEqual(self.store.get_call("c9")["status"], "booked")
        self._post("BOOKING_CANCELLED", self._booking(uid="bk_new456", metadata={}))
        self.assertEqual(self.store.get_call("c9")["status"], "cancelled")
        self.assertTrue(any(e["subject"].startswith("Cancelled") for e in self.http.emails()))

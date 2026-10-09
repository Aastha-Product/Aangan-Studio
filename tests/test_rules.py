"""Unit tests for the deterministic rules — no API key needed.

    python -m unittest discover -s tests -t .
"""
import unittest

from qualify.budget import check_budget_fit
from qualify.quotes import quote_in_transcript
from qualify.rules import evaluate
from qualify.transcripts import load_phone_calls

CALLS = load_phone_calls()
T01 = CALLS["T01"].text


def t01_fields(**overrides) -> dict:
    """A qualified, flexible-timeline caller with quotes taken verbatim from T01."""
    f = {
        "has_conversation": True, "caller_name": "Priya",
        "existing_client_issue": False, "returning_caller_let_down": False,
        "property_category": "residential", "scope_unit": "full_home",
        "rooms": ["kitchen", "living room", "bedroom", "bedroom"],
        "rooms_quote": "We want to redo the whole thing — kitchen, living room, both bedrooms.",
        "size_sqft": 1400, "size_quote": "About 1,400 sq ft carpet.",
        "service_wanted": "design_and_execution", "just_exploring": False,
        "location_text": "Kothrud — Dahanukar Colony", "location_city": "pune",
        "rented": False, "structural_change_requested": False, "current_state": "builder_finished",
        "timeline_discussed": True, "timeline_text": "done by March, no rush",
        "weeks_until_needed": 30, "deadline_driver": "preference_only", "weeks_until_site_available": 0,
        "open_to_later_start": False, "wants_meeting_within_2_weeks": False,
        "budget_volunteered": False, "budget_max_inr": None,
        "decision_maker": "owner_or_authorised",
        "decision_maker_quote": "He knows we’re calling and is happy to go ahead.",
        "source": "named_referral", "referrer_name": "Shruti Joshi",
        "source_quote": "I got your number from a friend — Shruti Joshi.",
        "booking_response": "agreed", "booking_quote": "Sure. (gives number)",
        "readiness": "clear_target_date", "readiness_quote": "We'd like to be done by March.",
        "asked_for_price": False, "price_ask_count": 0,
        "wants_site_visit": False, "call_dropped_and_returned": False,
    }
    f.update(overrides)
    return f


def run(**overrides):
    return evaluate("TX", t01_fields(**overrides), T01)


class TranscriptParsing(unittest.TestCase):
    def test_twenty_phone_calls(self):
        self.assertEqual(sorted(CALLS), [f"T{i:02d}" for i in range(1, 21)])

    def test_boundaries(self):
        self.assertIn("MISSED CALL", CALLS["T08"].text)
        self.assertIn("Second call", CALLS["T17"].text)
        self.assertNotIn("WHATSAPP", CALLS["T20"].text)
        self.assertNotIn("T12", CALLS["T11"].text.split("\n", 1)[1])

    def test_after_hours(self):
        self.assertTrue(CALLS["T08"].after_hours)    # 10:47pm
        self.assertTrue(CALLS["T07"].after_hours)    # 9:44am
        self.assertFalse(CALLS["T01"].after_hours)   # 10:23am


class Quotes(unittest.TestCase):
    def test_curly_vs_straight_apostrophe(self):
        self.assertTrue(quote_in_transcript("We'd like to be done by March", T01))

    def test_line_wrap_and_dash(self):
        self.assertTrue(quote_in_transcript("I got your number from a friend - Shruti Joshi", T01))

    def test_invented_quote_rejected(self):
        self.assertFalse(quote_in_transcript("We have a budget of twenty lakh", T01))
        self.assertFalse(quote_in_transcript("", T01))


class Routing(unittest.TestCase):
    def test_qualified_p2(self):
        r = run()
        self.assertEqual((r.decision, r.priority, r.reason_code), ("Qualified", "P2", ""))
        self.assertEqual(r.score, 100)

    def test_p1_fixed_date(self):
        for driver in ("move_in", "possession", "go_live"):
            self.assertEqual(run(deadline_driver=driver).priority, "P1", driver)

    def test_p1_meeting_soon_or_let_down(self):
        self.assertEqual(run(wants_meeting_within_2_weeks=True).priority, "P1")
        self.assertEqual(run(returning_caller_let_down=True).priority, "P1")

    def test_price_question_never_lowers_priority(self):
        r = run(asked_for_price=True, price_ask_count=2, deadline_driver="move_in")
        self.assertEqual((r.decision, r.priority), ("Qualified", "P1"))
        self.assertTrue(any("Asked for price x2" in f for f in r.flags))

    def test_no_data(self):
        r = evaluate("T08", {"has_conversation": False}, CALLS["T08"].text)
        self.assertEqual((r.decision, r.reason_code), ("No data", "NO_INFO"))

    def test_escalate_existing_client(self):
        r = run(existing_client_issue=True)
        self.assertEqual((r.decision, r.reason_code), ("Escalate", "EXISTING_CLIENT_COMPLAINT"))

    def test_just_exploring_is_nurture(self):
        r = run(just_exploring=True, booking_response="not_offered")
        self.assertEqual((r.decision, r.reason_code), ("Nurture", ""))


class Gates(unittest.TestCase):
    def test_out_of_area(self):
        r = run(location_text="Nashik", location_city="other_city")
        self.assertEqual((r.decision, r.reason_code), ("Not qualified", "OUT_OF_AREA"))

    def test_pune_locality_not_on_list_passes_with_flag(self):
        r = run(location_text="Kharadi", location_city="pune")
        self.assertEqual(r.decision, "Qualified")
        self.assertTrue(any("Confirm area" in f for f in r.flags))

    def test_advice_only(self):
        self.assertEqual(run(service_wanted="advice_or_ideas_only").reason_code, "ADVICE_ONLY")
        self.assertEqual(run(service_wanted="diy_execution").reason_code, "ADVICE_ONLY")

    def test_out_of_scope_types(self):
        self.assertEqual(run(property_category="restaurant_hotel_hospitality").reason_code, "OUT_OF_SCOPE_TYPE")
        self.assertEqual(run(property_category="gym").reason_code, "OUT_OF_SCOPE_TYPE")
        self.assertEqual(run(service_wanted="vastu_only").reason_code, "OUT_OF_SCOPE_TYPE")
        self.assertEqual(run(property_category="office_clinic_studio", size_sqft=5000).reason_code, "OUT_OF_SCOPE_TYPE")

    def test_office_sizes(self):
        self.assertEqual(run(property_category="office_clinic_studio", size_sqft=180).reason_code, "TOO_SMALL")
        self.assertEqual(run(property_category="office_clinic_studio", size_sqft=800).decision, "Qualified")

    def test_timeline_impossible(self):
        r = run(weeks_until_needed=3, deadline_driver="festival_or_event")
        self.assertEqual((r.decision, r.reason_code), ("Not qualified", "TIMELINE_IMPOSSIBLE"))

    def test_timeline_open_to_later_is_nurture(self):
        r = run(weeks_until_needed=3, open_to_later_start=True)
        self.assertEqual((r.decision, r.reason_code), ("Nurture", "TIMELINE_IMPOSSIBLE"))

    def test_site_unavailable_too_long(self):
        self.assertEqual(run(weeks_until_site_available=20).reason_code, "TIMELINE_IMPOSSIBLE")

    def test_six_weeks_is_enough(self):
        r = run(weeks_until_needed=6, deadline_driver="possession")
        self.assertEqual((r.decision, r.priority), ("Qualified", "P1"))
        self.assertTrue(any("Tight timeline" in f for f in r.flags))

    def test_timeline_not_asked_still_qualifies(self):
        r = run(timeline_discussed=False, weeks_until_needed=None, readiness="not_discussed")
        self.assertEqual(r.decision, "Qualified")
        self.assertTrue(any("timeline not asked" in f for f in r.flags))

    def test_budget_misaligned(self):
        r = run(rooms=["kitchen", "bedroom"], scope_unit="floor_or_multi_room", size_sqft=550,
                budget_volunteered=True, budget_max_inr=150_000)
        self.assertEqual((r.decision, r.reason_code), ("Not qualified", "BUDGET_MISALIGNED"))

    def test_budget_not_mentioned_passes(self):
        self.assertEqual(run(budget_volunteered=False).decision, "Qualified")

    def test_decision_maker(self):
        r = run(decision_maker="deciders_will_attend")
        self.assertEqual(r.decision, "Qualified")
        self.assertEqual(next(l for l in r.score_lines if l.factor == "Decision-maker").points, 8)
        self.assertEqual(run(decision_maker="unknown").decision, "Qualified")
        self.assertEqual(run(decision_maker="researching_no_authority").reason_code, "NOT_DECISION_MAKER")

    def test_two_fails_primary_reason(self):
        r = run(location_text="Mumbai", location_city="other_city", weeks_until_needed=2)
        self.assertEqual(r.reason_code, "OUT_OF_AREA")
        self.assertEqual(r.all_fail_codes, ["OUT_OF_AREA", "TIMELINE_IMPOSSIBLE"])


class Score(unittest.TestCase):
    def test_invented_quote_scores_zero(self):
        r = run(source_quote="My cousin Ramesh sent me")
        line = next(l for l in r.score_lines if l.factor == "Warm source")
        self.assertEqual(line.points, 0)
        self.assertIn("quote not found", line.note)

    def test_partials(self):
        r = run(booking_response="thinking", booking_quote="Lovely. Can I take your number",
                readiness="vague_date", size_sqft=None, source="online")
        pts = {l.factor: l.points for l in r.score_lines}
        self.assertEqual(pts, {"Commitment": 10, "Readiness": 10, "Scope clarity": 10,
                               "Decision-maker": 15, "Warm source": 5})


class Budget(unittest.TestCase):
    def test_labels_only(self):
        self.assertEqual(check_budget_fit(150_000, "residential", "floor_or_multi_room", ["kitchen", "bedroom"], 550), "misaligned")
        self.assertEqual(check_budget_fit(2_500_000, "residential", "full_home", ["whole home"], 1400), "aligned")
        self.assertEqual(check_budget_fit(None, "residential", "full_home", [], None), "unclear")


if __name__ == "__main__":
    unittest.main()

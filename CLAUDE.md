# Aangan Studio — Phone Enquiry Agent (project "vaani")

Read this file first. It is the agreed plan. Do not change the rules in it without asking Aastha.

## What we are building

A voice agent that answers **every incoming phone call** to Aangan Studio (interior design, Pune), day or night, qualifies the caller against Nikhil's rubric, **books qualified callers into a designer's Calendly on the call**, and emails the designer a report card. Every call is logged and shown on a dashboard with its own running cost.

- Scope: **incoming phone calls only.** WhatsApp and the web form are out of scope (they can plug into the Input stage later).
- Owner of the system: Aastha (founder's office). Owner of the rubric: Nikhil. Owner of the consultation and the price: the designer.

## Why (from the case brief, data/case_brief.pdf)

- ~200 enquiries a month; ~48% get no response within 48 hours (~96 a month).
- Answered in under 1 hour converts at 4x the rate of next-day.
- ~33% of enquiries arrive outside 10am–7pm; the front desk is 2 people.
- Designers receive leads as forwarded messages with no context and re-ask the same questions.
- Average project value ₹8–14 lakh.
- Nikhil's ask: "Every enquiry must get answered in five minutes, day or night. The ones worth my designers' time should reach them with everything already asked." (He also asked for live pricing — see THE CUT.)

## THE CUT — the agent never speaks a price (hard rule)

- context/pricing.md: "No number from this guide should be quoted to a client."
- Never say any number, range, or per-sq-ft figure. Never say "It'll cost around…", "Our rates start at…", "For a 2BHK it's typically…".
- The ONLY allowed answer to a pricing question (verbatim):
  > "Pricing depends on the site, the materials you choose, and the scope — your designer will walk you through it in detail at the consultation. I can book that for you right now if you'd like."
- Build a **speech guard**: before any agent utterance is spoken, block it if it contains a currency amount, "lakh", "per sq ft", "₹", or a price range. Replace with the line above. Log every block.
- Also cut: rejecting leads on profit/breakeven. There is no cost or margin data, and qualified.md says "Don't probe" budget.
- Asking for a price NEVER lowers priority (T02 and T13 asked for price and booked).

## Context files (the agent's only knowledge)

| File | Use |
|---|---|
| context/services.md | What Aangan does/doesn't do, service area list, timelines, min 6-week lead time |
| context/qualified.md | The 5-criteria rubric (source of truth for qualification) |
| context/pricing.md | Deflection line only. Numbers in it must never reach the caller |
| data/enquiries.txt / .pdf | 40 real transcripts (T01–T20 phone, W01–W10 WhatsApp, F01–F10 web form) |
| data/test_set_phone_calls.csv | Expected result for each of the 20 phone calls — the acceptance test |

## Qualification: the five gates (from qualified.md)

A call is **Qualified** only if it passes all five. Score ranks; it never decides.

| # | Criterion | Agent asks | Fails when | If unclear |
|---|---|---|---|---|
| 1 | Real project, in scope | "Are you looking for design and full execution, or ideas and advice?" | Advice/decor only; DIY execution; standalone furniture sourcing or Vastu; restaurant, hotel, retail, gym; structural work; office > ~3,000 sq ft | Ask one direct question |
| 2 | Service area | "Where is the property?" | Outside Pune city + PCMC (Talegaon, Lonavala, Nashik, Mumbai, other cities) | Not on services.md list → ask nearest landmark, then forward with a flag |
| 3 | Realistic timeline | "When would you need the project complete?" | Must be ready in < 6 weeks; site not available for execution within 8–10 weeks of consultation | Ask one direct question |
| 4 | Budget band | NEVER ask | Caller volunteers a number clearly below scope (e.g. ₹1–1.5 lakh for a full flat) | Treat as qualified, note it |
| 5 | Decision-maker | "Are you the owner, or deciding on their behalf?" | Only researching for someone else, no authority | Treat as qualified, note it |

Does NOT disqualify: not knowing what they want, calling after hours, asking for pricing, unsure on style/materials, single room, rented flat with no structural change.

Rule used: **one clear fail = Not qualified** (matches how T03, T04, T07, T10 were handled). OPEN QUESTION for Nikhil — qualified.md also says "two or more fail".

## Routing

| Outcome | Rule | Action |
|---|---|---|
| Qualified P1 | Passes all 5 AND (fixed date driving it: move-in/possession/go-live, OR asks to meet within 2 weeks, OR returning caller we already let down) | Book earliest Calendly slot on the call |
| Qualified P2 | Passes all 5, flexible timeline | Book a Calendly slot on the call |
| Nurture | No fail, but "just exploring", or a timeline fail that could work later (T07) | Polite close; follow-up in 4–6 weeks; in daily digest |
| Not qualified | Any clear fail | Polite close; reason code; in daily digest with explanation |
| Escalate | Existing client, complaint, or anything unanswerable | Promise senior callback; instant alert to studio head (T09) |

Reason codes: `OUT_OF_AREA`, `ADVICE_ONLY`, `OUT_OF_SCOPE_TYPE`, `TOO_SMALL`, `TIMELINE_IMPOSSIBLE`, `BUDGET_MISALIGNED`, `NOT_DECISION_MAKER`, `EXISTING_CLIENT_COMPLAINT`, `NO_INFO`.

Polite decline line (qualified.md): "This sounds like it may not be the right fit for us right now — but feel free to reach out if your timeline or scope changes."

## Interest score /100 (PROPOSED weights — confirm with Nikhil)

Every point must cite a quote from the transcript. No quote = 0 for that line.

| Factor | Full | Partial | Zero |
|---|---|---|---|
| Commitment (30) | Agreed to book on the call | "Let me think" (10) | Declined |
| Readiness (25) | Site available + clear target date | Date vague (10) | "Just exploring" |
| Scope clarity (20) | Rooms + size stated | Rooms only (10) | Vague |
| Decision-maker (15) | Owner/authorised on call | Deciders will attend (8) | Unclear |
| Warm source (10) | Named referral | Found online (5) | Unknown |

## Calendly — booking on the call and what happens after

Verified against Calendly's developer docs (Oct 2026):
- Booking via API = **Scheduling API**: `GET /event_types` → `GET /event_type_available_times` (max 31-day window, UTC) → `POST /invitees` creates the booking. Requires a **paid Calendly plan**.
- Calendly sends the confirmation email and calendar invite itself, based on the event type's settings.
- Webhooks: `invitee.created`, `invitee.canceled`, `invitee_no_show.created`, `invitee_no_show.deleted`. Webhook subscriptions need **Standard plan or higher**.
- `POST /invitees` needs the invitee's **email** → the agent must capture it on the call and read it back letter by letter. If the email cannot be captured, do not guess: mark `booking_pending` and alert the front desk to confirm a slot next working morning.
- Pass our `call_id` in the invitee's tracking fields (e.g. `utm_content`) so the webhook can be matched to the call.

### On the call
1. Agent fetches the next open slots for the consultation event type and offers two.
2. Caller picks one; agent collects name + email (read back), and asks site visit or studio.
3. Agent calls `POST /invitees` → on success reads back day, date, time; on failure (slot taken / API error) offers the next slot once, else `booking_pending` + front desk alert.

### After the slot is scheduled (invitee.created webhook → our backend)
1. **Calendly (automatic):** confirmation email + calendar invite to the caller and the designer; reminders if set on the event type.
2. **Match** the booking to the call via `utm_content = call_id`.
3. **Supabase:** lead status → `booked`; store slot time, designer, event URI, cancel and reschedule URLs.
4. **Report card email (Resend)** to the assigned designer, now including the confirmed slot (template: docs/report_card_template.md).
5. **HubSpot:** create/attach contact, create deal at stage "Consultation booked". Leave amount EMPTY (no pricing).
6. **Dashboard** counters update (bookings, time from call to booking).

### Later Calendly events
- `invitee.canceled` (not a reschedule) → status `cancelled`; designer emailed; front desk alerted to call back next working morning.
- Reschedule (Calendly sends a cancel flagged as rescheduled, then a new `invitee.created`) → update the slot; short "time changed" email to the designer. Verify the exact flag in the live payload.
- `invitee_no_show.created` (designer marks no-show in Calendly) → status `no_show`; front desk follow-up.
- After the consultation the designer marks the HubSpot deal won/lost → dashboard shows conversion.

## Outputs

- Qualified → Calendly booking + report card email to designer + HubSpot deal.
- Not qualified / Nurture → polite close + reason code + **7pm daily digest email** to designers (one row per call, with plain-language explanation). Designers can overturn any rejection.
- Escalate → instant alert to studio head.
- Every call → Supabase log (transcript, fields, decision, score, quotes, cost) → dashboard.

Handoff channel: **email (Resend)**, not Telegram — a report card is long, structured, must be a searchable record per client, and the caller already has a booked slot so designer response speed is not on the critical path.

## Dashboard metrics

Time to answer (target < 5 min, 24x7) · % calls answered · after-hours calls handled · qualified rate · bookings on the call · cancellations / no-shows · rejections by reason code · designer "had to re-ask basics" tick · cost per call (Vaani minutes + LLM tokens + email) · cost per booked consultation · monthly running cost vs bookings.

## Stack (from class)

Vaani Labs (telephony, voice, call orchestration) · Claude (conversation + judgment) · Gemini Flash (cheap classification/routing) · Calendly (booking) · Resend (email) · HubSpot (CRM) · Supabase or Neon (call log) · Vercel (webhooks + dashboard) · GitHub.

Secrets go in `.env` (see `.env.example`). Never commit `.env`.

## Build order (do one step at a time, show results before moving on)

1. System prompt from services.md + qualified.md + pricing deflection line.
2. Qualification logic (field extraction → 5 gates → routing → reason code → score with quotes).
3. **Acceptance test:** run on T01–T20 in data/enquiries.txt and compare with data/test_set_phone_calls.csv. Must match before step 4.
4. Speech guard (price blocker) + tests that try to force a price.
5. Vaani Labs number + agent wired to the prompt.
6. Calendly booking on the call (Scheduling API).
7. Calendly webhook handler → Supabase, report card email, HubSpot.
8. 7pm digest + escalation alert.
9. Dashboard on the call log, including cost.

## Open questions for Nikhil

- One clear fail = reject, or only two or more?
- Minimum office size? (T18 cites 500 sq ft; services.md does not say.)
- Which designer gets which lead — area, project type, or round robin?
- A list of current clients, to catch complaints in the first sentence?
- Approve the interest-score weights?

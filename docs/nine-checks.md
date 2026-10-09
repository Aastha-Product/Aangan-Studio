# Nine Checks — Aangan Studio phone enquiry agent

The gate before building: do we need to build this at all, and have we covered the risks?
Sources: case brief, `context/*.md`, the 40 enquiry transcripts, and the class discussion (guardrails).
North star (from class): **the end goal is revenue, not speed.** Answering in 5 minutes only matters if it turns into more booked consultations and more won projects.

| # | Check | Verdict | Evidence |
|---|---|---|---|
| 1 | **Is the problem real?** | ✅ Yes | ~48% of ~200 enquiries/month get no reply in 48 h (~96 leads/month). An enquiry answered within an hour converts at **4×** the rate of next-day. Average project ₹8–14 lakh. The real problem is **lost revenue** from leads that go cold. |
| 2 | **Is the workflow repeated?** | ✅ Yes | ~200 enquiries a month, every day; ~33% arrive outside 10am–7pm when the 2-person front desk is gone. Same steps every time: understand → qualify → hand to designer. |
| 3 | **Is the input available?** | ✅ Yes, with known gaps | 40 real transcripts (seed data), `services.md`, Nikhil's rubric `qualified.md`, `pricing.md`. The front desk's qualify/reject judgment is reproducible from these: **20/20 phone calls match** the expected outcome in the acceptance test. **Gaps** (questions for Nikhil): is the consultation free; minimum office size; one vs two fails; current client list (to spot complaints); the pricing line in Hindi/Marathi. |
| 4 | **Is the output valuable?** | ✅ Yes | Four outputs (below). Each booked consultation is a lead for an ₹8–14 lakh project, reaching the designer with everything already asked. |
| 5 | **Is the impact measurable?** | ✅ Yes, in rupees | Logged per call: time to answer, % answered, after-hours calls, qualified rate, **booked on the call**, cancellations/no-shows, designer "had to re-ask", rejections by reason, overturned rejections. Revenue: HubSpot deals won (designer enters the quote after the consultation) vs the system's running cost. |
| 6 | **Is the ROI worth it?** | ✅ Yes, by a wide margin | Voice AI costs ~₹5–10/min; a call averages ~4–5 min → roughly ₹25–50 per call, ~₹5,000–10,000/month for 200 calls, plus small LLM/email costs. **One** extra won project (₹8–14 lakh) pays for years of running. The dashboard tracks cost per call and per booked consultation, so this is checked, not assumed. |
| 7 | **Is the failure risk acceptable?** | ✅ With guardrails | See "Failure risks and guardrails" below. The costliest error is a **false negative** (a good lead rejected), so every rejection is reviewable by a human. |
| 8 | **Is judgment protected?** | ✅ Yes | **Price** stays with the designer: the agent never quotes (hard rule + speech guard). **Qualification** is AI-decided, but rejected and Nurture leads go to humans daily (digest + dashboard review queue) and can be overturned. **Complaints** escalate instantly to the studio head. **Budget fit** is a backend rule, not an LLM guess. Thresholds are Nikhil's to confirm. |
| 9 | **Is the owner clear?** | ✅ Yes | **Owner of the output = the designer**: they own the consultation, the price and the conversion (the revenue). Supporting roles: **Aastha** owns the system; **Nikhil** owns the rubric and sees the funnel in HubSpot; the **front desk** owns the review queue, callbacks and overturns. |

## The four outputs

1. **Dashboard**: for the front desk and designers (each enquiry with transcript, recording, extracted details, score, reason, and a review queue for rejected leads), plus an overview for Nikhil (bookings, conversion, revenue vs cost).
2. **HubSpot**: every qualified lead goes into the CRM funnel (deal "Appointment Scheduled" when booked, "Qualified To Buy" when not yet booked), so Nikhil sees his sales funnel. Deal amount is left empty; the designer enters it after quoting.
3. **Notification**: email (Resend) to the designer with the summary, the booked slot and a link to that caller's dashboard page; instant alert to the studio head for complaints; 7pm digest of everything not booked.
4. **Booking on the call**: the agent books the consultation in Cal.com while the caller is on the line, so the next step never depends on someone remembering to call back. Cal.com emails the confirmation and calendar invite.

## Failure risks and guardrails

| Risk | What could go wrong | Guardrail |
|---|---|---|
| **Wrong price** | Caller anchors on a number; lead lost when the real quote differs | Agent never says a price (THE CUT). Speech guard blocks any price before it is spoken; post-call audit flags it if Vaani's own LLM ever runs. Deliberate judgment call, monitored: track how callers who asked for price convert. |
| **Good lead rejected** (false negative, the costlier error) | Revenue silently lost | One clear question when a check is unclear; unclear budget or decision-maker = qualified; every rejection gets a reason code + score; 7pm digest + dashboard review queue; **overturn** by front desk or designer. |
| **Bad lead qualified** (false positive) | Designer time wasted | Lower cost; designer can mark it in HubSpot; tracked as qualified→won rate. |
| **Wrong or missing info to the designer** | Designer re-asks basics, poor first impression | Every score point backed by a verbatim quote (verified against the transcript); report card follows a fixed template; "I had to re-ask" tick feeds the dashboard. |
| **AI/speech quality** | Misheard names, emails, localities | Pune localities as speech keywords; email and phone read back letter by letter; test calls covering rude, edge and grey-area callers before go-live. |
| **Callback bandwidth** | Front desk can't call everyone back | Booking happens on the call; only `booking_pending` cases need a callback. |
| **Our server down** | Silent call | Vaani falls back to its own LLM with the same prompt, which takes a callback instead of booking. |
| **Call recording** | Consent/compliance | If recording is enabled, add "this call may be recorded" to the greeting (Nikhil's call). |

The first build is not the final build: monitor false negatives, overturns and price-askers' conversion, then adjust the rubric with Nikhil.

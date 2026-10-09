# Go-live checklist — Aangan Studio phone agent

Everything is built and tested offline (fake Calendly/Resend/HubSpot/Claude). These steps connect the real services.
Do them in order; each has a check you can run.

## 0. Local setup (done)
- `.venv` with `anthropic` + `websockets` · `.env` created · `APP_SECRET`, `DASHBOARD_TOKEN`, `CRON_SECRET`, `VOICE_SERVER_TOKEN` generated.
- Offline tests: `.venv/Scripts/python -m unittest discover -s tests -t .` → 54 pass.

## 1. Gemini → acceptance test (build step 3 gate)
1. Put `GEMINI_API_KEY` in `.env`.
2. `.venv/Scripts/python scripts/run_acceptance.py` → must show **20/20** before go-live (CLAUDE.md rule).
3. Read `out/acceptance_report.md` for each call's checks, score and quotes.

## 2. Claude → talk to the agent + price red-team (step 4, live half)
1. Put `ANTHROPIC_API_KEY` in `.env`.
2. `.venv/Scripts/python scripts/talk.py` — be a caller. Try T02/T10-style calls.
3. `.venv/Scripts/python scripts/redteam_price.py` — pushy callers in English/Hindi try to get a price.
   Pass = "price reached the caller: 0". "Model attempted" > 0 means the prompt needs tightening (the guard still caught it).
4. Model/latency: default `claude-opus-5-5` at effort `low`. If replies feel slow on the phone, try
   `CLAUDE_MODEL=claude-sonnet-5-5` or `claude-haiku-5-5` and re-run the red-team. Your call (cost vs judgment).

## 3. Supabase (call log)
1. New project → SQL Editor → run `db/schema.sql`.
2. Settings → API → copy URL + **service_role** key into `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`.

## 4. Resend (email)
1. Add and verify your sending domain (DNS records) → API key → `RESEND_API_KEY`, `EMAIL_FROM`.
2. Fill `DESIGNER_EMAILS`, `STUDIO_HEAD_ALERT_EMAIL`, `FRONT_DESK_EMAIL`.

## 5. HubSpot
1. Settings → Integrations → Private apps → scopes: contacts read/write, deals read/write → `HUBSPOT_ACCESS_TOKEN`.
2. Create a deal stage "Consultation booked" → copy its stage id → `HUBSPOT_DEAL_STAGE_CONSULTATION_BOOKED`.
   (Deal **amount is never set** — no pricing anywhere.)

## 6. Vercel (webhooks, 7pm digest, dashboard)
1. Push the repo to GitHub (`.env` is git-ignored) → import in Vercel.
2. Add every `.env` value in Vercel → Environment Variables. Set `PUBLIC_BASE_URL` to the Vercel URL.
3. Deploy. Check: `https://<app>/api/health` → `{"ok": true}`; `https://<app>/dashboard?token=<DASHBOARD_TOKEN>`.
4. The cron in `vercel.json` runs the digest at 13:30 UTC = **7pm IST**.

## 7a. Cal.com — the booking calendar in use (BOOKING_PROVIDER=calcom)
Done: `CALCOM_API_KEY` works (account aastha-pandey-xnw8vb, Asia/Kolkata); webhook secret generated; live slot reading
verified; booking + webhooks tested offline against Cal.com's documented payloads.
1. `python scripts/setup_calcom.py create-event-type --studio-address "<real studio address>" --minutes 60`
   → creates "Aangan Design Consultation" (site visit at the caller's address, or at the studio) and saves CALCOM_EVENT_TYPE_ID.
   (The default 15/30-min types are Cal Video — callers would get a video link. Don't use them.)
2. Cal.com → Availability: set the real consultation hours (default is Mon–Fri 9–5; W01/W03 asked for evenings/Saturdays).
   Connect the designers' Google/Outlook calendars so busy times are blocked.
3. After the web app is on Vercel: `python scripts/setup_calcom.py create-webhook` (needs PUBLIC_BASE_URL).
   Add CALCOM_* values to Vercel. Check the first real BOOKING_CREATED in `call_events` (kind `calcom_webhook`).
4. Leave Vaani's built-in Cal.com integration OFF: with BYOL our server books (linked to the call, report card,
   HubSpot); the built-in one would bypass all of that.

## 7b. Calendly (only if BOOKING_PROVIDER=calendly; paid plan; webhooks need Standard+)
1. Personal access token → `CALENDLY_API_TOKEN`.
2. `python scripts/setup_calendly.py list-event-types` → consultation event → `CALENDLY_EVENT_TYPE_URI`.
3. `python scripts/setup_calendly.py create-webhook` → copy the printed signing key into `.env` **and** Vercel.
4. Test: book once by talking to `scripts/talk.py`; the designer should get the report card after Calendly's webhook.
   **Check the first real webhook payload** (Supabase → `call_events`, kind `calendly_webhook`) — field names for
   reschedules are coded from Calendly's docs and must be confirmed live (CLAUDE.md asks for this).

## 8. Vaani (phone) — api.vaanivoice.ai, verified against docs.vaanivoice.ai (Oct 2026)
Our design: Vaani does telephony + speech; on every caller turn it asks **our** server what to say
(Vaani "Bring Your Own LLM"), so Claude books Calendly live and the price guard checks every sentence.

Done:
- ✅ `VAANI_API_KEY` in `.env` (works: `X-API-Key` header).
- ✅ Agent **"Aangan Studio — Front Desk"** created by `scripts/setup_vaani_agent.py sync` (id in `VAANI_AGENT_ID`):
  our system prompt + fallback addendum, greeting, English + auto language detection. Re-run `sync` after any prompt change.
- ✅ `scripts/setup_vaani_agent.py settings` applied (9 Oct): temperature 0.3, 27 Pune keywords for speech recognition,
  silence-check + hang-up lines, 15-min max call. Backup of the previous config: `out/vaani_agent_backup_*.json`.
  Set in the dashboard (API doesn't save them): pronunciations (BHK→"B H K", PCMC→"P C M C", sq ft→"square feet"),
  first silence check ("Hello?" is fine), STT language (multilingual), voice, untick static prompts, Publish.
- ✅ BYOL bridge `voice_server/server.py` speaks Vaani's documented protocol (tested end to end locally).
- ✅ Webhook handler for `call_started` / `call_ended` / `call_postprocessing`.

To do:
1. **Publish** the agent in the dashboard if it shows a Publish button (the API reports `published: false`).
   You can already try it with the dashboard's **Test** — it uses Vaani's own LLM until step 3.
2. Put `ANTHROPIC_API_KEY` in `.env` and try `scripts/talk.py` first.
3. Host the bridge (Railway / Render / Fly.io; for a quick test: ngrok) with the `.env` values:
   `python -m voice_server.server`. Then Agent → **Brain → Reasoning Language Model → Bring your Own LLM**:
   URL `wss://<host>/byol` · Auth Token = `VOICE_SERVER_TOKEN` · **Test Connection** · **Save URL** ·
   Fallback = **Use platform LLM** (if our server is down, Vaani's LLM takes over with the same prompt; the
   addendum makes it take a callback instead of booking).
4. Deploy the web app (step 6), then **Settings → Webhooks** → `https://<app>/api/vaani/webhook?key=<VAANI_WEBHOOK_SECRET>`.
   (Vaani documents no signature, so the secret lives in the URL — keep the URL private.)
5. **Settings → Telephony → Provision a Number** (India). Then `python scripts/setup_vaani_agent.py inbound +91XXXXXXXXXX`
   (or assign it to the agent in the dashboard). Forward the studio line to it. Call it at 11pm; check the dashboard.

## 9. Cost rates (dashboard)
Fill `COST_VAANI_INR_PER_MIN`, `COST_GEMINI_USD_PER_MTOK_IN/OUT`, `USD_INR_RATE` from your plans. Until then the
dashboard shows a yellow note listing exactly what's missing instead of a wrong number.

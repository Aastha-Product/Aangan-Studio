# Aangan Studio — phone enquiry agent

A voice agent that answers every call to Aangan Studio (interior design, Pune), day or night: it qualifies the caller against Nikhil's rubric, **books a consultation on the call**, never quotes a price, and hands the designer a report card. Every call is logged and shown on a dashboard with its own running cost.

- Plan and rules: [`CLAUDE.md`](CLAUDE.md) · Nine checks: [`docs/nine-checks.md`](docs/nine-checks.md) · Component map: [`docs/component-map.md`](docs/component-map.md)
- Go-live steps: [`docs/SETUP.md`](docs/SETUP.md)

## Stack

Vaani (phone + speech, BYOL) · Claude (conversation) · Gemini Flash (post-call extraction) · Cal.com (booking) · Resend (email) · HubSpot (CRM) · Neon (call log) · Vercel (webhooks, 7pm digest, dashboard)

## Layout

| Path | What |
|---|---|
| `prompts/` | Agent system prompt, Vaani fallback addendum, extraction prompt |
| `qualify/` | Transcript → facts with verbatim quotes → 5 gates → decision, reason code, score |
| `backend/` | Conversation engine + price guard, booking (Cal.com/Calendly), webhooks, emails, HubSpot, digest, dashboard, Neon store |
| `voice_server/` | Vaani BYOL WebSocket bridge (runs on a WebSocket host, not Vercel) |
| `api/index.py`, `vercel.json` | Vercel entry point + 7pm IST cron |
| `scripts/` | Acceptance test, simulators, setup helpers (Vaani, Cal.com, HubSpot, DB migration) |
| `tests/` | Offline tests (no keys needed) |

## Run locally

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt
cp .env.example .env          # fill in keys — never commit .env
.venv/Scripts/python -m unittest discover -s tests -t .
.venv/Scripts/python scripts/run_acceptance.py   # T01–T20 vs the expected outcomes (needs GEMINI_API_KEY)
```

-- Aangan Studio phone agent — call log (Neon / any Postgres).
-- Applied by `python scripts/db_migrate.py` (safe to re-run: everything is "if not exists").

create table if not exists calls (
  call_id            text primary key,           -- Vaani call id (or local id)
  caller_number      text,
  caller_name        text,
  started_at         timestamptz,
  answered_at        timestamptz,
  ended_at           timestamptz,
  duration_sec       integer,
  after_hours        boolean,
  answered           boolean default true,       -- false for failed / missed calls
  transcript         text,
  recording_url      text,                       -- from Vaani call_postprocessing
  summary            text,                       -- Vaani's call summary
  overturned_at      timestamptz,                -- front desk/designer overturned a rejection
  overturned_by      text,

  -- qualification (qualify/rules.py)
  fields             jsonb,                      -- extracted facts + quotes
  decision           text,                       -- Qualified | Nurture | Not qualified | Escalate | No data
  priority           text,                       -- P1 | P2
  priority_reasons   text[],
  reason_code        text,
  all_fail_codes     text[],
  gates              jsonb,
  score              integer,
  score_lines        jsonb,
  flags              text[],
  processing_started_at timestamptz,             -- claim, so a webhook retry cannot process twice
  processed_at       timestamptz,

  -- lifecycle
  status             text,                       -- booked | booking_pending | cancelled | no_show | nurture | not_qualified | escalated | no_data | qualified_not_booked
  follow_up_on       date,                       -- Nurture: 4–6 weeks later
  escalated_at       timestamptz,
  digest_sent_at     timestamptz,

  -- booking (Cal.com or Calendly; invitee_uri holds the booking id, e.g. calcom:<uid>)
  invitee_email      text,
  invitee_name       text,
  visit_type         text,                       -- site_visit | studio
  site_address       text,                       -- for site visits
  booking_provider   text,                       -- calcom | calendly
  slot_start         timestamptz,
  booked_at          timestamptz,
  booked_on_call     boolean,
  designer_email     text,
  designer_name      text,
  event_uri          text,
  invitee_uri        text,
  cancel_url         text,
  reschedule_url     text,
  report_card_sent_at timestamptz,
  had_to_reask       boolean,                    -- designer's tick in the report card

  -- CRM (HubSpot)
  hubspot_contact_id text,
  hubspot_deal_id    text,
  deal_amount        numeric,                    -- entered by the designer in HubSpot after quoting (revenue)
  deal_outcome       text,                       -- open | won | lost

  -- usage for cost (raw quantities; costs computed on the dashboard)
  usage              jsonb,                      -- {claude:{model,input,output,cache_read,cache_write}, gemini:{input,output,thinking}, emails:n}

  created_at         timestamptz default now(),
  updated_at         timestamptz default now()
);

create index if not exists calls_created_at_idx on calls (created_at);
create index if not exists calls_invitee_uri_idx on calls (invitee_uri);
create index if not exists calls_invitee_email_idx on calls (invitee_email);

create table if not exists call_events (
  id         bigserial primary key,
  call_id    text,
  kind       text,          -- speech_guard_block | tool_call | calendly_webhook | vaani_webhook | email_sent | error | ...
  payload    jsonb,
  created_at timestamptz default now()
);
create index if not exists call_events_kind_idx on call_events (kind, created_at);

-- Only the backend (service key) touches these tables.
alter table calls enable row level security;
alter table call_events enable row level security;

-- Columns added after the first version (no-ops on a fresh database).
alter table calls add column if not exists recording_url text;
alter table calls add column if not exists summary text;
alter table calls add column if not exists overturned_at timestamptz;
alter table calls add column if not exists overturned_by text;
alter table calls add column if not exists deal_amount numeric;
alter table calls add column if not exists site_address text;
alter table calls add column if not exists booking_provider text;
alter table calls add column if not exists processing_started_at timestamptz;
alter table calls add column if not exists priority_reasons text[];
alter table calls add column if not exists handled_at timestamptz;   -- follow-up done (dashboard "Mark as done")
alter table calls add column if not exists handled_by text;
alter table calls add column if not exists handled_note text;

"""Settings from the environment (.env locally, Vercel/host env in production)."""
import os
from datetime import datetime, time, timedelta, timezone

from qualify.env import ROOT, load_env

load_env()

IST = timezone(timedelta(hours=5, minutes=30), "IST")   # India has no DST
STUDIO_OPENS, STUDIO_CLOSES = time(10, 0), time(19, 0)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_list(name: str) -> list[str]:
    return [x.strip() for x in env(name).split(",") if x.strip()]


def env_float(name: str, default: float | None = None) -> float | None:
    v = env(name)
    try:
        return float(v) if v else default
    except ValueError:
        return default


def now_ist() -> datetime:
    return datetime.now(IST)


# --- Claude (conversation) -------------------------------------------------------------
CLAUDE_MODEL = env("CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_EFFORT = env("CLAUDE_EFFORT", "low")          # low suits live chat; raise if judgment suffers
# Models that accept server-side refusal fallbacks ("default" form)
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-opus-5"}

# --- Calendly ---------------------------------------------------------------------------
CALENDLY_API = "https://api.calendly.com"
CALENDLY_TOKEN = env("CALENDLY_API_TOKEN")
CALENDLY_EVENT_TYPE_URI = env("CALENDLY_EVENT_TYPE_URI")
CALENDLY_SIGNING_KEY = env("CALENDLY_WEBHOOK_SIGNING_KEY")
SLOT_LOOKAHEAD_DAYS = int(env("SLOT_LOOKAHEAD_DAYS", "14"))   # API max window is 31 days

# --- Cal.com ------------------------------------------------------------------------------
CALCOM_API_KEY = env("CALCOM_API_KEY")
CALCOM_EVENT_TYPE_ID = env("CALCOM_EVENT_TYPE_ID")
CALCOM_WEBHOOK_SECRET = env("CALCOM_WEBHOOK_SECRET")

# Which calendar books consultations: "calcom" or "calendly" (defaults to Cal.com when its key is set)
BOOKING_PROVIDER = env("BOOKING_PROVIDER") or ("calcom" if CALCOM_API_KEY else "calendly")

# --- Email (Resend) -----------------------------------------------------------------------
RESEND_API_KEY = env("RESEND_API_KEY")
EMAIL_FROM = env("EMAIL_FROM", "Aangan Studio Agent <agent@example.com>")
DESIGNER_EMAILS = env_list("DESIGNER_EMAILS")
STUDIO_HEAD_ALERT_EMAIL = env("STUDIO_HEAD_ALERT_EMAIL")
FRONT_DESK_EMAIL = env("FRONT_DESK_EMAIL") or STUDIO_HEAD_ALERT_EMAIL

# --- HubSpot ---------------------------------------------------------------------------------
HUBSPOT_TOKEN = env("HUBSPOT_ACCESS_TOKEN")
HUBSPOT_PIPELINE = env("HUBSPOT_PIPELINE_ID", "default")
HUBSPOT_STAGE_BOOKED = env("HUBSPOT_DEAL_STAGE_CONSULTATION_BOOKED")  # stage id for "Consultation booked"
HUBSPOT_PORTAL_ID = env("HUBSPOT_PORTAL_ID")      # optional; turns deal ids on the dashboard into links
HUBSPOT_UI_DOMAIN = env("HUBSPOT_UI_DOMAIN", "app.hubspot.com")   # e.g. app-na2.hubspot.com for a US (na2) account

# --- Database: Neon/Postgres (DATABASE_URL, injected by Vercel Storage) or Supabase ---
DATABASE_URL = env("DATABASE_URL") or env("POSTGRES_URL")
# --- Supabase ----------------------------------------------------------------------------------
SUPABASE_URL = env("SUPABASE_URL").rstrip("/")
SUPABASE_KEY = env("SUPABASE_SERVICE_KEY")

# --- Vaani -------------------------------------------------------------------------------------
VAANI_API_KEY = env("VAANI_API_KEY")
VAANI_WEBHOOK_SECRET = env("VAANI_WEBHOOK_SECRET")

# --- App -----------------------------------------------------------------------------------------
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL").rstrip("/")     # e.g. https://aangan-agent.vercel.app
APP_SECRET = env("APP_SECRET")                            # signs "re-ask" links
DASHBOARD_TOKEN = env("DASHBOARD_TOKEN")
CRON_SECRET = env("CRON_SECRET")                          # Vercel sends "Authorization: Bearer <CRON_SECRET>"
NURTURE_FOLLOW_UP_DAYS = 35                               # CLAUDE.md: follow up in 4–6 weeks

# --- Cost rates (dashboard). Raw usage is stored per call; costs are computed with these. ----------
# Claude per-MTok USD (Anthropic list prices, Oct 2026): input, output, cache read, cache write (1.25x input)
CLAUDE_PRICES_USD = {
    "claude-opus-5-5":   (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5":  (0.10, 0.50, 0.01, 0.125),
}
GEMINI_USD_PER_MTOK_IN = env_float("COST_GEMINI_USD_PER_MTOK_IN")      # fill from Google's price page
GEMINI_USD_PER_MTOK_OUT = env_float("COST_GEMINI_USD_PER_MTOK_OUT")    # (output incl. thinking tokens)
VAANI_INR_PER_MIN = env_float("COST_VAANI_INR_PER_MIN")                # from your Vaani plan
EMAIL_INR_EACH = env_float("COST_EMAIL_INR_EACH", 0.0)                 # Resend free tier = 0
USD_INR_RATE = env_float("USD_INR_RATE")                               # e.g. today's rate

LOCAL_STORE_PATH = ROOT / "out" / "local_store.json"

"""Vaani (api.vaanivoice.ai) — API client + webhook adapter.

Verified against docs.vaanivoice.ai (Oct 2026):
- REST: base https://api.vaanivoice.ai, header "X-API-Key: vaani_…"
    POST  /api/create-agent                {agent_display_name, config:{persona,…}}  -> {agent_id}
    PATCH /api/agent/{id}/persona          identity.system_prompt, identity.greeting_message, senses_capabilities…
    PATCH /api/agent/{id}/deployment       {deployment:{phone:{call_type:{Inbound:"+91…"}}}}
    GET   /api/list-agents
- Webhooks (Dashboard → Settings → Webhooks), body {"event": …}:
    call_started {room_name, phone_number} · call_ended {room_name, call_duration (SECONDS)}
    call_postprocessing {call_id, data:{call_duration (MILLISECONDS), transcript "[hh:mm:ss] AGENT: …", summary, …}}
  No signature is documented, so the registered URL carries a secret: /api/vaani/webhook?key=<VAANI_WEBHOOK_SECRET>.
- BYOL (live turns) is handled by voice_server/server.py.
"""
import hashlib
import hmac
import re

from . import config
from .http import request_json

API = "https://api.vaanivoice.ai"
_LINE = re.compile(r"^\s*(?:\[\d{1,2}:\d{2}(?::\d{2})?\]\s*)?(agent|assistant|bot|ai|user|customer|caller|human)\s*:\s*(.*)$", re.I)


# --- REST ---------------------------------------------------------------------------------

def _h():
    return {"X-API-Key": config.VAANI_API_KEY}


def list_agents() -> list:
    return request_json("GET", f"{API}/api/list-agents", _h()) or []


def create_agent(name: str, persona: dict) -> str:
    res = request_json("POST", f"{API}/api/create-agent", _h(), {"agent_display_name": name, "config": {"persona": persona}})
    return res["agent_id"]


def update_persona(agent_id: str, persona: dict) -> dict:
    return request_json("PATCH", f"{API}/api/agent/{agent_id}/persona", _h(), persona)


def update_experience(agent_id: str, experience: dict) -> dict:
    return request_json("PATCH", f"{API}/api/agent/{agent_id}/experience", _h(), experience)


def set_inbound_number(agent_id: str, number: str) -> dict:
    return request_json("PATCH", f"{API}/api/agent/{agent_id}/deployment", _h(),
                        {"deployment": {"phone": {"call_type": {"Inbound": number}}}})


# --- web calls (WebRTC): the caller talks to the agent from the browser, no phone number needed ----------
# POST /api/trigger-call/ {medium: "webrtc"} -> {token, room_name, connection_url, live_captions_url}
# (checked live 10 Oct 2026: a LiveKit room token for "Web User", valid 6 hours). The room name is the call id.

def start_web_call(name: str = "Website caller", call_ref: str | None = None) -> dict:
    """`call_ref` goes in as metadata: Vaani fills it into the agent's instructions where they say {call_ref}."""
    body = {"agent_id": config.env("VAANI_AGENT_ID"), "medium": "webrtc", "name": name[:60] or "Website caller",
            "voice_gender": "female", "primary_language": "en", "secondary_language": "hi"}
    if call_ref:
        body["metadata"] = {"call_ref": call_ref}
    return request_json("POST", f"{API}/api/trigger-call/", _h(), body)


def get_transcript(call_id: str) -> str | None:
    """The finished call's transcript ('AGENT: …\\n\\n USER: …'), or None while Vaani is still writing it."""
    from .http import HttpError
    try:
        res = request_json("GET", f"{API}/api/transcript/{call_id}", _h()) or {}
    except HttpError as e:
        if e.status == 404:
            return None
        raise
    t = res.get("transcript") or ""
    return t if t and "not found" not in t.lower()[:80] else None


def get_summary(call_id: str) -> str | None:
    try:
        s = (request_json("GET", f"{API}/api/call_details/{call_id}", _h()) or {}).get("summary")
    except Exception:  # noqa: BLE001 — the summary is a nice-to-have
        return None
    return s if s and len(s) > 20 else None


# --- webhooks -------------------------------------------------------------------------------

def verify(raw_body: bytes, signature_header: str | None, query_key: str | None, secret: str | None = None) -> bool:
    """Secret in the URL (?key=) — Vaani documents no signature. A sha256= HMAC header is also accepted."""
    secret = secret or config.VAANI_WEBHOOK_SECRET
    if not secret:
        return False
    if query_key and hmac.compare_digest(query_key, secret):
        return True
    if signature_header:
        sig = signature_header.split("=", 1)[-1].strip()
        return hmac.compare_digest(hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest(), sig)
    return False


def normalise_transcript(t) -> str:
    """'[13:33:14] AGENT: hi' lines (or a list of {role, content}) -> 'Agent: …' / 'Caller: …' lines."""
    if not t:
        return ""
    if isinstance(t, list):
        out = []
        for turn in t:
            role = str(turn.get("role") or turn.get("speaker") or "").lower()
            if role == "system":
                continue
            text = turn.get("content") or turn.get("text") or ""
            out.append(f"{'Agent' if role in ('agent', 'assistant', 'bot', 'ai') else 'Caller'}: {text}")
        return "\n".join(out)
    out = []
    for line in str(t).splitlines():
        m = _LINE.match(line)
        if m:
            who = "Agent" if m.group(1).lower() in ("agent", "assistant", "bot", "ai") else "Caller"
            out.append(f"{who}: {m.group(2)}")
        elif line.strip() and out:
            out[-1] += " " + line.strip()          # wrapped continuation of the previous turn
    return "\n".join(out)


def parse_event(body: dict) -> dict:
    data = body.get("data") or {}
    event = body.get("event") or body.get("type")
    duration = data.get("call_duration", body.get("call_duration"))
    if isinstance(duration, (int, float)):
        duration = duration / 1000 if event == "call_postprocessing" else duration   # ms vs s (documented)
    return {
        "event": event,
        "call_id": str(body.get("call_id") or data.get("call_id") or body.get("room_name") or data.get("room_name") or ""),
        "caller_number": body.get("phone_number") or data.get("phone_number") or data.get("contact_number"),
        "duration_sec": int(duration) if isinstance(duration, (int, float)) else None,
        "end_reason": body.get("end_reason") or data.get("end_reason"),
        "transcript": normalise_transcript(data.get("transcript") or body.get("transcript")),
        "summary": data.get("summary"),
        "recording_url": data.get("recording_url"),
    }

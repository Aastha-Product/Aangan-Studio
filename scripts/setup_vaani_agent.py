"""Create / update the Aangan agent in Vaani (api.vaanivoice.ai) from this repo — so the prompt never drifts.

    python scripts/setup_vaani_agent.py sync                       # create the agent (first run) or update its prompt + greeting
    python scripts/setup_vaani_agent.py settings                   # speech settings: temperature, keywords, pronunciations, silence lines, max duration
    python scripts/setup_vaani_agent.py inbound +91XXXXXXXXXX      # route inbound calls on a number provisioned in the dashboard
    python scripts/setup_vaani_agent.py list                       # agents on this account

Writes VAANI_AGENT_ID to .env after creating. BYOL (our live server) is switched on in the dashboard:
Agent → Brain → Reasoning Language Model → "Bring your Own LLM" (URL + Auth Token + Fallback = platform LLM).
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, vaani  # noqa: E402
from backend.agent import OPENING, SYSTEM_PROMPT  # noqa: E402

AGENT_NAME = "Aangan Studio — Front Desk"
GREETING = OPENING   # one source of truth: backend/agent.py
ENV_PATH = config.ROOT / ".env"


def persona() -> dict:
    addendum = (config.ROOT / "prompts" / "vaani_fallback_addendum.md").read_text(encoding="utf-8")
    return {
        "identity": {
            "system_prompt": SYSTEM_PROMPT + "\n\n" + addendum,
            "greeting_message": {"agent_message": GREETING, "agent_speech_delay": 1, "interruptible": True,
                                 "let_user_speak_first": False},
        },
        "senses_capabilities": {"language": "en", "auto_detect": True},   # English + Hindi/Marathi callers
    }


SILENCE_CHECK = "Hello, this is Aangan Studio — can you hear me?"
SILENCE_HANGUP = "I can't hear you, so I'll end the call — please call back anytime."
STT_KEYWORDS = ["Aangan", "BHK", "carpet area", "Kothrud", "Baner", "Aundh", "Wakad", "Hinjewadi", "Magarpatta",
                "Kharadi", "Undri", "Kondhwa", "Hadapsar", "Pimple Saudagar", "Pimple Nilakh", "Chinchwad", "Ravet",
                "Pimpri", "Viman Nagar", "Kalyani Nagar", "Koregaon Park", "NIBM", "Warje", "Erandwane", "Deccan",
                "Shivane", "PCMC"]


def voice_settings() -> tuple[dict, dict]:
    """(persona patch, experience patch) — speech settings only; never touches the prompt."""
    persona_patch = {"senses_capabilities": {
        "brain": {"llm": {"primary": {"parameters": {"temperature": 0.3}}}},   # steadier word-for-word lines
        "ears": {"stt": {"primary": {"keywords": STT_KEYWORDS}}},             # Pune localities heard correctly
        "mouth": {"tts": {"word_pronunciation_pairs": {"BHK": "B H K", "PCMC": "P C M C", "sq ft": "square feet"}}},
    }}
    experience_patch = {"settings": {
        "idle_conversation_settings": {"initial_warning_message": SILENCE_CHECK,
                                       "idle_call_warning_message": SILENCE_CHECK,
                                       "idle_call_hangup_message": SILENCE_HANGUP},
        "call_settings": {"max_call_duration": 15, "max_duration_enabled": True},
    }}
    return persona_patch, experience_patch


def save_agent_id(agent_id: str):
    s = ENV_PATH.read_text(encoding="utf-8")
    if re.search(r"^VAANI_AGENT_ID=", s, re.M):
        s = re.sub(r"^VAANI_AGENT_ID=.*$", f"VAANI_AGENT_ID={agent_id}", s, flags=re.M)
    else:
        s = s.rstrip("\n") + f"\nVAANI_AGENT_ID={agent_id}\n"
    ENV_PATH.write_text(s, encoding="utf-8")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not config.VAANI_API_KEY:
        sys.exit("VAANI_API_KEY is empty in .env")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"
    agent_id = config.env("VAANI_AGENT_ID")

    if cmd == "list":
        for a in vaani.list_agents():
            print(a.get("id"), "|", a.get("display_name"), "| status:", a.get("status"), "| published:", a.get("published"))
    elif cmd == "sync":
        if agent_id:
            vaani.update_persona(agent_id, persona())
            print(f"Updated prompt + greeting on agent {agent_id}")
        else:
            agent_id = vaani.create_agent(AGENT_NAME, persona())
            save_agent_id(agent_id)
            print(f"Created agent {agent_id} ('{AGENT_NAME}') and saved VAANI_AGENT_ID to .env")
    elif cmd == "settings":
        if not agent_id:
            sys.exit("Run 'sync' first")
        persona_patch, experience_patch = voice_settings()
        vaani.update_persona(agent_id, persona_patch)
        vaani.update_experience(agent_id, experience_patch)
        print(f"Applied voice settings to agent {agent_id} (temperature, keywords, pronunciations, silence lines, max duration)")
    elif cmd == "inbound":
        if not agent_id or len(sys.argv) < 3:
            sys.exit("usage: setup_vaani_agent.py inbound +91XXXXXXXXXX   (run 'sync' first)")
        vaani.set_inbound_number(agent_id, sys.argv[2])
        print(f"Inbound calls on {sys.argv[2]} now go to agent {agent_id}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()

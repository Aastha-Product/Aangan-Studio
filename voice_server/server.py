"""Vaani BYOL bridge (build step 5) — implements docs.vaanivoice.ai/guides/byol (Oct 2026).

Vaani does telephony, speech-to-text and text-to-speech. On every caller turn it asks THIS server what to say.
We answer with CallSession (Claude + tools + speech guard): Calendly is booked live and no price is ever spoken.

Protocol
- Vaani opens one WebSocket per call at  <your BYOL URL>/<call_id>  with header  Authorization: Bearer <token>
- On accept we must send:   {"interaction_type":"config","content":"Server ready"}
                            {"interaction_type":"greeting","content":"Hello"}
- Per turn Vaani sends:     {"interaction_type":"response_required","response_id":N,"call_id":…,
                             "transcript":[{"role":"system"|"assistant"|"user","content":…}, …],"req_body":{…}|null}
- We reply:                 {"response_type":"response","response_id":N,"content":"…","content_complete":true[,"end_call":true]}
- Keep-alive:               {"response_type":"ping_pong"} (Vaani echoes it)
- Call ends -> Vaani closes the socket -> we qualify + log the call (CallSession.finish).

Needs a host that keeps WebSockets open (Railway, Render, Fly.io; ngrok/cloudflared for a quick test) — not Vercel.
In the Vaani dashboard: Agent → Brain → LLM → "Bring your Own LLM": URL wss://<host>/byol, Auth Token = VOICE_SERVER_TOKEN,
Fallback LLM = "Use platform LLM".

Run:  python -m voice_server.server     (PORT env, default 8765)
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets  # noqa: E402

from backend.agent import CallSession  # noqa: E402

TOKEN = os.environ.get("VOICE_SERVER_TOKEN", "")
PING_EVERY_SEC = 20


def make_session(call_id: str, caller_number: str | None, opening: str | None) -> CallSession:
    """Factory (tests swap it for one with a fake Claude client)."""
    return CallSession(call_id, caller_number, opening=opening)


def new_caller_text(transcript: list[dict]) -> str:
    """Caller words since the agent last spoke. Vaani can repeat a user line; collapse exact repeats."""
    tail = []
    for msg in reversed(transcript or []):
        if msg.get("role") == "assistant":
            break
        if msg.get("role") == "user" and (msg.get("content") or "").strip():
            tail.append(msg["content"].strip())
    tail.reverse()
    deduped = [t for i, t in enumerate(tail) if i == 0 or t != tail[i - 1]]
    return " ".join(deduped)


def first_agent_line(transcript: list[dict]) -> str | None:
    return next((m.get("content") for m in transcript or [] if m.get("role") == "assistant"), None)


def caller_number_from(req_body: dict | None) -> str | None:
    return (req_body or {}).get("contact_number") or None


def reply_frame(response_id: int, text: str, end_call: bool) -> str:
    frame = {"response_type": "response", "response_id": response_id, "content": text, "content_complete": True}
    if end_call:
        frame["end_call"] = True
    return json.dumps(frame)


def _authorised(ws) -> bool:
    req = getattr(ws, "request", None)
    headers = getattr(req, "headers", {}) or {}
    return bool(TOKEN) and headers.get("Authorization", "") == f"Bearer {TOKEN}"


async def _keepalive(ws):
    try:
        while True:
            await asyncio.sleep(PING_EVERY_SEC)
            await ws.send(json.dumps({"response_type": "ping_pong"}))
    except (asyncio.CancelledError, websockets.ConnectionClosed):
        pass


async def handle(ws):
    if not _authorised(ws):
        await ws.close(code=1008, reason="unauthorised")
        return
    path = getattr(getattr(ws, "request", None), "path", "") or ""
    call_id = path.rstrip("/").rsplit("/", 1)[-1].split("?")[0] or "unknown-call"

    await ws.send(json.dumps({"interaction_type": "config", "content": "Server ready"}))
    await ws.send(json.dumps({"interaction_type": "greeting", "content": "Hello"}))

    session: CallSession | None = None
    pinger = asyncio.create_task(_keepalive(ws))
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("interaction_type") != "response_required":
                continue
            transcript = msg.get("transcript") or []
            if session is None:
                session = await asyncio.to_thread(make_session, msg.get("call_id") or call_id,
                                                  caller_number_from(msg.get("req_body")), first_agent_line(transcript))
            text = new_caller_text(transcript)
            if not text:
                continue
            reply = await asyncio.to_thread(session.caller_says, text)
            await ws.send(reply_frame(msg.get("response_id", 0), reply, session.ended))
    except websockets.ConnectionClosed:
        pass
    finally:
        pinger.cancel()
        if session is not None:
            await asyncio.to_thread(session.finish)   # qualify + log + report card / alerts


async def main():
    port = int(os.environ.get("PORT", "8765"))
    if not TOKEN:
        sys.exit("Set VOICE_SERVER_TOKEN (and paste the same value into Vaani's BYOL Auth Token field).")
    async with websockets.serve(handle, "0.0.0.0", port):
        print(f"Vaani BYOL bridge listening on ws://0.0.0.0:{port}  (Vaani appends /<call_id> to the URL)")
        await asyncio.Future()


if __name__ == "__main__":
    from backend import config  # noqa: F401  (loads .env)
    TOKEN = os.environ.get("VOICE_SERVER_TOKEN", "")
    asyncio.run(main())

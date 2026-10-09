"""Minimal Gemini REST client (stdlib only) returning structured JSON + token usage."""
import json
import os
import time
import urllib.error
import urllib.request

from .env import load_env

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3.8-flash"  # 2.5-flash is closed to new accounts (Oct 2026)


class GeminiError(RuntimeError):
    pass


def generate_json(prompt: str, schema: dict, model: str | None = None, retries: int = 3) -> tuple[dict, dict]:
    load_env()
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise GeminiError("GEMINI_API_KEY is empty. Add it to .env")
    model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL

    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        API.format(model=model), data=body, method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
    )

    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if e.code in (429, 500, 503) and attempt < retries - 1:
                time.sleep(2 ** attempt * 5)
                continue
            raise GeminiError(f"Gemini HTTP {e.code}: {detail}") from None

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise GeminiError(f"Unexpected Gemini response: {json.dumps(data)[:500]}") from None

    u = data.get("usageMetadata", {})
    usage = {
        "model": model,
        "input_tokens": u.get("promptTokenCount", 0),
        "output_tokens": u.get("candidatesTokenCount", 0),
        "thinking_tokens": u.get("thoughtsTokenCount", 0),
    }
    return json.loads(text), usage

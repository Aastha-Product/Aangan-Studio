"""Send email through Resend (POST https://api.resend.com/emails)."""
from . import config
from .http import request_json

RESEND_URL = "https://api.resend.com/emails"


def send_email(to: list[str] | str, subject: str, text: str, html: str | None = None,
               reply_to: str | None = None) -> dict:
    to = [to] if isinstance(to, str) else [t for t in to if t]
    if not to:
        raise ValueError(f"No recipients for email: {subject}")
    body = {"from": config.EMAIL_FROM, "to": to, "subject": subject, "text": text}
    if html:
        body["html"] = html
    if reply_to:
        body["reply_to"] = reply_to
    return request_json("POST", RESEND_URL, {"Authorization": f"Bearer {config.RESEND_API_KEY}"}, body)

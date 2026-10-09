"""Parse the phone-call transcripts (T01–T20) out of data/enquiries.txt."""
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

from .env import ROOT

HEADER = re.compile(r"^(T\d{2}) · Phone · (\d{1,2}) (\w+) · (.*)$")
TIME = re.compile(r"(\d{1,2}):(\d{2})\s*(am|pm)", re.I)

STUDIO_OPENS = time(10, 0)
STUDIO_CLOSES = time(19, 0)


@dataclass
class Call:
    call_id: str
    call_date: date
    call_time: time | None
    header: str
    text: str  # header + body, exactly as in the source file

    @property
    def after_hours(self) -> bool:
        if self.call_time is None:
            return False
        return self.call_time < STUDIO_OPENS or self.call_time >= STUDIO_CLOSES


def _parse_time(s: str) -> time | None:
    m = TIME.search(s)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    return time(hour, minute)


def load_phone_calls(path: Path = ROOT / "data" / "enquiries.txt", year: int = 2026) -> dict[str, Call]:
    lines = path.read_text(encoding="utf-8").splitlines()
    calls: dict[str, Call] = {}
    current: list[str] | None = None
    meta = None

    def flush():
        if current is not None and meta is not None:
            call_id, call_date, call_time, header = meta
            calls[call_id] = Call(call_id, call_date, call_time, header, "\n".join(current).strip())

    for line in lines:
        m = HEADER.match(line.strip())
        if m:
            flush()
            call_id, day, month, rest = m.groups()
            call_date = datetime.strptime(f"{day} {month} {year}", "%d %B %Y").date()
            meta = (call_id, call_date, _parse_time(rest), line.strip())
            current = [line.strip()]
        elif line.startswith("SECTION 2"):
            flush()
            current, meta = None, None
            break
        elif current is not None:
            current.append(line)
    flush()
    return calls

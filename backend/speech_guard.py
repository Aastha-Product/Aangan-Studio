"""Speech guard (build step 4) — THE CUT.

Every agent utterance passes through guard() before it is spoken. If it contains a currency
amount, "lakh"/"crore", "₹", a per-sq-ft figure, or a money word next to a number, the WHOLE
utterance is blocked and replaced with the approved deflection line (CLAUDE.md). Every block is
returned with reasons so the caller can log it.

Deliberately strict: a false positive costs one replaced sentence; a false negative quotes a price.
Things that must still pass: timelines ("8 to 16 weeks"), slots ("Thursday 9 October, 11 am"),
sizes ("about 3,000 square feet"), localities ("Pimple Nilakh").
"""
import re
from dataclasses import dataclass, field

DEFLECTION_LINE = (
    "Pricing depends on the site, the materials you choose, and the scope — your designer will walk you "
    "through it in detail at the consultation. I can book that for you right now if you'd like."
)

_NUM_WORDS = (r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
              r"sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
              r"hundred|thousand|million|half|ek|paanch|bees|pachaas|sau|hazaar|hazar")
_NUMBER = re.compile(rf"\d|\b({_NUM_WORDS})\b", re.I)

_ALWAYS_BLOCK = [
    ("currency symbol", re.compile(r"[₹$€£]")),
    ("rupee word", re.compile(r"\b(rs\.?|inr|rupees?|rupaye|rupaiye)(?=[\s\d.,]|$)|रुपये|रुपए|रुपया|रुपयांचे", re.I)),
    ("lakh/crore", re.compile(r"\b(lakhs?|lacs?|crores?|karod)\b|लाख|करोड़|करोड", re.I)),
    ("per area rate", re.compile(
        r"\b(per|a|an|each|every|prati)\s+(sq\.?\s*(ft|feet|foot|m|mtr|metre|meter)|square\s+(feet|foot|metres?|meters?))\b"
        r"|/\s*sq\.?\s*ft|\bpsf\b|\bper\s*sq\b", re.I)),
    ("k amount", re.compile(r"\b\d+(\.\d+)?\s?k\b", re.I)),
]

_MONEY_WORD = re.compile(
    r"\b(cost|costs|costing|price|prices|priced|pricing|rates?|charges?|charged|fees?|budget|ballpark|"
    r"quote|quotation|quoted|expensive|cheap|cheaper|affordable|amount|discount|paisa|paise|kharcha|daam|keemat)\b",
    re.I)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?।])\s+")


@dataclass
class GuardResult:
    text: str                      # what may be spoken
    blocked: bool
    original: str
    reasons: list[str] = field(default_factory=list)


def find_price_reasons(text: str) -> list[str]:
    if not text:
        return []
    # The approved line itself is always allowed, even inside a longer utterance.
    scrubbed = text.replace(DEFLECTION_LINE, " ")
    reasons = [name for name, pattern in _ALWAYS_BLOCK if pattern.search(scrubbed)]
    for sentence in _SENTENCE_SPLIT.split(scrubbed):
        if _MONEY_WORD.search(sentence) and _NUMBER.search(sentence):
            reasons.append(f"money word + number: {sentence.strip()[:80]}")
    return reasons


def guard(text: str) -> GuardResult:
    reasons = find_price_reasons(text)
    if reasons:
        return GuardResult(DEFLECTION_LINE, True, text, reasons)
    return GuardResult(text, False, text)

"""
Safety gate for autopilot actions — pure functions, no Qt, no I/O.

Final level = the stricter of
  (a) the model's own "safety" label, and
  (b) deterministic checks:
      • the target label or the text to type matches a confirm keyword
        (send / delete / pay / confirm …, editable in config)  -> "confirm"
      • typing into a password / card / code field              -> "private"

  "safe"     execute immediately
  "confirm"  ask a short Georgian yes/no question; act only on „დიახ“
  "private"  never type it; the person types it themselves while the
             assistant does not look (no screenshot, nothing logged)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

SAFE, CONFIRM, PRIVATE = "safe", "confirm", "private"
_RANK = {SAFE: 0, CONFIRM: 1, PRIVATE: 2}

DEFAULT_CONFIRM_KEYWORDS = (
    "გაგზავნა", "წაშლა", "გადახდა", "დადასტურება", "შეკვეთა",
    "Send", "Delete", "Pay", "Confirm", "Submit", "Buy", "Remove",
)

# Labels of fields whose content is private. Only applied to *typing*:
# clicking into a password box is fine, typing into it is not.
PRIVATE_FIELD_KEYWORDS = (
    "password", "passcode", "pin", "cvv", "cvc", "card number", "otp",
    "verification code", "security code", "sms code", "one-time",
    "პაროლ", "ბარათ", "კოდ", "პინ",
)

_GEORGIAN = re.compile(r"[Ⴀ-ჿᲐ-Ჿ]")


@dataclass
class SafetyDecision:
    level: str
    reasons: List[str] = field(default_factory=list)   # never contains typed text


def normalize_level(value) -> str:
    """The model's label; anything unrecognised counts as "confirm"."""
    v = str(value or "").strip().lower()
    return v if v in _RANK else CONFIRM


def stricter(a: str, b: str) -> str:
    return a if _RANK[a] >= _RANK[b] else b


def _stem(keyword: str) -> str:
    # Georgian verbs/nouns inflect at the end (გაგზავნა / გაგზავნე / გაგზავნეთ),
    # so match on the stem; Latin keywords match as word prefixes (Pay → Payment).
    kw = keyword.strip().casefold()
    return kw[:-1] if _GEORGIAN.search(kw) and len(kw) >= 5 else kw


def matches_keyword(text: str, keywords: Iterable[str]) -> Optional[str]:
    """The first keyword found at the start of a word in `text`, else None.
    Word-start matching keeps "Pay" out of "display"."""
    t = (text or "").casefold()
    if not t:
        return None
    for kw in keywords:
        stem = _stem(kw)
        if stem and re.search(r"(?<!\w)" + re.escape(stem), t):
            return kw
    return None


def classify(model_level, action: dict, target_label: str = "",
             target_is_private_field: bool = False,
             keywords: Iterable[str] = DEFAULT_CONFIRM_KEYWORDS) -> SafetyDecision:
    """Final safety level for one action."""
    level = normalize_level(model_level)
    reasons = [f"model said {level}"]
    keywords = tuple(keywords)
    action = action or {}
    kind = str(action.get("type", "")).lower()

    hit = matches_keyword(target_label, keywords)
    if hit:
        level = stricter(level, CONFIRM)
        reasons.append(f"target matches confirm keyword {hit!r}")
    if kind == "type":
        hit = matches_keyword(str(action.get("text", "")), keywords)
        if hit:
            level = stricter(level, CONFIRM)
            reasons.append(f"typed text matches confirm keyword {hit!r}")
        field_hit = matches_keyword(target_label, PRIVATE_FIELD_KEYWORDS)
        if target_is_private_field or field_hit:
            level = PRIVATE
            reasons.append("typing into a password/card/code field"
                           + (f" ({field_hit!r})" if field_hit else ""))
    return SafetyDecision(level=level, reasons=reasons)

"""
„რომელია ხრომი?“ / „სად არის ვორდი?“ — which app the person means, among
the icons that are on the screen right now, and a short Georgian line that
explains it. One text-only model call (no screenshot: the icon names come
from UIA, agent/show_icon.py), through the app's active provider.

    is_which_question(text) -> bool             cheap gate, no model call
    await pick_app(text, icons, llm, model) -> Pick | None

The reply must be JSON: {"asked_app": bool, "index": int|null, "explain": str}.
asked_app=false means "not about an app" — the normal flow answers instead.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import List, Optional

log = logging.getLogger("clicky.which_app")

# Only questions with one of these go to the model at all.
_CUE_RE = re.compile(
    r"რომელია|სად არის|სადაა|სად მაქვს|სად არი\b|ვერ ვპოულობ|ვერ ვიპოვე|"
    r"რომელი აპლიკაცია|რომელი პროგრამა|რა აპლიკაცია|რა პროგრამა|"
    r"where is|which (?:one )?is",
    re.IGNORECASE,
)

SYSTEM_PROMPT = """You help elderly Georgian computer users find an app's icon on their screen.
You get what the person said and a numbered list of the icons that are on the
screen right now (desktop and taskbar).

Reply with ONE JSON object and nothing else — no markdown, no text around it:
{"asked_app": <true|false>, "index": <a number from the list, or null>, "explain": "<Georgian>"}

- asked_app: true ONLY when the person asks which / where an app, program,
  website shortcut or icon is („სად არის ვორდი?“, „რომელია ხრომი?“,
  „ინტერნეტი რომელია?“, „წერილები სად მაქვს?“). Anything else (weather, time,
  places, how-to questions) is false, with index null and explain "".
- index: the icon they mean, also when they describe it instead of naming it
  („ინტერნეტი“ = a web browser, „წერილები“ = mail, „ვორდი“ = Microsoft Word,
  „ნაგავი“ = Recycle Bin). Prefer an app over a document with a similar name.
  null when it is not in the list.
- explain: one or two short, warm sentences in simple Georgian, polite form,
  as if pointing at it: say which app this is (its name as written) and what
  it is used for, e.g. „აი, ეს არის Google Chrome — ინტერნეტის
  დასათვალიერებელი პროგრამა.“ When index is null, kindly say it is not on
  the screen now."""

SAY_NOT_ON_SCREEN = "სამწუხაროდ, ეს პროგრამა ეკრანზე ვერ ვიპოვე."


def is_which_question(text: str) -> bool:
    return bool(_CUE_RE.search(text or ""))


# Code-only fallback when the model can't be reached: how people say an app
# (Georgian stems, as Google STT writes them) -> a word in its icon's name.
ALIASES = {
    "chrome": ("ხრომ", "ქრომ", "გუგლ", "chrom", "google"),
    "edge": ("ედჯ", "ეჯ", "edge"),
    "word": ("ვორდ", "word"),
    "excel": ("ექსელ", "excel"),
    "powerpoint": ("პაუერპოინტ", "პოვერპოინტ", "powerpoint"),
    "telegram": ("ტელეგრამ", "telegram"),
    "whatsapp": ("ვოთსაპ", "ვატსაპ", "უოთსაპ", "whatsapp"),
    "viber": ("ვაიბერ", "viber"),
    "skype": ("სკაიპ", "skype"),
    "zoom": ("ზუმ", "zoom"),
    "discord": ("დისკორდ", "discord"),
    "steam": ("სტიმ", "steam"),
    "roblox": ("რობლოქს", "რობლოკს", "roblox"),
    "anydesk": ("ენიდესკ", "anydesk"),
    "rustdesk": ("რასტდესკ", "rustdesk"),
    "acrobat": ("აკრობატ", "acrobat", "pdf"),
    "stremio": ("სტრემიო", "stremio"),
    "claude": ("კლოდ", "claude"),
    "file explorer": ("ფაილებ", "საქაღალდ", "explorer"),
    "recycle bin": ("ნაგავ", "სანაგვე", "recycle"),
    "this pc": ("ჩემი კომპიუტერ", "this pc"),
}
SAY_THIS_IS = "აი, ეს არის {name}."


def local_pick(text: str, icons) -> Optional[Pick]:
    """Match the request to an icon without a model: a known alias, or the
    icon's own name written in the request. Apps beat documents („Word“ over
    „New Microsoft Word Document“). None when nothing matches."""
    said = (text or "").lower()
    keys = [k for k, aliases in ALIASES.items() if any(a in said for a in aliases)]
    best, best_rank = None, 9
    for n, icon in enumerate(icons):
        name = icon.name.lower()
        for key in keys:
            if key not in name:
                continue
            rank = 0 if (name == key or name.startswith(key) or name.endswith(" " + key)) else \
                1 if "document" not in name and "worksheet" not in name and "presentation" not in name else 2
            if rank < best_rank:
                best, best_rank = n, rank
        if best_rank > 0 and len(name) >= 4 and name in said:
            best, best_rank = n, 0
    if best is None:
        return None
    return Pick(True, best, SAY_THIS_IS.format(name=icons[best].name))


@dataclass
class Pick:
    asked_app: bool
    index: Optional[int]      # 0-based into the icons list
    explain: str


def icon_list_text(icons) -> str:
    where_ka = {"taskbar": "ქვედა ზოლზე", "desktop": "სამუშაო მაგიდაზე"}
    return "\n".join(f"{n}. {i.name} ({where_ka.get(i.where, i.where)})"
                     for n, i in enumerate(icons, start=1))


def parse_pick(text: str, count: int) -> Optional[Pick]:
    """The model's reply -> Pick; None when it is unusable."""
    from agent.planner import PlanParseError, extract_json_object
    try:
        data = extract_json_object(text)
    except PlanParseError:
        return None
    asked = data.get("asked_app")
    if not isinstance(asked, bool):
        return None
    explain = data.get("explain")
    explain = explain.strip() if isinstance(explain, str) else ""
    raw = data.get("index")
    index = None
    if isinstance(raw, int) and not isinstance(raw, bool) and 1 <= raw <= count:
        index = raw - 1
    return Pick(asked, index if asked else None, explain if asked else "")


async def pick_app(text: str, icons, llm, model: Optional[str] = None) -> Optional[Pick]:
    """Ask the model which icon `text` means. None on any failure (the
    caller then lets the normal flow answer)."""
    from timing import mark, since_ms
    if not icons:
        return None
    user = f"The person said: {text.strip()}\n\nIcons on the screen:\n{icon_list_text(icons)}"
    t0, first, parts = time.perf_counter(), None, []
    try:
        async for chunk in llm.stream_response(user_text=user, screenshots_b64=[], history=[],
                                               system_prompt=SYSTEM_PROMPT, model=model):
            if first is None:
                first = since_ms(t0)
            parts.append(chunk)
    except Exception as e:
        log.warning("which-app call failed: %s", e)
        return None
    finally:
        mark("which_app.model_call", since_ms(t0),
             first_token_ms=round(first) if first is not None else None, icons=len(icons))
    pick = parse_pick("".join(parts), len(icons))
    if pick is None:
        log.warning("which-app reply unusable: %r", "".join(parts)[:200])
    return pick

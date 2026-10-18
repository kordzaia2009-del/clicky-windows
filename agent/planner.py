"""
Task planner — decides whether a request is a task to carry out on the
computer and, if so, breaks it into goals and high-level steps.

One model call through the app's existing provider (BaseLLMProvider, picked
the same way as CompanionManager._get_llm()). The reply must be strict JSON:

    {"is_task": bool,
     "goals": [{"goal": str,
                "steps": [{"id": int, "title": str, "done_when": str,
                           "private": bool}]}]}

Parsing is defensive: markdown fences and surrounding prose are stripped,
the structure and rules are validated, a bad reply gets one retry that
quotes the error, and anything still unusable falls back to is_task=false
(with `error` set so the caller can log why).

CLI:
    python -m agent.planner "გახსენი მეილი და პირველი წერილი"
    python -m agent.planner --screenshot "..."   # also sends the screen
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, List, Optional

from ai.base_provider import BaseLLMProvider, Message

if TYPE_CHECKING:   # screen.capture pulls in mss/PIL; only needed for typing
    from screen.capture import ScreenShot

log = logging.getLogger("clicky.planner")

MIN_STEPS, MAX_STEPS = 3, 6

_GEORGIAN_RE = re.compile(r"[Ⴀ-ჿᲐ-Ჿⴀ-⴯]")
# Steps describe what to do, never where on screen: no "(840, 610)",
# "1280x720", "px", "pixel", "კოორდინატი" ...
_COORD_RE = re.compile(
    r"\(\s*\d+\s*,\s*\d+\s*\)|\b\d{2,5}\s*[x×,]\s*\d{2,5}\b|\bpx\b|pixel|"
    r"coordinat|პიქსელ|კოორდინატ",
    re.IGNORECASE,
)


@dataclass
class Step:
    id: int
    title: str
    done_when: str
    private: bool = False


@dataclass
class Goal:
    goal: str
    steps: List[Step] = field(default_factory=list)


@dataclass
class PlanResult:
    is_task: bool
    goals: List[Goal] = field(default_factory=list)
    error: Optional[str] = None     # why we fell back; None for a real plan

    def to_dict(self) -> dict:
        return asdict(self)


class PlanParseError(ValueError):
    """The model's reply was not a valid plan."""


SYSTEM_PROMPT = """You are the task planner for Klipitauri, a voice assistant on a Windows
computer. The people using it are elderly Georgian speakers who are often
new to computers. You never answer the user; you only produce a plan as JSON.

Reply with ONE JSON object and nothing else — no markdown, no code fences,
no comments, no text before or after it:

{"is_task": <true|false>,
 "goals": [{"goal": "<string>",
            "steps": [{"id": <int>, "title": "<string>",
                       "done_when": "<string>", "private": <true|false>}]}]}

RULES
1. is_task is true ONLY when the user asks the assistant to DO something on
   the computer: open an app or website, go to a page, search and open
   something, log in, write or send something, play, download, etc.
   It is false for questions, explanations, "how do I…", greetings, and
   anything that only needs an answer. When false, "goals" is [].
2. One goal per distinct thing the user asked for, in the order they said
   it. "Open my mail and the first letter" is two goals.
3. Each goal has 3 to 6 high-level steps, numbered from 1 within the goal.
   A step says WHAT the person does, in plain words — never coordinates,
   pixel positions, screen measurements or numbers describing a location.
4. "goal", "title" and "done_when" are written in Georgian, in simple
   everyday words, using the polite form (გახსენით, დააჭირეთ). Product and
   site names stay as they are written (Gmail, Google Chrome, YouTube).
   - goal: a short name for the goal (at most 5 words).
   - title: one short instruction (at most 6 words).
   - done_when: what the person will see when the step is finished
     (at most 10 words), e.g. „ეკრანზე ჩანს წერილების სია“.
5. private is true ONLY for a step where the person must type a password,
   bank card details, or a one-time / SMS code. Typing an email address,
   a name or search text is NOT private.
6. If a screenshot is attached, use it only to choose sensible steps (for
   example, skip opening the browser if it is already open). Never refer
   to positions on it.

EXAMPLES
User: ჩართე იუთუბი და მოძებნე ქართული სიმღერები
{"is_task": true, "goals": [
 {"goal": "YouTube-ის გახსნა", "steps": [
  {"id": 1, "title": "გახსენით ბრაუზერი", "done_when": "ბრაუზერის ფანჯარა გახსნილია", "private": false},
  {"id": 2, "title": "გადადით YouTube-ის საიტზე", "done_when": "ეკრანზე ჩანს YouTube-ის გვერდი", "private": false},
  {"id": 3, "title": "დაელოდეთ გვერდის ჩატვირთვას", "done_when": "გვერდი სრულად ჩაიტვირთა", "private": false}]},
 {"goal": "ქართული სიმღერების მოძებნა", "steps": [
  {"id": 1, "title": "დააჭირეთ ძებნის ველს", "done_when": "ძებნის ველში ციმციმებს კურსორი", "private": false},
  {"id": 2, "title": "ჩაწერეთ: ქართული სიმღერები", "done_when": "ტექსტი ჩაწერილია ძებნის ველში", "private": false},
  {"id": 3, "title": "დააჭირეთ ძებნის ღილაკს", "done_when": "ეკრანზე ჩანს სიმღერების სია", "private": false}]}]}

User: რა ამინდია დღეს?
{"is_task": false, "goals": []}"""

_RETRY_PROMPT = (
    "Your previous reply could not be used: {error}. Reply again with ONLY "
    "the JSON object, following every rule."
)


# ── Parsing ──────────────────────────────────────────────────────────────────

def strip_fences(text: str) -> str:
    """Remove ```json … ``` (or bare ```) fences around a reply."""
    t = (text or "").strip()
    m = re.match(r"^```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?```\s*$", t, re.DOTALL)
    return m.group(1).strip() if m else t


def extract_json_object(text: str) -> dict:
    """The first JSON object in `text`, tolerating fences and prose around it."""
    t = strip_fences(text)
    start = t.find("{")
    if start < 0:
        raise PlanParseError("no JSON object in the reply")
    try:
        obj, _ = json.JSONDecoder().raw_decode(t[start:])
    except json.JSONDecodeError as e:
        raise PlanParseError(f"invalid JSON ({e.msg} at char {e.pos})") from e
    if not isinstance(obj, dict):
        raise PlanParseError("the reply is not a JSON object")
    return obj


def _text(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanParseError(f"{where} must be a non-empty string")
    value = value.strip()
    if not _GEORGIAN_RE.search(value):
        raise PlanParseError(f"{where} must be written in Georgian: {value!r}")
    if _COORD_RE.search(value):
        raise PlanParseError(f"{where} must not contain coordinates or pixel positions: {value!r}")
    return value


def plan_from_dict(data: dict) -> PlanResult:
    """Validate a decoded reply and build the dataclasses. Raises PlanParseError."""
    is_task = data.get("is_task")
    if not isinstance(is_task, bool):
        raise PlanParseError('"is_task" must be true or false')
    if not is_task:
        return PlanResult(is_task=False, goals=[])

    raw_goals = data.get("goals")
    if not isinstance(raw_goals, list) or not raw_goals:
        raise PlanParseError('"goals" must be a non-empty list when is_task is true')

    goals: List[Goal] = []
    for gi, g in enumerate(raw_goals, start=1):
        if not isinstance(g, dict):
            raise PlanParseError(f"goal {gi} must be an object")
        name = _text(g.get("goal"), f"goal {gi} name")
        raw_steps = g.get("steps")
        if not isinstance(raw_steps, list):
            raise PlanParseError(f'goal {gi} "steps" must be a list')
        if not MIN_STEPS <= len(raw_steps) <= MAX_STEPS:
            raise PlanParseError(
                f"goal {gi} has {len(raw_steps)} steps; it needs {MIN_STEPS}-{MAX_STEPS}"
            )
        steps: List[Step] = []
        seen_ids = set()
        for si, s in enumerate(raw_steps, start=1):
            where = f"goal {gi} step {si}"
            if not isinstance(s, dict):
                raise PlanParseError(f"{where} must be an object")
            sid = s.get("id")
            # bool is an int subclass — True must not pass as id 1
            if not isinstance(sid, int) or isinstance(sid, bool) or sid < 1:
                raise PlanParseError(f"{where} id must be a positive integer")
            if sid in seen_ids:
                raise PlanParseError(f"{where} repeats id {sid}")
            seen_ids.add(sid)
            private = s.get("private", False)
            if not isinstance(private, bool):
                raise PlanParseError(f"{where} private must be true or false")
            steps.append(Step(
                id=sid,
                title=_text(s.get("title"), f"{where} title"),
                done_when=_text(s.get("done_when"), f"{where} done_when"),
                private=private,
            ))
        goals.append(Goal(goal=name, steps=steps))
    return PlanResult(is_task=True, goals=goals)


def parse_plan(text: str) -> PlanResult:
    """Model reply text → PlanResult. Raises PlanParseError on anything invalid."""
    return plan_from_dict(extract_json_object(text))


# ── Model call ───────────────────────────────────────────────────────────────

async def _collect(llm: BaseLLMProvider, user_text: str, images: List[str],
                   history: List[Message], model: Optional[str]) -> str:
    import time
    from timing import mark, since_ms
    t0, first = time.perf_counter(), None
    parts = []
    async for chunk in llm.stream_response(
        user_text=user_text,
        screenshots_b64=images,
        history=history,
        system_prompt=SYSTEM_PROMPT,
        model=model,
    ):
        if first is None:
            first = since_ms(t0)
        parts.append(chunk)
    mark("planner.model_call", since_ms(t0),
         first_token_ms=round(first) if first is not None else None,
         images=len(images), retry=bool(history))
    return "".join(parts)


async def plan(user_text: str, screenshot: "ScreenShot | None" = None, *,
               llm: Optional[BaseLLMProvider] = None,
               model: Optional[str] = None) -> PlanResult:
    """Plan `user_text`. Never raises: failures come back as is_task=False
    with `error` set. `llm` / `model` default to the app's active provider
    and that provider's default model."""
    user_text = (user_text or "").strip()
    if not user_text:
        return PlanResult(is_task=False, error="empty request")
    if llm is None:
        from ai.provider_factory import create_llm_provider
        llm = create_llm_provider()
    images = [screenshot.base64_jpeg] if screenshot is not None else []

    history: List[Message] = []
    prompt = user_text
    last_error = "no reply"
    for attempt in (1, 2):
        try:
            raw = await _collect(llm, prompt, images, history, model)
        except Exception as e:
            log.warning("planner call failed: %s", e)
            return PlanResult(is_task=False, error=f"model call failed: {e}")
        try:
            result = parse_plan(raw)
            log.info("plan for %r: is_task=%s, %d goal(s) (attempt %d)",
                     user_text[:60], result.is_task, len(result.goals), attempt)
            return result
        except PlanParseError as e:
            last_error = str(e)
            log.warning("planner reply rejected (attempt %d): %s | %r",
                        attempt, last_error, raw[:300])
            # Retry once as a follow-up turn, so the model sees its own reply
            # and the exact problem. The screenshot is not re-sent.
            history = [Message(role="user", content=user_text),
                       Message(role="assistant", content=raw)]
            prompt = _RETRY_PROMPT.format(error=last_error)
            images = []
    return PlanResult(is_task=False, error=f"invalid plan after retry: {last_error}")


# ── CLI ──────────────────────────────────────────────────────────────────────

def _main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent.planner",
        description="Plan a request with the app's active LLM provider and print the JSON.",
    )
    parser.add_argument("text", nargs="+", help="the request, e.g. \"გახსენი მეილი\"")
    parser.add_argument("--screenshot", action="store_true",
                        help="also send a screenshot of the primary screen")
    parser.add_argument("--model", default=None, help="model id (default: provider default)")
    parser.add_argument("--debug", action="store_true", help="log the raw replies")
    args = parser.parse_args(argv)

    # Georgian output on a Windows console
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    logging.basicConfig(level=logging.INFO if args.debug else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    shot = None
    if args.screenshot:
        from screen.capture import capture_primary
        shot = capture_primary()

    result = asyncio.run(plan(" ".join(args.text), shot, model=args.model))
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.error is None else 1


if __name__ == "__main__":
    sys.exit(_main())

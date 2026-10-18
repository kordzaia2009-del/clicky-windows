"""
Autopilot — carries out a planned task on the computer, step by step.

An explicit state machine (every transition is logged):

    idle -> planning -> acting <-> verifying -> ... -> done
                          |-> awaiting_confirm (safety "confirm")
                          |-> awaiting_private (private step / field)
                          +-> replanning (3 failed attempts on a step)
    any state -> cancelled (Esc / "stop" / cancel_event)

Per step:
  1. private step: no screenshot is taken or sent; the person is asked to
     type it themselves; continue on „მზად ვარ“.
  2. otherwise: screenshot + one model call -> strict JSON decision
     {step_done, action, say_ka, safety, stuck}.
  3. not done: locate the action's target (UIA -> OCR -> Computer Use ->
     grid, supplied by the caller), apply the safety gate, say say_ka
     (non-blocking), execute.
  4. wait for the screen to settle; an action that changed nothing is a
     failed attempt; 3 failures -> replan the remaining steps from the
     current screen. The next model call verifies done_when.
  5. next step, no prompt to the user.
Limits: 15 actions per goal; several goals run one after another.

No Qt here: everything that touches the app (model, screen, locator,
executor, speech, questions, signals) comes in through AutopilotDeps, so the
whole machine runs under pytest with fakes.

Privacy: private steps never capture the screen or call the model, typed
text is never logged (only its length), and private replies are not stored.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, List, Optional

from agent.planner import (
    Goal, PlanParseError, PlanResult, Step, extract_json_object,
)
from agent.safety import (
    CONFIRM, DEFAULT_CONFIRM_KEYWORDS, PRIVATE, classify, normalize_level,
)

log = logging.getLogger("clicky.autopilot")


class State(str, Enum):
    IDLE = "idle"
    PLANNING = "planning"
    ACTING = "acting"
    VERIFYING = "verifying"
    REPLANNING = "replanning"
    AWAITING_CONFIRM = "awaiting_confirm"
    AWAITING_PRIVATE = "awaiting_private"
    DONE = "done"
    CANCELLED = "cancelled"


# What the assistant says (Georgian, short)
SAY_PRIVATE = "აკრიფე შენი მონაცემები, მე არ ვუყურებ. როცა მორჩები, თქვი: მზად ვარ."
SAY_GOAL = "ახლა: {goal}"
SAY_ALL_DONE = "მზადაა! ყველაფერი შევასრულე."
SAY_CANCELLED = "კარგი, გავჩერდი."
SAY_DECLINED = "კარგი, ამას არ გავაკეთებ. შევჩერდი."
SAY_GAVE_UP = "ვერ მოვახერხე ამ ნაბიჯის შესრულება. ბოდიში."
SAY_REPLAN = "სხვა გზას ვცდი."
SAY_USER_REPLAN = "კარგი, თავიდან შევხედავ ეკრანს."
ASK_CONFIRM = "ეს მნიშვნელოვანი მოქმედებაა: {what}. გავაგრძელო? თქვით „დიახ“ ან „არა“."

ACTION_TYPES = ("click", "double_click", "type", "key", "scroll", "wait")

DECIDE_SYSTEM_PROMPT = """You operate a Windows computer for an elderly Georgian-speaking person,
one small action at a time. You get the task, the plan, the CURRENT STEP,
the recent actions and a screenshot of the screen as it is now.

Reply with ONE JSON object and nothing else (no markdown, no prose):
{"step_done": <true|false>,
 "action": {"type": "click|double_click|type|key|scroll|wait",
            "target": "<string>", "text": "<string>", "keys": "<string>",
            "amount": <int>} or null,
 "say_ka": "<string>",
 "safety": "safe|confirm|private",
 "stuck": <true|false>}

RULES
- If the screenshot shows the CURRENT STEP's "done when" condition is
  already true, reply step_done: true and action: null.
- Otherwise choose exactly ONE next action that moves the current step
  forward.
- target: the element's visible text copied exactly as on screen (any
  language). If it has no text, a short description ("search box",
  "Start button"). NEVER coordinates or pixel positions.
- type: "text" is exactly what to type; "target" is the field to type into
  (it is clicked first). key: "keys" like "enter" or "ctrl+l".
  scroll: "amount" = wheel steps, positive = up. wait: "amount" = seconds.
- say_ka: one short, calm Georgian sentence for the person, polite form,
  saying what you are doing now. Empty when step_done is true.
- safety: "confirm" for anything that sends, deletes, pays, buys, orders,
  submits or confirms; "private" when the person must enter a password,
  card data or a one-time code — you never type those; otherwise "safe".
- stuck: true when you cannot see how to make progress (unexpected window,
  error message, missing element)."""


# ── Pure helpers ─────────────────────────────────────────────────────────────

def interpret_reply(text: str, kind: str) -> Optional[str]:
    """A spoken/typed reply while waiting. confirm: only „დიახ“ is yes,
    anything else is no. private: „მზად ვარ“ (or similar) is ready, other
    talk is ignored (None)."""
    t = (text or "").strip().casefold().strip(" .!?,")
    if kind == "confirm":
        return "yes" if t.startswith("დიახ") or t in ("yes", "yes please") else "no"
    if kind == "private":
        if "მზად" in t or t in ("ready", "done", "i'm ready", "im ready"):
            return "ready"
        return None
    return None


@dataclass
class Decision:
    step_done: bool
    action: Optional[dict]
    say_ka: str
    safety: str
    stuck: bool


class DecisionError(ValueError):
    pass


def parse_decision(text: str) -> Decision:
    """Model reply -> Decision. Raises DecisionError."""
    try:
        data = extract_json_object(text)
    except PlanParseError as e:
        raise DecisionError(str(e)) from e
    step_done = data.get("step_done")
    if not isinstance(step_done, bool):
        raise DecisionError('"step_done" must be true or false')
    stuck = data.get("stuck", False)
    if not isinstance(stuck, bool):
        raise DecisionError('"stuck" must be true or false')
    say = data.get("say_ka") or ""
    if not isinstance(say, str):
        raise DecisionError('"say_ka" must be a string')
    action = data.get("action")
    if step_done or stuck:
        action = None if step_done else action
    if not step_done:
        if action is None and not stuck:
            raise DecisionError("an action is required when the step is not done")
        if action is not None:
            if not isinstance(action, dict):
                raise DecisionError('"action" must be an object')
            kind = action.get("type")
            if kind not in ACTION_TYPES:
                raise DecisionError(f"unknown action type {kind!r}")
            target = action.get("target")
            if kind in ("click", "double_click") and not (isinstance(target, str) and target.strip()):
                raise DecisionError(f"{kind} needs a target")
            if kind == "type" and not (isinstance(action.get("text"), str) and action["text"]):
                raise DecisionError("type needs text")
            if kind == "key" and not action.get("keys"):
                raise DecisionError("key needs keys")
            if kind == "scroll" and not isinstance(action.get("amount"), int):
                raise DecisionError("scroll needs an integer amount")
    return Decision(step_done=step_done, action=action, say_ka=say.strip(),
                    safety=normalize_level(data.get("safety")), stuck=stuck)


def image_diff(a, b) -> float:
    """0..1 difference between two screenshots (mean absolute grey-level
    difference on small thumbnails). Different monitor sizes = 1.0."""
    if a is None or b is None:
        return 1.0
    if (getattr(a, "physical_width", None), getattr(a, "physical_height", None)) != \
            (getattr(b, "physical_width", None), getattr(b, "physical_height", None)):
        return 1.0
    from PIL import Image, ImageChops, ImageStat

    def thumb(s):
        img = Image.open(io.BytesIO(base64.b64decode(s.base64_jpeg)))
        return img.convert("L").resize((96, 54))

    stat = ImageStat.Stat(ImageChops.difference(thumb(a), thumb(b)))
    return stat.mean[0] / 255.0


# ── Dependencies ─────────────────────────────────────────────────────────────

@dataclass
class Located:
    """Where an action's target is. Physical pixels; `element` when it came
    from UI Automation."""
    px: int
    py: int
    label: str
    element: Any = None
    source: str = ""
    is_private_field: bool = False


@dataclass
class AutopilotDeps:
    plan: Callable[[str, Any], Awaitable[PlanResult]]               # planner.plan
    decide: Callable[[str, str, Optional[str]], Awaitable[str]]     # system, user, image -> text
    capture: Callable[[], list]                                     # -> [ScreenShot]
    locate: Callable[[str, Any], Optional[Located]]
    execute: Callable[[dict, Any], Any]                             # -> executor.ActionResult
    say: Callable[[str], None]                                      # must not block
    ask: Callable[[str, str], Awaitable[Optional[str]]]             # kind, question -> reply
    on_state: Callable[[str, str], None] = lambda s, d: None
    on_plan: Callable[[list], None] = lambda goals: None
    on_step: Callable[[int, int, Step], None] = lambda gi, si, s: None
    # the current goal's steps — at its start and after every replan
    on_steps: Callable[[int, List[Step]], None] = lambda gi, steps: None
    # right before an action runs: (type, target label, say_ka) — never the typed text
    on_action: Callable[[str, str, str], None] = lambda kind, label, say: None
    image_diff: Callable[[Any, Any], float] = image_diff
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    run_blocking: Optional[Callable] = None     # default: asyncio.to_thread


@dataclass
class Settings:
    max_actions_per_goal: int = 15
    max_failures: int = 3
    max_replans: int = 2
    history_len: int = 6
    settle_timeout_s: float = 4.0
    settle_interval_s: float = 0.35
    change_eps: float = 0.01      # screen counts as changed above this diff
    stable_eps: float = 0.003     # two samples this close = settled
    dry_run: bool = False
    confirm_keywords: tuple = DEFAULT_CONFIRM_KEYWORDS


@dataclass
class Outcome:
    handled: bool                 # False: not a task — the normal Q&A runs
    state: State = State.IDLE
    goals_done: int = 0
    message: str = ""
    transitions: List[str] = field(default_factory=list)


class _Stop(Exception):
    def __init__(self, state: State, message: str):
        super().__init__(message)
        self.state, self.message = state, message


# ── The machine ──────────────────────────────────────────────────────────────

class Autopilot:
    def __init__(self, deps: AutopilotDeps, cancel_event: threading.Event,
                 settings: Optional[Settings] = None):
        self.d = deps
        self.cancel_event = cancel_event
        self.s = settings or Settings()
        self.state = State.IDLE
        self.transitions: List[str] = []
        self.request = ""
        self._actions = 0
        self._replan_requested = threading.Event()   # „დავიბენი“

    # state + helpers
    def _set(self, state: State, detail: str = ""):
        if state != self.state or detail:
            log.info("autopilot: %s -> %s%s", self.state.value, state.value,
                     f" ({detail})" if detail else "")
            self.transitions.append(state.value)
        self.state = state
        try:
            self.d.on_state(state.value, detail)
        except Exception:
            pass

    def _check_cancel(self):
        if self.cancel_event.is_set():
            raise _Stop(State.CANCELLED, SAY_CANCELLED)

    async def _blocking(self, fn, *args):
        runner = self.d.run_blocking or asyncio.to_thread
        return await runner(fn, *args)

    async def _screen(self):
        shots = await self._blocking(self.d.capture)
        return shots[0] if shots else None

    def _say(self, text: str):
        if text:
            try:
                self.d.say(text)
            except Exception:
                log.debug("say failed", exc_info=True)

    def request_replan(self):
        """„დავიბენი“: the person is lost — look at the screen again and plan
        the rest of the current goal from there. Thread-safe."""
        self._replan_requested.set()

    def _take_replan_request(self) -> bool:
        if self._replan_requested.is_set():
            self._replan_requested.clear()
            return True
        return False

    # entry points
    async def plan_request(self, user_text: str) -> PlanResult:
        """planning: decide whether this is a task at all."""
        self.request = user_text
        self.cancel_event.clear()
        self._set(State.PLANNING)
        from timing import timed
        with timed("autopilot.planning_screenshot"):
            shot = await self._screen()
        with timed("autopilot.planner") as tm:
            result = await self.d.plan(user_text, shot)
            tm["is_task"] = result.is_task
        if not result.is_task:
            self._set(State.IDLE, "not a task")
        return result

    async def run_plan(self, plan: PlanResult) -> Outcome:
        """Run every goal in order. Never raises."""
        goals_done = 0
        try:
            self.d.on_plan(plan.goals)
            for gi, goal in enumerate(plan.goals):
                self._check_cancel()
                if len(plan.goals) > 1:
                    self._say(SAY_GOAL.format(goal=goal.goal))
                await self._run_goal(gi, goal)
                goals_done += 1
            self._set(State.DONE, "all goals done")
            self._say(SAY_ALL_DONE)
            return Outcome(True, State.DONE, goals_done, "done", self.transitions)
        except _Stop as stop:
            self._set(stop.state, stop.message)
            self._say(stop.message)
            return Outcome(True, stop.state, goals_done, stop.message, self.transitions)
        except Exception as e:                       # never leave the app hanging
            log.exception("autopilot crashed")
            self._set(State.DONE, f"error: {e}")
            self._say(SAY_GAVE_UP)
            return Outcome(True, State.DONE, goals_done, f"error: {e}", self.transitions)

    async def handle(self, user_text: str) -> Outcome:
        plan = await self.plan_request(user_text)
        if not plan.is_task:
            return Outcome(False, State.IDLE, 0, plan.error or "not a task", self.transitions)
        return await self.run_plan(plan)

    # one goal
    async def _run_goal(self, gi: int, goal: Goal):
        steps: List[Step] = list(goal.steps)
        history: List[str] = []
        self._actions = 0
        replans = 0
        i = 0
        self.d.on_steps(gi, steps)
        while i < len(steps):
            self._check_cancel()
            step = steps[i]
            self.d.on_step(gi, i, step)
            if self._take_replan_request():
                self._say(SAY_USER_REPLAN)
                steps = await self._replan(goal, steps, i, by_user=True)
                self.d.on_steps(gi, steps)
                continue
            if step.private:
                await self._private_pause()
                history.append(f"step {i + 1}: done by the person (private)")
                i += 1
                continue
            result = await self._run_step(gi, goal, steps, i, history)
            if result == "done":
                i += 1
                continue
            if result == "user_replan":
                self._say(SAY_USER_REPLAN)
                steps = await self._replan(goal, steps, i, by_user=True)
                self.d.on_steps(gi, steps)
                continue
            # "replan": 3 failed attempts or the model is stuck
            if replans >= self.s.max_replans:
                raise _Stop(State.DONE, SAY_GAVE_UP)
            replans += 1
            steps = await self._replan(goal, steps, i)    # same i, new steps
            self.d.on_steps(gi, steps)

    async def _run_step(self, gi, goal, steps, i, history) -> str:
        """Try one step until it is done ("done") or needs a new plan ("replan")."""
        failures = 0
        while True:
            self._check_cancel()
            if self._take_replan_request():
                return "user_replan"
            if self._actions >= self.s.max_actions_per_goal:
                raise _Stop(State.DONE, SAY_GAVE_UP)
            self._set(State.ACTING, f"goal {gi + 1} step {i + 1}")
            before = await self._screen()
            decision = await self._decide(goal, steps, i, history, before)
            if decision is not None and decision.step_done:
                history.append(f"step {i + 1} done")
                return "done"
            ok = False
            if decision is not None and decision.action is not None:
                ok = await self._act(decision, before, history)
                if ok == "private_done":
                    continue                  # look at the screen again
                if ok is True and self.s.dry_run:
                    # Nothing really happened on screen; assume it worked.
                    history.append(f"step {i + 1} assumed done (dry run)")
                    return "done"
            if decision is not None and decision.stuck:
                return "replan"
            if ok is not True:
                failures += 1
                if failures >= self.s.max_failures:
                    return "replan"

    async def _private_pause(self):
        self._set(State.AWAITING_PRIVATE)
        self._say(SAY_PRIVATE)
        reply = await self.d.ask("private", SAY_PRIVATE)
        self._check_cancel()
        if reply != "ready":
            raise _Stop(State.CANCELLED, SAY_CANCELLED)

    async def _decide(self, goal, steps, i, history, shot) -> Optional[Decision]:
        from timing import mark, since_ms
        t_dec = time.perf_counter()
        user = self._decision_prompt(goal, steps, i, history)
        image = shot.base64_jpeg if shot is not None else None
        error = ""
        for attempt in (1, 2):
            prompt = user if not error else (
                user + f"\n\nYour previous reply could not be used: {error}. "
                       "Reply again with ONLY the JSON object.")
            self._check_cancel()
            try:
                raw = await self.d.decide(DECIDE_SYSTEM_PROMPT, prompt, image)
            except Exception as e:
                log.warning("decision call failed: %s", e)
                return None
            try:
                decision = parse_decision(raw)
                mark("autopilot.decide", since_ms(t_dec), step=i + 1, attempts=attempt,
                     step_done=decision.step_done,
                     action=(decision.action or {}).get("type"))
                return decision
            except DecisionError as e:
                error = str(e)
                log.warning("decision rejected (attempt %d): %s", attempt, error)
        return None

    def _decision_prompt(self, goal, steps, i, history) -> str:
        lines = [f"USER REQUEST: {self.request}", f"CURRENT GOAL: {goal.goal}", "PLAN:"]
        for j, s in enumerate(steps):
            mark = "done" if j < i else ("CURRENT" if j == i else "todo")
            lines.append(f"  {j + 1}. [{mark}] {s.title} — done when: {s.done_when}")
        step = steps[i]
        lines.append(f"CURRENT STEP: {i + 1}. {step.title}")
        lines.append(f"DONE WHEN: {step.done_when}")
        recent = history[-self.s.history_len:]
        lines.append("RECENT ACTIONS: " + ("; ".join(recent) if recent else "none yet"))
        lines.append("A screenshot of the screen as it is now is attached.")
        return "\n".join(lines)

    async def _act(self, decision: Decision, before, history):
        """Locate, gate, say, execute, settle. Returns True (the screen
        changed), False (failed attempt) or "private_done"."""
        a = decision.action
        kind = a["type"]
        label = (a.get("target") or "").strip()
        where = None
        if label and kind in ("click", "double_click", "type", "scroll"):
            where = await self._blocking(self.d.locate, label, before)
            if where is None:
                history.append(f"{kind} {label!r}: not found on screen")
                log.info("target %r not found", label)
                return False

        gate = classify(decision.safety, a, where.label if where else label,
                        bool(where and where.is_private_field),
                        self.s.confirm_keywords)
        log.info("safety %s for %s %r (%s)", gate.level, kind, label, "; ".join(gate.reasons))
        if gate.level == PRIVATE:
            await self._private_pause()
            history.append(f"{kind} {label!r}: done by the person (private)")
            return "private_done"
        if gate.level == CONFIRM:
            self._set(State.AWAITING_CONFIRM, label or kind)
            question = ASK_CONFIRM.format(what=decision.say_ka or label or kind)
            reply = await self.d.ask("confirm", question)
            self._check_cancel()
            if reply != "yes":
                raise _Stop(State.DONE, SAY_DECLINED)
            self._set(State.ACTING, "confirmed")

        self._say(decision.say_ka)
        steps_to_run = []
        target = None
        if where is not None and where.element is not None:
            from types import SimpleNamespace
            target = SimpleNamespace(x=where.px, y=where.py, label=where.label,
                                     bbox=(where.px, where.py, where.px, where.py),
                                     source="uia", element=where.element)
        elif where is not None:
            target = {"x": where.px, "y": where.py, "space": "physical", "label": where.label}
        if kind == "type" and target is not None:
            steps_to_run.append({"type": "click", "target": target,
                                 "explanation": decision.say_ka})
        exec_action = {"type": kind, "explanation": decision.say_ka}
        if kind in ("click", "double_click") or (kind == "scroll" and target is not None):
            exec_action["target"] = target
        if kind == "type":
            exec_action["text"] = a["text"]
        if kind == "key":
            exec_action["keys"] = a["keys"]
        if kind == "scroll":
            exec_action["amount"] = a["amount"]
        if kind == "wait":
            exec_action["seconds"] = float(a.get("amount") or 1)
        steps_to_run.append(exec_action)

        try:
            self.d.on_action(kind, where.label if where else label, decision.say_ka)
        except Exception:
            log.debug("on_action failed", exc_info=True)

        for act in steps_to_run:
            self._check_cancel()
            result = await self._blocking(self.d.execute, act, before)
            self._actions += 1
            status = getattr(result, "status", "failed")
            # Never the typed text — only its length.
            what = f"{act['type']} ({len(act['text'])} chars)" if act["type"] == "type" \
                else f"{act['type']} {label!r}"
            history.append(f"{what}: {status}")
            if status == "cancelled":
                raise _Stop(State.CANCELLED, SAY_CANCELLED)
            if not getattr(result, "ok", False):
                return False

        if self.s.dry_run:
            return True
        self._set(State.VERIFYING)
        changed = await self._settle(before)
        if not changed:
            history.append("the screen did not change")
        return bool(changed)

    async def _settle(self, before) -> bool:
        """Wait until two consecutive screenshots agree (or time out); report
        whether the screen differs from `before`."""
        from timing import timed
        with timed("autopilot.settle", timeout_s=self.s.settle_timeout_s) as tm:
            changed, samples, timed_out = await self._settle_inner(before)
            tm.update(changed=changed, samples=samples, timed_out=timed_out)
        return changed

    async def _settle_inner(self, before):
        deadline = time.monotonic() + self.s.settle_timeout_s
        prev, stable, samples = before, 0, 0
        while time.monotonic() < deadline:
            self._check_cancel()
            await self.d.sleep(self.s.settle_interval_s)
            cur = await self._screen()
            samples += 1
            if cur is None:
                break
            stable = stable + 1 if self.d.image_diff(prev, cur) < self.s.stable_eps else 0
            prev = cur
            if stable >= 2:
                return self.d.image_diff(before, prev) > self.s.change_eps, samples, False
        return self.d.image_diff(before, prev) > self.s.change_eps, samples, True

    async def _replan(self, goal: Goal, steps: List[Step], i: int,
                      by_user: bool = False) -> List[Step]:
        self._set(State.REPLANNING, ("asked by the person — " if by_user else "")
                  + f"step {i + 1}: {steps[i].title}")
        if not by_user:
            self._say(SAY_REPLAN)
        shot = await self._screen()
        done = "; ".join(s.title for s in steps[:i]) or "nothing yet"
        why = ("The person says they are lost at the step" if by_user
               else "This step did not work:")
        text = (f"{self.request}\n\nCurrent goal: {goal.goal}. Already done: {done}. "
                f"{why} '{steps[i].title}'. Plan ONLY the remaining "
                f"steps of this goal, starting from what is on the screen now.")
        result = await self.d.plan(text, shot)
        if not result.is_task or not result.goals:
            raise _Stop(State.DONE, SAY_GAVE_UP)
        new = [Step(id=i + k + 1, title=s.title, done_when=s.done_when, private=s.private)
               for k, s in enumerate(result.goals[0].steps)]
        return steps[:i] + new

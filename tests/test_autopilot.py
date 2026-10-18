"""agent.autopilot state machine with the model, screen, locator and executor
all faked — no real input, no screenshots, no network."""

import asyncio
import base64
import io
import json
import threading
from types import SimpleNamespace

import pytest

from agent import autopilot as ap
from agent.planner import Goal, PlanResult, Step


def steps(*titles, private=()):
    return [Step(id=i + 1, title=t, done_when=f"{t} — დასრულდა", private=(i + 1) in private)
            for i, t in enumerate(titles)]


def done():
    return json.dumps({"step_done": True, "action": None, "say_ka": "", "safety": "safe", "stuck": False})


def act(kind="click", target="Chrome", safety="safe", say="ვაჭერ ღილაკს", stuck=False, **extra):
    action = {"type": kind, "target": target, **extra}
    return json.dumps({"step_done": False, "action": action, "say_ka": say,
                       "safety": safety, "stuck": stuck}, ensure_ascii=False)


class Env:
    def __init__(self, plans, decisions, replies=(), screen_changes=True,
                 missing=(), password_fields=(), cancel_on_execute=None):
        self.plans = list(plans)
        self.decisions = list(decisions)
        self.replies = list(replies)
        self.screen_changes = screen_changes
        self.missing, self.password_fields = set(missing), set(password_fields)
        self.cancel_on_execute = cancel_on_execute
        self.version = 0
        self.events = []
        self.executed = []
        self.said = []
        self.prompts = []
        self.states = []
        self.cancel = threading.Event()

    # deps
    async def plan(self, text, shot):
        self.events.append(("plan",))
        return self.plans.pop(0)

    async def decide(self, system, user, image):
        self.events.append(("decide",))
        self.prompts.append(user)
        return self.decisions.pop(0) if self.decisions else done()

    def capture(self):
        self.events.append(("capture",))
        return [SimpleNamespace(version=self.version, base64_jpeg=f"v{self.version}",
                                physical_width=10, physical_height=10)]

    def locate(self, label, shot):
        if label in self.missing:
            return None
        return ap.Located(px=100, py=200, label=label,
                          is_private_field=label in self.password_fields)

    def execute(self, action, shot):
        self.events.append(("execute", action["type"]))
        self.executed.append(action)
        if self.cancel_on_execute is not None and len(self.executed) == self.cancel_on_execute:
            self.cancel.set()
        if self.screen_changes:
            self.version += 1
        return SimpleNamespace(ok=True, status="done")

    async def ask(self, kind, question):
        self.events.append(("ask", kind))
        return self.replies.pop(0) if self.replies else None

    def machine(self, **settings):
        async def run_blocking(fn, *args):
            return fn(*args)

        async def no_sleep(_):
            return None

        deps = ap.AutopilotDeps(
            plan=self.plan, decide=self.decide, capture=self.capture, locate=self.locate,
            execute=self.execute, say=self.said.append, ask=self.ask,
            on_state=lambda s, d: self.states.append(s),
            image_diff=lambda a, b: 0.0 if a.version == b.version else 1.0,
            sleep=no_sleep, run_blocking=run_blocking,
        )
        return ap.Autopilot(deps, self.cancel, ap.Settings(**settings))


def run(env, text="გახსენი ბრაუზერი", **settings):
    return asyncio.run(env.machine(**settings).handle(text))


TASK = lambda *goals: PlanResult(is_task=True, goals=list(goals))


# ── normal run ───────────────────────────────────────────────────────────────

def test_normal_run_acts_verifies_and_finishes():
    goal = Goal("ძებნა", steps("გახსენით Chrome", "დაელოდეთ", "მოძებნეთ"))
    env = Env([TASK(goal)], [
        act("click", "Google Chrome"), done(),                     # step 1
        done(),                                                     # step 2 already true
        act("type", "Search", text="ამინდი თბილისში"), done(),    # step 3
    ])
    out = run(env)
    assert out.handled and out.state == ap.State.DONE and out.goals_done == 1
    assert [a["type"] for a in env.executed] == ["click", "click", "type"]   # type clicks its field first
    assert env.executed[0]["target"] == {"x": 100, "y": 200, "space": "physical",
                                         "label": "Google Chrome"}
    assert env.executed[2]["text"] == "ამინდი თბილისში"
    assert "ვაჭერ ღილაკს" in env.said and env.said[-1] == ap.SAY_ALL_DONE
    for s in ("planning", "acting", "verifying", "done"):
        assert s in env.states
    # the typed text never goes back to the model, only its length
    assert all("ამინდი" not in p for p in env.prompts[env.prompts.index(env.prompts[-1]):])
    assert any("type (15 chars)" in p for p in env.prompts)


def test_not_a_task_falls_through():
    env = Env([PlanResult(is_task=False)], [])
    out = run(env, "რა ამინდია?")
    assert out.handled is False and env.executed == []
    assert env.states == ["planning", "idle"]


def test_several_goals_run_in_order_and_are_announced():
    g1 = Goal("მეილის გახსნა", steps("ა", "ბ", "გ"))
    g2 = Goal("წერილის გახსნა", steps("დ", "ე", "ვ"))
    env = Env([TASK(g1, g2)], [done()] * 6)
    out = run(env)
    assert out.state == ap.State.DONE and out.goals_done == 2
    assert ap.SAY_GOAL.format(goal="მეილის გახსნა") in env.said
    assert ap.SAY_GOAL.format(goal="წერილის გახსნა") in env.said


# ── failures and replanning ──────────────────────────────────────────────────

def test_screen_never_changes_replans_then_gives_up():
    goal = Goal("ძებნა", steps("ა", "ბ", "გ"))
    replanned = PlanResult(is_task=True, goals=[Goal("ძებნა", steps("ა2", "ბ2", "გ2"))])
    env = Env([TASK(goal), replanned, replanned],
              [act("click", "OK")] * 30, screen_changes=False)
    out = run(env)
    assert out.state == ap.State.DONE and out.message == ap.SAY_GAVE_UP
    assert [e for e in env.events if e == ("plan",)] == [("plan",)] * 3   # first + 2 replans
    assert env.states.count("replanning") == 2
    assert len(env.executed) == 9          # 3 attempts x (1 + 2 replans)
    assert "the screen did not change" in env.prompts[-1]


def test_action_limit_per_goal():
    goal = Goal("ძებნა", steps("ა", "ბ", "გ"))
    env = Env([TASK(goal)], [act("click", "OK")] * 40)        # screen changes, never done
    out = run(env, max_actions_per_goal=15)
    assert out.message == ap.SAY_GAVE_UP and len(env.executed) == 15


def test_target_not_found_counts_as_a_failure():
    goal = Goal("ძებნა", steps("ა", "ბ", "გ"))
    replan = PlanResult(is_task=True, goals=[Goal("ძებნა", steps("ა", "ბ", "გ"))])
    env = Env([TASK(goal), replan, replan], [act("click", "Ghost")] * 20, missing={"Ghost"})
    out = run(env)
    assert out.message == ap.SAY_GAVE_UP and env.executed == []


# ── safety: confirm ──────────────────────────────────────────────────────────

def test_confirm_keyword_asks_and_runs_on_yes():
    goal = Goal("გაგზავნა", steps("ა", "ბ", "გ"))
    env = Env([TASK(goal)], [act("click", "გაგზავნა", safety="safe"), done(), done(), done()],
              replies=["yes"])
    out = run(env)
    assert ("ask", "confirm") in env.events and out.state == ap.State.DONE
    assert env.executed and env.executed[0]["target"]["label"] == "გაგზავნა"
    assert "awaiting_confirm" in env.states
    ask_at = env.events.index(("ask", "confirm"))
    assert env.events.index(("execute", "click")) > ask_at           # asked before acting


@pytest.mark.parametrize("reply", ["no", None])
def test_confirm_without_yes_does_nothing_and_stops(reply):
    goal = Goal("გადახდა", steps("ა", "ბ", "გ"))
    env = Env([TASK(goal)], [act("click", "Pay now", safety="safe")], replies=[reply])
    out = run(env)
    assert env.executed == [] and out.message == ap.SAY_DECLINED


def test_model_confirm_label_is_respected():
    goal = Goal("ა", steps("ა", "ბ", "გ"))
    env = Env([TASK(goal)], [act("click", "Next", safety="confirm")], replies=["no"])
    run(env)
    assert ("ask", "confirm") in env.events and env.executed == []


# ── safety: private ──────────────────────────────────────────────────────────

def test_private_step_takes_no_screenshot_and_waits_for_ready():
    goal = Goal("შესვლა", steps("გახსენით გვერდი", "შეიყვანეთ პაროლი", "დააჭირეთ შესვლას", private=(2,)))
    env = Env([TASK(goal)], [done(), done(), done()], replies=["ready"])
    out = run(env)
    assert out.state == ap.State.DONE
    i = env.events.index(("ask", "private"))
    # step 1 finished with a decision; the private step neither captured nor asked the model
    assert env.events[i - 1] == ("decide",)
    assert env.events[i + 1] == ("capture",)          # step 3 starts normally
    assert ap.SAY_PRIVATE in env.said
    assert not any("[CURRENT] შეიყვანეთ პაროლი" in p for p in env.prompts)
    assert "awaiting_private" in env.states


def test_typing_into_a_password_field_is_never_done_by_the_app():
    goal = Goal("შესვლა", steps("ა", "ბ", "გ"))
    env = Env([TASK(goal)], [act("type", "Password", text="hunter2"), done(), done(), done()],
              replies=["ready"], password_fields={"Password"})
    out = run(env)
    assert out.state == ap.State.DONE
    assert not any(a["type"] == "type" for a in env.executed)
    assert all("hunter2" not in p for p in env.prompts)


def test_private_without_ready_cancels():
    goal = Goal("შესვლა", steps("პაროლი", "ბ", "გ", private=(1,)))
    env = Env([TASK(goal)], [], replies=[None])
    out = run(env)
    assert out.state == ap.State.CANCELLED and env.executed == []


# ── cancel ───────────────────────────────────────────────────────────────────

def test_cancel_mid_run_stops_everything_and_clears_the_queue():
    g1 = Goal("პირველი", steps("ა", "ბ", "გ"))
    g2 = Goal("მეორე", steps("დ", "ე", "ვ"))
    env = Env([TASK(g1, g2)], [act("click", "OK")] * 10, cancel_on_execute=1)
    out = run(env)
    assert out.state == ap.State.CANCELLED and out.goals_done == 0
    assert len(env.executed) == 1
    assert env.said[-1] == ap.SAY_CANCELLED
    assert ap.SAY_GOAL.format(goal="მეორე") not in env.said


def test_dry_run_assumes_each_action_worked():
    goal = Goal("ა", steps("ა", "ბ", "გ"))
    env = Env([TASK(goal)], [act("click", "X")] * 3, screen_changes=False)
    out = run(env, dry_run=True)
    assert out.state == ap.State.DONE and len(env.executed) == 3
    assert "verifying" not in env.states


# ── pure helpers ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, kind, reply", [
    ("დიახ", "confirm", "yes"), ("დიახ.", "confirm", "yes"), ("არა", "confirm", "no"),
    ("კარგი", "confirm", "no"), ("yes", "confirm", "yes"),
    ("მზად ვარ", "private", "ready"), ("უკვე მზადაა", "private", "ready"),
    ("ერთი წუთი", "private", None),
])
def test_interpret_reply(text, kind, reply):
    assert ap.interpret_reply(text, kind) == reply


def test_parse_decision_validates():
    d = ap.parse_decision("```json\n" + act("type", "Search", text="x") + "\n```")
    assert d.action["type"] == "type" and d.safety == "safe" and not d.step_done
    with pytest.raises(ap.DecisionError):
        ap.parse_decision(act("click", ""))
    with pytest.raises(ap.DecisionError):
        ap.parse_decision(act("teleport", "x"))
    assert ap.parse_decision(json.dumps({"step_done": False, "action": None, "say_ka": "",
                                         "stuck": True})).stuck


def test_image_diff_on_real_jpegs():
    from PIL import Image

    def shot(color):
        buf = io.BytesIO()
        Image.new("RGB", (64, 36), color).save(buf, "JPEG")
        return SimpleNamespace(base64_jpeg=base64.b64encode(buf.getvalue()).decode(),
                               physical_width=64, physical_height=36)

    assert ap.image_diff(shot((10, 10, 10)), shot((10, 10, 10))) < 0.01
    assert ap.image_diff(shot((0, 0, 0)), shot((255, 255, 255))) > 0.9
    other = shot((0, 0, 0)); other.physical_width = 99
    assert ap.image_diff(shot((0, 0, 0)), other) == 1.0


# ── „დავიბენი“ (P2) ─────────────────────────────────────────────────────────

def test_user_replan_from_the_current_screen_does_not_use_up_the_limit():
    goal = Goal("ძებნა", steps("ა", "ბ", "გ"))
    new = PlanResult(is_task=True, goals=[Goal("ძებნა", steps("ა2", "ბ2", "გ2"))])
    env = Env([TASK(goal), new, new, new], [done()] * 30)
    machine = env.machine(max_replans=0)          # automatic replans not allowed
    seen_steps = []
    machine.d.on_steps = lambda gi, s: seen_steps.append([x.title for x in s])
    machine.request_replan()                       # pressed before step 1
    out = asyncio.run(machine.handle("მოძებნე"))
    assert out.state == ap.State.DONE
    assert seen_steps[0] == ["ა", "ბ", "გ"] and seen_steps[1] == ["ა2", "ბ2", "გ2"]
    assert "replanning" in env.states and ap.SAY_USER_REPLAN in env.said
    # the replan looked at the screen as it is now
    plan_i = [i for i, e in enumerate(env.events) if e == ("plan",)][1]
    assert env.events[plan_i - 1] == ("capture",)


def test_user_replan_between_attempts():
    goal = Goal("ძებნა", steps("ა", "ბ", "გ"))
    new = PlanResult(is_task=True, goals=[Goal("ძებნა", steps("x", "y", "z"))])
    env = Env([TASK(goal), new], [act("click", "OK"), done(), done(), done()])
    machine = env.machine()
    orig = env.execute

    def execute_then_lost(action, shot):
        result = orig(action, shot)
        machine.request_replan()                   # the person presses დავიბენი
        return result
    machine.d.execute = execute_then_lost
    out = asyncio.run(machine.handle("მოძებნე"))
    assert out.state == ap.State.DONE
    assert [e for e in env.events if e == ("plan",)] == [("plan",)] * 2


# ── island hook (on_action) ──────────────────────────────────────────────────

def test_on_action_announces_each_action_before_it_runs_without_typed_text():
    goal = Goal("ძებნა", steps("ა", "ბ", "გ"))
    env = Env([TASK(goal)], [act("double_click", "Zoom Workplace", say="ვხსნი ზუმს"), done(),
                             act("type", "Search", text="საიდუმლო", say=""), done(), done()])
    machine = env.machine()
    seen = []
    machine.d.on_action = lambda kind, label, say: (seen.append((kind, label, say)),
                                                    env.events.append(("announce",)))
    asyncio.run(machine.handle("გახსენი ზუმი"))
    assert seen == [("double_click", "Zoom Workplace", "ვხსნი ზუმს"), ("type", "Search", "")]
    first = env.events.index(("announce",))
    assert env.events[first + 1][0] == "execute"             # announced, then executed
    assert all("საიდუმლო" not in str(s) for s in seen)

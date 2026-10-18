"""CompanionManager._agent_intercept: mode switch, single flight, and routing
replies to a running autopilot. Autopilot is faked; no windows, no mic
stream, no model calls."""

import asyncio
import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import agent.autopilot as ap_mod
import agent.executor as agent_executor
import companion_manager as cm
from agent.planner import Goal, PlanResult, Step
from config import cfg


class FakePilot:
    instances = []
    is_task = True
    cancel_while_planning = False

    def __init__(self, deps, cancel_event, settings=None):
        self.cancel_event = cancel_event
        self.planned = []
        self.release = threading.Event()
        FakePilot.instances.append(self)

    async def plan_request(self, text):
        self.planned.append(text)
        self.ran = False
        if FakePilot.cancel_while_planning:
            self.cancel_event.set()          # Esc / stop window during planning
        if not FakePilot.is_task:
            return PlanResult(is_task=False)
        return PlanResult(is_task=True, goals=[Goal("ა", [Step(1, "ა", "ბ")] * 3)])

    async def run_plan(self, plan):
        self.ran = True
        while not self.release.is_set() and not self.cancel_event.is_set():
            await asyncio.sleep(0.01)


class FakeTTS:
    async def speak(self, text):
        return None


@pytest.fixture
def mgr(monkeypatch):
    FakePilot.instances.clear()
    FakePilot.is_task = True
    FakePilot.cancel_while_planning = False
    monkeypatch.setattr(ap_mod, "Autopilot", FakePilot)
    monkeypatch.setattr(cfg, "agent_mode", "do")
    agent_executor.reset_cancel()
    m = cm.CompanionManager()
    m._tts = FakeTTS()
    deadline = time.time() + 5
    while m._loop is None and time.time() < deadline:
        time.sleep(0.01)
    yield m
    for p in FakePilot.instances:
        p.release.set()
    agent_executor.reset_cancel()


def call(m, text):
    return asyncio.run_coroutine_threadsafe(m._agent_intercept(text), m._loop).result(5)


def wait_until(cond, timeout=3):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_guide_mode_never_plans(mgr, monkeypatch):
    monkeypatch.setattr(cfg, "agent_mode", "guide")
    assert call(mgr, "გახსენი მეილი") is False
    assert FakePilot.instances == []


def test_question_falls_through_to_the_old_flow(mgr):
    FakePilot.is_task = False
    assert call(mgr, "რა ამინდია?") is False
    assert mgr._agent is None and mgr._agent_lock.acquire(blocking=False)   # released
    mgr._agent_lock.release()


def test_voice_commands_skip_planning(mgr):
    assert call(mgr, "გაჩერდი") is False
    assert FakePilot.instances == []


def test_task_starts_one_run_and_blocks_a_second(mgr):
    assert call(mgr, "გახსენი მეილი") is True
    assert wait_until(lambda: mgr._agent is not None)
    replies = []

    async def record(msg):
        replies.append(msg)
    mgr._reply_local = record
    assert call(mgr, "გახსენი იუთუბი") is True          # busy, not a second run
    assert len(FakePilot.instances) == 1
    assert any("წინა დავალებას" in r for r in replies)
    FakePilot.instances[0].release.set()
    assert wait_until(lambda: mgr._agent is None)        # lock released afterwards
    assert mgr._agent_lock.acquire(blocking=False)
    mgr._agent_lock.release()


def test_spoken_yes_reaches_a_waiting_confirm(mgr):
    assert call(mgr, "გაგზავნე წერილი") is True
    assert wait_until(lambda: mgr._agent is not None)
    fut = asyncio.run_coroutine_threadsafe(
        mgr._agent_ask("confirm", "გავაგრძელო?"), mgr._loop)
    assert wait_until(lambda: mgr._agent_wait is not None)
    assert call(mgr, "დიახ") is True
    assert fut.result(3) == "yes"


def test_panel_ready_button_reaches_a_private_wait(mgr):
    assert call(mgr, "შედი ანგარიშზე") is True
    assert wait_until(lambda: mgr._agent is not None)
    fut = asyncio.run_coroutine_threadsafe(
        mgr._agent_ask("private", "აკრიფე"), mgr._loop)
    assert wait_until(lambda: mgr._agent_wait is not None)
    mgr.agent_reply("ready")                              # from the GUI thread
    assert fut.result(3) == "ready"


def test_stop_cancels_a_running_autopilot(mgr):
    assert call(mgr, "გახსენი მეილი") is True
    assert wait_until(lambda: mgr._agent is not None)
    assert call(mgr, "გაჩერდი") is True
    assert agent_executor.cancel_event.is_set()
    assert wait_until(lambda: mgr._agent is None)


# ── running flag + stopping (P1) ─────────────────────────────────────────────

from PyQt6.QtCore import Qt as _Qt


def _record_running(m):
    seen = []
    # Direct: the signal is emitted on the manager's worker thread and pytest
    # runs no Qt event loop to deliver queued calls.
    m.sig_agent_running.connect(seen.append, _Qt.ConnectionType.DirectConnection)
    return seen


def test_running_flag_covers_the_whole_run(mgr):
    seen = _record_running(mgr)
    assert mgr.agent_running is False
    assert call(mgr, "გახსენი მეილი") is True
    assert wait_until(lambda: mgr._agent is not None)
    assert mgr.agent_running is True and seen == [True]
    # waiting for a reply: the manager itself is IDLE, the flag stays on
    asyncio.run_coroutine_threadsafe(mgr._agent_ask("confirm", "გავაგრძელო?"), mgr._loop)
    assert wait_until(lambda: mgr._agent_wait is not None)
    assert mgr._state == cm.AppState.IDLE and mgr.agent_running is True
    FakePilot.instances[0].release.set()
    mgr.agent_reply("no")
    assert wait_until(lambda: not mgr.agent_running)
    assert seen == [True, False]


def test_panel_button_stops_a_run_instead_of_listening(mgr):
    started = []
    mgr.on_hotkey_press = lambda: started.append(1)
    assert call(mgr, "გახსენი მეილი") is True
    assert wait_until(lambda: mgr._agent is not None)
    assert mgr._state == cm.AppState.IDLE
    mgr.on_panel_ask()                                   # the big button
    assert started == []                                 # did not open the mic
    assert agent_executor.cancel_event.is_set()
    assert wait_until(lambda: not mgr.agent_running)


def test_stop_window_cancel_path(mgr):
    assert call(mgr, "გახსენი მეილი") is True
    assert wait_until(lambda: mgr._agent is not None)
    mgr.cancel_agent()                                   # StopWindow.on_stop
    assert agent_executor.cancel_event.is_set()
    assert wait_until(lambda: not mgr.agent_running)
    assert mgr._agent_lock.acquire(blocking=False)       # free for the next task
    mgr._agent_lock.release()


def test_cancel_while_planning_never_starts_the_run(mgr):
    FakePilot.cancel_while_planning = True
    seen = _record_running(mgr)
    assert call(mgr, "გახსენი მეილი") is True           # handled: it was a task
    pilot = FakePilot.instances[0]
    assert pilot.ran is False and mgr._agent is None
    assert mgr.agent_running is False and seen == []
    assert mgr._agent_lock.acquire(blocking=False)
    mgr._agent_lock.release()


def test_cancel_agent_is_a_no_op_when_idle(mgr):
    mgr.cancel_agent()
    assert not agent_executor.cancel_event.is_set()


# ── P2: mode switch + „დავიბენი“ routing ─────────────────────────────────────

def test_set_agent_mode_emits_for_panel_and_tray(mgr, monkeypatch):
    monkeypatch.setattr(cfg, "_write_env", lambda k, v: None)
    seen = []
    mgr.sig_agent_mode.connect(seen.append, _Qt.ConnectionType.DirectConnection)
    mgr.set_agent_mode(False)
    assert cfg.agent_mode == "guide" and seen == [False]
    mgr.set_agent_mode(True)
    assert cfg.agent_mode == "do" and seen == [False, True]


def test_spoken_lost_requests_a_replan(mgr):
    asked = []
    FakePilot.request_replan = lambda self: asked.append(1)
    assert call(mgr, "გახსენი მეილი") is True
    assert wait_until(lambda: mgr._agent is not None)
    assert call(mgr, "დავიბენი") is True
    assert asked == [1]
    mgr.agent_request_replan()                    # the panel button
    assert asked == [1, 1]


# ── island × (dismiss) ───────────────────────────────────────────────────────

def test_island_close_drops_a_recording_in_progress(mgr):
    stopped = []
    mgr._listener.stop_recording = lambda: stopped.append(1) or b""
    mgr._state = cm.AppState.LISTENING
    mgr._end_claimed = False
    mgr.dismiss_from_island()
    assert stopped == [1] and mgr._state == cm.AppState.IDLE


def test_island_close_stops_a_running_autopilot(mgr):
    assert call(mgr, "გახსენი მეილი") is True
    assert wait_until(lambda: mgr._agent is not None)
    mgr.dismiss_from_island()
    assert agent_executor.cancel_event.is_set()
    assert wait_until(lambda: not mgr.agent_running)


def test_island_close_stops_an_answer(mgr):
    mgr.dismiss_from_island()                     # idle / speaking: just stop()
    assert mgr._cancel_flag is True


def test_taxi_request_runs_the_routine_without_planning(mgr, monkeypatch):
    import agent.routines as rt
    monkeypatch.setattr(cfg, "agent_dry_run", False)
    calls, release = [], threading.Event()

    def fake_run(name, ui, cancel_event, flight_scale=1.0):
        calls.append(name)
        while not release.is_set() and not cancel_event.is_set():
            time.sleep(0.01)
        return rt.RoutineResult(True, "done")

    monkeypatch.setattr(rt, "run_routine", fake_run)
    assert call(mgr, "ტაქსი გამომიძახე") is True
    assert FakePilot.instances == []                     # no planner call
    assert wait_until(lambda: calls == ["call_taxi"])
    assert mgr.agent_running
    assert call(mgr, "გაჩერდი") is True                  # stop words reach it
    assert wait_until(lambda: mgr._agent is None)
    assert mgr._agent_lock.acquire(blocking=False)
    mgr._agent_lock.release()


def test_planner_chosen_routine_runs_it(mgr, monkeypatch):
    import agent.routines as rt
    monkeypatch.setattr(cfg, "agent_dry_run", False)
    calls = []
    monkeypatch.setattr(rt, "run_routine",
                        lambda name, *a, **k: calls.append(name) or rt.RoutineResult(True, "done"))

    async def plan_routine(self, text):
        return PlanResult(is_task=True, routine="call_taxi")
    monkeypatch.setattr(FakePilot, "plan_request", plan_routine)
    assert call(mgr, "მანქანა მჭირდება ექიმთან") is True
    assert wait_until(lambda: calls == ["call_taxi"])
    assert wait_until(lambda: mgr._agent is None)


def test_tray_edit_mode_records_the_chosen_step(mgr, monkeypatch):
    import agent.routines as rt
    monkeypatch.setattr(cfg, "agent_dry_run", False)
    steps = []
    monkeypatch.setattr(rt, "record_routine",
                        lambda name, ui, cancel_event, step, *a: steps.append((name, step))
                        or rt.RoutineResult(True, "done"))
    mgr.record_routine("call_taxi", 2)                    # from the GUI thread
    assert wait_until(lambda: steps == [("call_taxi", 2)])
    assert wait_until(lambda: mgr._agent is None)
    assert mgr._agent_lock.acquire(blocking=False)
    mgr._agent_lock.release()


def test_show_chrome_runs_in_any_mode_without_planning(mgr, monkeypatch):
    import agent.show_icon as si
    shown = []
    monkeypatch.setattr(si, "show_icon", lambda app, ui, cancel, scale=1.0: shown.append(app) or True)
    monkeypatch.setattr(cfg, "agent_mode", "guide")
    run = lambda text: asyncio.run_coroutine_threadsafe(mgr._show_intercept(text), mgr._loop).result(5)
    assert run("მაჩვენე ხრომი") is True and shown == ["chrome"]
    assert run("მაჩვენე გუგლში როგორ ვეძებო") is False     # normal flow keeps it
    assert FakePilot.instances == []


def test_show_chrome_waits_while_the_autopilot_runs(mgr, monkeypatch):
    import agent.show_icon as si
    shown = []
    monkeypatch.setattr(si, "show_icon", lambda *a, **k: shown.append(a) or True)
    assert call(mgr, "გახსენი მეილი") is True
    assert wait_until(lambda: mgr._agent is not None)
    run = asyncio.run_coroutine_threadsafe(mgr._show_intercept("მაჩვენე ხრომი"), mgr._loop).result(5)
    assert run is False and shown == []


class _WhichLLM:
    def __init__(self, reply):
        self.reply, self.calls = reply, 0

    async def stream_response(self, **kw):
        self.calls += 1
        yield self.reply


def _which_setup(mgr, monkeypatch, reply):
    import agent.show_icon as si
    icons = [si.Icon("Google Chrome", (1155, 1020, 1210, 1080), "taskbar")]
    monkeypatch.setattr(si.finder, "icons", lambda: icons)
    llm = _WhichLLM(reply)
    monkeypatch.setattr(mgr, "_get_llm", lambda: llm)
    said, rings = [], []

    async def reply_local(msg):
        said.append(msg)
    monkeypatch.setattr(mgr, "_reply_local", reply_local)
    from PyQt6.QtCore import Qt
    mgr.sig_circle_around.connect(lambda x, y, r, hold: rings.append((x, y, r, hold)),
                                  Qt.ConnectionType.DirectConnection)   # no Qt loop in tests
    run = lambda text: asyncio.run_coroutine_threadsafe(mgr._which_intercept(text), mgr._loop).result(5)
    return run, llm, said, rings


def test_which_app_circles_the_icon_and_explains_it(mgr, monkeypatch):
    run, llm, said, rings = _which_setup(
        mgr, monkeypatch, '{"asked_app": true, "index": 1, "explain": "აი, ეს არის Google Chrome."}')
    assert run("რომელია ხრომი?") is True
    assert said == ["აი, ეს არის Google Chrome."]
    assert wait_until(lambda: len(rings) == 1)
    x, y, r, hold = rings[0]
    cx, cy = agent_executor.to_logical(1182.5, 1050)
    assert (x, y) == (pytest.approx(cx), pytest.approx(cy)) and r > 20 and hold == mgr.WHICH_HOLD_S
    assert FakePilot.instances == []


def test_which_app_leaves_other_questions_to_the_normal_flow(mgr, monkeypatch):
    run, llm, said, rings = _which_setup(
        mgr, monkeypatch, '{"asked_app": false, "index": null, "explain": ""}')
    assert run("სად არის თბილისი?") is False             # model: not about an app
    assert run("რომელი საათია?") is False                 # no cue word: no model call
    assert llm.calls == 1 and said == [] and rings == []


def test_which_app_says_when_it_is_not_on_screen(mgr, monkeypatch):
    run, _llm, said, rings = _which_setup(
        mgr, monkeypatch, '{"asked_app": true, "index": null, "explain": "ვორდი ეკრანზე ვერ ვიპოვე."}')
    assert run("სად არის ვორდი?") is True
    assert said == ["ვორდი ეკრანზე ვერ ვიპოვე."] and rings == []


def test_which_app_still_circles_when_the_model_is_down(mgr, monkeypatch):
    run, llm, said, rings = _which_setup(mgr, monkeypatch, "")

    async def down(**kw):
        raise ConnectionError("All connection attempts failed")
        yield ""                                          # an async generator
    llm.stream_response = down
    assert run("ბაბუ სად არის ხრომ") is True
    assert wait_until(lambda: len(rings) == 1)            # circled from the code match
    assert said == ["აი, ეს არის Google Chrome."]

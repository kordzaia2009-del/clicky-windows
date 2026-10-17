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

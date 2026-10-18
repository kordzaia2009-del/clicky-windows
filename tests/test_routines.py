"""agent/routines.py: the code-only taxi routine and its edit mode. Win32
input, the mouse/keyboard state and Edge are faked — no window opens and no
real input is sent. The routine file goes to a temp folder."""

import os
import threading

import pytest

import agent.executor as agent_executor
import agent.routines as rt
from agent.planner import PlanParseError, plan_from_dict

HWND = 100
SCALE = 1.25
VIEWPORT = (0, 120, 1920, 1080)
BAR = (100, 50, 900, 80)


class FakeWin:
    def __init__(self, under=(1234, rt.EDGE_CLASS, "Glide - Microsoft Edge")):
        self.pos = (10, 10)
        self.events = []
        self.under = under

    def ensure_dpi_awareness(self):
        pass

    def cursor_pos(self):
        return self.pos

    def set_cursor(self, x, y):
        self.pos = (x, y)
        return True

    def click(self, count=1):
        self.events.append(("click", self.pos))

    def press_keys(self, vks):
        self.events.append(("keys", tuple(vks)))

    def type_unicode(self, text):
        self.events.append(("type", text))

    def window_at(self, x, y):
        return self.under


class FakeEdge:
    def __init__(self, win):
        self.win = win
        self.launched = False
        self.maximized = []

    def windows(self):
        return [HWND] if self.launched else []

    def launch(self):
        self.launched = True

    def maximize(self, hwnd):
        self.maximized.append(hwnd)

    def rect(self, hwnd):
        return (-8, -8, 1928, 1088)

    def dpi_scale(self, hwnd):
        return SCALE

    def viewport(self, hwnd):
        return VIEWPORT

    def address_bar(self, hwnd, below):
        assert below == VIEWPORT[1]
        return BAR

    def title(self, hwnd):
        entered = ("keys", (0x0D,)) in self.win.events
        return "Glide" if entered else "New tab"


class FakeKeys:
    """Scripted mouse/keyboard: ("click", (px, py)) or ("enter", None). Each
    item is up, then down, then released again on the next queries."""

    def __init__(self, win, script):
        self.win, self.script, self.i, self.n = win, list(script), 0, 0

    def down(self, vk):
        if self.i >= len(self.script):
            return False
        kind, arg = self.script[self.i]
        if vk != (rt.VK_LBUTTON if kind == "click" else rt.VK_RETURN):
            return False
        self.n += 1
        if self.n == 1:
            return False
        if self.n == 2:
            if kind == "click":
                self.win.pos = arg
            return True
        self.i, self.n = self.i + 1, 0
        return False


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    win = FakeWin()
    ed = FakeEdge(win)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(agent_executor, "win", win)
    monkeypatch.setattr(rt, "edge", ed)
    monkeypatch.setattr(rt, "keys", FakeKeys(win, []))
    for name in ("FLIGHT_SPEED", "HOLD_S", "TYPE_INTERVAL_S", "MAXIMIZE_SETTLE_S", "PAGE_SETTLE_S"):
        monkeypatch.setattr(rt, name, 0.0)
    monkeypatch.setattr(rt, "TAXI_CLICKS", tuple((x, y, 0.0, say) for x, y, _p, say in rt.TAXI_CLICKS))
    monkeypatch.setattr(rt, "DEFAULT_PAUSE_S", 0.0)
    monkeypatch.setattr(rt, "RECORD_IDLE_TIMEOUT_S", 0.3)
    return win, ed


def page_point(x, y):
    return VIEWPORT[0] + x, VIEWPORT[1] + y          # screen px from the page corner


def clicks_of(win):
    return [e[1] for e in win.events if e[0] == "click"]


def bar_center():
    return (BAR[0] + BAR[2]) // 2, (BAR[1] + BAR[3]) // 2


def test_taxi_words_match_without_a_model():
    assert rt.match_routine("ტაქსი გამომიძახე") == "call_taxi"
    assert rt.match_routine("გამომიძახე ტაქსი, გთხოვ") == "call_taxi"
    assert rt.match_routine("რა ამინდია დღეს?") is None


def test_misheard_taxi_still_counts():
    # Google STT drops the ტ: „გამომიძახი აქცი“ was heard for „გამომიძახე ტაქსი“
    assert rt.match_routine("გამომიძახი აქცი") == "call_taxi"
    assert rt.match_routine("აქსი გამომიძახე") == "call_taxi"
    assert rt.match_routine("გამოიძახეთ ტაქ") == "call_taxi"          # cut off by STT
    assert rt.match_routine("მაქსიმუმ რამდენი ღირს?") is None
    assert rt.match_routine("აქცია ვიყიდო") is None


def test_call_taxi_runs_every_step_in_order(fakes):
    win, ed = fakes
    said, speeds = [], []
    ui = rt.RoutineUI(say=said.append, set_speed=speeds.append)
    result = rt.run_routine("call_taxi", ui, threading.Event())
    assert result.ok and result.status == "done", result.message
    assert ed.launched and ed.maximized == [HWND]
    assert clicks_of(win) == [bar_center()] + [page_point(x, y) for x, y, _p, _s in rt.TAXI_CLICKS]
    typed = "".join(e[1] for e in win.events if e[0] == "type")
    assert typed == rt.TAXI_URL
    keys = [e[1] for e in win.events if e[0] == "keys"]
    assert keys == [(0x11, 0x4C), (0x2E,), (0x0D,)]      # Ctrl+L, Delete, Enter
    assert said == ["კარგი"] + [say for *_xyp, say in rt.TAXI_CLICKS]   # „კარგი“, then each step's line
    assert speeds == [0.0, 1.0]                            # fast flights, then back


def test_run_follows_the_saved_file(fakes):
    win, _ed = fakes
    rt.save_routine("call_taxi", {"url": "https://example.com/",
                                  "steps": [{"x": 5, "y": 6, "pause_s": 0}]})
    assert rt.run_routine("call_taxi", rt.RoutineUI(), threading.Event()).ok
    assert clicks_of(win) == [bar_center(), page_point(5, 6)]
    assert "".join(e[1] for e in win.events if e[0] == "type") == "https://example.com/"


def test_a_broken_file_falls_back_to_the_defaults(fakes):
    path = rt.routine_file("call_taxi")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"url": "https://x/", "steps": [{"x": "left"}]}')
    assert rt.load_routine("call_taxi")["steps"] == rt._default_routine()["steps"]


def test_a_failure_says_nothing_but_okay(fakes):
    win, _ed = fakes
    win.under = (os.getpid(), "QWidget", "Clicky")
    said = []
    result = rt.run_routine("call_taxi", rt.RoutineUI(say=said.append), threading.Event())
    assert result.status == "failed"
    assert not any(e[0] == "click" for e in win.events)   # never clicks through itself
    assert said == ["კარგი"]


def test_cancel_before_start_opens_nothing(fakes):
    win, ed = fakes
    stop = threading.Event()
    stop.set()
    result = rt.run_routine("call_taxi", rt.RoutineUI(), stop)
    assert result.status == "cancelled"
    assert not ed.launched and win.events == []


def test_edit_mode_records_every_click_until_enter(fakes, monkeypatch):
    win, _ed = fakes
    monkeypatch.setattr(rt, "keys", FakeKeys(win, [
        ("click", page_point(100, 200)),
        ("click", (5, 5)),                       # on the toolbar, not the page: ignored
        ("click", page_point(300, 400)),
        ("enter", None),
    ]))
    said = []
    result = rt.record_routine("call_taxi", rt.RoutineUI(say=said.append), threading.Event(), step=0)
    assert result.ok, result.message
    saved = rt.load_routine("call_taxi")
    assert [(s["x"], s["y"]) for s in saved["steps"]] == [(100, 200), (300, 400)]
    assert saved["steps"][-1]["pause_s"] == 0.0
    assert clicks_of(win) == [bar_center()]       # edit mode only clicks the address bar
    assert said == [rt.SAY_RECORD_ALL, rt.SAY_SAVED]


def test_edit_mode_re_records_one_step(fakes, monkeypatch):
    win, _ed = fakes
    before = rt.load_routine("call_taxi")["steps"]
    monkeypatch.setattr(rt, "keys", FakeKeys(win, [("click", page_point(500, 400))]))
    assert rt.record_routine("call_taxi", rt.RoutineUI(), threading.Event(), step=2).ok
    after = rt.load_routine("call_taxi")["steps"]
    assert (after[1]["x"], after[1]["y"]) == (500, 400)
    assert after[0] == before[0] and after[2] == before[2]
    first = before[0]
    assert clicks_of(win) == [bar_center(), page_point(first["x"], first["y"])]   # step 1 replayed


def test_edit_mode_adds_a_step_at_the_end(fakes, monkeypatch):
    win, _ed = fakes
    n = len(rt.load_routine("call_taxi")["steps"])
    monkeypatch.setattr(rt, "keys", FakeKeys(win, [("click", page_point(7, 8))]))
    assert rt.record_routine("call_taxi", rt.RoutineUI(), threading.Event(), step=n + 1).ok
    after = rt.load_routine("call_taxi")["steps"]
    assert len(after) == n + 1 and (after[-1]["x"], after[-1]["y"]) == (7, 8)


def test_edit_mode_saves_nothing_without_a_click(fakes):
    result = rt.record_routine("call_taxi", rt.RoutineUI(), threading.Event(), step=1)
    assert result.status == "failed"
    assert not os.path.exists(rt.routine_file("call_taxi"))


def test_planner_can_pick_a_routine():
    plan = plan_from_dict({"is_task": True, "routine": "call_taxi", "goals": []})
    assert plan.is_task and plan.routine == "call_taxi" and plan.goals == []
    with pytest.raises(PlanParseError):
        plan_from_dict({"is_task": True, "routine": "fly_to_mars", "goals": []})


def test_a_step_without_a_line_stays_quiet(fakes):
    rt.save_routine("call_taxi", {"url": "https://example.com/",
                                  "steps": [{"x": 1, "y": 2, "pause_s": 0},
                                            {"x": 3, "y": 4, "pause_s": 0, "say": "ვირჩევ სტანდარტს"}]})
    said = []
    assert rt.run_routine("call_taxi", rt.RoutineUI(say=said.append), threading.Event()).ok
    assert said == ["კარგი", "ვირჩევ სტანდარტს"]


def test_re_recording_a_step_keeps_its_line(fakes, monkeypatch):
    win, _ed = fakes
    monkeypatch.setattr(rt, "keys", FakeKeys(win, [("click", page_point(500, 400))]))
    before = rt.load_routine("call_taxi")["steps"]
    assert rt.record_routine("call_taxi", rt.RoutineUI(), threading.Event(), step=2).ok
    assert rt.load_routine("call_taxi")["steps"][1]["say"] == before[1]["say"]


def test_each_click_waits_until_its_line_is_spoken(fakes):
    import concurrent.futures
    import time as _time
    win, _ed = fakes
    rt.save_routine("call_taxi", {"url": "https://example.com/",
                                  "steps": [{"x": 1, "y": 2, "pause_s": 0, "say": "ვირჩევ სტანდარტს"}]})
    spoken_at = []

    def say(text):
        fut = concurrent.futures.Future()
        if text == "კარგი":
            fut.set_result(None)
            return fut

        def finish():
            _time.sleep(0.2)
            spoken_at.append(_time.monotonic())
            fut.set_result(None)
        threading.Thread(target=finish, daemon=True).start()
        return fut

    clicked_at = []
    real_click = win.click
    win.click = lambda count=1: (clicked_at.append(_time.monotonic()), real_click(count))
    assert rt.run_routine("call_taxi", rt.RoutineUI(say=say), threading.Event()).ok
    assert spoken_at and clicked_at[-1] >= spoken_at[0]      # the page click came after the line

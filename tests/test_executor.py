"""agent.executor with the Win32 layer replaced by FakeWin — no real mouse,
keyboard or UIA activity happens in these tests."""

import os
import threading
from types import SimpleNamespace

import pytest

import agent.executor as ex


class FakeWin:
    def __init__(self):
        self.calls = []
        self.pos = (10, 10)
        self.window = (12345, "Notepad", "Untitled - Notepad")   # not our pid
        self.password = False
        self.target = None
        self.on_cursor_read = None          # hook to simulate the user/Esc
        self.cursor_lands_at = None         # simulate a DPI mismatch
        self.invoke_result = None

    def ensure_dpi_awareness(self): pass
    def virtual_desktop(self): return (-1920, 0, 3840, 1080)

    def cursor_pos(self):
        if self.on_cursor_read:
            self.on_cursor_read(self)
        return self.pos

    def set_cursor(self, x, y):
        self.calls.append(("set_cursor", x, y))
        self.pos = self.cursor_lands_at or (x, y)
        return True

    def click(self, count=1): self.calls.append(("click", count))
    def wheel(self, clicks): self.calls.append(("wheel", clicks))
    def press_keys(self, vks): self.calls.append(("keys", tuple(vks)))
    def type_unicode(self, text): self.calls.append(("type", text))
    def window_at(self, x, y): return self.window
    def focused_is_password(self): return self.password
    def element_rect(self, element):
        # `rects` = successive answers (the element moves between reads)
        rects = getattr(element, "rects", None)
        if rects:
            return rects.pop(0) if len(rects) > 1 else rects[0]
        return getattr(element, "rect", None)

    def uia_activate(self, element):
        self.calls.append(("uia_activate", element.name))
        return self.invoke_result

    def find_target(self, name):
        return self.target

    def inputs(self):
        return [c for c in self.calls if c[0] != "uia_activate"]


@pytest.fixture
def fake(monkeypatch):
    w = FakeWin()
    monkeypatch.setattr(ex, "win", w)
    monkeypatch.setattr(ex, "HOLD_S", 0.0)
    monkeypatch.setattr(ex, "POLL_S", 0.005)
    monkeypatch.setattr(ex, "RESUME_STILL_S", 0.05)
    monkeypatch.setattr(ex._Run, "flight_seconds", lambda self, lx, ly: 0.0)
    ex.set_dry_run(False)
    ex.reset_cancel()
    yield w
    ex.set_dry_run(False)
    ex.reset_cancel()


class Recorder:
    def __init__(self):
        self.events = []
        self.logs = []

    def callbacks(self, **kw):
        return ex.ExecutorCallbacks(
            point_at=lambda x, y, label: self.events.append(("point_at", round(x), round(y), label)),
            point_hold=lambda on: self.events.append(("hold", on)),
            point_release=lambda: self.events.append(("release",)),
            on_log=self.logs.append,
            on_pause=lambda on: self.events.append(("pause", on)),
            **kw,
        )


# A second monitor to the right of the primary, at 150% scaling
SHOT = SimpleNamespace(physical_left=1920, physical_top=0, physical_width=1920,
                       physical_height=1080, dpi_scale=1.5,
                       logical_left=1280, logical_top=0)


def _uia_target(name="Save", rect=(100, 200, 180, 230), x=140, y=215):
    element = SimpleNamespace(name=name, rect=rect)
    return SimpleNamespace(x=x, y=y, bbox=rect, label=name, source="uia", element=element)


# ── coordinate helpers ───────────────────────────────────────────────────────

def test_norm_to_physical_includes_the_monitor_origin():
    assert ex.norm_to_physical(500, 500, SHOT) == (1920 + 960, 540)
    assert ex.norm_to_physical(0, 1000, SHOT) == (1920, 1080)


def test_to_logical_matches_denorm_convention():
    assert ex.to_logical(1920 + 960, 540, SHOT) == (1280 + 640, 360)


@pytest.mark.parametrize("keys, vks", [
    ("enter", [0x0D]), ("ctrl+a", [0x11, 0x41]), (["alt", "F4"], [0x12, 0x73]),
    ("Ctrl + Shift + Esc", [0x11, 0x10, 0x1B]),
])
def test_parse_keys(keys, vks):
    assert ex.parse_keys(keys) == vks


def test_parse_keys_rejects_unknown():
    with pytest.raises(ValueError):
        ex.parse_keys("ctrl+banana")


# ── click ────────────────────────────────────────────────────────────────────

def test_click_on_coordinates_uses_physical_pixels_and_animates(fake):
    rec = Recorder()
    r = ex.execute({"type": "click", "target": {"x": 500, "y": 500, "label": "OK"},
                    "explanation": "press OK"}, SHOT, rec.callbacks())
    assert r.ok and r.status == "done" and r.method == "mouse"
    assert (r.x, r.y) == (2880, 540)
    assert fake.inputs() == [("set_cursor", 2880, 540), ("click", 1)]
    # pointer: hold, fly to the LOGICAL point, release afterwards
    assert rec.events == [("hold", True), ("point_at", 1920, 360, "OK"), ("release",)]


def test_click_prefers_uia_invoke_for_elements(fake):
    fake.invoke_result = "uia-invoke"
    r = ex.execute({"type": "click", "target": _uia_target()}, None, Recorder().callbacks())
    assert r.status == "done" and r.method == "uia-invoke"
    assert fake.inputs() == []                      # no mouse at all


def test_click_falls_back_to_mouse_when_uia_has_no_pattern(fake):
    fake.invoke_result = None
    r = ex.execute({"type": "click", "target": _uia_target()}, None, Recorder().callbacks())
    assert r.method == "mouse"
    # element re-read right before clicking: centre of its rect
    assert fake.inputs() == [("set_cursor", 140, 215), ("click", 1)]


def test_element_that_moved_is_reshown_then_clicked_at_its_new_place(fake):
    t = _uia_target(rect=(100, 200, 180, 230))
    # first read: where it was; later reads: the window was dragged during the hold
    t.element.rects = [(100, 200, 180, 230), (500, 600, 580, 630)]
    rec = Recorder()
    r = ex.execute({"type": "click", "target": t}, None, rec.callbacks())
    assert r.status == "done" and (r.x, r.y) == (540, 615)
    assert [e[0] for e in rec.events].count("point_at") == 2   # pointer flew again
    assert ("set_cursor", 540, 615) in fake.calls
    assert any(e["event"] == "target_moved" for e in rec.logs)


def test_element_moved_off_screen_is_not_clicked(fake):
    # Seen in a real dry run: after a long pause the element's centre was
    # below the bottom of the screen.
    t = _uia_target()
    t.element.rect = (300, 5000, 1500, 5700)
    r = ex.execute({"type": "click", "target": t}, None, Recorder().callbacks())
    assert r.status == "failed" and "no longer visible" in r.message
    assert fake.inputs() == []


def test_partly_visible_element_is_clicked_in_its_visible_part(fake):
    t = _uia_target(rect=(100, 900, 300, 1300), x=200, y=1100)   # bottom past 1080
    t.element.rect = (100, 900, 300, 1300)
    r = ex.execute({"type": "click", "target": t}, None, Recorder().callbacks())
    assert r.status == "done" and (r.x, r.y) == (200, 990)


def test_click_by_name_resolves_through_find_target(fake):
    fake.target = _uia_target("Text Editor", rect=(0, 0, 400, 300), x=200, y=150)
    r = ex.execute({"type": "click", "target": {"name": "Text Editor"}}, None, Recorder().callbacks())
    assert r.ok and r.target == "Text Editor"


def test_click_by_name_not_found_fails_without_input(fake):
    r = ex.execute({"type": "click", "target": {"name": "Nope"}}, None, Recorder().callbacks())
    assert r.status == "failed" and "could not find" in r.message
    assert fake.inputs() == []


def test_double_click_never_uses_invoke(fake):
    fake.invoke_result = "uia-invoke"
    r = ex.execute({"type": "double_click", "target": _uia_target()}, None, Recorder().callbacks())
    assert r.method == "mouse" and ("click", 2) in fake.inputs()
    assert not any(c[0] == "uia_activate" for c in fake.calls)


def test_click_refused_when_our_own_window_is_under_the_point(fake):
    fake.window = (os.getpid(), "Qt6QWindowToolSaveBits", "Clicky")
    r = ex.execute({"type": "click", "target": {"x": 10, "y": 10, "space": "physical"}},
                   None, Recorder().callbacks())
    assert r.status == "refused" and "own window" in r.message
    assert fake.inputs() == []


def test_coordinate_click_refused_when_another_window_is_on_top(fake):
    # Seen in a real dry run: the user brought Chrome over Notepad mid-run.
    fake.window = (777, "Chrome_WidgetWin_1", "Claude - Google Chrome")
    r = ex.execute({"type": "click", "target": {"x": 10, "y": 10, "space": "physical"},
                    "expect_window": "Notepad"}, None, Recorder().callbacks())
    assert r.status == "refused" and "Notepad" in r.message
    assert fake.inputs() == []


def test_coordinate_click_with_matching_window_goes_ahead(fake):
    r = ex.execute({"type": "click", "target": {"x": 10, "y": 10, "space": "physical"},
                    "expect_window": "notepad"}, None, Recorder().callbacks())
    assert r.status == "done"


def test_element_click_refused_when_another_app_covers_it(fake):
    t = _uia_target()
    t.element.ProcessId = 999                 # Notepad's pid; Chrome (777) is on top
    fake.window = (777, "Chrome_WidgetWin_1", "Google Chrome")
    r = ex.execute({"type": "click", "target": t}, None, Recorder().callbacks())
    assert r.status == "refused" and "covered by another window" in r.message
    assert fake.inputs() == []


def test_element_click_in_its_own_window_goes_ahead(fake):
    t = _uia_target()
    t.element.ProcessId = 12345               # same pid as FakeWin.window
    r = ex.execute({"type": "click", "target": t}, None, Recorder().callbacks())
    assert r.status == "done" and r.method == "mouse"


def test_click_fails_if_the_cursor_lands_elsewhere(fake):
    fake.cursor_lands_at = (5, 5)
    r = ex.execute({"type": "click", "target": {"x": 300, "y": 300, "space": "physical"}},
                   None, Recorder().callbacks())
    assert r.status == "failed" and "DPI" in r.message
    assert ("click", 1) not in fake.calls


def test_click_outside_the_desktop_fails(fake):
    r = ex.execute({"type": "click", "target": {"x": 99999, "y": 5, "space": "physical"}},
                   None, Recorder().callbacks())
    assert r.status == "failed" and "outside the screen" in r.message


def test_normalized_target_needs_a_screenshot(fake):
    r = ex.execute({"type": "click", "target": {"x": 10, "y": 10}}, None, Recorder().callbacks())
    assert r.status == "failed" and "screenshot" in r.message


# ── type ─────────────────────────────────────────────────────────────────────

def test_type_georgian_in_chunks(fake):
    text = "გამარჯობა, როგორ ხართ?"
    r = ex.execute({"type": "type", "text": text}, None, Recorder().callbacks())
    assert r.status == "done" and r.method == "unicode"
    typed = [c[1] for c in fake.calls if c[0] == "type"]
    assert "".join(typed) == text and all(len(t) <= ex.TYPE_CHUNK for t in typed)


def test_type_refuses_private_text(fake):
    r = ex.execute({"type": "type", "text": "hunter2", "private": True}, None, Recorder().callbacks())
    assert r.status == "refused" and fake.inputs() == []


def test_type_refuses_password_fields(fake):
    fake.password = True
    r = ex.execute({"type": "type", "text": "abc"}, None, Recorder().callbacks())
    assert r.status == "refused" and "password" in r.message and fake.inputs() == []


# ── key / scroll / wait ──────────────────────────────────────────────────────

def test_key_chord(fake):
    r = ex.execute({"type": "key", "keys": "ctrl+a"}, None, Recorder().callbacks())
    assert r.ok and fake.inputs() == [("keys", (0x11, 0x41))]


def test_our_own_esc_does_not_latch_cancel(fake):
    # The real keyboard hook sees injected keys too; simulate it.
    orig = fake.press_keys
    fake.press_keys = lambda vks: (orig(vks), ex.cancel_event.set() if 0x1B in vks else None)
    r = ex.execute({"type": "key", "keys": "esc"}, None, Recorder().callbacks())
    assert r.ok and not ex.cancel_event.is_set()


def test_scroll_at_target(fake):
    r = ex.execute({"type": "scroll", "amount": -3, "target": {"x": 50, "y": 60, "space": "physical"}},
                   None, Recorder().callbacks())
    assert r.ok
    assert fake.inputs() == [("set_cursor", 50, 60)] + [("wheel", -1)] * 3


def test_unknown_action_type_fails(fake):
    assert ex.execute({"type": "teleport"}).status == "failed"


# ── dry run, cancel, pause ───────────────────────────────────────────────────

def test_dry_run_animates_and_logs_but_sends_nothing(fake):
    ex.set_dry_run(True)
    rec = Recorder()
    actions = [
        {"type": "click", "target": {"x": 500, "y": 500}, "explanation": "open it"},
        {"type": "double_click", "target": {"x": 10, "y": 10, "space": "physical"}},
        {"type": "type", "text": "გამარჯობა"},
        {"type": "key", "keys": "enter"},
        {"type": "scroll", "amount": 2},
        {"type": "wait", "seconds": 0.01},
    ]
    results = [ex.execute(a, SHOT, rec.callbacks()) for a in actions]
    assert all(r.status == "dry_run" and r.ok and r.dry_run for r in results)
    assert fake.inputs() == []
    assert ("point_at", 1920, 360, "") in rec.events
    first = next(e for e in rec.logs if e["action"] == "click" and e["event"] == "dry_run")
    assert first["explanation"] == "open it" and first["x"] == 2880 and "time" in first


def test_cancel_before_the_action(fake):
    ex.cancel_event.set()
    r = ex.execute({"type": "type", "text": "abc"}, None, Recorder().callbacks())
    assert r.status == "cancelled" and fake.inputs() == []


def test_cancel_during_the_hold_releases_the_pointer(fake, monkeypatch):
    monkeypatch.setattr(ex, "HOLD_S", 0.5)
    reads = {"n": 0}

    def esc_on_third_read(w):
        reads["n"] += 1
        if reads["n"] == 3:
            ex.cancel_event.set()
    fake.on_cursor_read = esc_on_third_read
    rec = Recorder()
    r = ex.execute({"type": "click", "target": {"x": 5, "y": 5, "space": "physical"}},
                   None, rec.callbacks())
    assert r.status == "cancelled" and fake.inputs() == []
    assert rec.events[-1] == ("release",)


def test_user_moving_the_mouse_pauses_then_resumes(fake, monkeypatch):
    monkeypatch.setattr(ex, "HOLD_S", 0.05)
    reads = {"n": 0}

    def user_moves_once(w):
        reads["n"] += 1
        if reads["n"] == 2:
            w.pos = (w.pos[0] + 300, w.pos[1])    # a real hand on the mouse
    fake.on_cursor_read = user_moves_once
    rec = Recorder()
    r = ex.execute({"type": "click", "target": {"x": 5, "y": 5, "space": "physical"}},
                   None, rec.callbacks())
    assert r.status == "done"                      # paused, not cancelled
    assert ("pause", True) in rec.events and ("pause", False) in rec.events
    assert [e["event"] for e in rec.logs].count("paused") == 1


def test_stop_hotkey_sets_the_shared_event():
    from hotkey import StopHotkey
    event, stopped = threading.Event(), []
    StopHotkey(on_stop=lambda: stopped.append(1), cancel_event=event)._fire()
    assert event.is_set() and stopped == [1]

"""agent/show_icon.py: „მაჩვენე ხრომი“ — glide to Chrome's icon and circle it.
UIA and Win32 input are faked; nothing is clicked and no window opens."""

import threading

import pytest

import agent.executor as agent_executor
import agent.routines as rt
import agent.show_icon as si

ICON = (900, 1040, 940, 1080)          # a taskbar button, physical px


class FakeWin:
    def __init__(self):
        self.pos = (10, 10)
        self.clicks = 0

    def ensure_dpi_awareness(self):
        pass

    def cursor_pos(self):
        return self.pos

    def set_cursor(self, x, y):
        self.pos = (x, y)
        return True

    def click(self, count=1):
        self.clicks += 1


class FakeFinder:
    def __init__(self, found):
        self.found = found
        self.asked = []

    def find(self, names):
        self.asked.append(names)
        return self.found


@pytest.fixture
def win(monkeypatch):
    w = FakeWin()
    monkeypatch.setattr(agent_executor, "win", w)
    monkeypatch.setattr(rt, "FLIGHT_SPEED", 0.0)
    monkeypatch.setattr(rt, "HOLD_S", 0.0)
    monkeypatch.setattr(si, "HOLD_S", 0.0)
    return w


@pytest.mark.parametrize("text", [
    "მაჩვენე გუგლი", "მაჩვენე ხრომი", "მაჩვენე გუგლ ხრომი", "მაჩვენე გუგლი ხრომი",
    "მაჩვენეთ ხრომი.", "  მაჩვენე Google Chrome ", "მაჩვენე ქრომი",
    # what Google STT really wrote (clicky.log, 2026-10-18)
    "მაჩვენი google chrom", "შემომიხაზე მაჩვენე სად არის google chromi", "მაჩვენე ხრომ",
])
def test_show_chrome_commands_match(text):
    assert si.match_show(text) == "chrome"


@pytest.mark.parametrize("text", [
    "მაჩვენე გუგლში როგორ ვეძებო",       # a question: the normal flow answers it
    "გახსენი ხრომი",                      # "open", not "show" — the autopilot's job
    "მაჩვენე ამინდი",
    "ხრომი",
    "მაჩვენე",
    "ტაქსი გამომიძახე",
    "google chrome",                      # no "show" word
    "გახსენი google chrome",
])
def test_other_requests_are_left_alone(text):
    assert si.match_show(text) is None


def test_glides_to_the_icon_and_circles_it_without_clicking(win, monkeypatch):
    finder = FakeFinder((ICON, "taskbar"))
    monkeypatch.setattr(si, "finder", finder)
    circles, points, released = [], [], []
    ui = rt.RoutineUI(point_at=lambda x, y, label: points.append(label),
                      point_release=lambda: released.append(True),
                      circle=lambda x, y, r, ttl: circles.append((x, y, r, ttl)))
    assert si.show_icon("chrome", ui, threading.Event()) is True
    center = ((ICON[0] + ICON[2]) // 2, (ICON[1] + ICON[3]) // 2)
    assert win.pos == center                     # the real mouse is on the icon
    assert win.clicks == 0                       # showing never clicks
    assert points == ["Google Chrome"] and released == [True]
    (cx, cy, radius, ttl), = circles
    lx, ly = agent_executor.to_logical(*center)
    assert (cx, cy) == (pytest.approx(lx), pytest.approx(ly))
    assert radius > (ICON[2] - ICON[0]) / 2 / 3     # wider than the icon at any DPI
    assert ttl == si.CIRCLE_TTL_S
    assert finder.asked == [("Google Chrome",)]


def test_says_so_when_chrome_is_not_on_screen(win, monkeypatch):
    monkeypatch.setattr(si, "finder", FakeFinder(None))
    said, circles = [], []
    ui = rt.RoutineUI(say=said.append, circle=lambda *a: circles.append(a))
    assert si.show_icon("chrome", ui, threading.Event()) is False
    assert said == ["Google Chrome ვერ ვიპოვე ეკრანზე."]
    assert circles == [] and win.pos == (10, 10)


def test_stop_cuts_it_short(win, monkeypatch):
    monkeypatch.setattr(si, "finder", FakeFinder((ICON, "taskbar")))
    stop = threading.Event()
    stop.set()
    circles = []
    assert si.show_icon("chrome", rt.RoutineUI(circle=lambda *a: circles.append(a)), stop) is False
    assert circles == []

"""
Routines — fixed, code-only tasks. Once a request is matched to a routine
(by keyword in match_routine(), or by the planner returning "routine"), no
model is called again: every step below is plain Win32 / UIA code, with the
grape pointer and the real mouse gliding together to each target.

    match_routine(text) -> "call_taxi" | None
    run_routine(name, ui, cancel_event, flight_scale=1.0) -> RoutineResult
    record_routine(name, ui, cancel_event, step=0) -> RoutineResult

"call_taxi" („ტაქსი გამომიძახე“):
    1. open a new Microsoft Edge window, maximized
    2. glide to the address bar and click it
    3. type the URL (animated), Enter, wait for the page
    4. click the steps in order

The URL and the steps live in routine_file("call_taxi")
(%LOCALAPPDATA%\\Clicky\\routines\\call_taxi.json), editable by hand:

    {"url": "https://...",
     "steps": [{"x": 973, "y": 763, "pause_s": 1.5, "say": "ვირჩევ სტანდარტს"}, ...]}

x / y are SCREEN pixels from the top-left corner of the web page (just under
the address bar); pause_s is the wait after that click; say (optional) is
spoken and shown in the mascot's bubble just before the click. Edit mode
(record_routine) fills them from the person's own clicks: step=0 records
every step (Enter finishes), step=k re-records only step k (the steps before
it are clicked automatically first). Esc cancels without saving.

Threading: run_routine() blocks; run it off the GUI and asyncio threads. The
`ui` callbacks must be Qt signal emits. All input goes through
agent.executor.win and all Edge window access through the module-level
`edge` object, so tests can replace both and nothing real happens.
"""

from __future__ import annotations

import ctypes
import json
import logging
import math
import os
import re
import subprocess
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

log = logging.getLogger("clicky.routines")

# What the planner may answer with: name -> when to pick it (English, for the prompt).
ROUTINES = {
    "call_taxi": "the person wants a taxi / a car to come and pick them up",
}

# ── The taxi routine's data ──────────────────────────────────────────────────

TAXI_URL = "https://glide-to-you.lovable.app/"
# (x, y, pause after the click in seconds, what to say first) — used until the file exists
TAXI_CLICKS: Tuple[Tuple[int, int, float, str], ...] = (
    (973, 763, 1.5, "ვიძახებ ტაქსს."),
    (967, 299, 1.5, "ვირჩევ სტანდარტს, როგორც ყოველთვის."),
    (1024, 886, 0.0, "ვადასტურებ შეკვეთას."),
)
DEFAULT_PAUSE_S = 1.5         # between actions
MAX_PAUSE_S = 30.0


def routine_file(name: str) -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Clicky", "routines", f"{name}.json")


def _default_routine() -> dict:
    return {"url": TAXI_URL,
            "steps": [{"x": x, "y": y, "pause_s": p, "say": say}
                      for x, y, p, say in TAXI_CLICKS]}


def parse_routine(data) -> dict:
    """Validate a routine file's contents. Raises ValueError."""
    if not isinstance(data, dict):
        raise ValueError("the file must hold one JSON object")
    url = data.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError('"url" must start with http:// or https://')
    raw = data.get("steps")
    if not isinstance(raw, list):
        raise ValueError('"steps" must be a list')
    steps = []
    for i, s in enumerate(raw, start=1):
        if not isinstance(s, dict):
            raise ValueError(f"step {i} must be an object")
        try:
            x, y = int(s["x"]), int(s["y"])
            pause = float(s.get("pause_s", DEFAULT_PAUSE_S))
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"step {i} needs whole-number x and y") from None
        if x < 0 or y < 0:
            raise ValueError(f"step {i}: x and y must not be negative")
        say = s.get("say", "")
        if not isinstance(say, str):
            raise ValueError(f'step {i}: "say" must be text')
        steps.append({"x": x, "y": y, "pause_s": max(0.0, min(pause, MAX_PAUSE_S)),
                      "say": say.strip()})
    return {"url": url, "steps": steps}


def load_routine(name: str) -> dict:
    """The routine's URL and steps: the file if it is valid, else the defaults."""
    path = routine_file(name)
    try:
        with open(path, encoding="utf-8") as f:
            return parse_routine(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("routine file %s is not usable (%s) — using the defaults", path, e)
    return _default_routine()


def save_routine(name: str, routine: dict) -> str:
    routine = parse_routine(routine)
    path = routine_file(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(routine, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    log.info("routine %s saved: %d step(s) -> %s", name, len(routine["steps"]), path)
    return path


def ensure_routine_file(name: str) -> str:
    """Write the defaults if there is no file yet (for opening it to edit)."""
    path = routine_file(name)
    if not os.path.exists(path):
        save_routine(name, load_routine(name))
    return path

# ── Speed ────────────────────────────────────────────────────────────────────

FLIGHT_SPEED = 0.22         # overlay flight time multiplier (1.0 = teaching pace)
HOLD_S = 0.08               # rest on the target before the click
GLIDE_TICK_S = 0.008        # real-cursor step (~120 Hz)
TYPE_INTERVAL_S = 0.012     # between URL characters
EDGE_START_TIMEOUT_S = 10.0
MAXIMIZE_SETTLE_S = 0.25
PAGE_LOAD_TIMEOUT_S = 12.0
SPEECH_WAIT_MAX_S = 2.5     # a step's line may hold its click this long at most (it keeps playing after)
PAGE_SETTLE_S = 1.5         # after the title changes: let the page render
UIA_SEARCH_LIMIT_S = 1.5

RECORD_IDLE_TIMEOUT_S = 180.0   # no click for this long ends edit mode (and saves)

SAY_START = "კარგი"             # the only thing a run says
SAY_RECORD_ALL = "ჩაწერის რეჟიმი. დააჭირეთ ღილაკებს რიგრიგობით. ბოლოს დააჭირეთ Enter-ს."
SAY_RECORD_ONE = "დააჭირეთ ადგილს, სადაც ნაბიჯი {n} უნდა დააჭიროს."
SAY_SAVED = "შენახულია."

# Google STT often drops the ტ: „გამომიძახი აქცი“ — აქსი / აქცი count as ტაქსი
# (as whole words, so „მაქსიმუმ“ or „აქცია“ do not). A cut-off „ტაქ“
# („გამოიძახეთ ტაქ“) counts too.
_TAXI_RE = re.compile(r"ტაქს|ტაქც|\bტაქ\b|\bაქ[სც]ი\b|taxi|такси|bolt|ბოლტ|yandex|იანდექს",
                      re.IGNORECASE)


def match_routine(text: str) -> Optional[str]:
    """The routine a request plainly asks for — no model call."""
    if _TAXI_RE.search(text or ""):
        return "call_taxi"
    return None


def _noop(*_a, **_k):
    return None


def _done(fut) -> bool:
    try:
        return bool(fut.done())
    except Exception:
        return True


@dataclass
class RoutineUI:
    """How a routine talks to the UI (Qt signal emits in the app)."""
    point_at: Callable[[float, float, str], None] = _noop   # logical px
    point_hold: Callable[[bool], None] = _noop
    point_release: Callable[[], None] = _noop
    set_speed: Callable[[float], None] = _noop              # overlay flight multiplier
    say: Callable[[str], object] = _noop                    # may return a Future: done = spoken
    on_action: Callable[[str, str, str], None] = _noop      # type, label, say_ka
    circle: Callable[[float, float, float, float], None] = _noop   # logical x, y, radius, ttl s


@dataclass
class RoutineResult:
    ok: bool
    status: str             # done | cancelled | failed
    message: str = ""


class _Cancelled(Exception):
    pass


class RoutineError(Exception):
    pass


# ── Edge windows (Win32 + UIA) ───────────────────────────────────────────────

SW_MAXIMIZE = 3
EDGE_CLASS = "Chrome_WidgetWin_1"
RENDER_CLASS = "Chrome_RenderWidgetHostHWND"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

Rect = Tuple[int, int, int, int]        # physical px: left, top, right, bottom


class _Edge:
    """The Edge window calls the taxi routine needs."""

    @property
    def _u(self):
        return ctypes.windll.user32

    def _class(self, hwnd) -> str:
        buf = ctypes.create_unicode_buffer(256)
        self._u.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def title(self, hwnd) -> str:
        buf = ctypes.create_unicode_buffer(512)
        self._u.GetWindowTextW(hwnd, buf, 512)
        return buf.value

    def _exe(self, hwnd) -> str:
        pid = wintypes.DWORD()
        self._u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        k = ctypes.windll.kernel32
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower()
            return ""
        finally:
            k.CloseHandle(h)

    def windows(self) -> List[int]:
        """Visible top-level Edge browser windows."""
        found: List[int] = []

        def _cb(hwnd, _lp):
            if (self._u.IsWindowVisible(hwnd) and self._class(hwnd) == EDGE_CLASS
                    and self.title(hwnd) and self._exe(hwnd) == "msedge.exe"):
                found.append(int(hwnd))
            return True

        self._u.EnumWindows(_WNDENUMPROC(_cb), 0)
        return found

    def launch(self) -> None:
        # `start` resolves msedge through App Paths, so it needs no PATH entry.
        subprocess.Popen('start "" msedge --new-window', shell=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def maximize(self, hwnd) -> None:
        self._u.ShowWindow(hwnd, SW_MAXIMIZE)
        self._u.SetForegroundWindow(hwnd)

    def rect(self, hwnd) -> Rect:
        r = wintypes.RECT()
        self._u.GetWindowRect(hwnd, ctypes.byref(r))
        return r.left, r.top, r.right, r.bottom

    def dpi_scale(self, hwnd) -> float:
        try:
            return max(1.0, self._u.GetDpiForWindow(hwnd) / 96.0)
        except Exception:
            return 1.0

    def viewport(self, hwnd) -> Optional[Rect]:
        """The web page's area: Chromium's render widget (largest visible one)."""
        best: List[Rect] = []

        def _cb(child, _lp):
            if self._u.IsWindowVisible(child) and self._class(child) == RENDER_CLASS:
                r = self.rect(child)
                if r[2] > r[0] and r[3] > r[1]:
                    best.append(r)
            return True

        self._u.EnumChildWindows(hwnd, _WNDENUMPROC(_cb), 0)
        if best:
            return max(best, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))
        return self._uia_rect(hwnd, "DocumentControl")

    def address_bar(self, hwnd, below: int) -> Optional[Rect]:
        """The address bar: the first edit box above the page (y < below)."""
        return self._uia_rect(hwnd, "EditControl", max_bottom=below)

    def _uia_rect(self, hwnd, control_type: str, max_bottom: Optional[int] = None) -> Optional[Rect]:
        try:
            import uiautomation as auto
        except Exception:
            return None
        t0 = time.monotonic()
        try:
            with auto.UIAutomationInitializerInThread():
                root = auto.ControlFromHandle(hwnd)
                for ctrl, _depth in auto.WalkControl(root, maxDepth=16):
                    if time.monotonic() - t0 > UIA_SEARCH_LIMIT_S:
                        break
                    if ctrl.ControlTypeName != control_type:
                        continue
                    r = ctrl.BoundingRectangle
                    if r.width() <= 0 or r.height() <= 0:
                        continue
                    if max_bottom is not None and r.bottom > max_bottom:
                        continue
                    return r.left, r.top, r.right, r.bottom
        except Exception as e:
            log.debug("UIA %s lookup failed: %s", control_type, e)
        return None


edge = _Edge()

VK_LBUTTON, VK_RETURN = 0x01, 0x0D


class _Keys:
    """Is a mouse button / key held right now (edit mode watches clicks
    without blocking them, so the page still gets every click)."""

    def down(self, vk: int) -> bool:
        return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


keys = _Keys()


# ── The runner ───────────────────────────────────────────────────────────────

class _Run:
    def __init__(self, ui: RoutineUI, cancel_event: threading.Event, flight_scale: float):
        from agent import executor
        self.win = executor.win
        self.ui = ui
        self.cancel = cancel_event
        self.flight_scale = max(0.0, flight_scale)
        self.pointing = False

    def check(self):
        if self.cancel.is_set():
            raise _Cancelled()

    def sleep(self, seconds: float):
        end = time.monotonic() + max(0.0, seconds)
        while True:
            self.check()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.02, left))

    # pointer + real mouse, together
    def _glide_seconds(self, lx0, ly0, lx1, ly1) -> float:
        try:
            from ui.overlay import FLY_DURATION_MIN, FLY_DURATION_MAX
        except Exception:
            FLY_DURATION_MIN, FLY_DURATION_MAX = 1.6, 2.8
        dist = math.hypot(lx1 - lx0, ly1 - ly0)
        dur = max(FLY_DURATION_MIN, min(FLY_DURATION_MAX, FLY_DURATION_MIN + dist / 700.0))
        return dur * FLIGHT_SPEED * self.flight_scale

    def glide_to(self, px: int, py: int, label: str):
        """Fly the grape to (px, py) and move the real cursor along the same
        arc in the same time (the overlay's smoothstep + quadratic curve)."""
        from agent.executor import to_logical
        self.check()
        sx, sy = self.win.cursor_pos()
        lx0, ly0 = to_logical(sx, sy)
        lx1, ly1 = to_logical(px, py)
        dur = self._glide_seconds(lx0, ly0, lx1, ly1)
        self.ui.point_hold(True)
        self.ui.point_at(float(lx1), float(ly1), label)
        self.pointing = True
        dist = math.hypot(px - sx, py - sy)
        cx = (sx + px) / 2.0
        cy = (sy + py) / 2.0 - min(dist * 0.22, 90.0)
        t0 = time.monotonic()
        while dur > 0:
            self.check()
            lp = min(1.0, (time.monotonic() - t0) / dur)
            t = lp * lp * (3.0 - 2.0 * lp)
            omt = 1.0 - t
            self.win.set_cursor(int(round(omt * omt * sx + 2 * omt * t * cx + t * t * px)),
                                int(round(omt * omt * sy + 2 * omt * t * cy + t * t * py)))
            if lp >= 1.0:
                break
            time.sleep(GLIDE_TICK_S)
        self.win.set_cursor(px, py)
        self.sleep(HOLD_S)

    def click_at(self, px: int, py: int, label: str, expect_hwnd: Optional[int] = None):
        self.glide_to(px, py, label)
        self.check()
        pid, cls, title = self.win.window_at(px, py)
        if pid == os.getpid():
            raise RoutineError(f"Clicky's own window ({cls}) covers ({px}, {py})")
        if expect_hwnd is not None and cls != EDGE_CLASS:
            raise RoutineError(f"the window under ({px}, {py}) is {title or cls!r}, not Edge")
        self.win.click(1)
        log.info("routine click %s at (%d, %d)", label, px, py)

    # steps of call_taxi
    def open_edge(self) -> int:
        self.check()
        before = set(edge.windows())
        edge.launch()
        end = time.monotonic() + EDGE_START_TIMEOUT_S
        hwnd = None
        while time.monotonic() < end:
            self.sleep(0.05)
            new = [h for h in edge.windows() if h not in before]
            if new:
                hwnd = new[0]
                break
        if hwnd is None:                       # Edge reused a window: take any
            existing = edge.windows()
            if not existing:
                raise RoutineError("Microsoft Edge did not open")
            hwnd = existing[0]
        edge.maximize(hwnd)
        self.sleep(MAXIMIZE_SETTLE_S)
        return hwnd

    def page_rect(self, hwnd) -> Rect:
        """The web page's area in screen pixels."""
        vp = edge.viewport(hwnd)
        if vp is not None:
            return vp
        scale = edge.dpi_scale(hwnd)           # no render widget / UIA: estimate
        l, t, r, b = edge.rect(hwnd)
        return l + int(8 * scale), t + int(round(87 * scale)), r - int(8 * scale), b - int(8 * scale)

    def page_origin(self, hwnd) -> Tuple[int, int]:
        r = self.page_rect(hwnd)
        return r[0], r[1]

    def click_address_bar(self, hwnd):
        _ox, page_top = self.page_origin(hwnd)
        scale = edge.dpi_scale(hwnd)
        bar = edge.address_bar(hwnd, below=page_top)
        if bar is not None:
            px, py = (bar[0] + bar[2]) // 2, (bar[1] + bar[3]) // 2
        else:                                  # estimate: middle of the toolbar
            l, t, r, _b = edge.rect(hwnd)
            px, py = l + (r - l) * 35 // 100, page_top - int(round(22 * scale))
        self.ui.on_action("click", "მისამართის ველი", "ვაჭერ მისამართის ველს")
        self.click_at(px, py, "მისამართის ველი", expect_hwnd=hwnd)
        self.win.press_keys([0x11, 0x4C])      # Ctrl+L: focus + select all, whatever was hit

    def type_url(self, hwnd, url: str):
        self.ui.on_action("type", url, "ვწერ მისამართს")
        for ch in url:
            self.check()
            self.win.type_unicode(ch)
            time.sleep(TYPE_INTERVAL_S)
        self.win.press_keys([0x2E])            # Delete: drop an inline autocomplete
        before = edge.title(hwnd)
        self.win.press_keys([0x0D])            # Enter
        end = time.monotonic() + PAGE_LOAD_TIMEOUT_S
        while time.monotonic() < end:
            self.sleep(0.05)
            now = edge.title(hwnd)
            if now and now != before:
                break

    def click_page(self, hwnd, x: int, y: int, say: str = ""):
        """(x, y) = screen pixels from the page's top-left corner."""
        ox, oy = self.page_origin(hwnd)
        self.ui.on_action("click", f"{x}, {y}", say or "ვაჭერ")
        self.click_at(ox + x, oy + y, say or "ტაქსი", expect_hwnd=hwnd)

    def open_page(self, url: str) -> int:
        from timing import timed
        with timed("routine.open_edge"):
            hwnd = self.open_edge()
        with timed("routine.address_bar"):
            self.click_address_bar(hwnd)
        with timed("routine.type_url_and_load"):
            self.type_url(hwnd, url)
        return hwnd

    def wait_gap(self, seconds: float, spoken=None):
        """Wait `seconds`, and also until `spoken` (a Future) is done — at
        most SPEECH_WAIT_MAX_S — so a step's line is never cut off."""
        start = time.monotonic()
        end, hard = start + seconds, start + max(seconds, SPEECH_WAIT_MAX_S)
        while True:
            self.check()
            now = time.monotonic()
            if now >= end and (spoken is None or _done(spoken) or now >= hard):
                return
            time.sleep(0.02)

    def click_steps(self, hwnd, steps: List[dict], first_wait: float = 0.0):
        """Each step: its line is spoken during the gap before its click
        (first_wait for step 1, the previous step's pause_s after that), and
        the click waits until the line has been said."""
        from timing import timed
        wait = first_wait
        for i, s in enumerate(steps, start=1):
            with timed("routine.page_click", n=i, x=s["x"], y=s["y"]):
                spoken = self.ui.say(s["say"]) if s.get("say") else None
                self.wait_gap(wait, spoken)
                self.click_page(hwnd, s["x"], s["y"], s.get("say", ""))
            wait = s["pause_s"]

    def call_taxi(self):
        routine = load_routine("call_taxi")
        hwnd = self.open_page(routine["url"])
        self.click_steps(hwnd, routine["steps"], first_wait=PAGE_SETTLE_S)

    # edit mode
    def release_pointer(self):
        if self.pointing:
            self.ui.point_release()
            self.pointing = False

    def wait_for_click(self, hwnd, idle_timeout: float, finish_on_enter: bool) -> Optional[Tuple[int, int]]:
        """The next left click inside the page, as page coordinates; None
        when Enter is pressed (finish_on_enter) or nothing happens for
        idle_timeout. The click itself still reaches the page."""
        was_down = keys.down(VK_LBUTTON)
        end = time.monotonic() + idle_timeout
        while time.monotonic() < end:
            self.check()
            if finish_on_enter and keys.down(VK_RETURN):
                while keys.down(VK_RETURN):
                    self.sleep(0.01)
                return None
            down = keys.down(VK_LBUTTON)
            if down and not was_down:
                px, py = self.win.cursor_pos()
                l, t, r, b = self.page_rect(hwnd)
                if l <= px < r and t <= py < b:
                    return px - l, py - t
                log.info("edit mode: click at (%d, %d) is outside the page — ignored", px, py)
            was_down = down
            time.sleep(0.01)
        return None

    def record(self, name: str, step: int) -> dict:
        """Edit mode. step=0: record every step until Enter; step=k: record
        only step k. Returns the routine to save."""
        routine = load_routine(name)
        old = routine["steps"]
        hwnd = self.open_page(routine["url"])
        if step <= 0:
            self.release_pointer()                 # the grape follows the person now
            self.ui.say(SAY_RECORD_ALL)
            new: List[dict] = []
            while True:
                n = len(new) + 1
                self.ui.on_action("record", f"ნაბიჯი {n}", "დააჭირეთ სასურველ ადგილს")
                got = self.wait_for_click(hwnd, RECORD_IDLE_TIMEOUT_S, finish_on_enter=bool(new))
                if got is None:
                    break
                say = old[n - 1].get("say", "") if n <= len(old) else ""
                new.append({"x": got[0], "y": got[1], "pause_s": DEFAULT_PAUSE_S, "say": say})
                log.info("edit mode: step %d = %s", n, got)
                self.ui.on_action("record", f"ნაბიჯი {n} შენახულია", f"{got[0]}, {got[1]}")
            if not new:
                raise RoutineError("no click was recorded")
            new[-1]["pause_s"] = 0.0
            return {"url": routine["url"], "steps": new}
        k = min(step, len(old) + 1)                # one past the end = add a step
        self.click_steps(hwnd, old[:k - 1], first_wait=PAGE_SETTLE_S)
        self.release_pointer()
        self.ui.say(SAY_RECORD_ONE.format(n=k))
        self.ui.on_action("record", f"ნაბიჯი {k}", "დააჭირეთ სასურველ ადგილს")
        got = self.wait_for_click(hwnd, RECORD_IDLE_TIMEOUT_S, finish_on_enter=False)
        if got is None:
            raise RoutineError(f"no click was recorded for step {k}")
        pause = old[k - 1]["pause_s"] if k <= len(old) else DEFAULT_PAUSE_S
        steps = [dict(s) for s in old]
        say = old[k - 1].get("say", "") if k <= len(old) else ""
        entry = {"x": got[0], "y": got[1], "pause_s": pause, "say": say}
        if k <= len(old):
            steps[k - 1] = entry
        else:
            if steps:
                steps[-1]["pause_s"] = max(steps[-1]["pause_s"], DEFAULT_PAUSE_S)
            steps.append(entry)
        log.info("edit mode: step %d = %s", k, got)
        return {"url": routine["url"], "steps": steps}


def run_routine(name: str, ui: Optional[RoutineUI] = None,
                cancel_event: Optional[threading.Event] = None,
                flight_scale: float = 1.0) -> RoutineResult:
    """Run one routine to the end. Blocking; never raises."""
    from agent import executor
    from timing import timed
    ui = ui or RoutineUI()
    cancel_event = cancel_event or executor.cancel_event
    if name not in ROUTINES:
        return RoutineResult(False, "failed", f"unknown routine {name!r}")
    executor.win.ensure_dpi_awareness()
    run = _Run(ui, cancel_event, flight_scale)
    ui.set_speed(FLIGHT_SPEED)
    ui.say(SAY_START)                  # „კარგი“ — and nothing else, whatever happens
    try:
        with timed("routine.total", name=name) as tm:
            getattr(run, name)()
            tm["status"] = "done"
        return RoutineResult(True, "done")
    except _Cancelled:
        return RoutineResult(False, "cancelled", "stopped by the user")
    except Exception as e:
        log.exception("routine %s failed", name)
        return RoutineResult(False, "failed", str(e) or e.__class__.__name__)
    finally:
        if run.pointing:
            ui.point_release()
        ui.set_speed(1.0)


def record_routine(name: str, ui: Optional[RoutineUI] = None,
                   cancel_event: Optional[threading.Event] = None,
                   step: int = 0, flight_scale: float = 1.0) -> RoutineResult:
    """Edit mode: open the page, record the person's clicks, save the file.
    Blocking; never raises. Nothing is saved on cancel or failure."""
    from agent import executor
    ui = ui or RoutineUI()
    cancel_event = cancel_event or executor.cancel_event
    if name not in ROUTINES:
        return RoutineResult(False, "failed", f"unknown routine {name!r}")
    executor.win.ensure_dpi_awareness()
    run = _Run(ui, cancel_event, flight_scale)
    ui.set_speed(FLIGHT_SPEED)
    try:
        routine = run.record(name, step)
        path = save_routine(name, routine)
        ui.say(SAY_SAVED)
        return RoutineResult(True, "done", path)
    except _Cancelled:
        return RoutineResult(False, "cancelled", "stopped by the user")
    except Exception as e:
        log.exception("recording routine %s failed", name)
        return RoutineResult(False, "failed", str(e) or e.__class__.__name__)
    finally:
        if run.pointing:
            ui.point_release()
        ui.set_speed(1.0)

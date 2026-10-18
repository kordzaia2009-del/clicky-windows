"""
Executor — performs ONE real input action on Windows, with the animated
pointer showing where it is about to act.

    execute(action: dict, shot: ScreenShot | None, callbacks) -> ActionResult

Actions (dicts, e.g. from a plan):
    {"type": "click" | "double_click", "target": <target>, "explanation": "...",
     "expect_window": "Notepad"}       # optional: refuse if another window is on top
    {"type": "type",   "text": "გამარჯობა", "private": false}
    {"type": "key",    "keys": "ctrl+a" | ["ctrl", "a"], "repeat": 1}
    {"type": "scroll", "amount": -3, "target": <target, optional>}   # + = up
    {"type": "wait",   "seconds": 1.5}

<target> is one of
    {"name": "Save"}                       UI element found by name (UIA first)
    {"x": 512, "y": 300}                   0-1000 on `shot`, like [POINT] tags
    {"x": 1234, "y": 567, "space": "physical"}   absolute physical pixels
    a hybrid_pointer.Target                already resolved

Coordinates: clicks use PHYSICAL virtual-desktop pixels only (screenshot
origin + size from mss, SetCursorPos/SendInput), with the process made
per-monitor DPI aware, so they are exact on every monitor. Logical pixels
are derived only for the overlay animation (the same convention as
CompanionManager._denorm).

Safety:
  • dry run (set_dry_run / CLICKY_DRY_RUN=1): animate + log, no input at all.
  • cancel_event: set by StopHotkey (Esc) — checked before and during every
    action. It stays set until the caller calls reset_cancel() for a new task.
  • the user moving the mouse during an action pauses it until the mouse has
    been still for a moment.
  • text marked private, or a focused password field (UIA IsPassword), is
    never typed.
  • a click whose point lands on one of Clicky's own windows is refused
    (the overlay is click-through; the panel is not).

Threading: run execute() on a worker thread. It only *calls* the callbacks;
in the app those must be Qt signal emits so widgets are touched on the GUI
thread only.

All Win32 access goes through the module-level `win` object, so tests can
replace it and no real input is ever sent from a test.
"""

from __future__ import annotations

import ctypes
import logging
import math
import os
import threading
import time
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable, List, Optional, Tuple

log = logging.getLogger("clicky.executor")

# ── Shared state ─────────────────────────────────────────────────────────────

cancel_event = threading.Event()      # set by StopHotkey (Esc)
DRY_RUN = os.getenv("CLICKY_DRY_RUN", "").strip().lower() in ("1", "true", "yes")

HOLD_S = 0.4              # pointer rests on the target this long before acting
POLL_S = 0.05
PAUSE_MOVE_PX = 8         # physical mouse movement that counts as the user
RESUME_STILL_S = 1.5      # mouse must be still this long to resume
MAX_WAIT_S = 30.0
TYPE_CHUNK = 8            # characters per SendInput batch (checks in between)
REPOINT_PX = 24           # an element that moved further than this is re-shown


def set_dry_run(enabled: bool) -> None:
    global DRY_RUN
    DRY_RUN = bool(enabled)


def reset_cancel() -> None:
    """Call when starting a new task; Esc from an earlier one stays latched."""
    cancel_event.clear()


def _noop(*_a, **_k):
    return None


@dataclass
class ExecutorCallbacks:
    """How the executor talks to the UI. In the app, pass signal emits:
    point_at=manager.sig_point_at.emit, point_hold=manager.sig_point_hold.emit,
    point_release=manager.sig_point_release.emit."""
    point_at: Callable[[float, float, str], None] = _noop   # logical px
    point_hold: Callable[[bool], None] = _noop
    point_release: Callable[[], None] = _noop
    on_log: Callable[[dict], None] = _noop                 # every log entry
    on_pause: Callable[[bool], None] = _noop               # True = paused
    flight_scale: float = 1.0                              # 1.7 in slow mode


@dataclass
class ActionResult:
    ok: bool
    type: str
    status: str            # done | dry_run | cancelled | refused | failed
    message: str = ""
    method: str = ""       # uia-invoke | uia-select | mouse | unicode | keys | wheel | wait
    x: Optional[int] = None    # physical px of the target, when there is one
    y: Optional[int] = None
    target: str = ""
    elapsed_s: float = 0.0
    dry_run: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class _Cancelled(Exception):
    pass


class _Refused(Exception):
    pass


# ── Win32 layer ──────────────────────────────────────────────────────────────

ULONG_PTR = ctypes.c_size_t
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, MOUSEEVENTF_WHEEL = 0x0002, 0x0004, 0x0800
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0001, 0x0002, 0x0004
WHEEL_DELTA = 120


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


# Virtual-key codes for the "key" action
_VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12,
    "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23,
    "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
    "win": 0x5B, "windows": 0x5B,
}
_VK.update({chr(c): c for c in range(ord("0"), ord("9") + 1)})
_VK.update({chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)})
_VK.update({f"f{i}": 0x6F + i for i in range(1, 13)})
_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B}


def parse_keys(keys) -> List[int]:
    """'ctrl+a' / ['ctrl', 'a'] -> [0x11, 0x41]. Raises ValueError."""
    parts = keys if isinstance(keys, (list, tuple)) else str(keys).split("+")
    out = []
    for p in parts:
        name = str(p).strip().lower()
        if name not in _VK:
            raise ValueError(f"unknown key {p!r}")
        out.append(_VK[name])
    if not out:
        raise ValueError("no keys given")
    return out


class _Win32:
    """Thin wrapper over the Win32 / UIA calls the executor needs."""

    def __init__(self):
        self._dpi_done = False

    @property
    def _u(self):
        return ctypes.windll.user32

    def ensure_dpi_awareness(self):
        """Per-monitor-v2 so SetCursorPos/UIA use true physical pixels. In the
        app Qt has already done this and the call fails harmlessly."""
        if self._dpi_done:
            return
        self._dpi_done = True
        try:
            self._u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                pass

    def _send(self, inputs: List[INPUT]) -> int:
        u = self._u
        u.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        u.SendInput.restype = wintypes.UINT
        arr = (INPUT * len(inputs))(*inputs)
        sent = u.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
        if sent != len(inputs):
            # Typically UIPI: the target window runs elevated.
            raise OSError(f"SendInput sent {sent}/{len(inputs)} events "
                          f"(error {ctypes.GetLastError()}) — is the target app "
                          f"running as administrator?")
        return sent

    def virtual_desktop(self) -> Tuple[int, int, int, int]:
        sm = self._u.GetSystemMetrics
        left, top = sm(76), sm(77)
        return left, top, left + sm(78), top + sm(79)

    def cursor_pos(self) -> Tuple[int, int]:
        pt = wintypes.POINT()
        self._u.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def set_cursor(self, x: int, y: int) -> bool:
        return bool(self._u.SetCursorPos(int(x), int(y)))

    def click(self, count: int = 1):
        evs = []
        for _ in range(count):
            for flag in (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP):
                evs.append(INPUT(type=INPUT_MOUSE, u=_INPUTUNION(
                    mi=MOUSEINPUT(0, 0, 0, flag, 0, 0))))
        self._send(evs)

    def wheel(self, clicks: int):
        delta = (WHEEL_DELTA * int(clicks)) & 0xFFFFFFFF
        self._send([INPUT(type=INPUT_MOUSE, u=_INPUTUNION(
            mi=MOUSEINPUT(0, 0, delta, MOUSEEVENTF_WHEEL, 0, 0)))])

    def _key_input(self, vk: int, up: bool) -> INPUT:
        flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0)
        scan = self._u.MapVirtualKeyW(vk, 0)
        return INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(
            ki=KEYBDINPUT(vk, scan, flags, 0, 0)))

    def press_keys(self, vks: List[int]):
        """Chord: press in order, release in reverse."""
        evs = [self._key_input(vk, False) for vk in vks]
        evs += [self._key_input(vk, True) for vk in reversed(vks)]
        self._send(evs)

    def type_unicode(self, text: str):
        """Unicode SendInput (KEYEVENTF_UNICODE): the character itself is sent,
        independent of the keyboard layout, so Georgian works on an English
        layout. Newline/tab go as real Enter/Tab keys."""
        evs = []
        for ch in text:
            if ch == "\r":
                continue
            if ch in "\n\t":
                vk = 0x0D if ch == "\n" else 0x09
                evs += [self._key_input(vk, False), self._key_input(vk, True)]
                continue
            data = ch.encode("utf-16-le")
            for i in range(0, len(data), 2):          # surrogate pairs too
                unit = int.from_bytes(data[i:i + 2], "little")
                for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                    evs.append(INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(
                        ki=KEYBDINPUT(0, unit, flags, 0, 0))))
        if evs:
            self._send(evs)

    def window_at(self, x: int, y: int) -> Tuple[int, str, str]:
        """(pid, class, title) of the top-level window under a physical point."""
        u = self._u
        u.WindowFromPoint.restype = wintypes.HWND
        u.WindowFromPoint.argtypes = [wintypes.POINT]
        u.GetAncestor.restype = wintypes.HWND
        hwnd = u.WindowFromPoint(wintypes.POINT(int(x), int(y)))
        if not hwnd:
            return 0, "", ""
        root = u.GetAncestor(hwnd, 2) or hwnd       # GA_ROOT
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(root, ctypes.byref(pid))
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(root, cls, 256)
        title = ctypes.create_unicode_buffer(256)
        u.GetWindowTextW(root, title, 256)
        return pid.value, cls.value, title.value

    def focused_is_password(self) -> Optional[bool]:
        try:
            import uiautomation as auto
            ctrl = auto.GetFocusedControl()
            return bool(ctrl.IsPassword) if ctrl is not None else None
        except Exception:
            return None

    def type_text(self, text: str) -> str:
        """Type with the configured backend (agent/typing_backends.py).
        Returns how it was typed; falls back to SendInput if the backend's
        package is missing."""
        from agent import typing_backends as tb
        backend = tb.configured_backend()
        try:
            if backend == "pyautogui":
                return tb.type_with_pyautogui(text)
            if backend == "pywinauto":
                return tb.type_with_pywinauto(text)
        except ImportError:
            log.warning("%s is not installed — typing with SendInput", backend)
        self.type_unicode(text)
        return "unicode"

    def element_rect(self, element) -> Optional[Tuple[int, int, int, int]]:
        if hasattr(element, "rectangle") and not hasattr(element, "BoundingRectangle"):
            try:                                    # pywinauto wrapper
                r = element.rectangle()
                if r.right > r.left and r.bottom > r.top:
                    return int(r.left), int(r.top), int(r.right), int(r.bottom)
            except Exception:
                pass
            return None
        try:
            r = element.BoundingRectangle
            if r and r.width() > 0 and r.height() > 0:
                return int(r.left), int(r.top), int(r.right), int(r.bottom)
        except Exception:
            pass
        return None

    def uia_activate(self, element) -> Optional[str]:
        """Invoke (buttons, links, menu items) or Select (list/tab items).
        Returns the method used, or None when neither pattern works."""
        if hasattr(element, "invoke") and not hasattr(element, "GetPattern"):
            # pywinauto wrapper: its own Invoke / Select
            for method, name in (("invoke", "pywinauto-invoke"), ("select", "pywinauto-select")):
                try:
                    if hasattr(element, method):
                        getattr(element, method)()
                        return name
                except Exception as e:
                    log.debug("pywinauto %s failed: %s", method, e)
            return None
        try:
            import uiautomation as auto
        except Exception:
            return None
        for pid, method, name in ((auto.PatternId.InvokePattern, "Invoke", "uia-invoke"),
                                  (auto.PatternId.SelectionItemPattern, "Select", "uia-select")):
            try:
                pattern = element.GetPattern(pid)
                if pattern is not None:
                    getattr(pattern, method)()
                    return name
            except Exception as e:
                log.debug("UIA %s failed: %s", method, e)
        return None

    def find_target(self, name: str):
        from ai.hybrid_pointer import find_target
        return find_target(name, skip_vision=True)


win = _Win32()


def _element_pid(element) -> Optional[int]:
    """Process id of a UI element — uiautomation (ProcessId) or pywinauto
    (process_id())."""
    try:
        if hasattr(element, "ProcessId"):
            return int(element.ProcessId)
        if hasattr(element, "process_id"):
            return int(element.process_id())
    except Exception:
        pass
    return None


# ── Target resolution ────────────────────────────────────────────────────────

@dataclass
class _Resolved:
    px: int
    py: int
    name: str
    element: Any = None


def _system_scale() -> float:
    try:
        return max(1.0, ctypes.windll.user32.GetDpiForSystem() / 96.0)
    except Exception:
        return 1.0


def to_logical(px: float, py: float, shot=None) -> Tuple[float, float]:
    """Physical -> logical, for the overlay only (CompanionManager._denorm's
    convention: one scale; exact on single or equal-DPI monitors)."""
    if shot is not None:
        s = shot.dpi_scale or 1.0
        return (shot.logical_left + (px - shot.physical_left) / s,
                shot.logical_top + (py - shot.physical_top) / s)
    s = _system_scale()
    return px / s, py / s


def norm_to_physical(nx: float, ny: float, shot) -> Tuple[int, int]:
    """0-1000 on a screenshot -> physical virtual-desktop pixels."""
    return (int(round(shot.physical_left + nx / 1000.0 * shot.physical_width)),
            int(round(shot.physical_top + ny / 1000.0 * shot.physical_height)))


def _resolve(target, shot) -> _Resolved:
    if target is None:
        raise ValueError("this action needs a target")
    if hasattr(target, "bbox") and hasattr(target, "source"):          # Target
        element = target.element if target.source == "uia" else None
        return _Resolved(int(target.x), int(target.y), target.label or "", element)
    if not isinstance(target, dict):
        raise ValueError(f"unsupported target {target!r}")
    name = target.get("name") or target.get("element")
    if name:
        t = win.find_target(str(name))
        if t is None:
            raise LookupError(f"could not find {name!r} on screen")
        element = t.element if t.source == "uia" else None
        return _Resolved(int(t.x), int(t.y), t.label or str(name), element)
    if "x" in target and "y" in target:
        x, y = float(target["x"]), float(target["y"])
        space = target.get("space", "norm")
        label = str(target.get("label", ""))
        if space == "physical":
            return _Resolved(int(round(x)), int(round(y)), label)
        if space == "logical":
            s = (shot.dpi_scale if shot else _system_scale()) or 1.0
            return _Resolved(int(round(x * s)), int(round(y * s)), label)
        if shot is None:
            raise ValueError("normalized (0-1000) coordinates need a screenshot")
        if not (0 <= x <= 1000 and 0 <= y <= 1000):
            raise ValueError(f"normalized coordinates out of range: {x}, {y}")
        px, py = norm_to_physical(x, y, shot)
        return _Resolved(px, py, label)
    raise ValueError("target needs a name or x/y")


# ── The executor ─────────────────────────────────────────────────────────────

class _Run:
    """One execute() call: waits, pausing, logging, animation."""

    def __init__(self, action: dict, shot, cb: ExecutorCallbacks):
        self.action = action
        self.shot = shot
        self.cb = cb
        self.type = str(action.get("type", "")).strip().lower()
        self.explanation = str(action.get("explanation") or action.get("why") or "")
        self.t0 = time.monotonic()
        self.dry = DRY_RUN
        self.pointing = False

    # logging
    def log(self, event: str, **fields):
        entry = {"time": datetime.now().isoformat(timespec="milliseconds"),
                 "action": self.type, "event": event, "dry_run": self.dry,
                 "explanation": self.explanation, **fields}
        log.info("%s %s %s", self.type, event,
                 {k: v for k, v in entry.items() if k not in ("time", "action", "event")})
        try:
            self.cb.on_log(entry)
        except Exception:
            pass

    def result(self, status: str, message: str = "", method: str = "",
               where: Optional[_Resolved] = None) -> ActionResult:
        r = ActionResult(
            ok=status in ("done", "dry_run"), type=self.type, status=status,
            message=message, method=method,
            x=where.px if where else None, y=where.py if where else None,
            target=where.name if where else "",
            elapsed_s=round(time.monotonic() - self.t0, 3), dry_run=self.dry,
        )
        self.log(status, message=message, method=method,
                 x=r.x, y=r.y, target=r.target)
        return r

    # waiting with cancel + pause
    def check_cancel(self):
        if cancel_event.is_set():
            raise _Cancelled()

    def wait(self, seconds: float):
        end = time.monotonic() + max(0.0, seconds)
        last = win.cursor_pos()
        while True:
            self.check_cancel()
            now = time.monotonic()
            if now >= end:
                return
            time.sleep(min(POLL_S, end - now))
            pos = win.cursor_pos()
            if math.hypot(pos[0] - last[0], pos[1] - last[1]) > PAUSE_MOVE_PX:
                end += self._pause_until_still()
                pos = win.cursor_pos()
            last = pos

    def _pause_until_still(self) -> float:
        """The user moved the mouse: hold everything until it rests."""
        start = time.monotonic()
        self.log("paused", reason="the user moved the mouse")
        self.cb.on_pause(True)
        last, still_since = win.cursor_pos(), time.monotonic()
        while time.monotonic() - still_since < RESUME_STILL_S:
            self.check_cancel()
            time.sleep(POLL_S)
            pos = win.cursor_pos()
            if math.hypot(pos[0] - last[0], pos[1] - last[1]) > 2:
                still_since = time.monotonic()
            last = pos
        self.cb.on_pause(False)
        paused = time.monotonic() - start
        self.log("resumed", paused_s=round(paused, 2))
        return paused

    # pointer animation
    def flight_seconds(self, lx: float, ly: float) -> float:
        try:
            from ui.overlay import FLY_DURATION_MIN, FLY_DURATION_MAX
        except Exception:
            FLY_DURATION_MIN, FLY_DURATION_MAX = 1.6, 2.8
        cx, cy = to_logical(*win.cursor_pos(), self.shot)
        dist = math.hypot(lx - cx, ly - cy)
        dur = max(FLY_DURATION_MIN, min(FLY_DURATION_MAX, FLY_DURATION_MIN + dist / 700.0))
        # 0 = no overlay to wait for (only the hold applies)
        return dur * max(0.0, self.cb.flight_scale)

    def show_target(self, where: _Resolved):
        lx, ly = to_logical(where.px, where.py, self.shot)
        self.log("pointing", x=where.px, y=where.py, logical=(round(lx), round(ly)),
                 target=where.name)
        self.cb.point_hold(True)
        self.cb.point_at(float(lx), float(ly), where.name)
        self.pointing = True
        self.wait(self.flight_seconds(lx, ly) + HOLD_S)

    def refresh(self, where: _Resolved) -> _Resolved:
        """Re-read an element's position right before acting: a pause (the
        user moving the mouse — or the window) or a slow page may have moved
        it. Aims at the centre of the part that is actually on screen; if it
        moved noticeably the pointer flies there again first, so the person
        always sees where the click will land."""
        if where.element is None:
            return where
        for attempt in (1, 2):
            moved = self.aim(where)
            if moved <= REPOINT_PX or attempt == 2:
                return where
            self.log("target_moved", moved_px=round(moved), x=where.px, y=where.py)
            self.show_target(where)
        return where

    def aim(self, where: _Resolved) -> float:
        """Point an element target at the centre of its on-screen part.
        Returns how far that moved it. Raises if none of it is visible."""
        if where.element is None:
            return 0.0
        rect = win.element_rect(where.element)
        if not rect:
            raise LookupError(f"{where.name!r} is no longer on screen")
        dl, dt, dr, db = win.virtual_desktop()
        l, t = max(rect[0], dl), max(rect[1], dt)
        r, b = min(rect[2], dr), min(rect[3], db)
        if l >= r or t >= b:
            raise LookupError(f"{where.name!r} is no longer visible (moved off screen?)")
        nx, ny = (l + r) // 2, (t + b) // 2
        moved = math.hypot(nx - where.px, ny - where.py)
        where.px, where.py = nx, ny
        return moved

    def check_inside_desktop(self, where: _Resolved):
        l, t, r, b = win.virtual_desktop()
        if not (l <= where.px < r and t <= where.py < b):
            raise ValueError(f"point ({where.px}, {where.py}) is outside the screen")

    def check_not_ours(self, where: _Resolved):
        """The window that would really receive the click must be the right one:
        never Clicky's own (the overlay is click-through, the panel is not),
        the element's own app for element targets, and the expected window
        when the action names one ("expect_window": part of its title/class).
        A window the user brought to the front over the target — seen in
        testing — must not get the click instead."""
        pid, cls, title = win.window_at(where.px, where.py)
        self.log("window_under_point", window_class=cls, window_title=title[:80])
        if pid == os.getpid():
            raise _Refused(
                f"the point is covered by Clicky's own window ({cls or 'unknown'}); "
                f"not clicking through it"
            )
        if where.element is not None:
            owner = _element_pid(where.element)
            if owner and pid and owner != pid:
                raise _Refused(
                    f"{where.name!r} is covered by another window ({title or cls!r}); "
                    f"not clicking"
                )
        expect = str(self.action.get("expect_window") or "").strip().lower()
        if expect and expect not in title.lower() and expect not in cls.lower():
            raise _Refused(
                f"the window under the point is {title or cls!r}, "
                f"not {self.action.get('expect_window')!r}; not clicking"
            )

    # actions
    def do_click(self, count: int) -> ActionResult:
        where = _resolve(self.action.get("target"), self.shot)
        self.aim(where)                      # elements: their visible part
        self.check_inside_desktop(where)
        self.show_target(where)
        where = self.refresh(where)
        self.check_inside_desktop(where)
        self.check_cancel()
        if self.dry:
            return self.result("dry_run", "would click", where=where,
                               method="uia-or-mouse" if where.element is not None else "mouse")
        if count == 1 and where.element is not None:
            method = win.uia_activate(where.element)
            if method:
                return self.result("done", f"activated {where.name!r}", method, where)
        self.check_not_ours(where)
        if not win.set_cursor(where.px, where.py):
            raise OSError("SetCursorPos failed")
        got = win.cursor_pos()
        if abs(got[0] - where.px) > 1 or abs(got[1] - where.py) > 1:
            raise OSError(f"cursor landed at {got}, not ({where.px}, {where.py}) — DPI mismatch?")
        win.click(count)
        return self.result("done", "clicked" if count == 1 else "double-clicked",
                           "mouse", where)

    def do_type(self) -> ActionResult:
        text = self.action.get("text")
        if self.action.get("private"):
            raise _Refused("this step is private (password, card or code) — "
                           "the person types it themselves")
        if not isinstance(text, str) or not text:
            raise ValueError("type needs non-empty text")
        if win.focused_is_password():
            raise _Refused("the focused field is a password field — not typing into it")
        self.check_cancel()
        self.log("typing", chars=len(text))
        from agent import typing_backends as tb
        backend = tb.configured_backend()
        if self.dry:
            return self.result("dry_run", f"would type {len(text)} characters", backend)
        # Pasted text (pyautogui + Georgian) goes in one go; typed text in
        # small chunks so Esc / a moving mouse can still stop it midway.
        if backend == "pyautogui" and not tb.is_plain_ascii(text):
            chunks = [text]
        else:
            chunks = [text[i:i + TYPE_CHUNK] for i in range(0, len(text), TYPE_CHUNK)]
        method = "unicode"
        try:
            for chunk in chunks:
                self.wait(0.03)                  # cancel/pause between chunks
                method = win.type_text(chunk)
        except Exception as e:
            if type(e).__name__ == "FailSafeException":     # pyautogui: mouse in a corner
                raise _Cancelled() from e
            raise
        return self.result("done", f"typed {len(text)} characters", method)

    def do_key(self) -> ActionResult:
        vks = parse_keys(self.action.get("keys"))
        repeat = max(1, min(int(self.action.get("repeat", 1)), 50))
        self.check_cancel()
        if self.dry:
            return self.result("dry_run", f"would press {self.action.get('keys')!r} x{repeat}", "keys")
        sends_esc = 0x1B in vks
        for _ in range(repeat):
            self.wait(0.05)
            was_cancelled = cancel_event.is_set()
            win.press_keys(vks)
            if sends_esc and not was_cancelled:
                # Our own Esc reaches StopHotkey's keyboard hook too; it must
                # not count as the user stopping us.
                time.sleep(0.1)
                cancel_event.clear()
        return self.result("done", f"pressed {self.action.get('keys')!r} x{repeat}", "keys")

    def do_scroll(self) -> ActionResult:
        amount = int(self.action.get("amount", 0))
        if amount == 0:
            raise ValueError("scroll needs a non-zero amount (+ up, - down)")
        where = None
        if self.action.get("target") is not None:
            where = _resolve(self.action.get("target"), self.shot)
            self.aim(where)
            self.check_inside_desktop(where)
            self.show_target(where)
            where = self.refresh(where)
            self.check_inside_desktop(where)
        self.check_cancel()
        if self.dry:
            return self.result("dry_run", f"would scroll {amount}", "wheel", where)
        if where is not None:
            self.check_not_ours(where)
            win.set_cursor(where.px, where.py)
        step = 1 if amount > 0 else -1
        for _ in range(abs(amount)):
            self.wait(0.04)
            win.wheel(step)
        return self.result("done", f"scrolled {amount}", "wheel", where)

    def do_wait(self) -> ActionResult:
        seconds = float(self.action.get("seconds", 1.0))
        seconds = max(0.0, min(seconds, MAX_WAIT_S))
        self.wait(seconds)                       # waits in dry run too
        return self.result("dry_run" if self.dry else "done", f"waited {seconds:g}s", "wait")

    def run(self) -> ActionResult:
        handlers = {
            "click": lambda: self.do_click(1),
            "double_click": lambda: self.do_click(2),
            "type": self.do_type,
            "key": self.do_key,
            "scroll": self.do_scroll,
            "wait": self.do_wait,
        }
        if self.type not in handlers:
            return self.result("failed", f"unknown action type {self.type!r}")
        self.log("start", action_spec={k: v for k, v in self.action.items()
                                       if k not in ("text",)})
        try:
            self.check_cancel()
            return handlers[self.type]()
        except _Cancelled:
            return self.result("cancelled", "stopped by the user (Esc)")
        except _Refused as e:
            return self.result("refused", str(e))
        except Exception as e:
            log.debug("action failed", exc_info=True)
            return self.result("failed", str(e) or e.__class__.__name__)
        finally:
            if self.pointing:
                self.cb.point_release()


def execute(action: dict, shot=None,
            callbacks: Optional[ExecutorCallbacks] = None) -> ActionResult:
    """Perform one action. Blocking; run it off the GUI thread. Never raises."""
    win.ensure_dpi_awareness()
    return _Run(action or {}, shot, callbacks or ExecutorCallbacks()).run()

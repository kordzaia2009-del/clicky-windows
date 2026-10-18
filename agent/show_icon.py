"""
„მაჩვენე ხრომი“ — point at an app's icon and circle it. Code only, no model
call, and it never clicks: the grape and the real mouse glide to the icon
(taskbar first, then the desktop) and a circle is drawn around it.

    match_show(text) -> "chrome" | None
    show_icon(app, ui, cancel_event, flight_scale=1.0) -> bool

match_show() only takes a short command made of nothing but a "show" word
(„მაჩვენე“, „შემომიხაზე“), the app's name and a few fillers („სად არის“):
„მაჩვენე ხრომი“, „მაჩვენი google chrom“, „შემომიხაზე სად არის გუგლ ხრომი“.
Questions like „მაჩვენე გუგლში როგორ ვეძებო“ still go to the normal flow.

All UIA access goes through the module-level `finder`, and the glide through
agent.routines (agent.executor.win), so tests can fake both.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

log = logging.getLogger("clicky.show_icon")

# app -> (UIA names it may have, how to say it)
APPS = {
    "chrome": (("Google Chrome",), "Google Chrome"),
}
# The command is ONLY these words, in any order: at least one "show" word and
# one app word, plus fillers. Spellings are what Google STT really writes
# („მაჩვენი google chrom“, „შემომიხაზე მაჩვენე სად არის google chromi“).
_SHOW_WORDS = {"მაჩვენე", "მაჩვენი", "მაჩვენეთ", "მიჩვენე", "მიჩვენი", "მიჩვენეთ",
               "შემომიხაზე", "შემოხაზე", "შემომიხაზეთ", "შემომიხაზი", "show"}
_FILLER_WORDS = {"სად", "არის", "აქ", "ა", "მაქვს", "ჩემი", "აიქონი", "იკონი", "ხატულა",
                 "გთხოვ", "გთხოვთ", "where", "is", "me", "the", "icon", "please"}
_APP_WORDS = {
    "chrome": {"გუგლი", "გუგლ", "გუგლის", "ხრომი", "ხრომ", "ხრომს", "ქრომი", "ქრომ",
               "google", "chrome", "chrom", "chromi", "chromy", "crome", "gugl",
               "გუგლ-ხრომი", "google-chrome"},
}
_PUNCT_RE = re.compile(r"[.!?,;:„“\"']+")

HOLD_S = 2.5            # the grape stays on the icon this long
CIRCLE_TTL_S = 4.0      # the circle fades after this long
CIRCLE_PAD = 1.35       # circle radius = half the icon's size x this, + 8 px
UIA_SEARCH_LIMIT_S = 1.5
MAX_ICONS = 80

# taskbar buttons that are not apps
_TASKBAR_SKIP = ("start", "search", "widgets", "task view", "copilot", "show hidden icons",
                 "show desktop", "notifications", "clock", "network", "volume", "power",
                 "tray input indicator", "onedrive", "battery")
_TASKBAR_SUFFIX_RE = re.compile(r"(\s+-\s+\d+\s+running windows?|\s+pinned)$", re.IGNORECASE)

SAY_NOT_FOUND = "{name} ვერ ვიპოვე ეკრანზე."

Rect = Tuple[int, int, int, int]        # physical px: left, top, right, bottom


def match_show(text: str) -> Optional[str]:
    words = _PUNCT_RE.sub(" ", (text or "").lower()).split()
    if not any(w in _SHOW_WORDS for w in words):
        return None
    for app, vocab in _APP_WORDS.items():
        known = all(w in vocab or w in _SHOW_WORDS or w in _FILLER_WORDS for w in words)
        if known and any(w in vocab for w in words):
            return app
    return None


@dataclass
class Icon:
    name: str            # as the person reads it („Google Chrome“, „Word“)
    rect: Rect
    where: str           # "taskbar" | "desktop"
    visible: bool = True # desktop: no window covers it


def clean_taskbar_name(raw: str) -> str:
    """„Google Chrome pinned“ / „Claude - 1 running window“ -> the app's name."""
    return _TASKBAR_SUFFIX_RE.sub("", raw.strip()).strip()


def best_of(icons: List[Icon], chosen: Icon) -> Icon:
    """The same app may be on the desktop and the taskbar: a visible desktop
    icon, then the taskbar button, then a covered desktop icon."""
    same = [i for i in icons if i.name.lower() == chosen.name.lower()] or [chosen]
    rank = lambda i: 0 if (i.where == "desktop" and i.visible) else 1 if i.where == "taskbar" else 2
    return min(same, key=rank)


class _Finder:
    """App icons on the screen: taskbar buttons and desktop icons (UIA)."""

    def icons(self) -> List[Icon]:
        try:
            import uiautomation as auto
        except Exception:
            log.warning("uiautomation is not installed — cannot list icons")
            return []
        out: List[Icon] = []
        try:
            with auto.UIAutomationInitializerInThread():
                out += self._taskbar_icons(auto)
                out += self._desktop_icons(auto)
        except Exception as e:
            log.debug("icon listing failed: %s", e)
        return out[:MAX_ICONS]

    def find(self, names) -> Optional[Tuple[Rect, str]]:
        """The first icon named like one of `names`: taskbar, else a visible
        desktop icon."""
        wanted = tuple(n.lower() for n in names)
        hits = [i for i in self.icons() if i.name.lower().startswith(wanted)]
        for icon in sorted(hits, key=lambda i: i.where != "taskbar"):
            if icon.where == "taskbar" or icon.visible:
                return icon.rect, icon.where
        return None

    @staticmethod
    def _walk(auto, hwnd, max_depth, types):
        t0 = time.monotonic()
        for ctrl, _depth in auto.WalkControl(auto.ControlFromHandle(hwnd), maxDepth=max_depth):
            if time.monotonic() - t0 > UIA_SEARCH_LIMIT_S:
                break
            if ctrl.ControlTypeName not in types:
                continue
            r = ctrl.BoundingRectangle
            if r.width() > 0 and r.height() > 0:
                yield (ctrl.Name or "").strip(), (r.left, r.top, r.right, r.bottom)

    def _taskbar_icons(self, auto) -> List[Icon]:
        import ctypes
        hwnd = ctypes.windll.user32.FindWindowW("Shell_TrayWnd", None)
        if not hwnd:
            return []
        out = []
        for raw, rect in self._walk(auto, hwnd, 12, ("ButtonControl",)):
            if not raw or "\n" in raw or "\r" in raw or raw.lower().startswith(_TASKBAR_SKIP):
                continue                             # tray icons, Start, Search ...
            name = clean_taskbar_name(raw)
            if name:
                out.append(Icon(name, rect, "taskbar"))
        return out

    def _desktop_icons(self, auto) -> List[Icon]:
        out = []
        for hwnd in self._desktop_hosts():
            for name, rect in self._walk(auto, hwnd, 5, ("ListItemControl",)):
                if name:
                    out.append(Icon(name, rect, "desktop", self._desktop_visible(rect)))
            if out:
                break
        return out

    @staticmethod
    def _desktop_hosts() -> List[int]:
        """Progman, and the WorkerW windows the desktop icons move to when a
        wallpaper slideshow is on."""
        import ctypes
        u = ctypes.windll.user32
        u.FindWindowExW.restype = ctypes.c_void_p
        hosts = []
        progman = u.FindWindowW("Progman", None)
        if progman:
            hosts.append(progman)
        w = None
        for _ in range(64):
            w = u.FindWindowExW(None, w, "WorkerW", None)
            if not w:
                break
            hosts.append(w)
        return hosts

    @staticmethod
    def _desktop_visible(rect: Rect) -> bool:
        """A desktop icon counts only when no window covers it."""
        from agent import executor
        _pid, cls, _title = executor.win.window_at((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
        return cls in ("Progman", "WorkerW")


finder = _Finder()


def show_icon(app: str, ui, cancel_event: Optional[threading.Event] = None,
              flight_scale: float = 1.0) -> bool:
    """Glide to the app's icon and circle it. Blocking; never raises.
    `ui` is an agent.routines.RoutineUI (its `circle` draws the ring)."""
    from agent import executor, routines
    from agent.executor import to_logical
    from timing import timed
    if app not in APPS:
        return False
    names, spoken = APPS[app]
    cancel_event = cancel_event or executor.cancel_event
    executor.win.ensure_dpi_awareness()
    with timed("show_icon.find", app=app) as tm:
        found = finder.find(names)
        tm["where"] = found[1] if found else None
    if found is None:
        ui.say(SAY_NOT_FOUND.format(name=spoken))
        return False
    (l, t, r, b), where = found
    cx, cy = (l + r) // 2, (t + b) // 2
    run = routines._Run(ui, cancel_event, flight_scale)
    ui.set_speed(routines.FLIGHT_SPEED)
    try:
        run.glide_to(cx, cy, spoken)
        lx0, ly0 = to_logical(l, t)
        lx1, ly1 = to_logical(r, b)
        lx, ly = to_logical(cx, cy)
        radius = max(lx1 - lx0, ly1 - ly0) / 2.0 * CIRCLE_PAD + 8.0
        ui.circle(float(lx), float(ly), float(radius), CIRCLE_TTL_S)
        log.info("showing %s on the %s at (%d, %d)", app, where, cx, cy)
        run.sleep(HOLD_S)
        return True
    except routines._Cancelled:
        return False
    except Exception:
        log.exception("show_icon %s failed", app)
        return False
    finally:
        if run.pointing:
            ui.point_release()
        ui.set_speed(1.0)

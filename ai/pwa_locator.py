"""
pywinauto control finder — the first tier of the autopilot's locator.

Finds a control by its visible name in the foreground window (then the
taskbar), the way pywinauto scripts do, and returns the pywinauto wrapper so
the executor can press it with pywinauto's own invoke() — no pixel guessing.

Breadth-first with a node cap and a time budget, so a huge tree (a browser
page) cannot stall it; a window that stops answering UI Automation calls is
cut off by a watchdog (measured: single lookups once hung 72 s, 122 s and
22 minutes). Coordinates are physical pixels (the app is per-monitor DPI aware).
Disable with CLICKY_USE_PYWINAUTO=0.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional

log = logging.getLogger("clicky.pwa")

MAX_NODES = 600
MAX_SECONDS = 3.0           # tree walk budget; the best match so far is kept
WATCHDOG_SECONDS = 4.0      # a single hung UIA call is abandoned after this
MIN_SCORE = 0.75
CLICKABLE = {"Button", "MenuItem", "Hyperlink", "ListItem", "TabItem", "TreeItem",
             "CheckBox", "RadioButton", "ComboBox", "Edit", "SplitButton", "Document"}


def enabled() -> bool:
    return os.getenv("CLICKY_USE_PYWINAUTO", "1").strip().lower() not in ("0", "false", "no")


@dataclass
class Found:
    wrapper: Any
    name: str
    rect: tuple                 # (left, top, right, bottom) physical px
    is_password: bool = False

    @property
    def center(self):
        l, t, r, b = self.rect
        return (l + r) // 2, (t + b) // 2


def _control_type(w) -> str:
    try:
        return str(w.element_info.control_type or "")
    except Exception:
        return ""


def _is_password(w) -> bool:
    try:
        return bool(w.element_info.element.CurrentIsPassword)
    except Exception:
        return False


def best_match(label: str, roots: Iterable, max_nodes: int = MAX_NODES,
               deadline: Optional[float] = None) -> Optional[Found]:
    """Walk the roots breadth-first; best visible control whose name matches.
    Stops at max_nodes or at `deadline` (time.monotonic())."""
    from ai.point_refiner import text_score
    best, best_score, seen = None, 0.0, 0
    queue = deque(roots)
    while queue and seen < max_nodes:
        if deadline is not None and time.monotonic() > deadline:
            log.info("pywinauto: %r time budget used up after %d nodes", label, seen)
            break
        w = queue.popleft()
        seen += 1
        try:
            name = w.window_text() or ""
            r = w.rectangle()
            visible = r.right > r.left and r.bottom > r.top
        except Exception:
            continue
        if name and visible:
            score = text_score(label, name)
            if _control_type(w) in CLICKABLE:
                score += 0.05
            if score > best_score:
                best_score = score
                best = Found(w, name, (int(r.left), int(r.top), int(r.right), int(r.bottom)),
                             _is_password(w))
                if score >= 1.0:
                    break
        try:
            queue.extend(w.children())
        except Exception:
            pass
    if best is not None and best_score >= MIN_SCORE:
        log.info("pywinauto: %r -> %r %s (score %.2f, %d nodes)",
                 label, best.name, _control_type(best.wrapper), best_score, seen)
        return best
    return None


def _roots(desktop) -> List:
    roots = []
    try:
        fg = ctypes.windll.user32.GetForegroundWindow()
        if fg:
            roots.append(desktop.window(handle=fg).wrapper_object())
    except Exception:
        pass
    try:
        roots.append(desktop.window(class_name="Shell_TrayWnd").wrapper_object())
    except Exception:
        pass
    return roots


def find_control(label: str, desktop=None) -> Optional[Found]:
    """The control named `label` in the foreground window or the taskbar.
    Runs on a helper thread so a hung window cannot block the autopilot."""
    if not label or not label.strip() or not enabled():
        return None
    box: dict = {}

    def work():
        box["found"] = _find_control(label, desktop)

    t = threading.Thread(target=work, name="pwa-lookup", daemon=True)
    t.start()
    t.join(WATCHDOG_SECONDS)
    if t.is_alive():
        log.warning("pywinauto: lookup for %r gave up after %.0f s (window not responding)",
                    label, WATCHDOG_SECONDS)
        return None
    return box.get("found")


def _find_control(label: str, desktop=None) -> Optional[Found]:
    try:
        import comtypes                       # UIA needs COM on this (worker) thread
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass                              # already initialised here
        if desktop is None:
            from pywinauto import Desktop
            desktop = Desktop(backend="uia")
        return best_match(label.strip(), _roots(desktop),
                          deadline=time.monotonic() + MAX_SECONDS)
    except ImportError:
        log.warning("pywinauto is not installed — skipping that locator tier")
    except Exception as e:
        log.debug("pywinauto lookup failed: %s", e)
    return None

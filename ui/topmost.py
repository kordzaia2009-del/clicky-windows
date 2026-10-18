"""
Keep one of the app's windows at the top of the always-on-top band.

SetWindowPos must be called with typed arguments: without argtypes ctypes
passes HWND_TOPMOST (-1) as a 32-bit int, Windows x64 reads a different
handle and the call fails with ERROR_INVALID_WINDOW_HANDLE (1400) — so the
overlay (mascot + 🍇) slowly ended up under Edge and other windows.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger("clicky.topmost")

HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010

_user32 = None
_warned = False


def _api():
    global _user32
    if _user32 is None:
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u.SetWindowPos.restype = wintypes.BOOL
        _user32 = u
    return _user32


def raise_topmost(win_id) -> bool:
    """Put the window (a Qt winId()) on top of the topmost band without
    activating it. True when Windows accepted it; never raises."""
    global _warned
    try:
        ok = bool(_api().SetWindowPos(wintypes.HWND(int(win_id)), HWND_TOPMOST, 0, 0, 0, 0,
                                      SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE))
        if not ok and not _warned:
            _warned = True
            log.warning("SetWindowPos(HWND_TOPMOST) failed: error %d", ctypes.get_last_error())
        return ok
    except Exception:
        return False                # non-Windows / no window handle yet

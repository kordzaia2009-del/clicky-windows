"""ui/topmost.py — the stay-on-top call is typed, so HWND_TOPMOST (-1)
reaches Windows intact (untyped, it failed with error 1400 and the overlay
sank under other windows). No real window is touched."""

import ctypes

import ui.topmost as tm


class FakeUser32:
    def __init__(self):
        self.calls = []

        class _F:
            argtypes = None
            restype = None

            def __call__(_self, *args):
                self.calls.append(args)
                return 1
        self.SetWindowPos = _F()


def test_raise_topmost_passes_a_real_hwnd_topmost(monkeypatch):
    fake = FakeUser32()
    monkeypatch.setattr(tm, "_user32", fake)
    assert tm.raise_topmost(12345) is True
    (hwnd, after, *_rest, flags), = fake.calls
    assert hwnd.value == 12345
    assert after.value == ctypes.c_void_p(-1).value      # full-width -1, not 0xFFFFFFFF
    assert flags == tm.SWP_NOSIZE | tm.SWP_NOMOVE | tm.SWP_NOACTIVATE


def test_raise_topmost_never_raises(monkeypatch):
    monkeypatch.setattr(tm, "_api", lambda: (_ for _ in ()).throw(OSError("no user32")))
    assert tm.raise_topmost(1) is False

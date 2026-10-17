"""
Stop window — a small separate window with one big red „გაჩერება“ button and
a green „ვმუშაობ შენს მაგივრად“ strip, shown only while the autopilot runs.

  • Not click-through (the overlay is, so it cannot host this button).
  • Never takes focus from the window being operated:
    Qt.WindowDoesNotAcceptFocus + WA_ShowWithoutActivating, NoFocus button.
  • Sits at a screen edge; before each action the autopilot announces its
    target and the window moves to another edge if it would cover it. It
    also keeps clear of the panel and re-asserts topmost so nothing of the
    app covers it.

choose_position() is pure (plain tuples) so it is unit-tested without Qt.
"""

from __future__ import annotations

from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from PyQt6.QtCore import QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget

from ui import strings_ka

Rect = Tuple[int, int, int, int]          # x, y, width, height (logical px)

EDGE_MARGIN = 16
TARGET_CLEARANCE = 60                     # keep at least this far from a target


def _hit(rect: Rect, point: Tuple[float, float], pad: int = 0) -> bool:
    x, y, w, h = rect
    return x - pad <= point[0] <= x + w + pad and y - pad <= point[1] <= y + h + pad


def _overlaps(a: Rect, b: Rect) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def candidate_positions(avail: Rect, size: Tuple[int, int]) -> List[Tuple[int, int]]:
    """Edge spots, in order of preference."""
    ax, ay, aw, ah = avail
    w, h = size
    left, right = ax + EDGE_MARGIN, ax + aw - w - EDGE_MARGIN
    top, bottom = ay + EDGE_MARGIN, ay + ah - h - EDGE_MARGIN
    cx, cy = ax + (aw - w) // 2, ay + (ah - h) // 2
    return [(cx, top), (right, top), (left, top), (left, bottom),
            (cx, bottom), (left, cy), (right, cy), (right, bottom)]


def choose_position(avail: Rect, size: Tuple[int, int],
                    target: Optional[Tuple[float, float]] = None,
                    avoid: Sequence[Rect] = (),
                    current: Optional[Tuple[int, int]] = None) -> Tuple[int, int]:
    """Where the stop window goes: clear of `target` (with clearance) and of
    every `avoid` rect (the panel). Keeps `current` when it is still fine,
    so the window does not jump between actions."""
    w, h = size

    def ok(pos, strict=True):
        r = (pos[0], pos[1], w, h)
        if target is not None and _hit(r, target, TARGET_CLEARANCE):
            return False
        return not strict or not any(_overlaps(r, a) for a in avoid)

    if current is not None and _hit(avail, current) and ok(current):
        return current
    spots = candidate_positions(avail, size)
    for strict in (True, False):           # never covering the target wins over the panel
        for pos in spots:
            if ok(pos, strict):
                return pos
    return spots[0]


class StopWindow(QWidget):
    on_stop = pyqtSignal()

    WIDTH, HEIGHT = 300, 150

    def __init__(self, avoid_rects: Callable[[], Iterable[QRect]] = lambda: ()):
        super().__init__()
        self._avoid_rects = avoid_rects
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setWindowTitle(strings_ka.STOP_WINDOW_BUTTON)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        strip = QLabel(strings_ka.STOP_WINDOW_STRIP)
        strip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        strip.setFixedHeight(38)
        strip.setStyleSheet("background: rgb(30,150,70); color: white;"
                            "font-size: 16px; font-weight: 600;")
        layout.addWidget(strip)
        self.button = QPushButton(strings_ka.STOP_WINDOW_BUTTON)
        self.button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.setToolTip(strings_ka.STOP_WINDOW_TOOLTIP)
        self.button.setStyleSheet(
            "QPushButton { background: rgb(200,30,30); color: white; border: none;"
            "font-size: 34px; font-weight: 800; }"
            "QPushButton:hover { background: rgb(225,40,40); }"
            "QPushButton:pressed { background: rgb(160,20,20); }"
        )
        self.button.clicked.connect(self.on_stop)
        layout.addWidget(self.button, stretch=1)

        self._top_timer = QTimer(self)
        self._top_timer.timeout.connect(self.keep_on_top)

    # slots (connected to CompanionManager signals; Qt queues them to us)
    def set_running(self, running: bool):
        if running:
            self.place(None)
            self.show()
            self.keep_on_top()
            self._top_timer.start(1000)
        else:
            self._top_timer.stop()
            self.hide()

    def avoid_target(self, x: float, y: float):
        """Called before each action with its target (logical px)."""
        if self.isVisible():
            self.place((x, y))

    def place(self, target: Optional[Tuple[float, float]]):
        point = QPoint(int(target[0]), int(target[1])) if target else None
        screen = (QApplication.screenAt(point) if point else None) or QApplication.primaryScreen()
        a = screen.availableGeometry()
        avoid = [(r.x(), r.y(), r.width(), r.height()) for r in self._avoid_rects()]
        current = (self.x(), self.y()) if self.isVisible() else None
        x, y = choose_position((a.x(), a.y(), a.width(), a.height()),
                               (self.WIDTH, self.HEIGHT), target, avoid, current)
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def keep_on_top(self):
        """Above the app's other topmost windows (the panel), without focus."""
        if not self.isVisible():
            return
        try:
            import ctypes
            ctypes.windll.user32.SetWindowPos(
                int(self.winId()), -1, 0, 0, 0, 0,     # HWND_TOPMOST
                0x0001 | 0x0002 | 0x0010,               # NOSIZE | NOMOVE | NOACTIVATE
            )
        except Exception:
            pass

"""
The island — an iPhone-style notification pill at the top of the screen that
says, in Georgian, what is happening right now: listening, thinking,
answering, and every autopilot action („იხსნება ზუმი“ with Zoom's icon).

  IslandModel       pure: events in, "show this / hide in N s" out (tested)
  IslandWindow      the pill: frameless, always on top, never takes focus,
                    slides down from the top, cross-fades its text, and its ×
                    stops whatever is running
  IslandController  Qt glue: CompanionManager signals -> model -> window

It is a real window (not part of the click-through overlay) so its × can be
clicked. If an autopilot target is under it, it moves to the bottom centre.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple, Union

from PyQt6.QtCore import (
    QEasingCurve, QObject, QPoint, QPointF, QPropertyAnimation, QRect, QRectF,
    Qt, QTimer, QVariantAnimation, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QFont, QFontMetrics, QLinearGradient,
    QPainter, QPen,
)
from PyQt6.QtWidgets import QApplication, QWidget

from ui import strings_ka as S

# ── Model ────────────────────────────────────────────────────────────────────

WAVE, SPINNER, PULSE, DONE, STOP = "wave", "spinner", "pulse", "done", "stop"


@dataclass
class Status:
    kind: str                       # indicator: wave | spinner | pulse | done | stop
    text: str
    app_label: str = ""             # an app being opened -> its icon
    hide_after: Optional[float] = None


@dataclass
class Hide:
    delay: float = 0.0


Update = Optional[Union[Status, Hide]]


def action_text(kind: str, label: str, say: str, app_ka: Optional[str]) -> str:
    """Georgian line for one autopilot action. Never contains typed text."""
    label = (label or "").strip()
    if kind in ("click", "double_click") and app_ka:
        return S.ISLAND_APP_OPENING.format(app=app_ka)
    if say:
        return say
    if kind == "double_click" and label:
        return S.ISLAND_DOUBLE_CLICK.format(label=label)
    if kind == "click" and label:
        return S.ISLAND_CLICK.format(label=label)
    return {"type": S.ISLAND_TYPE, "key": S.ISLAND_KEY, "scroll": S.ISLAND_SCROLL,
            "wait": S.ISLAND_WAIT}.get(kind, S.ISLAND_POINT_NO_LABEL)


class IslandModel:
    """What the island shows. The autopilot owns it while it runs; otherwise
    it follows the normal listen/think/answer states."""

    def __init__(self):
        self.agent_running = False

    def app_state(self, name: str) -> Update:
        name = (name or "").upper()
        if name == "LISTENING":
            return Status(WAVE, S.ISLAND_LISTENING)
        if name == "THINKING":
            return None if self.agent_running else Status(SPINNER, S.ISLAND_THINKING)
        if name == "SPEAKING":
            return None if self.agent_running else Status(WAVE, S.ISLAND_SPEAKING)
        if name == "IDLE":
            return None if self.agent_running else Hide(0.8)
        return None

    def set_agent_running(self, running: bool) -> Update:
        self.agent_running = bool(running)
        return None                     # done/cancelled states say goodbye

    def agent_state(self, state: str, detail: str = "") -> Update:
        return {
            "planning": Status(SPINNER, S.ISLAND_PLANNING),
            "verifying": Status(SPINNER, S.ISLAND_VERIFYING),
            "replanning": Status(SPINNER, S.ISLAND_REPLANNING),
            "awaiting_confirm": Status(PULSE, S.ISLAND_CONFIRM),
            "awaiting_private": Status(PULSE, S.ISLAND_PRIVATE),
            "done": Status(DONE, S.ISLAND_DONE, hide_after=2.5),
            "cancelled": Status(STOP, S.ISLAND_CANCELLED, hide_after=1.8),
        }.get(state)

    def agent_action(self, kind: str, label: str, say: str,
                     app_ka: Optional[str], app_label: str = "") -> Update:
        return Status(PULSE, action_text(kind, label, say, app_ka),
                      app_label=app_label if app_ka else "")

    def pointer(self, label: str) -> Update:
        if self.agent_running:
            return None                 # the action line already says it
        label = (label or "").strip()
        return Status(PULSE, S.ISLAND_POINT.format(label=label) if label
                      else S.ISLAND_POINT_NO_LABEL)


# ── Window ───────────────────────────────────────────────────────────────────

class IslandWindow(QWidget):
    on_close = pyqtSignal()

    PILL_H = 76
    PAD = 16                 # room for the soft shadow around the pill
    MIN_W, MAX_W = 300, 720
    ICON = 46
    TOP_GAP = 10
    CLOSE = 34
    RADIUS = 22              # rounded rectangle, like an iPhone notification

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.setToolTip(S.ISLAND_CLOSE_TIP)

        self._font = QFont()
        self._font.setFamilies(["Segoe UI", "Sylfaen"])
        self._font.setPixelSize(26)
        self._font.setWeight(QFont.Weight.DemiBold)

        self.kind = PULSE
        self.text = ""
        self._old_text = ""
        self._fade = 1.0
        self._icon_pm = None
        self._level = 0.0
        self._phase = 0.0
        self._hover_close = False
        self._hiding = False
        self.at_bottom = False

        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._animate)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide_island)
        self._top_timer = QTimer(self)
        self._top_timer.timeout.connect(self.keep_on_top)

        self._pos_anim = QPropertyAnimation(self, b"pos", self)
        self._pos_anim.finished.connect(self._after_slide)
        self._op_anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._width_anim = QVariantAnimation(self)
        self._width_anim.valueChanged.connect(self._set_width)
        self._fade_anim = QVariantAnimation(self)
        self._fade_anim.valueChanged.connect(self._set_fade)

    # geometry
    def _content_width(self, text: str, has_icon: bool, kind: str) -> int:
        fm = QFontMetrics(self._font)
        indicator = {WAVE: 28, SPINNER: 22, PULSE: 8, DONE: 22, STOP: 20}.get(kind, 8)
        w = 18 + (self.ICON + 10 if has_icon else 0) + indicator + 12 \
            + fm.horizontalAdvance(text) + 16 + self.CLOSE + 14
        return max(self.MIN_W, min(self.MAX_W, w)) + 2 * self.PAD

    def _home(self, width: int) -> QPoint:
        a = QApplication.primaryScreen().availableGeometry()
        x = a.center().x() - width // 2
        if self.at_bottom:
            y = a.bottom() - self.PILL_H - self.PAD - 90       # above the taskbar & mascot row
        else:
            y = a.top() + self.TOP_GAP - self.PAD
        return QPoint(x, y)

    def pill_rect_global(self) -> QRect:
        return QRect(self.x() + self.PAD, self.y() + self.PAD,
                     self.width() - 2 * self.PAD, self.PILL_H)

    def _close_rect(self) -> QRectF:
        r = self.rect().adjusted(self.PAD, self.PAD, -self.PAD, -self.PAD)
        return QRectF(r.right() - 14 - self.CLOSE, r.center().y() - self.CLOSE / 2 + 1,
                      self.CLOSE, self.CLOSE)

    # public API (slots)
    def show_status(self, kind: str, text: str, icon=None, hide_after: Optional[float] = None):
        self._hide_timer.stop()
        self._hiding = False
        pm = None
        if icon is not None and not icon.isNull():
            # Logical size + screen scale: Qt picks the sharpest variant and
            # sets the pixmap's own DPR (overriding it made the icon huge).
            from PyQt6.QtCore import QSize
            dpr = self.devicePixelRatioF() or QApplication.primaryScreen().devicePixelRatio() or 1.0
            pm = icon.pixmap(QSize(self.ICON, self.ICON), dpr)
            logical = pm.width() / (pm.devicePixelRatio() or 1.0)
            if pm.isNull():
                pm = None
            elif abs(logical - self.ICON) > 1:
                pm = pm.scaled(int(self.ICON * dpr), int(self.ICON * dpr),
                               Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
                pm.setDevicePixelRatio(dpr)
        width = self._content_width(text, pm is not None, kind)
        self.kind, self._icon_pm = kind, pm
        if not self.isVisible():
            self.text, self._old_text, self._fade = text, "", 1.0
            self.resize(width, self.PILL_H + 2 * self.PAD)
            home = self._home(width)
            self.move(home.x(), home.y() - self.PILL_H - 2 * self.PAD - self.TOP_GAP)
            self.setWindowOpacity(0.0)
            self.show()
            self.keep_on_top()
            self._top_timer.start(1000)
            self._slide_to(home, 420, QEasingCurve.Type.OutCubic)
            self._animate_opacity(1.0, 300)
        else:
            if text != self.text:
                self._old_text, self.text = self.text, text
                self._fade_anim.stop()
                self._fade_anim.setStartValue(0.0)
                self._fade_anim.setEndValue(1.0)
                self._fade_anim.setDuration(240)
                self._fade_anim.start()
            if width != self.width():
                self._width_anim.stop()
                self._width_anim.setStartValue(self.width())
                self._width_anim.setEndValue(width)
                self._width_anim.setDuration(260)
                self._width_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
                self._width_anim.start()
            if self._op_anim.endValue() != 1.0 or self.windowOpacity() < 1.0:
                self._animate_opacity(1.0, 200)
        self._anim_timer.start(16)
        if hide_after:
            self._hide_timer.start(int(hide_after * 1000))
        self.update()

    def hide_island(self, delay: float = 0.0):
        if delay:
            self._hide_timer.start(int(delay * 1000))
            return
        if not self.isVisible() or self._hiding:
            return
        self._hiding = True
        up = QPoint(self.x(), self.y() - self.PILL_H - self.PAD - self.TOP_GAP)
        self._slide_to(up, 300, QEasingCurve.Type.InCubic)
        self._animate_opacity(0.0, 260)

    def set_level(self, rms: float):
        self._level = self._level * 0.6 + min(1.0, max(0.0, rms) * 6) * 0.4

    def avoid_target(self, x: float, y: float):
        """Before an autopilot action: never sit on its target."""
        if not self.isVisible():
            return
        was = self.at_bottom
        self.at_bottom = False
        top = self._home(self.width())                   # where it sits at the top
        top_pill = QRect(top.x() + self.PAD, top.y() + self.PAD,
                         self.width() - 2 * self.PAD, self.PILL_H)
        self.at_bottom = top_pill.adjusted(-60, -60, 60, 60).contains(int(x), int(y))
        if self.at_bottom != was:
            self._slide_to(self._home(self.width()), 320, QEasingCurve.Type.OutCubic)

    def keep_on_top(self):
        if not self.isVisible():
            return
        try:
            import ctypes
            ctypes.windll.user32.SetWindowPos(int(self.winId()), -1, 0, 0, 0, 0,
                                              0x0001 | 0x0002 | 0x0010)
        except Exception:
            pass

    # animation plumbing
    def _slide_to(self, target: QPoint, ms: int, curve):
        self._pos_anim.stop()
        self._pos_anim.setStartValue(self.pos())
        self._pos_anim.setEndValue(target)
        self._pos_anim.setDuration(ms)
        self._pos_anim.setEasingCurve(curve)
        self._pos_anim.start()

    def _animate_opacity(self, value: float, ms: int):
        self._op_anim.stop()
        self._op_anim.setStartValue(self.windowOpacity())
        self._op_anim.setEndValue(value)
        self._op_anim.setDuration(ms)
        self._op_anim.start()

    def _after_slide(self):
        if self._hiding:
            self.hide()
            self._anim_timer.stop()
            self._top_timer.stop()
            self._hiding = False
            self.at_bottom = False

    def _set_width(self, w):
        w = int(w)
        x = self.x() + (self.width() - w) // 2           # grow around the centre
        self.setGeometry(x, self.y(), w, self.height())

    def _set_fade(self, v):
        self._fade = float(v)
        self.update()

    def _animate(self):
        self._phase += 0.12
        self.update()

    # input
    def mouseMoveEvent(self, e):
        hover = self._close_rect().contains(e.position())
        if hover != self._hover_close:
            self._hover_close = hover
            self.setCursor(Qt.CursorShape.PointingHandCursor if hover
                           else Qt.CursorShape.ArrowCursor)
            self.update()

    def leaveEvent(self, e):
        self._hover_close = False
        self.update()

    def mousePressEvent(self, e):
        if self._close_rect().contains(e.position()):
            self.on_close.emit()

    # painting
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        pill = QRectF(self.rect().adjusted(self.PAD, self.PAD, -self.PAD, -self.PAD))
        radius = self.RADIUS

        # soft shadow
        p.setPen(Qt.PenStyle.NoPen)
        for grow, a in ((10, 10), (6, 16), (3, 24)):
            p.setBrush(QColor(0, 0, 0, a))
            p.drawRoundedRect(pill.adjusted(-grow, -grow + 3, grow, grow + 3),
                              radius + grow, radius + grow)
        # dark glass
        fill = QLinearGradient(pill.topLeft(), pill.bottomLeft())
        fill.setColorAt(0.0, QColor(44, 44, 52, 236))
        fill.setColorAt(1.0, QColor(18, 18, 24, 240))
        p.setBrush(QBrush(fill))
        p.setPen(QPen(QColor(255, 255, 255, 48), 1.0))
        p.drawRoundedRect(pill, radius, radius)

        x = pill.left() + 16
        cy = pill.center().y()
        if self._icon_pm is not None:
            p.drawPixmap(QPointF(x, cy - self.ICON / 2), self._icon_pm)
            x += self.ICON + 10
        x = self._paint_indicator(p, x, cy) + 12

        # text, cross-fading from the previous line
        p.setFont(self._font)
        text_w = self._close_rect().left() - 12 - x
        fm = QFontMetrics(self._font)
        for txt, alpha in ((self._old_text, 1.0 - self._fade), (self.text, self._fade)):
            if not txt or alpha <= 0.01:
                continue
            p.setPen(QColor(255, 255, 255, int(255 * alpha)))
            p.drawText(QRectF(x, pill.top(), max(10, text_w), pill.height()),
                       int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                       fm.elidedText(txt, Qt.TextElideMode.ElideRight, int(max(10, text_w))))

        # close ×
        c = self._close_rect()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 60 if self._hover_close else 32))
        p.drawEllipse(c)
        pen = QPen(QColor(255, 255, 255, 220 if self._hover_close else 170), 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        k = 6.0
        cc = c.center()
        p.drawLine(QPointF(cc.x() - k, cc.y() - k), QPointF(cc.x() + k, cc.y() + k))
        p.drawLine(QPointF(cc.x() + k, cc.y() - k), QPointF(cc.x() - k, cc.y() + k))
        p.end()

    def _paint_indicator(self, p, x: float, cy: float) -> float:
        """Draws the small activity mark; returns the x after it."""
        t = self._phase
        if self.kind == WAVE:
            colors = [QColor(90, 205, 255), QColor(110, 160, 255), QColor(150, 120, 255),
                      QColor(110, 160, 255), QColor(90, 205, 255)]
            profile = (0.5, 0.8, 1.0, 0.8, 0.5)
            for i in range(5):
                h = 5 + 18 * profile[i] * min(1.0, self._level + 0.25 * (math.sin(t * 1.7 + i * 0.9) + 1) / 2)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(colors[i])
                p.drawRoundedRect(QRectF(x + i * 6, cy - h / 2, 3.2, h), 1.6, 1.6)
            return x + 28
        if self.kind == SPINNER:
            r = 9.0
            ang = -math.degrees(t * 2.2)
            grad = QConicalGradient(QPointF(x + r, cy), ang)
            grad.setColorAt(0.0, QColor(140, 175, 255, 255))
            grad.setColorAt(0.6, QColor(190, 120, 255, 0))
            pen = QPen(QBrush(grad), 2.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(QRectF(x, cy - r, 2 * r, 2 * r), int(ang * 16), int(280 * 16))
            return x + 22
        if self.kind == DONE:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(52, 199, 89))
            p.drawEllipse(QPointF(x + 10, cy), 10, 10)
            pen = QPen(QColor(255, 255, 255), 2.2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawPolyline([QPointF(x + 5.5, cy), QPointF(x + 9, cy + 3.5), QPointF(x + 14.5, cy - 3.5)])
            return x + 22
        if self.kind == STOP:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(200, 200, 210))
            p.drawRoundedRect(QRectF(x + 2, cy - 7, 14, 14), 3, 3)
            return x + 20
        # PULSE: the slim blue activity bar
        a = int(170 + 85 * (math.sin(t * 1.4) + 1) / 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(40, 145, 255, a))
        p.drawRoundedRect(QRectF(x, cy - 15, 5, 30), 2.5, 2.5)
        return x + 9


# ── Glue ─────────────────────────────────────────────────────────────────────

class IslandController(QObject):
    """CompanionManager signals -> IslandModel -> IslandWindow. All slots run
    on the GUI thread (Qt queues the manager's cross-thread signals)."""

    def __init__(self, window: IslandWindow,
                 resolve_app: Optional[Callable] = None,
                 icon_for: Optional[Callable] = None):
        super().__init__()
        from ui import app_icons
        self.window = window
        self.model = IslandModel()
        self._resolve = resolve_app or app_icons.resolve
        self._icon_for = icon_for or app_icons.icon_for

    def _apply(self, update: Update, icon=None):
        if update is None:
            return
        if isinstance(update, Hide):
            self.window.hide_island(update.delay)
            return
        self.window.show_status(update.kind, update.text, icon, update.hide_after)

    def on_app_state(self, state):
        self._apply(self.model.app_state(getattr(state, "name", str(state))))

    def on_agent_running(self, running: bool):
        self._apply(self.model.set_agent_running(running))

    def on_agent_state(self, state: str, detail: str):
        self._apply(self.model.agent_state(state, detail))

    def on_agent_action(self, kind: str, label: str, say: str):
        app = self._resolve(label) if kind in ("click", "double_click") and label else None
        update = self.model.agent_action(kind, label, say,
                                         app.name_ka if app else None,
                                         app.label if app else "")
        self._apply(update, self._icon_for(app) if app else None)

    def on_point(self, x: float, y: float, label: str):
        self._apply(self.model.pointer(label))

    def on_level(self, rms: float):
        self.window.set_level(rms)

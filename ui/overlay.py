"""
Clicky's blue buddy cursor — faithful port of the original macOS overlay.

Matches clicky-main/leanring-buddy/OverlayWindow.swift:
  - Flat solid blue equilateral triangle (#3380FF), 16×16, rotated -35°
  - Sits at (+35, +25) relative to the real cursor
  - Soft blue drop shadow (radius ~8)
  - States cross-fade in place:
      idle / speaking → triangle
      listening       → 5-bar waveform
      thinking        → rotating arc spinner
      pointing        → triangle + speech bubble ("found it!" etc.)
"""

import math
import os
import random
import time
from typing import Optional

from PyQt6.QtWidgets import QWidget, QApplication
from PyQt6.QtCore import Qt, QTimer, QPointF, QRectF
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QPainterPath, QCursor, QFont,
    QPixmap, QRadialGradient, QConicalGradient, QFontMetrics,
)

from ui.mascot import (
    BUBBLE_MAX_W, MascotAnimator, bubble_geometry, load_frames, revealed_text,
)

try:   # loudness of the speech being played, for the mascot's mouth
    from audio import playback as _playback
except Exception:      # no audio stack (tests) — the mascot just idles
    _playback = None


MODE_IDLE      = "idle"
MODE_LISTENING = "listening"
MODE_THINKING  = "thinking"
MODE_SPEAKING  = "speaking"


# Exactly matches Swift source: buddyX = swiftUIPosition.x + 35; buddyY = +25
OFFSET_X = 35
OFFSET_Y = 25

# Triangle bounding box edge (Swift: frame(width: 16, height: 16))
TRI_SIZE = 16
# Swift: .rotationEffect(.degrees(-35))
TRI_ROTATION_DEG = -35.0

# #3380FF
CURSOR_BLUE = QColor(0x33, 0x80, 0xFF)

# Teaching-annotation palette. The LLM picks colors by name; default is the
# Clicky blue. Chosen for contrast on both light and dark content.
ANNOT_COLORS = {
    "blue":   QColor(0x33, 0x80, 0xFF),
    "red":    QColor(0xFF, 0x45, 0x55),
    "green":  QColor(0x2E, 0xCC, 0x71),
    "yellow": QColor(0xFF, 0xD5, 0x4F),
    "orange": QColor(0xFF, 0x8C, 0x2E),
    "purple": QColor(0xB5, 0x6C, 0xFF),
    "white":  QColor(0xF5, 0xF7, 0xFA),
    "cyan":   QColor(0x4F, 0xD9, 0xFF),
}

# Progressive stroke animation — shapes draw at "hand speed" (px/sec), so a
# long line takes visibly longer than a short tick mark. Queued sequentially;
# the buddy cursor rides the pen tip while each stroke draws.
STROKE_SPEED_PX_S   = 420.0
SHAPE_DRAW_MIN_S    = 0.6
SHAPE_DRAW_MAX_S    = 2.2
SHAPE_GAP_SECONDS   = 0.28

_TEXT_SIZES = {"s": 12, "m": 17, "l": 26}


def _shape_path_pts(shape: dict):
    """(pts, closed) polyline for stroke-able shapes; None for circle/text."""
    kind = shape.get("kind")
    if kind in ("line", "arrow"):
        return list(shape["pts"]), False
    if kind == "poly":
        return list(shape["pts"]), True
    if kind == "rect":
        x1, y1, x2, y2 = shape["x1"], shape["y1"], shape["x2"], shape["y2"]
        return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)], True
    if kind == "underline":
        return [(shape["x"], shape["y"]),
                (shape["x"] + shape["w"], shape["y"])], False
    if kind == "angle":
        s = shape.get("s", 22)
        rot = math.radians(shape.get("rot", 0))
        x, y = shape["x"], shape["y"]
        d1 = (math.cos(rot), math.sin(rot))
        d2 = (math.cos(rot + math.pi / 2), math.sin(rot + math.pi / 2))
        return [(x + d1[0] * s, y + d1[1] * s),
                (x + (d1[0] + d2[0]) * s, y + (d1[1] + d2[1]) * s),
                (x + d2[0] * s, y + d2[1] * s)], False
    return None, False


def _shape_length(shape: dict) -> float:
    """Approximate stroke length in logical px — drives draw duration."""
    kind = shape.get("kind")
    if kind == "circle":
        return 2 * math.pi * shape.get("r", 30)
    if kind == "text":
        return 90.0   # fixed short reveal
    pts, closed = _shape_path_pts(shape)
    if not pts or len(pts) < 2:
        return 60.0
    if closed:
        pts = pts + [pts[0]]
    return sum(math.hypot(b[0] - a[0], b[1] - a[1])
               for a, b in zip(pts, pts[1:]))


def _partial_pts(pts, closed, u):
    """First u-fraction of a polyline. Returns the drawn point list."""
    if closed and len(pts) >= 3:
        pts = list(pts) + [pts[0]]
    if len(pts) < 2:
        return list(pts)
    seg_lens = [math.hypot(b[0] - a[0], b[1] - a[1])
                for a, b in zip(pts, pts[1:])]
    total = sum(seg_lens)
    if total <= 0:
        return [pts[0]]
    budget = total * min(1.0, max(0.0, u))
    drawn = [pts[0]]
    for (a, b), L in zip(zip(pts, pts[1:]), seg_lens):
        if budget <= 0:
            break
        if L <= budget:
            drawn.append(b)
            budget -= L
        else:
            t = budget / L
            drawn.append((a[0] + (b[0] - a[0]) * t,
                          a[1] + (b[1] - a[1]) * t))
            budget = 0
    return drawn


def _stroke_tip(shape: dict, u: float):
    """Current pen-tip position of a shape at progress u — where the buddy
    hovers while 'drawing'. Returns (x, y) in logical screen coords."""
    kind = shape.get("kind")
    if kind == "circle":
        a = math.radians(90 - 360 * u)   # clockwise from 12 o'clock
        return (shape["x"] + shape["r"] * math.cos(a),
                shape["y"] - shape["r"] * math.sin(a))
    if kind == "text":
        return (shape["x"], shape["y"])
    pts, closed = _shape_path_pts(shape)
    if not pts:
        return None
    drawn = _partial_pts(pts, closed, u)
    return drawn[-1] if drawn else None

# Pointing phrases from the original
POINTER_PHRASES = (
    "აი აქ!", "აი ეს!", "აქეთ!",
    "აქ დააჭირეთ!", "ეს არის!", "ვიპოვე!",
)

# Caption of what Clicky heard — shown large at the bottom of the screen as
# soon as speech is recognised, so the user can check it before the answer.
CAPTION_FONT_PX   = 34
CAPTION_HEADER    = "თქვენ თქვით:"
CAPTION_MAX_CHARS = 220
CAPTION_FADE_IN_S = 0.25
CAPTION_FADE_OUT_S = 0.6
CAPTION_LINGER_S  = 5.0     # stays this long after the answer finishes
CAPTION_BG_ALPHA  = 128     # 0-255; half see-through so the screen shows behind it
AGENT_CAPTION_MIN_S = 4.0   # autopilot lines: shown at least this long...
AGENT_CAPTION_CHARS_PER_S = 12.0   # ...plus reading time for elderly users

# The 🍇 pointer, its golden comet trail, and the mascot
GRAPE_RADIUS = 22              # logical px, frosted circle around the grapes
TRAIL_SECONDS = 0.45           # how much of the flight path stays visible
TRAIL_GOLD = (244, 192, 92)
# The taskbar arrow and the ring round a target use the same orange as the
# pointer's glow while it answers (they used to be Clicky blue).
POINT_ACCENT = QColor(*TRAIL_GOLD)
MASCOT_SIZE = 150              # logical px
MASCOT_MARGIN = 18
MASCOT_ENABLED = os.getenv("CLICKY_MASCOT", "1").strip() not in ("0", "false", "no")
SAY_LINGER_S = 3.0             # the speech bubble stays this long after the words end
SAY_FADE_IN_S, SAY_FADE_OUT_S = 0.18, 0.4
SAY_MAX_LINES = 5

# Teacher-pace pointing timings (seconds)
# Flight duration scales with distance but stays slow enough to follow visually.
FLY_DURATION_MIN = 1.6
FLY_DURATION_MAX = 2.8
DWELL_SECONDS    = 4.0    # sits on the element while the LLM explains
RETURN_DURATION  = 1.4

# Pointing state machine
_PHASE_FOLLOW    = "follow"
_PHASE_FLYING    = "flying"
_PHASE_DWELLING  = "dwelling"
_PHASE_RETURNING = "returning"


def render_grape_pixmap(dpr: float = 1.0) -> QPixmap:
    """The 🍇 pointer: a frosted white disc holding a little bunch of grapes
    (painted, not an emoji, so it looks the same on every PC)."""
    pad = 6
    side = 2 * (GRAPE_RADIUS + pad)
    pm = QPixmap(int(round(side * dpr)), int(round(side * dpr)))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = QPointF(side / 2, side / 2)
    # soft shadow + frosted disc
    for r_add, a in ((4, 18), (2, 30)):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, a))
        p.drawEllipse(c + QPointF(0, 1.5), GRAPE_RADIUS + r_add, GRAPE_RADIUS + r_add)
    disc = QRadialGradient(c + QPointF(-4, -5), GRAPE_RADIUS * 1.4)
    disc.setColorAt(0.0, QColor(255, 255, 255, 245))
    disc.setColorAt(1.0, QColor(225, 228, 240, 225))
    p.setBrush(QBrush(disc))
    p.setPen(QPen(QColor(255, 255, 255, 255), 1.2))
    p.drawEllipse(c, GRAPE_RADIUS, GRAPE_RADIUS)
    # the bunch is drawn for a 17 px disc and scaled to the real size
    k = GRAPE_RADIUS / 17.0
    p.translate(c)
    p.scale(k, k)
    p.translate(-c)
    # stem + leaf
    p.setPen(QPen(QColor(110, 70, 40), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(c + QPointF(0.5, -6.5), c + QPointF(2.0, -10.5))
    leaf = QPainterPath()
    leaf.moveTo(c + QPointF(2.0, -9.5))
    leaf.cubicTo(c + QPointF(5.5, -13.0), c + QPointF(9.0, -10.5), c + QPointF(8.5, -7.0))
    leaf.cubicTo(c + QPointF(6.0, -6.0), c + QPointF(3.5, -7.0), c + QPointF(2.0, -9.5))
    p.setPen(QPen(QColor(60, 130, 50), 0.8))
    p.setBrush(QColor(110, 190, 75))
    p.drawPath(leaf)
    # the bunch: rows of grapes, top to bottom
    r = 3.4
    for gx, gy in ((-5.6, -3.6), (0.0, -3.9), (5.6, -3.6),
                   (-2.8, 1.6), (2.8, 1.6), (0.0, 6.8)):
        g = QRadialGradient(c + QPointF(gx - 1.1, gy - 1.2), r * 1.6)
        g.setColorAt(0.0, QColor(196, 150, 240))
        g.setColorAt(0.45, QColor(122, 62, 190))
        g.setColorAt(1.0, QColor(66, 22, 118))
        p.setPen(QPen(QColor(50, 15, 90, 180), 0.6))
        p.setBrush(QBrush(g))
        p.drawEllipse(c + QPointF(gx, gy), r, r)
    p.end()
    return pm


class CursorOverlay(QWidget):

    def __init__(self):
        super().__init__()

        # Spring follow state
        self._display_pos = QPointF(0, 0)
        self._vel = QPointF(0, 0)
        self._mode: str = MODE_IDLE
        self._audio_level: float = 0.0
        self._phase: float = 0.0

        # Pointing / speech bubble
        self._locked_pos: Optional[QPointF] = None
        self._bubble_text: str = ""
        self._bubble_alpha: float = 0.0
        self._bubble_scale: float = 0.5
        self._rotation_deg: float = TRI_ROTATION_DEG

        # Bezier flight state (teacher pace)
        self._flight_phase: str = _PHASE_FOLLOW
        self._fly_start_pos = QPointF(0, 0)
        self._fly_end_pos   = QPointF(0, 0)
        self._fly_control   = QPointF(0, 0)
        self._fly_t0: float = 0.0
        self._fly_duration: float = 1.8
        self._flight_scale: float = 1.0
        self._dwell_until: float = 0.0
        # When True, dwell never expires — manager releases after TTS finishes
        self._hold_dwell: bool = False

        # Slow mode — doubles flight + dwell so Clicky feels more like a teacher
        self._slow_mode: bool = False
        self._agent_pace: float = 1.0      # < 1 while the autopilot runs
        self._last_still_sig = None        # last idle frame painted (see _tick)

        # Optional highlight ring around detected element (x, y, radius)
        self._ring: Optional[tuple] = None
        self._ring_phase: float = 0.0
        # Set when the target sits on the taskbar: the buddy parks just above
        # it and draws an arrow down to this exact point instead.
        self._arrow_target: Optional[QPointF] = None

        # Teaching annotations — list of shape dicts the overlay paints each
        # tick. Shapes animate in sequentially (progressive strokes) and
        # persist until clear_annotations() — a lesson stays on screen while
        # the student studies it.
        self._annotations: list[dict] = []
        self._draw_queue_end: float = 0.0   # when the last queued stroke finishes
        self._last_tip = None               # pen position held between strokes

        # Thinking spinner phase
        self._spin_phase: float = 0.0

        # 🍇 pointer (pre-rendered once per DPI) + golden flight trail
        self._grape_pm = None
        self._trail: list = []            # [(t, x, y)] global logical px

        # Mascot (bottom-left): frame chosen every tick from speech + blinks
        self._mascot = MascotAnimator()
        self._mascot_frames = None        # loaded lazily (needs the DPR)
        self._mascot_frame = "idle_eyes_open"

        # Speech bubble: the line being spoken, revealed in step with the audio
        self._say_line = ""
        self._say_text = ""               # the part already spoken
        self._say_gen = 0                 # playback clip number when the line started
        self._say_done = False
        self._say_t0 = 0.0
        self._say_hide_at = float("inf")
        self._say_rect = None             # global QRectF while visible

        # Recognised-speech caption
        self._caption_text: str = ""
        self._caption_t0: float = 0.0
        self._caption_hide_at: float = float("inf")
        self._caption_area = None   # QRect of the screen the user is on
        self._caption_header: str = CAPTION_HEADER

        # Transparent click-through, covers all monitors
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._cover_all_monitors()

        # Seed position so we don't flash at (0, 0)
        qp = QCursor.pos()
        self._display_pos = QPointF(qp.x() + OFFSET_X, qp.y() + OFFSET_Y)

        # 60 FPS
        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start(16)

        # The panel and dialogs are always-on-top windows too, and whichever
        # was raised last wins — so the pointer used to vanish behind the
        # panel. Re-assert the overlay's place at the top of the stack.
        self._top_timer = QTimer(self)
        self._top_timer.timeout.connect(self.keep_on_top)
        self._top_timer.start(1000)

        # Release bubble / pointing
        self._lock_timer = QTimer(self)
        self._lock_timer.setSingleShot(True)
        self._lock_timer.timeout.connect(self._release_lock)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_mode(self, mode: str):
        self._mode = mode
        if mode == MODE_LISTENING:
            # A new question is starting — drop the previous caption.
            self._caption_text = ""
        elif mode == MODE_IDLE and self._caption_text:
            self._caption_hide_at = min(
                self._caption_hide_at, time.monotonic() + CAPTION_LINGER_S
            )

    def show_caption(self, text: str, header: str = CAPTION_HEADER,
                     hold_s: Optional[float] = None):
        """Show text in large type at the bottom of the screen. Without
        hold_s it stays until the answer ends (the user's own question);
        with hold_s it fades out after that many seconds."""
        text = " ".join(text.split())
        if len(text) > CAPTION_MAX_CHARS:
            text = text[:CAPTION_MAX_CHARS].rstrip() + "…"
        self._caption_text = text
        self._caption_header = header
        self._caption_t0 = time.monotonic()
        self._caption_hide_at = (float("inf") if hold_s is None
                                 else self._caption_t0 + hold_s)
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        self._caption_area = screen.availableGeometry()

    def on_speaking(self, text: str):
        """A spoken line starts (text) or ends ("") — from ReportingTTS."""
        now = time.monotonic()
        if text:
            self._say_line, self._say_done = text, False
            self._say_gen = _playback.playback_progress()[0] if _playback else 0
            if not self._say_text:
                self._say_t0 = now           # pop in only when it was hidden
            self._say_text = ""
            self._say_hide_at = float("inf")
        else:
            self._say_done = True
            self._say_hide_at = now + SAY_LINGER_S

    def occupied_rects(self):
        """Screen areas the mascot and its bubble cover (global logical px):
        the stop window and panel keep off them."""
        from PyQt6.QtCore import QRect
        rects = [self.mascot_rect()]
        if self._say_rect is not None:
            rects.append(self._say_rect.toAlignedRect())
        return rects

    def show_agent_caption(self, text: str):
        """What the autopilot says, as the same large caption. During a run
        the app stays idle, so it times out by length instead of waiting for
        the end of an answer."""
        from ui.strings_ka import AGENT_CAPTION_HEADER
        self.show_caption(text, AGENT_CAPTION_HEADER,
                          hold_s=AGENT_CAPTION_MIN_S + len(text) / AGENT_CAPTION_CHARS_PER_S)

    def set_audio_level(self, rms: float):
        # Match Swift easing: eased = pow(min(rms*2.85, 1), 0.76)
        norm = max(rms - 0.008, 0.0)
        eased = min(norm * 2.85, 1.0) ** 0.76
        self._audio_level = self._audio_level * 0.55 + eased * 0.45

    def point_at(self, x: float, y: float, label: str = ""):
        """Fly to an on-screen target at teacher pace, dwell, then fly back.

        (x, y) is the EXACT pixel of the UI element in logical screen space
        (same space Qt's QCursor.pos() uses). The buddy lands with the tip of
        its triangle on that pixel — the highlight ring marks the exact spot."""
        # Buddy's tip should sit on the target pixel. The triangle is drawn
        # centred on _display_pos, so we just plant _display_pos there —
        # unless the target is on the taskbar (see _placement).
        self._locked_pos, self._arrow_target = self._placement(x, y)
        self._bubble_text = label or random.choice(POINTER_PHRASES)
        self._bubble_scale = 0.5
        self._bubble_alpha = 0.0
        # Halo ring around the actual target pixel
        self._ring = (x, y, 26.0)
        self._ring_phase = 0.0
        self._lock_timer.stop()   # dwell controlled by phase machine, not timer
        self._begin_flight(self._display_pos, self._locked_pos, _PHASE_FLYING)

    def retarget(self, x: float, y: float):
        """Nudge the current pointing target (e.g. after snapping it onto the
        matching on-screen text) without restarting the whole gesture."""
        if self._locked_pos is None or self._flight_phase == _PHASE_RETURNING:
            return
        anchor, self._arrow_target = self._placement(x, y)
        self._ring = (x, y, 26.0)
        self._locked_pos = anchor
        if self._flight_phase == _PHASE_FLYING:
            self._fly_end_pos = QPointF(anchor)   # the curve bends to the new end
        else:
            self._begin_flight(self._display_pos, anchor, _PHASE_FLYING)

    @staticmethod
    def _placement(x: float, y: float):
        """Where the buddy should sit for a target at (x, y), and the arrow
        target if it can't sit on the target itself.

        The taskbar is a topmost shell window that can cover the buddy, so a
        target outside the screen's available area (= on the taskbar or another
        docked bar, on whichever edge it lives) gets the buddy parked just
        inside the free area with an arrow pointing at the exact spot."""
        from PyQt6.QtCore import QPoint
        target = QPointF(x, y)
        screen = QApplication.screenAt(QPoint(int(x), int(y)))
        if screen is None:
            return target, None
        avail = screen.availableGeometry()
        if avail.contains(QPoint(int(x), int(y))):
            return target, None
        inset_x, inset_y = 50, 70
        ax = min(max(x, avail.left() + inset_x), avail.right() - inset_x)
        ay = min(max(y, avail.top() + inset_y), avail.bottom() - inset_y)
        return QPointF(ax, ay), target

    def set_slow_mode(self, enabled: bool):
        """Doubles flight + dwell duration so students can track the motion."""
        self._slow_mode = enabled

    def set_agent_running(self, running: bool):
        """Autopilot flights are quicker; the executor waits the same scale."""
        from agent.executor import AGENT_FLIGHT_SCALE
        self._agent_pace = AGENT_FLIGHT_SCALE if running else 1.0

    def set_point_hold(self, hold: bool):
        """Called by manager when TTS starts (True) / ends (False).
        While held, dwell never auto-expires — the buddy stays on the element
        the entire time Clicky speaks."""
        self._hold_dwell = hold
        if hold and self._flight_phase == _PHASE_DWELLING:
            self._dwell_until = float("inf")

    def release_point(self):
        """Manager signals that TTS is done — fly buddy back to cursor now.
        Teaching drawings are NOT cleared here — they persist so the student
        can keep studying them; the manager clears them on the next query."""
        self._hold_dwell = False
        if self._flight_phase == _PHASE_DWELLING:
            self._dwell_until = 0.0   # expires this tick → triggers return
        # Fade ring on next paint
        self._ring = None
        self._arrow_target = None

    # ── Teaching annotations ─────────────────────────────────────────────────

    def add_shape(self, shape: dict):
        """Queue a shape for progressive drawing. Logical screen coords.

        shape: {"kind": "line"|"arrow"|"circle"|"rect"|"poly"|"text"|
                        "underline"|"angle",
                ...kind-specific fields...,
                "color": palette name (optional),
                "ttl": seconds or None (None = persist until cleared)}

        Shapes animate in arrival order: each waits for the previous stroke
        to finish, like a hand drawing on a board.
        """
        now = time.monotonic()
        start = max(now, self._draw_queue_end)
        shape = dict(shape)
        # Draw at hand speed — long strokes take visibly longer
        dur = _shape_length(shape) / STROKE_SPEED_PX_S
        dur = max(SHAPE_DRAW_MIN_S, min(SHAPE_DRAW_MAX_S, dur))
        shape["start"] = start
        shape["dur"] = dur
        shape.setdefault("color", "blue")
        shape.setdefault("ttl", None)
        self._draw_queue_end = start + dur + SHAPE_GAP_SECONDS
        self._annotations.append(shape)

    # Legacy single-shape helpers (kept for compatibility with old signals)
    def add_arrow(self, x1, y1, x2, y2, ttl=None):
        self.add_shape({"kind": "arrow", "pts": [(x1, y1), (x2, y2)], "ttl": ttl})

    def add_circle(self, x, y, radius=30.0, ttl=None):
        self.add_shape({"kind": "circle", "x": x, "y": y, "r": radius, "ttl": ttl})

    def add_underline(self, x, y, width, ttl=None):
        self.add_shape({"kind": "underline", "x": x, "y": y, "w": width, "ttl": ttl})

    def add_text(self, x, y, text, ttl=None):
        self.add_shape({"kind": "text", "x": x, "y": y, "text": text,
                        "size": "s", "ttl": ttl})

    def clear_annotations(self):
        self._annotations = []
        self._draw_queue_end = 0.0
        self._last_tip = None

    def _active_stroke_tip(self):
        """Pen tip of the currently-animating shape, or None. The buddy
        rides this point so Clicky visibly draws each stroke."""
        now = time.monotonic()
        active = None
        for ann in self._annotations:
            if ann["start"] <= now < ann["start"] + ann["dur"]:
                if active is None or ann["start"] > active["start"]:
                    active = ann
        if active is None:
            return None
        u = (now - active["start"]) / active["dur"]
        return _stroke_tip(active, u)

    def _begin_flight(self, start: QPointF, end: QPointF, phase: str):
        dx, dy = end.x() - start.x(), end.y() - start.y()
        dist = math.hypot(dx, dy)
        mult = 1.7 if self._slow_mode else 1.0
        # Scale duration by distance so short hops don't feel sluggish
        dur = max(FLY_DURATION_MIN, min(FLY_DURATION_MAX, FLY_DURATION_MIN + dist / 700.0))
        if phase == _PHASE_RETURNING:
            dur = RETURN_DURATION
        dur *= mult * self._agent_pace
        # Arc up over the midpoint
        mid = QPointF((start.x() + end.x()) / 2, (start.y() + end.y()) / 2)
        arc_height = min(dist * 0.22, 90.0)
        self._fly_control   = QPointF(mid.x(), mid.y() - arc_height)
        self._fly_start_pos = QPointF(start.x(), start.y())
        self._fly_end_pos   = QPointF(end.x(), end.y())
        self._fly_t0 = time.monotonic()
        self._fly_duration = dur
        self._flight_phase = phase
        self._vel = QPointF(0, 0)

    def keep_on_top(self):
        """Put the overlay above every other topmost window, including the
        app's own panel and dialogs, without taking keyboard focus."""
        if not self.isVisible():
            return
        try:
            import ctypes
            HWND_TOPMOST = -1
            SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
            ctypes.windll.user32.SetWindowPos(
                int(self.winId()), HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE,
            )
        except Exception:
            pass   # non-Windows / no window handle yet — nothing to do

    def hide_cursor(self):
        self._release_lock()
        self.set_mode(MODE_IDLE)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _cover_all_monitors(self):
        geo = QApplication.primaryScreen().virtualGeometry()
        for s in QApplication.screens():
            geo = geo.united(s.geometry())
        # Leave a 2px gap at the bottom edge: a topmost window covering the
        # full screen suppresses Windows' auto-hide taskbar hover trigger
        # (GitHub issue #4). The buddy just never renders in those 2 rows.
        geo.setBottom(geo.bottom() - 2)
        self.setGeometry(geo)

    def _release_lock(self):
        self._locked_pos = None
        self._bubble_text = ""
        self._bubble_alpha = 0.0
        self._bubble_scale = 0.5
        self._flight_phase = _PHASE_FOLLOW
        self._flight_scale = 1.0
        self._rotation_deg = TRI_ROTATION_DEG
        self._hold_dwell = False
        self._ring = None
        self._arrow_target = None

    def _tick_extras(self):
        now = time.monotonic()
        if _playback is not None:
            speaking, level = _playback.is_playing(), _playback.speaking_level()
        else:
            speaking, level = False, 0.0
        self._mascot_frame = self._mascot.frame(now, speaking, level)
        if self._say_line:
            if now >= self._say_hide_at + SAY_FADE_OUT_S:
                self._say_line = self._say_text = ""
                self._say_rect = None
            elif self._say_done:
                self._say_text = revealed_text(self._say_line, 1.0)
            elif _playback is not None:
                gen, progress = _playback.playback_progress()
                self._say_text = revealed_text(self._say_line, progress if gen > self._say_gen else 0.0)
        if self._flight_phase in (_PHASE_FLYING, _PHASE_RETURNING):
            self._trail.append((now, self._display_pos.x(), self._display_pos.y()))
        if self._trail:
            self._trail = [pt for pt in self._trail if now - pt[0] < TRAIL_SECONDS]

    def _tick(self):
        self._tick_extras()
        qp = QCursor.pos()
        real = QPointF(qp.x(), qp.y())

        # ── Pointing phase machine ──
        if self._flight_phase in (_PHASE_FLYING, _PHASE_RETURNING):
            elapsed = time.monotonic() - self._fly_t0
            lp = min(1.0, elapsed / max(0.001, self._fly_duration))
            # Smoothstep easing — gentle start and end, teacher-friendly
            t = lp * lp * (3.0 - 2.0 * lp)
            omt = 1.0 - t
            bx = omt * omt * self._fly_start_pos.x() \
                 + 2 * omt * t * self._fly_control.x() \
                 + t * t * self._fly_end_pos.x()
            by = omt * omt * self._fly_start_pos.y() \
                 + 2 * omt * t * self._fly_control.y() \
                 + t * t * self._fly_end_pos.y()
            self._display_pos = QPointF(bx, by)
            # Rotate to tangent so the triangle "leans into" the flight
            tgx = 2 * omt * (self._fly_control.x() - self._fly_start_pos.x()) \
                  + 2 * t * (self._fly_end_pos.x() - self._fly_control.x())
            tgy = 2 * omt * (self._fly_control.y() - self._fly_start_pos.y()) \
                  + 2 * t * (self._fly_end_pos.y() - self._fly_control.y())
            self._rotation_deg = math.degrees(math.atan2(tgy, tgx)) + 90.0
            # Swoop scale pulse at midpoint
            self._flight_scale = 1.0 + math.sin(lp * math.pi) * 0.25
            # Bubble eases in during the flight
            if self._flight_phase == _PHASE_FLYING:
                self._bubble_alpha = min(1.0, lp * 1.4)
                self._bubble_scale = 0.5 + lp * 0.5
            if lp >= 1.0:
                if self._flight_phase == _PHASE_FLYING:
                    self._flight_phase = _PHASE_DWELLING
                    if self._hold_dwell:
                        self._dwell_until = float("inf")
                    else:
                        mult = 1.7 if self._slow_mode else 1.0
                        self._dwell_until = time.monotonic() + DWELL_SECONDS * mult
                    self._flight_scale = 1.0
                    self._rotation_deg = TRI_ROTATION_DEG
                else:  # RETURNING
                    self._release_lock()
            self._phase += 0.10
            self.update()
            return

        if self._flight_phase == _PHASE_DWELLING:
            # Gentle breathing pulse while the LLM explains
            breathe = 1.0 + 0.05 * math.sin(self._phase * 1.4)
            self._flight_scale = breathe
            self._bubble_alpha = min(1.0, self._bubble_alpha + 0.05)
            self._bubble_scale = self._bubble_scale + (1.0 - self._bubble_scale) * 0.15
            # Stay planted on the element
            self._display_pos = QPointF(self._locked_pos.x(), self._locked_pos.y())
            if time.monotonic() >= self._dwell_until:
                cursor_target = QPointF(real.x() + OFFSET_X, real.y() + OFFSET_Y)
                self._begin_flight(self._display_pos, cursor_target, _PHASE_RETURNING)
                # Fade bubble out during return
                self._bubble_alpha = 0.0
            self._phase += 0.10
            self.update()
            return

        # ── Drawing mode: buddy rides the pen tip of the active stroke, and
        # holds at the last tip between strokes while the lesson is still
        # playing (no yo-yo back to the mouse cursor mid-lesson) ──
        tip = self._active_stroke_tip()
        if tip is not None:
            self._last_tip = tip
        elif time.monotonic() >= self._draw_queue_end + 2.5:
            self._last_tip = None   # lesson over — resume cursor follow
        hold = tip or self._last_tip
        if hold is not None:
            # Strong pull toward the pen tip — smooth but keeps up with it
            tx, ty = hold[0] + 6, hold[1] + 6
            self._display_pos = QPointF(
                self._display_pos.x() + (tx - self._display_pos.x()) * 0.45,
                self._display_pos.y() + (ty - self._display_pos.y()) * 0.45,
            )
            self._vel = QPointF(0, 0)
            self._phase += 0.10
            self.update()
            return

        # ── Normal cursor-follow spring ──
        target = QPointF(real.x() + OFFSET_X, real.y() + OFFSET_Y)
        stiffness, damping = 0.28, 0.62

        # Spring
        ax = (target.x() - self._display_pos.x()) * stiffness
        ay = (target.y() - self._display_pos.y()) * stiffness
        self._vel = QPointF(self._vel.x() * damping + ax,
                            self._vel.y() * damping + ay)
        self._display_pos = QPointF(
            self._display_pos.x() + self._vel.x(),
            self._display_pos.y() + self._vel.y(),
        )

        self._phase += 0.10
        self._spin_phase += 0.14  # ~0.8s per rev @ 60fps matches Swift
        self._ring_phase += 0.08

        # Low-end PCs: the overlay spans every monitor, so a repaint is costly.
        # When nothing on it can change (pointer at rest, no caption, bubble,
        # drawing or animation, same mascot frame), skip this frame.
        sig = self._still_signature()
        if sig is not None and sig == self._last_still_sig:
            return
        self._last_still_sig = sig
        self.update()

    def _still_signature(self):
        """What the idle overlay looks like, or None while anything animates."""
        if (self._mode in (MODE_LISTENING, MODE_THINKING) or self._caption_text
                or self._say_line or self._annotations or self._trail
                or self._ring is not None or self._locked_pos is not None):
            return None
        if abs(self._vel.x()) > 0.05 or abs(self._vel.y()) > 0.05:
            return None
        return (round(self._display_pos.x()), round(self._display_pos.y()),
                self._mascot_frame, self._mode)

    # ── Painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # Mascot is the bottom layer: everything else draws over it
        if MASCOT_ENABLED:
            self._draw_mascot(p)
            self._draw_speech_bubble(p)

        # Ring goes UNDER the buddy
        if self._ring is not None and self._flight_phase in (
            _PHASE_DWELLING, _PHASE_FLYING
        ):
            self._draw_ring(p)

        # Whiteboard annotations — drawn under the buddy too
        if self._annotations:
            self._draw_annotations(p)

        # Caption under the buddy as well — the pointer is always the top
        # layer, so it stays visible when it flies over the caption.
        if self._caption_text:
            self._draw_caption(p)

        cx = self._display_pos.x() - self.x()
        cy = self._display_pos.y() - self.y()

        if self._trail:
            self._draw_trail(p)
        self._draw_grape(p, cx, cy)
        # Minimal cues around the pointer; the island shows the full status
        if self._mode == MODE_LISTENING:
            self._draw_listen_ring(p, cx, cy)
        elif self._mode == MODE_THINKING:
            self._draw_think_arc(p, cx, cy)

        if self._arrow_target is not None and self._flight_phase in (
            _PHASE_FLYING, _PHASE_DWELLING
        ):
            self._draw_arrow(p, cx, cy)

        if self._locked_pos is not None and self._bubble_text:
            # With an arrow below, put the label above so it stays clear of
            # the taskbar.
            by = cy - 46 if self._arrow_target is not None else cy
            self._draw_bubble(p, cx, by, self._bubble_text)

        p.end()

    def _draw_arrow(self, p, cx, cy):
        """Arrow from the buddy to a target it can't sit on (the taskbar).
        The tip bobs gently towards the target so it reads as 'down here'."""
        tx = self._arrow_target.x() - self.x()
        ty = self._arrow_target.y() - self.y()
        dx, dy = tx - cx, ty - cy
        dist = math.hypot(dx, dy)
        if dist < 24:
            return
        ux, uy = dx / dist, dy / dist
        a = self._bubble_alpha if self._flight_phase == _PHASE_FLYING else 1.0
        bob = (math.sin(self._phase * 1.8) + 1) * 3     # 0..6 px
        start = QPointF(cx + ux * 16, cy + uy * 16)
        tip = QPointF(tx - ux * (4 + bob), ty - uy * (4 + bob))
        head, half = 16.0, 9.0
        base = QPointF(tip.x() - ux * head, tip.y() - uy * head)
        left = QPointF(base.x() - uy * half, base.y() + ux * half)
        right = QPointF(base.x() + uy * half, base.y() - ux * half)
        path = QPainterPath(tip)
        path.lineTo(left)
        path.lineTo(right)
        path.closeSubpath()
        for col, width in ((QColor(255, 255, 255, int(230 * a)), 9),
                           (QColor(POINT_ACCENT.red(), POINT_ACCENT.green(),
                                   POINT_ACCENT.blue(), int(255 * a)), 5)):
            p.setPen(QPen(col, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(start, base)
        p.setPen(QPen(QColor(255, 255, 255, int(230 * a)), 3))
        p.setBrush(QColor(POINT_ACCENT.red(), POINT_ACCENT.green(), POINT_ACCENT.blue(), int(255 * a)))
        p.drawPath(path)
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _draw_caption(self, p):
        now = time.monotonic()
        if now >= self._caption_hide_at + CAPTION_FADE_OUT_S:
            self._caption_text = ""
            return
        alpha = min(1.0, (now - self._caption_t0) / CAPTION_FADE_IN_S)
        if now > self._caption_hide_at:
            alpha *= 1.0 - (now - self._caption_hide_at) / CAPTION_FADE_OUT_S
        if alpha <= 0.0 or self._caption_area is None:
            return

        area = self._caption_area
        pad = 22
        max_w = int(area.width() * 0.6)

        body_font = QFont()
        body_font.setFamilies(["Segoe UI", "Sylfaen"])   # Sylfaen: Georgian fallback
        body_font.setPixelSize(CAPTION_FONT_PX)
        body_font.setWeight(QFont.Weight.DemiBold)
        head_font = QFont(body_font)
        head_font.setPixelSize(int(CAPTION_FONT_PX * 0.55))
        head_font.setWeight(QFont.Weight.Normal)

        flags = int(Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap)
        p.setFont(body_font)
        body = p.fontMetrics().boundingRect(
            0, 0, max_w - 2 * pad, 10_000, flags, self._caption_text
        )
        p.setFont(head_font)
        head_h = p.fontMetrics().height()

        box_w = max(body.width(), p.fontMetrics().horizontalAdvance(self._caption_header)) + 2 * pad
        box_h = head_h + 6 + body.height() + 2 * pad
        # Bottom-centre of the screen the user is on, above the taskbar.
        # The overlay spans every monitor, so convert to window-local coords.
        left = area.x() + (area.width() - box_w) / 2 - self.x()
        top = area.y() + area.height() - box_h - 48 - self.y()
        if self._say_rect is not None:
            bubble = self._say_rect.translated(-self.x(), -self.y()).adjusted(-16, -16, 16, 16)
            if QRectF(left, top, box_w, box_h).intersects(bubble):
                left = max(area.x() - self.x() + 16, bubble.left() - box_w)
        box = QRectF(left, top, box_w, box_h)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(12, 14, 20, int(CAPTION_BG_ALPHA * alpha)))
        p.drawRoundedRect(box, 18, 18)
        p.setPen(QPen(QColor(0x33, 0x80, 0xFF, int(150 * alpha)), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(box, 18, 18)

        # With the lighter box, a soft dark shadow keeps the text readable
        # over bright windows behind it.
        head_rect = QRectF(left, top + pad, box_w, head_h)
        body_rect = QRectF(left + pad, top + pad + head_h + 6,
                           box_w - 2 * pad, body.height())
        shadow = QColor(0, 0, 0, int(200 * alpha))
        p.setFont(head_font)
        p.setPen(shadow)
        p.drawText(head_rect.translated(1.5, 1.5), int(Qt.AlignmentFlag.AlignHCenter), self._caption_header)
        p.setPen(QColor(190, 210, 245, int(255 * alpha)))
        p.drawText(head_rect, int(Qt.AlignmentFlag.AlignHCenter), self._caption_header)
        p.setFont(body_font)
        p.setPen(shadow)
        p.drawText(body_rect.translated(2, 2), flags, self._caption_text)
        p.setPen(QColor(255, 255, 255, int(255 * alpha)))
        p.drawText(body_rect, flags, self._caption_text)

    def _draw_annotations(self, p):
        """Render teaching shapes with progressive draw-in animation.

        Each shape animates from 0→100% over SHAPE_DRAW_SECONDS starting at
        its queued "start" time. Shapes with ttl=None persist at full alpha;
        shapes with a ttl fade out over their last 25%.
        """
        now = time.monotonic()
        keep = []
        for ann in self._annotations:
            u = (now - ann["start"]) / ann.get("dur", 0.6)
            if u <= 0:
                keep.append(ann)     # queued, not started yet
                continue
            u = min(1.0, u)

            alpha = 1.0
            ttl = ann.get("ttl")
            if ttl is not None:
                age = now - ann["start"]
                if age > ttl:
                    continue         # expired — drop
                if age > ttl * 0.75:
                    alpha = max(0.0, 1.0 - (age - ttl * 0.75) / (ttl * 0.25))
            keep.append(ann)

            col = QColor(ANNOT_COLORS.get(ann.get("color", "blue"), CURSOR_BLUE))
            col.setAlpha(int(235 * alpha))
            kind = ann["kind"]
            try:
                if kind in ("line", "arrow"):
                    self._paint_stroke_path(p, ann["pts"], False, u, col,
                                            arrow=(kind == "arrow"))
                elif kind == "poly":
                    self._paint_stroke_path(p, ann["pts"], True, u, col)
                elif kind == "rect":
                    x1, y1, x2, y2 = ann["x1"], ann["y1"], ann["x2"], ann["y2"]
                    pts = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
                    self._paint_stroke_path(p, pts, True, u, col)
                elif kind == "circle":
                    self._paint_circle(p, ann, u, col, alpha)
                elif kind == "underline":
                    pts = [(ann["x"], ann["y"]), (ann["x"] + ann["w"], ann["y"])]
                    self._paint_stroke_path(p, pts, False, u, col)
                elif kind == "angle":
                    self._paint_angle(p, ann, u, col)
                elif kind == "text":
                    self._paint_text(p, ann, u, col, alpha)
            except Exception:
                continue   # a malformed shape must never break the paint loop
        self._annotations = keep

    def _pens(self, col, width=4.0):
        """(under, main) pen pair — dark under-stroke keeps lines readable
        over light content, like a marker outline."""
        under_col = QColor(0, 0, 0)
        under_col.setAlpha(min(110, col.alpha()))
        under = QPen(under_col, width + 2.5)
        under.setCapStyle(Qt.PenCapStyle.RoundCap)
        under.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        main = QPen(col, width)
        main.setCapStyle(Qt.PenCapStyle.RoundCap)
        main.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        return under, main

    def _paint_stroke_path(self, p, pts, closed, u, col, arrow=False, width=4.0):
        """Draw the first u-fraction of a polyline (progressive stroke)."""
        pts = [(x - self.x(), y - self.y()) for (x, y) in pts]
        drawn = _partial_pts(pts, closed, u)
        if len(drawn) < 2:
            return
        if closed and len(pts) >= 3:
            pts = pts + [pts[0]]
        path = QPainterPath(QPointF(*drawn[0]))
        for pt in drawn[1:]:
            path.lineTo(QPointF(*pt))
        p.setBrush(Qt.BrushStyle.NoBrush)
        for pen in self._pens(col, width):
            p.setPen(pen)
            p.drawPath(path)
        # Arrowhead appears once the line is fully drawn
        if arrow and u >= 1.0 and len(pts) >= 2:
            (x1, y1), (x2, y2) = pts[-2], pts[-1]
            ang = math.atan2(y2 - y1, x2 - x1)
            head = 15
            for pen in self._pens(col, width):
                p.setPen(pen)
                for sign in (-1, 1):
                    ax = x2 - head * math.cos(ang + sign * math.radians(28))
                    ay = y2 - head * math.sin(ang + sign * math.radians(28))
                    p.drawLine(QPointF(x2, y2), QPointF(ax, ay))

    def _paint_circle(self, p, ann, u, col, alpha):
        x = ann["x"] - self.x()
        y = ann["y"] - self.y()
        r = ann["r"]
        rect = QRectF(x - r, y - r, 2 * r, 2 * r)
        p.setBrush(Qt.BrushStyle.NoBrush)
        # Sweep the arc clockwise from 12 o'clock as it draws in
        span = int(-360 * 16 * u)
        for pen in self._pens(col, 3.5):
            p.setPen(pen)
            p.drawArc(rect, 90 * 16, span)
        label = ann.get("label", "")
        if label and u >= 1.0:
            f = QFont("Segoe UI", 11, QFont.Weight.Bold)
            p.setFont(f)
            fm = p.fontMetrics()
            tx = x - fm.horizontalAdvance(label) / 2
            ty = y - r - 8
            self._outlined_text(p, tx, ty, label, col, alpha)

    def _paint_angle(self, p, ann, u, col):
        """Right-angle marker: a small square corner at (x,y), rotated rot°."""
        x, y = ann["x"], ann["y"]
        s = ann.get("s", 22)
        rot = math.radians(ann.get("rot", 0))
        d1 = (math.cos(rot), math.sin(rot))
        d2 = (math.cos(rot + math.pi / 2), math.sin(rot + math.pi / 2))
        p1 = (x + d1[0] * s, y + d1[1] * s)
        p2 = (x + (d1[0] + d2[0]) * s, y + (d1[1] + d2[1]) * s)
        p3 = (x + d2[0] * s, y + d2[1] * s)
        self._paint_stroke_path(p, [p1, p2, p3], False, u, col, width=3.0)

    def _paint_text(self, p, ann, u, col, alpha):
        x = ann["x"] - self.x()
        y = ann["y"] - self.y()
        pt_size = _TEXT_SIZES.get(ann.get("size", "m"), 17)
        font = QFont("Segoe UI", pt_size, QFont.Weight.Bold)
        p.setFont(font)
        # Ease in: fade + slight rise
        fade = u * alpha
        y += (1.0 - u) * 6
        self._outlined_text(p, x, y, ann["text"], col, fade)

    def _outlined_text(self, p, x, y, text, col, alpha):
        """Text with a dark outline so it reads on any background."""
        shadow = QColor(0, 0, 0)
        shadow.setAlpha(int(170 * alpha))
        p.setPen(QPen(shadow, 1))
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (1, 1)):
            p.drawText(QPointF(x + dx, y + dy), text)
        c = QColor(col)
        c.setAlpha(int(255 * alpha))
        p.setPen(QPen(c, 1))
        p.drawText(QPointF(x, y), text)

    def _draw_ring(self, p):
        """Pulsing orange ring around the detected UI element."""
        rx, ry, base_r = self._ring
        rx -= self.x()
        ry -= self.y()
        pulse = (math.sin(self._ring_phase) + 1) / 2   # 0..1
        r = base_r + pulse * 6
        # Outer halo
        glow = QColor(POINT_ACCENT)
        glow.setAlpha(60)
        pen = QPen(glow, 4)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(rx, ry), r + 3, r + 3)
        # Crisp inner ring
        inner = QColor(POINT_ACCENT)
        inner.setAlpha(190)
        pen = QPen(inner, 2)
        p.setPen(pen)
        p.drawEllipse(QPointF(rx, ry), r, r)

    def _draw_glow(self, p, cx, cy, radius, color_alpha=90):
        """Cheap soft shadow approximating Swift's .shadow(radius: 8)."""
        g = QColor(CURSOR_BLUE)
        for i, r_mul in enumerate((1.6, 1.25, 1.0)):
            g.setAlpha(color_alpha // (i + 1))
            p.setBrush(QBrush(g))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(cx, cy), radius * r_mul, radius * r_mul)

    # ── 🍇 pointer ────────────────────────────────────────────────────────────

    def _grape_pixmap(self) -> QPixmap:
        dpr = self.devicePixelRatioF() or 1.0
        if self._grape_pm is not None and abs(self._grape_pm.devicePixelRatio() - dpr) < 0.01:
            return self._grape_pm
        self._grape_pm = render_grape_pixmap(dpr)
        return self._grape_pm

    def _draw_grape(self, p, cx, cy):
        pm = self._grape_pixmap()
        half = pm.width() / pm.devicePixelRatio() / 2
        p.save()
        p.translate(cx, cy)
        p.scale(self._flight_scale, self._flight_scale)
        if self._flight_phase in (_PHASE_FLYING, _PHASE_DWELLING):
            glow = QColor(*TRAIL_GOLD)
            for r_mul, a in ((1.9, 30), (1.4, 55)):
                glow.setAlpha(a)
                p.setBrush(glow)
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QPointF(0, 0), GRAPE_RADIUS * r_mul, GRAPE_RADIUS * r_mul)
        p.drawPixmap(QPointF(-half, -half), pm)
        p.restore()

    def _draw_listen_ring(self, p, cx, cy):
        """Listening: a thin ring that breathes with the voice."""
        level = min(1.0, self._audio_level)
        r = GRAPE_RADIUS + 4 + level * 10 + (math.sin(self._phase * 1.6) + 1) * 1.2
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(90, 175, 255, 170), 2))
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.setPen(QPen(QColor(90, 175, 255, 60), 4))
        p.drawEllipse(QPointF(cx, cy), r + 3, r + 3)

    def _draw_think_arc(self, p, cx, cy):
        """Loading: one thin arc fading round the pointer."""
        r = GRAPE_RADIUS + 5
        rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        angle = -math.degrees(self._spin_phase * 2)
        grad = QConicalGradient(QPointF(cx, cy), angle)
        grad.setColorAt(0.0, QColor(120, 160, 255, 230))
        grad.setColorAt(0.35, QColor(190, 120, 255, 140))
        grad.setColorAt(0.7, QColor(190, 120, 255, 0))
        pen = QPen(QBrush(grad), 2.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(rect, int(angle * 16), int(250 * 16))

    def _draw_trail(self, p):
        """Golden comet tail along the flight path: wide and bright at the
        pointer, thin and transparent at the start."""
        pts = [(x - self.x(), y - self.y()) for _, x, y in self._trail]
        n = len(pts)
        if n < 2:
            return
        for glow_pass in (True, False):
            for i in range(1, n):
                f = i / (n - 1)
                width = 3 + 15 * f
                alpha = int(210 * f ** 1.3)
                col = QColor(*TRAIL_GOLD, int(alpha * (0.28 if glow_pass else 1.0)))
                pen = QPen(col, width * (2.0 if glow_pass else 1.0))
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                p.drawLine(QPointF(*pts[i - 1]), QPointF(*pts[i]))

    # ── Speech bubble ─────────────────────────────────────────────────────────

    def _draw_speech_bubble(self, p):
        if not self._say_text:
            self._say_rect = None
            return
        now = time.monotonic()
        alpha = min(1.0, (now - self._say_t0) / SAY_FADE_IN_S)
        if now > self._say_hide_at:
            alpha *= max(0.0, 1.0 - (now - self._say_hide_at) / SAY_FADE_OUT_S)

        font = QFont()
        font.setFamilies(["Segoe UI", "Sylfaen"])
        font.setPixelSize(20)
        fm = QFontMetrics(font)
        pad = 16
        flags = int(Qt.TextFlag.TextWordWrap)
        max_w = BUBBLE_MAX_W - 2 * pad
        text = self._say_text
        br = fm.boundingRect(0, 0, max_w, 10_000, flags, text)
        while br.height() > fm.lineSpacing() * SAY_MAX_LINES and " " in text:
            text = text.split(" ", 1)[1]          # keep the newest words
            br = fm.boundingRect(0, 0, max_w, 10_000, flags, "… " + text)
        if text != self._say_text:
            text = "… " + text
        size = (max(br.width(), 60) + 2 * pad, br.height() + 2 * pad)

        m = self.mascot_rect()
        a = QApplication.primaryScreen().availableGeometry()
        (bx, by, bw, bh), (tx, ty) = bubble_geometry(
            (a.x(), a.y(), a.width(), a.height()), (m.x(), m.y(), m.width(), m.height()), size)
        # Recorded even on a fully transparent frame (the first frame of the
        # fade-in): the stop window and panel keep off this area from the start.
        self._say_rect = QRectF(bx, by, bw, bh)
        if alpha <= 0.0:
            return
        ox, oy = self.x(), self.y()
        rect = QRectF(bx - ox, by - oy, bw, bh)
        tip = QPointF(tx - ox, ty - oy)

        # bubble + tail as one shape, so the outline flows into the tail
        path = QPainterPath()
        path.addRoundedRect(rect, 18, 18)
        base_x = min(rect.right() - 30, max(rect.left() + 30, tip.x() - 40))
        tail = QPainterPath()
        tail.moveTo(base_x - 14, rect.bottom() - 2)
        tail.quadTo(base_x + 4, rect.bottom() + 14, tip.x(), tip.y())
        tail.quadTo(base_x + 14, rect.bottom() + 4, base_x + 18, rect.bottom() - 2)
        tail.closeSubpath()
        shape = path.united(tail)

        p.save()
        pop = 0.92 + 0.08 * alpha if now - self._say_t0 < SAY_FADE_IN_S else 1.0
        p.translate(tip)
        p.scale(pop, pop)
        p.translate(-tip)
        p.setPen(Qt.PenStyle.NoPen)
        for grow, a_ in ((6, 14), (3, 22)):
            p.setBrush(QColor(0, 0, 0, int(a_ * alpha)))
            p.drawRoundedRect(rect.adjusted(-grow, -grow + 3, grow, grow + 3), 18 + grow, 18 + grow)
        p.setBrush(QColor(252, 252, 254, int(248 * alpha)))
        p.setPen(QPen(QColor(0, 0, 0, int(40 * alpha)), 1.2))
        p.drawPath(shape)
        p.setFont(font)
        p.setPen(QColor(26, 26, 32, int(255 * alpha)))
        p.drawText(rect.adjusted(pad, pad, -pad, -pad), flags, text)
        p.restore()

    # ── Mascot ────────────────────────────────────────────────────────────────

    def mascot_rect(self):
        """Where the mascot sits (global logical px): bottom-right, standing
        on the taskbar. Used to keep the panel and stop window off it."""
        from PyQt6.QtCore import QRect
        a = QApplication.primaryScreen().availableGeometry()
        return QRect(a.right() - MASCOT_MARGIN - MASCOT_SIZE, a.bottom() - MASCOT_SIZE - 4,
                     MASCOT_SIZE, MASCOT_SIZE)

    def _draw_mascot(self, p):
        if self._mascot_frames is None:
            self._mascot_frames = load_frames(MASCOT_SIZE, self.devicePixelRatioF() or 1.0)
        pm = self._mascot_frames.get(self._mascot_frame)
        if pm is None:
            return
        r = self.mascot_rect()
        bob = math.sin(time.monotonic() * 1.6) * 1.5      # gentle idle breathing
        p.drawPixmap(QPointF(r.x() - self.x(), r.y() - self.y() + bob), pm)

    def _draw_bubble(self, p, cx, cy, label: str):
        """Speech bubble next to the buddy, matches Swift pill style."""
        font = QFont("Segoe UI", 9, QFont.Weight.Medium)
        p.setFont(font)
        fm = p.fontMetrics()

        pad_x, pad_y = 8, 4
        tw = fm.horizontalAdvance(label) + pad_x * 2
        th = fm.height() + pad_y * 2

        # Swift positions bubble at (cursor.x + 10 + bubbleW/2, cursor.y + 18)
        box_x = cx + 10
        box_y = cy + 18 - th / 2

        # Apply scale-bounce entrance around the bubble's left edge
        scale = max(0.01, self._bubble_scale)
        p.save()
        p.translate(box_x, box_y + th / 2)
        p.scale(scale, scale)
        p.translate(-box_x, -(box_y + th / 2))

        a = int(255 * self._bubble_alpha)
        bg = QColor(CURSOR_BLUE)
        bg.setAlpha(a)
        glow = QColor(CURSOR_BLUE)
        glow.setAlpha(int(90 * self._bubble_alpha))

        # Glow
        p.setBrush(QBrush(glow))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(box_x - 4, box_y - 4, tw + 8, th + 8), 9, 9)

        # Pill
        p.setBrush(QBrush(bg))
        p.drawRoundedRect(QRectF(box_x, box_y, tw, th), 6, 6)

        # Text
        text_color = QColor(255, 255, 255, a)
        p.setPen(QPen(text_color, 1))
        p.drawText(QRectF(box_x, box_y, tw, th), Qt.AlignmentFlag.AlignCenter, label)

        p.restore()

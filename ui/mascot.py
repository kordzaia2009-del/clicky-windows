"""
The mascot — the mustached bean in the bottom-left corner.

Four frames (assets/mascot/):
    idle_eyes_open     mouth closed, eyes open    (resting)
    idle_eyes_closed   mouth closed, eyes closed  (resting blink)
    talk_eyes_open     mouth open,   eyes open    (talking)
    talk_eyes_closed   mouth open,   eyes closed  (blink while talking)

MascotAnimator is pure (no Qt): give it the time, whether speech is playing
and how loud it is, and it returns the frame to draw.
  • eyes: random blinks every 2.5–6 s (sometimes a quick double blink)
  • mouth: follows the real loudness of the speech; if the loudness is not
    known (fallback playback) it flaps at a natural talking rhythm
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Callable, Optional

FRAMES = ("idle_eyes_open", "idle_eyes_closed", "talk_eyes_open", "talk_eyes_closed")

BLINK_MIN_S, BLINK_MAX_S = 2.5, 6.0
BLINK_LEN_S = 0.13
DOUBLE_BLINK_CHANCE = 0.2

OPEN_LEVEL = 0.035          # loudness that opens the mouth...
CLOSE_LEVEL = 0.020         # ...and closes it again (hysteresis)
MIN_OPEN_S = 0.07           # no machine-gun flicker
MIN_CLOSED_S = 0.06
FLAP_MIN_S, FLAP_MAX_S = 0.10, 0.18   # rhythm when loudness is unknown


def frame_name(mouth_open: bool, eyes_closed: bool) -> str:
    return ("talk" if mouth_open else "idle") + ("_eyes_closed" if eyes_closed else "_eyes_open")


class MascotAnimator:
    def __init__(self, rng: Optional[random.Random] = None):
        self.rng = rng or random.Random()
        self._next_blink = None
        self._blink_until = -1.0
        self._mouth_open = False
        self._mouth_since = float("-inf")     # free to open on the very first frame
        self._next_flap = 0.0

    def _schedule_blink(self, now: float):
        self._next_blink = now + self.rng.uniform(BLINK_MIN_S, BLINK_MAX_S)

    def eyes_closed(self, now: float) -> bool:
        if self._next_blink is None:
            self._schedule_blink(now)
        if now >= self._next_blink:
            self._blink_until = now + BLINK_LEN_S
            if self.rng.random() < DOUBLE_BLINK_CHANCE:
                self._next_blink = now + BLINK_LEN_S * 2.4      # quick second blink
            else:
                self._schedule_blink(now)
        return now < self._blink_until

    def mouth_open(self, now: float, speaking: bool, level: float) -> bool:
        if not speaking:
            self._mouth_open = False
            return False
        held = now - self._mouth_since
        if level <= 0.0:
            # Loudness unknown: flap at a talking rhythm.
            if now >= self._next_flap:
                self._mouth_open = not self._mouth_open
                self._mouth_since = now
                self._next_flap = now + self.rng.uniform(FLAP_MIN_S, FLAP_MAX_S)
            return self._mouth_open
        if self._mouth_open:
            if level < CLOSE_LEVEL and held >= MIN_OPEN_S:
                self._mouth_open, self._mouth_since = False, now
        elif level > OPEN_LEVEL and held >= MIN_CLOSED_S:
            self._mouth_open, self._mouth_since = True, now
        return self._mouth_open

    def frame(self, now: float, speaking: bool, level: float) -> str:
        return frame_name(self.mouth_open(now, speaking, level), self.eyes_closed(now))


def asset_dir() -> Path:
    """assets/mascot next to the app (or inside a PyInstaller bundle)."""
    import sys
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "assets" / "mascot"


def load_frames(size_px: int, device_pixel_ratio: float = 1.0,
                loader: Optional[Callable] = None) -> dict:
    """QPixmaps for every frame, pre-scaled for crisp drawing. Missing files
    are skipped (the overlay then simply draws no mascot)."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QPixmap
    frames = {}
    target = max(1, int(round(size_px * device_pixel_ratio)))
    for name in FRAMES:
        pm = QPixmap(str(asset_dir() / f"{name}.png"))
        if pm.isNull():
            continue
        pm = pm.scaled(target, target, Qt.AspectRatioMode.KeepAspectRatio,
                       Qt.TransformationMode.SmoothTransformation)
        pm.setDevicePixelRatio(device_pixel_ratio)
        frames[name] = pm
    return frames


# ── Speech bubble ("words coming out of its mouth") ──────────────────────────

# Mouth position inside the mascot frame (measured on the artwork), 0..1
MOUTH_X, MOUTH_Y = 0.47, 0.50
BUBBLE_MAX_W = 380
BUBBLE_GAP = 14


def revealed_text(text: str, progress: float) -> str:
    """The words already spoken: `progress` (0..1) of the clip has played.
    Cuts at word boundaries; at least the first word once speech starts."""
    words = (text or "").split()
    if not words or progress <= 0.0:
        return ""
    if progress >= 1.0:
        return " ".join(words)
    total = sum(len(w) + 1 for w in words)
    budget, shown, used = progress * total, [], 0
    for w in words:
        if shown and used + len(w) > budget:
            break
        shown.append(w)
        used += len(w) + 1
    return " ".join(shown)


def bubble_geometry(avail, mascot, size):
    """Where the bubble goes and where its tail ends.
    avail / mascot: (x, y, w, h) logical px; size: (w, h) of the bubble.
    Returns ((x, y, w, h), (tail_x, tail_y)). The bubble sits up and to the
    left of the mascot (it lives in the bottom-right corner), fully on screen,
    and its tail points at the mouth."""
    ax, ay, aw, ah = avail
    mx, my, mw, mh = mascot
    bw, bh = size
    mouth = (mx + mw * MOUTH_X, my + mh * MOUTH_Y)
    x = mx + mw * 0.15 - bw                      # mostly left of the mascot
    y = my - bh + mh * 0.30                      # bottom edge a little above the mouth
    x = max(ax + 10, min(x, ax + aw - bw - 10))
    y = max(ay + 10, min(y, ay + ah - bh - 10))
    return (int(x), int(y), int(bw), int(bh)), (mouth[0] - mw * 0.06, mouth[1])

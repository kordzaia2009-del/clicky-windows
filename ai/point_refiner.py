"""
Point refiner — snap the AI's approximate [POINT] onto the thing it names.

The AI writes [POINT:x,y:label] from a downscaled screenshot, so its x,y can
be tens of pixels off. But it also tells us *what* it is pointing at: the
label is the target's visible on-screen text (see the prompt). This module
looks only around the guessed spot and moves the pointer onto the matching
element:

  1. UI Automation at the guessed point (~5-50 ms). ControlFromPoint gives
     the element under the guess; its ancestors and its parent's children
     are checked for a name matching the label. Unlike the global UIA walk
     in hybrid_pointer this is not limited to the foreground window, so it
     also covers the taskbar, desktop icons and system tray.
  2. OCR on a small crop around the guess (RapidOCR, local) — for apps that
     expose no accessibility tree. A crop is several times faster than the
     full-screen OCR pass.
  3. No text match: if the guess landed on a small interactive control
     (a button, menu item, tab...), centre on that control.

Everything is local: no extra API calls and nothing extra uploaded. Runs off
the UI thread; the overlay is already flying to the raw guess and gets
retargeted if this finds something better.

Coordinates: callers pass LOGICAL screen pixels plus the DPI scale; UIA and
screen grabs work in PHYSICAL pixels.
"""

from __future__ import annotations

import difflib
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("clicky.refine")

SEARCH_RADIUS = 180        # logical px around the guess that may be snapped to
MIN_TEXT_SCORE = 0.6       # label/text similarity needed to trust a match
DIST_PENALTY = 0.25        # score lost at the edge of the search radius
MAX_UIA_NODES = 250
SCOPE_LEVELS = 3          # ancestors to climb before searching downwards

_CLICKABLE = {
    "ButtonControl", "MenuItemControl", "TabItemControl", "ListItemControl",
    "HyperlinkControl", "CheckBoxControl", "RadioButtonControl",
    "ComboBoxControl", "EditControl", "TreeItemControl", "SplitButtonControl",
}


@dataclass
class Refined:
    x: float            # logical px
    y: float
    source: str         # "uia-text" | "ocr-text" | "uia-under"
    score: float
    matched: str


def _norm(s: str) -> str:
    # Unicode-aware: keeps Georgian, Cyrillic etc. (a-z-only would erase them)
    return re.sub(r"[\W_]+", " ", (s or "").lower()).strip()


def text_score(label: str, text: str) -> float:
    """0..1 similarity between the AI's label and an on-screen string."""
    a, b = _norm(label), _norm(text)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        # "chrome" in "google chrome 1 running window" — strong, but prefer
        # the tighter match when several contain it.
        short, long_ = sorted((a, b), key=len)
        return 0.75 + 0.2 * len(short) / len(long_)
    aw, bw = set(a.split()), set(b.split())
    overlap = len(aw & bw) / max(len(aw), 1)
    fuzzy = difflib.SequenceMatcher(None, a, b).ratio()   # OCR typos
    return max(overlap * 0.7, fuzzy * 0.85)


def _uia_candidates(px: int, py: int, radius: float):
    """Elements near a physical point.

    Starts from the element under the point, climbs SCOPE_LEVELS ancestors,
    then walks that subtree breadth-first, descending only into branches
    whose rectangle overlaps the search box. Siblings alone weren't enough:
    on the taskbar, Start and the clock sit in different containers from the
    app buttons next to them."""
    import uiautomation as auto

    hit = auto.ControlFromPoint(px, py)
    if hit is None:
        return
    scope = hit
    for _ in range(SCOPE_LEVELS):
        parent = scope.GetParentControl()
        if parent is None or not parent.Name and parent.ControlTypeName == "PaneControl" \
                and parent.ClassName in ("#32769",):   # desktop root — stop
            break
        scope = parent
    bl, bt, br, bb = px - radius, py - radius, px + radius, py + radius
    queue, seen = [scope], 0
    while queue and seen < MAX_UIA_NODES:
        node = queue.pop(0)
        seen += 1
        try:
            r = node.BoundingRectangle
        except Exception:
            continue
        empty = not r or r.width() <= 0 or r.height() <= 0
        overlaps = not empty and r.left < br and r.right > bl and r.top < bb and r.bottom > bt
        if overlaps:
            yield node
        if empty or overlaps:
            try:
                queue.extend(node.GetChildren())
            except Exception:
                pass


def _best_uia(px, py, label, scale, radius_phys):
    best = None
    under = None
    for node in _uia_candidates(px, py, radius_phys):
        try:
            r = node.BoundingRectangle
            name = node.Name or ""
            ctype = node.ControlTypeName or ""
        except Exception:
            continue
        if not r or r.width() <= 0 or r.height() <= 0:
            continue
        cx, cy = (r.left + r.right) / 2, (r.top + r.bottom) / 2
        dist = math.hypot(cx - px, cy - py)
        if dist > radius_phys:
            continue
        if under is None and ctype in _CLICKABLE and r.left <= px <= r.right \
                and r.top <= py <= r.bottom and r.width() <= 320 * scale \
                and r.height() <= 140 * scale:
            under = (cx, cy, name or ctype)
        if label:
            s = text_score(label, name) - DIST_PENALTY * dist / radius_phys
            if best is None or s > best[0]:
                best = (s, cx, cy, name)
    return best, under


def _best_ocr(px, py, label, radius_phys, image=None, origin=(0, 0)):
    """OCR a crop around (px, py). `image`/`origin` let tests inject a frame
    (an RGB numpy array and its top-left in physical screen coordinates)."""
    import numpy as np
    from ai.hybrid_pointer import _get_ocr

    ocr = _get_ocr()
    if ocr is None:
        return None
    half_w, half_h = int(radius_phys * 1.4), int(radius_phys * 0.8)
    if image is None:
        import mss
        with mss.mss() as sct:
            v = sct.monitors[0]   # whole virtual desktop
            left = max(v["left"], px - half_w)
            top = max(v["top"], py - half_h)
            right = min(v["left"] + v["width"], px + half_w)
            bottom = min(v["top"] + v["height"], py + half_h)
            raw = sct.grab({"left": left, "top": top,
                            "width": right - left, "height": bottom - top})
            crop = np.array(raw)[:, :, 2::-1]   # BGRA -> RGB
    else:
        ox, oy = origin
        h, w = image.shape[:2]
        left = max(ox, px - half_w)
        top = max(oy, py - half_h)
        right = min(ox + w, px + half_w)
        bottom = min(oy + h, py + half_h)
        crop = image[top - oy:bottom - oy, left - ox:right - ox]
    result, _ = ocr(np.ascontiguousarray(crop))
    best = None
    for det in result or []:
        try:
            box, text, conf = det
        except Exception:
            continue
        if float(conf) < 0.4:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        cx = left + (min(xs) + max(xs)) / 2
        cy = top + (min(ys) + max(ys)) / 2
        dist = math.hypot(cx - px, cy - py)
        if dist > radius_phys * 1.4:
            continue
        s = text_score(label, text) - DIST_PENALTY * min(1.0, dist / radius_phys)
        if best is None or s > best[0]:
            best = (s, cx, cy, text)
    return best


def refine(x: float, y: float, label: str, scale: float = 1.0,
           use_ocr: bool = True) -> Optional[Refined]:
    """Better position for a pointer aimed at (x, y) [logical px] that should
    land on `label`, or None to keep the original guess."""
    t0 = time.perf_counter()
    scale = scale or 1.0
    px, py = int(round(x * scale)), int(round(y * scale))
    radius = SEARCH_RADIUS * scale
    label = (label or "").strip()
    found = None

    under = None
    try:
        best, under = _best_uia(px, py, label, scale, radius)
        if best and best[0] >= MIN_TEXT_SCORE:
            found = Refined(best[1] / scale, best[2] / scale, "uia-text", best[0], best[3])
    except Exception as e:
        log.debug("UIA refine failed: %s", e)

    if found is None and use_ocr and label:
        try:
            best = _best_ocr(px, py, label, radius)
            if best and best[0] >= MIN_TEXT_SCORE:
                found = Refined(best[1] / scale, best[2] / scale, "ocr-text", best[0], best[3])
        except Exception as e:
            log.debug("OCR refine failed: %s", e)

    if found is None and under is not None:
        found = Refined(under[0] / scale, under[1] / scale, "uia-under", 0.5, under[2])

    ms = (time.perf_counter() - t0) * 1000
    if found is None:
        log.info("refine %r @ (%.0f,%.0f): no better spot (%.0f ms)", label, x, y, ms)
        return None
    log.info("refine %r @ (%.0f,%.0f) -> (%.0f,%.0f) via %s %r score=%.2f (%.0f ms)",
             label, x, y, found.x, found.y, found.source, found.matched, found.score, ms)
    return found

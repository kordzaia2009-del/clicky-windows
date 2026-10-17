"""
Pure helpers for the autopilot's panel view (no widgets, unit-tested):
the plan checklist and where the panel docks during a run.
"""

from __future__ import annotations

import html
from typing import Optional, Sequence, Tuple

from ui import strings_ka

Rect = Tuple[int, int, int, int]            # x, y, width, height (logical px)

CHECKLIST_FONT_PX = 24
DOCK_MARGIN = 16
TARGET_CLEARANCE = 60


def progress_text(current_index: int, total: int) -> str:
    """0-based index -> „ნაბიჯი 2 / 4“."""
    return strings_ka.STEP_PROGRESS.format(current=current_index + 1, total=total)


def checklist_html(steps: Sequence, current_index: int, goal: str = "",
                   goal_index: int = 0, goal_total: int = 1) -> str:
    """Large, high-contrast checklist: ✓ for finished steps, ▶ on a yellow
    highlight for the current one, plain white for the rest."""
    total = len(steps)
    parts = [f'<div style="font-size:{CHECKLIST_FONT_PX}px; color:#ffffff;">']
    if goal_total > 1 and goal:
        parts.append(
            f'<div style="font-size:18px; color:#c8d6ff;">'
            f'{html.escape(strings_ka.GOAL_PROGRESS.format(current=goal_index + 1, total=goal_total, goal=goal))}'
            f'</div>')
    if total:
        idx = min(max(current_index, 0), total - 1)
        parts.append(f'<div style="font-weight:700; color:#ffe14d; margin-bottom:6px;">'
                     f'{html.escape(progress_text(idx, total))}</div>')
    for i, step in enumerate(steps):
        title = html.escape(getattr(step, "title", str(step)))
        if i < current_index:
            parts.append(f'<div style="color:#8ee6a0;">✓ {title}</div>')
        elif i == current_index:
            parts.append(f'<div style="background:#ffe14d; color:#000000; font-weight:700;'
                         f' padding:4px;">▶ {title}</div>')
        else:
            parts.append(f'<div style="color:#ffffff;">○ {title}</div>')
    parts.append("</div>")
    return "".join(parts)


def _hit(rect: Rect, point, pad: int = 0) -> bool:
    x, y, w, h = rect
    return x - pad <= point[0] <= x + w + pad and y - pad <= point[1] <= y + h + pad


def _overlaps(a: Rect, b: Rect) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def panel_dock_position(avail: Rect, size: Tuple[int, int],
                        target: Optional[Tuple[float, float]],
                        keep_clear: Sequence[Rect] = ()) -> Tuple[int, int]:
    """The corner farthest from `target` that does not cover the target or any
    `keep_clear` rect (the stop window). Falls back to the corner farthest
    from the target that at least leaves the target uncovered."""
    ax, ay, aw, ah = avail
    w, h = size
    left, right = ax + DOCK_MARGIN, ax + aw - w - DOCK_MARGIN
    top, bottom = ay + DOCK_MARGIN, ay + ah - h - DOCK_MARGIN
    corners = [(right, bottom), (left, bottom), (right, top), (left, top)]
    if target is not None:
        tx, ty = target
        corners.sort(key=lambda c: -((c[0] + w / 2 - tx) ** 2 + (c[1] + h / 2 - ty) ** 2))
    for strict in (True, False):
        for c in corners:
            r = (c[0], c[1], w, h)
            if target is not None and _hit(r, target, TARGET_CLEARANCE):
                continue
            if strict and any(_overlaps(r, k) for k in keep_clear):
                continue
            return c
    return corners[0]

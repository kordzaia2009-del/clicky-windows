"""
Timing logs for finding what makes the autopilot slow — measurement only,
no behaviour. Every line goes to clicky.log as

    timing: <phase> <milliseconds> ms key=value ...

    with timed("locator.pywinauto", label=label) as t:
        found = ...
        t["hit"] = found is not None        # extra fields can be added inside

grep "timing:" in %LOCALAPPDATA%\\Clicky\\clicky.log to read a run.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager

log = logging.getLogger("clicky.timing")


def mark(phase: str, ms: float, **fields) -> None:
    extra = " ".join(f"{k}={v!r}" if isinstance(v, str) else f"{k}={v}"
                     for k, v in fields.items())
    log.info("timing: %s %.0f ms%s", phase, ms, (" " + extra) if extra else "")


@contextmanager
def timed(phase: str, **fields):
    t0 = time.perf_counter()
    try:
        yield fields
    finally:
        mark(phase, (time.perf_counter() - t0) * 1000, **fields)


def since_ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000

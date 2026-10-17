"""
Tiny manual check of agent/executor.py. After a 3-second pause it types
„გამარჯობა“ into whichever window has focus, then clicks at --x/--y
(PHYSICAL screen pixels), and prints what it did.

    python scripts/executor_manual_test.py --x 800 --y 400          # dry run
    python scripts/executor_manual_test.py --x 800 --y 400 --real   # real input

No locator, no model calls, no programs launched. Esc cancels.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TEXT = "გამარჯობა"
PAUSE_S = 3.0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Type Georgian, then click at x,y.")
    ap.add_argument("--x", type=int, required=True, help="physical screen x")
    ap.add_argument("--y", type=int, required=True, help="physical screen y")
    ap.add_argument("--real", action="store_true",
                    help="send real input (default: dry run)")
    return ap


def run(args, ex, sleep=time.sleep, say=print) -> list:
    """Returns the two ActionResults. `ex` is agent.executor (or a fake)."""
    ex.set_dry_run(not args.real)
    ex.reset_cancel()
    say(f"{'REAL' if args.real else 'DRY RUN'}: focus the target window — "
        f"starting in {PAUSE_S:g} s (Esc cancels)")
    sleep(PAUSE_S)
    # No overlay here, so no flight to wait for; the 400 ms hold still applies.
    cb = ex.ExecutorCallbacks(flight_scale=0.0)
    results = [
        ex.execute({"type": "type", "text": TEXT,
                    "explanation": "manual test: type Georgian"}, None, cb),
        ex.execute({"type": "click",
                    "target": {"x": args.x, "y": args.y, "space": "physical"},
                    "explanation": "manual test: click"}, None, cb),
    ]
    for r in results:
        where = f" at ({r.x}, {r.y})" if r.x is not None else ""
        say(f"{r.type:6s} {r.status:9s} {r.method:10s}{where}  {r.message}")
    if args.real and results[1].status == "done":
        cx, cy = ex.win.cursor_pos()
        say(f"cursor now at ({cx}, {cy}) — off by ({cx - args.x}, {cy - args.y}) px")
    ex.set_dry_run(False)
    return results


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    args = build_parser().parse_args()
    import agent.executor as ex
    from hotkey import StopHotkey
    StopHotkey(on_stop=lambda: print("Esc — cancelling"), key="esc",
               cancel_event=ex.cancel_event).start()
    results = run(args, ex)
    sys.exit(0 if all(r.ok for r in results) else 1)


if __name__ == "__main__":
    main()

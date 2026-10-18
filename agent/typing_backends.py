"""
How the executor types text — pick with CLICKY_TYPE_BACKEND:

  pyautogui  (default)  ASCII text with pyautogui.write(); anything else
                        (Georgian!) is pasted: clipboard + pyautogui.hotkey
                        ("ctrl", "v"), then the previous clipboard text is put
                        back. pyautogui.write() alone cannot type Georgian —
                        it only knows keys on the keyboard layout and silently
                        drops other characters.
  pywinauto             pywinauto.keyboard.send_keys — sends Unicode
                        characters directly (Georgian works), special keys
                        escaped so the text is typed literally.
  sendinput             the executor's own Unicode SendInput.

Private text never reaches this module: the executor refuses it first.
"""

from __future__ import annotations

import os
import time
from typing import Callable, Optional

BACKENDS = ("pyautogui", "pywinauto", "sendinput")
CLIPBOARD_RESTORE_DELAY_S = 0.35     # let the target app read the paste first


def configured_backend() -> str:
    b = os.getenv("CLICKY_TYPE_BACKEND", "pyautogui").strip().lower()
    return b if b in BACKENDS else "pyautogui"


def is_plain_ascii(text: str) -> bool:
    """Characters pyautogui.write() can actually type."""
    return all(32 <= ord(c) < 127 or c in "\n\t" for c in text)


_PWA_SPECIAL = set("{}+^%~()[]")


def escape_send_keys(text: str) -> str:
    """pywinauto send_keys treats {}+^%~() as commands — brace them so the
    text is typed exactly as written."""
    return "".join("{" + c + "}" if c in _PWA_SPECIAL else c for c in text)


def type_with_pyautogui(text: str, pyautogui=None, pyperclip=None,
                        sleep: Callable[[float], None] = time.sleep) -> str:
    """Returns how it typed: "pyautogui-write" or "pyautogui-paste"."""
    if pyautogui is None:
        import pyautogui                        # lazy: sets DPI awareness on import
        pyautogui.FAILSAFE = True               # mouse to a corner = emergency stop
    if is_plain_ascii(text):
        pyautogui.write(text, interval=0.02)
        return "pyautogui-write"
    if pyperclip is None:
        import pyperclip
    try:
        previous: Optional[str] = pyperclip.paste()
    except Exception:
        previous = None
    pyperclip.copy(text)
    try:
        pyautogui.hotkey("ctrl", "v")
        sleep(CLIPBOARD_RESTORE_DELAY_S)
    finally:
        if previous is not None:
            try:
                pyperclip.copy(previous)        # put the person's clipboard back
            except Exception:
                pass
    return "pyautogui-paste"


def type_with_pywinauto(text: str, keyboard=None) -> str:
    if keyboard is None:
        from pywinauto import keyboard
    keyboard.send_keys(escape_send_keys(text), pause=0.02, with_spaces=True,
                       with_tabs=True, with_newlines=True)
    return "pywinauto-keys"

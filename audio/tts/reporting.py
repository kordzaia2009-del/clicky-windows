"""
ReportingTTS — wraps any TTS provider and reports each line as it is spoken,
so the mascot can show the words coming out of its mouth. Behaviour of the
wrapped provider is unchanged; every other attribute (set_voice, stop, ...)
is passed straight through.
"""

from __future__ import annotations

from typing import Callable


class ReportingTTS:
    def __init__(self, inner, on_line: Callable[[str], None]):
        self._inner = inner
        self._on_line = on_line          # text = starting to say it, "" = done

    @property
    def inner(self):
        return self._inner

    def _report(self, text: str):
        try:
            self._on_line(text)
        except Exception:
            pass                          # a UI hiccup must never stop speech

    async def speak(self, text: str):
        if text and text.strip():
            self._report(text.strip())
        try:
            return await self._inner.speak(text)
        finally:
            self._report("")

    def __getattr__(self, name):
        return getattr(self._inner, name)

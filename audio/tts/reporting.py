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
        import time
        from timing import mark, since_ms
        t0 = time.perf_counter()
        if text and text.strip():
            self._report(text.strip())
        try:
            return await self._inner.speak(text)
        finally:
            self._report("")
            start_ms = None
            try:
                from audio import playback
                started = playback.last_start_time()
                if started >= t0:
                    start_ms = round((started - t0) * 1000)   # synthesis + decode
            except Exception:
                pass
            mark("tts.speak", since_ms(t0), start_speaking_ms=start_ms,
                 chars=len(text or ""), provider=type(self._inner).__name__)

    def __getattr__(self, name):
        return getattr(self._inner, name)

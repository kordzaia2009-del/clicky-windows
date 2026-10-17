"""
Google web speech STT — the free endpoint Chrome's voice typing uses, via the
SpeechRecognition package. No API key.

Local Whisper cannot transcribe Georgian: tested on clean synthesised speech,
base returned Latin-letter gibberish and small/medium returned random symbols
(and took 6-15 minutes on CPU). This endpoint returned the same samples word
for word in about a second, so it is the default when the language is "ka".

Trade-offs: audio leaves the machine (to Google), it needs internet, and the
endpoint is rate-limited and unofficial — for a production deployment, move
to Google Cloud Speech-to-Text or Azure Speech with a real key.
"""

from __future__ import annotations

import asyncio

from audio.stt.base_stt import BaseSTT
from audio.capture import trim_silence
from config import cfg

# Whisper-style ISO codes → the BCP-47 tags Google expects.
_GOOGLE_LANG = {
    "ka": "ka-GE", "en": "en-US", "ru": "ru-RU", "de": "de-DE", "fr": "fr-FR",
    "es": "es-ES", "it": "it-IT", "pt": "pt-BR", "el": "el-GR", "hi": "hi-IN",
}


class GoogleWebSTT(BaseSTT):

    def __init__(self):
        try:
            import speech_recognition as sr   # type: ignore
        except ImportError as e:
            raise ImportError(
                "Google speech recognition needs:  pip install SpeechRecognition"
            ) from e
        self._sr = sr
        self._recognizer = sr.Recognizer()

    async def transcribe(self, pcm_bytes: bytes, sample_rate: int = 16000) -> str:
        if not pcm_bytes:
            return ""
        pcm_bytes = trim_silence(pcm_bytes)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._run, pcm_bytes, sample_rate)

    def _run(self, pcm_bytes: bytes, sample_rate: int) -> str:
        code = cfg.whisper_language or "ka"
        lang = _GOOGLE_LANG.get(code, code)
        audio = self._sr.AudioData(pcm_bytes, sample_rate, 2)
        try:
            return self._recognizer.recognize_google(audio, language=lang).strip()
        except self._sr.UnknownValueError:
            return ""   # nothing intelligible — treated like silence
        except self._sr.RequestError as e:
            raise RuntimeError(
                f"მეტყველების ამოცნობა ვერ მოხერხდა — შეამოწმეთ ინტერნეტი. ({e})"
            ) from e

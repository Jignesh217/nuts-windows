"""Text-to-speech via Windows SAPI.

``pyttsx3`` wraps the SAPI5 COM interface; the voice is whatever the
user has set as default in Windows Settings -> Time & Language ->
Speech. We deliberately do NOT bring in cloud TTS (Edge-TTS, ElevenLabs)
to match Nuts's on-device-only stance.

Threading: SAPI calls block. We push spoken text onto a queue and a
single worker thread drains it, so the UI thread (and the SSE streamer)
stay responsive while a long response is being read aloud.

Cutoff: the caller can `cancel()` mid-utterance - useful when the user
presses push-to-talk again before the previous response finished.
"""
from __future__ import annotations

import queue
import threading

import pyttsx3


class Speaker:
    def __init__(self) -> None:
        self._engine = pyttsx3.init()
        # Slightly faster than the SAPI default - default reads slowly.
        self._engine.setProperty("rate", 190)
        self._q: "queue.Queue[str | None]" = queue.Queue()
        self._cancel = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def speak(self, text: str) -> None:
        """Enqueue a chunk to be spoken.

        We split on sentence boundaries before pushing so partial chunks
        from the SSE stream don't get spoken with weird mid-sentence
        prosody. Caller can stream many small chunks - we buffer until a
        period/question mark/newline lands.
        """
        if text:
            self._q.put(text)

    def cancel(self) -> None:
        """Interrupt any in-progress speech and drain the queue."""
        self._cancel.set()
        try:
            self._engine.stop()
        except Exception:
            pass
        # Drain pending utterances.
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        self._cancel.clear()

    def shutdown(self) -> None:
        self._q.put(None)

    # ----- internal --------------------------------------------------------

    def _run(self) -> None:
        buf = ""
        while True:
            item = self._q.get()
            if item is None:
                return
            buf += item
            # Speak as soon as we have a complete sentence. This matches
            # how Nuts streams audio chunk-by-chunk on Mac.
            while True:
                end = _sentence_end(buf)
                if end == -1:
                    break
                sentence, buf = buf[: end + 1], buf[end + 1 :].lstrip()
                if self._cancel.is_set():
                    buf = ""
                    break
                try:
                    self._engine.say(sentence)
                    self._engine.runAndWait()
                except Exception:
                    # SAPI dropped - skip this utterance, keep the loop alive.
                    pass


def _sentence_end(s: str) -> int:
    """Index of the next sentence-terminating punctuation, or -1."""
    best = -1
    for ch in (".", "!", "?", "\n"):
        i = s.rfind(ch)
        if i > best:
            best = i
    return best

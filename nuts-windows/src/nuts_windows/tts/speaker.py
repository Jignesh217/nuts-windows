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

import logging
import queue
import threading

import pyttsx3


_log = logging.getLogger("nuts.tts")


_FLUSH = object()   # sentinel: drain remaining buffer immediately


class Speaker:
    """Sentence-boundary-buffered TTS over Windows SAPI.

    Threading note (was a bug before the v0.1 review): pyttsx3 + SAPI is
    COM-bound. The engine MUST be created on the same thread that drives
    runAndWait(). We previously instantiated it on the main thread and
    used it from the worker thread, which caused silent failures on
    Windows. The engine is now created inside ``_run``.
    """

    def __init__(self) -> None:
        self._q: "queue.Queue[str | object | None]" = queue.Queue()
        self._cancel = threading.Event()
        # Engine handle is bound to whatever thread calls init() - we let
        # the worker thread do it. cancel() touches the engine from
        # outside, but engine.stop() is documented as safe to call from
        # any thread.
        self._engine: "pyttsx3.Engine | None" = None
        self._engine_ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def speak(self, text: str) -> None:
        """Enqueue a chunk to be spoken.

        We buffer until a sentence-ending punctuation so partial chunks
        from the SSE stream don't get spoken with weird mid-sentence
        prosody. Caller can stream many small chunks.
        """
        if text:
            _log.info("speak() queued %d chars (qsize=%d)", len(text), self._q.qsize())
            self._q.put(text)

    def flush(self) -> None:
        """Drain any remaining buffered text immediately.

        Call this once an upstream stream ends - otherwise the trailing
        fragment (e.g. a final clause without a period) sits in the
        buffer forever and the user never hears it.
        """
        _log.info("flush() requested")
        self._q.put(_FLUSH)

    def cancel(self) -> None:
        """Drop pending utterances WITHOUT calling engine.stop().

        IMPORTANT - the v0.x cancel called self._engine.stop() to cut off
        the currently-speaking sentence. That call has a longstanding
        pyttsx3 + Windows SAPI bug: stop() during runAndWait() leaves
        SAPI in a state where every subsequent say() returns instantly
        without producing audio. Result: the FIRST voice turn spoke,
        every turn after it queued text but the user heard silence.

        New behavior: just discard everything queued. The current
        sentence (already inside runAndWait) finishes naturally, then
        the worker proceeds to the new turn's content. Adds at most
        one stale sentence of audio - way better than silent forever.
        """
        _log.info("cancel() - draining queue (current sentence finishes)")
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    def shutdown(self) -> None:
        self._q.put(None)

    # ----- internal --------------------------------------------------------

    def _run(self) -> None:
        # Init the engine on THIS thread - SAPI COM apartment is per-thread.
        try:
            self._engine = pyttsx3.init()
            self._engine.setProperty("rate", 190)
        except Exception:
            # No SAPI? Mark ready anyway so callers don't deadlock. speak()
            # calls will queue but never play; the app degrades gracefully.
            self._engine_ready.set()
            return
        self._engine_ready.set()

        buf = ""
        while True:
            item = self._q.get()
            if item is None:
                return
            if item is _FLUSH:
                # Speak whatever is left, sentence-end or not.
                tail, buf = buf.strip(), ""
                if tail:
                    self._say(tail)
                continue
            buf += item   # type: ignore[operator]
            # Speak as soon as we have a complete sentence. This matches
            # how Nuts streams audio chunk-by-chunk on Mac.
            while True:
                end = _sentence_end(buf)
                if end == -1:
                    break
                sentence, buf = buf[: end + 1], buf[end + 1 :].lstrip()
                self._say(sentence)

    def _say(self, text: str) -> None:
        _log.info("_say speaking %d chars: %r", len(text), text[:80])
        try:
            self._engine.say(text)        # type: ignore[union-attr]
            self._engine.runAndWait()     # type: ignore[union-attr]
            _log.info("_say done")
        except Exception as e:
            # SAPI dropped - skip this utterance, keep the loop alive.
            _log.exception("_say FAILED: %s", e)


def _sentence_end(s: str) -> int:
    """Index of the next sentence-terminating punctuation, or -1."""
    best = -1
    for ch in (".", "!", "?", "\n"):
        i = s.rfind(ch)
        if i > best:
            best = i
    return best

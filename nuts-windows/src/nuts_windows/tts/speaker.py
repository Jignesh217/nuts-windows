"""Text-to-speech via Windows SAPI (direct COM, no pyttsx3).

Why direct COM and not pyttsx3?
  The pyttsx3 SAPI driver has a longstanding bug where runAndWait()
  returns BEFORE the audio buffer has actually drained. With the
  default settings, a 224-character utterance reports 'done' in
  ~850 ms - way too fast for actual playback - and the user hears
  nothing. Switching to direct comtypes + SAPI.SpVoice.Speak() with
  the synchronous flag actually blocks until the audio finishes, so
  every queued utterance is fully heard.

  comtypes is already in our deps (pyttsx3 depends on it). Going
  direct removes one fragile abstraction layer.

Public API stays the same: speak(text), flush(), cancel(),
shutdown(). The worker thread / queue model also stays the same -
SAPI is COM and the COM object MUST be created on the thread that
calls Speak() (STA apartment rules), so we still init inside _run.
"""
from __future__ import annotations

import logging
import queue
import threading


_log = logging.getLogger("nuts.tts")

# SAPI flags - constants from SpVoice (we don't import them from a
# generated tlb file because comtypes' tlb cache is brittle on first
# launch; the integers are stable for SAPI5).
SVSFDefault = 0                  # synchronous (blocks until done)
SVSFlagsAsync = 1
SVSFPurgeBeforeSpeak = 2
SVSFIsXML = 8

# SVSFDefault | SVSFIsNotXML happens to be 0, which the docs say is
# "treat input as plain text and block until done" - exactly what we
# want per utterance.
_SPEAK_FLAGS_BLOCKING = SVSFDefault


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
        # Holds the SAPI.SpVoice COM object once _run initializes it.
        # comtypes returns a duck-typed dynamic dispatch object; we
        # just call .Speak() / .Rate on it.
        self._engine = None  # type: ignore[assignment]
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
        # Init COM apartment + SAPI on THIS thread. CoInitialize is
        # required because we're using a STA-bound COM object from a
        # non-main thread; without it CreateObject silently fails on
        # some Windows configurations.
        engine = None
        try:
            import comtypes
            comtypes.CoInitialize()
            import comtypes.client
            engine = comtypes.client.CreateObject("SAPI.SpVoice")
            # Rate: SAPI scale is -10 (slowest) .. +10 (fastest), 0 default.
            # 1 = ~10% faster than default. Comfortable for spoken replies.
            engine.Rate = 1
            # Volume: 0..100. Default 100.
            engine.Volume = 100
            _log.info("SAPI ready (voice=%r rate=%d vol=%d)",
                      engine.Voice.GetDescription(), engine.Rate, engine.Volume)
        except Exception as e:
            _log.exception("SAPI init FAILED: %s", e)
            self._engine_ready.set()
            return

        self._engine = engine
        self._engine_ready.set()

        buf = ""
        while True:
            item = self._q.get()
            if item is None:
                return
            if item is _FLUSH:
                tail, buf = buf.strip(), ""
                if tail:
                    self._say(tail)
                continue
            buf += item   # type: ignore[operator]
            while True:
                end = _sentence_end(buf)
                if end == -1:
                    break
                sentence, buf = buf[: end + 1], buf[end + 1 :].lstrip()
                self._say(sentence)

    def _say(self, text: str) -> None:
        """Speak one sentence, BLOCKING until audio playback finishes.

        Uses SpVoice.Speak(text, 0) - the 0 flags = synchronous default,
        which actually waits for the audio buffer to drain (unlike
        pyttsx3.runAndWait which returned almost immediately).
        """
        if self._engine is None:
            _log.warning("_say called but engine=None; skipping")
            return
        _log.info("_say speaking %d chars: %r", len(text), text[:80])
        try:
            # SpVoice.Speak takes (text, flags). 0 = blocking + plain text.
            self._engine.Speak(text, _SPEAK_FLAGS_BLOCKING)
            _log.info("_say done")
        except Exception as e:
            _log.exception("_say FAILED: %s", e)


def _sentence_end(s: str) -> int:
    """Index of the next sentence-terminating punctuation, or -1."""
    best = -1
    for ch in (".", "!", "?", "\n"):
        i = s.rfind(ch)
        if i > best:
            best = i
    return best

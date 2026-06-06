"""Microphone capture + speech-to-text.

Push-to-talk model:
  Recorder.start()  -> begins buffering audio via sounddevice callback.
  Recorder.stop()   -> drains buffer, returns a Recording (WAV bytes).

Transcribe.run(wav_bytes) -> on-device Whisper transcript (or None if
the model isn't installed - the caller can fall back to cloud STT).

The Whisper backend is faster-whisper (CTranslate2) - same approach as
Clicky's WhisperKit on macOS, but cross-platform. First call downloads
the model (~150 MB for ``base``) into ``~/.cache/huggingface/hub`` and
caches it for every subsequent run. The download is lazy and threaded so
it doesn't block the Qt event loop.
"""
from __future__ import annotations

import io
import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np
import sounddevice as sd
import soundfile as sf


SAMPLE_RATE = 16_000   # Whisper's native input rate; no resample needed.
CHANNELS = 1


@dataclass(frozen=True, slots=True)
class Recording:
    """A captured push-to-talk utterance."""
    wav_bytes: bytes        # 16 kHz mono PCM WAV, ready to send/transcribe
    duration_s: float


class Recorder:
    """Background mic recorder driven by start()/stop().

    A single instance is reusable - calling start() while already
    recording is a noop. We hold the lock across start/stop so the
    sounddevice callback never sees a half-mutated state.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._chunks: list[np.ndarray] = []
        self._stream: Optional[sd.InputStream] = None

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                return
            self._chunks = []
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="float32",
                callback=self._on_chunk,
                blocksize=0,    # let PortAudio pick - lowest latency
            )
            self._stream.start()

    def stop(self) -> Optional[Recording]:
        """Stop recording and return the captured audio, or None if empty."""
        with self._lock:
            if self._stream is None:
                return None
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None
            chunks, self._chunks = self._chunks, []

        if not chunks:
            return None
        audio = np.concatenate(chunks, axis=0)
        if audio.size == 0:
            return None

        buf = io.BytesIO()
        sf.write(buf, audio, SAMPLE_RATE, format="WAV", subtype="PCM_16")
        duration = audio.shape[0] / float(SAMPLE_RATE)
        return Recording(wav_bytes=buf.getvalue(), duration_s=duration)

    # ----- internal --------------------------------------------------------

    def _on_chunk(self, indata: np.ndarray, frames, time, status) -> None:
        # sounddevice docs explicitly say "do not allocate or block here".
        # Appending to a list is O(1) and amortizes well enough that we get
        # ~no dropouts even at large buffer counts; the alternative
        # (preallocate a ring) is more code for negligible gain at our
        # expected push-to-talk durations (<30s).
        if status:
            # Underrun / overflow - log later; for now we just drop a frame
            # rather than break the stream.
            return
        # Copy because PortAudio reuses its buffer underneath us.
        self._chunks.append(indata.copy())


# ---------------------------------------------------------------------------
# Local speech-to-text via faster-whisper.
# ---------------------------------------------------------------------------
# Initialized lazily on first transcribe(); first call pays the model download
# cost (~150 MB). Subsequent calls hit the local cache. Falls back to None
# transparently if the dependency isn't installed - the worker can then do
# cloud STT.

_WHISPER_MODEL = None
_WHISPER_LOCK = threading.Lock()


def _whisper_init():
    """Return a loaded WhisperModel, or None if faster-whisper isn't installed."""
    global _WHISPER_MODEL
    if _WHISPER_MODEL is not None:
        return _WHISPER_MODEL
    with _WHISPER_LOCK:
        if _WHISPER_MODEL is not None:
            return _WHISPER_MODEL
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return None
        # "base" balances size (~150 MB) and quality; "small" is sharper
        # but ~3x bigger. device="auto" + compute_type="auto" picks GPU
        # when CUDA is present, else INT8 on CPU which is plenty fast.
        _WHISPER_MODEL = WhisperModel("base", device="auto", compute_type="auto")
        return _WHISPER_MODEL


def transcribe(wav_bytes: bytes) -> Optional[str]:
    """Transcribe WAV bytes with local Whisper. Returns text, or None if
    Whisper isn't installed / failed. Synchronous - call from a worker
    thread, never from the Qt main thread."""
    model = _whisper_init()
    if model is None:
        return None
    # faster-whisper wants a file-like object or path. BytesIO works.
    try:
        bio = io.BytesIO(wav_bytes)
        segments, _info = model.transcribe(bio, beam_size=1, language=None)
        return " ".join(s.text.strip() for s in segments).strip()
    except Exception:
        return None

"""Microphone capture + speech-to-text.

Push-to-talk model:
  start_recording() -> returns immediately, audio buffers accumulate in
    a background sounddevice callback.
  stop_recording()  -> drains the buffer, returns either:
                          * (audio_bytes, "wav") for the Worker to STT, or
                          * (text, "transcript") if we've already run STT locally.

Today we send raw WAV to the Worker because the on-device Whisper bit is
still a TODO. Wiring local Whisper:

  pip install faster-whisper
  # then below in stop_recording(), do:
  from faster_whisper import WhisperModel
  model = WhisperModel("base", device="auto", compute_type="auto")
  segments, _ = model.transcribe(wav_path, beam_size=1)
  text = " ".join(s.text for s in segments)

That gives us parity with Nuts's on-device WhisperKit. ``base`` is a
~150 MB model; ``small`` (~480 MB) is better. We default to ``base`` to
keep first-launch download under 200 MB.
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

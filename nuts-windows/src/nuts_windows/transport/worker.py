"""HTTP client for the Cloudflare Worker.

The Worker (see https://github.com/atharvalepse/nuts/tree/main/worker)
accepts a multipart POST containing:

  * ``screenshot``  JPEG bytes
  * ``audio``       WAV bytes (mono 16 kHz)  - OR -
  * ``transcript``  plain-text string (if local STT already ran)

It responds with a streaming SSE body where each ``data:`` line is a
chunk of model text. We yield each chunk as it arrives so the UI can
start playing TTS without waiting for the whole response.

Two-call alternative (not implemented yet, leave for later): split into
``/transcribe`` (audio in, text out) and ``/respond`` (image+text in,
text stream out). Useful when local Whisper is wired up - we'd skip
the upload entirely and only send the small transcript + screenshot.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Optional

import httpx


@dataclass(frozen=True, slots=True)
class WorkerRequest:
    screenshot_jpeg: bytes
    audio_wav: Optional[bytes] = None
    transcript: Optional[str] = None


class WorkerClient:
    def __init__(self, base_url: str, bearer: Optional[str]) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
        # http2=True keeps the connection warm across rapid push-to-talk
        # cycles; the worker upstream supports it.
        self._client = httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def stream_response(self, req: WorkerRequest) -> AsyncIterator[str]:
        """POST to /respond and yield text chunks as the SSE stream arrives."""
        files = [("screenshot", ("screen.jpg", req.screenshot_jpeg, "image/jpeg"))]
        data: dict[str, str] = {}
        if req.audio_wav is not None:
            files.append(("audio", ("speech.wav", req.audio_wav, "audio/wav")))
        if req.transcript is not None:
            data["transcript"] = req.transcript

        async with self._client.stream(
            "POST",
            f"{self._base}/respond",
            files=files,
            data=data,
            headers=self._headers,
        ) as resp:
            resp.raise_for_status()
            # Parse SSE: line starting with "data: " carries the chunk;
            # blank line separates events. We only care about ``data:``.
            buf = ""
            async for raw in resp.aiter_text():
                buf += raw
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.rstrip("\r")
                    if line.startswith("data:"):
                        chunk = line[5:].lstrip()
                        if chunk == "[DONE]":
                            return
                        yield chunk

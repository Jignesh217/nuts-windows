"""Screen capture - multi-monitor aware.

Uses `mss` because it's the fastest pure-Python option on Windows
(BitBlt under the hood) and handles multiple monitors natively. We
capture all monitors as one composited image; the model gets the full
desktop and the cursor-positioning protocol references
``screenN`` ordinals (matching Nuts's existing convention).

Returned bytes are JPEG-encoded (smaller payload than PNG for screen
content, and the vision model doesn't need lossless).
"""
from __future__ import annotations

import io
from dataclasses import dataclass

import mss
import mss.tools
from PIL import Image


@dataclass(frozen=True, slots=True)
class Screenshot:
    """A captured screen.

    Carries the raw bytes plus per-monitor geometry so downstream code
    can translate model output like ``[POINT:120,340:label:screen1]``
    back into desktop coordinates.
    """
    jpeg_bytes: bytes
    width: int
    height: int
    monitors: list[dict]   # list of {left, top, width, height} per monitor


def capture_all() -> Screenshot:
    """Grab the composited desktop. Fast (~5-15 ms on a typical machine)."""
    with mss.mss() as sct:
        # monitors[0] is the union of all displays; the rest are individual.
        union = sct.monitors[0]
        raw = sct.grab(union)
        # mss.grab returns BGRA; PIL wants RGB for JPEG.
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        out = io.BytesIO()
        # quality 92 = small text in IDE / chat UIs reads crisp on the
        # model side. Was 80 in v0.x; user reported the model giving
        # generic 'a chat app is open' answers instead of recognising
        # the actual content - turns out the JPEG was just too lossy
        # for the model to read window titles + first-line headers.
        # Payload still under ~300 KB at typical screen sizes.
        img.save(out, format="JPEG", quality=92, optimize=True)
        return Screenshot(
            jpeg_bytes=out.getvalue(),
            width=union["width"],
            height=union["height"],
            monitors=[m for m in sct.monitors[1:]],
        )

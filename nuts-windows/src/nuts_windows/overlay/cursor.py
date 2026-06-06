"""Cursor positioning + overlay marker.

The model embeds ``[POINT:x,y:label:screenN]`` tags in its response
(matching Nuts's existing convention). When we detect one mid-stream we:

  1. Translate (x, y, screenN) into absolute desktop coordinates using
     the per-monitor geometry captured by ``capture/screen.py``.
  2. Move the OS cursor with ``pyautogui.moveTo()``.
  3. Optionally draw a transparent ring at that point so the user sees
     where the assistant is pointing.

The overlay window is intentionally TODO: PyQt6 frameless +
``WA_TranslucentBackground`` + ``setWindowFlag(Qt.WindowStaysOnTopHint)``
gets you a transparent canvas; the painting itself is straightforward
QPainter. Skipped from this scaffold because the moveTo() call alone is
already useful and the overlay polish is independent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator, Optional

import pyautogui

# Match: [POINT:1234,567:label words:screen1]
#       group(1)=x  group(2)=y  group(3)=label  group(4)=screen ordinal
POINT_RE = re.compile(r"\[POINT:(\d+),(\d+):([^:\]]+):screen(\d+)\]")


@dataclass(frozen=True, slots=True)
class PointTag:
    x: int
    y: int
    label: str
    screen: int   # 1-indexed, matches the model's convention


def find_points(text: str) -> Iterator[PointTag]:
    """Yield every [POINT:...] tag in a chunk of model text."""
    for m in POINT_RE.finditer(text):
        yield PointTag(
            x=int(m.group(1)),
            y=int(m.group(2)),
            label=m.group(3).strip(),
            screen=int(m.group(4)),
        )


def strip_points(text: str) -> str:
    """Remove tags so they're never spoken aloud."""
    return POINT_RE.sub("", text)


def move_cursor(point: PointTag, monitors: list[dict]) -> Optional[tuple[int, int]]:
    """Translate a (x, y, screenN) tag to absolute coords and move there.

    Returns the absolute (x, y) used, or None if the screen index is bad.
    pyautogui's moveTo() is synchronous and very fast (~1 ms).
    """
    if point.screen < 1 or point.screen > len(monitors):
        return None
    mon = monitors[point.screen - 1]
    abs_x = mon["left"] + point.x
    abs_y = mon["top"] + point.y
    try:
        pyautogui.moveTo(abs_x, abs_y, duration=0)
    except pyautogui.FailSafeException:
        # User dragged the cursor to a corner to bail out - respect it.
        return None
    return abs_x, abs_y


# ---------------------------------------------------------------------------
# TODO: transparent always-on-top overlay window to visually mark the target.
#
#   class Overlay(QWidget):
#       def __init__(self) -> None:
#           super().__init__(None, Qt.FramelessWindowHint
#                                  | Qt.WindowStaysOnTopHint
#                                  | Qt.Tool)
#           self.setAttribute(Qt.WA_TranslucentBackground)
#           self.setAttribute(Qt.WA_TransparentForMouseEvents)
#           ...
#       def paintEvent(self, e):
#           QPainter ring centered on self._target
#
# Spawn one per monitor (geometry pinned to that monitor's screen rect),
# show() when a tag arrives, fade and hide() after ~1.5s.
# ---------------------------------------------------------------------------

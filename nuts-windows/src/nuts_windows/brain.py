"""Pluggable 'brain' interface.

Each brain takes a (transcript, screenshot, monitors) and yields response
chunks plus optional [POINT:x,y] tags - same shape the existing
``transport/worker.py`` SSE stream emits. The point of the abstraction:
the rest of the app already knows how to handle a stream of chunks, so
the brain can be a remote LLM, a local model, OR a fast offline rule
table that just demonstrates the wiring.

Today we ship two brains:

  * ``DemoBrain``  - rule-based. Recognises common 'click', 'point at',
    'look at', 'what is on screen' style commands and synthesises an
    answer + a [POINT:x,y] tag pointing at a relevant area of the
    screen. Lets the user verify voice -> STT -> response -> TTS -> arrow
    works without an API key.

  * ``WorkerBrain`` - thin adapter over WorkerClient. Same streaming
    interface but talks to a real Cloudflare Worker (the Anthropic
    proxy at clicky's worker/index.ts shape). Use this once a worker URL
    + bearer are configured.

Selection happens in app.py based on env vars and config:
  NUTS_BRAIN=demo            -> DemoBrain (default if worker URL missing)
  NUTS_BRAIN=worker          -> WorkerBrain
  unset, worker URL present  -> WorkerBrain
  unset, worker URL absent   -> DemoBrain
"""
from __future__ import annotations

import os
import random
import re
from dataclasses import dataclass
from typing import AsyncIterator, Optional, Protocol

from PyQt6.QtGui import QGuiApplication


@dataclass(frozen=True, slots=True)
class BrainContext:
    transcript: str
    screenshot_jpeg: Optional[bytes]
    monitors: list[dict]      # [{left, top, width, height}, ...]


class Brain(Protocol):
    async def stream(self, ctx: BrainContext) -> AsyncIterator[str]:
        """Yield response chunks (text + embedded [POINT:x,y:label:screenN]
        tags). End when the response is complete; consumer handles the rest.
        """
        ...


# ---------------------------------------------------------------------------
# DemoBrain - works offline, no API key.
# ---------------------------------------------------------------------------

# Crude command classifier. Pattern -> reply template. The template can
# include {x}, {y}, {screen}, {label} placeholders which get filled in
# below. Intent: smoke-test the whole pipeline (speech -> response -> arrow
# + TTS) without an LLM. Replace with WorkerBrain for real chat.
_COMMANDS = [
    # "show me <thing>" / "where is <thing>" / "point at <thing>"
    (re.compile(r"(point at|show me|where is|find)\s+(?P<thing>.+)", re.I),
     "Sure — pointing at the {thing}. [POINT:{x},{y}:{thing}:screen1]"),
    # "click on <thing>"
    (re.compile(r"click(?: on)?\s+(?P<thing>.+)", re.I),
     "Okay, here is the {thing}. Move your cursor there to click. [POINT:{x},{y}:{thing}:screen1]"),
    # "open <something>" / "launch <something>"
    (re.compile(r"(open|launch|start)\s+(?P<thing>.+)", re.I),
     "{thing.capitalize} can be opened from the taskbar at the bottom of your screen. [POINT:{x},{y}:taskbar:screen1]"),
    # "what is on the screen" / "what is this"
    (re.compile(r"(what.{0,8}(on the screen|here|this)|describe.{0,15}screen)", re.I),
     "I can see your desktop. To analyse it, connect a real LLM via NUTS_WORKER_URL — for now I am running in demo mode."),
    # "hello" / "hi" / "test"
    (re.compile(r"^(hello|hi|hey|test).{0,8}$", re.I),
     "Hi! I am listening. Try saying 'point at the start menu' or 'where is the clock'."),
]


class DemoBrain:
    """Offline rule-based brain. Returns plausible chat + arrow tags."""

    async def stream(self, ctx: BrainContext) -> AsyncIterator[str]:
        t = ctx.transcript.strip()
        if not t:
            yield "I did not catch that. Try again?"
            return

        # Pick a target point on the primary screen based on the command.
        # We don't actually know where things are without a vision model -
        # but for the demo we approximate: 'clock' -> bottom-right,
        # 'start menu' -> bottom-left, otherwise center.
        screen = QGuiApplication.primaryScreen().geometry()
        cx = screen.left() + screen.width() // 2
        cy = screen.top() + screen.height() // 2
        bl_x = screen.left() + 40
        bl_y = screen.bottom() - 24
        br_x = screen.right() - 60
        br_y = screen.bottom() - 24

        x, y = cx, cy
        t_low = t.lower()
        if "clock" in t_low or "time" in t_low or "date" in t_low:
            x, y = br_x, br_y
        elif "start" in t_low or "menu" in t_low or "windows button" in t_low:
            x, y = bl_x, bl_y
        elif "search" in t_low:
            x, y = screen.left() + screen.width() // 4, screen.bottom() - 24
        elif "task" in t_low or "bar" in t_low:
            x, y = cx, screen.bottom() - 24

        for pattern, template in _COMMANDS:
            m = pattern.search(t)
            if not m:
                continue
            thing = (m.groupdict().get("thing") or "it").strip()
            # Stream the response in two chunks so the panel and TTS see
            # progress, not a single blast at the end.
            chunks = self._format(template, thing=thing, x=x, y=y).split(". ")
            for i, c in enumerate(chunks):
                yield c + ("." if i < len(chunks) - 1 else "")
            return

        # No pattern matched - friendly fallback.
        yield (
            f"You said: \"{t}\". I am running in demo mode without a real "
            f"language model. Set NUTS_WORKER_URL to point at a Cloudflare "
            f"Worker (see the nuts repo for one) and I can actually answer."
        )

    @staticmethod
    def _format(template: str, *, thing: str, x: int, y: int) -> str:
        out = template
        # We can't use .format() because {thing.capitalize} isn't a valid
        # format spec. Do simple substitutions instead.
        out = out.replace("{thing.capitalize}", thing[:1].upper() + thing[1:])
        out = out.replace("{thing}", thing)
        out = out.replace("{x}", str(x))
        out = out.replace("{y}", str(y))
        return out


# ---------------------------------------------------------------------------
# WorkerBrain - real worker / LLM backend.
# ---------------------------------------------------------------------------


class WorkerBrain:
    """Adapter so app.py can treat a WorkerClient like a Brain."""

    def __init__(self, client) -> None:
        # We accept Any to avoid the circular import between brain.py and
        # transport.worker - the only methods we use are stream_response()
        # and aclose().
        self._client = client

    async def stream(self, ctx: BrainContext) -> AsyncIterator[str]:
        from nuts_windows.transport.worker import WorkerRequest
        req = WorkerRequest(
            screenshot_jpeg=ctx.screenshot_jpeg or b"",
            transcript=ctx.transcript or None,
        )
        async for chunk in self._client.stream_response(req):
            yield chunk


# ---------------------------------------------------------------------------
# Selection helper.
# ---------------------------------------------------------------------------


def pick_brain(worker_url: Optional[str], token: Optional[str]) -> Brain:
    """Return whichever brain matches the current config."""
    forced = (os.environ.get("NUTS_BRAIN") or "").lower()
    if forced == "demo":
        return DemoBrain()
    if forced == "worker" and worker_url:
        from nuts_windows.transport.worker import WorkerClient
        return WorkerBrain(WorkerClient(worker_url, token))
    # Auto: prefer worker if URL is configured AND looks like a real /respond
    # endpoint (clicky-style), else demo. Today we assume /respond is
    # available when worker_url is set; if it isn't, the user gets clear
    # transport errors which is fine for now.
    if worker_url and worker_url.endswith(("/mcp",)):
        # akhrots.com/mcp is the JSON-RPC MCP server, not a /respond SSE.
        # Demo brain until a proper worker is wired up.
        return DemoBrain()
    if worker_url:
        from nuts_windows.transport.worker import WorkerClient
        return WorkerBrain(WorkerClient(worker_url, token))
    return DemoBrain()

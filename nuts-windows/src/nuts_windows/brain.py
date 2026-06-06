"""Pluggable 'brain' interface.

Each brain takes a (transcript, screenshot, monitors) and yields response
chunks plus optional [POINT:x,y] tags - same shape the rest of the app
already knows how to handle.

Three brains ship today:

  * ``DemoBrain`` - rule-based offline. Recognises common 'click',
    'point at', 'where is X' commands and synthesises a reply +
    [POINT:x,y] tag. Smoke-test mode for verifying STT -> arrow works
    without an API key. 'Demo mode' that the user wants to leave.

  * ``AnthropicBrain`` - REAL Claude with vision. Streams SSE chunks
    from the Anthropic Messages API. Sees your screenshot, decides
    where to point, talks back. This is production mode.
    Requires ANTHROPIC_API_KEY env var or NUTS_ANTHROPIC_KEY.

  * ``WorkerBrain`` - thin adapter over a Cloudflare Worker (clicky's
    /respond SSE shape). Useful when you want to hide the API key on
    a server and have multiple users share it.

Selection happens in pick_brain():
  NUTS_BRAIN=demo                  -> DemoBrain
  NUTS_BRAIN=anthropic             -> AnthropicBrain
  NUTS_BRAIN=worker + worker_url   -> WorkerBrain
  unset, ANTHROPIC_API_KEY set     -> AnthropicBrain (production default)
  unset, worker_url is /respond    -> WorkerBrain
  unset, otherwise                 -> DemoBrain
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
# AnthropicBrain - production-mode real LLM.
# ---------------------------------------------------------------------------

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

# System prompt mirrors clicky's CompanionManager.swift conventions: the
# model knows it is an on-screen assistant and that pointing is done via
# the [POINT:x,y:label:screenN] tag. screenN is always 1 unless future
# multi-monitor support is added on the capture side.
_SYSTEM_PROMPT = """\
You are Akhort, a friendly on-screen assistant. The user is asking about \
what's on their screen and you can see a screenshot of it. Keep replies \
short and spoken-style - they will be read aloud by a text-to-speech voice.

When the user asks you to point at something on screen, or when showing \
them where to click would help, embed a pointing tag in your reply:

  [POINT:<x>,<y>:<label>:screen1]

where x and y are pixel coordinates ON THE SCREENSHOT and label is a 1-3 \
word description of what you're pointing at (it will appear next to the \
arrow). Only include ONE tag per reply; if multiple things are relevant, \
pick the most important.

Be concise. Two short sentences is usually enough.\
"""


class AnthropicBrain:
    """Real Claude with vision. Streams SSE chunks via the Messages API."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 400,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens

    async def stream(self, ctx: BrainContext) -> AsyncIterator[str]:
        import base64
        import httpx

        # Build the multimodal content block: screenshot first (Anthropic
        # recommends image before text for best attention), then the
        # transcribed prompt.
        content = []
        if ctx.screenshot_jpeg:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(ctx.screenshot_jpeg).decode("ascii"),
                },
            })
        content.append({
            "type": "text",
            "text": ctx.transcript or "What do you see?",
        })

        body = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": _SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": content}],
            "stream": True,
        }

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "POST", ANTHROPIC_API_URL, json=body, headers=headers,
            ) as resp:
                resp.raise_for_status()
                # SSE parser: data: lines carry JSON event objects. We
                # only care about content_block_delta with text_delta
                # type - that's the per-token text stream.
                buf = ""
                async for raw in resp.aiter_text():
                    buf += raw
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.rstrip("\r")
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if not payload or payload == "[DONE]":
                            continue
                        try:
                            evt = __import__("json").loads(payload)
                        except Exception:
                            continue
                        if evt.get("type") != "content_block_delta":
                            continue
                        delta = evt.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                yield text


# ---------------------------------------------------------------------------
# OpenAI-compatible brain (Grok / OpenAI / custom).
# ---------------------------------------------------------------------------
#
# xAI Grok and OpenAI both speak the same /v1/chat/completions shape, so a
# single client handles them just by swapping base URL + model id.
# Custom self-hosted endpoints (like akhrots.com if it exposes a
# chat-completions surface someday) also drop right in.

PROVIDER_DEFAULTS = {
    "grok": {
        "base_url": "https://api.x.ai/v1",
        "model": "grok-2-vision-1212",   # vision-capable; required for screen grounding
        "vision": True,
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",          # cheaper than gpt-4o; supports vision
        "vision": True,
    },
}


class OpenAICompatibleBrain:
    """Streaming chat-completions client. Works with Grok, OpenAI, or any
    drop-in compatible endpoint (custom: pass your own base_url + model).
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        model: str,
        supports_vision: bool = True,
        max_tokens: int = 400,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._supports_vision = supports_vision
        self._max_tokens = max_tokens

    async def stream(self, ctx: BrainContext) -> AsyncIterator[str]:
        import base64, json as _json
        import httpx

        # Build the user message. Vision-capable models accept a content
        # ARRAY mixing text and image_url parts. Text-only models get
        # plain string content + a note that we can't see the screen.
        if self._supports_vision and ctx.screenshot_jpeg:
            b64 = base64.b64encode(ctx.screenshot_jpeg).decode("ascii")
            user_content = [
                {"type": "image_url", "image_url": {
                    "url": f"data:image/jpeg;base64,{b64}",
                }},
                {"type": "text", "text": ctx.transcript or "What do you see?"},
            ]
        else:
            user_content = ctx.transcript or "What do you see?"

        body = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "stream": True,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        }

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "POST", f"{self._base_url}/chat/completions",
                json=body, headers=headers,
            ) as resp:
                resp.raise_for_status()
                buf = ""
                async for raw in resp.aiter_text():
                    buf += raw
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.rstrip("\r")
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if not payload or payload == "[DONE]":
                            continue
                        try:
                            evt = _json.loads(payload)
                        except Exception:
                            continue
                        # OpenAI-style streaming: choices[0].delta.content
                        try:
                            text = evt["choices"][0]["delta"].get("content")
                        except (KeyError, IndexError, TypeError):
                            continue
                        if text:
                            yield text


# ---------------------------------------------------------------------------
# Selection helper.
# ---------------------------------------------------------------------------


def pick_brain(worker_url: Optional[str], token: Optional[str]) -> Brain:
    """Return whichever brain matches the current config.

    Priority order:
      1. NUTS_BRAIN env override (demo / anthropic / grok / openai / worker)
      2. Persisted brain settings from the in-app Settings dialog
      3. ANTHROPIC_API_KEY env back-compat
      4. Worker URL fallback
      5. DemoBrain (last resort - offline rules)
    """
    import logging
    log = logging.getLogger("nuts.brain")

    # Load persisted settings from keyring (set via in-app Settings dialog).
    from nuts_windows import config as _config
    settings = _config.load_brain_settings()
    log.info("brain settings: provider=%s configured=%s",
             settings.provider, settings.is_configured)

    forced = (os.environ.get("NUTS_BRAIN") or "").lower()

    # 1. Env-var override takes precedence.
    if forced:
        provider = forced
    else:
        provider = settings.provider

    # 2. Resolve.
    if provider == "demo":
        log.info("brain: DemoBrain (offline, no key required)")
        return DemoBrain()

    if provider == "anthropic":
        key = settings.api_key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("NUTS_ANTHROPIC_KEY")
        if not key:
            log.warning("provider=anthropic but no API key - falling back to DemoBrain")
            return DemoBrain()
        log.info("brain: AnthropicBrain (live)")
        return AnthropicBrain(key)

    if provider in ("grok", "openai"):
        key = settings.api_key
        if not key:
            log.warning("provider=%s but no API key - falling back to DemoBrain", provider)
            return DemoBrain()
        defaults = PROVIDER_DEFAULTS[provider]
        base_url = settings.base_url or defaults["base_url"]
        model = settings.model or defaults["model"]
        log.info("brain: OpenAICompatibleBrain provider=%s model=%s base=%s",
                 provider, model, base_url)
        return OpenAICompatibleBrain(
            api_key=key, base_url=base_url, model=model,
            supports_vision=defaults.get("vision", True),
        )

    if provider == "custom":
        key = settings.api_key
        base = settings.base_url
        if not (key and base):
            log.warning("provider=custom needs both key and base_url - falling back to DemoBrain")
            return DemoBrain()
        model = settings.model or "gpt-4o-mini"
        log.info("brain: OpenAICompatibleBrain CUSTOM model=%s base=%s", model, base)
        return OpenAICompatibleBrain(api_key=key, base_url=base, model=model)

    if provider == "worker" and worker_url:
        from nuts_windows.transport.worker import WorkerClient
        log.info("brain: WorkerBrain")
        return WorkerBrain(WorkerClient(worker_url, token))

    log.info("brain: provider=%s unknown - DemoBrain", provider)
    return DemoBrain()

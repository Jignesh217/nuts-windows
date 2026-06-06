"""Local context memory - the "remember this" feature.

Clicky's Nuts has a button + voice command that captures the current
screen description and saves it for future context injection. We mirror
that here:

  * append(entry)  - store a new memory entry with timestamp.
  * recent(n)      - last N entries (in order), for prompt injection.
  * search(query)  - simple substring match for opportunistic recall.

Storage is a plain JSONL file under %LOCALAPPDATA%\\Akhort\\memory.jsonl.
One line per entry. Append-only - easy to back up, easy to inspect,
survives crashes. No DB dependency.

A future iteration can swap in a real vector store (the dashboard /
orchestration backend has one) without changing this module's API.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Optional


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    timestamp: float           # POSIX time
    summary: str               # what we want to remember (model-generated)
    source: str                # "voice" | "auto" | "manual"
    screenshot_b64: Optional[str] = None   # optional thumbnail (small jpeg)


class Memory:
    def __init__(self, path: Optional[Path] = None) -> None:
        if path is None:
            base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
            path = base / "Akhort" / "memory.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = threading.Lock()
        self._cache: list[MemoryEntry] = self._load()

    def append(self, summary: str, *, source: str = "voice",
               screenshot_b64: Optional[str] = None) -> MemoryEntry:
        """Save a new memory entry. Returns the persisted entry."""
        entry = MemoryEntry(
            timestamp=time.time(),
            summary=summary.strip(),
            source=source,
            screenshot_b64=screenshot_b64,
        )
        with self._lock:
            self._cache.append(entry)
            # Append-and-flush so a crash doesn't lose the entry.
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
                f.flush()
                # fsync is overkill here - localappdata is local disk and
                # the OS flushes regularly. Skip the latency.
        return entry

    def recent(self, n: int = 10) -> list[MemoryEntry]:
        """Last N entries, newest last (chronological)."""
        with self._lock:
            return list(self._cache[-n:])

    def search(self, query: str, *, limit: int = 5) -> list[MemoryEntry]:
        """Cheap substring match. Case-insensitive."""
        if not query:
            return []
        q = query.lower()
        with self._lock:
            hits = [e for e in self._cache if q in e.summary.lower()]
        return hits[-limit:]

    def clear(self) -> None:
        """Drop all entries on disk. Used by Sign-Out."""
        with self._lock:
            self._cache.clear()
            try:
                self._path.unlink()
            except FileNotFoundError:
                pass

    # ----- internal -------------------------------------------------------

    def _load(self) -> list[MemoryEntry]:
        if not self._path.exists():
            return []
        out: list[MemoryEntry] = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    out.append(MemoryEntry(**obj))
                except (json.JSONDecodeError, TypeError):
                    # Skip malformed lines instead of bailing - one bad
                    # write shouldn't lose the whole history.
                    continue
        return out


# ----- voice-command parser ----------------------------------------------

# Heuristics for detecting "remember this" / "save this" patterns in
# transcribed speech. Run BEFORE sending to the model so we can intercept
# the command, save locally, and either skip the model call or pass the
# memory as context for a confirmation response.

_REMEMBER_PHRASES = (
    "remember this",
    "save this",
    "note this",
    "remember that",
    "keep this in mind",
)
_RECALL_PHRASES = (
    "what do you remember about",
    "do you remember",
    "what did i tell you about",
    "remind me about",
)


def is_remember(transcript: str) -> bool:
    t = transcript.strip().lower()
    return any(p in t for p in _REMEMBER_PHRASES)


def is_recall(transcript: str) -> bool:
    t = transcript.strip().lower()
    return any(p in t for p in _RECALL_PHRASES)


def extract_remember_payload(transcript: str) -> str:
    """Strip the trigger phrase, return the rest as the payload."""
    t = transcript.strip()
    lower = t.lower()
    for p in _REMEMBER_PHRASES:
        i = lower.find(p)
        if i >= 0:
            return (t[:i] + t[i + len(p):]).strip(" ,.:;-")
    return t


def extract_recall_query(transcript: str) -> str:
    t = transcript.strip()
    lower = t.lower()
    for p in _RECALL_PHRASES:
        i = lower.find(p)
        if i >= 0:
            return (t[i + len(p):]).strip(" ,.:;-?")
    return t

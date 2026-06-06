"""Global push-to-talk hotkey.

We use ``pynput.keyboard.GlobalHotKeys`` rather than the ``keyboard``
package because the latter requires admin privileges on Windows
(intercepts via a low-level keyboard hook driver). pynput's hook is
user-level and works without elevation.

Push-to-talk semantics:
  * ``on_press``  -> emit a "start" callback, begin recording
  * ``on_release`` -> emit a "stop" callback, send the recording

pynput's ``GlobalHotKeys`` only fires once-per-press by default. We use
the lower-level ``Listener`` to get both edges.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from pynput import keyboard


_DEFAULT_COMBO = {keyboard.Key.ctrl, keyboard.Key.alt}


def _parse(combo: str) -> set:
    """Parse a pynput-style ``<ctrl>+<alt>`` string into a set of Keys.

    We intentionally support only a small subset (modifier-key combinations).
    Anything else falls back to the default. This is good enough for a
    push-to-talk hotkey; richer parsing comes when we expose a UI for it.
    """
    parts = {p.strip().lower().strip("<>") for p in combo.split("+")}
    mapped = set()
    for p in parts:
        if p in ("ctrl", "control"):
            mapped.update({keyboard.Key.ctrl_l, keyboard.Key.ctrl_r, keyboard.Key.ctrl})
        elif p in ("alt", "option"):
            mapped.update({keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.alt})
        elif p in ("shift",):
            mapped.update({keyboard.Key.shift_l, keyboard.Key.shift_r, keyboard.Key.shift})
        elif p in ("cmd", "win", "super"):
            mapped.update({keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r})
        else:
            # Unknown token - bail out to the safe default.
            return _DEFAULT_COMBO
    return mapped


class PushToTalk:
    """Manage push-to-talk state for one modifier-only combo.

    The Listener thread invokes our callbacks; we keep ``_pressed`` to
    debounce key autorepeat so ``on_start`` only fires once per press.
    """

    def __init__(
        self,
        combo: str,
        on_start: Callable[[], None],
        on_stop: Callable[[], None],
    ) -> None:
        self._combo = _parse(combo)
        self._on_start = on_start
        self._on_stop = on_stop
        self._held: set = set()
        self._active = False
        self._lock = threading.Lock()
        self._listener: Optional[keyboard.Listener] = None

    def start(self) -> None:
        self._listener = keyboard.Listener(
            on_press=self._handle_press,
            on_release=self._handle_release,
        )
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> None:
        if self._listener:
            self._listener.stop()
            self._listener = None

    # ----- internal -------------------------------------------------------

    def _handle_press(self, key) -> None:
        with self._lock:
            self._held.add(key)
            if self._active:
                return
            # All combo keys held? Pynput exposes each modifier in a
            # location-specific variant (Key.ctrl_l vs Key.ctrl_r); the
            # parsed combo covers both, so we just check any-of for each
            # logical modifier family.
            if self._combo_satisfied():
                self._active = True
                try:
                    self._on_start()
                except Exception:
                    # Swallow callback errors to avoid killing the hook thread.
                    pass

    def _handle_release(self, key) -> None:
        with self._lock:
            self._held.discard(key)
            if self._active and not self._combo_satisfied():
                self._active = False
                try:
                    self._on_stop()
                except Exception:
                    pass

    def _combo_satisfied(self) -> bool:
        return bool(self._held & self._combo) and \
            all(any(k in self._held for k in _family(target)) for target in self._combo)


def _family(key) -> set:
    """All location-variants of a modifier key (left/right + canonical)."""
    name = getattr(key, "name", str(key))
    if "ctrl" in name:
        return {keyboard.Key.ctrl_l, keyboard.Key.ctrl_r, keyboard.Key.ctrl}
    if "alt" in name:
        return {keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.alt}
    if "shift" in name:
        return {keyboard.Key.shift_l, keyboard.Key.shift_r, keyboard.Key.shift}
    if "cmd" in name:
        return {keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r}
    return {key}

"""Application wiring.

Pulls together every isolated piece (config, bootstrap, tray, hotkey,
capture, transport, tts, overlay) and runs the Qt event loop.

Threading model:
  * Qt event loop runs on the main thread.
  * pynput Listener runs its own thread; callbacks from it are NOT safe
    to touch Qt widgets directly. We marshal back to the main thread via
    ``QMetaObject.invokeMethod`` (queued connection) - in this scaffold,
    via QTimer.singleShot(0, callable) which is the lighter idiom.
  * The Worker streaming + TTS run inside an asyncio loop on a dedicated
    background thread - long-running I/O has no business on the Qt loop.

On startup we run :func:`bootstrap.try_auto_signin` once. If it finds a
config, the rest of the app starts already authenticated; if not, the
tray tooltip nudges the user at akhrots.com/app.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from nuts_windows import bootstrap, config
from nuts_windows.capture.audio import Recorder, transcribe
from nuts_windows.capture.screen import capture_all
from nuts_windows import memory as memory_mod
from nuts_windows.brain import BrainContext, pick_brain
from nuts_windows.hotkey import PushToTalk
from nuts_windows.overlay import cursor as cursor_mod
from nuts_windows.overlay.indicator import CursorIndicator, MicBadge
from nuts_windows.overlay.hover_bar import (
    HoverBar,
    STATE_IDLE,
    STATE_LISTENING,
    STATE_BUSY,
)
from nuts_windows.overlay.spring_arrow import (
    SpringArrow,
    STATE_IDLE as ARROW_IDLE,
    STATE_LISTENING as ARROW_LISTENING,
)
# ControlPanel was the old separate floating window; replaced by HoverBar
# which is BOTH the persistent indicator and the expanded panel. Kept the
# import absent so any stale references blow up loudly instead of
# silently importing dead code.
from nuts_windows.memory import Memory
from nuts_windows.transport.worker import WorkerClient, WorkerRequest
from nuts_windows.tray import Tray
from nuts_windows.tts.speaker import Speaker


class _AsyncWorker:
    """One asyncio loop on its own thread for streaming I/O."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro) -> None:
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


class Application(QObject):
    # Cross-thread signals. The pynput keyboard listener runs in its own
    # thread and CANNOT touch Qt widgets directly. Signals dispatched
    # across threads automatically use Qt.QueuedConnection, which puts
    # the handler call on the Qt main thread event loop. This replaces
    # the QTimer.singleShot pattern that silently failed - QTimer
    # scheduled from a non-Qt thread doesn't reliably wake the Qt loop.
    sig_hotkey_press = pyqtSignal()
    sig_hotkey_release = pyqtSignal()

    def __init__(self, qt_app: QApplication) -> None:
        super().__init__()
        self._qt = qt_app
        bootstrap.try_auto_signin()
        self._cfg = config.load()
        self._recorder = Recorder()
        self._speaker = Speaker()
        self._async = _AsyncWorker()
        self._client: Optional[WorkerClient] = None

        # Visual overlays. ControlPanel is the floating window that pops
        # up when the user clicks the tray icon. CursorIndicator is the
        # ring that tracks the cursor while the user is holding the
        # push-to-talk hotkey. PointArrow is the marker drawn when the
        # model emits [POINT:x,y] tags.
        self._cursor_indicator = CursorIndicator()
        self._mic_badge = MicBadge()
        # SpringArrow follows the cursor with spring physics, redirects
        # to any [POINT:x,y] target the model emits, and reads its color
        # from the user's saved choice in config (color picker in the
        # HoverBar below sets it).
        self._spring_arrow = SpringArrow()
        self._spring_arrow.set_color(self._cfg.arrow_color)
        self._spring_arrow.start()
        self._spring_arrow.set_state(ARROW_IDLE)
        # HoverBar replaces both the old PersistentBadge AND the old
        # floating ControlPanel. It IS the persistent indicator (small
        # horizontal pill above the clock) AND the full panel (expanded
        # on hover). All the panel buttons + color picker live there.
        # We treat self._panel as an alias so existing code that calls
        # self._panel.set_status() etc. still works.
        self._panel = HoverBar()
        self._panel.set_active_color(self._cfg.arrow_color)
        self._panel.quit_requested.connect(self._quit)
        self._panel.signout_requested.connect(self._handle_signout)
        self._panel.open_dashboard_requested.connect(self._open_dashboard)
        self._panel.color_chosen.connect(self._on_color_chosen)
        self._panel.settings_requested.connect(self._open_settings)
        self._panel.show_()
        self._panel.set_state(STATE_IDLE)
        # Used by every legacy line that called self._persistent_badge -
        # they all just want to flip the state-color, which the HoverBar
        # already exposes via set_state(). Alias keeps the diff small.
        self._persistent_badge = self._panel
        # Memory: local on-disk JSONL store for "remember this" voice
        # commands. Loaded once, written incrementally. Lives in the
        # same %LOCALAPPDATA%\Akhort directory as the log file.
        self._memory = Memory()
        # Brain selection: DemoBrain (offline rules + spring-arrow
        # responses) when no API key, AnthropicBrain (real Claude with
        # vision) when an API key is configured. See brain.py
        # pick_brain() for the exact selection rules.
        self._brain = pick_brain(self._cfg.worker_url, self._cfg.token)
        # Show the brain in the panel so the user knows whether they're
        # in demo mode or talking to a real LLM. Done after _panel is
        # constructed so the call lands on the live widget.
        brain_name = type(self._brain).__name__
        if brain_name == "DemoBrain":
            self._panel.set_signin("DEMO mode — open Settings to add an API key")
        elif brain_name == "AnthropicBrain":
            self._panel.set_signin("Claude vision (Anthropic) — live")
        elif brain_name == "OpenAICompatibleBrain":
            from nuts_windows import config as _cfg
            s = _cfg.load_brain_settings()
            self._panel.set_signin(f"{s.provider.capitalize()} — live")
        elif brain_name == "WorkerBrain":
            self._panel.set_signin("Worker proxy — live")
        # Pre-load Whisper in the background so the FIRST push-to-talk
        # turn doesn't pay the ~10s 'first call downloads the model'
        # cost. Subsequent calls are then ~0.5-1.5s with the 'tiny'
        # model + INT8 quantization.
        threading.Thread(target=self._prewarm_whisper, daemon=True).start()

        self._tray = Tray(
            qt_app,
            on_reload=self._reload,
            on_quit=self._quit,
            # Tray left-click now just briefly shows the bar (in case it
            # was hidden by some other app's overlay) - the HoverBar
            # itself handles expansion on actual hover. No more separate
            # toggle window.
            on_left_click=self._panel.show_,
            on_test_arrow=self._demo_arrow,
        )
        # Wire the cross-thread signals to the main-thread turn handlers
        # BEFORE starting the pynput listener so we never miss an early
        # press. QueuedConnection is implicit when the threads differ.
        self.sig_hotkey_press.connect(self._begin_turn)
        self.sig_hotkey_release.connect(self._end_turn)
        self._hotkey = PushToTalk(
            self._cfg.hotkey,
            on_start=self._hotkey_press,
            on_stop=self._hotkey_release,
        )
        self._hotkey.start()
        self._last_screenshot = None
        self._sync_panel_state()

    # ----- lifecycle -------------------------------------------------------

    def shutdown(self) -> None:
        self._hotkey.stop()
        self._speaker.shutdown()
        if self._client is not None:
            self._async.submit(self._client.aclose())
        self._async.stop()

    def _reload(self) -> None:
        bootstrap.try_auto_signin()
        self._cfg = config.load()

    def _prewarm_whisper(self) -> None:
        """Background-load the Whisper model so the first push-to-talk
        turn doesn't take 10+ seconds for the model download / first
        compile. Logged so we can see in Nuts.log when it's ready."""
        wlog = __import__("logging").getLogger("nuts.whisper")
        wlog.info("pre-warm START (background)")
        try:
            from nuts_windows.capture.audio import _whisper_init
            m = _whisper_init()
            wlog.info("pre-warm DONE (model=%s)", type(m).__name__ if m else "None")
        except Exception as e:
            wlog.exception("pre-warm FAILED: %s", e)

    def _demo_arrow(self) -> None:
        """Smoke-test: fire the spring arrow at a random spot on the
        primary screen so the user can see the physics without needing a
        live model response. Wired to the tray menu's 'Test arrow' item.
        """
        from random import randint, choice
        from PyQt6.QtGui import QGuiApplication as _Q
        screen = _Q.primaryScreen().geometry()
        # Pick a random point well inside the screen bounds.
        margin = 80
        x = randint(screen.left() + margin, screen.right() - margin)
        y = randint(screen.top() + margin, screen.bottom() - margin)
        label = choice([
            "right here!",
            "this button",
            "look at this",
            "your target",
            "press this",
        ])
        self._spring_arrow.fly_to(x, y, label=label)
        self._sync_panel_state()

    def _quit(self) -> None:
        self.shutdown()
        self._qt.quit()

    def _open_dashboard(self) -> None:
        import webbrowser
        webbrowser.open("https://akhrots.com/app")

    def _handle_signout(self) -> None:
        config.clear_credentials()
        self._cfg = config.load()
        self._sync_panel_state()

    def _open_settings(self) -> None:
        """Lazily build the settings dialog so we don't pay the QWidget
        startup cost until the user actually clicks the gear."""
        if not hasattr(self, "_settings_dialog") or self._settings_dialog is None:
            from nuts_windows.overlay.settings_dialog import SettingsDialog
            self._settings_dialog = SettingsDialog()
            self._settings_dialog.saved.connect(self._reload_brain)
        self._settings_dialog.show_centered()

    def _reload_brain(self) -> None:
        """Called after the user saves new brain settings - swap to a
        fresh brain instance and update the sign-in line so the user
        sees confirmation."""
        wlog = __import__("logging").getLogger("nuts.brain")
        self._brain = pick_brain(self._cfg.worker_url, self._cfg.token)
        brain_name = type(self._brain).__name__
        wlog.info("brain reloaded -> %s", brain_name)
        if brain_name == "DemoBrain":
            self._panel.set_signin("DEMO mode — open Settings to add an API key")
        elif brain_name == "AnthropicBrain":
            self._panel.set_signin("Claude vision (Anthropic) — live")
        elif brain_name == "OpenAICompatibleBrain":
            from nuts_windows import config as _cfg
            s = _cfg.load_brain_settings()
            self._panel.set_signin(f"{s.provider.capitalize()} — live")
        elif brain_name == "WorkerBrain":
            self._panel.set_signin("Worker proxy — live")

    def _on_color_chosen(self, hex_color: str) -> None:
        """User picked a swatch in the HoverBar - apply + persist."""
        try:
            config.save_arrow_color(hex_color)
        except Exception:
            # Keyring can fail under locked-down corp profiles; the live
            # in-memory state still updates, just won't survive restart.
            pass
        self._spring_arrow.set_color(hex_color)

    def _sync_panel_state(self) -> None:
        """Push the latest config + status into the floating panel."""
        if self._cfg.signed_in:
            self._panel.set_signin("Signed in")
        else:
            self._panel.set_signin("Not signed in - visit akhrots.com/app")

    # ----- push-to-talk turn ----------------------------------------------

    def _hotkey_press(self) -> None:
        """Pynput callback (RUNS ON LISTENER THREAD - cannot touch Qt
        directly). We just emit a signal; Qt routes it across the thread
        boundary via QueuedConnection automatically because emitter
        and receiver live in different threads."""
        _hk_log = __import__("logging").getLogger("nuts.hotkey")
        _hk_log.info("hotkey PRESS detected (pynput thread) - emit signal")
        self.sig_hotkey_press.emit()

    def _hotkey_release(self) -> None:
        _hk_log = __import__("logging").getLogger("nuts.hotkey")
        _hk_log.info("hotkey RELEASE detected (pynput thread) - emit signal")
        self.sig_hotkey_release.emit()

    def _begin_turn(self) -> None:
        """Hotkey pressed: grab the screen NOW (before any UI shifts) and
        start recording. We capture the screenshot up front so the user
        gets the state they were looking at when they decided to talk."""
        _t_log = __import__("logging").getLogger("nuts.turn")
        _t_log.info("BEGIN turn (signed_in=%s)", self._cfg.signed_in)
        # Voice works in two modes:
        #   * with auth + a real Worker -> WorkerBrain (LLM + vision)
        #   * without auth, DemoBrain -> offline rule responses
        # We deliberately removed the early "not signed_in -> return"
        # gate from v0.6 so testing voice without a live worker doesn't
        # silently noop. DemoBrain doesn't need a token.
        self._speaker.cancel()
        try:
            self._last_screenshot = capture_all()
            _t_log.info("screenshot ok, %d bytes", len(self._last_screenshot.jpeg_bytes))
        except Exception as e:
            _t_log.exception("screenshot FAILED: %s", e)
            self._last_screenshot = None
        try:
            self._recorder.start()
            _t_log.info("recorder started")
        except Exception as e:
            _t_log.exception("recorder.start FAILED (no microphone?): %s", e)
        # Three visual signals for "recording": the ring glued to the cursor
        # (immediate, hard to miss), the mic badge floating top-center of
        # the screen (explicit label), and the persistent badge by the clock
        # turning green (peripheral, always-visible state indicator).
        # The spring arrow ALSO swaps to its green / listening palette so
        # the always-on follower visually confirms the hotkey.
        self._cursor_indicator.start()
        self._mic_badge.start()
        self._persistent_badge.set_state(STATE_LISTENING)
        self._spring_arrow.set_state(ARROW_LISTENING)
        self._panel.set_status("Listening")

    def _end_turn(self) -> None:
        """Hotkey released: stop the mic, send to worker, stream response."""
        _t_log = __import__("logging").getLogger("nuts.turn")
        _t_log.info("END turn")
        # The cursor ring + mic badge go away the moment the user releases
        # the hotkey. The persistent badge transitions to "busy" while we
        # wait on the model. The spring arrow returns to its idle palette
        # (it stays on screen the whole time - the headline UX).
        self._cursor_indicator.stop()
        self._mic_badge.stop()
        self._persistent_badge.set_state(STATE_BUSY)
        self._spring_arrow.set_state(ARROW_IDLE)
        rec = self._recorder.stop()
        if rec is None:
            _t_log.info("recorder.stop() returned None")
        else:
            _t_log.info("recorder.stop() returned %d bytes (dur=%.2fs)",
                        len(rec.wav_bytes), rec.duration_s)
        if rec is None or self._last_screenshot is None:
            _t_log.warning("no recording or screenshot - bailing")
            self._panel.set_status("Idle")
            self._persistent_badge.set_state(STATE_IDLE)
            return
        snap = self._last_screenshot
        self._last_screenshot = None
        self._panel.set_status("Responding")
        self._panel.set_response("")

        # Recreate the client if EITHER the URL or the token has changed
        # since last turn. Previously we only checked the URL, which meant
        # a token rotation (e.g. user signed out + back in) kept the stale
        # bearer until the next URL change. Fixed in the v0.1 bug review.
        if (
            self._client is None
            or self._client.base_url != self._cfg.worker_url.rstrip("/")
            or self._client.bearer != self._cfg.token
        ):
            if self._client is not None:
                # Don't leak the old connection pool.
                self._async.submit(self._client.aclose())
            self._client = WorkerClient(self._cfg.worker_url, self._cfg.token)

        client = self._client
        speaker = self._speaker

        memory = self._memory
        panel = self._panel

        _t_log = __import__("logging").getLogger("nuts.turn")

        async def go() -> None:
            # 1. Transcribe locally (Whisper). Falls back to None if the
            #    dep isn't installed - we then ship raw WAV to the worker
            #    for cloud STT.
            _t_log.info("transcribe START (wav=%d bytes)", len(rec.wav_bytes))
            transcript = await asyncio.to_thread(transcribe, rec.wav_bytes)
            _t_log.info("transcribe DONE: %r", transcript)
            # 2. Voice-command shortcuts. Intercept "remember this …" and
            #    "what do you remember about …" without going to the model.
            if transcript:
                panel.set_response("> " + transcript)
                if memory_mod.is_remember(transcript):
                    payload = memory_mod.extract_remember_payload(transcript) or transcript
                    memory.append(payload, source="voice")
                    line = f"Remembered: {payload}"
                    panel.set_response(line)
                    speaker.speak(line + ".")
                    speaker.flush()
                    panel.set_status("Idle")
                    self._persistent_badge.set_state(STATE_IDLE)
                    return
                if memory_mod.is_recall(transcript):
                    q = memory_mod.extract_recall_query(transcript)
                    hits = memory.search(q) if q else memory.recent(3)
                    if hits:
                        line = "You told me: " + "; ".join(h.summary for h in hits) + "."
                    else:
                        line = f"I don't have anything about {q or 'that'} yet."
                    panel.set_response(line)
                    speaker.speak(line)
                    speaker.flush()
                    panel.set_status("Idle")
                    self._persistent_badge.set_state(STATE_IDLE)
                    return

            # 3. Hand the transcript + screenshot to whichever Brain is
            #    selected. DemoBrain answers offline using rule patterns
            #    (good enough to verify the whole loop without an API
            #    key); WorkerBrain proxies to a real Cloudflare Worker
            #    when one is configured. See brain.py pick_brain().
            ctx = BrainContext(
                transcript=transcript or "",
                screenshot_jpeg=snap.jpeg_bytes,
                monitors=snap.monitors,
            )
            _t_log.info("brain.stream START (%s)", type(self._brain).__name__)
            try:
                chunk_count = 0
                async for chunk in self._brain.stream(ctx):
                    chunk_count += 1
                    _t_log.info("brain chunk %d: %r", chunk_count, chunk[:120])
                    self._handle_chunk(chunk, snap.monitors)
                _t_log.info("brain.stream END (%d chunks)", chunk_count)
            except Exception as e:
                # The brain failed before producing any text. Without
                # this branch the pipeline just goes silent ('idle') and
                # the user has no idea what went wrong. Show the error
                # in the panel + speak a short notice so they know.
                _t_log.exception("brain.stream FAILED: %s", e)
                msg = str(e)
                # Try to extract the most useful sub-string. Provider
                # error messages tend to be the most informative bit.
                short = msg[:200]
                panel.set_response(f"Error: {short}")
                # Spoken notice - keep it short, the full text is on screen.
                if "403" in msg or "Forbidden" in msg:
                    spoken = "Provider refused the key. Check billing or model access."
                elif "401" in msg or "Unauthorized" in msg:
                    spoken = "Provider says the API key is invalid."
                elif "429" in msg or "rate" in msg.lower():
                    spoken = "Rate limited by provider."
                elif "timeout" in msg.lower() or "timed out" in msg.lower():
                    spoken = "Provider timed out."
                else:
                    spoken = "Brain request failed. See the panel for details."
                speaker.speak(spoken)
                speaker.flush()
            finally:
                # Flush any text the speaker is still holding (a trailing
                # fragment without a sentence-ending punctuation). Without
                # this the final clause was silently dropped.
                speaker.flush()
                # Stream finished - reset the status pill in the panel.
                panel.set_status("Idle")
                self._persistent_badge.set_state(STATE_IDLE)

        self._async.submit(go())

    def _handle_chunk(self, chunk: str, monitors: list[dict]) -> None:
        """Parse [POINT:...] tags out of the chunk; speak the rest."""
        for pt in cursor_mod.find_points(chunk):
            abs_pos = cursor_mod.move_cursor(pt, monitors)
            if abs_pos is not None:
                # Hand off to the spring arrow. It picks up the new target,
                # the physics smoothly redirects from "trailing the cursor"
                # to "flying to (abs_x, abs_y)", and the model's label
                # ("right here!") floats next to the head while held.
                self._spring_arrow.fly_to(
                    abs_pos[0], abs_pos[1],
                    label=pt.label or "",
                )
        speakable = cursor_mod.strip_points(chunk)
        if speakable:
            # Also surface the streaming text in the floating panel so
            # there's a visual transcript next to the audio.
            self._panel.append_response(speakable)
            self._speaker.speak(speakable)


def _log_dir() -> Path:
    """Per-user log directory: %LOCALAPPDATA%\\Akhort\\."""
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    p = base / "Akhort"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _install_logging() -> Path:
    """Set up file logging so silent crashes leave a trace.

    PyInstaller's --windowed mode hides the console, so an unhandled
    exception during startup vanishes from view. With this, the same
    traceback is appended to %LOCALAPPDATA%\\Akhort\\Nuts.log instead.
    Also installs a global ``sys.excepthook`` so non-Qt-loop exceptions
    (e.g. inside startup) are captured.
    """
    log_path = _log_dir() / "Nuts.log"
    logging.basicConfig(
        filename=str(log_path),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        encoding="utf-8",
    )

    def _hook(exc_type, exc, tb):
        logging.exception("unhandled", exc_info=(exc_type, exc, tb))
        # Still call the default hook so console mode still prints.
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _hook
    return log_path


def run() -> int:
    log_path = _install_logging()
    log = logging.getLogger("nuts.run")
    log.info("nuts starting; pid=%d frozen=%s", os.getpid(), getattr(sys, "frozen", False))

    try:
        qt = QApplication([])
    except Exception:
        log.exception("QApplication() failed")
        return 1
    qt.setQuitOnLastWindowClosed(False)   # tray-only app

    # No tray? No app. Surface this loudly instead of running headlessly.
    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.error("system tray is not available on this OS")
        QMessageBox.critical(
            None,
            "Akhort - tray unavailable",
            "Your system doesn't expose a tray. Nuts can't run without it.",
        )
        return 2

    try:
        app = Application(qt)
    except Exception as e:
        log.exception("Application init failed")
        QMessageBox.critical(
            None,
            "Akhort - startup error",
            f"Nuts couldn't start:\n\n{e}\n\nFull traceback: {log_path}",
        )
        return 1

    log.info("nuts ready; entering event loop")
    try:
        return qt.exec()
    except Exception:
        log.exception("event loop crashed")
        return 1
    finally:
        try:
            app.shutdown()
        except Exception:
            log.exception("shutdown failed")

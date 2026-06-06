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

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from nuts_windows import bootstrap, config
from nuts_windows.capture.audio import Recorder, transcribe
from nuts_windows.capture.screen import capture_all
from nuts_windows import memory as memory_mod
from nuts_windows.hotkey import PushToTalk
from nuts_windows.overlay import cursor as cursor_mod
from nuts_windows.overlay.indicator import CursorIndicator, MicBadge, PointOverlay
from nuts_windows.overlay.panel import ControlPanel
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


class Application:
    def __init__(self, qt_app: QApplication) -> None:
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
        self._panel = ControlPanel(
            on_open_dashboard=self._open_dashboard,
            on_signout=self._handle_signout,
            on_quit=self._quit,
        )
        self._cursor_indicator = CursorIndicator()
        self._mic_badge = MicBadge()
        self._point_overlay = PointOverlay()
        # Memory: local on-disk JSONL store for "remember this" voice
        # commands. Loaded once, written incrementally. Lives in the
        # same %LOCALAPPDATA%\Akhort directory as the log file.
        self._memory = Memory()

        self._tray = Tray(
            qt_app,
            on_reload=self._reload,
            on_quit=self._quit,
            on_left_click=self._panel.toggle_near_tray,
        )
        self._hotkey = PushToTalk(
            self._cfg.hotkey,
            on_start=lambda: QTimer.singleShot(0, self._begin_turn),
            on_stop=lambda: QTimer.singleShot(0, self._end_turn),
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

    def _sync_panel_state(self) -> None:
        """Push the latest config + status into the floating panel."""
        if self._cfg.signed_in:
            self._panel.set_signin("Signed in")
        else:
            self._panel.set_signin("Not signed in - visit akhrots.com/app")

    # ----- push-to-talk turn ----------------------------------------------

    def _begin_turn(self) -> None:
        """Hotkey pressed: grab the screen NOW (before any UI shifts) and
        start recording. We capture the screenshot up front so the user
        gets the state they were looking at when they decided to talk."""
        if not self._cfg.signed_in:
            # No bearer - silently noop. Tray tooltip already nudges them.
            return
        self._speaker.cancel()
        try:
            self._last_screenshot = capture_all()
        except Exception:
            self._last_screenshot = None
        self._recorder.start()
        # Two visual signals for "recording": the ring glued to the cursor
        # (immediate, hard to miss) and the mic badge floating top-center
        # of the screen (explicit, label tells you exactly what's happening).
        self._cursor_indicator.start()
        self._mic_badge.start()
        self._panel.set_status("Listening")

    def _end_turn(self) -> None:
        """Hotkey released: stop the mic, send to worker, stream response."""
        # The cursor ring + mic badge go away the moment the user releases
        # the hotkey, even if the recording was empty or the screenshot
        # failed - the visual contract is "rings/badge = listening".
        self._cursor_indicator.stop()
        self._mic_badge.stop()
        rec = self._recorder.stop()
        if rec is None or self._last_screenshot is None:
            self._panel.set_status("Idle")
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

        async def go() -> None:
            # 1. Transcribe locally (Whisper). Falls back to None if the
            #    dep isn't installed - we then ship raw WAV to the worker
            #    for cloud STT.
            transcript = await asyncio.to_thread(transcribe, rec.wav_bytes)
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
                    return

            req = WorkerRequest(
                screenshot_jpeg=snap.jpeg_bytes,
                audio_wav=rec.wav_bytes if not transcript else None,
                transcript=transcript or None,
            )
            try:
                async for chunk in client.stream_response(req):
                    self._handle_chunk(chunk, snap.monitors)
            except Exception:
                # The model dropped the connection or auth failed - swallow
                # for the scaffold; later wire to a tray notification.
                pass
            finally:
                # Flush any text the speaker is still holding (a trailing
                # fragment without a sentence-ending punctuation). Without
                # this the final clause was silently dropped.
                speaker.flush()
                # Stream finished - reset the status pill in the panel.
                panel.set_status("Idle")

        self._async.submit(go())

    def _handle_chunk(self, chunk: str, monitors: list[dict]) -> None:
        """Parse [POINT:...] tags out of the chunk; speak the rest."""
        for pt in cursor_mod.find_points(chunk):
            abs_pos = cursor_mod.move_cursor(pt, monitors)
            # Flash the on-screen arrow so the user SEES where the model
            # is pointing - the OS cursor jumps without visual context,
            # the overlay arrow animates the destination so it's obvious.
            if abs_pos is not None:
                self._point_overlay.flash_at(abs_pos[0], abs_pos[1])
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

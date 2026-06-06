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
import threading
from typing import Optional

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from nuts_windows import bootstrap, config
from nuts_windows.capture.audio import Recorder
from nuts_windows.capture.screen import capture_all
from nuts_windows.hotkey import PushToTalk
from nuts_windows.overlay import cursor as cursor_mod
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
        self._tray = Tray(qt_app, on_reload=self._reload, on_quit=self._quit)
        self._hotkey = PushToTalk(
            self._cfg.hotkey,
            on_start=lambda: QTimer.singleShot(0, self._begin_turn),
            on_stop=lambda: QTimer.singleShot(0, self._end_turn),
        )
        self._hotkey.start()
        self._last_screenshot = None

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

    def _quit(self) -> None:
        self.shutdown()
        self._qt.quit()

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

    def _end_turn(self) -> None:
        """Hotkey released: stop the mic, send to worker, stream response."""
        rec = self._recorder.stop()
        if rec is None or self._last_screenshot is None:
            return
        snap = self._last_screenshot
        self._last_screenshot = None

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

        async def go() -> None:
            req = WorkerRequest(
                screenshot_jpeg=snap.jpeg_bytes,
                audio_wav=rec.wav_bytes,
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

        self._async.submit(go())

    def _handle_chunk(self, chunk: str, monitors: list[dict]) -> None:
        """Parse [POINT:...] tags out of the chunk; speak the rest."""
        for pt in cursor_mod.find_points(chunk):
            cursor_mod.move_cursor(pt, monitors)
        speakable = cursor_mod.strip_points(chunk)
        if speakable:
            self._speaker.speak(speakable)


def run() -> int:
    qt = QApplication([])
    qt.setQuitOnLastWindowClosed(False)   # tray-only app
    app = Application(qt)
    try:
        return qt.exec()
    finally:
        app.shutdown()

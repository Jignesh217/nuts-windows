"""System tray icon + menu.

The tray icon is the user's persistent surface area:

  Akhort  (header, disabled)
  ----
  Status: <Signed in as foo@bar / Not signed in>
  ----
  Open dashboard           -> opens https://akhrots.com/app
  Reload config            -> re-runs first-launch lookup
  Sign out                 -> clears Credential Manager
  ----
  Quit

When the user clicks the icon (not the menu), we fire a callback the app
can hook to toggle a settings window or whatever - kept abstract here so
this module stays UI-only.
"""
from __future__ import annotations

import logging
import webbrowser
from typing import Callable, Optional

from PyQt6.QtCore import QSize
from PyQt6.QtGui import QAction, QCursor, QIcon, QPixmap, QPainter, QColor, QFont
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from nuts_windows import config

_log = logging.getLogger("nuts.tray")


DASHBOARD_URL = "https://akhrots.com/app"


class Tray:
    def __init__(
        self,
        app: QApplication,
        on_reload: Callable[[], None],
        on_quit: Callable[[], None],
        on_left_click: Optional[Callable[[], None]] = None,
        on_test_arrow: Optional[Callable[[], None]] = None,
    ) -> None:
        self._app = app
        self._on_reload = on_reload
        self._on_quit = on_quit
        # Optional handler for left-click. When provided, left-click toggles
        # the floating control panel (the clicky-style UI). The right-click
        # context menu is always available regardless.
        self._on_left_click = on_left_click
        # Test arrow handler - fires a synthetic [POINT:x,y] target at a
        # random screen location so we can verify the spring physics without
        # waiting on a real model response.
        self._on_test_arrow = on_test_arrow
        self._icon = QSystemTrayIcon(_make_icon(), parent=app)
        self._icon.setToolTip("Akhort - hold Ctrl+Alt to talk")
        self._menu = QMenu()
        self._status_action: Optional[QAction] = None
        self._build_menu()
        self._icon.setContextMenu(self._menu)
        self._icon.activated.connect(self._on_activated)
        self._icon.show()
        if not self._icon.isVisible():
            # Some setups (rare, but happens with shell extensions or
            # locked-down corporate Windows) silently refuse to register
            # tray icons. Log loudly so we can find out via Nuts.log.
            _log.error("tray icon failed to become visible after show()")
        self.refresh()
        # Visible confirmation that the app started. This is the surface
        # area for "it ran but I can't see it" - the user can't miss a
        # toast notification near the clock. Auto-dismisses after ~4s.
        self._icon.showMessage(
            "Akhort is running",
            "Hold Ctrl+Alt to talk. Right-click the tray icon for options.",
            QSystemTrayIcon.MessageIcon.Information,
            4000,
        )
        _log.info("tray icon shown")

    def refresh(self) -> None:
        """Re-read config and update the Status row + tooltip."""
        cfg = config.load()
        if self._status_action is not None:
            self._status_action.setText(
                "Status: Signed in" if cfg.signed_in else "Status: Not signed in"
            )
        if cfg.signed_in:
            self._icon.setToolTip("Akhort - hold Ctrl+Alt to talk")
        else:
            self._icon.setToolTip(
                "Akhort - sign in at akhrots.com/app and drop the install zip in Downloads"
            )

    # ----- internal --------------------------------------------------------

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Route tray-icon clicks.

        Logging the raw reason value is intentional: Qt/Windows sometimes
        reports clicks under non-obvious reasons (e.g. Unknown on shells
        with custom tray hosts), and we want Nuts.log to tell us why a
        click went nowhere. Any non-context reason now triggers the
        panel, not just Trigger/DoubleClick - matches clicky's "any
        click that isn't a right-click opens the panel" UX.
        """
        _log.info("tray activated reason=%s", reason)
        if reason == QSystemTrayIcon.ActivationReason.Context:
            # Right-click. Qt has already popped the context menu via
            # setContextMenu(); we don't need to do anything.
            return
        # Everything else (Trigger / DoubleClick / MiddleClick / Unknown)
        # we treat as "the user wants the panel".
        if self._on_left_click is not None:
            try:
                self._on_left_click()
            except Exception:
                _log.exception("on_left_click handler raised")
        else:
            _log.warning("no on_left_click handler set; falling back to menu")
            self._menu.popup(QCursor.pos())

    def _build_menu(self) -> None:
        header = QAction("Akhort", self._menu)
        header.setEnabled(False)
        self._menu.addAction(header)

        self._menu.addSeparator()

        self._status_action = QAction("Status: ...", self._menu)
        self._status_action.setEnabled(False)
        self._menu.addAction(self._status_action)

        self._menu.addSeparator()

        # Explicit "Show Panel" entry - guarantees the user has a way to
        # open the floating panel even if their Windows configuration
        # doesn't fire QSystemTrayIcon.Trigger on left-click (some shells
        # don't). Routes through the same callback as a real left-click.
        show_panel = QAction("Show Panel", self._menu)
        show_panel.triggered.connect(self._handle_show_panel)
        self._menu.addAction(show_panel)

        open_dash = QAction("Open dashboard", self._menu)
        open_dash.triggered.connect(lambda: webbrowser.open(DASHBOARD_URL))
        self._menu.addAction(open_dash)

        # Dev / smoke-test: fly the spring arrow to a random spot so the
        # user can see the physics without waiting for a real model
        # response. Only listed when a handler is supplied.
        if self._on_test_arrow is not None:
            test_arrow = QAction("Test arrow (fly to random spot)", self._menu)
            test_arrow.triggered.connect(self._on_test_arrow)
            self._menu.addAction(test_arrow)

        reload = QAction("Reload config", self._menu)
        reload.triggered.connect(self._handle_reload)
        self._menu.addAction(reload)

        signout = QAction("Sign out", self._menu)
        signout.triggered.connect(self._handle_signout)
        self._menu.addAction(signout)

        self._menu.addSeparator()

        quit_action = QAction("Quit", self._menu)
        quit_action.triggered.connect(self._on_quit)
        self._menu.addAction(quit_action)

    def _handle_reload(self) -> None:
        self._on_reload()
        self.refresh()

    def _handle_signout(self) -> None:
        config.clear_credentials()
        self.refresh()

    def _handle_show_panel(self) -> None:
        """Same callback path as a real left-click; logged so the menu
        item being used (vs. the icon click) is visible in Nuts.log."""
        _log.info("show_panel menu item clicked")
        if self._on_left_click is not None:
            try:
                self._on_left_click()
            except Exception:
                _log.exception("show_panel handler raised")


def _make_icon() -> QIcon:
    """Programmatic monogram icon - placeholder until a real one ships.

    Draws a tan circle with a black "A" centered. Replace with a real
    .ico/.png in assets/ once branding lands; QIcon(path) replaces this.
    """
    pix = QPixmap(QSize(64, 64))
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(245, 215, 145))   # warm tan
    p.setPen(QColor(31, 27, 22))
    p.drawEllipse(2, 2, 60, 60)
    font = QFont("Segoe UI", 32, QFont.Weight.Bold)
    p.setFont(font)
    p.drawText(pix.rect(), 0x0084, "A")  # AlignCenter
    p.end()
    return QIcon(pix)

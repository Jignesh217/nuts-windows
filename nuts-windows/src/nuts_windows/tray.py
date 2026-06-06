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
    ) -> None:
        self._app = app
        self._on_reload = on_reload
        self._on_quit = on_quit
        self._icon = QSystemTrayIcon(_make_icon(), parent=app)
        self._icon.setToolTip("Akhort - hold Ctrl+Alt to talk")
        self._menu = QMenu()
        self._status_action: Optional[QAction] = None
        self._build_menu()
        self._icon.setContextMenu(self._menu)
        # Left-click should also pop the menu (right-click already does via
        # setContextMenu). On Windows the default is right-click only, so
        # users who left-click out of habit get nothing - this fixes that.
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
        """Show the menu on any icon click (left or right).

        Windows treats left-click as ``Trigger`` and right-click as
        ``Context``. setContextMenu wires the right-click for free, but
        users who left-click out of habit got nothing. Pop the same menu
        either way - cheap UX win.
        """
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
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

        open_dash = QAction("Open dashboard", self._menu)
        open_dash.triggered.connect(lambda: webbrowser.open(DASHBOARD_URL))
        self._menu.addAction(open_dash)

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

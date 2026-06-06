"""Always-visible floating badge above the clock.

Sits ~24 px above the right end of the taskbar, always on top, always
visible. Click toggles the full control panel; hover-pulse signals that
it's clickable. Inspired by Clicky's persistent blue dot on macOS - we
keep it stationary (anchored to the taskbar) rather than cursor-trailing
because Windows users expect a status indicator near the clock.

Behaviors:
  * Renders a soft halo + a brand-tan core dot.
  * Hover -> halo intensifies, tooltip "Hold Ctrl+Alt to talk"
  * Click -> emits clicked() (the app wires it to panel.toggle_near_tray)
  * State color tracks recording / busy state, matching the panel pill.

Why a separate widget instead of the QSystemTrayIcon? On Windows 11 the
tray icon is hidden by default behind the chevron - users miss it
entirely. A floating widget bolted to the screen rectangle is
unmissable, and we keep the tray icon ALSO so right-click context menu
still works.
"""
from __future__ import annotations

import logging
import math

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QPointF, QRectF
from PyQt6.QtGui import (
    QColor,
    QGuiApplication,
    QPainter,
    QBrush,
    QPen,
)
from PyQt6.QtWidgets import QWidget


_log = logging.getLogger("nuts.badge")

# Visual states
STATE_IDLE = "idle"
STATE_LISTENING = "listening"
STATE_BUSY = "busy"


class PersistentBadge(QWidget):
    """Tiny always-on-top dot near the taskbar clock."""

    SIZE = 38   # outer halo box; the actual visible dot is smaller
    MARGIN_RIGHT = 8
    MARGIN_BOTTOM = 56   # taskbar height + a touch more so we sit ABOVE it

    clicked = pyqtSignal()

    def __init__(self) -> None:
        super().__init__(None)
        # Frameless top-most, NOT a Tool (Tool windows hide when the user
        # alt-tabs, defeats the "always visible" promise).
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # We DO want mouse events here (unlike the cursor / point
        # overlays which are click-through); the whole point is that the
        # user can click it.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.resize(self.SIZE, self.SIZE)
        self.setToolTip("Akhort — hold Ctrl+Alt to talk")

        self._state = STATE_IDLE
        self._hovering = False
        self._pulse = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        self._reposition()
        # Reposition on screen-geometry changes (monitors swap, DPI changes,
        # taskbar auto-hide toggles). Use a slow polling tick because Qt's
        # screen-changed signals are unreliable on Win11.
        self._anchor_timer = QTimer(self)
        self._anchor_timer.setInterval(1000)
        self._anchor_timer.timeout.connect(self._reposition)
        self._anchor_timer.start()

    # ----- public API -----------------------------------------------------

    def show_(self) -> None:
        # Underscore to avoid clobbering QWidget.show() if some future
        # caller does badge.show; same semantics + log.
        self._reposition()
        self.show()
        self.raise_()
        _log.info("badge shown at %s", self.geometry())

    def set_state(self, state: str) -> None:
        self._state = state
        self.update()

    # ----- internal -------------------------------------------------------

    def _reposition(self) -> None:
        screen = QGuiApplication.primaryScreen()
        # Use the FULL geometry (not availableGeometry) so we land in the
        # space the taskbar occupies; we add a bottom margin to lift the
        # badge above it. availableGeometry would put us above the
        # taskbar but FAR above the clock, which the user doesn't want.
        full = screen.geometry()
        x = full.right() - self.SIZE - self.MARGIN_RIGHT
        y = full.bottom() - self.SIZE - self.MARGIN_BOTTOM
        new = self.pos()
        if new.x() != x or new.y() != y:
            self.move(x, y)

    def _tick(self) -> None:
        # Faster pulse when listening, gentle drift when idle.
        speed = 0.18 if self._state == STATE_LISTENING else 0.05
        self._pulse = (self._pulse + speed) % (2 * math.pi)
        self.update()

    def _color(self) -> QColor:
        if self._state == STATE_LISTENING:
            return QColor(47, 122, 79)         # green
        if self._state == STATE_BUSY:
            return QColor(138, 90, 48)         # warm brown
        return QColor(245, 215, 145)           # tan idle

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = QPointF(self.SIZE / 2, self.SIZE / 2)
        core = self._color()

        # Halo - bigger when hovering or listening, gentle when idle
        breath = 0.5 + 0.5 * math.sin(self._pulse)
        halo_r = 14 + 4 * breath
        if self._hovering or self._state == STATE_LISTENING:
            halo_r += 4
        halo = QColor(core)
        halo.setAlpha(int(40 + 80 * breath))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(halo))
        p.drawEllipse(center, halo_r, halo_r)

        # Inner core dot
        p.setPen(QPen(QColor(31, 27, 22, 200), 1.5))
        c_solid = QColor(core); c_solid.setAlpha(240)
        p.setBrush(QBrush(c_solid))
        p.drawEllipse(center, 7.5, 7.5)
        p.end()

    # ----- mouse ----------------------------------------------------------

    def enterEvent(self, event):
        self._hovering = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovering = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            _log.info("badge clicked")
            self.clicked.emit()
        super().mousePressEvent(event)

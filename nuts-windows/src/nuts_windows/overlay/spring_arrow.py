"""Spring-physics cursor follower / pointer arrow.

This is the headline UX from Clicky's ``OverlayWindow.swift``: a small
dot that ALWAYS lives on top of every monitor, follows the user's
mouse cursor with spring physics (lag + overshoot = it feels alive),
and flies to arbitrary screen coordinates when the model emits a
``[POINT:x, y]`` instruction.

State machine:
  * ``IDLE``     - target = QCursor.pos(); springs after the user
  * ``LISTENING`` - same target but rendered green + bigger halo
  * ``POINTING``  - target = (abs_x, abs_y) for a hold window, then
                    decays back to IDLE. The arrow rotates so the head
                    points along its velocity vector.

The window is:
  * frameless + topmost + tool + click-through
  * sized to the union of every monitor's geometry (one window covers
    the entire virtual desktop, no per-screen instances)
  * 60 Hz update loop via QTimer

Spring math is semi-implicit Euler with mass=1, tuned by ear so the
follow feels "alive but not jittery". Higher STIFFNESS = snappier;
higher DAMPING = less overshoot.
"""
from __future__ import annotations

import math
import time

from PyQt6.QtCore import Qt, QTimer, QPointF, QRectF
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PyQt6.QtWidgets import QWidget


STATE_IDLE = "idle"
STATE_LISTENING = "listening"
STATE_POINTING = "pointing"


class SpringArrow(QWidget):
    """Always-on-screen spring-following arrow."""

    # Tuning constants. Numbers that "felt right" in manual play. Keep
    # together so future tweaks are obvious.
    STIFFNESS = 240.0
    DAMPING = 22.0
    MASS = 1.0
    FRAME_MS = 16          # ~60 fps
    TRAIL_LEN = 18
    POINT_HOLD_MS = 2400   # how long a [POINT:x,y] target stays locked
    # Offset from the cursor for the FOLLOW state, in screen pixels.
    # The arrow trails to the upper-right of the cursor so it never sits
    # directly under what the user is trying to click/read. When the
    # model gives a [POINT:x,y] target, we drop the offset (the arrow
    # should land ON the target, not next to it).
    FOLLOW_OFFSET_X = 36
    FOLLOW_OFFSET_Y = -36

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        # Initial pose - somewhere on screen; first _step() snaps to cursor.
        self._pos = QPointF(0, 0)
        self._vel = QPointF(0, 0)
        self._target = QPointF(0, 0)
        self._point_lock_until = 0.0   # POSIX time when point hold expires
        self._point_label: str = ""    # "right here!"-style overlay label
        self._trail: list[QPointF] = []
        self._state = STATE_IDLE
        self._first_frame = True

        self._timer = QTimer(self)
        self._timer.setInterval(self.FRAME_MS)
        self._timer.timeout.connect(self._step)

    # ----- public API -----------------------------------------------------

    def start(self) -> None:
        """Become visible and start the physics loop."""
        self._size_to_virtual_desktop()
        self.show()
        self.raise_()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self.hide()

    def set_state(self, state: str) -> None:
        """Change the visual state (idle / listening / pointing)."""
        self._state = state

    def fly_to(self, abs_x: int, abs_y: int, *, label: str = "") -> None:
        """Direct the arrow to a desktop coord for POINT_HOLD_MS, then
        decay back to following the cursor. The spring math makes the
        transition smooth automatically - we don't snap, we just nudge
        the target and let the simulator catch up.
        """
        self._target = QPointF(abs_x, abs_y)
        self._point_lock_until = time.time() + self.POINT_HOLD_MS / 1000.0
        self._point_label = label
        self._state = STATE_POINTING

    # ----- internal -------------------------------------------------------

    def _size_to_virtual_desktop(self) -> None:
        rect = QGuiApplication.primaryScreen().virtualGeometry()
        for s in QGuiApplication.screens():
            rect = rect.united(s.geometry())
        self.setGeometry(rect)

    def _step(self) -> None:
        # Re-evaluate target each frame.
        now = time.time()
        if now > self._point_lock_until:
            # Idle / listening: target = cursor PLUS the follow offset so
            # the arrow trails off to the upper-right instead of sitting
            # under whatever the user is reading or trying to click.
            c = QCursor.pos()
            self._target = QPointF(
                c.x() + self.FOLLOW_OFFSET_X,
                c.y() + self.FOLLOW_OFFSET_Y,
            )
            if self._state == STATE_POINTING:
                self._state = STATE_IDLE
                self._point_label = ""

        if self._first_frame:
            # Snap to target on the first frame so we don't see a wild
            # accelerating tail from (0, 0) to the cursor.
            self._pos = QPointF(self._target)
            self._vel = QPointF(0, 0)
            self._first_frame = False

        # Spring math (semi-implicit Euler, dt = FRAME_MS / 1000)
        dt = self.FRAME_MS / 1000.0
        dx = self._pos.x() - self._target.x()
        dy = self._pos.y() - self._target.y()
        fx = -self.STIFFNESS * dx - self.DAMPING * self._vel.x()
        fy = -self.STIFFNESS * dy - self.DAMPING * self._vel.y()
        self._vel = QPointF(
            self._vel.x() + fx / self.MASS * dt,
            self._vel.y() + fy / self.MASS * dt,
        )
        self._pos = QPointF(
            self._pos.x() + self._vel.x() * dt,
            self._pos.y() + self._vel.y() * dt,
        )
        # Trail - fixed length ring; cheap and looks alive.
        self._trail.append(QPointF(self._pos))
        if len(self._trail) > self.TRAIL_LEN:
            self._trail = self._trail[-self.TRAIL_LEN:]

        self.update()

    # ----- paint ----------------------------------------------------------

    def _state_color(self) -> QColor:
        if self._state == STATE_LISTENING:
            return QColor(47, 122, 79)        # green
        if self._state == STATE_POINTING:
            return QColor(245, 215, 145)      # bright tan
        return QColor(245, 215, 145)          # tan idle (same hue, less saturation handled by alpha)

    def paintEvent(self, _event) -> None:
        # Translate desktop coords into widget-local. The overlay's
        # top-left is the virtual-desktop top-left.
        g = self.geometry()
        ox, oy = -g.left(), -g.top()

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = self._state_color()

        # 1) Trail - older points more transparent + slightly smaller
        if len(self._trail) > 1:
            for i, pt in enumerate(self._trail):
                t = i / len(self._trail)
                alpha = int(140 * t * t)
                r = 4.0 + 2.0 * t
                tc = QColor(color); tc.setAlpha(alpha)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(tc))
                p.drawEllipse(QPointF(pt.x() + ox, pt.y() + oy), r, r)

        # 2) Halo at the head - bigger / brighter when listening or pointing
        head = QPointF(self._pos.x() + ox, self._pos.y() + oy)
        halo_r = 14
        halo_a = 60
        if self._state == STATE_LISTENING:
            halo_r = 22; halo_a = 110
        elif self._state == STATE_POINTING:
            halo_r = 28; halo_a = 140
        halo = QColor(color); halo.setAlpha(halo_a)
        p.setBrush(QBrush(halo))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(head, halo_r, halo_r)

        # 3) Arrowhead. Rotated to align with velocity (with a minimum
        # speed threshold so it doesn't spin wildly when idle).
        speed = math.hypot(self._vel.x(), self._vel.y())
        if speed > 60:
            angle = math.atan2(self._vel.y(), self._vel.x())
        else:
            angle = -math.pi / 4   # idle default: pointer-style up-left
        self._draw_arrow(p, head, angle, core=color)

        # 4) Optional utterance label near the target
        if self._point_label and self._state == STATE_POINTING:
            label_pos = QPointF(head.x() + 24, head.y() - 22)
            self._draw_label(p, label_pos, self._point_label)

        p.end()

    def _draw_arrow(self, p: QPainter, at: QPointF, angle: float, core: QColor) -> None:
        p.save()
        p.translate(at)
        p.rotate(math.degrees(angle) + 90)
        tri = QPolygonF([
            QPointF(0, -12),
            QPointF(9, 9),
            QPointF(0, 4),
            QPointF(-9, 9),
        ])
        outline = QPen(QColor(31, 27, 22, 220)); outline.setWidth(2)
        p.setPen(outline)
        c = QColor(core); c.setAlpha(245)
        p.setBrush(QBrush(c))
        p.drawPolygon(tri)
        p.restore()

    def _draw_label(self, p: QPainter, at: QPointF, text: str) -> None:
        p.save()
        font = QFont("Segoe UI", 10, QFont.Weight.DemiBold)
        p.setFont(font)
        fm = p.fontMetrics()
        text_w = fm.horizontalAdvance(text)
        pad_x, pad_y = 10, 6
        rect = QRectF(at.x(), at.y(), text_w + pad_x * 2, fm.height() + pad_y * 2)
        path = QPainterPath()
        path.addRoundedRect(rect, 10, 10)
        p.fillPath(path, QBrush(QColor(31, 27, 22, 235)))
        p.setPen(QColor(245, 215, 145))
        p.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), text)
        p.restore()

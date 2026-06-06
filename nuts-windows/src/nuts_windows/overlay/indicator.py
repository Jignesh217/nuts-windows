"""Cursor indicator + animated pointing overlay.

Two screen overlays:

  1. ``CursorIndicator`` - a small ring that follows the user's cursor
     while push-to-talk is active. Visible confirmation that we heard
     the hotkey and are recording. Disappears the moment they release.

  2. ``PointOverlay`` - a full-screen, click-through, transparent window
     covering every monitor. When the model emits a [POINT:x,y] tag we
     animate a tan arrow along a bezier curve from its current position
     to the new target, mirroring Clicky's ``OverlayWindow.swift``. Auto
     hides if no new point arrives for a few seconds.

Both overlays are click-through (Qt.WA_TransparentForMouseEvents) so the
user's actual clicks reach the apps underneath.
"""
from __future__ import annotations

import math

from PyQt6.QtCore import (
    Qt,
    QTimer,
    QPoint,
    QPointF,
    QRectF,
    QPropertyAnimation,
    QEasingCurve,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
    QBrush,
    QPolygonF,
)
from PyQt6.QtWidgets import QWidget


# Brand palette
_TAN = QColor(245, 215, 145, 235)
_TAN_HALO = QColor(245, 215, 145, 90)
_INK = QColor(31, 27, 22, 220)
_TRAIL = QColor(245, 215, 145, 60)


class _OverlayBase(QWidget):
    """Frameless, top-most, transparent, click-through. Shared setup."""

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


class CursorIndicator(_OverlayBase):
    """Ring that tracks the cursor while listening."""

    SIZE = 56

    def __init__(self) -> None:
        super().__init__()
        self.resize(self.SIZE, self.SIZE)
        self._timer = QTimer(self)
        self._timer.setInterval(16)        # ~60 fps
        self._timer.timeout.connect(self._follow)
        self._pulse = 0.0
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(33)
        self._pulse_timer.timeout.connect(self._tick_pulse)

    def start(self) -> None:
        QTimer.singleShot(0, self._start_impl)

    def stop(self) -> None:
        QTimer.singleShot(0, self._stop_impl)

    def _start_impl(self) -> None:
        self._follow()
        self.show()
        self.raise_()
        self._timer.start()
        self._pulse_timer.start()

    def _stop_impl(self) -> None:
        self._timer.stop()
        self._pulse_timer.stop()
        self.hide()

    def _follow(self) -> None:
        pos = QCursor.pos()
        self.move(pos.x() - self.SIZE // 2, pos.y() - self.SIZE // 2)

    def _tick_pulse(self) -> None:
        # Soft 0..1..0 sine wave at ~0.6 Hz - the ring "breathes".
        self._pulse = (self._pulse + 0.06) % (2 * math.pi)
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = self.SIZE / 2
        # Pulsing halo - subtle, doesn't obscure.
        breath = 0.5 + 0.5 * math.sin(self._pulse)
        halo_r = 22 + 6 * breath
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(245, 215, 145, int(70 + 60 * breath))))
        p.drawEllipse(QPointF(center, center), halo_r, halo_r)
        # Crisp outer ring
        pen = QPen(_TAN); pen.setWidth(3)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(center, center), 18, 18)
        # Inner dot
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QBrush(_INK))
        p.drawEllipse(QPointF(center, center), 3, 3)
        p.end()


class PointOverlay(_OverlayBase):
    """Full-screen click-through canvas that draws an animated arrow.

    Single instance for all monitors. We size it to the virtual desktop
    bounding box so a target on any screen lands inside. The arrow
    animates from its current position to ``flash_at(x, y)`` over
    ``ANIM_MS`` ms with a bezier-eased curve. After ``HOLD_MS`` of no
    new targets, the whole overlay hides.
    """

    ANIM_MS = 420
    HOLD_MS = 2500

    # Qt property - what QPropertyAnimation drives. Note pyqtProperty
    # registered against this class via the metaclass dance below.

    def __init__(self) -> None:
        super().__init__()
        self._pos = QPointF(-1000, -1000)   # off-screen initially
        self._from = QPointF(-1000, -1000)
        self._trail: list[QPointF] = []
        self._size_to_virtual_desktop()
        self._anim = QPropertyAnimation(self, b"posF")
        self._anim.setDuration(self.ANIM_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)

    # ----- public API ----------------------------------------------------

    def flash_at(self, abs_x: int, abs_y: int) -> None:
        """Animate the arrow to (abs_x, abs_y) in DESKTOP coords."""
        QTimer.singleShot(0, lambda: self._flash_impl(abs_x, abs_y))

    def hide_now(self) -> None:
        QTimer.singleShot(0, self.hide)

    # ----- QPropertyAnimation target -------------------------------------

    def _get_posF(self) -> QPointF:
        return self._pos

    def _set_posF(self, p: QPointF) -> None:
        self._pos = p
        # Keep a short trail of recent positions so the arrow leaves a
        # fading vapor behind it. Cap length so memory stays bounded.
        self._trail.append(QPointF(p))
        if len(self._trail) > 24:
            self._trail = self._trail[-24:]
        self.update()

    posF = pyqtProperty(QPointF, fget=_get_posF, fset=_set_posF)

    # ----- internal -------------------------------------------------------

    def _size_to_virtual_desktop(self) -> None:
        # Union of every screen's geometry = the desktop bounding rect.
        # With this the overlay covers all monitors at once.
        rect = QGuiApplication.primaryScreen().virtualGeometry()
        # On multi-screen setups primaryScreen() doesn't always know;
        # iterate to be safe.
        for s in QGuiApplication.screens():
            rect = rect.united(s.geometry())
        self.setGeometry(rect)

    def _flash_impl(self, abs_x: int, abs_y: int) -> None:
        self._size_to_virtual_desktop()  # re-check (monitors may have changed)
        target_widget = self._desktop_to_widget(QPointF(abs_x, abs_y))
        if not self.isVisible():
            # First showing: snap to the target instead of animating from
            # off-screen, looks cleaner.
            self._pos = target_widget
            self._from = target_widget
            self._trail.clear()
            self.show()
            self.raise_()
        self._from = QPointF(self._pos)
        self._anim.stop()
        self._anim.setStartValue(self._from)
        self._anim.setEndValue(target_widget)
        self._anim.start()
        self._hide_timer.start(self.HOLD_MS)

    def _fade_out(self) -> None:
        # Simple: just hide. We can add an opacity animation later if
        # the snap-off feels too abrupt.
        self.hide()
        self._trail.clear()

    def _desktop_to_widget(self, abs_pos: QPointF) -> QPointF:
        # The overlay's geometry top-left is the virtual desktop's
        # top-left. Translate desktop coords into widget-local coords.
        g = self.geometry()
        return QPointF(abs_pos.x() - g.left(), abs_pos.y() - g.top())

    def paintEvent(self, _event) -> None:
        if self._pos.x() < 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 1) Trail - fade older points more transparent
        if len(self._trail) > 1:
            for i, pt in enumerate(self._trail):
                alpha = int(110 * (i / len(self._trail)))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(QColor(245, 215, 145, alpha)))
                p.drawEllipse(pt, 6 + i * 0.2, 6 + i * 0.2)
        # 2) Halo at the head
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(_TAN_HALO))
        p.drawEllipse(self._pos, 26, 26)
        # 3) Arrow shape pointing at the target. We point "up-left" by
        # default (cursor-arrow style) but rotate to face the direction
        # of travel.
        angle = self._travel_angle()
        self._draw_arrow(p, self._pos, angle)
        p.end()

    def _travel_angle(self) -> float:
        # Direction from oldest trail point to current head.
        if len(self._trail) < 2:
            return -math.pi / 4   # default: pointing up-left
        a = self._trail[max(0, len(self._trail) - 6)]
        b = self._pos
        dx, dy = b.x() - a.x(), b.y() - a.y()
        if dx == 0 and dy == 0:
            return -math.pi / 4
        return math.atan2(dy, dx)

    def _draw_arrow(self, p: QPainter, at: QPointF, angle: float) -> None:
        # Triangular arrowhead. We draw centered at origin then translate.
        p.save()
        p.translate(at)
        p.rotate(math.degrees(angle) + 90)   # tip points "up" in local space
        tri = QPolygonF([
            QPointF(0, -14),
            QPointF(10, 10),
            QPointF(0, 5),
            QPointF(-10, 10),
        ])
        # Outline first (slightly larger, dark) for contrast on any bg.
        outline = QPen(_INK); outline.setWidth(3)
        p.setPen(outline)
        p.setBrush(QBrush(_TAN))
        p.drawPolygon(tri)
        p.restore()

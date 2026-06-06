"""Hover bar - always-visible horizontal pill that expands into the
full control panel when the cursor enters it.

Lives above the taskbar clock / battery, always on top, never in the
alt-tab list. Two states:

  * COLLAPSED (default) - 180x32 px pill. Shows brand mark + status
    word + a tiny chevron. Mouse-tracking is enabled so we can detect
    hover-enter / hover-leave.

  * EXPANDED - 360x300 px (or whatever the inner panel needs). Same
    cream brand surface as the existing dashboard dialog. Shows the
    full panel UI: status, hotkey hint, sign-in line, last response,
    color picker swatches, and the action buttons.

The user wanted the hover to "feel like the panel" rather than spawn
a separate window, so we use a single QWidget that re-lays-out its
contents when the state changes. We DO NOT shrink the geometry on
collapse - it would clip the panel mid-fade - we just hide the
expanded children and reset the fixed size.

State color is set via set_state(STATE_IDLE / STATE_LISTENING /
STATE_BUSY), matching the spring arrow's state palette so the user
sees the same color story everywhere.
"""
from __future__ import annotations

import logging
import math
import webbrowser

from PyQt6.QtCore import (
    Qt,
    QTimer,
    QPoint,
    QSize,
    QPropertyAnimation,
    QEasingCurve,
    QRect,
    QEvent,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QGuiApplication,
    QPainter,
    QBrush,
    QPen,
)
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QFrame,
)

from nuts_windows import config


_log = logging.getLogger("nuts.hoverbar")

DASHBOARD_URL = "https://akhrots.com/app"

STATE_IDLE = "idle"
STATE_LISTENING = "listening"
STATE_BUSY = "busy"

# Available arrow colors for the picker - light pastels so the arrow
# doesn't fight whatever's on screen behind it.
ARROW_COLORS = [
    ("Red",    "#ff8c8c"),
    ("Blue",   "#8cb4ff"),
    ("Yellow", "#ffe48c"),
    ("Green",  "#a0e0a0"),
    ("Tan",    "#f5d791"),    # default brand
]


class HoverBar(QWidget):
    """Two-state horizontal bar / expanded panel.

    The widget lives at a fixed screen position (right edge of the
    taskbar). It NEVER moves - only its size and child visibility
    change between collapsed and expanded states.
    """

    COLLAPSED_W = 200
    COLLAPSED_H = 32
    EXPANDED_W = 360
    EXPANDED_H = 320
    MARGIN_RIGHT = 12
    MARGIN_BOTTOM = 54   # above the taskbar clock

    # Hover de-bounce: how long the cursor has to be OUTSIDE before we
    # collapse. Stops a flicker when the cursor leaves momentarily.
    COLLAPSE_DELAY_MS = 220

    # Signals
    quit_requested = pyqtSignal()
    signout_requested = pyqtSignal()
    open_dashboard_requested = pyqtSignal()
    color_chosen = pyqtSignal(str)   # hex string, e.g. "#ff8c8c"
    test_arrow_requested = pyqtSignal()

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        # Translucent so QSS rounded corners actually look round; the
        # alternative is square corners on Windows.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        # Track mouse motion to know when to expand / collapse.
        self.setMouseTracking(True)
        self._expanded = False
        self._state = STATE_IDLE
        self._pulse = 0.0
        self._collapse_timer = QTimer(self)
        self._collapse_timer.setSingleShot(True)
        self._collapse_timer.timeout.connect(self._collapse_now)
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(33)
        self._anim_timer.timeout.connect(self._tick_pulse)
        self._anim_timer.start()

        self._build_ui()
        self.resize(self.COLLAPSED_W, self.COLLAPSED_H)
        self._set_expanded(False)
        self._reposition()
        # Re-anchor periodically - covers monitor swap, DPI change, taskbar
        # auto-hide toggle. Qt's signals for these are unreliable on Win11.
        self._anchor_timer = QTimer(self)
        self._anchor_timer.setInterval(1000)
        self._anchor_timer.timeout.connect(self._reposition)
        self._anchor_timer.start()

    # ----- public API -----------------------------------------------------

    def show_(self) -> None:
        self._reposition()
        self.show()
        self.raise_()
        _log.info("hover bar shown at %s", self.geometry())

    def set_state(self, state: str) -> None:
        self._state = state
        upper = state.upper()
        self._status_label.setText(upper)
        if state == STATE_LISTENING:
            self._status_label.setObjectName("StatusListening")
        elif state == STATE_BUSY:
            self._status_label.setObjectName("StatusBusy")
        else:
            self._status_label.setObjectName("StatusIdle")
        # Re-polish so the QSS background applies.
        self._status_label.style().unpolish(self._status_label)
        self._status_label.style().polish(self._status_label)
        self.update()

    def set_signin(self, text: str) -> None:
        self._signin_label.setText(text)

    def set_response(self, text: str) -> None:
        body = text[-260:] if len(text) > 260 else text
        self._response_label.setText(body)

    def append_response(self, chunk: str) -> None:
        current = self._response_label.text()
        self.set_response((current or "") + chunk)

    def set_active_color(self, hex_color: str) -> None:
        """Highlight which swatch is currently selected."""
        for hex_, swatch in self._color_buttons.items():
            swatch.setProperty("active", hex_.lower() == hex_color.lower())
            swatch.style().unpolish(swatch)
            swatch.style().polish(swatch)

    # ----- internal -------------------------------------------------------

    def _reposition(self) -> None:
        screen = QGuiApplication.primaryScreen().geometry()
        w = self.width()
        # Anchor by the RIGHT edge so expanding to the left looks
        # right-anchored, exactly like a Windows notification flyout.
        x = screen.right() - w - self.MARGIN_RIGHT
        y = screen.bottom() - self.height() - self.MARGIN_BOTTOM
        if self.pos() != QPoint(x, y):
            self.move(x, y)

    def _tick_pulse(self) -> None:
        # Subtle breathing on the status dot so the bar feels "alive"
        # even when idle. Faster while listening.
        speed = 0.16 if self._state == STATE_LISTENING else 0.04
        self._pulse = (self._pulse + speed) % (2 * math.pi)
        self._status_dot.update()

    def _build_ui(self) -> None:
        self.setObjectName("HoverBarRoot")
        self.setStyleSheet(
            "QWidget#HoverBarRoot { background: #faf5e8; border: 1px solid #ebe3cf; border-radius: 14px; }"
            "QLabel { color: #1f1b16; font-family: 'Segoe UI'; }"
            "QLabel#Brand { font-size: 12px; font-weight: 700; letter-spacing: 0.4px; }"
            "QLabel#Title { font-size: 17px; font-weight: 700; letter-spacing: -0.2px; }"
            "QLabel#Subtle { color: #6b6357; font-size: 12px; }"
            "QLabel#SubtleSmall { color: #6b6357; font-size: 11px; font-weight: 500; }"
            "QLabel#StatusIdle { color: #1f1b16; background: #ebe3cf; padding: 3px 10px; "
            "  border-radius: 9px; font-size: 10px; font-weight: 700; letter-spacing: 0.6px; }"
            "QLabel#StatusListening { color: white; background: #2f7a4f; padding: 3px 10px; "
            "  border-radius: 9px; font-size: 10px; font-weight: 700; letter-spacing: 0.6px; }"
            "QLabel#StatusBusy { color: white; background: #8a5a30; padding: 3px 10px; "
            "  border-radius: 9px; font-size: 10px; font-weight: 700; letter-spacing: 0.6px; }"
            "QLabel#Response { color: #1f1b16; font-size: 12px; }"
            "QPushButton { color: #1f1b16; background: #ffffff; border: 1px solid #ebe3cf; "
            "  border-radius: 8px; padding: 6px 14px; font-family: 'Segoe UI'; "
            "  font-size: 12px; font-weight: 600; }"
            "QPushButton:hover { background: #f5efde; border-color: #d9d0b8; }"
            "QPushButton#Quit { color: #8a3030; }"
            "QPushButton#Quit:hover { background: #fae5e5; border-color: #e7c4c4; }"
            "QFrame#Sep { background: #ebe3cf; max-height: 1px; }"
            "QPushButton#Swatch { border-radius: 13px; min-width: 26px; max-width: 26px; "
            "  min-height: 26px; max-height: 26px; border: 2px solid rgba(0,0,0,0.06); }"
            "QPushButton#Swatch[active=\"true\"] { border: 2px solid #1f1b16; }"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 6, 12, 6)
        root.setSpacing(8)

        # Row 1: ALWAYS visible. The "bar" itself.
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self._status_dot = _PulseDot(self)
        bar.addWidget(self._status_dot)
        brand = QLabel("AKHORT")
        brand.setObjectName("Brand")
        bar.addWidget(brand)
        bar.addStretch()
        self._status_label = QLabel("IDLE")
        self._status_label.setObjectName("StatusIdle")
        bar.addWidget(self._status_label)
        root.addLayout(bar)

        # ----- Expanded content - hidden by default ---------------------
        self._expanded_widgets: list[QWidget] = []

        sep = QFrame(); sep.setObjectName("Sep"); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        root.addWidget(sep)
        self._expanded_widgets.append(sep)

        title = QLabel("Akhort")
        title.setObjectName("Title")
        root.addWidget(title)
        self._expanded_widgets.append(title)

        hint = QLabel("Hold <b>Ctrl + Alt</b> to talk")
        hint.setObjectName("Subtle")
        root.addWidget(hint)
        self._expanded_widgets.append(hint)

        self._signin_label = QLabel("Signed in")
        self._signin_label.setObjectName("SubtleSmall")
        root.addWidget(self._signin_label)
        self._expanded_widgets.append(self._signin_label)

        last_label = QLabel("Last response")
        last_label.setObjectName("SubtleSmall")
        root.addWidget(last_label)
        self._expanded_widgets.append(last_label)

        self._response_label = QLabel("Nothing yet — hold Ctrl+Alt and ask me something.")
        self._response_label.setObjectName("Response")
        self._response_label.setWordWrap(True)
        self._response_label.setMinimumHeight(48)
        self._response_label.setMaximumHeight(96)
        root.addWidget(self._response_label)
        self._expanded_widgets.append(self._response_label)

        # Color picker
        col_label = QLabel("Arrow color")
        col_label.setObjectName("SubtleSmall")
        root.addWidget(col_label)
        self._expanded_widgets.append(col_label)

        colors_row = QHBoxLayout()
        colors_row.setSpacing(8)
        colors_row.setContentsMargins(0, 0, 0, 0)
        colors_wrap = QFrame()
        colors_wrap.setLayout(colors_row)
        self._color_buttons: dict[str, QPushButton] = {}
        for name, hex_ in ARROW_COLORS:
            b = QPushButton("")
            b.setObjectName("Swatch")
            b.setToolTip(name)
            b.setStyleSheet(f"#Swatch {{ background: {hex_}; }}")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _checked=False, h=hex_: self._on_color_pick(h))
            self._color_buttons[hex_] = b
            colors_row.addWidget(b)
        colors_row.addStretch()
        root.addWidget(colors_wrap)
        self._expanded_widgets.append(colors_wrap)

        # Button row
        btns = QHBoxLayout()
        btns.setSpacing(8)
        btns_wrap = QFrame(); btns_wrap.setLayout(btns)
        dash = QPushButton("Dashboard")
        dash.clicked.connect(lambda: self.open_dashboard_requested.emit())
        btns.addWidget(dash)
        signout = QPushButton("Sign out")
        signout.clicked.connect(lambda: self.signout_requested.emit())
        btns.addWidget(signout)
        btns.addStretch()
        quit_btn = QPushButton("Quit")
        quit_btn.setObjectName("Quit")
        quit_btn.clicked.connect(lambda: self.quit_requested.emit())
        btns.addWidget(quit_btn)
        root.addWidget(btns_wrap)
        self._expanded_widgets.append(btns_wrap)

    # ----- hover state ----------------------------------------------------

    def enterEvent(self, event):
        self._collapse_timer.stop()
        if not self._expanded:
            self._set_expanded(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        # Don't collapse immediately - small grace window so accidental
        # cursor exits (e.g. moving between two child widgets) don't
        # flicker the panel away. Real hover-out triggers the collapse.
        self._collapse_timer.start(self.COLLAPSE_DELAY_MS)
        super().leaveEvent(event)

    def _collapse_now(self) -> None:
        if self._expanded:
            self._set_expanded(False)

    def _set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        for w in self._expanded_widgets:
            w.setVisible(expanded)
        if expanded:
            self.setFixedSize(self.EXPANDED_W, self.EXPANDED_H)
        else:
            # Collapsed: only the bar row visible. Adjust height to the
            # contents so we don't leave a giant blank pill.
            self.setFixedSize(self.COLLAPSED_W, self.COLLAPSED_H)
        self._reposition()

    def _on_color_pick(self, hex_color: str) -> None:
        _log.info("color picked %s", hex_color)
        self.color_chosen.emit(hex_color)
        self.set_active_color(hex_color)

    # ----- paint (custom - draws the rounded background under children) --

    def paintEvent(self, _event) -> None:
        # WA_TranslucentBackground means QSS background doesn't show
        # behind the layout area unless we paint it ourselves.
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(235, 227, 207), 1))
        p.setBrush(QBrush(QColor(250, 245, 232)))
        p.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 14, 14)
        p.end()


class _PulseDot(QWidget):
    """Tiny breathing dot - sits to the left of 'AKHORT' in the bar."""

    SIZE = 12

    def __init__(self, parent_bar: "HoverBar") -> None:
        super().__init__(parent_bar)
        self._bar = parent_bar
        self.setFixedSize(self.SIZE + 4, self.SIZE + 4)

    def paintEvent(self, _event) -> None:
        breath = 0.5 + 0.5 * math.sin(self._bar._pulse)
        if self._bar._state == STATE_LISTENING:
            core = QColor(47, 122, 79)
        elif self._bar._state == STATE_BUSY:
            core = QColor(138, 90, 48)
        else:
            core = QColor(245, 200, 110)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() / 2, self.height() / 2
        halo = QColor(core); halo.setAlpha(int(60 + 110 * breath))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(halo))
        p.drawEllipse(QPoint(int(cx), int(cy)), 7, 7)
        p.setBrush(QBrush(core))
        p.drawEllipse(QPoint(int(cx), int(cy)), 4, 4)
        p.end()

"""Floating control panel - the small always-on-top window that pops up
when the user clicks the tray icon.

Mirrors what Clicky / Nuts on Mac show in their NSPanel dropdown:
  * App name + status line ("Idle" / "Listening" / "Responding")
  * Hotkey reminder
  * The most recent assistant response (live as it streams)
  * Buttons: Open dashboard / Sign out / Quit

Why a panel and not just a tooltip? Tooltips can't stream live text, can't
hold a button, and Windows aggressively dismisses them. A real (small,
borderless) widget is more honest and matches the UX users expect from
this class of app.

Threading: any UI mutation from background threads must go through
``QTimer.singleShot(0, callable)`` since QWidget methods are main-thread
only. The public set_* helpers below already use that pattern; just call
them from anywhere.
"""
from __future__ import annotations

import logging

from PyQt6.QtCore import Qt, QTimer, QPoint
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QFrame,
)

_log = logging.getLogger("nuts.panel")


class ControlPanel(QWidget):
    def __init__(
        self,
        on_open_dashboard,
        on_signout,
        on_quit,
    ) -> None:
        # No parent on purpose: this is a top-level "shell" widget that
        # outlives any individual window. The QApplication keeps it alive
        # via setQuitOnLastWindowClosed(False) in app.run().
        super().__init__(None)

        # Window flags: frameless + always-on-top.
        # We deliberately DROPPED Qt.Tool here. On Windows 11, frameless
        # Tool windows with no parent + WA_ShowWithoutActivating sometimes
        # never paint visibly even after show() returns - the icon shows
        # in the taskbar but the actual window content is invisible. Using
        # a regular top-level (no Tool) makes show() reliable. We hide the
        # taskbar entry separately if needed (it's already minimal).
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # WA_TranslucentBackground intentionally NOT set: with frameless,
        # translucent backgrounds need a custom paintEvent or the window
        # is fully invisible. We paint via QSS background instead.
        self.setFixedWidth(340)

        self._on_open_dashboard = on_open_dashboard
        self._on_signout = on_signout
        self._on_quit = on_quit

        self._build_ui()
        self.set_status("Idle")
        self.set_signin("Checking...")
        self.set_response("")

    # ----- public API (thread-safe via QTimer marshalling) ----------------

    def set_status(self, text: str) -> None:
        # Map the human label to the object name that drives QSS color so
        # the pill goes green on "Listening", brown on "Thinking", etc.
        # Polymorphic QSS approach - no extra CSS rules per state.
        upper = text.upper()
        if "LISTEN" in upper:
            new_name = "StatusListening"
        elif "THINK" in upper or "REPLY" in upper or "RESPOND" in upper or "BUSY" in upper:
            new_name = "StatusBusy"
        else:
            new_name = "Status"
        def _apply():
            self._status.setText(upper)
            self._status.setObjectName(new_name)
            # Force QSS re-evaluation; setObjectName alone doesn't repaint.
            self._status.style().unpolish(self._status)
            self._status.style().polish(self._status)
        QTimer.singleShot(0, _apply)

    def set_signin(self, text: str) -> None:
        QTimer.singleShot(0, lambda: self._signin.setText(text))

    def set_response(self, text: str) -> None:
        # Truncate aggressively - this is a status line, not a chat log.
        # Show the last 280 chars so the tail of the latest response is
        # what's visible if the model is verbose.
        body = text[-280:] if len(text) > 280 else text
        QTimer.singleShot(0, lambda: self._response.setText(body))

    def append_response(self, chunk: str) -> None:
        current = self._response.text()
        self.set_response((current or "") + chunk)

    def show_near_tray(self) -> None:
        """Position the panel just above the system tray and show it.

        We compute the screen's available rect (which excludes the
        taskbar) and anchor the panel to its bottom-right corner with a
        small margin. That puts it where the user is already looking
        after a tray-icon click.
        """
        QTimer.singleShot(0, self._show_impl)

    def _show_impl(self) -> None:
        screen = QGuiApplication.screenAt(self._cursor_pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        # Force a layout pass so width/height reflect the populated widget.
        # CRITICAL: use self.width()/height() here, NOT sizeHint() - the
        # widget has setFixedWidth(340) but sizeHint().width() returns the
        # layout's natural width (~225), so positioning by sizeHint pushed
        # the right edge 100+ px off-screen. That's why v0.2 looked like
        # "panel doesn't appear" even though it was technically visible.
        self.adjustSize()
        w, h = self.width(), self.height()
        margin = 12
        x = area.right() - w - margin
        y = area.bottom() - h - margin
        # Clamp inside the available area as a belt-and-suspenders against
        # any future regression (multi-monitor scaling, DPI changes).
        x = max(area.left() + margin, min(x, area.right() - w - margin))
        y = max(area.top() + margin, min(y, area.bottom() - h - margin))
        _log.info("show panel at (%d, %d) actual size=%dx%d screen=%s",
                  x, y, w, h, area)
        self.move(QPoint(x, y))
        self.show()
        self.raise_()
        self.activateWindow()
        _log.info("panel visible=%s geometry=%s", self.isVisible(), self.geometry())

    def toggle_near_tray(self) -> None:
        QTimer.singleShot(0, self._toggle_impl)

    def _toggle_impl(self) -> None:
        was_visible = self.isVisible()
        _log.info("toggle requested; currently visible=%s", was_visible)
        if was_visible:
            self.hide()
        else:
            self._show_impl()

    # ----- internal -------------------------------------------------------

    def _cursor_pos(self) -> QPoint:
        # Imported lazily because QCursor pulls a QGuiApplication instance,
        # which the caller (Application) only finishes constructing AFTER
        # this widget's __init__. Avoids a chicken-and-egg in startup.
        from PyQt6.QtGui import QCursor
        return QCursor.pos()

    def _build_ui(self) -> None:
        # CRITICAL: setObjectName MUST come BEFORE setStyleSheet, otherwise
        # the QWidget#ControlPanelRoot {...} rule never matches and the
        # frameless window paints with nothing - looked invisible / broken
        # in v0.1. Same ordering applies to every named child below.
        self.setObjectName("ControlPanelRoot")
        # We hand-roll styling to match the akhrots.com warm-cream brand
        # from the dashboard so users feel they're using the same product.
        # CRITICAL fixes vs. v0.2:
        #   * Explicit `color:` on QPushButton - without it, Windows dark
        #     mode rendered button text white-on-white (looked empty).
        #   * Bigger font defaults; v0.2's 11pt looked dense and amateur.
        #   * Status pill colored by state (cream when idle, green when
        #     listening) - matches clicky's "you can see what mode I'm in".
        self.setStyleSheet(
            "QWidget#ControlPanelRoot { background: #faf5e8; border: 1px solid #ebe3cf; border-radius: 12px; }"
            "QLabel { color: #1f1b16; font-family: 'Segoe UI'; font-size: 13px; }"
            "QLabel#Title { font-size: 17px; font-weight: 700; letter-spacing: -0.2px; }"
            "QLabel#Subtle { color: #6b6357; font-size: 12px; }"
            "QLabel#SubtleSmall { color: #6b6357; font-size: 11px; font-weight: 500; }"
            "QLabel#Status { color: #1f1b16; font-size: 11px; font-weight: 700; padding: 4px 10px; "
            "  background: #ebe3cf; border-radius: 10px; letter-spacing: 0.4px; }"
            "QLabel#StatusListening { color: #ffffff; background: #2f7a4f; }"
            "QLabel#StatusBusy { color: #ffffff; background: #8a5a30; }"
            "QLabel#Response { color: #1f1b16; font-size: 12px; line-height: 1.45; }"
            "QPushButton { color: #1f1b16; background: #ffffff; border: 1px solid #ebe3cf; "
            "  border-radius: 8px; padding: 6px 14px; font-family: 'Segoe UI'; "
            "  font-size: 12px; font-weight: 600; }"
            "QPushButton:hover { background: #f5efde; border-color: #d9d0b8; }"
            "QPushButton:pressed { background: #ebe3cf; }"
            "QPushButton#Quit { color: #8a3030; }"
            "QPushButton#Quit:hover { background: #fae5e5; border-color: #e7c4c4; }"
            "QFrame#Sep { color: #ebe3cf; background: #ebe3cf; max-height: 1px; }"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        # Title row: brand + status pill
        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("Akhort")
        title.setObjectName("Title")
        head.addWidget(title)
        head.addStretch()
        self._status = QLabel("IDLE")
        self._status.setObjectName("Status")
        head.addWidget(self._status)
        root.addLayout(head)

        # Hotkey reminder - small but legible
        hint = QLabel("Hold <b>Ctrl + Alt</b> to talk")
        hint.setObjectName("Subtle")
        root.addWidget(hint)

        # Sign-in line
        self._signin = QLabel("Signed in")
        self._signin.setObjectName("SubtleSmall")
        root.addWidget(self._signin)

        sep = QFrame(); sep.setObjectName("Sep"); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        root.addWidget(sep)

        # Live response area
        resp_label = QLabel("Last response")
        resp_label.setObjectName("SubtleSmall")
        root.addWidget(resp_label)
        self._response = QLabel("Nothing yet — hold Ctrl+Alt and ask me something.")
        self._response.setObjectName("Response")
        self._response.setWordWrap(True)
        self._response.setMinimumHeight(64)
        self._response.setMaximumHeight(140)
        self._response.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self._response)

        # Buttons
        sep2 = QFrame(); sep2.setObjectName("Sep"); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setFixedHeight(1)
        root.addWidget(sep2)
        btns = QHBoxLayout()
        btns.setSpacing(8)
        dash = QPushButton("Dashboard")
        dash.clicked.connect(self._on_open_dashboard)
        btns.addWidget(dash)
        signout = QPushButton("Sign out")
        signout.clicked.connect(self._on_signout)
        btns.addWidget(signout)
        btns.addStretch()
        quit_btn = QPushButton("Quit")
        quit_btn.setObjectName("Quit")
        quit_btn.clicked.connect(self._on_quit)
        btns.addWidget(quit_btn)
        root.addLayout(btns)

    # ----- behavior -------------------------------------------------------

    # Note: we deliberately do NOT override focusOutEvent to auto-hide.
    # WA_ShowWithoutActivating means the panel never receives focus to
    # begin with, so focus-out fires unpredictably (often immediately on
    # show, which made the panel flash and disappear in v0.1). Toggle is
    # via tray-icon click and the in-panel Quit/Sign-out buttons.

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

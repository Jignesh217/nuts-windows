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

        # Frameless + always-on-top + tool-window stops it from appearing
        # in the taskbar (the tray icon is enough). WA_StyledBackground is
        # the magic bit: without it, QSS `background:` on a frameless
        # widget paints nothing and the panel looks invisible-on-desktop,
        # which is exactly the "fix the panel" bug we were hitting.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # Show-without-activating keeps the panel from stealing focus from
        # whatever the user was doing. We deliberately do NOT auto-hide on
        # focus-out anymore (was a bug: panel never got focus, so it hid
        # IMMEDIATELY). Toggling happens via the tray icon and the Quit
        # button instead.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
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
        QTimer.singleShot(0, lambda: self._status.setText(text))

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
        # Force a layout so sizeHint reflects the populated widget.
        self.adjustSize()
        sz = self.sizeHint()
        margin = 12
        x = area.right() - sz.width() - margin
        y = area.bottom() - sz.height() - margin
        self.move(QPoint(x, y))
        self.show()
        self.raise_()

    def toggle_near_tray(self) -> None:
        QTimer.singleShot(0, self._toggle_impl)

    def _toggle_impl(self) -> None:
        if self.isVisible():
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
        self.setStyleSheet(
            "QWidget#ControlPanelRoot { background: #faf5e8; border: 1px solid #ebe3cf; border-radius: 10px; }"
            "QLabel { color: #1f1b16; font-family: 'Segoe UI'; }"
            "QLabel#Title { font-size: 14px; font-weight: 600; }"
            "QLabel#Subtle { color: #6b6357; font-size: 11px; }"
            "QLabel#Status { font-size: 12px; font-weight: 600; padding: 2px 6px; "
            "  background: #ebe3cf; border-radius: 4px; }"
            "QLabel#Response { color: #1f1b16; font-size: 12px; line-height: 1.4; }"
            "QPushButton { background: #ffffff; border: 1px solid #ebe3cf; "
            "  border-radius: 6px; padding: 5px 10px; font-family: 'Segoe UI'; font-size: 11px; }"
            "QPushButton:hover { background: #f5efde; }"
            "QPushButton#Quit { color: #8a3030; }"
            "QFrame#Sep { background: #ebe3cf; max-height: 1px; }"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        # Title row: brand + status pill
        head = QHBoxLayout()
        title = QLabel("Akhort")
        title.setObjectName("Title")
        head.addWidget(title)
        head.addStretch()
        self._status = QLabel("Idle")
        self._status.setObjectName("Status")
        head.addWidget(self._status)
        root.addLayout(head)

        # Hotkey reminder
        hint = QLabel("Hold <b>Ctrl + Alt</b> to talk")
        hint.setObjectName("Subtle")
        root.addWidget(hint)

        # Sign-in line
        self._signin = QLabel("Signed in")
        self._signin.setObjectName("Subtle")
        root.addWidget(self._signin)

        sep = QFrame(); sep.setObjectName("Sep"); sep.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep)

        # Live response area
        resp_label = QLabel("Last response")
        resp_label.setObjectName("Subtle")
        root.addWidget(resp_label)
        self._response = QLabel("")
        self._response.setObjectName("Response")
        self._response.setWordWrap(True)
        self._response.setMinimumHeight(60)
        self._response.setMaximumHeight(120)
        self._response.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self._response)

        # Buttons
        sep2 = QFrame(); sep2.setObjectName("Sep"); sep2.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep2)
        btns = QHBoxLayout()
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

"""In-app settings dialog for brain provider + API key configuration.

Opened via the top-right gear icon in the HoverBar. Cleaner than v0.9:
proper typography, real spacing, framed soft-shadow window, big primary
Save button, clear field hierarchy, polished combo box.
"""
from __future__ import annotations

import logging

from PyQt6.QtCore import Qt, pyqtSignal, QPoint
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
    QLineEdit,
    QPushButton,
    QComboBox,
    QFrame,
    QGraphicsDropShadowEffect,
)

from nuts_windows import config


_log = logging.getLogger("nuts.settings")


# (display label, provider id, hint, key url)
PROVIDERS = [
    ("Demo mode (offline)",       "demo",       "No key — rule-based responses for testing",                          ""),
    ("Grok (xAI)",                "grok",       "xai-... — free tier with monthly credits",                          "https://console.x.ai/"),
    ("Anthropic Claude",          "anthropic",  "sk-ant-api03-... — paid, best vision quality",                       "https://console.anthropic.com/settings/keys"),
    ("OpenAI GPT-4o",             "openai",     "sk-proj-... or sk-... — paid, also great vision",                    "https://platform.openai.com/api-keys"),
    ("Custom OpenAI-compatible",  "custom",     "Bring your own URL — useful for self-hosted / Akhrot integration",   ""),
]


class SettingsDialog(QWidget):
    saved = pyqtSignal()

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # Container holds everything so we can paint a soft drop-shadow
        # around it via QGraphicsDropShadowEffect. The shadow margin is
        # built into the outer layout so child widgets aren't clipped.
        self._SHADOW_MARGIN = 24
        self.setFixedWidth(500 + self._SHADOW_MARGIN * 2)
        self._build_ui()
        self._load_current()

    # ----- public API -----------------------------------------------------

    def show_centered(self) -> None:
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.adjustSize()
        x = screen.left() + (screen.width() - self.width()) // 2
        y = screen.top() + max(40, int((screen.height() - self.height()) * 0.28))
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()

    # ----- internal -------------------------------------------------------

    def _build_ui(self) -> None:
        # Outer layout adds the shadow margin so the drop shadow can
        # render around the inner card without being clipped.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            self._SHADOW_MARGIN, self._SHADOW_MARGIN,
            self._SHADOW_MARGIN, self._SHADOW_MARGIN,
        )
        outer.setSpacing(0)

        # The card itself - a QFrame so we can clip rounded corners
        # and apply the drop shadow on it.
        self._card = QFrame()
        self._card.setObjectName("SettingsCard")
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(36)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(20, 16, 8, 110))
        self._card.setGraphicsEffect(shadow)
        outer.addWidget(self._card)

        # ---- Typography ----
        # Font family chain prefers Win11's "Segoe UI Variable" optical
        # sizes (Display for headings, Text for body) and gracefully
        # falls back to plain Segoe UI on Win10 / older. Variable
        # fonts give us much cleaner small-size rendering and finer
        # weight control. SF Pro Display is the macOS fallback even
        # though we're Windows-only (helps in dev on a Mac).
        head_family = ('"Segoe UI Variable Display", "Segoe UI Variable", '
                       '"Segoe UI", "SF Pro Display", system-ui, sans-serif')
        body_family = ('"Segoe UI Variable Text", "Segoe UI Variable", '
                       '"Segoe UI", "SF Pro Text", system-ui, sans-serif')
        mono_family = ('"Cascadia Code", "Cascadia Mono", "Consolas", '
                       '"SF Mono", monospace')

        self._card.setStyleSheet(
            # Card
            "QFrame#SettingsCard { background: #fdfaf1; border-radius: 16px; }"
            # Default label = body
            f"QLabel {{ color: #1f1b16; font-family: {body_family}; font-size: 13px; }}"
            # TITLE - display family, large, tight tracking
            f"QLabel#Title {{ font-family: {head_family}; font-size: 24px; "
            "  font-weight: 600; letter-spacing: -0.6px; line-height: 28px; }"
            # SUB - body family, medium grey, comfortable size
            "QLabel#Sub { color: #756a58; font-size: 13.5px; font-weight: 400; "
            "  line-height: 19px; }"
            # FIELD LABEL - small caps, slightly muted, looser tracking
            # so it reads as a section header not a shouting line
            "QLabel#FieldLabel { color: #4b4434; font-size: 10.5px; "
            "  font-weight: 600; letter-spacing: 1.4px; }"
            # HELP text inside the info card
            "QLabel#Help { color: #6b6357; font-size: 12.5px; "
            "  font-weight: 400; line-height: 17px; }"
            # Status pills - body family
            "QLabel#StatusOk { color: #155932; background: #dceee2; "
            "  padding: 10px 14px; border-radius: 9px; font-size: 12.5px; "
            "  font-weight: 600; }"
            "QLabel#StatusErr { color: #7a2424; background: #f5d5d5; "
            "  padding: 10px 14px; border-radius: 9px; font-size: 12.5px; "
            "  font-weight: 600; }"
            # Inputs - body family, slightly looser line for vertical rhythm
            f"QLineEdit, QComboBox {{ color: #1f1b16; background: #ffffff; "
            "  border: 1.5px solid #e3d8b5; border-radius: 10px; "
            "  padding: 11px 14px; "
            f"  font-family: {body_family}; font-size: 13.5px; font-weight: 400; "
            "  selection-background-color: #ebe3cf; }"
            "QLineEdit:focus, QComboBox:focus { border-color: #2f7a4f; }"
            "QLineEdit::placeholder { color: #a59a83; font-style: italic; }"
            # API keys are mono - read-friendly for long opaque strings
            f"QLineEdit#KeyInput {{ font-family: {mono_family}; "
            "  font-size: 12.5px; letter-spacing: 0.2px; }"
            "QComboBox::drop-down { width: 30px; border: 0; }"
            "QComboBox::down-arrow { width: 11px; height: 11px; }"
            "QComboBox QAbstractItemView { background: #ffffff; "
            "  border: 1px solid #ebe3cf; border-radius: 10px; "
            f"  font-family: {body_family}; font-size: 13.5px; "
            "  selection-background-color: #f5efde; "
            "  selection-color: #1f1b16; outline: 0; padding: 6px; }"
            "QComboBox QAbstractItemView::item { padding: 9px 12px; border-radius: 6px; }"
            # Buttons - body family
            f"QPushButton {{ color: #1f1b16; background: #ffffff; "
            "  border: 1.5px solid #e3d8b5; border-radius: 10px; "
            "  padding: 10px 20px; "
            f"  font-family: {body_family}; font-size: 13px; font-weight: 600; "
            "  letter-spacing: 0.1px; }"
            "QPushButton:hover { background: #f5efde; border-color: #c9bd96; }"
            "QPushButton:pressed { background: #ebe3cf; }"
            # Primary (Save) - dark green pill, the most prominent CTA
            "QPushButton#Primary { color: #ffffff; background: #2f7a4f; "
            "  border-color: #246238; padding: 11px 26px; "
            "  font-size: 13.5px; font-weight: 600; }"
            "QPushButton#Primary:hover { background: #266a43; }"
            "QPushButton#Primary:pressed { background: #1f5a39; }"
            # Show / Hide pill for the API key
            "QPushButton#Toggle { border-color: #ebe3cf; "
            "  font-size: 12px; font-weight: 600; }"
            # Link buttons (Get a key)
            f"QPushButton#Link {{ color: #2f7a4f; background: transparent; "
            "  border: 0; "
            f"  font-family: {body_family}; font-size: 12.5px; font-weight: 700; "
            "  text-align: left; padding: 4px 0; letter-spacing: 0.1px; }"
            "QPushButton#Link:hover { color: #1f5a39; text-decoration: underline; }"
            # Close X - quiet glyph that doesn't compete with the title
            f"QPushButton#Close {{ color: #8a8273; background: transparent; "
            f"  border: 0; font-family: {head_family}; font-size: 24px; "
            "  font-weight: 300; padding: 0; }"
            "QPushButton#Close:hover { color: #1f1b16; }"
            # Provider info band below the dropdown
            "QFrame#ProviderInfo { background: #f5efde; border-radius: 12px; "
            "  border: 1px solid #ebe3cf; }"
        )

        # Card content
        card_root = QVBoxLayout(self._card)
        card_root.setContentsMargins(32, 24, 32, 26)
        card_root.setSpacing(0)

        # Header row: title left, close button right
        head = QHBoxLayout()
        head.setSpacing(0)
        title = QLabel("Brain settings")
        title.setObjectName("Title")
        head.addWidget(title)
        head.addStretch()
        close = QPushButton("×")
        close.setObjectName("Close")
        close.setFixedSize(32, 32)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.hide)
        head.addWidget(close)
        card_root.addLayout(head)

        sub = QLabel("Pick where Akhort thinks. Keys live in Windows Credential Manager — never in plain text on disk.")
        sub.setObjectName("Sub")
        sub.setWordWrap(True)
        sub.setContentsMargins(0, 6, 0, 0)
        card_root.addWidget(sub)

        # Spacer
        card_root.addSpacing(22)

        # ----- Provider -----
        card_root.addWidget(self._field_label("PROVIDER"))
        card_root.addSpacing(6)
        self._provider_box = QComboBox()
        for label, _id, _hint, _url in PROVIDERS:
            self._provider_box.addItem(label)
        self._provider_box.currentIndexChanged.connect(self._on_provider_change)
        self._provider_box.setCursor(Qt.CursorShape.PointingHandCursor)
        card_root.addWidget(self._provider_box)

        # Inline info band that updates with the provider
        info_wrap = QFrame()
        info_wrap.setObjectName("ProviderInfo")
        info_layout = QVBoxLayout(info_wrap)
        info_layout.setContentsMargins(14, 10, 14, 12)
        info_layout.setSpacing(2)
        self._provider_hint = QLabel("")
        self._provider_hint.setObjectName("Help")
        self._provider_hint.setWordWrap(True)
        info_layout.addWidget(self._provider_hint)
        self._provider_link = QPushButton("")
        self._provider_link.setObjectName("Link")
        self._provider_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self._provider_link.clicked.connect(self._open_provider_url)
        self._provider_link.setVisible(False)
        info_layout.addWidget(self._provider_link)
        card_root.addSpacing(10)
        card_root.addWidget(info_wrap)

        # ----- API key -----
        card_root.addSpacing(20)
        card_root.addWidget(self._field_label("API KEY"))
        card_root.addSpacing(6)
        key_row = QHBoxLayout()
        key_row.setSpacing(8)
        self._key_input = QLineEdit()
        self._key_input.setObjectName("KeyInput")
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText("Paste your provider key…")
        key_row.addWidget(self._key_input)
        self._show_btn = QPushButton("Show")
        self._show_btn.setObjectName("Toggle")
        self._show_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._show_btn.setCheckable(True)
        self._show_btn.toggled.connect(self._toggle_key_visibility)
        self._show_btn.setFixedWidth(78)
        key_row.addWidget(self._show_btn)
        card_root.addLayout(key_row)

        # ----- Custom-only fields -----
        self._custom_url_label = self._field_label("BASE URL")
        self._custom_url_input = QLineEdit()
        self._custom_url_input.setPlaceholderText("https://your-llm.example.com/v1")
        self._model_label = self._field_label("MODEL (OPTIONAL)")
        self._model_input = QLineEdit()
        self._model_input.setPlaceholderText("Leave blank to use the provider default")
        card_root.addSpacing(16)
        card_root.addWidget(self._custom_url_label)
        card_root.addSpacing(6)
        card_root.addWidget(self._custom_url_input)
        card_root.addSpacing(14)
        card_root.addWidget(self._model_label)
        card_root.addSpacing(6)
        card_root.addWidget(self._model_input)

        # ----- Status (success / error feedback) -----
        self._status = QLabel("")
        self._status.setObjectName("StatusOk")
        self._status.setVisible(False)
        card_root.addSpacing(16)
        card_root.addWidget(self._status)

        # ----- Footer buttons -----
        card_root.addSpacing(22)
        foot = QHBoxLayout()
        foot.setSpacing(10)
        foot.addStretch()
        cancel = QPushButton("Cancel")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.hide)
        foot.addWidget(cancel)
        save = QPushButton("Save")
        save.setObjectName("Primary")
        save.setCursor(Qt.CursorShape.PointingHandCursor)
        save.clicked.connect(self._on_save)
        foot.addWidget(save)
        card_root.addLayout(foot)

    def _field_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("FieldLabel")
        return label

    # ----- state ----------------------------------------------------------

    def _load_current(self) -> None:
        s = config.load_brain_settings()
        for i, (_label, prov_id, _hint, _url) in enumerate(PROVIDERS):
            if prov_id == s.provider:
                self._provider_box.setCurrentIndex(i)
                break
        if s.api_key:
            self._key_input.setText(s.api_key)
        if s.base_url:
            self._custom_url_input.setText(s.base_url)
        if s.model:
            self._model_input.setText(s.model)
        self._on_provider_change(self._provider_box.currentIndex())

    def _on_provider_change(self, idx: int) -> None:
        _label, prov_id, hint, url = PROVIDERS[idx]
        self._provider_hint.setText(hint)
        if url:
            self._provider_link.setText("Get a key →")
            self._provider_link.setProperty("_url", url)
            self._provider_link.setVisible(True)
        else:
            self._provider_link.setVisible(False)
        self._status.setVisible(False)

        is_demo = prov_id == "demo"
        is_custom = prov_id == "custom"
        self._key_input.setEnabled(not is_demo)
        self._show_btn.setEnabled(not is_demo)
        if is_demo:
            self._key_input.setPlaceholderText("(no key needed for Demo)")
        else:
            self._key_input.setPlaceholderText("Paste your provider key…")
        self._custom_url_label.setVisible(is_custom)
        self._custom_url_input.setVisible(is_custom)
        self._model_label.setVisible(is_custom)
        self._model_input.setVisible(is_custom)
        self.adjustSize()

    def _toggle_key_visibility(self, shown: bool) -> None:
        self._key_input.setEchoMode(
            QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
        )
        self._show_btn.setText("Hide" if shown else "Show")

    def _on_save(self) -> None:
        idx = self._provider_box.currentIndex()
        _label, prov_id, _hint, _url = PROVIDERS[idx]
        key = self._key_input.text().strip() or None
        base_url = self._custom_url_input.text().strip() or None
        model = self._model_input.text().strip() or None

        if prov_id != "demo" and not key:
            self._show_status(False, "API key is required for this provider.")
            return
        if prov_id == "custom" and not base_url:
            self._show_status(False, "Custom provider needs a Base URL.")
            return

        settings = config.BrainSettings(
            provider=prov_id, api_key=key, base_url=base_url, model=model,
        )
        try:
            config.save_brain_settings(settings)
        except Exception as e:
            _log.exception("save_brain_settings failed")
            self._show_status(False, f"Save failed: {e}")
            return

        nice = {
            "demo": "Demo mode",
            "anthropic": "Claude (Anthropic)",
            "grok": "Grok (xAI)",
            "openai": "OpenAI GPT-4o",
            "custom": "your custom endpoint",
        }.get(prov_id, prov_id)
        self._show_status(True, f"Saved. Akhort is now using {nice}.")
        self.saved.emit()

    def _show_status(self, ok: bool, msg: str) -> None:
        self._status.setObjectName("StatusOk" if ok else "StatusErr")
        self._status.style().unpolish(self._status)
        self._status.style().polish(self._status)
        self._status.setText(msg)
        self._status.setVisible(True)
        self.adjustSize()

    def _open_provider_url(self) -> None:
        import webbrowser
        url = self._provider_link.property("_url")
        if url:
            webbrowser.open(str(url))

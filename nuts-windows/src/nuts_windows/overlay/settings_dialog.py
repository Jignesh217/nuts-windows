"""In-app settings dialog for brain provider + API key configuration.

The user opens this via the gear icon in the expanded HoverBar.

  Provider dropdown:
    * Demo (no key)        - offline rule responses, free, dumb
    * Grok (xAI)           - free tier with credits, vision-capable
    * Anthropic Claude     - paid, best quality vision-LLM today
    * OpenAI GPT-4o        - paid, great vision
    * Custom               - paste your own OpenAI-compatible base URL

  API key field          - masked input. Stored in Windows Credential
                           Manager via the existing config.save_brain_settings.

  Test connection        - hits the provider with a tiny request to
                           verify the key works. Shows green / red.

  Save                   - persists + triggers a brain reload.

We avoid framing this as a QDialog (which steals focus and forces a
modal interaction) - it's a borderless flat panel that floats above
the HoverBar exactly like the dashboard's connector modals.
"""
from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QComboBox,
    QFrame,
)

from nuts_windows import config


_log = logging.getLogger("nuts.settings")


# Display label -> (provider id, hint about what the key looks like)
PROVIDERS = [
    ("Demo (offline, no key)",  "demo",       ""),
    ("Grok / xAI (free tier)",  "grok",       "xai-... starts with 'xai-'"),
    ("Anthropic Claude",        "anthropic",  "sk-ant-api03-..."),
    ("OpenAI GPT-4o",           "openai",     "sk-proj-... or sk-..."),
    ("Custom OpenAI-compatible","custom",     "your provider's key"),
]


class SettingsDialog(QWidget):
    saved = pyqtSignal()   # emitted after save; app.py listens to reload the brain

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedWidth(440)
        self._build_ui()
        self._load_current()

    # ----- public API -----------------------------------------------------

    def show_centered(self) -> None:
        """Center on the primary screen and raise to the top."""
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.adjustSize()
        x = screen.left() + (screen.width() - self.width()) // 2
        y = screen.top() + int(screen.height() * 0.22)
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()

    # ----- internal -------------------------------------------------------

    def _build_ui(self) -> None:
        self.setObjectName("SettingsRoot")
        self.setStyleSheet(
            "QWidget#SettingsRoot { background: #faf5e8; border: 1px solid #ebe3cf; border-radius: 14px; }"
            "QLabel { color: #1f1b16; font-family: 'Segoe UI'; font-size: 13px; }"
            "QLabel#Title { font-size: 17px; font-weight: 700; letter-spacing: -0.2px; }"
            "QLabel#Subtle { color: #6b6357; font-size: 11px; }"
            "QLabel#Status { padding: 6px 10px; border-radius: 6px; font-size: 11px; font-weight: 600; }"
            "QLabel#StatusOk { color: #1d5f3a; background: #d5eedd; }"
            "QLabel#StatusErr { color: #7a2424; background: #f5d5d5; }"
            "QLineEdit, QComboBox { color: #1f1b16; background: #ffffff; border: 1px solid #ebe3cf; "
            "  border-radius: 8px; padding: 6px 10px; font-family: 'Segoe UI'; font-size: 12px; }"
            "QLineEdit:focus, QComboBox:focus { border-color: #b8a878; }"
            "QPushButton { color: #1f1b16; background: #ffffff; border: 1px solid #ebe3cf; "
            "  border-radius: 8px; padding: 6px 14px; font-family: 'Segoe UI'; "
            "  font-size: 12px; font-weight: 600; }"
            "QPushButton:hover { background: #f5efde; border-color: #d9d0b8; }"
            "QPushButton#Save { color: #ffffff; background: #2f7a4f; border-color: #266a43; }"
            "QPushButton#Save:hover { background: #266a43; }"
            "QFrame#Sep { background: #ebe3cf; max-height: 1px; }"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        # Header
        head = QHBoxLayout()
        title = QLabel("Settings")
        title.setObjectName("Title")
        head.addWidget(title)
        head.addStretch()
        close_btn = QPushButton("×")
        close_btn.setFixedWidth(34)
        close_btn.clicked.connect(self.hide)
        head.addWidget(close_btn)
        root.addLayout(head)

        sub = QLabel("Choose where Nuts gets its brain. Keys are stored in Windows Credential Manager.")
        sub.setObjectName("Subtle")
        sub.setWordWrap(True)
        root.addWidget(sub)

        sep = QFrame(); sep.setObjectName("Sep"); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        root.addWidget(sep)

        # Provider
        root.addWidget(QLabel("Provider"))
        self._provider_box = QComboBox()
        for label, _id, _hint in PROVIDERS:
            self._provider_box.addItem(label)
        self._provider_box.currentIndexChanged.connect(self._on_provider_change)
        root.addWidget(self._provider_box)

        # API key
        root.addWidget(QLabel("API key"))
        self._key_input = QLineEdit()
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText("Paste your provider key here…")
        root.addWidget(self._key_input)

        # Hint for the provider
        self._hint_label = QLabel("")
        self._hint_label.setObjectName("Subtle")
        root.addWidget(self._hint_label)

        # Custom URL + model (shown only when provider == 'custom')
        self._custom_url_label = QLabel("Base URL")
        self._custom_url_label.setVisible(False)
        root.addWidget(self._custom_url_label)
        self._custom_url_input = QLineEdit()
        self._custom_url_input.setPlaceholderText("https://your-llm.example.com/v1")
        self._custom_url_input.setVisible(False)
        root.addWidget(self._custom_url_input)

        self._model_label = QLabel("Model (optional override)")
        self._model_label.setVisible(False)
        root.addWidget(self._model_label)
        self._model_input = QLineEdit()
        self._model_input.setPlaceholderText("Provider default if blank")
        self._model_input.setVisible(False)
        root.addWidget(self._model_input)

        # Status / test result row
        self._status = QLabel("")
        self._status.setObjectName("Status")
        self._status.setVisible(False)
        root.addWidget(self._status)

        # Buttons
        sep2 = QFrame(); sep2.setObjectName("Sep"); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setFixedHeight(1)
        root.addWidget(sep2)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        get_key = QPushButton("Get a free Grok key →")
        get_key.clicked.connect(self._open_grok_signup)
        btns.addWidget(get_key)
        btns.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.hide)
        btns.addWidget(cancel)
        save = QPushButton("Save")
        save.setObjectName("Save")
        save.clicked.connect(self._on_save)
        btns.addWidget(save)
        root.addLayout(btns)

    # ----- state ----------------------------------------------------------

    def _load_current(self) -> None:
        s = config.load_brain_settings()
        for i, (_label, prov_id, _hint) in enumerate(PROVIDERS):
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
        _label, prov_id, hint = PROVIDERS[idx]
        self._hint_label.setText(f"Format: {hint}" if hint else "")
        self._status.setVisible(False)
        is_demo = prov_id == "demo"
        is_custom = prov_id == "custom"
        self._key_input.setEnabled(not is_demo)
        self._key_input.setPlaceholderText(
            "(no key needed for Demo)" if is_demo else "Paste your provider key here…"
        )
        self._custom_url_label.setVisible(is_custom)
        self._custom_url_input.setVisible(is_custom)
        self._model_label.setVisible(is_custom)
        self._model_input.setVisible(is_custom)
        self.adjustSize()

    def _on_save(self) -> None:
        idx = self._provider_box.currentIndex()
        _label, prov_id, _hint = PROVIDERS[idx]
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

        self._show_status(True, f"Saved — Nuts will use {prov_id}.")
        self.saved.emit()

    def _show_status(self, ok: bool, msg: str) -> None:
        self._status.setObjectName("StatusOk" if ok else "StatusErr")
        self._status.style().unpolish(self._status)
        self._status.style().polish(self._status)
        self._status.setText(msg)
        self._status.setVisible(True)

    def _open_grok_signup(self) -> None:
        import webbrowser
        webbrowser.open("https://console.x.ai/")

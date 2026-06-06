"""Runtime configuration.

Resolution order (first hit wins):

  1. Process env vars (``NUTS_WORKER_URL``, ``NUTS_TOKEN``).
     Useful for `python -m nuts_windows` during development.

  2. Windows Credential Manager entry written by
     :mod:`nuts_windows.bootstrap` on first launch.

  3. None - app starts in a "not signed in" state and shows a tooltip on
     the tray icon asking the user to sign in at akhrots.com/app and
     drop the install zip in Downloads.

The worker URL has a sensible default (akhrot's production worker); the
bearer never has a default - if it isn't found we explicitly stay logged
out rather than silently failing on every API call.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import keyring

SERVICE = "com.akhrots.nuts"
ACCOUNT_TOKEN = "mcp-bearer"
ACCOUNT_URL = "worker-url"
ACCOUNT_ARROW_COLOR = "arrow-color"
ACCOUNT_BRAIN_PROVIDER = "brain-provider"   # 'demo' / 'anthropic' / 'grok' / 'openai' / 'custom'
ACCOUNT_BRAIN_API_KEY = "brain-api-key"     # the actual secret, in Windows Cred Mgr
ACCOUNT_BRAIN_BASE_URL = "brain-base-url"   # for 'custom' provider
ACCOUNT_BRAIN_MODEL = "brain-model"         # override model id (optional)

DEFAULT_WORKER_URL = "https://akhrots.com/mcp"
DEFAULT_HOTKEY = "<ctrl>+<alt>"   # pynput-style notation
# Default to the YELLOW swatch from the HoverBar picker - it's the
# closest match to the akhrots.com cream branding without the
# Tan-vs-Yellow visual confusion the v0.7 picker had.
DEFAULT_ARROW_COLOR = "#ffe48c"


@dataclass(frozen=True, slots=True)
class Config:
    worker_url: str
    token: Optional[str]   # None when the user hasn't signed in yet
    hotkey: str            # pynput key combo, e.g. "<ctrl>+<alt>"
    arrow_color: str       # CSS-style hex string set by the color picker

    @property
    def signed_in(self) -> bool:
        return bool(self.token)


def load() -> Config:
    """Read the current effective config from env + keyring."""
    env_url = os.environ.get("NUTS_WORKER_URL")
    env_token = os.environ.get("NUTS_TOKEN")
    env_hotkey = os.environ.get("NUTS_HOTKEY")

    url = env_url or keyring.get_password(SERVICE, ACCOUNT_URL) or DEFAULT_WORKER_URL
    token = env_token or keyring.get_password(SERVICE, ACCOUNT_TOKEN)
    hotkey = env_hotkey or DEFAULT_HOTKEY
    arrow_color = (
        keyring.get_password(SERVICE, ACCOUNT_ARROW_COLOR)
        or DEFAULT_ARROW_COLOR
    )
    return Config(
        worker_url=url, token=token, hotkey=hotkey, arrow_color=arrow_color,
    )


def save_arrow_color(hex_color: str) -> None:
    """Persist the user's arrow-color pick so it survives restarts."""
    keyring.set_password(SERVICE, ACCOUNT_ARROW_COLOR, hex_color)


# ---------------------------------------------------------------------------
# Brain provider configuration (settings dialog persistence)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class BrainSettings:
    provider: str             # 'demo' / 'anthropic' / 'grok' / 'openai' / 'custom'
    api_key: Optional[str]
    base_url: Optional[str]   # only used for 'custom'
    model: Optional[str]      # optional model override

    @property
    def is_configured(self) -> bool:
        if self.provider == "demo":
            return True
        return bool(self.api_key)


def load_brain_settings() -> BrainSettings:
    """Read brain provider config from keyring (and ANTHROPIC_API_KEY env
    var as a back-compat fallback so existing setups keep working)."""
    provider = keyring.get_password(SERVICE, ACCOUNT_BRAIN_PROVIDER) or "demo"
    api_key = keyring.get_password(SERVICE, ACCOUNT_BRAIN_API_KEY)
    base_url = keyring.get_password(SERVICE, ACCOUNT_BRAIN_BASE_URL)
    model = keyring.get_password(SERVICE, ACCOUNT_BRAIN_MODEL)
    # If nothing stored but ANTHROPIC_API_KEY is in env, surface it.
    if not api_key and provider == "demo":
        anth = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("NUTS_ANTHROPIC_KEY")
        if anth:
            provider = "anthropic"
            api_key = anth
    return BrainSettings(
        provider=provider, api_key=api_key, base_url=base_url, model=model,
    )


def save_brain_settings(settings: BrainSettings) -> None:
    """Persist brain config so the user doesn't re-enter keys at every launch."""
    keyring.set_password(SERVICE, ACCOUNT_BRAIN_PROVIDER, settings.provider)
    if settings.api_key:
        keyring.set_password(SERVICE, ACCOUNT_BRAIN_API_KEY, settings.api_key)
    else:
        try:
            keyring.delete_password(SERVICE, ACCOUNT_BRAIN_API_KEY)
        except Exception:
            pass
    for account, value in (
        (ACCOUNT_BRAIN_BASE_URL, settings.base_url),
        (ACCOUNT_BRAIN_MODEL, settings.model),
    ):
        if value:
            keyring.set_password(SERVICE, account, value)
        else:
            try:
                keyring.delete_password(SERVICE, account)
            except Exception:
                pass


def save_credentials(url: str, token: str) -> None:
    """Persist worker URL + bearer to Windows Credential Manager."""
    keyring.set_password(SERVICE, ACCOUNT_URL, url)
    keyring.set_password(SERVICE, ACCOUNT_TOKEN, token)


def clear_credentials() -> None:
    """Sign out - wipe both stored entries."""
    for account in (ACCOUNT_TOKEN, ACCOUNT_URL):
        try:
            keyring.delete_password(SERVICE, account)
        except keyring.errors.PasswordDeleteError:
            pass

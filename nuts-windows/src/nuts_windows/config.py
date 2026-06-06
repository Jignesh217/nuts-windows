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

DEFAULT_WORKER_URL = "https://akhrots.com/mcp"
DEFAULT_HOTKEY = "<ctrl>+<alt>"   # pynput-style notation


@dataclass(frozen=True, slots=True)
class Config:
    worker_url: str
    token: Optional[str]   # None when the user hasn't signed in yet
    hotkey: str            # pynput key combo, e.g. "<ctrl>+<alt>"

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
    return Config(worker_url=url, token=token, hotkey=hotkey)


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

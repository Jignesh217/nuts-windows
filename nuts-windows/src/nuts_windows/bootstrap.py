"""First-launch akhort-config.json reader.

Mirrors the Mac ``AkhortBootstrap.swift`` reference dropped into the Nuts
Xcode project. On the user's first run we look for a config file the
akhrots.com dashboard bundled into the install zip, copy its credentials
into Windows Credential Manager via :func:`config.save_credentials`, then
delete the source so the bearer never lingers in a world-readable spot.

Lookup order, first hit wins:

  1. ``%USERPROFILE%\\Downloads\\akhort-config.json``
     The dashboard zip extracts here by default, so this is the common case.

  2. ``%LOCALAPPDATA%\\Akhort\\config.json``
     The long-term home a user (or our installer) may have moved it to.

  3. ``<frozen exe dir>/akhort-config.json``
     For PyInstaller-bundled distributions that ship a pre-paired config.

Returns True on a successful silent sign-in. False means no config was
found and the app should show its existing sign-in UI (currently: a tray
tooltip pointing at akhrots.com/app).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

from nuts_windows import config


def _candidate_paths() -> list[Path]:
    user = Path(os.environ.get("USERPROFILE", str(Path.home())))
    local_app = Path(os.environ.get("LOCALAPPDATA", str(user / "AppData" / "Local")))
    paths = [
        user / "Downloads" / "akhort-config.json",
        local_app / "Akhort" / "config.json",
    ]
    # Bundled-next-to-exe case for PyInstaller builds.
    if getattr(sys, "frozen", False):
        paths.append(Path(sys.executable).parent / "akhort-config.json")
    return paths


def try_auto_signin() -> bool:
    """Find an akhort-config.json, store its credentials, delete the source.

    Idempotent: if the user already has credentials in Credential Manager,
    we still pick up a fresh config (rotating their token) and overwrite.
    The deletion only runs after the credential write succeeds, so a power
    loss mid-way leaves the source file intact for next launch.
    """
    for path in _candidate_paths():
        if not path.is_file():
            continue
        cfg = _read_json(path)
        if not cfg:
            continue
        token = cfg.get("token")
        url = cfg.get("url") or config.DEFAULT_WORKER_URL
        if not token:
            continue
        try:
            config.save_credentials(url=url, token=token)
        except Exception:
            # Credential Manager write failed - leave source intact so we
            # try again on next launch instead of losing the bearer.
            continue
        # Don't delete a file we didn't put there - skip cleanup for the
        # frozen-exe-adjacent bundle case (which is read-only anyway).
        if getattr(sys, "frozen", False) and \
                path == Path(sys.executable).parent / "akhort-config.json":
            return True
        try:
            path.unlink()
        except OSError:
            # Couldn't delete (locked, perms) - not fatal; the credentials
            # are already stored. Worst case we re-read on next launch and
            # noop because the keychain already has them.
            pass
        return True
    return False


def _read_json(path: Path) -> Optional[dict]:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

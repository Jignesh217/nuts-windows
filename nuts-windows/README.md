# Nuts (Windows)

Windows port of [atharvalepse/nuts](https://github.com/atharvalepse/nuts) — itself a fork of
[farzaa/clicky](https://github.com/farzaa/clicky) — rebuilt from scratch in Python + PyQt6
because Swift only runs on macOS.

A system-tray AI companion that listens, looks at your screen, and speaks back.
Push-to-talk hotkey captures audio + a screenshot, sends them to your Cloudflare
Worker (which talks to your self-hosted vision model), streams the response, and
speaks it on-device.

## Status: starting scaffold

This is a working starting point, not a finished app. What works today:

- [x] System tray icon with menu (Settings / Quit)
- [x] Global push-to-talk hotkey (`Ctrl+Alt` by default)
- [x] Screenshot capture (multi-monitor, via `mss`)
- [x] Microphone capture (via `sounddevice`)
- [x] HTTP streaming client for the Cloudflare Worker
- [x] On-device text-to-speech (Windows SAPI via `pyttsx3`)
- [x] First-launch config reader (mirrors Mac `AkhortBootstrap.swift`)
- [x] Credential storage in Windows Credential Manager
- [ ] On-device speech recognition (Whisper) — stubbed; see `capture/audio.py` TODO
- [ ] Cursor overlay window with `[POINT:x,y]` parsing — stubbed; see `overlay/cursor.py` TODO
- [ ] Auto-update (Squirrel / WinSparkle)
- [ ] Code signing / installer (`.msi` or `.exe`)

The two `[ ]` items in the *core feature surface* are the next obvious work.
Everything else is polish.

## Requirements

- Windows 10 (1903+) or Windows 11
- Python 3.10+
- A microphone, a screen
- A live Cloudflare Worker URL (see [Configuration](#configuration))

## Install

```powershell
git clone https://github.com/atharvalepse/nuts-windows
cd nuts-windows
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run

```powershell
python -m nuts_windows
```

A system-tray icon appears. Hold **Ctrl+Alt** to push-to-talk. Release to send.

## Configuration

Two ways to configure:

**1. Drop in `akhort-config.json`** — automatic. If you signed in on
[akhrots.com/app](https://akhrots.com/app) and downloaded the install zip, it
already contains this file. Place it in any of:

- `%USERPROFILE%\Downloads\akhort-config.json` (where the dashboard zip extracts)
- `%LOCALAPPDATA%\Akhort\config.json` (preferred long-term home)

Nuts reads it on first launch, stores the bearer in Windows Credential Manager,
then deletes the source file.

**2. Set environment variables** — manual. Useful for development:

```powershell
$env:NUTS_WORKER_URL = "https://your-worker.workers.dev"
$env:NUTS_TOKEN      = "gml_..."
python -m nuts_windows
```

## Build a standalone `.exe`

```powershell
.\scripts\build.ps1
```

Output: `dist\Nuts.exe` (single-file executable, signed if a cert is configured).
Run on any Windows 10/11 box — no Python required on the target machine.

## License

MIT. Same chain as upstream: clicky → nuts → nuts-windows.

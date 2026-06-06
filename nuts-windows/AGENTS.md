# AGENTS.md

Notes for any agent (human or LLM) working on this repo.

## What this is

Windows port of [Nuts](https://github.com/atharvalepse/nuts) (a fork of
[Clicky](https://github.com/farzaa/clicky)) in Python + PyQt6, because
Swift only runs on macOS. The upstream Mac repo stays the source of
truth for product behavior; this repo mirrors it on Windows.

## Architecture, in one paragraph

A Qt event loop with a system-tray icon, a global push-to-talk hotkey
(pynput), audio recording (sounddevice), screen capture (mss), and
streaming HTTP to a Cloudflare Worker (httpx) that talks to the
self-hosted vision model. Model output is parsed inline for
`[POINT:x,y:label:screenN]` tags — those move the cursor (pyautogui) —
the rest is spoken via Windows SAPI (pyttsx3). Bootstrap reads an
`akhort-config.json` dropped by the dashboard install zip, stores the
bearer in Credential Manager, and deletes the source file.

## Codepaths to know

- `app.py`        - wires every module together; threading model lives here
- `tray.py`       - the menu the user sees
- `hotkey.py`     - push-to-talk; press/release callbacks
- `capture/`      - mic + screen
- `transport/`    - worker SSE client
- `tts/`          - SAPI wrapper with sentence-boundary buffering
- `overlay/`      - [POINT:...] parsing and cursor move; overlay window TODO
- `bootstrap.py`  - first-launch config reader
- `config.py`     - loads env -> keyring -> defaults; never returns garbage

## What's NOT in the scaffold (intentional)

- On-device Whisper. `capture/audio.py` ships the audio to the Worker as
  WAV; the Worker currently does STT. Add `faster-whisper` and run STT
  locally when ready (see TODO in that file). Targets parity with
  WhisperKit on Mac.
- Cursor overlay window. We move the cursor but don't draw a ring on
  the screen yet (see TODO in `overlay/cursor.py`).
- Auto-update. Sparkle has no Windows equivalent we want to take a hard
  dependency on yet. Squirrel is one path; WinSparkle is another. Pick
  before 1.0.
- Code signing wired into build.ps1 - signtool stub is there, supply a
  PFX via env vars to use it.

## How to test locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:NUTS_WORKER_URL = "https://your-worker.workers.dev"
$env:NUTS_TOKEN      = "gml_..."
python -m nuts_windows
```

Tray icon appears. Hold Ctrl+Alt, speak, release. Watch stdout for
worker stream chunks; listen for SAPI playback.

## Conventions

- All modules: type hints + dataclasses where they help.
- No silent fallbacks. If config is missing, we say so on the tray
  tooltip. If a stream drops, we swallow inside the async task but
  surface via tray notification (TODO).
- Threads are explicit. Anything that touches Qt does so from the main
  thread or via `QTimer.singleShot(0, ...)`.

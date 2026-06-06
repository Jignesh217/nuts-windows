# Build a standalone Windows .exe of Nuts (Windows).
#
# Output: dist\Nuts.exe (single-file, ~80-120 MB once PyQt6 is bundled).
# Run on a target Windows 10/11 machine without Python installed.
#
# Code signing: if AKHORT_PFX_PATH and AKHORT_PFX_PASS env vars are set,
# we sign the produced exe with signtool. Otherwise we ship unsigned and
# Windows SmartScreen will warn on first launch - the user can click
# "More info -> Run anyway". Fine for development; get a code-signing
# certificate for production.

$ErrorActionPreference = "Stop"

# 1. Activate venv if present, else assume we're already inside one.
if (Test-Path ".\.venv\Scripts\Activate.ps1") {
    . .\.venv\Scripts\Activate.ps1
}

# 2. Make sure build deps are present.
pip install --quiet --upgrade pyinstaller

# 3. Clean previous build artifacts so PyInstaller picks up source edits.
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

# 4. Build. Notes:
#    --windowed   = no console window on launch (tray-only app)
#    --onefile    = bundle into a single self-extracting exe
#    --collect-all = PyQt6's plugin dirs need to be packaged explicitly
pyinstaller `
    --name Nuts `
    --windowed `
    --onefile `
    --collect-all PyQt6 `
    --collect-all sounddevice `
    --collect-all soundfile `
    --collect-all mss `
    --hidden-import pyttsx3.drivers.sapi5 `
    src\nuts_windows\__main__.py

if (-not (Test-Path "dist\Nuts.exe")) {
    Write-Error "PyInstaller did not produce dist\Nuts.exe"
}

# 5. Optional signing.
if ($env:AKHORT_PFX_PATH -and $env:AKHORT_PFX_PASS) {
    Write-Host "signing dist\Nuts.exe ..."
    & signtool sign `
        /f $env:AKHORT_PFX_PATH `
        /p $env:AKHORT_PFX_PASS `
        /tr http://timestamp.digicert.com /td sha256 /fd sha256 `
        dist\Nuts.exe
} else {
    Write-Host "no AKHORT_PFX_PATH set - shipping unsigned. SmartScreen will warn on first launch."
}

Write-Host "done. dist\Nuts.exe is ready."

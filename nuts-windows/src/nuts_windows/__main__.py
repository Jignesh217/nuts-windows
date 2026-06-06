"""Entry point: ``python -m nuts_windows``.

Kept thin on purpose. The real wiring lives in :mod:`nuts_windows.app` so
PyInstaller / freeze tools can target the same module path the CLI does.
"""
from __future__ import annotations

import sys


def main() -> int:
    # Import lazily so a bad install (missing PyQt6 etc.) reports the right
    # ModuleNotFoundError instead of crashing before we can print a hint.
    try:
        from nuts_windows.app import run
    except ModuleNotFoundError as e:
        print(
            f"missing dependency: {e.name}\n"
            "run `pip install -r requirements.txt` inside the project venv.",
            file=sys.stderr,
        )
        return 2
    return run()


if __name__ == "__main__":
    raise SystemExit(main())

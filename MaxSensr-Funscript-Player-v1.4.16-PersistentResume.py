#!/usr/bin/env python3
"""Compatibility launcher for MaxSensr Funscript Player v1.4.16."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from maxsensr_player.main import main

if __name__ == "__main__":
    raise SystemExit(main())

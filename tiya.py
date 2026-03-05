#!/usr/bin/env python3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.app import main as app_main
from src.cli import entry_logs, entry_restart, entry_start, entry_status, entry_stop


def main() -> None:
    app_main()


if __name__ == "__main__":
    main()

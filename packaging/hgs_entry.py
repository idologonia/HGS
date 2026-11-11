"""PyInstaller entry point: one executable for the GUI, the core (--core) and game workers (--worker)."""

import sys

from hgs.__main__ import main

if __name__ == "__main__":
    sys.exit(main())

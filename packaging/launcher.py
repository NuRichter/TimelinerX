"""PyInstaller entry point (absolute import: the frozen entry script has no parent package)."""
import sys

from timelinerx.__main__ import main

if __name__ == "__main__":
    sys.exit(main())

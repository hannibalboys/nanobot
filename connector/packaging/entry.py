"""PyInstaller entry: double-click opens the GUI; CLI flags still work."""

from __future__ import annotations

import sys


def main() -> None:
    if len(sys.argv) == 1:
        from nanobot_connector.gui import run_gui

        run_gui()
        return
    from nanobot_connector.cli import app

    app()


if __name__ == "__main__":
    main()

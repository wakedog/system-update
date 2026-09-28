"""Entry point: no arguments opens the desktop app, anything else is the CLI.

Run as root without arguments, it updates the system the way system-update.sh
did, so it can take the script's place in timers and cron jobs.
"""

from __future__ import annotations

import logging
import os
import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.WARNING, format="system-update: %(message)s")
    if argv or os.geteuid() == 0:
        from system_update import cli

        return cli.main(argv)

    from system_update import gui

    return gui.run()

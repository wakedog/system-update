"""Filesystem locations used by System Update.

User locations are resolved at call time so tests can point ``HOME`` or the
XDG variables somewhere else. System-wide locations (logs, run history, the
lock file) belong to the helper and are defined in helper.py.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
INSTALL_ROOT = Path("/usr/share/system-update")
IS_INSTALLED = PACKAGE_DIR.parent == INSTALL_ROOT
SOURCE_ROOT = None if IS_INSTALLED else PACKAGE_DIR.parent

SYSTEM_HELPER = Path("/usr/libexec/system-update/system-update-helper")
PKEXEC = "/usr/bin/pkexec"


def helper_command() -> list[str]:
    """Return the command that runs the privileged helper (without pkexec).

    Installed copies use the root-owned helper that the polkit policy refers
    to. A source checkout runs its own copy so code and helper always match;
    polkit then shows its generic "run a program as administrator" prompt,
    even for checking for updates.
    """
    if IS_INSTALLED and SYSTEM_HELPER.exists():
        return [str(SYSTEM_HELPER)]
    return [sys.executable or "/usr/bin/python3", "-I", str(PACKAGE_DIR / "helper.py")]


def _xdg(variable: str, fallback: str) -> Path:
    value = os.environ.get(variable, "")
    return Path(value) if os.path.isabs(value) else Path.home() / fallback


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "system-update"


def dev_icon_dir() -> Path | None:
    """Icon theme directory of a source checkout, or None when installed."""
    return SOURCE_ROOT / "data" / "icons" if SOURCE_ROOT else None

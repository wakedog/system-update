"""User preferences, shared by the desktop app and the command line."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields

from system_update import paths

LIMITS = {
    "journal_keep_days": (1, 365),
    "journal_max_mb": (16, 100_000),
}
FLAGS = ("restart_services", "check_on_start")


@dataclass
class Settings:
    # Task id -> whether it is selected. Missing ids use the task's default.
    selection: dict[str, bool] = field(default_factory=dict)
    # Journal entries newer than this are always kept.
    journal_keep_days: int = 7
    # The journal is then trimmed to at most this size.
    journal_max_mb: int = 200
    # Let needrestart restart outdated services instead of only listing them.
    restart_services: bool = False
    # Refresh package information when the app opens, if it is over an hour old.
    check_on_start: bool = True

    @classmethod
    def load(cls) -> "Settings":
        try:
            with open(paths.config_dir() / "settings.json", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        settings = cls()
        for name, (low, high) in LIMITS.items():
            value = data.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                setattr(settings, name, min(max(value, low), high))
        for name in FLAGS:
            if isinstance(data.get(name), bool):
                setattr(settings, name, data[name])
        selection = data.get("selection")
        if isinstance(selection, dict):
            settings.selection = {
                str(key): value for key, value in selection.items() if isinstance(value, bool)
            }
        return settings

    def save(self) -> None:
        directory = paths.config_dir()
        directory.mkdir(parents=True, exist_ok=True)
        known = {f.name for f in fields(self)}
        data = {key: value for key, value in asdict(self).items() if key in known}
        fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".settings-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temp_path, directory / "settings.json")
        except BaseException:
            os.unlink(temp_path)
            raise

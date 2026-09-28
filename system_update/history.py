"""Past update runs, read from the history the helper keeps in /var/lib/system-update.

Every run is recorded there, whether it was started from the app, from the
command line, or by a timer running as root.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from system_update import helper

UPDATE_TASKS = set(helper.UPDATE_TASKS)


@dataclass
class TaskRecord:
    task: str
    state: str
    count: int | None = None
    freed: int | None = None
    message: str = ""


@dataclass
class Run:
    started: float
    finished: float
    user: str = "root"
    via: str = "root"  # "pkexec" (the app or command line), "sudo", or "root" (timers and cron)
    tasks: list[TaskRecord] = field(default_factory=list)
    reboot_required: bool = False
    log: str = ""
    stopped: bool = False

    @property
    def installed(self) -> int:
        """Updates installed across all sources."""
        return sum(record.count or 0 for record in self.tasks
                   if record.task in UPDATE_TASKS and record.state in (helper.DONE, helper.WARNING))

    @property
    def freed(self) -> int:
        return sum(record.freed or 0 for record in self.tasks)

    @property
    def problems(self) -> list[TaskRecord]:
        return [record for record in self.tasks if record.state == helper.FAILED]

    @property
    def unattended(self) -> bool:
        return self.via == "root"

    @classmethod
    def from_json(cls, data: dict) -> "Run | None":
        try:
            tasks = [
                TaskRecord(
                    task=str(item["task"]), state=str(item["state"]),
                    count=item.get("count") if isinstance(item.get("count"), int) else None,
                    freed=item.get("freed") if isinstance(item.get("freed"), int) else None,
                    message=str(item.get("message") or ""),
                )
                for item in data.get("tasks", []) if isinstance(item, dict)
            ]
            return cls(
                started=float(data["started"]), finished=float(data.get("finished") or data["started"]),
                user=str(data.get("user") or "root"), via=str(data.get("via") or "root"), tasks=tasks,
                reboot_required=bool(data.get("reboot_required")), log=str(data.get("log") or ""),
                stopped=bool(data.get("stopped")),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def to_json(self) -> dict:
        return {
            "started": self.started, "finished": self.finished, "user": self.user, "via": self.via,
            "reboot_required": self.reboot_required, "log": self.log, "stopped": self.stopped,
            "installed": self.installed, "freed": self.freed,
            "tasks": [record.__dict__ for record in self.tasks],
        }


def load() -> list[Run]:
    """Every recorded run, newest first. Damaged lines are skipped."""
    runs = []
    try:
        with open(helper.HISTORY_FILE, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if isinstance(data, dict) and (run := Run.from_json(data)):
                    runs.append(run)
    except OSError:
        return []
    runs.sort(key=lambda run: run.started, reverse=True)
    return runs

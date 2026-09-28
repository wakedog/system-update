"""Checking for and installing updates, shared by the command line and the desktop app."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from system_update import helper, paths, updates
from system_update.settings import Settings
from system_update.updates import MAINTENANCE, UPDATES, CheckResult
from system_update.util import plural

DONE, WARNING, FAILED, SKIPPED = helper.DONE, helper.WARNING, helper.FAILED, helper.SKIPPED
REFRESH_AFTER = 3600  # seconds: older package information is refreshed when the app opens

AUTH_MESSAGES = {
    126: "Authentication was cancelled",
    127: "Not authorized to update this computer",
}
COUNTED = {*helper.UPDATE_TASKS, updates.PERSONAL_FLATPAK.id}


@dataclass
class TaskOutcome:
    task: str
    state: str  # DONE, WARNING, FAILED or SKIPPED
    count: int | None = None
    freed: int | None = None
    message: str = ""
    items: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    started: float
    finished: float = 0.0
    outcomes: dict[str, TaskOutcome] = field(default_factory=dict)
    log_path: str | None = None
    reboot_required: bool = False
    reboot_packages: list[str] = field(default_factory=list)
    stopped: bool = False
    error: str = ""  # why the run could not start
    error_reason: str = ""  # "busy", "offline", "auth" or "helper"

    @property
    def installed(self) -> int:
        return sum(outcome.count or 0 for outcome in self.outcomes.values()
                   if outcome.task in COUNTED and outcome.state in (DONE, WARNING))

    @property
    def freed(self) -> int:
        return sum(outcome.freed or 0 for outcome in self.outcomes.values())

    @property
    def problems(self) -> list[TaskOutcome]:
        return [outcome for outcome in self.outcomes.values() if outcome.state == FAILED]

    def to_json(self) -> dict:
        return {
            "started": self.started, "finished": self.finished, "installed": self.installed,
            "freed": self.freed, "log": self.log_path, "reboot_required": self.reboot_required,
            "reboot_packages": self.reboot_packages, "stopped": self.stopped, "error": self.error or None,
            "tasks": [outcome.__dict__ for outcome in self.outcomes.values()],
        }


@dataclass
class RunEvent:
    # "authenticating", "log", "message", "waiting", "started", "progress", "output",
    # "finished-task", "stopping" or "finished"
    kind: str
    task: str | None = None
    text: str = ""
    level: str = ""
    fraction: float = 0.0
    outcome: TaskOutcome | None = None
    report: RunReport | None = None


@dataclass
class Plan:
    tasks: list[str]  # helper task ids
    journal_days: int = 7
    journal_max_mb: int = 200
    firmware: str = "install"  # "report" only lists available firmware updates
    restart_services: bool = False
    personal_flatpak: list[str] = field(default_factory=list)  # your own Flatpak apps to update
    clean_personal_flatpak: bool = False

    @property
    def steps(self) -> list[str]:
        """Every task in the order it runs."""
        steps = [task for task in helper.TASKS if task in self.tasks]
        return steps + [updates.PERSONAL_FLATPAK.id] if self.personal_flatpak else steps


# ─────────────────────────────── Checking ──────────────────────────────

def lists_are_stale(now: float | None = None) -> bool:
    updated = helper.lists_updated()
    return updated is None or (now if now is not None else time.time()) - updated > REFRESH_AFTER


def check(*, refresh_first: bool = False, firmware: bool = True,
          on_progress: Callable[[str], None] | None = None) -> CheckResult:
    """Look for updates, optionally refreshing package information first (see refresh())."""
    progress = on_progress or (lambda text: None)
    refresh_error = ""
    if refresh_first:
        progress("Refreshing package information…")
        refresh_error = refresh(firmware=firmware,
                                on_event=lambda event: event.kind == "progress" and progress(event.text))
    progress("Looking for updates…")
    result = updates.check(firmware=firmware)
    result.refresh_error = refresh_error
    return result


def selectable(result: CheckResult, task_id: str) -> bool:
    task = updates.BY_ID[task_id]
    if task.group == UPDATES:
        source = result.sources.get(task_id)
        return bool(source and source.available and (source.actionable or source.error))
    cleanup = result.cleanup.get(task_id)
    return cleanup is None or cleanup.available


def default_selection(settings: Settings, result: CheckResult) -> list[str]:
    """Sources with updates and the maintenance tasks, following the user's saved choices."""
    chosen = []
    for task in updates.TASKS:
        if not selectable(result, task.id) or not updates.is_selected(settings, task):
            continue
        source = result.sources.get(task.id)
        if task.group == MAINTENANCE or (source and source.actionable):
            chosen.append(task.id)
    return chosen


def plan_for(selected: list[str], settings: Settings, result: CheckResult | None) -> Plan:
    flatpak = result.sources.get("flatpak") if result else None
    personal = [update.name for update in flatpak.updates if update.personal] if flatpak and "flatpak" in selected else []
    return Plan(
        tasks=[task for task in helper.TASKS if task in selected],
        journal_days=settings.journal_keep_days, journal_max_mb=settings.journal_max_mb,
        restart_services=settings.restart_services, personal_flatpak=personal,
        clean_personal_flatpak=bool(personal) and "flatpak_unused" in selected,
    )


# ─────────────────────────────── Helper runs ───────────────────────────

def helper_argv(command: str, options: list[str]) -> list[str]:
    argv = [*paths.helper_command(), command, *options]
    return argv if os.geteuid() == 0 else [paths.PKEXEC, *argv]


def _decode(line: str) -> dict | None:
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) and isinstance(event.get("event"), str) else None


def _number(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def refresh(*, firmware: bool = True, on_event: Callable[[RunEvent], None] | None = None) -> str:
    """Refresh package information through the helper (no password on an installed desktop).

    Returns "" on success, otherwise a message saying what went wrong.
    """
    emit = on_event or (lambda event: None)
    argv = helper_argv("refresh", ["--firmware"] if firmware else [])
    if argv[0] == paths.PKEXEC and not os.path.exists(paths.PKEXEC):
        return "pkexec is not installed"
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)
    except OSError as error:
        return f"Could not start the system helper: {error.strerror}"
    error, stray = "", []
    with process:
        assert process.stdout is not None
        for line in process.stdout:
            event = _decode(line)
            if event is None:
                if line.strip():
                    stray.append(line.strip())
                continue
            kind = event["event"]
            if kind == "progress":
                emit(RunEvent("progress", "refresh", text=str(event.get("text") or ""),
                              fraction=float(event.get("fraction") or 0)))
            elif kind == "output":
                emit(RunEvent("output", "refresh", text=str(event.get("text") or "")))
            elif kind == "waiting":
                emit(RunEvent("waiting", "refresh", text=_waiting_text(event)))
            elif kind == "fatal":
                error = str(event.get("text") or "Package information could not be refreshed")
            elif kind == "done" and event.get("state") != DONE:
                error = str(event.get("message") or "Package information could not be refreshed")
    status = process.returncode
    if status in AUTH_MESSAGES:
        return AUTH_MESSAGES[status]
    if status != 0 and not error:
        error = stray[-1] if stray else f"The system helper stopped unexpectedly (exit status {status})"
    return error


def _waiting_text(event: dict) -> str:
    holders = [str(holder) for holder in event.get("holders") or []]
    return f"Waiting for {', '.join(holders) or 'another program'} to finish using the package system"


class _Relay(helper.Reporter):
    """Lets helper.Context run commands in this process, forwarding its events."""

    def __init__(self, emit: Callable[[RunEvent], None]):
        super().__init__(stream=None)
        self._forward = emit

    def emit(self, event: str, **data) -> None:
        task = data.get("task")
        if event == "output":
            self._forward(RunEvent("output", task, text=data.get("text", "")))
        elif event == "progress":
            self._forward(RunEvent("progress", task, text=data.get("text", ""), fraction=data.get("fraction", 0.0)))
        elif event == "message":
            self._forward(RunEvent("message", task, text=data.get("text", ""), level=data.get("level", "info")))


class Run:
    """One update run. execute() blocks until it is over; stop() may be called from any thread."""

    def __init__(self, plan: Plan, on_event: Callable[[RunEvent], None] | None = None):
        self.plan = plan
        self.report = RunReport(started=time.time())
        self._emit = on_event or (lambda event: None)
        self._process: subprocess.Popen | None = None
        self._stop = threading.Event()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def stop(self) -> None:
        """Finish the task that is running and skip the rest. apt and dpkg are never interrupted."""
        first = not self._stop.is_set()
        self._stop.set()
        process = self._process
        if process is not None and process.stdin is not None:
            try:
                process.stdin.write("stop\n")
                process.stdin.flush()
            except (OSError, ValueError):
                pass
        elif first:
            self._emit(RunEvent("stopping"))

    def execute(self) -> RunReport:
        report = self.report
        tasks = [task for task in helper.TASKS if task in self.plan.tasks]
        if tasks:
            self._run_helper(tasks)
        if self.plan.personal_flatpak:
            if report.error or self._stop.is_set():
                self._finish(TaskOutcome(updates.PERSONAL_FLATPAK.id, SKIPPED,
                                         message=report.error or "Skipped because you stopped the update"))
            else:
                self._run_personal_flatpak()
        report.stopped = report.stopped or self._stop.is_set()
        report.finished = time.time()
        self._emit(RunEvent("finished", report=report))
        return report

    def _finish(self, outcome: TaskOutcome) -> None:
        self.report.outcomes[outcome.task] = outcome
        self._emit(RunEvent("finished-task", outcome.task, outcome=outcome))

    def _run_helper(self, tasks: list[str]) -> None:
        plan, report = self.plan, self.report
        options = [f"--journal-days={plan.journal_days}", f"--journal-max-mb={plan.journal_max_mb}",
                   f"--firmware={plan.firmware}"]
        if plan.restart_services:
            options.append("--restart-services")
        argv = helper_argv("upgrade", [*options, *tasks])
        pending = list(tasks)

        def skip_pending(reason: str) -> None:
            for task in list(pending):
                pending.remove(task)
                self._finish(TaskOutcome(task, SKIPPED, message=reason))

        if argv[0] == paths.PKEXEC and not os.path.exists(paths.PKEXEC):
            report.error, report.error_reason = "pkexec is not installed, so updates cannot be installed", "helper"
            skip_pending(report.error)
            return
        if argv[0] == paths.PKEXEC:
            self._emit(RunEvent("authenticating"))
        try:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)
        except OSError as error:
            report.error, report.error_reason = f"Could not start the system helper: {error.strerror}", "helper"
            skip_pending(report.error)
            return
        self._process = process
        if self._stop.is_set():  # asked to stop while the helper was starting
            self.stop()
        stray: list[str] = []
        with process:
            assert process.stdout is not None
            for line in process.stdout:
                event = _decode(line)
                if event is None:
                    if text := line.strip():
                        stray.append(text)
                        self._emit(RunEvent("output", text=text))
                    continue
                self._handle(event, pending)
        self._process = None
        status = process.returncode
        if pending:
            if not report.error:
                if status in AUTH_MESSAGES:
                    report.error, report.error_reason = AUTH_MESSAGES[status], "auth"
                else:
                    report.error = stray[-1] if stray else f"The system helper stopped unexpectedly (exit status {status})"
                    report.error_reason = "helper"
            skip_pending(report.error)

    def _handle(self, event: dict, pending: list[str]) -> None:
        report, kind = self.report, event["event"]
        task = event.get("task") if isinstance(event.get("task"), str) else None
        text = str(event.get("text") or "")
        if kind == "fatal":
            report.error = text or "The update could not start"
            report.error_reason = str(event.get("reason") or "helper")
        elif kind == "log":
            report.log_path = str(event.get("path") or "") or None
            self._emit(RunEvent("log", text=report.log_path or ""))
        elif kind == "message":
            level = event.get("level") if event.get("level") in helper.LOG_LEVELS else "info"
            self._emit(RunEvent("message", task, text=text, level=level))
        elif kind == "waiting":
            self._emit(RunEvent("waiting", task, text=_waiting_text(event)))
        elif kind == "start" and task in pending:
            self._emit(RunEvent("started", task))
        elif kind == "progress" and task:
            fraction = event.get("fraction")
            fraction = min(max(float(fraction), 0.0), 1.0) if isinstance(fraction, (int, float)) else 0.0
            self._emit(RunEvent("progress", task, text=text, fraction=fraction))
        elif kind == "output":
            self._emit(RunEvent("output", task, text=text))
        elif kind == "done" and task in pending:
            pending.remove(task)
            state = event.get("state") if event.get("state") in (DONE, WARNING, FAILED, SKIPPED) else FAILED
            self._finish(TaskOutcome(
                task, state, count=_number(event.get("count")), freed=_number(event.get("freed")),
                message=str(event.get("message") or ""),
                items=[str(item) for item in event.get("items") or []][:1000],
            ))
        elif kind == "stopping":
            self._emit(RunEvent("stopping"))
        elif kind == "finished":
            report.reboot_required = bool(event.get("reboot_required"))
            report.reboot_packages = [str(package) for package in event.get("reboot_packages") or []]
            report.stopped = bool(event.get("stopped"))

    def _run_personal_flatpak(self) -> None:
        task = updates.PERSONAL_FLATPAK.id
        names = self.plan.personal_flatpak
        self._emit(RunEvent("started", task))
        context = helper.Context(argparse.Namespace(), _Relay(self._emit), helper.command_env())
        try:
            context.run([helper.FLATPAK, "update", "--user", "-y", "--noninteractive"], task,
                        timeout=helper.HELPER_TIMEOUT)
            if self.plan.clean_personal_flatpak:
                context.run([helper.FLATPAK, "uninstall", "--unused", "--user", "-y", "--noninteractive"], task,
                            timeout=600, check=False)
        except helper.TaskError as error:
            self._finish(TaskOutcome(task, FAILED, message=str(error)))
            return
        self._finish(TaskOutcome(task, DONE, count=len(names), items=list(names),
                                 message=f"Updated {plural(len(names), 'app')}"))

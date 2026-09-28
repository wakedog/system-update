"""Command-line interface: `system-update check`, `system-update upgrade`, …

The options of system-update.sh keep working: `system-update --yes` and the
other script options run `upgrade`, so the app can replace the script in
timers and cron jobs.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import shutil
import sys
import threading
import time

from system_update import APP_NAME, __version__, engine, helper, history, updates
from system_update.settings import LIMITS, Settings
from system_update.updates import BY_ID, CheckResult
from system_update.util import format_ago, format_duration, format_size, plural

COMMANDS = ("check", "upgrade", "history")
LEGACY_OPTIONS = ("--dry-run", "--yes", "-y", "--no-firmware", "--verbose", "-v")
EXIT_UPDATES_AVAILABLE = 100


class Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _paint(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled and text else text

    def bold(self, text: str) -> str:
        return self._paint("1", text)

    def dim(self, text: str) -> str:
        return self._paint("2", text)

    def red(self, text: str) -> str:
        return self._paint("31", text)

    def green(self, text: str) -> str:
        return self._paint("32", text)

    def yellow(self, text: str) -> str:
        return self._paint("33", text)

    def cyan(self, text: str) -> str:
        return self._paint("36", text)


def colors_wanted(stream, disabled: bool) -> bool:
    return (not disabled and stream.isatty() and "NO_COLOR" not in os.environ
            and os.environ.get("TERM") != "dumb")


class Spinner:
    """One animated status line on stderr; println() writes above it."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, style: Style, enabled: bool):
        self.stream = sys.stderr
        self.style = style
        self.enabled = enabled
        self._text = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def show(self, text: str) -> None:
        with self._lock:
            self._text = text
        if self.enabled and self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._animate, daemon=True)
            self._thread.start()

    def _animate(self) -> None:
        width = shutil.get_terminal_size((80, 24)).columns
        for frame in itertools.cycle(self.FRAMES):
            with self._lock:
                text = self._text if len(self._text) < width - 6 else self._text[: width - 7] + "…"
                self.stream.write(f"\r\033[K  {self.style.cyan(frame)} {text}")
                self.stream.flush()
            if self._stop.wait(0.08):
                return

    def stop(self) -> None:
        if self._thread is not None:
            self._stop.set()
            self._thread.join()
            self._thread = None
            with self._lock:
                self.stream.write("\r\033[K")
                self.stream.flush()

    def println(self, text: str, stream=None) -> None:
        with self._lock:
            if self._thread is not None:
                self.stream.write("\r\033[K")
                self.stream.flush()
            print(text, file=stream or sys.stdout, flush=True)


# ─────────────────────────────── Arguments ─────────────────────────────

def bounded(name: str):
    low, high = LIMITS[name]

    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            value = low - 1
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be a whole number from {low} to {high}")
        return value

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="system-update", allow_abbrev=False,
        description="Keep Ubuntu up to date: system packages, snaps, Flatpak apps and firmware, "
                    "then clean up. Run without arguments to open the desktop app.",
        epilog="The options of system-update.sh still work: `system-update --yes` is the same as "
               "`system-update upgrade --yes`.\nSee system-update(1) for details.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print machine-readable JSON")
    common.add_argument("--no-color", action="store_true", help="never use colors")

    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    check = commands.add_parser(
        "check", parents=[common], allow_abbrev=False, help="list available updates; changes nothing",
        description="List the updates that are available. Nothing is changed. Exits with status 100 "
                    "when there are updates, 0 when everything is up to date.")
    check.add_argument("--refresh", action="store_true",
                       help="refresh package information first (as the app does when it opens)")
    check.add_argument("--no-firmware", action="store_true", help="do not check for firmware updates")

    upgrade = commands.add_parser(
        "upgrade", parents=[common], allow_abbrev=False, help="install all updates, then clean up",
        description="Refresh package information, install every available update, then clean up. "
                    "Nothing ever stops to ask a question, so it is safe to run from a timer.")
    upgrade.add_argument("-y", "--yes", action="store_true",
                         help="fully unattended: also install firmware updates and restart outdated services")
    upgrade.add_argument("-n", "--dry-run", action="store_true", help="only show what would be done")
    upgrade.add_argument("-v", "--verbose", action="store_true", help="show the output of every command")
    group = upgrade.add_argument_group("choosing what to do")
    group.add_argument("--no-firmware", action="store_true", help="skip firmware checks and updates entirely")
    group.add_argument("--no-snap", action="store_true", help="do not update snaps")
    group.add_argument("--no-flatpak", action="store_true", help="do not update Flatpak apps")
    group.add_argument("--no-cleanup", action="store_true",
                       help="skip removing unused packages, caches, old snap revisions and old journal entries")
    group.add_argument("--journal-days", type=bounded("journal_keep_days"), metavar="DAYS",
                       help="keep this many days of system journal (default: from preferences, 7)")
    group.add_argument("--journal-max-mb", type=bounded("journal_max_mb"), metavar="MB",
                       help="trim the system journal to this size (default: from preferences, 200)")

    commands.add_parser("history", parents=[common], allow_abbrev=False, help="list past update runs")
    return parser


def normalize(argv: list[str]) -> list[str]:
    """system-update.sh took options without a command; those mean `upgrade`."""
    if not argv:
        return ["upgrade"]  # only reached as root: the script updated when run without options
    if argv[0] in COMMANDS or argv[0] in ("-h", "--help", "-V", "--version"):
        return argv
    if argv[0].startswith("-") and all(arg in LEGACY_OPTIONS or arg.startswith("--") for arg in argv):
        return ["upgrade", *argv]
    return argv


# ──────────────────────────────── Output ───────────────────────────────

def version_text(update: updates.Update) -> str:
    if update.kind == "remove":
        return f"{update.old_version} (will be removed)".strip()
    if update.kind == "install":
        return f"{update.new_version} (new)"
    return f"{update.old_version} → {update.new_version}" if update.old_version else update.new_version


def check_lines(result: CheckResult, style: Style, limit: int = 25) -> list[str]:
    sources = [source for source in result.sources.values() if source.available]
    width = max((len(BY_ID[source.id].title) for source in sources), default=10) + 3
    lines = []
    for source in sources:
        title = BY_ID[source.id].title
        summary = source.summary()
        painted = style.red(summary) if source.error else style.dim(summary) if not source.actionable else summary
        lines.append(f"  {style.bold(title.ljust(width))}{painted}")
        if not source.updates:
            continue
        name_width = min(32, max(len(update.name) for update in source.updates) + 2)
        for update in source.updates[:limit]:
            tag = style.yellow("  security") if update.security else ""
            lines.append(f"    {update.name.ljust(name_width)}{style.dim(version_text(update))}{tag}")
        if len(source.updates) > limit:
            lines.append(style.dim(f"    … and {len(source.updates) - limit} more (see --json)"))
    return lines


def status_lines(result: CheckResult, style: Style) -> list[str]:
    lines = []
    if result.refresh_error:
        lines.append(style.yellow(f"  ⚠ Package information could not be refreshed: {result.refresh_error}"))
    if result.running:
        lines.append(style.yellow(f"  ⚠ An update is running right now: {result.running}"))
    elif result.busy:
        lines.append(style.dim(f"  Another program is using the package system: {', '.join(result.busy)}"))
    if result.reboot_required:
        packages = f" ({', '.join(result.reboot_packages[:4])})" if result.reboot_packages else ""
        lines.append(style.yellow(f"  ⚠ Restart required to finish installing earlier updates{packages}"))
    return lines


def cmd_check(args, style: Style, spinner: Spinner) -> int:
    try:
        spinner.show("Checking for updates…")
        result = engine.check(refresh_first=args.refresh, firmware=not args.no_firmware, on_progress=spinner.show)
    finally:
        spinner.stop()
    repair = any(source.repair for source in result.sources.values())
    errors = [source for source in result.sources.values() if source.available and source.error]
    status = EXIT_UPDATES_AVAILABLE if result.total or repair else (1 if errors else 0)
    if args.json:
        print(json.dumps({"version": __version__, **result.to_json()}, indent=2))
        return status
    print("\n".join(check_lines(result, style)))
    print()
    for line in status_lines(result, style):
        print(line)
    age = f" Package information from {format_ago(result.lists_updated)}." if result.lists_updated else ""
    if result.total:
        security = f" ({result.security} security)" if result.security else ""
        print(f"  {style.bold(plural(result.total, 'update'))} available{security}.{style.dim(age)}")
        print(style.dim("  Install them with: system-update upgrade\n"))
    elif errors:
        print(style.red(f"  Some sources could not be checked.{age}\n"))
    else:
        print(style.green(f"  ✔ Everything is up to date.{style.dim(age)}\n"))
    return status


def cli_plan(args, settings: Settings) -> engine.Plan:
    """Everything available, like system-update.sh did, minus what the options leave out."""
    tasks = [task.id for task in updates.TASKS if task.available()]
    skip = set()
    if args.no_firmware:
        skip.add("firmware")
    if args.no_snap:
        skip |= {"snap", "snap_revisions"}
    if args.no_flatpak:
        skip |= {"flatpak", "flatpak_unused"}
    if args.no_cleanup:
        skip |= set(helper.CLEANUP_TASKS)
    tasks = [task for task in tasks if task not in skip]
    personal = []
    if "flatpak" in tasks and os.geteuid() != 0:
        try:
            personal = [update.name for update in helper.flatpak_updates("user")]
        except helper.HelperError:
            personal = []
    return engine.Plan(
        tasks=tasks,
        journal_days=args.journal_days or settings.journal_keep_days,
        journal_max_mb=args.journal_max_mb or settings.journal_max_mb,
        firmware="install" if args.yes else "report",
        restart_services=args.yes or settings.restart_services,
        personal_flatpak=personal,
        clean_personal_flatpak=bool(personal) and "flatpak_unused" in tasks,
    )


def outcome_line(outcome: engine.TaskOutcome, style: Style) -> str:
    title = BY_ID[outcome.task].title
    detail = outcome.message
    if outcome.freed:
        detail = f"{detail}, {format_size(outcome.freed)} freed" if detail else f"{format_size(outcome.freed)} freed"
    if outcome.state == engine.DONE:
        return f"  {style.green('✔')} {title:<28} {detail or 'Done'}"
    if outcome.state == engine.WARNING:
        return f"  {style.yellow('⚠')} {title:<28} {detail}"
    if outcome.state == engine.SKIPPED:
        return f"  {style.dim('–')} {title:<28} {style.dim(detail or 'Skipped')}"
    return f"  {style.red('✖')} {title:<28} {style.red(detail or 'Failed')}"


def cmd_upgrade(args, style: Style, spinner: Spinner) -> int:
    settings = Settings.load()
    plan = cli_plan(args, settings)
    if args.dry_run:
        return dry_run(args, plan, style, spinner)

    current: dict[str, str] = {}

    def on_event(event: engine.RunEvent) -> None:
        if args.json:
            return
        if event.kind == "authenticating":
            # No spinner here: a terminal password prompt may be sharing the screen.
            print(style.dim("  Waiting for administrator authentication…"), file=sys.stderr, flush=True)
        elif event.kind == "log":
            spinner.println(style.dim(f"  Log: {event.text}\n"))
        elif event.kind == "started":
            current["title"] = BY_ID[event.task].title
            spinner.show(f"{current['title']}…")
        elif event.kind == "progress" and current.get("title"):
            spinner.show(f"{current['title']}…  {style.dim(event.text)}")
        elif event.kind == "waiting":
            spinner.show(event.text + "…")
        elif event.kind == "message" and event.level in ("warning", "error"):
            mark = style.yellow("⚠") if event.level == "warning" else style.red("✖")
            spinner.println(f"    {mark} {event.text}", stream=sys.stderr)
        elif event.kind in ("output", "message") and args.verbose:
            spinner.println(style.dim(f"      {event.text}"))
        elif event.kind == "stopping":
            spinner.println(style.yellow("  Stopping after the current step…"), stream=sys.stderr)
        elif event.kind == "finished-task":
            current.pop("title", None)
            if not run.report.error:  # a run that could not start is reported once, below
                spinner.println(outcome_line(event.outcome, style))

    run = engine.Run(plan, on_event)
    worker = threading.Thread(target=run.execute, name="update")
    worker.start()
    interrupts = 0
    while worker.is_alive():
        try:
            worker.join(0.2)
        except KeyboardInterrupt:
            interrupts += 1
            if interrupts == 1:
                run.stop()
                spinner.println(style.yellow("  Stopping after the current step. Press Ctrl+C again to leave it "
                                             "running in the background."), stream=sys.stderr)
            else:
                spinner.stop()
                print("\n  Left running in the background; see the log for the result.", file=sys.stderr)
                return 130
    spinner.stop()
    report = run.report

    if args.json:
        print(json.dumps({"version": __version__, **report.to_json()}, indent=2))
    else:
        if report.error:
            print(style.red(f"  ✖ {report.error}"), file=sys.stderr)
        if any(outcome.state != engine.SKIPPED for outcome in report.outcomes.values()):
            print_summary(report, style)
        else:
            print()
    if report.error_reason in ("busy", "offline", "auth") or (report.error and not report.problems):
        return 3
    return 1 if report.problems else 0


def print_summary(report: engine.RunReport, style: Style) -> None:
    print(style.dim("\n  " + "─" * 60))
    elapsed = format_duration(report.finished - report.started)
    parts = [plural(report.installed, "update") + " installed"]
    if report.freed:
        parts.append(f"{format_size(report.freed)} freed")
    if report.problems:
        headline = style.yellow(f"Finished with problems in {elapsed}")
    elif report.stopped:
        headline = style.yellow(f"Stopped after {elapsed}")
    else:
        headline = style.green(f"Done in {elapsed}")
    print(f"  {style.bold(headline)}: {', '.join(parts)}.")
    if report.reboot_required:
        packages = f" ({', '.join(report.reboot_packages[:4])})" if report.reboot_packages else ""
        print(style.yellow(f"  ⚠ Restart the computer to finish installing updates{packages}."))
    if report.log_path:
        print(style.dim(f"  Log: {report.log_path}"))
    print()


def dry_run(args, plan: engine.Plan, style: Style, spinner: Spinner) -> int:
    try:
        spinner.show("Checking for updates…")
        result = engine.check(firmware="firmware" in plan.tasks, on_progress=spinner.show)
    finally:
        spinner.stop()
    if args.json:
        print(json.dumps({"version": __version__, "dry_run": True, "tasks": plan.steps, "firmware": plan.firmware,
                          "check": result.to_json()}, indent=2))
        return 0
    print("\n".join(check_lines(result, style)))
    print()
    for line in status_lines(result, style):
        print(line)
    print(f"  {style.bold('Would run')}:")
    for task in plan.steps:
        note = ""
        if task == "firmware" and plan.firmware == "report":
            note = style.dim(" (list only; --yes installs firmware)")
        print(f"    · {BY_ID[task].title}{note}")
    print(style.cyan("\n  Dry run: nothing was changed. Run without --dry-run to update.\n"))
    return 0


def cmd_history(args, style: Style) -> int:
    runs = history.load()
    if args.json:
        print(json.dumps([run.to_json() for run in runs], indent=2))
        return 0
    if not runs:
        print("\n  No updates have been run with System Update yet.\n")
        return 0
    print()
    for run in runs[:50]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(run.started))
        what = plural(run.installed, "update") if run.installed else "Up to date"
        if run.freed:
            what += f", {format_size(run.freed)} freed"
        who = "timer or root" if run.unattended else run.user
        flags = []
        if run.problems:
            flags.append(style.red(plural(len(run.problems), "problem")))
        if run.stopped:
            flags.append(style.yellow("stopped"))
        if run.reboot_required:
            flags.append(style.yellow("restart needed"))
        print(f"  {when}  {what:<34} {style.dim(who):<12}  {'  '.join(flags)}".rstrip())
    print()
    return 0


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(normalize(argv))
    if not args.command:
        parser.print_help()
        return 2
    style = Style(colors_wanted(sys.stdout, args.no_color))
    spinner = Spinner(Style(colors_wanted(sys.stderr, args.no_color)),
                      enabled=sys.stderr.isatty() and not args.json)
    if not args.json and args.command in ("check", "upgrade"):
        print(f"\n  {style.bold(APP_NAME)} {style.dim(__version__)}\n")
    try:
        if args.command == "check":
            return cmd_check(args, style, spinner)
        if args.command == "upgrade":
            return cmd_upgrade(args, style, spinner)
        return cmd_history(args, style)
    except KeyboardInterrupt:
        spinner.stop()
        print("\n  Interrupted.", file=sys.stderr)
        return 130

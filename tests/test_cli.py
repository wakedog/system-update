import contextlib
import io
import json
import re
import unittest
from pathlib import Path
from unittest import mock

from system_update import cli, engine, helper
from system_update.updates import CheckResult, SourceStatus, Update
from tests.support import FakeSystemTestCase

ROOT = Path(__file__).resolve().parent.parent


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            status = cli.main(list(argv))
        except SystemExit as exit:
            status = exit.code
    return status, out.getvalue(), err.getvalue()


def with_updates() -> CheckResult:
    return CheckResult(sources={
        "apt": SourceStatus("apt", updates=[Update("libfoo", "1.0", "1.1", security=True)], download_size=0),
        "snap": SourceStatus("snap"),
        "flatpak": SourceStatus("flatpak", available=False),
    }, lists_updated=1.0)


class FakeRun:
    """Stands in for engine.Run, replaying a finished report."""

    report_state = engine.DONE
    error = ("", "")

    def __init__(self, plan, on_event):
        self.plan, self.on_event = plan, on_event
        self.report = engine.RunReport(started=0.0, finished=75.0)

    def stop(self):
        pass

    def execute(self):
        self.report.error, self.report.error_reason = self.error
        for task in self.plan.steps:
            outcome = engine.TaskOutcome(task, self.report_state, count=2 if task == "apt" else None,
                                         message="Updated 2 packages" if task == "apt" else "")
            self.report.outcomes[task] = outcome
            self.on_event(engine.RunEvent("finished-task", task, outcome=outcome))
        self.on_event(engine.RunEvent("finished", report=self.report))
        return self.report


class ArgumentTests(unittest.TestCase):
    def test_script_options_mean_upgrade(self):
        self.assertEqual(cli.normalize(["--yes"]), ["upgrade", "--yes"])
        self.assertEqual(cli.normalize(["--dry-run", "--no-firmware", "-v"]),
                         ["upgrade", "--dry-run", "--no-firmware", "-v"])
        self.assertEqual(cli.normalize([]), ["upgrade"])
        self.assertEqual(cli.normalize(["check", "--json"]), ["check", "--json"])
        self.assertEqual(cli.normalize(["--help"]), ["--help"])

    def test_unknown_options_are_rejected(self):
        status, _, err = run("--frobnicate")
        self.assertEqual(status, 2)
        self.assertIn("unrecognized arguments", err)

    def test_plan_follows_the_options(self):
        parser = cli.build_parser()
        with mock.patch.object(helper, "snap_available", return_value=True), \
                mock.patch.object(helper, "flatpak_available", return_value=False), \
                mock.patch.object(helper, "fwupd_available", return_value=True):
            plan = cli.cli_plan(parser.parse_args(["upgrade"]), cli.Settings())
            self.assertEqual(plan.tasks, ["apt", "snap", "firmware", "autoremove", "apt_cache", "snap_revisions",
                                          "journal"])
            self.assertEqual((plan.firmware, plan.restart_services), ("report", False))
            plan = cli.cli_plan(parser.parse_args(["upgrade", "--no-snap", "--no-firmware"]), cli.Settings())
            self.assertEqual(plan.tasks, ["apt", "autoremove", "apt_cache", "journal"])
            plan = cli.cli_plan(parser.parse_args(["upgrade", "--yes", "--no-cleanup", "--journal-days=3"]),
                                cli.Settings())
        self.assertEqual((plan.firmware, plan.restart_services, plan.journal_days), ("install", True, 3))
        self.assertFalse(set(plan.tasks) & set(helper.CLEANUP_TASKS))


class CheckCommandTests(unittest.TestCase):
    def test_json_and_exit_status(self):
        with mock.patch.object(engine, "check", return_value=with_updates()):
            status, out, _ = run("check", "--json")
        self.assertEqual(status, cli.EXIT_UPDATES_AVAILABLE)
        data = json.loads(out)
        self.assertEqual((data["total"], data["security"]), (1, 1))
        self.assertEqual(data["sources"][0]["updates"][0]["name"], "libfoo")

    def test_text(self):
        with mock.patch.object(engine, "check", return_value=with_updates()):
            status, out, _ = run("check", "--no-color")
        self.assertIn("libfoo", out)
        self.assertIn("1.0 → 1.1", out)
        self.assertIn("1 update available (1 security)", out)
        self.assertNotIn("\033[", out)

    def test_up_to_date(self):
        result = CheckResult(sources={"apt": SourceStatus("apt")}, lists_updated=1.0)
        with mock.patch.object(engine, "check", return_value=result):
            status, out, _ = run("check", "--no-color")
        self.assertEqual(status, 0)
        self.assertIn("Everything is up to date", out)

    def test_dry_run_changes_nothing(self):
        with mock.patch.object(engine, "check", return_value=with_updates()), \
                mock.patch.object(engine, "Run", side_effect=AssertionError("must not run")):
            status, out, _ = run("--dry-run", "--no-color")
        self.assertEqual(status, 0)
        self.assertIn("Dry run: nothing was changed", out)
        self.assertIn("System Packages", out)


class UpgradeCommandTests(unittest.TestCase):
    def upgrade(self, *argv, state=engine.DONE, error=("", "")):
        fake = type("Fake", (FakeRun,), {"report_state": state, "error": error})
        with mock.patch.object(engine, "Run", fake), mock.patch.object(helper, "flatpak_available", return_value=False):
            return run("upgrade", "--no-color", *argv)

    def test_success(self):
        status, out, _ = self.upgrade()
        self.assertEqual(status, 0)
        self.assertIn("✔ System Packages", out)
        self.assertIn("Done in 1 minute 15 seconds: 2 updates installed.", out)

    def test_failures(self):
        status, out, _ = self.upgrade(state=engine.FAILED)
        self.assertEqual(status, 1)
        self.assertIn("Finished with problems", out)

    def test_could_not_start(self):
        status, _, err = self.upgrade(state=engine.SKIPPED, error=("Another update is already running", "busy"))
        self.assertEqual(status, 3)
        self.assertIn("Another update is already running", err)

    def test_json(self):
        status, out, _ = self.upgrade("--json")
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out)["installed"], 2)


class HistoryCommandTests(FakeSystemTestCase):
    def test_empty_and_filled(self):
        status, out, _ = run("history")
        self.assertEqual(status, 0)
        self.assertIn("No updates have been run", out)
        helper.append_history({"started": 1_790_000_000, "finished": 1_790_000_100, "user": "root", "via": "root",
                               "tasks": [{"task": "apt", "state": "done", "count": 4},
                                         {"task": "apt_cache", "state": "done", "freed": 5_000_000}],
                               "reboot_required": True})
        status, out, _ = run("history", "--no-color")
        self.assertIn("4 updates, 5.0 MB freed", out)
        self.assertIn("timer or root", out)
        self.assertIn("restart needed", out)
        status, out, _ = run("history", "--json")
        self.assertEqual(json.loads(out)[0]["installed"], 4)


class CompletionTests(unittest.TestCase):
    def test_bash_completion_knows_every_command_and_option(self):
        text = (ROOT / "data" / "system-update.bash-completion").read_text()
        commands = re.search(r'local commands="([^"]+)"', text).group(1).split()
        self.assertEqual(commands, list(cli.COMMANDS))
        parser = cli.build_parser()
        subparsers = next(action for action in parser._actions if action.dest == "command")
        for name, subparser in subparsers.choices.items():
            for action in subparser._actions:
                for option in action.option_strings:
                    if option.startswith("--"):
                        with self.subTest(command=name, option=option):
                            self.assertIn(option, text)


if __name__ == "__main__":
    unittest.main()

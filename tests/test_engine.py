import json
import sys
import threading
import time
import unittest
from unittest import mock

from system_update import engine, helper, paths
from system_update.settings import Settings
from system_update.updates import CheckResult, CleanupStatus, SourceStatus, Update
from tests.support import FakeSystemTestCase

# Replays the events in the scenario file named by its first argument.
FAKE_HELPER = """
    import json, sys
    scenario = json.load(open(sys.argv[1]))
    for event in scenario.get("events", []):
        if event == "wait-for-stop":
            for line in sys.stdin:
                if line.strip() == "stop":
                    break
            print(json.dumps({"event": "stopping"}), flush=True)
            continue
        print(json.dumps(event), flush=True)
    if scenario.get("stray"):
        print(scenario["stray"], flush=True)
    sys.exit(scenario.get("exit", 0))
"""

APT_DONE = {"event": "done", "task": "apt", "state": "done", "count": 3, "freed": None,
            "message": "Updated 3 packages", "items": ["a", "b", "c"]}


class RunTests(FakeSystemTestCase):
    def setUp(self):
        super().setUp()
        self.fake = self.write_script("fake-helper", FAKE_HELPER)
        self.scenario = self.home / "scenario.json"
        self.argv = []
        self.patch(engine, "helper_argv", self.fake_argv)

    def fake_argv(self, command, options):
        self.argv = [command, *options]
        return [sys.executable, self.fake, str(self.scenario), command, *options]

    def run_plan(self, scenario: dict, plan: engine.Plan, *, during=None):
        self.scenario.write_text(json.dumps(scenario))
        seen = []

        def on_event(event):
            seen.append(event)
            if during and event.kind == "started":
                during(run)

        run = engine.Run(plan, on_event)
        report = run.execute()
        return report, seen

    def test_successful_run(self):
        scenario = {"events": [
            {"event": "log", "path": "/var/log/system-update/update-1.log"},
            {"event": "start", "task": "apt"},
            {"event": "progress", "task": "apt", "fraction": 0.5, "text": "Unpacking libfoo"},
            {"event": "output", "task": "apt", "text": "Setting up libfoo"},
            {"event": "message", "level": "warning", "text": "Low disk space"},
            APT_DONE,
            {"event": "start", "task": "journal"},
            {"event": "done", "task": "journal", "state": "done", "count": None, "freed": 4096, "message": "",
             "items": []},
            {"event": "finished", "reboot_required": True, "reboot_packages": ["linux-image"], "problems": 0,
             "stopped": False},
        ], "stray": "a line that is not JSON"}
        report, seen = self.run_plan(scenario, engine.Plan(tasks=["journal", "apt"], journal_days=3,
                                                           restart_services=True))
        self.assertEqual(self.argv, ["upgrade", "--journal-days=3", "--journal-max-mb=200", "--firmware=install",
                                     "--restart-services", "apt", "journal"])
        self.assertEqual(list(report.outcomes), ["apt", "journal"])
        self.assertEqual((report.installed, report.freed, report.problems), (3, 4096, []))
        self.assertEqual((report.reboot_required, report.reboot_packages), (True, ["linux-image"]))
        self.assertEqual(report.log_path, "/var/log/system-update/update-1.log")
        kinds = [event.kind for event in seen]
        self.assertEqual(kinds[0], "log")
        self.assertEqual(kinds[-1], "finished")
        progress = next(event for event in seen if event.kind == "progress")
        self.assertEqual((progress.task, progress.fraction, progress.text), ("apt", 0.5, "Unpacking libfoo"))
        self.assertIn(("warning", "Low disk space"), [(e.level, e.text) for e in seen if e.kind == "message"])
        self.assertIn("a line that is not JSON", [event.text for event in seen if event.kind == "output"])

    def test_cancelled_authentication_skips_everything(self):
        report, seen = self.run_plan({"exit": 126}, engine.Plan(tasks=["apt", "journal"]))
        self.assertEqual((report.error, report.error_reason), ("Authentication was cancelled", "auth"))
        self.assertEqual({outcome.state for outcome in report.outcomes.values()}, {engine.SKIPPED})
        self.assertEqual(len([event for event in seen if event.kind == "finished-task"]), 2)

    def test_helper_crash_is_reported(self):
        scenario = {"events": [{"event": "start", "task": "apt"}], "stray": "RuntimeError: boom", "exit": 1}
        report, _ = self.run_plan(scenario, engine.Plan(tasks=["apt"]))
        self.assertEqual((report.error, report.error_reason), ("RuntimeError: boom", "helper"))
        self.assertEqual(report.outcomes["apt"].state, engine.SKIPPED)

    def test_busy(self):
        scenario = {"events": [{"event": "fatal", "reason": "busy",
                                "text": "Another update is already running: system-update.sh (process 12)"}],
                    "exit": 3}
        report, _ = self.run_plan(scenario, engine.Plan(tasks=["apt"]))
        self.assertEqual(report.error_reason, "busy")
        self.assertIn("system-update.sh", report.outcomes["apt"].message)

    def test_stop_is_passed_to_the_helper(self):
        scenario = {"events": [
            {"event": "start", "task": "apt"}, "wait-for-stop", APT_DONE,
            {"event": "done", "task": "journal", "state": "skipped", "message": "Skipped because you stopped"},
            {"event": "finished", "stopped": True},
        ]}
        report, seen = self.run_plan(scenario, engine.Plan(tasks=["apt", "journal"]), during=lambda run: run.stop())
        self.assertTrue(report.stopped)
        self.assertEqual([outcome.state for outcome in report.outcomes.values()], [engine.DONE, engine.SKIPPED])
        self.assertIn("stopping", [event.kind for event in seen])

    def test_personal_flatpak_runs_in_this_process(self):
        flatpak = self.write_script("flatpak", """
            import sys
            print("Updating", " ".join(sys.argv[1:]), flush=True)
        """)
        self.patch(helper, "FLATPAK", flatpak)
        report, seen = self.run_plan({}, engine.Plan(tasks=[], personal_flatpak=["Foo"],
                                                     clean_personal_flatpak=True))
        outcome = report.outcomes["flatpak_user"]
        self.assertEqual((outcome.state, outcome.count), (engine.DONE, 1))
        output = [event.text for event in seen if event.kind == "output"]
        self.assertIn("Updating update --user -y --noninteractive", output)
        self.assertIn("Updating uninstall --unused --user -y --noninteractive", output)
        self.assertEqual(self.argv, [])  # the privileged helper was not needed

    def test_refresh(self):
        self.scenario.write_text(json.dumps({"events": [
            {"event": "progress", "task": "refresh", "fraction": 0.4, "text": "Refreshing package lists (40%)"},
            {"event": "done", "task": "refresh", "state": "done"},
        ]}))
        texts = []
        self.assertEqual(engine.refresh(firmware=False, on_event=lambda event: texts.append(event.text)), "")
        self.assertEqual(self.argv, ["refresh"])
        self.assertEqual(texts, ["Refreshing package lists (40%)"])
        self.scenario.write_text(json.dumps({"exit": 127}))
        self.assertEqual(engine.refresh(), "Not authorized to update this computer")
        self.assertEqual(self.argv, ["refresh", "--firmware"])
        self.scenario.write_text(json.dumps({"events": [
            {"event": "done", "task": "refresh", "state": "failed", "message": "apt-get exited with status 100"}],
            "exit": 1}))
        self.assertEqual(engine.refresh(), "apt-get exited with status 100")


class HelperCommandTests(unittest.TestCase):
    def test_pkexec_unless_root(self):
        with mock.patch("os.geteuid", return_value=1000):
            argv = engine.helper_argv("upgrade", ["apt"])
        self.assertEqual(argv[0], paths.PKEXEC)
        self.assertEqual(argv[-2:], ["upgrade", "apt"])
        with mock.patch("os.geteuid", return_value=0):
            self.assertNotEqual(engine.helper_argv("refresh", [])[0], paths.PKEXEC)


class SelectionTests(unittest.TestCase):
    def result(self) -> CheckResult:
        return CheckResult(
            sources={
                "apt": SourceStatus("apt", updates=[Update("libfoo", security=True)]),
                "snap": SourceStatus("snap"),
                "flatpak": SourceStatus("flatpak", updates=[Update("Foo", personal=True), Update("Bar")]),
                "firmware": SourceStatus("firmware", updates=[Update("System Firmware")]),
            },
            cleanup={
                "autoremove": CleanupStatus("autoremove"), "apt_cache": CleanupStatus("apt_cache"),
                "snap_revisions": CleanupStatus("snap_revisions", available=False),
                "flatpak_unused": CleanupStatus("flatpak_unused"), "journal": CleanupStatus("journal"),
            },
        )

    def test_defaults(self):
        self.assertEqual(engine.default_selection(Settings(), self.result()),
                         ["apt", "flatpak", "autoremove", "apt_cache", "flatpak_unused", "journal"])

    def test_saved_choices_win(self):
        settings = Settings(selection={"firmware": True, "apt": False, "journal": False})
        self.assertEqual(engine.default_selection(settings, self.result()),
                         ["flatpak", "firmware", "autoremove", "apt_cache", "flatpak_unused"])

    def test_sources_without_updates_cannot_be_selected(self):
        result = self.result()
        self.assertFalse(engine.selectable(result, "snap"))
        result.sources["snap"].error = "snapd is not responding"
        self.assertTrue(engine.selectable(result, "snap"))
        self.assertFalse(engine.selectable(result, "snap_revisions"))

    def test_plan(self):
        plan = engine.plan_for(["flatpak_unused", "apt", "flatpak"], Settings(journal_keep_days=3), self.result())
        self.assertEqual(plan.tasks, ["apt", "flatpak", "flatpak_unused"])
        self.assertEqual((plan.personal_flatpak, plan.clean_personal_flatpak, plan.journal_days), (["Foo"], True, 3))
        self.assertEqual(plan.steps, ["apt", "flatpak", "flatpak_unused", "flatpak_user"])
        self.assertEqual(engine.plan_for(["apt"], Settings(), self.result()).personal_flatpak, [])

    def test_lists_are_stale(self):
        with mock.patch.object(helper, "lists_updated", return_value=time.time() - 60):
            self.assertFalse(engine.lists_are_stale())
        with mock.patch.object(helper, "lists_updated", return_value=time.time() - 7200):
            self.assertTrue(engine.lists_are_stale())
        with mock.patch.object(helper, "lists_updated", return_value=None):
            self.assertTrue(engine.lists_are_stale())


class ConcurrencyTests(FakeSystemTestCase):
    def test_stop_before_the_helper_starts(self):
        fake = self.write_script("fake-helper", FAKE_HELPER)
        scenario = self.home / "scenario.json"
        scenario.write_text(json.dumps({"events": ["wait-for-stop", {"event": "finished", "stopped": True}]}))
        self.patch(engine, "helper_argv", lambda command, options: [sys.executable, fake, str(scenario)])
        run = engine.Run(engine.Plan(tasks=["apt"]))
        run.stop()
        worker = threading.Thread(target=run.execute)
        worker.start()
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertTrue(run.report.stopped)


if __name__ == "__main__":
    unittest.main()

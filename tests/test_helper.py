import argparse
import fcntl
import io
import json
import os
import signal
import time
import unittest
from pathlib import Path
from unittest import mock

from system_update import helper
from tests.support import FakeSystemTestCase, events

SIMULATION = """\
Inst libfreerdp3-3 [3.31.0+dfsg-0ubuntu0.26.04.1] (3.32.0+dfsg-0ubuntu0.26.04.1 Ubuntu:26.04/resolute-updates, \
Ubuntu:26.04/resolute-security [amd64]) []
Inst linux-image-6.17.0-9-generic (6.17.0-9.9 Ubuntu:26.04/resolute-updates [amd64])
Inst libc6:i386 [2.39-0ubuntu8] (2.39-0ubuntu8.1 Ubuntu:24.04/noble-updates [i386])
Inst local-tool [1.0] (1.1 [all])
Inst debsec [1.0] (1.1 Debian-Security:12/stable-security [amd64])
Conf libfreerdp3-3 (3.32.0+dfsg-0ubuntu0.26.04.1 Ubuntu:26.04/resolute-updates [amd64])
Remv old-package [0.9]
"""

PROC_LOCKS = """\
1: POSIX  ADVISORY  WRITE 1234 08:02:1311 0 EOF
1: -> POSIX  ADVISORY  WRITE 1240 08:02:1311 0 EOF
2: FLOCK  ADVISORY  WRITE 5678 00:1a:99 0 EOF
3: OFDLCK ADVISORY  READ  -1 103:02:77 0 EOF
garbage
"""

FWUPD_JSON = json.dumps({"Devices": [
    {"Name": "System Firmware", "Version": "1.2",
     "Releases": [{"Version": "1.3", "Summary": "Fixes a sleep issue", "Size": 1234}, {"Version": "1.25"}]},
    {"Name": "No releases", "Version": "4", "Releases": []},
    "junk",
]})


class AptParserTests(unittest.TestCase):
    def test_simulation(self):
        changes = {change.name: change for change in helper.parse_simulation(SIMULATION)}
        self.assertEqual(list(changes), ["libfreerdp3-3", "linux-image-6.17.0-9-generic", "libc6:i386",
                                         "local-tool", "debsec", "old-package"])
        freerdp = changes["libfreerdp3-3"]
        self.assertEqual((freerdp.kind, freerdp.old_version, freerdp.new_version),
                         ("upgrade", "3.31.0+dfsg-0ubuntu0.26.04.1", "3.32.0+dfsg-0ubuntu0.26.04.1"))
        self.assertTrue(freerdp.security)
        self.assertEqual(freerdp.archive, "resolute-security")
        kernel = changes["linux-image-6.17.0-9-generic"]
        self.assertEqual((kernel.kind, kernel.old_version, kernel.security), ("install", "", False))
        self.assertEqual(changes["libc6:i386"].archive, "noble-updates")
        self.assertEqual(changes["local-tool"].archive, "")
        self.assertTrue(changes["debsec"].security)
        self.assertEqual((changes["old-package"].kind, changes["old-package"].old_version), ("remove", "0.9"))

    def test_autoremove_reads_purge_lines(self):
        self.assertEqual(helper.parse_autoremove("Purg libfoo1 [1.0]\nRemv libbar [2]\nsomething else\n"),
                         ["libfoo1", "libbar"])

    def test_print_uris(self):
        text = ("'http://archive.ubuntu.com/ubuntu/pool/main/a/apt/apt_2.7.14_amd64.deb' apt_2.7.14_amd64.deb "
                "1390000 SHA256:abc\n'http://x/y.deb' y.deb 610 SHA512:def\nnoise\n")
        self.assertEqual(helper.parse_print_uris(text), 1_390_610)

    def test_status_lines(self):
        self.assertEqual(helper.parse_status_line("dlstatus:3:45.2:Retrieving file 3 of 12"),
                         ("dlstatus", "3", 45.2, "Retrieving file 3 of 12"))
        self.assertEqual(helper.parse_status_line("pmstatus:libc6:i386:20.0000:Unpacking libc6:i386 (amd64)"),
                         ("pmstatus", "libc6:i386", 20.0, "Unpacking libc6:i386 (amd64)"))
        self.assertEqual(helper.parse_status_line("pmerror:libfoo:80:subprocess failed")[0], "pmerror")
        self.assertIsNone(helper.parse_status_line("Setting up libfoo (1.1) ..."))

    def test_installed_sizes(self):
        sizes = helper.parse_installed_sizes("libfoo1\tamd64\t100\nlibfoo1\ti386\t90\nbar\tall\t5\nbad\n", "amd64")
        self.assertEqual(sizes["libfoo1"], 100 * 1024)
        self.assertEqual(sizes["libfoo1:i386"], 90 * 1024)
        self.assertEqual(sizes["bar"], 5 * 1024)

    def test_needs_repair(self):
        self.assertTrue(helper.needs_repair(
            "dpkg was interrupted, you must manually run 'dpkg --configure -a' to correct the problem."))
        self.assertFalse(helper.needs_repair("Unable to locate package"))
        self.assertFalse(helper.needs_repair(None))


class SnapAndFlatpakParserTests(unittest.TestCase):
    def test_snap_refreshes(self):
        found = [
            {"name": "firefox", "version": "132.0", "revision": "5200", "download-size": 290_000_000,
             "publisher": {"display-name": "Mozilla", "username": "mozilla"}},
            {"name": "bad name!", "version": "1"},
            "junk",
        ]
        updates = helper.parse_snap_refreshes(found, [{"name": "firefox", "version": "131.0"}])
        self.assertEqual(updates, [helper.SnapUpdate("firefox", "132.0", "5200", "131.0", 290_000_000, "Mozilla")])
        self.assertEqual(helper.parse_snap_refreshes(None, []), [])

    def test_disabled_revisions_are_validated(self):
        snaps = [
            {"name": "core22", "revision": "1122", "status": "installed"},
            {"name": "core22", "revision": "1380", "status": "active"},
            {"name": "evil;rm", "revision": "12", "status": "installed"},
            {"name": "local", "revision": "x3", "status": "installed"},
            {"name": "weird", "revision": "../1", "status": "installed"},
        ]
        self.assertEqual(helper.parse_disabled_revisions(snaps), [("core22", "1122"), ("local", "x3")])

    def test_summarize_change(self):
        change = {"summary": "Refresh snaps", "tasks": [
            {"status": "Done", "summary": "Ensure prerequisites"},
            {"status": "Doing", "summary": 'Download snap "firefox"', "progress": {"done": 50, "total": 100}},
            {"status": "Do", "summary": "Mount snap"},
        ]}
        fraction, text = helper.summarize_change(change)
        self.assertAlmostEqual(fraction, 0.5)
        self.assertEqual(text, 'Download snap "firefox" (50%)')
        self.assertEqual(helper.summarize_change({"summary": "Nothing", "ready": True}), (1.0, "Nothing"))

    def test_flatpak_columns(self):
        text = "Firefox\torg.mozilla.firefox\tstable\t132.0\n\torg.gnome.Platform\t46\t\nbroken line\n\n"
        self.assertEqual(helper.parse_columns(text, 4), [["Firefox", "org.mozilla.firefox", "stable", "132.0"],
                                                          ["", "org.gnome.Platform", "46", ""]])

    def test_flatpak_updates_pairs_old_versions(self):
        outputs = {
            "list": "org.mozilla.firefox\tstable\t131.0\norg.gnome.Platform\t46\t\n",
            "remote-ls": "Firefox\torg.mozilla.firefox\tstable\t132.0\n\torg.gnome.Platform\t46\t\n",
        }
        with mock.patch.object(helper, "_query", lambda argv, timeout=300: outputs[argv[1]]):
            updates = helper.flatpak_updates("user")
        self.assertEqual([(u.name, u.old_version, u.version, u.installation) for u in updates],
                         [("Firefox", "131.0", "132.0", "user"), ("org.gnome.Platform", "", "", "user")])


class FirmwareParserTests(unittest.TestCase):
    def test_newest_release_of_each_device(self):
        self.assertEqual(helper.parse_fwupd_updates(FWUPD_JSON), [
            helper.FirmwareUpdate("System Firmware", "1.2", "1.3", "Fixes a sleep issue", 1234)])
        self.assertEqual(helper.parse_fwupd_updates("not json"), [])
        self.assertEqual(helper.parse_fwupd_updates('{"Devices": []}'), [])


class LockTests(unittest.TestCase):
    def test_proc_locks(self):
        self.assertEqual(helper.parse_proc_locks(PROC_LOCKS),
                         [(1234, "08:02", 1311), (5678, "00:1a", 99), (-1, "103:02", 77)])

    def test_locked_by_matches_device_and_inode(self):
        st = os.stat(__file__)
        device = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}"
        locks = [(42, device, st.st_ino), (43, "fe:02", st.st_ino), (44, device, st.st_ino + 1)]
        self.assertEqual(helper.locked_by(__file__, locks), [42])

    def test_locked_by_tolerates_btrfs_devices(self):
        fake = os.stat_result((0o100644, 77, os.makedev(0, 45), 1, 0, 0, 0, 0, 0, 0))
        with mock.patch("os.stat", return_value=fake):
            self.assertEqual(helper.locked_by("/var/lib/dpkg/lock", [(9, "00:2c", 77), (10, "08:02", 77)]), [9])

    def test_process_name(self):
        self.assertTrue(helper.process_name(os.getpid()))
        self.assertEqual(helper.process_name(999_999_999), "unknown program")


class SystemStateTests(FakeSystemTestCase):
    def test_reboot_status(self):
        self.assertEqual(helper.reboot_status(), (False, []))
        (self.root / "run/reboot-required").write_text("*** System restart required ***\n")
        (self.root / "run/reboot-required.pkgs").write_text("linux-image\nlinux-image\nlibc6\n")
        self.assertEqual(helper.reboot_status(), (True, ["libc6", "linux-image"]))

    def test_apt_source_hosts(self):
        sources = self.root / "sources.list"
        sources.write_text("deb http://us.archive.ubuntu.com/ubuntu noble main\n"
                           "# deb http://commented.example.com/ubuntu noble main\n")
        directory = self.root / "sources.list.d"
        directory.mkdir()
        (directory / "x.sources").write_text("Types: deb\nURIs: https://ppa.launchpadcontent.net/foo/ubuntu/\n")
        (directory / "y.list").write_text("deb [arch=amd64] http://mirror.example.com:8080/debian stable main\n")
        (directory / "z.save").write_text("deb http://ignored.example.com/ubuntu noble main\n")
        self.patch(helper, "APT_SOURCES", str(sources))
        self.patch(helper, "APT_SOURCES_DIR", str(directory))
        self.assertEqual(helper.apt_source_hosts(), [("us.archive.ubuntu.com", 80),
                                                     ("ppa.launchpadcontent.net", 443),
                                                     ("mirror.example.com", 8080)])

    def test_prune_logs_keeps_the_newest(self):
        directory = self.root / "logs"
        directory.mkdir()
        for day in range(10, 22):
            (directory / f"update-202609{day}-030000.log").write_text("x")
        (directory / "notes.txt").write_text("keep me")
        helper.prune_logs(str(directory), keep=5)
        self.assertEqual(sorted(path.name for path in directory.iterdir()),
                         sorted([f"update-202609{day}-030000.log" for day in range(17, 22)] + ["notes.txt"]))

    def test_history_is_trimmed(self):
        self.patch(helper, "HISTORY_LIMIT", 3)
        for number in range(60):
            helper.append_history({"started": number})
        lines = Path(helper.HISTORY_FILE).read_text().splitlines()
        self.assertLess(len(lines), 54)
        self.assertEqual(json.loads(lines[-1]), {"started": 59})
        self.assertEqual(os.stat(helper.HISTORY_FILE).st_mode & 0o777, 0o644)


class ArgumentTests(unittest.TestCase):
    def test_accepts_known_tasks_and_limits(self):
        options = helper.parse_args(["upgrade", "--journal-days=3", "--journal-max-mb", "500",
                                     "--firmware=report", "journal", "apt"])
        self.assertEqual(options.tasks, ["journal", "apt"])
        self.assertEqual((options.journal_days, options.journal_max_mb, options.firmware), (3, 500, "report"))
        self.assertTrue(helper.parse_args(["refresh", "--firmware"]).firmware)

    def test_rejects_anything_else(self):
        for argv in (
            ["upgrade"],
            ["upgrade", "bogus"],
            ["upgrade", "--journal-days=0", "journal"],
            ["upgrade", "--journal-days", "7; rm -rf /", "journal"],
            ["upgrade", "--journal-max-mb=8", "journal"],
            ["upgrade", "--journal-d=3", "journal"],
            ["upgrade", "--firmware=flash", "firmware"],
            ["upgrade", "--restart-services=yes", "apt"],
            ["refresh", "apt"],
            ["run", "/bin/sh"],
            [],
        ):
            with self.subTest(argv=argv), mock.patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    helper.parse_args(argv)

    def test_refuses_to_run_unprivileged(self):
        with mock.patch("os.geteuid", return_value=1000), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(helper.main(["upgrade", "apt"]), 2)


class RunCommandTests(FakeSystemTestCase):
    def context(self) -> helper.Context:
        self.report = self.reporter()
        return helper.Context(argparse.Namespace(), self.report, helper.command_env())

    def test_streams_output_and_apt_status(self):
        command = self.write_script("fake-apt", """
            import os, sys
            fd = int(sys.argv[-1].split("=")[1])
            print("Reading package lists...", flush=True)
            os.write(fd, b"pmstatus:libfoo:i386:75:Unpacking libfoo:i386\\n")
            os.write(fd, b"pmerror:libbar:80:subprocess installed post-installation script returned error\\n")
            sys.stdout.write("progress 1\\rprogress 2\\n")
            sys.exit(0 if "ok" in sys.argv else 3)
        """)
        ctx = self.context()
        seen = []
        self.assertEqual(ctx.run([command, "ok"], "apt", on_status=seen.append), 0)
        self.assertEqual(seen, ["pmstatus:libfoo:i386:75:Unpacking libfoo:i386",
                                "pmerror:libbar:80:subprocess installed post-installation script returned error"])
        output = [event["text"] for event in events(self.report) if event["event"] == "output"]
        self.assertEqual(output[1:], ["Reading package lists...", "progress 1", "progress 2"])
        self.assertRegex(output[0], r"ok -o APT::Status-Fd=\d+$")
        with self.assertRaisesRegex(helper.TaskError, "exited with status 3"):
            ctx.run([command], "apt", on_status=seen.append)
        self.assertEqual(ctx.run([command], "apt", on_status=seen.append, check=False), 3)

    def test_apt_progress_maps_phases(self):
        ctx = self.context()
        progress = helper.apt_progress(ctx, "apt", download=(0.1, 0.4), install=(0.5, 0.5))
        progress("dlstatus:1:50:Retrieving file 1 of 2")
        ctx.report._last_progress = 0
        progress("pmstatus:libfoo:50:Unpacking libfoo (amd64)")
        progress("pmerror:libbar:80:it broke")
        progress("junk")
        emitted = events(self.report)
        self.assertEqual([(e["fraction"], e["text"]) for e in emitted if e["event"] == "progress"],
                         [(0.3, "Downloading (50%)"), (0.75, "Unpacking libfoo (amd64)")])
        self.assertIn({"event": "message", "level": "error", "text": "libbar: it broke"}, emitted)

    def test_timeout_stops_the_command(self):
        command = self.write_script("slow", "import time\ntime.sleep(30)\n")
        started = time.monotonic()
        with self.assertRaisesRegex(helper.TaskError, "did not finish within 1 second"):
            self.context().run([command], "snap", timeout=1)
        self.assertLess(time.monotonic() - started, 10)

    def test_a_daemon_keeping_the_pipe_open_does_not_hang(self):
        command = self.write_script("starts-daemon", """
            import subprocess
            child = subprocess.Popen(["sleep", "30"])
            print(child.pid, flush=True)
        """)
        started = time.monotonic()
        ctx = self.context()
        self.assertEqual(ctx.run([command], "apt"), 0)
        self.assertLess(time.monotonic() - started, 10)
        pid = int([e["text"] for e in events(self.report) if e["event"] == "output"][1])
        os.kill(pid, signal.SIGTERM)


class WaitForAptTests(FakeSystemTestCase):
    def setUp(self):
        super().setUp()
        self.report = self.reporter()
        self.ctx = helper.Context(argparse.Namespace(), self.report, {})
        self.holder = helper.LockHolder(4242, "unattended-upgrade")

    def test_waits_until_the_lock_is_free(self):
        answers = [[self.holder], [self.holder], []]
        with mock.patch.object(helper, "apt_lock_holders", side_effect=answers), mock.patch("time.sleep"):
            helper.wait_for_apt(self.ctx, "apt")
        emitted = events(self.report)
        self.assertEqual([e["holders"] for e in emitted if e["event"] == "waiting"],
                         [["unattended-upgrade (process 4242)"]])
        self.assertIn("released after 10s", emitted[-1]["text"])

    def test_gives_up_and_blocks_later_apt_tasks(self):
        with mock.patch.object(helper, "apt_lock_holders", return_value=[self.holder]), mock.patch("time.sleep"):
            with self.assertRaisesRegex(helper.TaskError, "unattended-upgrade"):
                helper.wait_for_apt(self.ctx, "apt", timeout=10)
        with mock.patch.object(helper, "apt_lock_holders") as holders:
            with self.assertRaises(helper.TaskError):
                helper.wait_for_apt(self.ctx, "autoremove")
            holders.assert_not_called()

    def test_stop_ends_the_wait(self):
        helper.STOP.set()
        with mock.patch.object(helper, "apt_lock_holders", return_value=[self.holder]), mock.patch("time.sleep"):
            with self.assertRaises(helper.Stopped):
                helper.wait_for_apt(self.ctx, "apt")


FAKE_APT = """
    import os, sys
    args = sys.argv[1:]
    status = [int(a.split("=")[1]) for a in args if a.startswith("APT::Status-Fd=")]
    here = os.path.dirname(sys.argv[0])
    if "--simulate" in args:
        if os.path.exists(os.path.join(here, "up-to-date")):
            sys.exit(0)
        print("Inst libfoo [1.0] (1.1 Ubuntu:24.04/noble-security [amd64])")
        print("Inst linux-image-new (6.0 Ubuntu:24.04/noble-updates [amd64])")
    elif "update" in args:
        os.write(status[0], b"dlstatus:1:100:Retrieving file 1 of 1\\n")
        print("Hit:1 http://archive.ubuntu.com/ubuntu noble InRelease")
        sys.exit(100 if os.path.exists(os.path.join(here, "fail-update")) else 0)
    elif "full-upgrade" in args:
        os.write(status[0], b"pmstatus:libfoo:50:Unpacking libfoo (amd64)\\n")
        print("Setting up libfoo (1.1) ...")
        with open(os.path.join(here, "upgraded"), "w") as marker:
            marker.write(" ".join(args))
"""


class AptTaskTests(FakeSystemTestCase):
    def setUp(self):
        super().setUp()
        self.patch(helper, "APT_GET", self.write_script("apt-get", FAKE_APT))
        self.patch(helper, "DPKG", self.write_script("dpkg", "import sys\nsys.exit(0)\n"))
        self.patch(helper, "apt_lock_holders", lambda: [])
        self.report = self.reporter()
        self.ctx = helper.Context(helper.parse_args(["upgrade", "apt"]), self.report, helper.command_env())

    def test_upgrades_with_safe_options(self):
        result = helper.task_apt(self.ctx)
        self.assertEqual((result.count, result.warning), (2, False))
        self.assertEqual(result.message, "Updated 1 package, installed 1 new")
        self.assertEqual(result.items, ["libfoo", "linux-image-new"])
        options = (self.home / "bin/upgraded").read_text()
        self.assertIn("Dpkg::Options::=--force-confold", options)
        self.assertIn("-y", options.split())
        self.assertTrue(any(e["event"] == "progress" for e in events(self.report)))

    def test_failed_refresh_still_upgrades_but_warns(self):
        (self.home / "bin/fail-update").write_text("")
        result = helper.task_apt(self.ctx)
        self.assertTrue(result.warning)
        self.assertIn("some package sources could not be refreshed", result.message)
        self.assertTrue((self.home / "bin/upgraded").exists())

    def test_nothing_to_do(self):
        (self.home / "bin/up-to-date").write_text("")
        result = helper.task_apt(self.ctx)
        self.assertEqual((result.count, result.message), (0, "Already up to date"))
        self.assertFalse((self.home / "bin/upgraded").exists())


class UpgradeCommandTests(FakeSystemTestCase):
    def setUp(self):
        super().setUp()
        self.patch(helper, "network_available", lambda: True)

    def run_upgrade(self, *tasks, functions=None):
        report = self.reporter()
        with mock.patch.dict(helper.TASK_FUNCTIONS, functions or {}):
            status = helper.cmd_upgrade(helper.parse_args(["upgrade", *tasks]), report)
        return status, events(report)

    def history(self) -> list[dict]:
        return [json.loads(line) for line in Path(helper.HISTORY_FILE).read_text().splitlines()]

    def test_runs_tasks_in_order_logs_and_records_history(self):
        def broken(ctx):
            raise helper.TaskError("snapd broke")

        status, emitted = self.run_upgrade("journal", "snap", "apt", functions={
            "apt": lambda ctx: helper.TaskResult(count=3, message="Updated 3 packages", items=["a", "b", "c"]),
            "snap": broken,
            "journal": lambda ctx: helper.TaskResult(freed=4096),
        })
        self.assertEqual(status, 1)
        done = [(e["task"], e["state"]) for e in emitted if e["event"] == "done"]
        self.assertEqual(done, [("apt", "done"), ("snap", "failed"), ("journal", "done")])
        finished = emitted[-1]
        self.assertEqual((finished["event"], finished["problems"], finished["stopped"]), ("finished", 1, False))
        log_path = next(e["path"] for e in emitted if e["event"] == "log")
        text = Path(log_path).read_text()
        self.assertIn("System Update Started", text)
        self.assertIn("[FAIL] Snap: Refresh: failed (snapd broke)", text)
        self.assertIn("1 task(s) failed", text)
        self.assertRegex(Path(log_path).name, r"^update-\d{8}-\d{6}\.log$")
        record = self.history()[0]
        self.assertEqual([task["state"] for task in record["tasks"]], ["done", "failed", "done"])
        self.assertEqual(record["log"], log_path)

    def test_stop_skips_what_has_not_started(self):
        def apt(ctx):
            helper.STOP.set()
            return helper.TaskResult(count=1)

        status, emitted = self.run_upgrade("apt", "journal", functions={
            "apt": apt, "journal": lambda ctx: self.fail("journal should not run")})
        self.assertEqual(status, 0)
        self.assertEqual([(e["task"], e["state"]) for e in emitted if e["event"] == "done"],
                         [("apt", "done"), ("journal", "skipped")])
        self.assertTrue(self.history()[0]["stopped"])

    def test_refuses_to_run_twice(self):
        fd = os.open(helper.LOCK_FILE, os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        status, emitted = self.run_upgrade("apt")
        self.assertEqual(status, 3)
        self.assertEqual((emitted[0]["event"], emitted[0]["reason"]), ("fatal", "busy"))
        self.assertFalse(os.path.exists(helper.HISTORY_FILE))

    def test_offline_aborts_before_changing_anything(self):
        self.patch(helper, "network_available", lambda: False)
        status, emitted = self.run_upgrade("apt", functions={"apt": lambda ctx: self.fail("apt should not run")})
        self.assertEqual(status, 3)
        self.assertEqual(emitted[-1]["reason"], "offline")

    def test_maintenance_alone_does_not_need_the_network(self):
        self.patch(helper, "network_available", lambda: self.fail("no network check expected"))
        status, _ = self.run_upgrade("journal", functions={"journal": lambda ctx: helper.TaskResult(freed=0)})
        self.assertEqual(status, 0)


if __name__ == "__main__":
    unittest.main()

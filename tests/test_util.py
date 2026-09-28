import json
import unittest

from system_update import helper, history, paths
from system_update.settings import Settings
from system_update.util import format_ago, format_duration, format_size, plural
from tests.support import FakeSystemTestCase, TempHomeTestCase


class FormatTests(unittest.TestCase):
    def test_format_size_matches_glib(self):
        self.assertEqual(format_size(None), "Unknown")
        self.assertEqual(format_size(1), "1 byte")
        self.assertEqual(format_size(999), "999 bytes")
        self.assertEqual(format_size(1000), "1.0 kB")
        self.assertEqual(format_size(1_339_392), "1.3 MB")
        self.assertEqual(format_size(68_263_936_000), "68.3 GB")

    def test_plural(self):
        self.assertEqual(plural(1, "update"), "1 update")
        self.assertEqual(plural(1234, "update"), "1,234 updates")
        self.assertEqual(plural(2, "category", "categories"), "2 categories")

    def test_format_duration(self):
        self.assertEqual(format_duration(0.4), "0 seconds")
        self.assertEqual(format_duration(45), "45 seconds")
        self.assertEqual(format_duration(75), "1 minute 15 seconds")
        self.assertEqual(format_duration(600), "10 minutes")
        self.assertEqual(format_duration(725), "12 minutes")
        self.assertEqual(format_duration(3900), "1 hour 5 minutes")

    def test_format_ago(self):
        now = 1_000_000.0
        self.assertEqual(format_ago(now - 10, now), "just now")
        self.assertEqual(format_ago(now - 300, now), "5 minutes ago")
        self.assertEqual(format_ago(now - 3 * 3600, now), "3 hours ago")
        self.assertEqual(format_ago(now - 30 * 3600, now), "yesterday")
        self.assertEqual(format_ago(now - 5 * 86400, now), "5 days ago")


class SettingsTests(TempHomeTestCase):
    def test_round_trip_and_clamping(self):
        settings = Settings(journal_keep_days=3, restart_services=True, selection={"firmware": True})
        settings.save()
        self.assertEqual(Settings.load(), settings)

        (paths.config_dir() / "settings.json").write_text(json.dumps({
            "journal_keep_days": 9999, "journal_max_mb": "x", "restart_services": 1,
            "check_on_start": False, "selection": {"a": 1, "b": True},
        }))
        loaded = Settings.load()
        self.assertEqual(loaded.journal_keep_days, 365)
        self.assertEqual(loaded.journal_max_mb, 200)
        self.assertFalse(loaded.restart_services)
        self.assertFalse(loaded.check_on_start)
        self.assertEqual(loaded.selection, {"b": True})

    def test_damaged_file_gives_defaults(self):
        paths.config_dir().mkdir(parents=True)
        (paths.config_dir() / "settings.json").write_text("{not json")
        self.assertEqual(Settings.load(), Settings())


class HistoryTests(FakeSystemTestCase):
    def test_newest_first_and_skips_damaged_lines(self):
        helper.append_history({"started": 1.0, "via": "pkexec", "user": "bryan",
                               "tasks": [{"task": "apt", "state": "done", "count": 2}]})
        with open(helper.HISTORY_FILE, "a") as handle:
            handle.write("garbage\n{\"started\": \"x\"}\n[]\n")
        helper.append_history({"started": 2.0, "tasks": [{"task": "snap", "state": "failed", "message": "offline"},
                                                         {"task": "journal", "state": "done", "freed": 99}]})
        runs = history.load()
        self.assertEqual([run.started for run in runs], [2.0, 1.0])
        self.assertEqual((runs[0].installed, runs[0].freed, len(runs[0].problems)), (0, 99, 1))
        self.assertTrue(runs[0].unattended)
        self.assertEqual((runs[1].installed, runs[1].user, runs[1].unattended), (2, "bryan", False))

    def test_missing_file(self):
        self.assertEqual(history.load(), [])


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest import mock

from system_update import helper, updates
from system_update.helper import PackageChange
from system_update.updates import SourceStatus, Update

CHANGES = [
    PackageChange("libfoo", "upgrade", "1.0", "1.1", security=True, archive="noble-security"),
    PackageChange("linux-image-new", "install", "", "6.0", archive="noble-updates"),
    PackageChange("old-thing", "remove", "0.9"),
]


class AptCheckTests(unittest.TestCase):
    def test_updates_and_download_size(self):
        with mock.patch.object(helper, "simulate_upgrade", return_value=(CHANGES, None)), \
                mock.patch.object(helper, "download_size", return_value=12_000_000):
            status = updates.check_apt()
        self.assertEqual((status.count, status.security, len(status.removals)), (2, 1, 1))
        self.assertEqual(status.download_size, 12_000_000)
        self.assertEqual(status.summary(), "2 updates · 1 security · 1 to remove · 12.0 MB to download")
        self.assertEqual(status.updates[0].detail, "noble-security")

    def test_interrupted_installation_is_offered_as_a_repair(self):
        error = "dpkg was interrupted, you must manually run 'dpkg --configure -a' to correct the problem."
        with mock.patch.object(helper, "simulate_upgrade", return_value=([], error)):
            status = updates.check_apt()
        self.assertTrue(status.repair)
        self.assertEqual(status.error, "")
        self.assertTrue(status.actionable)
        self.assertEqual(status.summary(), "An interrupted installation will be completed")

    def test_other_errors_are_shown(self):
        with mock.patch.object(helper, "simulate_upgrade", return_value=([], "Unable to parse package file")):
            status = updates.check_apt()
        self.assertEqual(status.error, "Unable to parse package file")
        self.assertEqual(status.summary(), "Could not check: Unable to parse package file")


class SourceCheckTests(unittest.TestCase):
    def test_snap(self):
        refreshes = [helper.SnapUpdate("firefox", "132.0", "5200", "131.0", 290_000_000, "Mozilla")]
        with mock.patch.object(helper, "snap_available", return_value=True), \
                mock.patch.object(helper, "snap_refreshes", return_value=refreshes):
            status = updates.check_snap()
        self.assertEqual(status.updates, [Update("firefox", "131.0", "132.0", detail="Mozilla", size=290_000_000)])
        self.assertEqual(status.download_size, 290_000_000)
        with mock.patch.object(helper, "snap_available", return_value=True), \
                mock.patch.object(helper, "snap_refreshes", side_effect=helper.SnapdError("offline")):
            self.assertEqual(updates.check_snap().error, "offline")
        with mock.patch.object(helper, "snap_available", return_value=False):
            self.assertFalse(updates.check_snap().available)

    def test_flatpak_marks_personal_updates(self):
        found = {
            "system": [helper.FlatpakUpdate("Firefox", "org.mozilla.firefox", "stable", "132", "131")],
            "user": [helper.FlatpakUpdate("Notes", "org.example.Notes", "stable", "2", "1", "user")],
        }
        with mock.patch.object(helper, "flatpak_available", return_value=True), \
                mock.patch.object(helper, "flatpak_updates", side_effect=lambda installation: found[installation]):
            status = updates.check_flatpak()
        self.assertEqual([(u.name, u.personal) for u in status.updates], [("Firefox", False), ("Notes", True)])
        self.assertIn("installed for you", status.updates[1].detail)

    def test_firmware(self):
        found = [helper.FirmwareUpdate("System Firmware", "1.2", "1.3", "Fixes", 1234)]
        with mock.patch.object(helper, "fwupd_available", return_value=True), \
                mock.patch.object(helper, "firmware_updates", return_value=found):
            status = updates.check_firmware()
        self.assertEqual((status.count, status.download_size), (1, 1234))


class CheckTests(unittest.TestCase):
    def test_collects_every_source_and_survives_a_broken_one(self):
        def broken():
            raise RuntimeError("boom")

        checks = {
            "apt": lambda: SourceStatus("apt", updates=[Update("libfoo", security=True)], download_size=10),
            "snap": broken,
            "flatpak": lambda: SourceStatus("flatpak", available=False),
            "firmware": lambda: self.fail("firmware was not asked for"),
        }
        with mock.patch.dict(updates.CHECKS, checks), \
                mock.patch.object(updates, "estimate_cleanup", return_value={}), \
                mock.patch.object(helper, "reboot_status", return_value=(True, ["linux-image"])), \
                mock.patch.object(helper, "update_running", return_value=helper.LockHolder(12, "system-update.sh")), \
                mock.patch.object(helper, "apt_lock_holders", return_value=[]):
            result = updates.check(firmware=False)
        self.assertEqual((result.total, result.security, result.download_size()), (1, 1, 10))
        self.assertEqual(result.sources["snap"].error, "boom")
        self.assertFalse(result.sources["firmware"].available)
        self.assertEqual((result.reboot_required, result.running), (True, "system-update.sh (process 12)"))
        data = result.to_json()
        self.assertEqual([source["id"] for source in data["sources"]], ["apt", "snap"])

    def test_download_size_says_when_it_is_incomplete(self):
        result = updates.CheckResult(sources={
            "apt": SourceStatus("apt", updates=[Update("a")], download_size=10),
            "flatpak": SourceStatus("flatpak", updates=[Update("b")], download_size=None),
            "snap": SourceStatus("snap", download_size=None),
        })
        self.assertEqual((result.download_size(), result.download_complete()), (10, False))
        self.assertEqual((result.download_size(["apt"]), result.download_complete(["apt"])), (10, True))
        self.assertIsNone(result.download_size(["flatpak"]))

    def test_every_task_has_an_icon_and_description(self):
        from system_update.settings import Settings

        for task in updates.BY_ID.values():
            with self.subTest(task=task.id):
                self.assertTrue(task.icon.endswith("-symbolic"))
                self.assertTrue(task.describe(Settings()))
        self.assertEqual([task.id for task in updates.TASKS], list(helper.TASKS))


if __name__ == "__main__":
    unittest.main()

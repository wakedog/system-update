import re
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from system_update import APP_ID, HOMEPAGE, __version__, helper, paths

ROOT = Path(__file__).resolve().parent.parent


class VersionTests(unittest.TestCase):
    def test_helper_version_matches(self):
        self.assertEqual(helper.VERSION, __version__)

    def test_newest_appstream_release_is_current_version(self):
        tree = ElementTree.parse(ROOT / "data" / f"{APP_ID}.metainfo.xml")
        self.assertEqual(tree.find("releases/release").get("version"), __version__)

    def test_man_page_shows_current_version(self):
        header = (ROOT / "data" / "system-update.1").read_text().splitlines()[0]
        self.assertIn(f'"system-update {__version__}"', header)

    def test_appstream_homepage_matches_about_dialog(self):
        tree = ElementTree.parse(ROOT / "data" / f"{APP_ID}.metainfo.xml")
        urls = {url.get("type"): url.text for url in tree.findall("url")}
        self.assertEqual(urls["homepage"], HOMEPAGE)
        self.assertTrue(re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+", HOMEPAGE))


class PolicyTests(unittest.TestCase):
    def test_actions_cover_exactly_the_helper_commands(self):
        tree = ElementTree.parse(ROOT / "data" / f"{APP_ID}.policy")
        actions = {}
        for action in tree.findall("action"):
            annotations = {item.get("key"): item.text for item in action.findall("annotate")}
            self.assertEqual(annotations["org.freedesktop.policykit.exec.path"], str(paths.SYSTEM_HELPER))
            actions[annotations["org.freedesktop.policykit.exec.argv1"]] = action.find("defaults/allow_active").text
        # Only refreshing, which installs nothing, may skip the password.
        self.assertEqual(actions, {"refresh": "yes", "upgrade": "auth_admin_keep"})
        self.assertEqual(helper.parse_args(["refresh"]).command, "refresh")
        self.assertEqual(helper.parse_args(["upgrade", "apt"]).command, "upgrade")


if __name__ == "__main__":
    unittest.main()

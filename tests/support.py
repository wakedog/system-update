import io
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from system_update import helper


class TempHomeTestCase(unittest.TestCase):
    """Runs each test with HOME pointing at an empty temporary folder."""

    XDG_VARIABLES = ("XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME")

    def setUp(self):
        super().setUp()
        self._temp = tempfile.TemporaryDirectory(prefix="system-update-test-")
        self.home = Path(self._temp.name)
        self._saved = {name: os.environ.get(name) for name in ("HOME", *self.XDG_VARIABLES)}
        os.environ["HOME"] = str(self.home)
        for name in self.XDG_VARIABLES:
            os.environ.pop(name, None)

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._temp.cleanup()
        super().tearDown()

    def write_script(self, name: str, body: str) -> str:
        """An executable Python script standing in for a system command."""
        path = self.home / "bin" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
        path.chmod(0o755)
        return str(path)


class FakeSystemTestCase(TempHomeTestCase):
    """Also points the helper's system locations (logs, history, lock) into the temporary folder."""

    def setUp(self):
        super().setUp()
        self.root = self.home / "system"
        for directory in ("run/lock", "var/log", "var/lib"):
            (self.root / directory).mkdir(parents=True)
        for name, value in {
            "LOG_DIR": str(self.root / "var/log/system-update"),
            "STATE_DIR": str(self.root / "var/lib/system-update"),
            "HISTORY_FILE": str(self.root / "var/lib/system-update/history.jsonl"),
            "LOCK_FILE": str(self.root / "run/lock/system-update.lock"),
            "REBOOT_FILES": (str(self.root / "run/reboot-required"),),
        }.items():
            self.patch(helper, name, value)
        self.patch(helper, "_adm_gid", lambda: None)
        helper.STOP.clear()
        self.addCleanup(helper.STOP.clear)

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def reporter(self) -> helper.Reporter:
        return helper.Reporter(io.StringIO())


def events(reporter: helper.Reporter) -> list[dict]:
    return [json.loads(line) for line in reporter.stream.getvalue().splitlines()]

#!/usr/bin/python3 -I
"""Privileged helper for System Update.

pkexec runs this program as root. It refreshes package information and
installs updates through a fixed list of tasks: task names come from an
allow-list, numbers are range-checked, and nothing the caller sends becomes a
command, a path or a package name. Progress is reported on stdout as one JSON
object per line, and every update run is logged to /var/log/system-update in
the format system-update.sh used. Writing "stop" to stdin finishes the current
task and skips the rest; nothing ever interrupts apt or dpkg.

The query and parsing functions are also imported, unprivileged, by the
update check, so what the app shows matches what this helper does. Keep this
file free of imports outside the standard library: it is installed on its own
as /usr/libexec/system-update/system-update-helper.
"""

from __future__ import annotations

import argparse
import fcntl
import grp
import http.client
import json
import os
import platform
import pwd
import re
import selectors
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

VERSION = "1.0.0"

APT_GET = "/usr/bin/apt-get"
APT_CONFIG = "/usr/bin/apt-config"
DPKG = "/usr/bin/dpkg"
DPKG_QUERY = "/usr/bin/dpkg-query"
SNAP = "/usr/bin/snap"
FLATPAK = "/usr/bin/flatpak"
FWUPDMGR = "/usr/bin/fwupdmgr"
JOURNALCTL = "/usr/bin/journalctl"
SNAPD_SOCKET = "/run/snapd.socket"

LOG_DIR = "/var/log/system-update"
STATE_DIR = "/var/lib/system-update"
HISTORY_FILE = os.path.join(STATE_DIR, "history.jsonl")
LOCK_FILE = "/run/lock/system-update.lock"  # the same lock system-update.sh takes
LOG_RETENTION = 10
HISTORY_LIMIT = 200
MIN_FREE_MB = 512
MIN_BOOT_FREE_MB = 100
APT_LOCK_TIMEOUT = 300  # seconds to wait for another package manager
REFRESH_LOCK_TIMEOUT = 60
HELPER_TIMEOUT = 1800  # snap and flatpak operations
FWUPD_TIMEOUT = 600

APT_ARCHIVES = "/var/cache/apt/archives"
APT_LOCKS = (
    "/var/lib/dpkg/lock-frontend",
    "/var/lib/dpkg/lock",
    "/var/lib/apt/lists/lock",
    "/var/cache/apt/archives/lock",
)
APT_SOURCES = "/etc/apt/sources.list"
APT_SOURCES_DIR = "/etc/apt/sources.list.d"
JOURNAL_ROOTS = ("/var/log/journal", "/run/log/journal")
REBOOT_FILES = ("/run/reboot-required", "/var/run/reboot-required")
SNAP_DIR = "/var/lib/snapd/snaps"
NETWORK_HOSTS = (
    ("archive.ubuntu.com", 80), ("security.ubuntu.com", 80),
    ("deb.debian.org", 80), ("api.snapcraft.io", 443),
)

# Keep existing configuration files, let apt wait for the dpkg lock, and never
# hang forever on a dead mirror.
APT_OPTIONS = (
    "-o", "Dpkg::Options::=--force-confdef",
    "-o", "Dpkg::Options::=--force-confold",
    "-o", f"DPkg::Lock::Timeout={APT_LOCK_TIMEOUT}",
    "-o", "Acquire::http::Timeout=30",
    "-o", "Acquire::https::Timeout=30",
    "-o", "Acquire::Retries=3",
)
# Without these fwupdmgr can stop to ask about enabling remotes, uploading
# reports or rebooting, which would hang a run nobody is watching.
FWUPD_OPTIONS = ("--no-remote-check", "--no-unreported-check", "--no-metadata-check")

UPDATE_TASKS = ("apt", "snap", "flatpak", "firmware")
CLEANUP_TASKS = ("autoremove", "apt_cache", "snap_revisions", "flatpak_unused", "journal")
TASKS = UPDATE_TASKS + CLEANUP_TASKS
TITLES = {
    "refresh": "Refresh package information",
    "apt": "APT: Package upgrade",
    "snap": "Snap: Refresh",
    "flatpak": "Flatpak: Update",
    "firmware": "Firmware: fwupd",
    "autoremove": "APT: Remove unused packages",
    "apt_cache": "APT: Clean package cache",
    "snap_revisions": "Snap: Remove disabled revisions",
    "flatpak_unused": "Flatpak: Remove unused runtimes",
    "journal": "System journal",
}
DONE, WARNING, FAILED, SKIPPED = "done", "warning", "failed", "skipped"

SNAP_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}(_[a-z0-9]{1,10})?")
SNAP_REVISION = re.compile(r"x?[0-9]{1,12}")
CHANGE_ID = re.compile(r"[0-9]{1,12}")
INTERPRETERS = re.compile(r"(python[0-9.]*|perl|sh|bash|dash)")

STOP = threading.Event()


class HelperError(Exception):
    """A query failed."""


class SnapdError(HelperError):
    def __init__(self, message: str, kind: str = ""):
        super().__init__(message)
        self.kind = kind


class TaskError(Exception):
    """A task failed, possibly after doing part of its work."""


class Stopped(Exception):
    """The run was asked to stop before this task could start."""


def _plural(count: int, word: str, plural: str | None = None) -> str:
    return f"{count} {word if count == 1 else plural or word + 's'}"


def _join(names: list[str], limit: int = 3) -> str:
    shown = names[:limit]
    if len(names) > limit:
        return ", ".join(shown) + f" and {len(names) - limit} more"
    return " and ".join(shown) if len(shown) == 2 else ", ".join(shown)


def allocated(st: os.stat_result) -> int:
    return st.st_blocks * 512


def command_env(*, restart_services: bool = False) -> dict[str, str]:
    """Environment for every command: never prompt, English output."""
    env = {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "LC_ALL": "C.UTF-8",
        "DEBIAN_FRONTEND": "noninteractive",
        "DEBCONF_NONINTERACTIVE_SEEN": "true",
        "UCF_FORCE_CONFFOLD": "1",
        "APT_LISTCHANGES_FRONTEND": "none",
        "NEEDRESTART_MODE": "a" if restart_services else "l",
    }
    if os.geteuid() == 0:
        env["HOME"] = "/root"
    else:
        for name in ("HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
                     "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
            if os.environ.get(name):
                env[name] = os.environ[name]
    return env


def _run(argv: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess:
    name = os.path.basename(argv[0])
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, errors="replace", env=command_env(),
            timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as error:
        raise HelperError(f"{name} did not answer within {timeout} seconds") from error
    except OSError as error:
        raise HelperError(f"Could not run {name}: {error.strerror}") from error


def _error_text(result: subprocess.CompletedProcess) -> str:
    lines = [line.strip() for line in (result.stderr or "").splitlines() if line.strip()]
    errors = [line for line in lines if line.startswith("E:")] or lines
    if not errors:
        return f"exit status {result.returncode}"
    return re.sub(r"^E:\s*", "", errors[-1])


def _query(argv: list[str], *, timeout: int = 300) -> str:
    result = _run(argv, timeout=timeout)
    if result.returncode != 0:
        raise HelperError(f"{os.path.basename(argv[0])} failed: {_error_text(result)}")
    return result.stdout


# ─────────────────────────────── APT queries ────────────────────────────────

@dataclass
class PackageChange:
    name: str
    kind: str  # "upgrade", "install" or "remove"
    old_version: str = ""
    new_version: str = ""
    security: bool = False
    archive: str = ""  # where the new version comes from, like "noble-updates"


INST_LINE = re.compile(
    r"Inst (?P<name>\S+)(?: \[(?P<old>[^\]]*)\])? \((?P<new>\S+)(?P<origins>[^\[]*)\[(?P<arch>[^\]]*)\]\)")
REMOVE_LINE = re.compile(r"(?:Remv|Purg) (?P<name>\S+)(?: \[(?P<old>[^\]]*)\])?")
URI_LINE = re.compile(r"'[^']*' \S+ (?P<size>[0-9]+)")
STATUS_LINE = re.compile(
    r"(?P<kind>dlstatus|pmstatus|pmerror|pmconffile):(?P<item>.*?):(?P<percent>[0-9]+(?:\.[0-9]*)?):(?P<text>.*)")


def _archives(origins: str) -> list[tuple[str, str]]:
    """(label, archive) pairs from "Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security"."""
    pairs = []
    for origin in origins.split(","):
        origin = origin.strip()
        if origin:
            label, _, rest = origin.partition(":")
            pairs.append((label, rest.rpartition("/")[2]))
    return pairs


def parse_simulation(text: str) -> list[PackageChange]:
    """What `apt-get --simulate full-upgrade` would install, upgrade and remove."""
    changes = []
    for line in text.splitlines():
        if match := INST_LINE.match(line):
            archives = _archives(match["origins"])
            secure = [archive for label, archive in archives
                      if archive.endswith("-security") or label.lower().endswith("-security")]
            changes.append(PackageChange(
                name=match["name"], kind="upgrade" if match["old"] else "install",
                old_version=match["old"] or "", new_version=match["new"], security=bool(secure),
                archive=secure[0] if secure else archives[0][1] if archives else "",
            ))
        elif match := REMOVE_LINE.match(line):
            changes.append(PackageChange(name=match["name"], kind="remove", old_version=match["old"] or ""))
    return changes


def needs_repair(error: str | None) -> bool:
    """True when apt refuses to plan because an earlier installation was interrupted."""
    return bool(error) and "dpkg --configure -a" in error


def simulate_upgrade() -> tuple[list[PackageChange], str | None]:
    """Planned changes, and apt's error message if the simulation failed.

    This honours phased updates and held packages, unlike `apt list --upgradable`.
    """
    result = _run([APT_GET, "-o", "Debug::NoLocking=1", "--simulate", "-qq", "full-upgrade"])
    changes = parse_simulation(result.stdout)
    return changes, (_error_text(result) if result.returncode != 0 else None)


def parse_print_uris(text: str) -> int:
    return sum(int(match["size"]) for line in text.splitlines() if (match := URI_LINE.match(line)))


def download_size() -> int | None:
    """Bytes a full upgrade still has to download (packages already cached are free)."""
    try:
        return parse_print_uris(_query([APT_GET, "-o", "Debug::NoLocking=1", "--print-uris", "-qq",
                                        "full-upgrade"]))
    except HelperError:
        return None


def parse_autoremove(text: str) -> list[str]:
    return [change.name for change in parse_simulation(text) if change.kind == "remove"]


def autoremove_candidates() -> list[str]:
    return parse_autoremove(_query([APT_GET, "-o", "Debug::NoLocking=1", "--simulate", "autoremove"]))


def parse_installed_sizes(text: str, native_arch: str) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            continue
        package, arch, size = fields
        try:
            value = int(size) * 1024  # dpkg reports KiB
        except ValueError:
            continue
        sizes[f"{package}:{arch}"] = value
        if arch in (native_arch, "all"):
            sizes[package] = value
        else:
            sizes.setdefault(package, value)
    return sizes


def installed_sizes() -> dict[str, int]:
    """Installed size in bytes of every package, keyed by name and name:arch."""
    native = _query([DPKG, "--print-architecture"], timeout=30).strip()
    listing = _query([DPKG_QUERY, "--show", "--showformat=${Package}\t${Architecture}\t${Installed-Size}\n"])
    return parse_installed_sizes(listing, native)


def parse_status_line(line: str) -> tuple[str, str, float, str] | None:
    """(kind, item, percent, text) from an APT::Status-Fd line."""
    match = STATUS_LINE.fullmatch(line.strip())
    if not match:
        return None
    return match["kind"], match["item"], min(100.0, float(match["percent"])), match["text"].strip()


def lists_updated() -> float | None:
    """When the package lists were last refreshed successfully."""
    for path in ("/var/lib/apt/periodic/update-success-stamp", "/var/lib/apt/lists"):
        try:
            return os.stat(path).st_mtime
        except OSError:
            continue
    return None


# ─────────────────────────────── snapd queries ──────────────────────────────

class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def snapd_request(method: str, path: str, body: dict | None = None, *, timeout: float = 60) -> dict:
    """Call the snapd REST API and return the decoded response."""
    connection = _UnixHTTPConnection(SNAPD_SOCKET, timeout)
    try:
        payload = json.dumps(body) if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        connection.request(method, path, body=payload, headers=headers)
        data = json.loads(connection.getresponse().read() or b"{}")
    except (OSError, http.client.HTTPException, ValueError) as error:
        raise SnapdError(f"Could not reach the snap service: {error}") from error
    finally:
        connection.close()
    if not isinstance(data, dict):
        raise SnapdError("Unexpected answer from the snap service")
    if data.get("type") == "error":
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        raise SnapdError(str(result.get("message") or f"snapd error {data.get('status-code')}"),
                         str(result.get("kind") or ""))
    return data


def snap_available() -> bool:
    return os.path.exists(SNAP) and os.path.exists(SNAPD_SOCKET)


@dataclass
class SnapUpdate:
    name: str
    version: str
    revision: str
    old_version: str = ""
    size: int | None = None
    publisher: str = ""


def installed_snaps(*, all_revisions: bool = False) -> list[dict]:
    result = snapd_request("GET", "/v2/snaps?select=all" if all_revisions else "/v2/snaps").get("result")
    return [snap for snap in result if isinstance(snap, dict)] if isinstance(result, list) else []


def parse_snap_refreshes(found: object, installed: list[dict]) -> list[SnapUpdate]:
    versions = {str(snap.get("name")): str(snap.get("version") or "") for snap in installed}
    updates = []
    for snap in found if isinstance(found, list) else []:
        if not isinstance(snap, dict) or not SNAP_NAME.fullmatch(str(snap.get("name", ""))):
            continue
        publisher = snap.get("publisher") if isinstance(snap.get("publisher"), dict) else {}
        size = snap.get("download-size")
        updates.append(SnapUpdate(
            name=snap["name"], version=str(snap.get("version") or ""), revision=str(snap.get("revision") or ""),
            old_version=versions.get(snap["name"], ""),
            size=size if isinstance(size, int) and not isinstance(size, bool) else None,
            publisher=str(publisher.get("display-name") or publisher.get("username") or ""),
        ))
    return updates


def snap_refreshes() -> list[SnapUpdate]:
    """Snaps with a newer revision in the store, as `snap refresh --list` shows them."""
    try:
        found = snapd_request("GET", "/v2/find?select=refresh", timeout=120).get("result")
    except SnapdError as error:
        if error.kind == "snap-not-found":
            return []
        raise
    return parse_snap_refreshes(found, installed_snaps())


def parse_disabled_revisions(snaps: list[dict]) -> list[tuple[str, str]]:
    revisions = []
    for snap in snaps:
        name, revision = str(snap.get("name", "")), str(snap.get("revision", ""))
        if snap.get("status") != "active" and SNAP_NAME.fullmatch(name) and SNAP_REVISION.fullmatch(revision):
            revisions.append((name, revision))
    return revisions


def disabled_snap_revisions() -> list[tuple[str, str]]:
    """(name, revision) of old revisions snapd keeps after refreshes."""
    return parse_disabled_revisions(installed_snaps(all_revisions=True))


def snap_file(name: str, revision: str) -> str:
    return os.path.join(SNAP_DIR, f"{name}_{revision}.snap")


def summarize_change(change: dict) -> tuple[float, str]:
    """Overall progress (0–1) of a snapd change and what it is doing now."""
    tasks = [task for task in change.get("tasks") or [] if isinstance(task, dict)]
    summary = str(change.get("summary") or "")
    if not tasks:
        return (1.0 if change.get("ready") else 0.0), summary
    finished, current = 0.0, ""
    for task in tasks:
        status = task.get("status")
        if status in ("Done", "Undone", "Error", "Hold", "Abort"):
            finished += 1
        elif status == "Doing":
            progress = task.get("progress") if isinstance(task.get("progress"), dict) else {}
            done, total = progress.get("done"), progress.get("total")
            part = 0.0
            if isinstance(done, (int, float)) and isinstance(total, (int, float)) and total > 1:
                part = min(max(done / total, 0.0), 1.0)
            finished += part
            if not current:
                current = str(task.get("summary") or "")
                if part:
                    current += f" ({part:.0%})"
    return finished / len(tasks), current or summary


# ────────────────────────────── Flatpak queries ─────────────────────────────

@dataclass
class FlatpakUpdate:
    name: str
    application: str
    branch: str
    version: str = ""
    old_version: str = ""
    installation: str = "system"


def flatpak_available() -> bool:
    return os.path.exists(FLATPAK)


def parse_columns(text: str, count: int) -> list[list[str]]:
    """Rows of tab-separated flatpak output (it prints no header when piped)."""
    rows = []
    for line in text.splitlines():
        fields = [value.strip() for value in line.split("\t")]
        if len(fields) == count and any(fields):
            rows.append(fields)
    return rows


def flatpak_installed(installation: str) -> list[list[str]]:
    """[application, branch, version] of every installed ref."""
    return parse_columns(_query([FLATPAK, "list", f"--{installation}", "--columns=application,branch,version"],
                                timeout=120), 3)


def flatpak_updates(installation: str) -> list[FlatpakUpdate]:
    """Apps and runtimes with an update in the given installation ("system" or "user")."""
    installed = {(app, branch): version for app, branch, version in flatpak_installed(installation)}
    if not installed:
        return []
    text = _query([FLATPAK, "remote-ls", "--updates", f"--{installation}",
                   "--columns=name,application,branch,version"], timeout=300)
    return [
        FlatpakUpdate(name=name or application, application=application, branch=branch, version=version,
                      old_version=installed.get((application, branch), ""), installation=installation)
        for name, application, branch, version in parse_columns(text, 4)
    ]


# ────────────────────────────── Firmware queries ────────────────────────────

@dataclass
class FirmwareUpdate:
    device: str
    old_version: str
    new_version: str
    summary: str = ""
    size: int | None = None


def fwupd_available() -> bool:
    return os.path.exists(FWUPDMGR)


def parse_fwupd_updates(text: str) -> list[FirmwareUpdate]:
    """Devices with an available update from `fwupdmgr get-updates --json`."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    updates = []
    for device in data.get("Devices") or [] if isinstance(data, dict) else []:
        if not isinstance(device, dict):
            continue
        releases = [release for release in device.get("Releases") or [] if isinstance(release, dict)]
        if not releases:
            continue
        release = releases[0]  # fwupd lists the newest release first
        size = release.get("Size")
        updates.append(FirmwareUpdate(
            device=str(device.get("Name") or "Unknown device"),
            old_version=str(device.get("Version") or ""),
            new_version=str(release.get("Version") or ""),
            summary=str(release.get("Summary") or ""),
            size=size if isinstance(size, int) and not isinstance(size, bool) else None,
        ))
    return updates


def firmware_updates(*, timeout: int = 120) -> list[FirmwareUpdate]:
    result = _run([FWUPDMGR, "get-updates", "--json", *FWUPD_OPTIONS], timeout=timeout)
    if result.returncode not in (0, 2):  # 2 means "no updates"
        raise HelperError(f"fwupdmgr failed: {_error_text(result)}")
    return parse_fwupd_updates(result.stdout)


# ─────────────────────────────── System state ───────────────────────────────

def reboot_status() -> tuple[bool, list[str]]:
    """Whether Ubuntu asks for a restart, and the packages that asked for it."""
    for path in REBOOT_FILES:
        if os.path.exists(path):
            try:
                with open(path + ".pkgs", encoding="utf-8", errors="replace") as handle:
                    packages = sorted({line.strip() for line in handle if line.strip()})
            except OSError:
                packages = []
            return True, packages
    return False, []


def tree_size(root: str) -> int:
    """Disk space used by the regular files below *root*, on its filesystem only."""
    try:
        device = os.stat(root, follow_symlinks=False).st_dev
    except OSError:
        return 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                st = os.stat(os.path.join(dirpath, name), follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += allocated(st)
        kept = []
        for name in dirnames:
            try:
                if os.stat(os.path.join(dirpath, name), follow_symlinks=False).st_dev == device:
                    kept.append(name)
            except OSError:
                continue
        dirnames[:] = kept
    return total


def journal_size() -> int:
    return sum(tree_size(root) for root in JOURNAL_ROOTS)


@dataclass(frozen=True)
class LockHolder:
    pid: int
    name: str

    def __str__(self) -> str:
        return f"{self.name} (process {self.pid})" if self.pid > 0 else self.name


def process_name(pid: int) -> str:
    """A readable name: the script for interpreters ("unattended-upgrade"), else the program."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            args = [os.path.basename(arg.decode("utf-8", "replace")) for arg in handle.read().split(b"\0") if arg]
    except OSError:
        args = []
    if args and INTERPRETERS.fullmatch(args[0]):
        scripts = [arg for arg in args[1:] if not arg.startswith("-")]
        if scripts:
            return scripts[0]
    if args:
        return args[0]
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip() or "unknown program"
    except OSError:
        return "unknown program"


def parse_proc_locks(text: str) -> list[tuple[int, str, int]]:
    """(pid, "major:minor", inode) of every lock held, from /proc/locks."""
    held = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 6 or fields[1] == "->":  # "->" marks a process waiting for the lock
            continue
        device, _, inode = fields[5].rpartition(":")
        try:
            held.append((int(fields[4]), device, int(inode)))
        except ValueError:
            continue
    return held


def locked_by(path: str, locks: list[tuple[int, str, int]] | None = None) -> list[int]:
    """Processes holding a lock on *path*, according to /proc/locks."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    if locks is None:
        try:
            with open("/proc/locks", encoding="ascii", errors="replace") as handle:
                locks = parse_proc_locks(handle.read())
        except OSError:
            return []
    major, minor = os.major(st.st_dev), os.minor(st.st_dev)
    device = f"{major:02x}:{minor:02x}"
    pids = []
    for pid, lock_device, inode in locks:
        # btrfs reports a different device in /proc/locks than stat() does.
        if inode == st.st_ino and (lock_device == device or (major == 0 and lock_device.startswith("00:"))):
            pids.append(pid)
    return pids


def _pids_with_open(paths: tuple[str, ...]) -> set[int]:
    """Processes that have any of *paths* open, like fuser(1). Needs root."""
    targets, found = set(paths), set()
    try:
        pids = [entry for entry in os.listdir("/proc") if entry.isdigit()]
    except OSError:
        return found
    for pid in pids:
        fd_dir = f"/proc/{pid}/fd"
        try:
            descriptors = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in descriptors:
            try:
                if os.readlink(f"{fd_dir}/{fd}") in targets:
                    found.add(int(pid))
                    break
            except OSError:
                continue
    return found


def apt_lock_holders() -> list[LockHolder]:
    """Other programs using apt or dpkg right now (unattended-upgrades, a terminal…)."""
    if os.geteuid() == 0:
        pids = _pids_with_open(APT_LOCKS)
    else:
        pids = {pid for path in APT_LOCKS for pid in locked_by(path)}
    pids.discard(os.getpid())
    return [LockHolder(pid, process_name(pid) if pid > 0 else "another program") for pid in sorted(pids)]


def update_running() -> LockHolder | None:
    """The process holding System Update's lock (or system-update.sh's), if any."""
    for pid in locked_by(LOCK_FILE):
        if pid != os.getpid():
            return LockHolder(pid, process_name(pid) if pid > 0 else "another update")
    return None


# ──────────────────────────────── Network ───────────────────────────────────

SOURCE_URI = re.compile(r"\b(https?)://([A-Za-z0-9.-]+)(?::([0-9]{1,5}))?")


def apt_source_hosts(limit: int = 4) -> list[tuple[str, int]]:
    """Hosts of the configured package sources, to test the connection against."""
    files = [APT_SOURCES]
    try:
        files += sorted(os.path.join(APT_SOURCES_DIR, name) for name in os.listdir(APT_SOURCES_DIR)
                        if name.endswith((".list", ".sources")))
    except OSError:
        pass
    hosts: list[tuple[str, int]] = []
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        for line in lines:
            if line.lstrip().startswith("#"):
                continue
            for scheme, host, port in SOURCE_URI.findall(line):
                entry = (host, int(port) if port else 443 if scheme == "https" else 80)
                if entry not in hosts:
                    hosts.append(entry)
    return hosts[:limit]


def apt_proxy_configured() -> bool:
    try:
        text = _query([APT_CONFIG, "dump"], timeout=30)
    except HelperError:
        return False
    for line in text.splitlines():
        match = re.match(r'Acquire::(?:https?|ftp)::Proxy(?:::\S+)? "(.*)";', line)
        if match and match[1] and match[1].upper() != "DIRECT":
            return True
    return False


def network_available(timeout: float = 5.0) -> bool:
    """Whether a package mirror (or a well-known host) can be reached.

    A configured apt proxy counts as a connection: direct connections are
    often blocked where one is required.
    """
    if apt_proxy_configured():
        return True
    for host, port in dict.fromkeys([*apt_source_hosts(), *NETWORK_HOSTS]):
        try:
            socket.create_connection((host, port), timeout=timeout).close()
            return True
        except OSError:
            continue
    return False


# ───────────────────────────── Running tasks ────────────────────────────────

LOG_LEVELS = {"info": "INFO", "ok": "OK", "warning": "WARN", "error": "FAIL"}


class RunLog:
    """/var/log/system-update/update-YYYYMMDD-HHMMSS.log, laid out like system-update.sh's."""

    def __init__(self) -> None:
        self.path: str | None = None
        self.error = ""
        self._file = None
        try:
            _prepare_log_dir()
            prune_logs(LOG_DIR, keep=LOG_RETENTION - 1)
            base = os.path.join(LOG_DIR, time.strftime("update-%Y%m%d-%H%M%S"))
            for suffix in ("", *(f"-{number}" for number in range(2, 100))):
                path = f"{base}{suffix}.log"
                try:
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640)
                except FileExistsError:
                    continue
                gid = _adm_gid()
                if gid is not None:
                    os.fchown(fd, -1, gid)
                os.fchmod(fd, 0o640)
                self._file = os.fdopen(fd, "w", encoding="utf-8", errors="replace")
                self.path = path
                break
        except OSError as error:
            self.error = error.strerror or str(error)

    def _write(self, text: str) -> None:
        if self._file:
            try:
                self._file.write(text)
                self._file.flush()
            except OSError:  # a full disk must not stop the update
                pass

    def line(self, level: str, text: str) -> None:
        self._write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | [{level:<4}] {text}\n")

    def raw(self, text: str) -> None:
        self._write(text + "\n")

    def section(self, title: str) -> None:
        border = "=" * 63
        for text in ("", border, f" {title}", border):
            self.line("INFO", text)

    def close(self) -> None:
        if self._file:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None


def _adm_gid() -> int | None:
    try:
        return grp.getgrnam("adm").gr_gid
    except KeyError:
        return None


def _prepare_log_dir() -> None:
    """Create the log folder, readable by the adm group like the rest of /var/log."""
    try:
        os.mkdir(LOG_DIR, 0o750)
    except FileExistsError:
        pass
    st = os.lstat(LOG_DIR)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
        raise OSError(f"{LOG_DIR} is not a folder owned by root")
    gid = _adm_gid()
    if gid is not None and st.st_gid != gid:
        os.chown(LOG_DIR, -1, gid)
    os.chmod(LOG_DIR, 0o2750)


def prune_logs(directory: str, keep: int) -> None:
    """Keep the newest *keep* update logs; names embed the time, so sorting is chronological."""
    try:
        logs = sorted(name for name in os.listdir(directory) if name.startswith("update-") and name.endswith(".log"))
    except OSError:
        return
    for name in logs[: max(0, len(logs) - keep)]:
        try:
            os.unlink(os.path.join(directory, name))
        except OSError:
            pass


def append_history(record: dict) -> None:
    """Add one run to the history that the app shows, keeping the newest HISTORY_LIMIT."""
    os.makedirs(STATE_DIR, mode=0o755, exist_ok=True)
    fd = os.open(HISTORY_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    os.fchmod(fd, 0o644)  # the app reads it as your user
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    with open(HISTORY_FILE, encoding="utf-8", errors="replace") as handle:
        lines = handle.readlines()
    if len(lines) > HISTORY_LIMIT + 50:
        temp = HISTORY_FILE + ".new"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.writelines(lines[-HISTORY_LIMIT:])
        os.replace(temp, HISTORY_FILE)


class Reporter:
    """Writes progress events as JSON lines, and messages to the run log."""

    def __init__(self, stream, log: RunLog | None = None):
        self.stream = stream
        self.log = log
        self.broken = False
        self._lock = threading.Lock()
        self._last_progress = 0.0

    def emit(self, event: str, **data) -> None:
        with self._lock:
            if self.broken:
                return
            try:
                self.stream.write(json.dumps({"event": event, **data}) + "\n")
                self.stream.flush()
            except (OSError, ValueError):
                # The app went away. Finish the current work quietly rather than
                # abandoning apt or dpkg halfway through.
                self.broken = True
                try:
                    devnull = os.open(os.devnull, os.O_WRONLY)
                    os.dup2(devnull, self.stream.fileno())
                    os.close(devnull)
                except (OSError, ValueError):
                    pass

    def output(self, task: str, text: str) -> None:
        self.emit("output", task=task, text=text)
        if self.log:
            self.log.raw(text)

    def message(self, level: str, text: str) -> None:
        self.emit("message", level=level, text=text)
        if self.log:
            self.log.line(LOG_LEVELS[level], text)

    def progress(self, task: str, fraction: float, text: str) -> None:
        now = time.monotonic()
        if now - self._last_progress >= 0.2 or fraction >= 1:
            self._last_progress = now
            self.emit("progress", task=task, fraction=round(min(max(fraction, 0.0), 1.0), 4), text=text)


@dataclass
class TaskResult:
    count: int | None = None  # updates installed or items removed
    freed: int | None = None
    message: str = ""
    warning: bool = False
    items: list[str] = field(default_factory=list)


@dataclass
class Context:
    options: argparse.Namespace
    report: Reporter
    env: dict[str, str]
    apt_blocked: str = ""  # set when the package system stayed busy too long
    firmware_reboot: bool = False

    def run(self, argv: list[str], task: str, *, timeout: int | None = None,
            on_status: Callable[[str], None] | None = None, check: bool = True) -> int:
        """Run a command with stdin closed, streaming its output; returns the exit status.

        *timeout* is only for clients that can be killed safely (snap, flatpak,
        fwupdmgr). apt and dpkg are never given one.
        """
        name = os.path.basename(argv[0])
        status_read = status_write = None
        if on_status is not None:
            status_read, status_write = os.pipe()
            argv = [*argv, "-o", f"APT::Status-Fd={status_write}"]
        self.report.output(task, "$ " + " ".join(argv))
        try:
            process = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=self.env, start_new_session=True,  # keep terminal signals away from apt and dpkg
                pass_fds=() if status_write is None else (status_write,),
            )
        except OSError as error:
            if status_read is not None:
                os.close(status_read)
            raise TaskError(f"Could not run {name}: {error.strerror}") from error
        finally:
            if status_write is not None:
                os.close(status_write)
        assert process.stdout is not None
        streams = {process.stdout.fileno(): None}
        if status_read is not None:
            streams[status_read] = on_status
        try:
            timed_out = self._pump(process, streams, task, timeout)
        finally:
            process.stdout.close()
            if status_read is not None:
                os.close(status_read)
        status = process.wait()
        if timed_out:
            limit = f"{timeout // 60} minutes" if timeout >= 120 else _plural(timeout, "second")
            raise TaskError(f"{name} did not finish within {limit} and was stopped")
        if check and status != 0:
            raise TaskError(f"{name} exited with status {status}")
        return status

    def _pump(self, process: subprocess.Popen, streams: dict, task: str, timeout: int | None) -> bool:
        selector = selectors.DefaultSelector()
        pending = {}
        for fd, handler in streams.items():
            selector.register(fd, selectors.EVENT_READ, handler)
            pending[fd] = b""
        deadline = None if timeout is None else time.monotonic() + timeout
        kill_at = exited_at = None
        timed_out = False
        try:
            while selector.get_map():
                now = time.monotonic()
                if deadline is not None and not timed_out and now >= deadline:
                    timed_out = True
                    process.terminate()
                    kill_at = now + 30
                if kill_at is not None and now >= kill_at:
                    process.kill()
                    kill_at = None
                if process.poll() is not None:
                    exited_at = exited_at or now
                    if now - exited_at > 2:  # something it started kept the pipe open
                        break
                for key, _ in selector.select(timeout=0.5):
                    try:
                        chunk = os.read(key.fd, 65536)
                    except OSError:
                        chunk = b""
                    if chunk:
                        *lines, pending[key.fd] = re.split(rb"[\r\n]", pending[key.fd] + chunk)
                    else:
                        selector.unregister(key.fd)
                        lines = [pending.pop(key.fd)]
                    for raw in lines:
                        line = raw.decode("utf-8", "replace").rstrip()
                        if not line:
                            continue
                        if key.data is None:
                            self.report.output(task, line)
                        else:
                            key.data(line)
        finally:
            selector.close()
        return timed_out


def apt_progress(ctx: Context, task: str, *, download: tuple[float, float] = (0.0, 0.0),
                 install: tuple[float, float] = (0.0, 1.0), label: str = "Downloading") -> Callable[[str], None]:
    """Map APT::Status-Fd lines to progress: *download* and *install* are (start, span)."""
    def on_status(line: str) -> None:
        parsed = parse_status_line(line)
        if parsed is None:
            return
        kind, item, percent, text = parsed
        if kind == "dlstatus":
            start, span = download
            ctx.report.progress(task, start + span * percent / 100, f"{label} ({percent:.0f}%)")
        elif kind == "pmstatus":
            start, span = install
            ctx.report.progress(task, start + span * percent / 100, text)
        elif kind == "pmerror":
            ctx.report.message("error", f"{item}: {text}")

    return on_status


def wait_for_apt(ctx: Context, task: str, timeout: int = APT_LOCK_TIMEOUT) -> None:
    """Wait until no other program uses apt or dpkg, as system-update.sh did."""
    if ctx.apt_blocked:
        raise TaskError(ctx.apt_blocked)
    waited = 0
    while True:
        holders = apt_lock_holders()
        if not holders:
            if waited:
                ctx.report.message("ok", f"Package manager lock released after {waited}s")
            return
        names = ", ".join(str(holder) for holder in holders)
        if waited >= timeout:
            ctx.apt_blocked = f"Another program kept the package system busy for over {timeout // 60} minutes: {names}"
            raise TaskError(ctx.apt_blocked)
        if waited % 30 == 0:
            ctx.report.emit("waiting", task=task, holders=[str(holder) for holder in holders],
                            waited=waited, limit=timeout)
            if waited % 60 == 0:
                ctx.report.message("warning", f"Waiting for package manager lock... ({waited}s / {timeout}s): {names}")
        if STOP.is_set():
            raise Stopped()
        time.sleep(5)
        waited += 5


def wait_for_change(ctx: Context, change_id: str, task: str, *, timeout: int = HELPER_TIMEOUT,
                    base: float = 0.0, span: float = 1.0) -> dict:
    """Follow a snapd change until it is ready, reporting what it is doing."""
    if not CHANGE_ID.fullmatch(change_id):
        raise TaskError(f"Unexpected change id from snapd: {change_id!r}")
    deadline = time.monotonic() + timeout
    logged: dict[str, str] = {}
    while True:
        try:
            change = snapd_request("GET", f"/v2/changes/{change_id}", timeout=30).get("result") or {}
        except SnapdError as error:
            raise TaskError(str(error)) from error
        for item in change.get("tasks") or []:
            if isinstance(item, dict) and item.get("status") in ("Done", "Error", "Undone", "Hold"):
                key = str(item.get("id"))
                if logged.get(key) != item["status"]:
                    logged[key] = item["status"]
                    ctx.report.output(task, f"{item.get('summary', '')}: {item['status']}")
        fraction, text = summarize_change(change)
        ctx.report.progress(task, base + span * fraction, text)
        if change.get("ready"):
            if change.get("status") == "Done":
                return change
            raise TaskError(str(change.get("err") or f"The snap change ended as {change.get('status')}").strip())
        if time.monotonic() > deadline:
            raise TaskError(f"snapd is still working after {timeout // 60} minutes; it will finish in the background")
        time.sleep(0.5)


def _start_snap_change(method: str, path: str, body: dict) -> str | None:
    """Start a snapd change; returns its id, or None if snapd finished right away."""
    data = snapd_request(method, path, body, timeout=120)
    return str(data["change"]) if data.get("type") == "async" and data.get("change") else None


# ─────────────────────────────────── Tasks ──────────────────────────────────

def task_apt(ctx: Context) -> TaskResult:
    wait_for_apt(ctx, "apt")
    notes = []
    try:
        ctx.run([APT_GET, *APT_OPTIONS, "-q", "update"], "apt",
                on_status=apt_progress(ctx, "apt", download=(0.0, 0.15), label="Refreshing package lists"))
    except TaskError as error:
        ctx.report.message("error", f"apt-get update failed ({error}); continuing with the current package lists")
        notes.append("some package sources could not be refreshed")
    if ctx.run([DPKG, "--configure", "-a"], "apt", check=False):
        ctx.report.message("warning", "dpkg --configure -a reported issues")
    if ctx.run([APT_GET, *APT_OPTIONS, "-y", "-f", "install"], "apt", check=False):
        ctx.report.message("warning", "apt-get -f install reported issues")

    changes, error = simulate_upgrade()
    if error:
        ctx.report.message("warning", f"Upgrade simulation failed ({error}); attempting full-upgrade anyway")
    upgrades = [change for change in changes if change.kind != "remove"]
    removals = [change for change in changes if change.kind == "remove"]
    if not changes and not error:
        ctx.report.message("ok", "System is already up to date")
        return TaskResult(count=0, message="Already up to date" + (f"; {notes[0]}" if notes else ""),
                          warning=bool(notes))
    ctx.report.message("info", f"{len(upgrades)} package(s) to install/upgrade, {len(removals)} to remove")
    for change in changes:
        versions = f"{change.old_version} -> {change.new_version}" if change.kind == "upgrade" else (
            change.new_version or change.old_version)
        ctx.report.message("info", f"  {change.kind}: {change.name} {versions}".rstrip())

    ctx.run([APT_GET, *APT_OPTIONS, "-y", "-q", "full-upgrade"], "apt",
            on_status=apt_progress(ctx, "apt", download=(0.15, 0.3), install=(0.45, 0.55)))
    installed = sum(1 for change in upgrades if change.kind == "install")
    parts = [f"Updated {_plural(len(upgrades) - installed, 'package')}"] if changes else ["Already up to date"]
    if installed:
        parts.append(f"installed {installed} new")
    if removals:
        parts.append(f"removed {len(removals)}")
    message = ", ".join(parts) + (f"; {notes[0]}" if notes else "")
    return TaskResult(count=len(upgrades), message=message, warning=bool(notes),
                      items=[change.name for change in upgrades])


def task_snap(ctx: Context) -> TaskResult:
    if not snap_available():
        return TaskResult(message="Snap is not installed")
    try:
        before = snap_refreshes()
    except HelperError as error:
        raise TaskError(str(error)) from error
    if not before:
        return TaskResult(count=0, message="Already up to date")
    for snap in before:
        ctx.report.message("info", f"  refresh: {snap.name} {snap.old_version} -> {snap.version}")
    try:
        change_id = _start_snap_change("POST", "/v2/snaps", {"action": "refresh"})
    except SnapdError as error:
        if error.kind == "snap-change-conflict":  # snapd's own automatic refresh is running
            return TaskResult(count=0, warning=True, message="snapd is already updating snaps in the background")
        raise TaskError(str(error)) from error
    if change_id:
        wait_for_change(ctx, change_id, "snap")
    try:
        still = {snap.name for snap in snap_refreshes()}
    except HelperError:
        still = set()
    updated = [snap.name for snap in before if snap.name not in still]
    waiting = [snap.name for snap in before if snap.name in still]
    message = f"Updated {_join(updated)}" if updated else "No snaps were updated"
    if waiting:
        message += f"; {_join(waiting)} will update later, for example once closed"
    return TaskResult(count=len(updated), message=message, warning=bool(waiting), items=updated)


def task_flatpak(ctx: Context) -> TaskResult:
    if not flatpak_available():
        return TaskResult(message="Flatpak is not installed")
    try:
        before = flatpak_updates("system")
    except HelperError as error:
        ctx.report.message("warning", f"Could not list Flatpak updates: {error}")
        before = None
    if before == []:
        return TaskResult(count=0, message="Already up to date")
    ctx.run([FLATPAK, "update", "--system", "-y", "--noninteractive"], "flatpak", timeout=HELPER_TIMEOUT)
    names = [update.name for update in before or []]
    return TaskResult(count=len(names) if before is not None else None, items=names,
                      message=f"Updated {_join(names)}" if names else "Flatpak apps updated")


def task_firmware(ctx: Context) -> TaskResult:
    if not fwupd_available():
        return TaskResult(message="fwupd is not installed")
    if ctx.run([FWUPDMGR, "refresh", "--force", *FWUPD_OPTIONS], "firmware", timeout=FWUPD_TIMEOUT, check=False):
        ctx.report.message("warning", "fwupd metadata refresh failed (continuing with cached metadata)")
    try:
        updates = firmware_updates(timeout=FWUPD_TIMEOUT)
    except HelperError as error:
        raise TaskError(f"Could not check for firmware updates: {error}") from error
    if not updates:
        ctx.report.message("ok", "No firmware updates available")
        return TaskResult(count=0, message="No firmware updates")
    devices = [update.device for update in updates]
    for update in updates:
        ctx.report.message("info", f"  firmware: {update.device} {update.old_version} -> {update.new_version}")
    if ctx.options.firmware == "report":
        ctx.report.message("warning", "Firmware updates NOT applied (re-run with --yes to apply)")
        return TaskResult(count=0, warning=True, items=devices,
                          message=f"{_plural(len(updates), 'firmware update')} available but not installed")
    ctx.run([FWUPDMGR, "update", "-y", "--no-reboot-check", *FWUPD_OPTIONS], "firmware", timeout=FWUPD_TIMEOUT)
    ctx.firmware_reboot = True
    return TaskResult(count=len(updates), items=devices,
                      message=f"Updated {_join(devices)}; a restart may be needed to finish")


def task_autoremove(ctx: Context) -> TaskResult:
    wait_for_apt(ctx, "autoremove")
    try:
        packages = autoremove_candidates()
        sizes = installed_sizes() if packages else {}
    except HelperError as error:
        raise TaskError(str(error)) from error
    if not packages:
        return TaskResult(count=0, freed=0, message="No unused packages")
    ctx.run([APT_GET, *APT_OPTIONS, "-y", "-q", "autoremove", "--purge"], "autoremove",
            on_status=apt_progress(ctx, "autoremove"))
    return TaskResult(count=len(packages), freed=sum(sizes.get(package, 0) for package in packages),
                      items=packages, message=f"Removed {_plural(len(packages), 'package')}")


def task_apt_cache(ctx: Context) -> TaskResult:
    wait_for_apt(ctx, "apt_cache")
    before = tree_size(APT_ARCHIVES)
    ctx.run([APT_GET, *APT_OPTIONS, "-q", "clean"], "apt_cache")
    return TaskResult(freed=max(0, before - tree_size(APT_ARCHIVES)))


def task_snap_revisions(ctx: Context) -> TaskResult:
    if not snap_available():
        return TaskResult(message="Snap is not installed")
    try:
        revisions = disabled_snap_revisions()
    except HelperError as error:
        raise TaskError(str(error)) from error
    if not revisions:
        return TaskResult(count=0, freed=0, message="No old revisions")
    freed, removed, failures = 0, [], []
    for number, (name, revision) in enumerate(revisions):
        ctx.report.message("info", f"Removing disabled snap revision: {name} (rev {revision})")
        try:
            size = allocated(os.stat(snap_file(name, revision)))
        except OSError:
            size = 0
        try:
            change_id = _start_snap_change("POST", f"/v2/snaps/{name}", {"action": "remove", "revision": revision})
            if change_id:
                wait_for_change(ctx, change_id, "snap_revisions", timeout=600,
                                base=number / len(revisions), span=1 / len(revisions))
        except (SnapdError, TaskError) as error:
            ctx.report.message("warning", f"Failed to remove {name} rev {revision}: {error}")
            failures.append(f"{name} ({revision})")
            continue
        freed += size
        removed.append(f"{name} ({revision})")
    if failures:
        raise TaskError(f"Could not remove {_join(failures)}")
    return TaskResult(count=len(removed), freed=freed, items=removed,
                      message=f"Removed {_plural(len(removed), 'old revision')}")


def task_flatpak_unused(ctx: Context) -> TaskResult:
    if not flatpak_available():
        return TaskResult(message="Flatpak is not installed")
    ctx.run([FLATPAK, "uninstall", "--unused", "--system", "-y", "--noninteractive"], "flatpak_unused", timeout=600)
    return TaskResult()


def task_journal(ctx: Context) -> TaskResult:
    before = journal_size()
    # Rotate first so the active journal files become eligible for vacuuming.
    if ctx.run([JOURNALCTL, "--rotate"], "journal", check=False):
        ctx.report.message("warning", "journal rotate had issues")
    ctx.run([JOURNALCTL, f"--vacuum-time={ctx.options.journal_days}d"], "journal")
    ctx.run([JOURNALCTL, f"--vacuum-size={ctx.options.journal_max_mb}M"], "journal")
    return TaskResult(freed=max(0, before - journal_size()))


TASK_FUNCTIONS: dict[str, Callable[[Context], TaskResult]] = {
    "apt": task_apt,
    "snap": task_snap,
    "flatpak": task_flatpak,
    "firmware": task_firmware,
    "autoremove": task_autoremove,
    "apt_cache": task_apt_cache,
    "snap_revisions": task_snap_revisions,
    "flatpak_unused": task_flatpak_unused,
    "journal": task_journal,
}


# ───────────────────────────────── Commands ─────────────────────────────────

def acquire_instance_lock() -> int | None:
    """Take the lock system-update.sh also takes, so only one update runs at a time.

    Returns the locked descriptor, None if another update holds the lock, and
    raises OSError if the lock file cannot be opened at all.
    """
    path = LOCK_FILE if os.path.isdir(os.path.dirname(LOCK_FILE)) else "/run/system-update.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _lock_or_report(report: Reporter) -> int | None:
    try:
        lock = acquire_instance_lock()
    except OSError as error:
        report.emit("fatal", reason="helper", text=f"Could not take the update lock: {error.strerror or error}")
        return None
    if lock is None:
        holder = update_running()
        report.emit("fatal", reason="busy",
                    text="Another update is already running" + (f": {holder}" if holder else ""))
    return lock


def requester() -> tuple[str, str]:
    """(user, how): who started this run, and through pkexec, sudo or directly as root."""
    for variable, via in (("PKEXEC_UID", "pkexec"), ("SUDO_UID", "sudo")):
        value = os.environ.get(variable, "")
        if value.isdigit():
            try:
                return pwd.getpwuid(int(value)).pw_name, via
            except KeyError:
                return f"uid {value}", via
    return "root", "root"


def check_disk_space(report: Reporter) -> None:
    for mount, minimum in (("/", MIN_FREE_MB), ("/boot", MIN_BOOT_FREE_MB)):
        if mount != "/" and not os.path.ismount(mount):
            continue
        try:
            info = os.statvfs(mount)
        except OSError:
            continue
        free_mb = info.f_bavail * info.f_frsize // 2**20
        if free_mb < minimum:
            report.message("warning", f"Low disk space on {mount}: {free_mb} MiB (threshold: {minimum} MiB)")
        elif mount == "/":
            report.message("info", f"Free space on /: {free_mb} MiB")


def cmd_refresh(options: argparse.Namespace, report: Reporter) -> int:
    lock = _lock_or_report(report)  # held until the helper exits
    if lock is None:
        return 3
    ctx = Context(options, report, command_env())
    report.emit("start", task="refresh")
    state, message = DONE, ""
    try:
        wait_for_apt(ctx, "refresh", timeout=REFRESH_LOCK_TIMEOUT)
        ctx.run([APT_GET, *APT_OPTIONS, "-q", "update"], "refresh",
                on_status=apt_progress(ctx, "refresh", download=(0.0, 0.9), label="Refreshing package lists"))
    except (TaskError, Stopped) as error:
        state, message = FAILED, str(error) or "Stopped"
    if options.firmware and fwupd_available():
        # Exit status 2 means the metadata was already current.
        if ctx.run([FWUPDMGR, "refresh", *FWUPD_OPTIONS], "refresh", timeout=120, check=False) not in (0, 2):
            report.message("warning", "Firmware information could not be refreshed")
    report.emit("done", task="refresh", state=state, count=None, freed=None, message=message, items=[])
    report.emit("finished", reboot_required=reboot_status()[0], reboot_packages=[], problems=int(state == FAILED),
                stopped=False, log=None)
    return 0 if state == DONE else 1


def cmd_upgrade(options: argparse.Namespace, report: Reporter) -> int:
    lock = _lock_or_report(report)  # held until the helper exits
    if lock is None:
        return 3
    log = RunLog()
    report.log = log
    if log.path:
        report.emit("log", path=log.path)
    started = time.time()
    ctx = Context(options, report, command_env(restart_services=options.restart_services))
    tasks = [task for task in TASKS if task in set(options.tasks)]
    user, via = requester()

    log.section("System Update Started")
    try:
        release = platform.freedesktop_os_release().get("PRETTY_NAME", "unknown")
    except OSError:
        release = "unknown"
    for label, value in (("Hostname", socket.gethostname()), ("Kernel", platform.release()),
                         ("Release", release), ("Log File", log.path or "none"), ("Version", VERSION),
                         ("Started", f"by {user} ({via})"), ("Tasks", ", ".join(tasks))):
        log.line("INFO", f"{label:<9}: {value}")
    if log.error:
        report.message("warning", f"Could not create a log file in {LOG_DIR}: {log.error}")
    check_disk_space(report)
    if any(task in UPDATE_TASKS for task in tasks):
        if not network_available():
            log.line("FAIL", "No network connectivity detected. Aborting.")
            report.emit("fatal", reason="offline", text="No network connection. Connect to the internet and try again.")
            log.close()
            return 3
        report.message("ok", "Network connectivity verified")

    outcomes = []
    for task in tasks:
        log.section(TITLES[task])
        if STOP.is_set():
            outcome = {"task": task, "state": SKIPPED, "count": None, "freed": None,
                       "message": "Skipped because you stopped the update", "items": []}
        else:
            report.emit("start", task=task)
            try:
                result = TASK_FUNCTIONS[task](ctx)
            except Stopped:
                result, state = TaskResult(message="Skipped because you stopped the update"), SKIPPED
            except TaskError as error:
                result, state = TaskResult(message=str(error)), FAILED
            except Exception as error:  # report it and carry on with the other tasks
                result, state = TaskResult(message=f"Unexpected error: {error}"), FAILED
            else:
                state = WARNING if result.warning else DONE
            outcome = {"task": task, "state": state, "count": result.count, "freed": result.freed,
                       "message": result.message, "items": result.items}
        outcomes.append(outcome)
        level = {DONE: "OK", WARNING: "WARN", FAILED: "FAIL", SKIPPED: "WARN"}[outcome["state"]]
        log.line(level, f"{TITLES[task]}: {outcome['state']}" + (f" ({outcome['message']})" if outcome["message"] else ""))
        report.emit("done", **outcome)

    elapsed = round(time.time() - started)
    problems = sum(1 for outcome in outcomes if outcome["state"] == FAILED)
    reboot, packages = reboot_status()
    reboot = reboot or ctx.firmware_reboot
    log.section("Summary")
    if problems:
        log.line("WARN", f"{problems} task(s) failed after {elapsed}s — review log: {log.path}")
    else:
        log.line("OK", f"All tasks completed successfully in {elapsed}s")
    if reboot:
        log.line("WARN", "*** REBOOT REQUIRED ***")
        for package in packages:
            log.line("INFO", f"  {package}")
    else:
        log.line("OK", "No reboot required")
    log.line("INFO", f"Full log: {log.path}")
    try:
        append_history({
            "version": 1, "started": started, "finished": time.time(), "user": user, "via": via,
            "tasks": outcomes, "reboot_required": reboot, "log": log.path, "stopped": STOP.is_set(),
        })
    except OSError as error:
        report.message("warning", f"Could not update the run history: {error.strerror or error}")
    log.close()
    report.emit("finished", reboot_required=reboot, reboot_packages=packages, problems=problems,
                stopped=STOP.is_set(), log=log.path, elapsed=elapsed)
    return 1 if problems else 0


def watch_stdin(report: Reporter) -> None:
    """A "stop" line on stdin finishes the current task and skips the rest."""
    def read() -> None:
        try:
            for line in sys.stdin:
                if line.strip() == "stop" and not STOP.is_set():
                    STOP.set()
                    report.emit("stopping")
        except (OSError, ValueError):
            pass

    threading.Thread(target=read, name="stdin", daemon=True).start()


def _bounded_int(low: int, high: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        if not re.fullmatch(r"[0-9]{1,6}", text) or not low <= int(text) <= high:
            raise argparse.ArgumentTypeError(f"must be a whole number from {low} to {high}")
        return int(text)

    return parse


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="system-update-helper", allow_abbrev=False,
        description="Privileged helper for System Update. Run `system-update` instead.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    refresh = commands.add_parser("refresh", allow_abbrev=False, help="refresh package information")
    refresh.add_argument("--firmware", action="store_true", help="also refresh firmware information")
    upgrade = commands.add_parser("upgrade", allow_abbrev=False, help="install updates and clean up")
    upgrade.add_argument("--journal-days", type=_bounded_int(1, 365), default=7)
    upgrade.add_argument("--journal-max-mb", type=_bounded_int(16, 100_000), default=200)
    upgrade.add_argument("--firmware", choices=("install", "report"), default="install")
    upgrade.add_argument("--restart-services", action="store_true")
    upgrade.add_argument("tasks", nargs="+", choices=TASKS, metavar="TASK", help=", ".join(TASKS))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    options = parse_args(sys.argv[1:] if argv is None else argv)
    if os.geteuid() != 0:
        print("system-update-helper must run as root; start `system-update` instead.", file=sys.stderr)
        return 2
    # Once started, finish: an interrupted apt or dpkg run is worse than a slow one.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    os.umask(0o022)
    os.chdir("/")
    report = Reporter(sys.stdout)
    watch_stdin(report)
    if options.command == "refresh":
        return cmd_refresh(options, report)
    return cmd_upgrade(options, report)


if __name__ == "__main__":
    sys.exit(main())

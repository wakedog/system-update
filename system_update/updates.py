"""What System Update can do, and the check for available updates.

The check never changes anything and needs no password: it asks apt to
simulate the upgrade, and asks snapd, Flatpak and fwupd what they would
update, using the same queries the privileged helper uses.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from system_update import helper
from system_update.settings import Settings
from system_update.util import format_size, plural

UPDATES, MAINTENANCE = "updates", "maintenance"


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    icon: str
    group: str  # UPDATES or MAINTENANCE
    describe: Callable[[Settings], str]
    default: bool = True
    available: Callable[[], bool] = lambda: True


def _snap() -> bool:
    return helper.snap_available()


def _flatpak() -> bool:
    return helper.flatpak_available()


def _fwupd() -> bool:
    return helper.fwupd_available()


TASKS = (
    Task("apt", "System Packages", "package-x-generic-symbolic", UPDATES,
         lambda s: "Ubuntu and the software installed with APT"),
    Task("snap", "Snap Packages", "application-x-executable-symbolic", UPDATES,
         lambda s: "Apps and runtimes from the Snap Store", available=_snap),
    Task("flatpak", "Flatpak Apps", "application-x-addon-symbolic", UPDATES,
         lambda s: "Apps and runtimes from Flatpak remotes such as Flathub", available=_flatpak),
    Task("firmware", "Firmware", "application-x-firmware-symbolic", UPDATES,
         lambda s: "Updates for the computer and its devices; some need a restart", default=False,
         available=_fwupd),
    Task("autoremove", "Remove Unused Packages", "user-trash-symbolic", MAINTENANCE,
         lambda s: "Installed automatically, and nothing needs them anymore"),
    Task("apt_cache", "Clear Package Cache", "folder-download-symbolic", MAINTENANCE,
         lambda s: "Downloaded package files that were already installed"),
    Task("snap_revisions", "Remove Old Snap Revisions", "document-open-recent-symbolic", MAINTENANCE,
         lambda s: "Previous versions snapd keeps after updates", available=_snap),
    Task("flatpak_unused", "Remove Unused Runtimes", "edit-clear-all-symbolic", MAINTENANCE,
         lambda s: "Flatpak runtimes and extensions that no app uses anymore", available=_flatpak),
    Task("journal", "Trim System Journal", "text-x-generic-symbolic", MAINTENANCE,
         lambda s: f"Keep {plural(s.journal_keep_days, 'day')} of system logs, at most {s.journal_max_mb} MB"),
)
# Not selectable on its own: runs with "flatpak" when you have apps installed just for yourself.
PERSONAL_FLATPAK = Task("flatpak_user", "Your Flatpak Apps", "application-x-addon-symbolic", UPDATES,
                        lambda s: "Flatpak apps installed only for you", available=_flatpak)
BY_ID = {task.id: task for task in (*TASKS, PERSONAL_FLATPAK)}


def is_selected(settings: Settings, task: Task) -> bool:
    return settings.selection.get(task.id, task.default)


# ──────────────────────────────── Results ──────────────────────────────

@dataclass
class Update:
    name: str
    old_version: str = ""
    new_version: str = ""
    detail: str = ""
    size: int | None = None
    security: bool = False
    kind: str = "upgrade"  # "upgrade", "install" or "remove"
    personal: bool = False  # in your own Flatpak installation


@dataclass
class SourceStatus:
    id: str
    available: bool = True
    updates: list[Update] = field(default_factory=list)
    download_size: int | None = None
    error: str = ""
    repair: bool = False  # an interrupted installation will be completed first

    @property
    def count(self) -> int:
        return sum(1 for update in self.updates if update.kind != "remove")

    @property
    def security(self) -> int:
        return sum(1 for update in self.updates if update.security)

    @property
    def removals(self) -> list[Update]:
        return [update for update in self.updates if update.kind == "remove"]

    @property
    def actionable(self) -> bool:
        return bool(self.updates) or self.repair

    def summary(self) -> str:
        if self.error:
            return f"Could not check: {self.error}"
        if self.repair and not self.updates:
            return "An interrupted installation will be completed"
        if not self.updates:
            return "Up to date"
        parts = [plural(self.count, "update")]
        if self.security:
            parts.append(f"{self.security} security")
        if self.removals:
            parts.append(f"{len(self.removals)} to remove")
        if self.download_size:
            parts.append(f"{format_size(self.download_size)} to download")
        return " · ".join(parts)


@dataclass
class CleanupStatus:
    id: str
    available: bool = True
    size: int | None = None  # what it frees, when that is known in advance
    count: int | None = None
    note: str = ""


@dataclass
class CheckResult:
    sources: dict[str, SourceStatus] = field(default_factory=dict)
    cleanup: dict[str, CleanupStatus] = field(default_factory=dict)
    checked: float = 0.0
    lists_updated: float | None = None
    reboot_required: bool = False
    reboot_packages: list[str] = field(default_factory=list)
    running: str = ""  # another update is running (system-update.sh, a timer…)
    busy: list[str] = field(default_factory=list)  # other programs using apt right now
    refresh_error: str = ""

    @property
    def total(self) -> int:
        return sum(source.count for source in self.sources.values())

    @property
    def security(self) -> int:
        return sum(source.security for source in self.sources.values())

    def _sizes(self, ids: list[str] | None) -> list[int | None]:
        return [source.download_size for source in self.sources.values()
                if source.count and (ids is None or source.id in ids)]

    def download_size(self, ids: list[str] | None = None) -> int | None:
        """What the given sources (or all) will download, as far as it is known."""
        known = [size for size in self._sizes(ids) if size is not None]
        return sum(known) if known else None

    def download_complete(self, ids: list[str] | None = None) -> bool:
        """Whether download_size() includes every source (Flatpak cannot tell in advance)."""
        return None not in self._sizes(ids)

    def to_json(self) -> dict:
        return {
            "checked": self.checked,
            "lists_updated": self.lists_updated,
            "total": self.total,
            "security": self.security,
            "reboot_required": self.reboot_required,
            "reboot_packages": self.reboot_packages,
            "update_running": self.running or None,
            "refresh_error": self.refresh_error or None,
            "sources": [
                {
                    "id": source.id, "title": BY_ID[source.id].title, "count": source.count,
                    "security": source.security, "download_size": source.download_size,
                    "error": source.error or None, "repair": source.repair,
                    "updates": [update.__dict__ for update in source.updates],
                }
                for source in self.sources.values() if source.available
            ],
        }


# ──────────────────────────────── Checking ─────────────────────────────

def check_apt() -> SourceStatus:
    status = SourceStatus("apt")
    changes, error = helper.simulate_upgrade()
    if helper.needs_repair(error):
        status.repair = True
    elif error:
        status.error = error
    status.updates = [
        Update(change.name, change.old_version, change.new_version, detail=change.archive,
               security=change.security, kind=change.kind)
        for change in changes
    ]
    if status.updates:
        status.download_size = helper.download_size()
    return status


def check_snap() -> SourceStatus:
    if not helper.snap_available():
        return SourceStatus("snap", available=False)
    try:
        refreshes = helper.snap_refreshes()
    except helper.HelperError as error:
        return SourceStatus("snap", error=str(error))
    updates = [Update(snap.name, snap.old_version, snap.version, detail=snap.publisher, size=snap.size)
               for snap in refreshes]
    sizes = [update.size for update in updates]
    return SourceStatus("snap", updates=updates,
                        download_size=None if None in sizes else sum(sizes) if sizes else None)


def check_flatpak() -> SourceStatus:
    if not helper.flatpak_available():
        return SourceStatus("flatpak", available=False)
    status = SourceStatus("flatpak")
    for installation in ("system", "user"):
        try:
            found = helper.flatpak_updates(installation)
        except helper.HelperError as error:
            if installation == "system":
                status.error = str(error)
            continue
        status.updates += [
            Update(update.name, update.old_version, update.version,
                   detail=f"{update.application} ({update.branch})" + (" · installed for you" if
                                                                       installation == "user" else ""),
                   personal=installation == "user")
            for update in found
        ]
    return status


def check_firmware() -> SourceStatus:
    if not helper.fwupd_available():
        return SourceStatus("firmware", available=False)
    try:
        found = helper.firmware_updates(timeout=90)
    except helper.HelperError as error:
        return SourceStatus("firmware", error=str(error))
    updates = [Update(update.device, update.old_version, update.new_version, detail=update.summary,
                      size=update.size) for update in found]
    sizes = [update.size for update in updates]
    return SourceStatus("firmware", updates=updates,
                        download_size=None if None in sizes else sum(sizes) if sizes else None)


def estimate_cleanup() -> dict[str, CleanupStatus]:
    """What each maintenance task would remove right now, where it can be told cheaply."""
    estimates = {"apt_cache": CleanupStatus("apt_cache", size=helper.tree_size(helper.APT_ARCHIVES))}
    try:
        packages = helper.autoremove_candidates()
        sizes = helper.installed_sizes() if packages else {}
        estimates["autoremove"] = CleanupStatus("autoremove", count=len(packages),
                                                size=sum(sizes.get(package, 0) for package in packages))
    except helper.HelperError:
        estimates["autoremove"] = CleanupStatus("autoremove")
    if helper.snap_available():
        try:
            revisions = helper.disabled_snap_revisions()
            size = 0
            for name, revision in revisions:
                try:
                    size += helper.allocated(os.stat(helper.snap_file(name, revision)))
                except OSError:
                    pass
            estimates["snap_revisions"] = CleanupStatus("snap_revisions", count=len(revisions), size=size)
        except helper.HelperError:
            estimates["snap_revisions"] = CleanupStatus("snap_revisions")
    else:
        estimates["snap_revisions"] = CleanupStatus("snap_revisions", available=False)
    estimates["flatpak_unused"] = CleanupStatus("flatpak_unused", available=helper.flatpak_available())
    used = helper.journal_size()
    estimates["journal"] = CleanupStatus("journal", note=f"The journal uses {format_size(used)}" if used else "")
    return estimates


CHECKS: dict[str, Callable[[], SourceStatus]] = {
    "apt": check_apt,
    "snap": check_snap,
    "flatpak": check_flatpak,
    "firmware": check_firmware,
}


def check(*, firmware: bool = True) -> CheckResult:
    """Ask every source what it would update. Runs the sources in parallel."""
    result = CheckResult(checked=time.time(), lists_updated=helper.lists_updated())
    ids = [task_id for task_id in CHECKS if firmware or task_id != "firmware"]
    with ThreadPoolExecutor(max_workers=len(ids) + 1, thread_name_prefix="check") as pool:
        futures = {task_id: pool.submit(CHECKS[task_id]) for task_id in ids}
        cleanup = pool.submit(estimate_cleanup)
        for task_id, future in futures.items():
            try:
                result.sources[task_id] = future.result()
            except Exception as error:  # one broken source must not hide the others
                result.sources[task_id] = SourceStatus(task_id, error=str(error) or type(error).__name__)
        try:
            result.cleanup = cleanup.result()
        except Exception:
            result.cleanup = {}
    if not firmware:
        result.sources["firmware"] = SourceStatus("firmware", available=False)
    result.reboot_required, result.reboot_packages = helper.reboot_status()
    holder = helper.update_running()
    result.running = str(holder) if holder else ""
    result.busy = [str(holder) for holder in helper.apt_lock_holders()]
    return result

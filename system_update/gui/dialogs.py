"""About, preferences, history and keyboard shortcut dialogs, and restarting."""

from __future__ import annotations

import os
import platform
from typing import Callable

from gi.repository import Adw, Gio, GLib, Gtk

from system_update import APP_ID, APP_NAME, HOMEPAGE, __version__, helper, history, paths
from system_update.gui.widgets import format_timestamp, status_icon, uses_twelve_hour_clock
from system_update.settings import Settings
from system_update.updates import BY_ID
from system_update.util import format_duration, format_size, plural

NEEDRESTART = "/usr/sbin/needrestart"
POLICY = f"/usr/share/polkit-1/actions/{APP_ID}.policy"

RELEASE_NOTES = """\
<p>System Update is now a desktop app.</p>
<ul>
<li>See every available update before installing: system packages, snaps, Flatpak apps and firmware</li>
<li>Live progress for each step, with the full command output one click away</li>
<li>Stop after the current step at any time; package installation is never interrupted</li>
<li>A reminder when the computer needs a restart, and a history of every update run</li>
<li>A command line that accepts the options of system-update.sh, for timers and scripts</li>
</ul>
"""

SHORTCUTS = (
    ("Check for Updates", "<Control>r"),
    ("Install Updates", "<Control>Return"),
    ("Update History", "<Control>h"),
    ("Preferences", "<Control>comma"),
    ("Keyboard Shortcuts", "<Control>question"),
    ("Close Window", "<Control>w"),
    ("Quit", "<Control>q"),
)


def open_path(parent: Gtk.Window, path: str | os.PathLike, on_error: Callable[[str], None] | None = None) -> None:
    if not os.access(path, os.R_OK):
        if on_error:
            on_error("Only administrators can read update logs (members of the adm group)")
        return
    launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(os.fspath(path)))

    def done(launcher: Gtk.FileLauncher, result: Gio.AsyncResult) -> None:
        try:
            launcher.launch_finish(result)
        except GLib.Error as error:
            if not error.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED) and on_error:
                on_error(f"Could not open {path}: {error.message}")

    launcher.launch(parent, None, done)


def request_restart(parent: Gtk.Window, on_error: Callable[[str], None]) -> None:
    """Show GNOME's own restart dialog, or ask for confirmation and restart through logind."""
    def session_done(bus: Gio.DBusConnection, result: Gio.AsyncResult) -> None:
        try:
            bus.call_finish(result)
        except GLib.Error:
            confirm_restart(parent, on_error)

    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error:
        confirm_restart(parent, on_error)
        return
    bus.call("org.gnome.SessionManager", "/org/gnome/SessionManager", "org.gnome.SessionManager", "Reboot",
             None, None, Gio.DBusCallFlags.NONE, 10_000, None, session_done)


def confirm_restart(parent: Gtk.Window, on_error: Callable[[str], None]) -> None:
    dialog = Adw.AlertDialog.new("Restart Now?", "Save your work first. Open apps will be closed.")
    dialog.add_response("cancel", "_Cancel")
    dialog.add_response("restart", "_Restart")
    dialog.set_response_appearance("restart", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def logind_done(bus: Gio.DBusConnection, result: Gio.AsyncResult) -> None:
        try:
            bus.call_finish(result)
        except GLib.Error as error:
            on_error(f"Could not restart: {error.message}")

    def response(_dialog, answer: str) -> None:
        if answer != "restart":
            return
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error as error:
            on_error(f"Could not restart: {error.message}")
            return
        bus.call("org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager", "Reboot",
                 GLib.Variant("(b)", (True,)), None, Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION, 60_000,
                 None, logind_done)

    dialog.connect("response", response)
    dialog.present(parent)


def _debug_info() -> str:
    try:
        system = platform.freedesktop_os_release().get("PRETTY_NAME", "Linux")
    except OSError:
        system = "Linux"
    runs = history.load()
    tools = ", ".join(f"{name} {'yes' if available else 'no'}" for name, available in (
        ("snap", helper.snap_available()), ("flatpak", helper.flatpak_available()),
        ("fwupd", helper.fwupd_available()), ("needrestart", os.path.exists(NEEDRESTART)),
    ))
    return "\n".join((
        f"{APP_NAME} {__version__}",
        f"Installed: {'yes' if paths.IS_INSTALLED else f'no, running from {paths.SOURCE_ROOT}'}",
        f"System: {system}",
        f"Python {platform.python_version()}",
        f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}",
        f"libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}.{Adw.get_micro_version()}",
        f"Helper: {' '.join(paths.helper_command())}",
        f"Polkit policy: {POLICY} ({'present' if os.path.exists(POLICY) else 'missing'})",
        f"Logs: {helper.LOG_DIR} ({'readable' if os.access(helper.LOG_DIR, os.R_OK) else 'not readable'})",
        f"History: {helper.HISTORY_FILE} ({plural(len(runs), 'run')})",
        f"Tools: {tools}",
    ))


def show_about(parent: Gtk.Widget) -> None:
    about = Adw.AboutDialog(
        application_name=APP_NAME,
        application_icon=APP_ID,
        version=__version__,
        developer_name="Bryan",
        copyright="© 2026 wakedog",
        website=HOMEPAGE,
        issue_url=HOMEPAGE + "/issues",
        license_type=Gtk.License.MIT_X11,
        comments="Keep Ubuntu up to date in one place: system packages, snaps, Flatpak apps and firmware, "
                 "followed by a tidy-up of what the updates leave behind.",
        release_notes_version=__version__,
        release_notes=RELEASE_NOTES,
        debug_info=_debug_info(),
        debug_info_filename="system-update-debug-info.txt",
    )
    about.present(parent)


def show_shortcuts(parent: Gtk.Widget) -> None:
    dialog = Adw.ShortcutsDialog()
    section = Adw.ShortcutsSection.new("General")
    for title, accelerator in SHORTCUTS:
        section.add(Adw.ShortcutsItem.new(title, accelerator))
    dialog.add(section)
    dialog.present(parent)


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, settings: Settings, on_closed: Callable[[bool], None]):
        super().__init__(title="Preferences", search_enabled=False)
        self.settings = settings
        self.changed = False

        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        checking = Adw.PreferencesGroup(title="Checking for Updates")
        on_start = Adw.SwitchRow(title="Check When Opened",
                                 subtitle="Refresh package information when System Update opens, "
                                          "if it is more than an hour old")
        on_start.set_active(settings.check_on_start)
        on_start.connect("notify::active", self._on_switch, "check_on_start")
        checking.add(on_start)
        page.add(checking)

        if os.path.exists(NEEDRESTART):
            services = Adw.PreferencesGroup(title="After Updating")
            restart = Adw.SwitchRow(title="Restart Services Automatically",
                                    subtitle="Restart background services that still use replaced libraries. "
                                             "Otherwise they are only listed in the log.")
            restart.set_active(settings.restart_services)
            restart.connect("notify::active", self._on_switch, "restart_services")
            services.add(restart)
            page.add(services)

        journal = Adw.PreferencesGroup(title="System Journal",
                                       description="How much system log history Trim System Journal keeps.")
        journal.add(self._number_row("journal_keep_days", "Days to Keep", "Older entries are removed", 1, 365, 1))
        journal.add(self._number_row("journal_max_mb", "Maximum Size",
                                     "In megabytes; the oldest entries go first", 16, 100_000, 16))
        page.add(journal)

        selection = Adw.PreferencesGroup(
            title="Selection",
            description="Your choices in the main window are remembered for the next check.",
        )
        reset = Adw.ActionRow(title="Restore Default Selection", activatable=True,
                              subtitle="Include every update source and maintenance task except firmware")
        reset.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        reset.connect("activated", self._on_reset)
        selection.add(reset)
        page.add(selection)
        self.add(page)
        self.connect("closed", lambda *_: on_closed(self.changed))

    def _number_row(self, name: str, title: str, subtitle: str, low: int, high: int, step: int) -> Adw.SpinRow:
        row = Adw.SpinRow.new_with_range(low, high, step)
        row.set_title(title)
        row.set_subtitle(subtitle)
        row.set_value(getattr(self.settings, name))
        row.connect("notify::value", self._on_value_changed, name)
        return row

    def _on_value_changed(self, row: Adw.SpinRow, _pspec, name: str) -> None:
        value = int(row.get_value())
        if value != getattr(self.settings, name):
            setattr(self.settings, name, value)
            self.changed = True
            self._save()

    def _on_switch(self, row: Adw.SwitchRow, _pspec, name: str) -> None:
        if row.get_active() != getattr(self.settings, name):
            setattr(self.settings, name, row.get_active())
            self.changed = True
            self._save()

    def _on_reset(self, _row) -> None:
        if self.settings.selection:
            self.settings.selection.clear()
            self.changed = True
            self._save()
        self.add_toast(Adw.Toast.new("Default selection restored"))

    def _save(self) -> None:
        try:
            self.settings.save()
        except OSError as error:
            self.add_toast(Adw.Toast.new(f"Could not save preferences: {error.strerror}"))


def run_subtitle(run: history.Run) -> str:
    parts = [f"{plural(run.installed, 'update')} installed" if run.installed else "Nothing to update"]
    if run.freed:
        parts.append(f"{format_size(run.freed)} freed")
    parts.append("by a timer or as root" if run.unattended else f"by {run.user}")
    if run.problems:
        parts.append(plural(len(run.problems), "problem"))
    elif run.stopped:
        parts.append("stopped")
    return " · ".join(parts)


class HistoryDialog(Adw.Dialog):
    def __init__(self, parent: Gtk.Window, on_error: Callable[[str], None]):
        super().__init__(title="Update History", content_width=600, content_height=640)
        self.parent_window = parent
        self.on_error = on_error
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        runs = history.load()
        if runs:
            view.set_content(self._build_list(runs))
        else:
            view.set_content(Adw.StatusPage(
                icon_name="document-open-recent-symbolic",
                title="No Updates Yet",
                description="Every update run is listed here, including the ones a timer starts, "
                            "with a log of everything it did.",
            ))
        self.set_child(view)

    def _build_list(self, runs: list[history.Run]) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        totals = Adw.PreferencesGroup()
        total_row = Adw.ActionRow(title="Updates Installed", subtitle=f"Across {plural(len(runs), 'run')}")
        total = Gtk.Label(label=f"{sum(run.installed for run in runs):,}", valign=Gtk.Align.CENTER)
        total.add_css_class("title-3")
        total.add_css_class("numeric")
        total_row.add_suffix(total)
        totals.add(total_row)
        page.add(totals)

        group = Adw.PreferencesGroup(title="Runs")
        twelve_hour = uses_twelve_hour_clock()
        for run in runs[:200]:
            row = Adw.ExpanderRow(title=format_timestamp(run.started, twelve_hour), subtitle=run_subtitle(run),
                                  use_markup=False)
            if run.problems:
                row.add_suffix(status_icon("dialog-warning-symbolic", "warning", "Some tasks failed"))
            elif run.reboot_required:
                row.add_suffix(status_icon("system-reboot-symbolic", "accent", "Needed a restart"))
            if run.log:
                button = Gtk.Button(icon_name="text-x-generic-symbolic", tooltip_text="Open Log",
                                    valign=Gtk.Align.CENTER)
                button.add_css_class("flat")
                button.connect("clicked", lambda _b, path=run.log: open_path(self.parent_window, path, self.on_error))
                row.add_suffix(button)
            took = Adw.ActionRow(title="Duration", subtitle=format_duration(run.finished - run.started),
                                 use_markup=False)
            row.add_row(took)
            for record in run.tasks:
                task = BY_ID.get(record.task)
                child = Adw.ActionRow(title=task.title if task else record.task, use_markup=False,
                                      subtitle=record.message or record.state.capitalize(), subtitle_lines=2)
                if task:
                    icon = Gtk.Image(icon_name=task.icon)
                    icon.add_css_class("dim-label")
                    child.add_prefix(icon)
                if record.freed:
                    size = Gtk.Label(label=format_size(record.freed), valign=Gtk.Align.CENTER)
                    size.add_css_class("dim-label")
                    size.add_css_class("numeric")
                    child.add_suffix(size)
                row.add_row(child)
            group.add(row)
        page.add(group)
        return page

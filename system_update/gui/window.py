"""The main window: check, review, install, done."""

from __future__ import annotations

import logging
import threading

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from system_update import APP_NAME, engine, helper, history, paths, updates
from system_update.gui import dialogs
from system_update.gui.widgets import (LogView, SelectableRow, SummaryCard, TaskRow, cleanup_row, download_text,
                                       relative_day, source_row, spinner)
from system_update.settings import Settings
from system_update.updates import BY_ID, UPDATES, CheckResult, SourceStatus
from system_update.util import format_duration, format_size, plural

log = logging.getLogger(__name__)

CHECKING, RESULTS, UPDATING, DONE = "checking", "results", "updating", "done"
# Rough share of the time each step takes, for the overall progress bar.
WEIGHTS = {"apt": 6, "snap": 3, "flatpak": 3, "firmware": 3, "flatpak_user": 2}


def summary_text(report: engine.RunReport) -> str:
    parts = [f"{plural(report.installed, 'update')} installed" if report.installed
             else "Everything was already up to date"]
    if report.freed:
        parts.append(f"{format_size(report.freed)} freed")
    parts.append(f"took {format_duration(report.finished - report.started)}")
    return " · ".join(parts)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application):
        super().__init__(application=application, title=APP_NAME, default_width=700, default_height=820)
        self.set_size_request(360, 500)
        self.settings = Settings.load()
        self.result: CheckResult | None = None
        self.rows: dict[str, SelectableRow] = {}
        self.task_rows: dict[str, TaskRow] = {}
        self.state = CHECKING
        self.run: engine.Run | None = None
        self.report: engine.RunReport | None = None
        self._check_token: object | None = None
        self._weights: dict[str, float] = {}
        self._fractions: dict[str, float] = {}
        self._progress_tasks: set[str] = set()
        self._output_lock = threading.Lock()
        self._pending_lines: list[str] = []
        self._activity: tuple[str, str] | None = None
        self._flush_scheduled = False
        self._inhibit_cookie = 0
        self._running_watch = 0
        self._banner_action = ""
        self.network = Gio.NetworkMonitor.get_default()

        self._build()
        self._add_actions()
        self.connect("close-request", self._on_close_request)
        self.network.connect("network-changed", lambda *_: self._update_banner())
        self._refresh_subtitle()
        GLib.idle_add(self._initial_check)

    # ───────────────────────────── Layout ─────────────────────────────

    def _build(self) -> None:
        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        self.view = Adw.ToolbarView()
        self.toasts.set_child(self.view)

        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title=APP_NAME)
        header.set_title_widget(self.window_title)
        header.pack_start(Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Check for Updates",
                                     action_name="win.check"))
        header.pack_end(self._build_menu_button())
        self.view.add_top_bar(header)
        self.banner = Adw.Banner(revealed=False)
        self.banner.connect("button-clicked", self._on_banner_clicked)
        self.view.add_top_bar(self.banner)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self._build_checking_page(), CHECKING)
        self.stack.add_named(self._build_results_page(), RESULTS)
        self.stack.add_named(self._build_updating_page(), UPDATING)
        self.view.set_content(self.stack)

        bar = Gtk.ActionBar()
        self.selection_label = Gtk.Label(xalign=0, margin_start=6, ellipsize=Pango.EllipsizeMode.END)
        bar.pack_start(self.selection_label)
        self.install_button = Gtk.Button(label="_Install Updates", use_underline=True, action_name="win.install")
        self.install_button.add_css_class("suggested-action")
        bar.pack_end(self.install_button)
        self.view.add_bottom_bar(bar)
        self.view.set_bottom_bar_style(Adw.ToolbarStyle.RAISED)
        self.view.set_reveal_bottom_bars(False)

    def _build_menu_button(self) -> Gtk.MenuButton:
        menu = Gio.Menu()
        main = Gio.Menu()
        main.append("_Preferences", "app.preferences")
        main.append("Update _History", "win.history")
        main.append("Open _Log Folder", "app.open-logs")
        menu.append_section(None, main)
        about = Gio.Menu()
        if hasattr(Adw, "ShortcutsDialog"):
            about.append("_Keyboard Shortcuts", "app.shortcuts")
        about.append(f"_About {APP_NAME}", "app.about")
        menu.append_section(None, about)
        return Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True,
                              tooltip_text="Main Menu")

    def _build_checking_page(self) -> Gtk.Widget:
        self.checking_page = Adw.StatusPage(title="Checking for Updates…")
        if hasattr(Adw, "SpinnerPaintable"):
            self.checking_page.set_paintable(Adw.SpinnerPaintable.new(self.checking_page))
        else:
            self.checking_page.set_icon_name("software-update-available-symbolic")
        return self.checking_page

    def _build_results_page(self) -> Gtk.Widget:
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                          margin_top=24, margin_bottom=24, margin_start=12, margin_end=12)
        self.summary = SummaryCard()
        content.append(self.summary)
        self.updates_group = Adw.PreferencesGroup(
            title="Updates", description="Choose what to update. Installing asks for your password.")
        self.maintenance_group = Adw.PreferencesGroup(
            title="Maintenance", description="Runs after the updates, to tidy up what they leave behind")
        content.append(self.updates_group)
        content.append(self.maintenance_group)
        clamp = Adw.Clamp(maximum_size=720, tightening_threshold=560, child=content)
        return Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, child=clamp)

    def _build_updating_page(self) -> Gtk.Widget:
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                          margin_top=36, margin_bottom=24, margin_start=12, margin_end=12)
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.update_icon = Gtk.Stack(halign=Gtk.Align.CENTER, transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.update_icon.add_named(spinner(64), "busy")
        for name, icon_name in (("success", "object-select-symbolic"), ("warning", "dialog-warning-symbolic"),
                                ("error", "dialog-error-symbolic")):
            image = Gtk.Image(icon_name=icon_name, pixel_size=48)
            image.add_css_class("status-badge")
            image.add_css_class(name)
            self.update_icon.add_named(image, name)
        self.update_title = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.update_title.add_css_class("title-1")
        self.update_description = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.update_description.add_css_class("dim-label")
        self.progress = Gtk.ProgressBar(margin_top=6, margin_start=24, margin_end=24)
        for widget in (self.update_icon, self.update_title, self.update_description, self.progress):
            header.append(widget)
        content.append(header)

        # Controls sit above the list of steps so they never scroll out of view.
        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        self.stop_button = Gtk.Button(label="_Stop After This Step", use_underline=True, action_name="win.stop",
                                      tooltip_text="Finish the current step and skip the rest. "
                                                   "Installing packages is never interrupted.")
        self.log_button = Gtk.Button(label="View _Log", use_underline=True)
        self.log_button.connect("clicked", lambda *_: self._open_report_log())
        self.again_button = Gtk.Button(label="_Check Again", use_underline=True, action_name="win.check")
        self.restart_button = Gtk.Button(label="_Restart…", use_underline=True, action_name="win.restart")
        self.restart_button.add_css_class("suggested-action")
        for button in (self.stop_button, self.log_button, self.again_button, self.restart_button):
            button.add_css_class("pill")
            buttons.append(button)
        self.log_view = LogView()
        controls.append(buttons)
        controls.append(self.log_view.toggle)
        content.append(controls)
        content.append(self.log_view)

        self.task_group = Adw.PreferencesGroup()
        content.append(self.task_group)

        clamp = Adw.Clamp(maximum_size=600, child=content)
        return Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, child=clamp)

    def _add_actions(self) -> None:
        for name, callback in (("check", lambda: self.start_check(refresh=True)),
                               ("install", self.confirm_install), ("history", self.show_history),
                               ("restart", lambda: dialogs.request_restart(self, self.toast)),
                               ("stop", self.stop_update)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _action, _param, callback=callback: callback())
            self.add_action(action)
        self._sync_actions()

    def _sync_actions(self) -> None:
        busy = self.state in (CHECKING, UPDATING)
        self.lookup_action("check").set_enabled(not busy)
        running_elsewhere = bool(self.result and self.result.running)
        self.lookup_action("install").set_enabled(
            self.state == RESULTS and bool(self.selected_ids()) and not running_elsewhere)
        self.lookup_action("stop").set_enabled(self.state == UPDATING and self.run is not None
                                               and not self.run.stopping)

    def _show(self, state: str) -> None:
        self.state = state
        self.stack.set_visible_child_name(UPDATING if state == DONE else state)
        self.view.set_reveal_bottom_bars(state == RESULTS)
        self._sync_actions()
        self._update_banner()

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast.new(text))

    def _refresh_subtitle(self) -> None:
        runs = [run for run in history.load() if not run.problems]
        self.window_title.set_subtitle(f"Last updated {relative_day(runs[0].started)}" if runs else "")

    def _update_banner(self) -> None:
        result = self.result
        reboot = (self.report.reboot_required if self.state == DONE and self.report else
                  bool(result and result.reboot_required))
        title, button, action = "", "", ""
        if self.state == UPDATING or self.state == CHECKING:
            pass
        elif result and result.running and self.state == RESULTS:
            title = f"Another update is running ({result.running}). You can install again when it finishes."
        elif reboot:
            title, button, action = "Restart the computer to finish installing updates", "_Restart…", "restart"
        elif not self.network.get_network_available() and self.state == RESULTS:
            title = "No internet connection. Updates cannot be downloaded until you connect."
        elif result and result.busy and self.state == RESULTS:
            title = (f"{', '.join(result.busy)} is using the package system. "
                     "Installing will wait until it finishes.")
        self._banner_action = action
        if title:
            self.banner.set_title(title)
            self.banner.set_button_label(button or None)
        self.banner.set_revealed(bool(title))

    def _on_banner_clicked(self, _banner) -> None:
        if self._banner_action == "restart":
            dialogs.request_restart(self, self.toast)

    # ──────────────────────────── Checking ────────────────────────────

    def _initial_check(self) -> bool:
        self.start_check(refresh=None)
        return GLib.SOURCE_REMOVE

    def start_check(self, refresh: bool | None = None) -> None:
        """Check for updates; refresh=None refreshes package information only when it is stale."""
        if self.state == UPDATING:
            return
        if refresh is None:
            refresh = self.settings.check_on_start and paths.IS_INSTALLED and engine.lists_are_stale()
        token = object()
        self._check_token = token
        self.checking_page.set_description("Refreshing package information…" if refresh else "Looking for updates…")
        self._show(CHECKING)
        threading.Thread(target=self._check_worker, args=(token, refresh), daemon=True).start()

    def _check_worker(self, token: object, refresh: bool) -> None:
        def progress(text: str) -> None:
            GLib.idle_add(self._on_check_progress, token, text)

        try:
            result = engine.check(refresh_first=refresh, on_progress=progress)
        except Exception as error:
            log.exception("Checking for updates failed")
            result = CheckResult(sources={"apt": SourceStatus("apt", error=str(error) or type(error).__name__)})
        GLib.idle_add(self._on_check_finished, token, result)

    def _on_check_progress(self, token: object, text: str) -> bool:
        if token is self._check_token:
            self.checking_page.set_description(text)
        return GLib.SOURCE_REMOVE

    def _on_check_finished(self, token: object, result: CheckResult) -> bool:
        if token is not self._check_token:
            return GLib.SOURCE_REMOVE
        self._check_token = None
        self.result = result
        self.report = None
        self._show(RESULTS)
        self._populate()
        if result.refresh_error:
            self.toast(f"Package information could not be refreshed: {result.refresh_error}")
        if result.running and not self._running_watch:
            self._running_watch = GLib.timeout_add_seconds(5, self._poll_running)
        return GLib.SOURCE_REMOVE

    def _poll_running(self) -> bool:
        if self.state == RESULTS and helper.update_running() is None:
            self._running_watch = 0
            self.start_check(refresh=False)
            return GLib.SOURCE_REMOVE
        if self.state not in (RESULTS, CHECKING):
            self._running_watch = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _populate(self) -> None:
        for row in self.rows.values():
            row.group.remove(row.widget)
        self.rows = {}
        result = self.result
        if result is None:
            return
        selection = set(engine.default_selection(self.settings, result))
        for task in updates.TASKS:
            if task.group == UPDATES:
                source = result.sources.get(task.id)
                if source is None or not source.available:
                    continue
                row = source_row(task, source, selected=task.id in selection,
                                 sensitive=engine.selectable(result, task.id), on_toggled=self._on_row_toggled)
                row.group = self.updates_group
            else:
                cleanup = result.cleanup.get(task.id)
                if cleanup is not None and not cleanup.available:
                    continue
                row = cleanup_row(task, cleanup, self.settings, selected=task.id in selection,
                                  on_toggled=self._on_row_toggled)
                row.group = self.maintenance_group
            row.group.add(row.widget)
            self.rows[task.id] = row
        self.summary.update(result)
        self._update_selection()

    def _on_row_toggled(self, row: SelectableRow) -> None:
        self.settings.selection[row.task.id] = row.check.get_active()
        try:
            self.settings.save()
        except OSError as error:
            self.toast(f"Could not save your selection: {error.strerror}")
        self._update_selection()

    def selected_ids(self) -> list[str]:
        return [task_id for task_id, row in self.rows.items() if row.selected]

    def _update_selection(self) -> None:
        ids = self.selected_ids()
        sources = [task_id for task_id in ids if BY_ID[task_id].group == UPDATES]
        result = self.result
        count = sum(result.sources[task_id].count for task_id in sources if task_id in result.sources) if result else 0
        if count:
            text = f"{plural(count, 'update')} selected"
            download = download_text(result.download_size(sources), result.download_complete(sources))
            if download and not download.startswith("nothing"):
                text += f" · {download}"
        elif sources:
            text = f"{plural(len(sources), 'source')} selected"
        elif ids:
            text = f"{plural(len(ids), 'maintenance task')} selected"
        else:
            text = "Nothing selected"
        self.selection_label.set_label(text)
        self.install_button.set_label("_Install Updates" if sources else "_Run Maintenance")
        self._sync_actions()

    # ──────────────────────────── Updating ────────────────────────────

    def confirm_install(self) -> None:
        ids = self.selected_ids()
        if self.state != RESULTS or not ids or self.result is None:
            return
        apt = self.result.sources.get("apt")
        removals = apt.removals if apt and "apt" in ids else []
        firmware = "firmware" in ids and bool(self.result.sources["firmware"].updates)
        if not removals and not firmware:
            self.start_install(ids)
            return
        if removals:
            names = ", ".join(update.name for update in removals[:5])
            more = f" and {len(removals) - 5} more" if len(removals) > 5 else ""
            heading = f"Remove {plural(len(removals), 'Package')}?"
            body = (f"Installing these updates removes {names}{more}. This is normal when packages are "
                    "replaced by newer ones.")
        else:
            heading, body = "Install Firmware Updates?", ""
        if firmware:
            body += ("\n\n" if body else "") + ("Firmware updates can take several minutes. Some devices restart "
                                                "while they update, and the computer may need a restart to finish. "
                                                "Keep laptops plugged in.")
        dialog = Adw.AlertDialog.new(heading, body)
        dialog.add_response("cancel", "_Cancel")
        dialog.add_response("install", "_Install")
        dialog.set_response_appearance(
            "install", Adw.ResponseAppearance.DESTRUCTIVE if removals else Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _dialog, response: response == "install" and self.start_install(ids))
        dialog.present(self)

    def start_install(self, ids: list[str]) -> None:
        if self.state != RESULTS:
            return
        plan = engine.plan_for(ids, self.settings, self.result)
        if not plan.steps:
            return
        self._prepare_updating_page(plan)
        run = engine.Run(plan, self._on_run_event_threaded)
        self.run = run
        self._inhibit(True)
        self._sync_actions()
        threading.Thread(target=self._run_worker, args=(run,), name="update").start()

    def _prepare_updating_page(self, plan: engine.Plan) -> None:
        for row in self.task_rows.values():
            self.task_group.remove(row)
        self.task_rows = {}
        for task_id in plan.steps:
            row = TaskRow(BY_ID[task_id])
            self.task_group.add(row)
            self.task_rows[task_id] = row
        self._weights = {task_id: WEIGHTS.get(task_id, 1) for task_id in plan.steps}
        self._fractions = dict.fromkeys(plan.steps, 0.0)
        self._progress_tasks = set()
        self.report = None
        self.progress.set_fraction(0)
        self.progress.set_visible(True)
        self.update_icon.set_visible_child_name("busy")
        updating = any(BY_ID[task_id].group == UPDATES for task_id in plan.steps)
        self.update_title.set_label("Installing Updates…" if updating else "Running Maintenance…")
        self.update_description.set_label("Starting")
        self.stop_button.set_label("_Stop After This Step")
        self.stop_button.set_visible(True)
        for button in (self.log_button, self.again_button, self.restart_button):
            button.set_visible(False)
        self.log_view.clear()
        self._show(UPDATING)

    def _run_worker(self, run: engine.Run) -> None:
        try:
            run.execute()
        except Exception as error:
            log.exception("The update failed")
            GLib.idle_add(self._on_run_crashed, str(error) or type(error).__name__)

    def _on_run_event_threaded(self, event: engine.RunEvent) -> None:
        """Called from the update thread. Output is batched; everything else goes to the main loop."""
        if event.kind in ("output", "message"):
            if event.kind == "message":
                line = {"warning": "⚠ ", "error": "✖ "}.get(event.level, "") + event.text
            else:
                line = event.text
            with self._output_lock:
                self._pending_lines.append(line)
                if event.kind == "output" and event.task:
                    self._activity = (event.task, event.text)
                if not self._flush_scheduled:
                    self._flush_scheduled = True
                    GLib.timeout_add(150, self._flush_output)
        else:
            GLib.idle_add(self._on_run_event, event)

    def _flush_output(self) -> bool:
        with self._output_lock:
            lines, self._pending_lines = self._pending_lines, []
            activity, self._activity = self._activity, None
            self._flush_scheduled = False
        self.log_view.add_lines(lines)
        if activity and activity[0] not in self._progress_tasks and activity[0] in self.task_rows:
            self.task_rows[activity[0]].set_activity(activity[1])
        return GLib.SOURCE_REMOVE

    def _on_run_event(self, event: engine.RunEvent) -> bool:
        row = self.task_rows.get(event.task) if event.task else None
        if event.kind == "authenticating":
            self.update_description.set_label("Waiting for authentication…")
        elif event.kind == "waiting":
            self.update_description.set_label(f"{event.text}…")
        elif event.kind == "started" and row:
            row.set_running()
            self.update_description.set_label(row.task.title)
        elif event.kind == "progress" and row:
            self._progress_tasks.add(event.task)
            row.set_activity(event.text)
            self._fractions[event.task] = event.fraction
            self._update_progress()
        elif event.kind == "stopping":
            self.stop_button.set_label("Stopping After This Step…")
            self._sync_actions()
        elif event.kind == "finished-task" and row:
            row.set_outcome(event.outcome)
            self._fractions[event.task] = 1.0
            self._update_progress()
        elif event.kind == "finished":
            self._on_run_finished(event.report)
        return GLib.SOURCE_REMOVE

    def _update_progress(self) -> None:
        total = sum(self._weights.values()) or 1
        done = sum(self._weights[task] * min(self._fractions.get(task, 0.0), 1.0) for task in self._weights)
        self.progress.set_fraction(done / total)

    def stop_update(self) -> None:
        if self.run is None or self.run.stopping:
            return
        self.run.stop()
        self.stop_button.set_label("Stopping After This Step…")
        self._sync_actions()

    def _on_run_finished(self, report: engine.RunReport) -> None:
        self._flush_output()
        self.report = report
        self.run = None
        self._inhibit(False)
        ran = [outcome for outcome in report.outcomes.values() if outcome.state != engine.SKIPPED]
        if report.error and not ran:
            icon = "error"
            title, description = {
                "auth": ("Nothing Was Changed", report.error),
                "busy": ("Another Update Is Running", report.error),
                "offline": ("No Internet Connection", "Connect to the internet and try again."),
            }.get(report.error_reason, ("Updates Could Not Be Installed", report.error))
        elif report.problems:
            icon, title = "warning", "Some Steps Failed"
            description = (f"{plural(len(report.problems), 'step')} could not be completed; "
                           "the list below and the log say why. " + summary_text(report))
        elif report.stopped:
            icon, title, description = "warning", "Update Stopped", summary_text(report)
        else:
            icon = "success"
            title = "Updates Installed" if report.installed else "All Done"
            description = summary_text(report)
        self.update_icon.set_visible_child_name(icon)
        self.update_title.set_label(title)
        self.update_description.set_label(description)
        self.progress.set_visible(False)
        self.stop_button.set_visible(False)
        self.log_button.set_visible(report.log_path is not None)
        # After a restart there is nothing to check again, so offer one or the other.
        self.restart_button.set_visible(report.reboot_required)
        self.again_button.set_visible(not report.reboot_required)
        self.again_button.add_css_class("suggested-action")
        self.result = None
        self._show(DONE)
        self._refresh_subtitle()
        if not self.is_active():
            notification = Gio.Notification.new(title)
            notification.set_body(description)
            self.get_application().send_notification("update-finished", notification)

    def _on_run_crashed(self, message: str) -> bool:
        self.run = None
        self._inhibit(False)
        self.update_icon.set_visible_child_name("error")
        self.update_title.set_label("Something Went Wrong")
        self.update_description.set_label(message)
        self.progress.set_visible(False)
        self.stop_button.set_visible(False)
        self.log_button.set_visible(False)
        self.restart_button.set_visible(False)
        self.again_button.set_visible(True)
        self.result = None
        self._show(DONE)
        return GLib.SOURCE_REMOVE

    def _inhibit(self, on: bool) -> None:
        application = self.get_application()
        if on and not self._inhibit_cookie:
            self._inhibit_cookie = application.inhibit(
                self, Gtk.ApplicationInhibitFlags.LOGOUT | Gtk.ApplicationInhibitFlags.SUSPEND,
                "Updates are being installed")
        elif not on and self._inhibit_cookie:
            application.uninhibit(self._inhibit_cookie)
            self._inhibit_cookie = 0

    def _open_report_log(self) -> None:
        if self.report and self.report.log_path:
            dialogs.open_path(self, self.report.log_path, self.toast)

    def _on_close_request(self, _window) -> bool:
        if self.state == UPDATING:
            self.toast("Please wait until the updates are installed")
            return True
        self._check_token = None
        return False

    # ──────────────────────────── Dialogs ─────────────────────────────

    def show_history(self) -> None:
        dialogs.HistoryDialog(self, self.toast).present(self)

    def show_preferences(self) -> None:
        def closed(changed: bool) -> None:
            if changed and self.state == RESULTS:
                self._populate()

        dialogs.PreferencesDialog(self.settings, closed).present(self)

    def open_log_folder(self) -> None:
        dialogs.open_path(self, helper.LOG_DIR, self.toast)

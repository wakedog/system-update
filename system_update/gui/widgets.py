"""Widgets used by the main window."""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, GLib, Gtk

from system_update import engine
from system_update.settings import Settings
from system_update.updates import CheckResult, CleanupStatus, SourceStatus, Task, Update
from system_update.util import format_ago, format_size, plural

MAX_ITEMS = 100
MAX_LOG_LINES = 5000


def spinner(size: int = 16) -> Gtk.Widget:
    widget = Adw.Spinner() if hasattr(Adw, "Spinner") else Gtk.Spinner(spinning=True)
    widget.set_size_request(size, size)
    return widget


def pill(text: str, style: str) -> Gtk.Label:
    label = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
    label.add_css_class("tag")
    label.add_css_class(style)
    return label


def task_icon(task: Task) -> Gtk.Image:
    icon = Gtk.Image(icon_name=task.icon, valign=Gtk.Align.CENTER)
    icon.add_css_class("task-icon")
    icon.add_css_class(task.group)
    return icon


def relative_day(timestamp: float) -> str:
    then = GLib.DateTime.new_from_unix_local(int(timestamp))
    now = GLib.DateTime.new_now_local()
    days = (GLib.DateTime.new_local(now.get_year(), now.get_month(), now.get_day_of_month(), 0, 0, 0)
            .difference(GLib.DateTime.new_local(then.get_year(), then.get_month(), then.get_day_of_month(), 0, 0, 0))
            // GLib.TIME_SPAN_DAY)
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    return then.format("on %B %-e") if then.get_year() == now.get_year() else then.format("on %B %-e, %Y")


def format_timestamp(timestamp: float, twelve_hour: bool) -> str:
    moment = GLib.DateTime.new_from_unix_local(int(timestamp))
    clock = moment.format("%-l:%M %p") if twelve_hour else moment.format("%H:%M")
    return f"{moment.format('%A, %B %-e, %Y')} at {clock}"


def uses_twelve_hour_clock() -> bool:
    from gi.repository import Gio

    source = Gio.SettingsSchemaSource.get_default()
    if source and source.lookup("org.gnome.desktop.interface", True):
        return Gio.Settings(schema_id="org.gnome.desktop.interface").get_string("clock-format") == "12h"
    return False


def download_text(size: int | None, complete: bool) -> str:
    """"148.0 MB to download", "615.0 MB or more to download", or "" when unknown."""
    if size is None:
        return ""
    if not size and complete:
        return "nothing left to download"
    return f"{format_size(size)}{'' if complete else ' or more'} to download"


def version_text(update: Update) -> str:
    if update.kind == "remove":
        return update.old_version
    if update.kind == "install" or not update.old_version:
        return update.new_version
    return f"{update.old_version} → {update.new_version}"


class SelectableRow:
    """A task the user can include or leave out: check box, icon, and optional details."""

    def __init__(self, task: Task, subtitle: str, *, selected: bool, sensitive: bool,
                 on_toggled: Callable[["SelectableRow"], None], children: list[Gtk.Widget] | None = None,
                 suffix: Gtk.Widget | None = None):
        self.task = task
        self.group: Adw.PreferencesGroup | None = None
        self.widget = row = Adw.ExpanderRow() if children else Adw.ActionRow()
        row.set_use_markup(False)
        row.set_title(task.title)
        row.set_subtitle(subtitle)
        if isinstance(row, Adw.ActionRow):
            row.set_subtitle_lines(3)

        self.check = Gtk.CheckButton(valign=Gtk.Align.CENTER, active=selected and sensitive, sensitive=sensitive)
        self.check.update_property([Gtk.AccessibleProperty.LABEL], [f"Include {task.title}"])
        self.check.connect("toggled", lambda *_: on_toggled(self))
        # One prefix widget: action and expander rows order multiple prefixes differently.
        prefix = Gtk.Box(spacing=12, valign=Gtk.Align.CENTER)
        prefix.append(self.check)
        prefix.append(task_icon(task))
        row.add_prefix(prefix)
        if suffix is not None:
            row.add_suffix(suffix)
        if children:
            for child in children[:MAX_ITEMS]:
                row.add_row(child)
            if len(children) > MAX_ITEMS:
                row.add_row(Adw.ActionRow(title=f"And {len(children) - MAX_ITEMS} more", use_markup=False))
        elif sensitive:
            row.set_activatable_widget(self.check)

    @property
    def selected(self) -> bool:
        return self.check.get_active() and self.check.get_sensitive()


def update_row(update: Update) -> Adw.ActionRow:
    subtitle = " · ".join(part for part in (version_text(update), update.detail) if part)
    row = Adw.ActionRow(title=update.name, subtitle=subtitle, use_markup=False, subtitle_lines=2)
    row.add_css_class("update-row")
    if update.kind == "remove":
        row.add_suffix(pill("Removed", "remove"))
    elif update.kind == "install":
        row.add_suffix(pill("New", "new"))
    if update.security:
        row.add_suffix(pill("Security", "security"))
    if update.size:
        size = Gtk.Label(label=format_size(update.size), valign=Gtk.Align.CENTER)
        size.add_css_class("dim-label")
        size.add_css_class("numeric")
        row.add_suffix(size)
    return row


def status_icon(icon_name: str, style: str, tooltip: str) -> Gtk.Image:
    image = Gtk.Image(icon_name=icon_name, valign=Gtk.Align.CENTER, tooltip_text=tooltip)
    image.add_css_class("row-state")
    image.add_css_class(style)
    return image


def source_row(task: Task, source: SourceStatus, *, selected: bool, sensitive: bool,
               on_toggled: Callable[[SelectableRow], None]) -> SelectableRow:
    if source.error:
        subtitle = f"Could not check: {source.error}"
        suffix = status_icon("dialog-warning-symbolic", "warning", "Could not check for updates")
    elif source.actionable:
        subtitle = source.summary()
        if source.repair:
            subtitle += ". An interrupted installation will be completed first."
        suffix = None
    else:
        subtitle = "Up to date"
        suffix = status_icon("object-select-symbolic", "done", "Up to date")
    if task.id == "firmware" and source.updates:
        subtitle += " · Some devices restart while updating"
    children = [update_row(update) for update in source.updates]
    return SelectableRow(task, subtitle, selected=selected, sensitive=sensitive, on_toggled=on_toggled,
                         children=children, suffix=suffix)


def cleanup_subtitle(task: Task, cleanup: CleanupStatus | None, settings: Settings) -> str:
    parts = [task.describe(settings)]
    if cleanup is not None:
        unit = {"autoremove": "package", "snap_revisions": "old revision"}.get(task.id)
        if cleanup.count and unit:
            parts.append(plural(cleanup.count, unit))
        elif cleanup.count == 0 and unit:
            parts.append("Nothing to remove right now")
        if cleanup.note:
            parts.append(cleanup.note)
    return " · ".join(parts)


def cleanup_row(task: Task, cleanup: CleanupStatus | None, settings: Settings, *, selected: bool,
                on_toggled: Callable[[SelectableRow], None]) -> SelectableRow:
    size = None
    if cleanup is not None and cleanup.size:
        size = Gtk.Label(label=format_size(cleanup.size), valign=Gtk.Align.CENTER,
                         tooltip_text="What this frees right now; updates may add to it")
        size.add_css_class("numeric")
        size.add_css_class("size-label")
    return SelectableRow(task, cleanup_subtitle(task, cleanup, settings), selected=selected, sensitive=True,
                         on_toggled=on_toggled, suffix=size)


class TaskRow(Adw.ActionRow):
    """Progress of one step while updating."""

    def __init__(self, task: Task):
        super().__init__(title=task.title, use_markup=False, subtitle_lines=2)
        self.task = task
        self.add_prefix(task_icon(task))
        self.result_label = Gtk.Label(valign=Gtk.Align.CENTER)
        self.result_label.add_css_class("dim-label")
        self.result_label.add_css_class("numeric")
        self.add_suffix(self.result_label)
        self.status = Gtk.Stack(valign=Gtk.Align.CENTER, transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.status.set_size_request(20, 20)
        self.status.add_named(Gtk.Box(), "pending")
        self.status.add_named(spinner(16), "running")
        for state, icon_name, label in (
            (engine.DONE, "object-select-symbolic", "Done"),
            (engine.WARNING, "dialog-warning-symbolic", "Done with warnings"),
            (engine.FAILED, "dialog-error-symbolic", "Failed"),
            (engine.SKIPPED, "action-unavailable-symbolic", "Skipped"),
        ):
            self.status.add_named(status_icon(icon_name, state, label), state)
        self.add_suffix(self.status)
        self.add_css_class("pending")
        self.state = "pending"

    def set_running(self) -> None:
        self.state = "running"
        self.remove_css_class("pending")
        self.status.set_visible_child_name("running")

    def set_activity(self, text: str) -> None:
        if self.state == "running":
            self.set_subtitle(text)

    def set_outcome(self, outcome: engine.TaskOutcome) -> None:
        self.state = outcome.state
        self.remove_css_class("pending")
        self.status.set_visible_child_name(outcome.state)
        self.result_label.set_label(format_size(outcome.freed) if outcome.freed else "")
        self.set_subtitle(outcome.message)


class SummaryCard(Gtk.Box):
    """What the check found, at a glance."""

    def __init__(self):
        super().__init__(spacing=18)
        self.add_css_class("card")
        self.add_css_class("summary-card")
        self.badge = Gtk.Image(pixel_size=32, valign=Gtk.Align.CENTER)
        self.badge.add_css_class("status-badge")
        self.badge.add_css_class("small")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER, hexpand=True)
        self.title = Gtk.Label(xalign=0, wrap=True)
        self.title.add_css_class("summary-title")
        self.caption = Gtk.Label(xalign=0, wrap=True)
        self.freshness = Gtk.Label(xalign=0, wrap=True)
        self.freshness.add_css_class("dim-label")
        self.freshness.add_css_class("caption")
        for widget in (self.title, self.caption, self.freshness):
            texts.append(widget)
        self.append(self.badge)
        self.append(texts)

    def update(self, result: CheckResult) -> None:
        for style in ("success", "accent", "warning"):
            self.badge.remove_css_class(style)
        repair = any(source.repair for source in result.sources.values())
        errors = [source for source in result.sources.values() if source.available and source.error]
        if result.total or repair:
            self.badge.set_from_icon_name("software-update-urgent-symbolic" if result.security
                                          else "software-update-available-symbolic")
            self.badge.add_css_class("accent")
            self.title.set_label(f"{plural(result.total, 'Update')} Available" if result.total else "Repair Needed")
            parts = []
            if result.security:
                parts.append(plural(result.security, "security update"))
            parts.append(download_text(result.download_size(), result.download_complete()))
            if repair:
                parts.append("an interrupted installation will be completed")
            text = " · ".join(part for part in parts if part)
            self.caption.set_label(text[:1].upper() + text[1:] if text else "Ready to install")
        elif errors:
            self.badge.set_from_icon_name("dialog-warning-symbolic")
            self.badge.add_css_class("warning")
            self.title.set_label("Some Updates Could Not Be Checked")
            self.caption.set_label("The rest is up to date")
        else:
            self.badge.set_from_icon_name("object-select-symbolic")
            self.badge.add_css_class("success")
            self.title.set_label("Up to Date")
            self.caption.set_label("This computer has the latest software")
        if result.lists_updated:
            text = f"Package information from {format_ago(result.lists_updated)}"
            if result.refresh_error:
                text += f". It could not be refreshed: {result.refresh_error}"
        else:
            text = "Package information has never been refreshed"
        self.freshness.set_label(text)


class LogView(Gtk.Revealer):
    """The live output of every command, shown and hidden by its toggle button."""

    def __init__(self):
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.toggle = Gtk.ToggleButton(label="Show _Details", use_underline=True, halign=Gtk.Align.CENTER)
        self.toggle.add_css_class("flat")
        self.toggle.connect("toggled", self._on_toggled)
        self.buffer = Gtk.TextBuffer()
        self.end_mark = self.buffer.create_mark("end", self.buffer.get_end_iter(), False)
        self.text = Gtk.TextView(buffer=self.buffer, editable=False, cursor_visible=False, monospace=True,
                                 wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=10, bottom_margin=10,
                                 left_margin=12, right_margin=12)
        self.text.add_css_class("terminal")
        self.text.update_property([Gtk.AccessibleProperty.LABEL], ["Command output"])
        scroller = Gtk.ScrolledWindow(child=self.text, min_content_height=260, max_content_height=260,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.add_css_class("card")
        scroller.add_css_class("terminal-frame")
        self.scroller = scroller
        self.set_child(scroller)
        self._lines = 0

    def _on_toggled(self, toggle: Gtk.ToggleButton) -> None:
        shown = toggle.get_active()
        self.set_reveal_child(shown)
        toggle.set_label("Hide _Details" if shown else "Show _Details")
        if shown:
            GLib.idle_add(self._scroll_to_end)

    def _scroll_to_end(self) -> bool:
        self.text.scroll_to_mark(self.end_mark, 0.0, False, 0.0, 1.0)
        return GLib.SOURCE_REMOVE

    def clear(self) -> None:
        self.buffer.set_text("")
        self._lines = 0

    def add_lines(self, lines: list[str]) -> None:
        if not lines:
            return
        adjustment = self.scroller.get_vadjustment()
        at_end = adjustment.get_value() >= adjustment.get_upper() - adjustment.get_page_size() - 24
        self.buffer.insert(self.buffer.get_end_iter(), "\n".join(lines) + "\n")
        self._lines += len(lines)
        if self._lines > MAX_LOG_LINES:
            excess = self._lines - MAX_LOG_LINES
            self.buffer.delete(self.buffer.get_start_iter(), self.buffer.get_iter_at_line(excess)[1])
            self._lines = MAX_LOG_LINES
        if at_end and self.get_reveal_child():
            GLib.idle_add(self._scroll_to_end)

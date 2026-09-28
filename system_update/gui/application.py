"""The Gtk application: actions, keyboard shortcuts and styling."""

from __future__ import annotations

from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from system_update import APP_ID, APP_NAME, paths
from system_update.gui import dialogs
from system_update.gui.window import UPDATING, MainWindow


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        GLib.set_application_name(APP_NAME)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        css = Gtk.CssProvider()
        css.load_from_path(str(Path(__file__).with_name("style.css")))
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        if icons := paths.dev_icon_dir():
            Gtk.IconTheme.get_for_display(display).add_search_path(str(icons))
        Gtk.Window.set_default_icon_name(APP_ID)

        self._action("quit", self._quit, ["<Control>q"])
        self._action("about", lambda: dialogs.show_about(self.get_active_window()))
        self._action("preferences", lambda: self._window().show_preferences(), ["<Control>comma"])
        self._action("open-logs", lambda: self._window().open_log_folder())
        if hasattr(Adw, "ShortcutsDialog"):
            self._action("shortcuts", lambda: dialogs.show_shortcuts(self.get_active_window()),
                         ["<Control>question"])
        self.set_accels_for_action("win.check", ["<Control>r", "F5"])
        self.set_accels_for_action("win.install", ["<Control>Return"])
        self.set_accels_for_action("win.history", ["<Control>h"])
        self.set_accels_for_action("window.close", ["<Control>w"])

    def do_activate(self) -> None:
        self._window().present()

    def _quit(self) -> None:
        for window in self.get_windows():
            if isinstance(window, MainWindow) and window.state == UPDATING:
                window.toast("Please wait until the updates are installed")
                return
        self.quit()

    def _window(self) -> MainWindow:
        window = self.get_active_window()
        return window if isinstance(window, MainWindow) else MainWindow(self)

    def _action(self, name: str, callback, accels: list[str] | None = None) -> None:
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", lambda *_: callback())
        self.add_action(action)
        if accels:
            self.set_accels_for_action(f"app.{name}", accels)

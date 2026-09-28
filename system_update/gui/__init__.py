"""GTK 4 / libadwaita desktop interface."""

from __future__ import annotations

import sys

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except (ImportError, ValueError):
    pass  # run() reports what is missing


def run() -> int:
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gtk
    except (ImportError, ValueError) as error:
        print(f"system-update: the desktop app needs python3-gi, gir1.2-gtk-4.0 and gir1.2-adw-1 ({error}).\n"
              "The command line works without them; see system-update --help.", file=sys.stderr)
        return 1
    if not Gtk.init_check():
        print("system-update: no graphical display is available.\n"
              "Use the command line instead; see system-update --help.", file=sys.stderr)
        return 1

    from system_update.gui.application import Application

    return Application().run([sys.argv[0]])

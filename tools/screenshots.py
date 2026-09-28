#!/usr/bin/env python3
"""Render the app's screens to PNG files on a headless Broadway display.

    tools/screenshots.py [OUTPUT_DIR]      (default: build/screenshots)
    tools/screenshots.py --update-readme   also copy the README images to data/screenshots

No window appears on screen and nothing is installed: the window is fed a
sample update check and sample update events.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DISPLAY = ":43"
README_SHOTS = ("results", "updating", "done", "results-dark")


def launch(output: Path) -> int:
    """Start a private Broadway server and rerun this script as its client."""
    if not shutil.which("gtk4-broadwayd"):
        print("gtk4-broadwayd is missing (package libgtk-4-bin)", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="system-update-shots-") as temp:
        runtime, home = Path(temp, "runtime"), Path(temp, "home")
        runtime.mkdir(mode=0o700)
        home.mkdir()
        env = dict(os.environ, XDG_RUNTIME_DIR=str(runtime), HOME=str(home), GSETTINGS_BACKEND="memory",
                   DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent", GDK_BACKEND="broadway",
                   BROADWAY_DISPLAY=DISPLAY, SYSTEM_UPDATE_SCREENSHOT_CHILD="1", NO_AT_BRIDGE="1")
        for name in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "WAYLAND_DISPLAY",
                     "DISPLAY"):
            env.pop(name, None)
        server = subprocess.Popen(["gtk4-broadwayd", DISPLAY], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(0.5)
            return subprocess.run([sys.executable, __file__, str(output)], env=env, timeout=180).returncode
        finally:
            server.terminate()
            server.wait(timeout=10)


def demo_history(path: Path, now: float) -> None:
    runs = [
        (1, "root", "root", [("apt", "done", 5, None, "Updated 5 packages"), ("snap", "done", 1, None, "Updated firefox"),
                             ("apt_cache", "done", None, 96_000_000, ""), ("journal", "done", None, 41_000_000, "")],
         False),
        (3, "bryan", "pkexec", [("apt", "done", 14, None, "Updated 12 packages, installed 2 new"),
                                ("autoremove", "done", 2, 410_000_000, "Removed 2 packages")], True),
        (8, "root", "root", [("apt", "done", 3, None, "Updated 3 packages"),
                             ("snap", "failed", None, None, "Could not reach the snap store")], False),
    ]
    with open(path, "w") as handle:
        for days, user, via, tasks, reboot in runs:
            started = now - days * 86400 - 3600
            handle.write(json.dumps({
                "started": started, "finished": started + 190, "user": user, "via": via,
                "reboot_required": reboot, "log": "", "stopped": False,
                "tasks": [{"task": t, "state": s, "count": c, "freed": f, "message": m} for t, s, c, f, m in tasks],
            }) + "\n")


def render(output: Path) -> int:
    sys.path.insert(0, str(ROOT))
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Gsk", "4.0")
    from gi.repository import Adw, GLib, Graphene, Gsk, Gtk

    from system_update import engine, helper
    from system_update.gui import dialogs
    from system_update.gui.application import Application
    from system_update.updates import CheckResult, CleanupStatus, SourceStatus, Update

    mb = 1_000_000
    now = time.time()
    history_file = Path(os.environ["HOME"]) / "history.jsonl"
    demo_history(history_file, now)
    helper.HISTORY_FILE = str(history_file)

    apt_updates = [
        Update("linux-image-6.17.0-12-generic", new_version="6.17.0-12.12", kind="install",
               detail="resolute-updates"),
        Update("linux-modules-6.17.0-12-generic", new_version="6.17.0-12.12", kind="install",
               detail="resolute-updates"),
        Update("linux-generic", "6.17.0-11.11", "6.17.0-12.12", detail="resolute-updates"),
        Update("openssl", "3.4.1-1ubuntu3", "3.4.1-1ubuntu3.1", security=True, detail="resolute-security"),
        Update("libssl3t64", "3.4.1-1ubuntu3", "3.4.1-1ubuntu3.1", security=True, detail="resolute-security"),
        Update("libfreerdp3-3", "3.31.0+dfsg-0ubuntu0.26.04.1", "3.32.0+dfsg-0ubuntu0.26.04.1", security=True,
               detail="resolute-security"),
        Update("libwinpr3-3", "3.31.0+dfsg-0ubuntu0.26.04.1", "3.32.0+dfsg-0ubuntu0.26.04.1", security=True,
               detail="resolute-security"),
        Update("gnome-shell", "49.1-0ubuntu2", "49.2-0ubuntu1", detail="resolute-updates"),
        Update("mesa-vulkan-drivers", "25.2.3-1ubuntu1", "25.2.5-0ubuntu1", detail="resolute-updates"),
        Update("python3-urllib3", "2.3.0-3", "2.3.0-3ubuntu0.1", detail="resolute-updates"),
        Update("tzdata", "2026a-1ubuntu1", "2026b-0ubuntu0.26.04", detail="resolute-updates"),
        Update("ubuntu-drivers-common", "1:0.10.4", "1:0.10.5", detail="resolute-updates"),
    ]
    result = CheckResult(
        sources={
            "apt": SourceStatus("apt", updates=apt_updates, download_size=148 * mb),
            "snap": SourceStatus("snap", updates=[
                Update("firefox", "142.0.1-1", "143.0-2", detail="Mozilla", size=291 * mb),
                Update("gnome-46-2404", "0+git.d9b6a3f", "0+git.4e7ab28", detail="Canonical", size=164 * mb)],
                download_size=455 * mb),
            "flatpak": SourceStatus("flatpak", updates=[
                Update("Blender", "4.5.2", "4.5.3", detail="org.blender.Blender (stable)")]),
            "firmware": SourceStatus("firmware", updates=[
                Update("System Firmware", "1.18.0", "1.19.2", detail="Improves battery life and fixes waking from sleep",
                       size=12 * mb)], download_size=12 * mb),
        },
        cleanup={
            "autoremove": CleanupStatus("autoremove", count=2, size=410 * mb),
            "apt_cache": CleanupStatus("apt_cache", size=118 * mb),
            "snap_revisions": CleanupStatus("snap_revisions", count=2, size=580 * mb),
            "flatpak_unused": CleanupStatus("flatpak_unused"),
            "journal": CleanupStatus("journal", note="The journal uses 312.4 MB"),
        },
        checked=now, lists_updated=now - 2 * 3600,
    )

    class Online:
        @staticmethod
        def get_network_available():
            return True

    Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
    app = Application()
    steps = []
    failures = []

    def shot(name, attempts=5):
        def take(window):
            width, height = window.get_width(), window.get_height()
            paintable = Gtk.WidgetPaintable.new(window)
            snapshot = Gtk.Snapshot()
            paintable.snapshot(snapshot, width, height)
            node = snapshot.to_node()
            if node is None:  # nothing painted yet; try again after the next frames
                if attempts > 1:
                    steps.insert(0, shot(name, attempts - 1))
                else:
                    failures.append(name)
                return
            renderer = Gsk.CairoRenderer()
            renderer.realize_for_display(window.get_display())
            renderer.render_texture(node, Graphene.Rect().init(0, 0, width, height)).save_to_png(
                str(output / f"{name}.png"))
            renderer.unrealize()
            print(f"saved {output / name}.png ({width}×{height})")
        return take

    def checking(window):
        window._check_token = None
        window.checking_page.set_description("Refreshing package information (62%)")
        window._show("checking")

    def show_results(window):
        window._check_token = None
        window.network = Online()
        window.settings.selection = {}
        window.result = result
        window.report = None
        window._show("results")
        window._populate()

    def expand(window):
        window.rows["apt"].widget.set_expanded(True)

    def collapse(window):
        window.rows["apt"].widget.set_expanded(False)

    def scroll(fraction):
        def apply(window):
            adjustment = window.stack.get_child_by_name("results").get_vadjustment()
            adjustment.set_value((adjustment.get_upper() - adjustment.get_page_size()) * fraction)
        return apply

    def confirm(window):
        window.rows["firmware"].check.set_active(True)
        window.confirm_install()

    def close_dialog(window):
        if dialog := window.get_visible_dialog():
            dialog.force_close()

    plan = engine.Plan(tasks=["apt", "snap", "flatpak", "autoremove", "apt_cache", "snap_revisions",
                              "flatpak_unused", "journal"])
    output_lines = [
        "$ apt-get -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold -y -q full-upgrade",
        "Reading package lists...", "Building dependency tree...", "Reading state information...",
        "Calculating upgrade...", "The following NEW packages will be installed:",
        "  linux-image-6.17.0-12-generic linux-modules-6.17.0-12-generic",
        "The following packages will be upgraded:",
        "  gnome-shell libfreerdp3-3 libssl3t64 libwinpr3-3 linux-generic mesa-vulkan-drivers openssl",
        "10 upgraded, 2 newly installed, 0 to remove and 0 not upgraded.",
        "Need to get 148 MB of archives.",
        "Get:1 http://us.archive.ubuntu.com/ubuntu resolute-updates/main amd64 linux-modules-6.17.0-12-generic [42.1 MB]",
        "Get:2 http://security.ubuntu.com/ubuntu resolute-security/main amd64 openssl amd64 3.4.1-1ubuntu3.1 [1.2 MB]",
        "Fetched 148 MB in 9s (16.4 MB/s)",
        "Preparing to unpack .../openssl_3.4.1-1ubuntu3.1_amd64.deb ...",
        "Unpacking openssl (3.4.1-1ubuntu3.1) over (3.4.1-1ubuntu3) ...",
        "Setting up openssl (3.4.1-1ubuntu3.1) ...",
        "Preparing to unpack .../gnome-shell_49.2-0ubuntu1_amd64.deb ...",
        "Unpacking gnome-shell (49.2-0ubuntu1) over (49.1-0ubuntu2) ...",
    ]

    def updating(window):
        window.is_active = lambda: True
        window._prepare_updating_page(plan)
        window.run = engine.Run(plan)  # never executed; enables the Stop button
        window._sync_actions()
        window._on_run_event(engine.RunEvent("started", "apt"))
        window._on_run_event(engine.RunEvent("progress", "apt", text="Unpacking gnome-shell (amd64)", fraction=0.62))

    def details(window):
        window.log_view.add_lines(output_lines)
        window.log_view.toggle.set_active(True)

    def hide_details(window):
        window.log_view.toggle.set_active(False)

    def finished(window):
        outcomes = {
            "apt": engine.TaskOutcome("apt", engine.DONE, 12, None, "Updated 10 packages, installed 2 new"),
            "snap": engine.TaskOutcome("snap", engine.DONE, 2, None, "Updated firefox and gnome-46-2404"),
            "flatpak": engine.TaskOutcome("flatpak", engine.DONE, 1, None, "Updated Blender"),
            "autoremove": engine.TaskOutcome("autoremove", engine.DONE, 2, 410 * mb, "Removed 2 packages"),
            "apt_cache": engine.TaskOutcome("apt_cache", engine.DONE, None, 266 * mb),
            "snap_revisions": engine.TaskOutcome("snap_revisions", engine.DONE, 2, 580 * mb,
                                                 "Removed 2 old revisions"),
            "flatpak_unused": engine.TaskOutcome("flatpak_unused", engine.DONE),
            "journal": engine.TaskOutcome("journal", engine.DONE, None, 112 * mb),
        }
        for task, outcome in outcomes.items():
            window._on_run_event(engine.RunEvent("started", task))
            window._on_run_event(engine.RunEvent("finished-task", task, outcome=outcome))
        report = engine.RunReport(started=now - 262, finished=now, outcomes=outcomes, reboot_required=True,
                                  reboot_packages=["linux-image-6.17.0-12-generic"],
                                  log_path="/var/log/system-update/update-20260928-151203.log")
        window._on_run_event(engine.RunEvent("finished", report=report))

    def dark(window):
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)

    steps += [
        checking, shot("checking"),
        show_results, shot("results"),
        expand, shot("results-expanded"), collapse,
        scroll(1.0), shot("results-maintenance"), scroll(0.0),
        confirm, shot("confirm"), close_dialog,
        updating, shot("updating-steps"),
        details, shot("updating"), hide_details,
        finished, shot("done"),
        show_results, lambda w: w.show_preferences(), shot("preferences"), close_dialog,
        lambda w: w.show_history(), shot("history"), close_dialog,
        lambda w: dialogs.show_about(w), shot("about"), close_dialog,
        dark, show_results, shot("results-dark"),
        updating, details, shot("updating-dark"), hide_details,
        finished, shot("done-dark"),
    ]

    def run_steps(window):
        # Broadway paints irregularly without a browser attached, so move on only
        # after the previous change has actually been laid out and painted.
        painted = {"frames": 0}
        window.get_frame_clock().connect("after-paint", lambda _clock: painted.update(frames=painted["frames"] + 1))

        def advance():
            if painted["frames"] < 2:
                window.queue_draw()
                return GLib.SOURCE_CONTINUE
            if not steps:
                app.quit()
                return GLib.SOURCE_REMOVE
            painted["frames"] = 0
            steps.pop(0)(window)
            window.queue_resize()
            return GLib.SOURCE_CONTINUE

        GLib.timeout_add(250, advance)

    def on_activate(application):
        window = application.get_active_window()
        window.set_default_size(700, 900)
        run_steps(window)

    app.connect_after("activate", on_activate)
    output.mkdir(parents=True, exist_ok=True)
    app.run([sys.argv[0]])
    if failures:
        print("could not render: " + ", ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    arguments = [argument for argument in sys.argv[1:] if argument != "--update-readme"]
    target = Path(arguments[0] if arguments else ROOT / "build" / "screenshots").resolve()
    if os.environ.get("SYSTEM_UPDATE_SCREENSHOT_CHILD"):
        sys.exit(render(target))
    status = launch(target)
    if status == 0 and "--update-readme" in sys.argv:
        for name in README_SHOTS:
            shutil.copyfile(target / f"{name}.png", ROOT / "data" / "screenshots" / f"{name}.png")
            print(f"updated data/screenshots/{name}.png")
    sys.exit(status)

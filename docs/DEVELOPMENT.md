# Developing System Update

Everything needed to rebuild, change and release System Update. For what the
app does and how to use it, see the [README](../README.md).

## Set up a development machine

System Update is plain Python 3 with GTK 4 and libadwaita through PyGObject.
There is no compile step, no virtualenv and nothing from PyPI; it uses the
system Python and the libraries that ship with Ubuntu.

```bash
sudo apt install git make python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
    desktop-file-utils appstream
git clone https://github.com/wakedog/system-update.git
cd system-update
make run      # start the app straight from the source tree
make check    # run the tests and validate the metadata; do this before every commit
```

Use the system `python3` (`/usr/bin/python3`), not a pyenv or conda Python,
because only the system Python can see `python3-gi`.

Supported targets: Ubuntu 24.04 (Python 3.12, GTK 4.14, libadwaita 1.5) and
newer. It is developed on Ubuntu 26.04. Continuous integration runs on 24.04,
so it catches anything that only works on newer libraries.

## Make targets

| Command | What it does |
| --- | --- |
| `make run` | Start the desktop app from this folder |
| `make test` | Unit tests (`python3 -m unittest discover -s tests -t .`) |
| `make check` | Tests, then validates the `.desktop` file, the AppStream metadata, the polkit policy and the bash completion |
| `make deb` | Build `dist/system-update_<version>_all.deb` |
| `make install` | Build and install the package with `sudo apt install` |
| `make uninstall` | Remove the installed package |
| `make clean` | Delete `build/`, `dist/` and `__pycache__` folders |

The command-line interface also runs from the source tree:

```bash
python3 -m system_update check
python3 -m system_update upgrade --dry-run
```

## How it fits together

```text
             ┌──────────── gui/ (GTK 4 + libadwaita) ────────────┐
             │ application.py  actions, shortcuts, CSS            │
             │ window.py       checking → results → updating →    │
             │                 done                               │
             │ widgets.py, dialogs.py, style.css                  │
             └───────────────┬────────────────────────────────────┘
                             │            cli.py (same engine)
                             ▼                   │
      updates.py  check()  ◄─────────────────────┤
      │   asks apt, snapd, Flatpak and fwupd      │
      │   what they would update (no password)   ▼
      │                               engine.py  refresh() / Run
      │                               │   plans a run, streams events
      │                               │
      └── uses the queries in ──►  helper.py  ◄── pkexec (as root)
                                   refresh and upgrade, one JSON event per line
```

- **`helper.py`** is the only code that runs as root. It has two commands:
  `refresh` (apt-get update and fwupd metadata) and `upgrade TASK...`. It
  accepts only task names from `TASKS` and range-checked numbers, never paths,
  commands or package names. It imports nothing outside the Python standard
  library, because it is installed on its own and runs with `python3 -I`. It
  also holds the parsers and queries that the unprivileged check reuses, so
  the app shows exactly what the helper will do.
- **`updates.py`** defines every task (`TASKS`: an id, a title, an icon, a
  group, a description and whether it is on by default) and `check()`, which
  runs the source checks in parallel and returns a `CheckResult`. Checking
  never changes anything.
- **`engine.py`** turns a selection into a `Plan`, starts the helper through
  `pkexec` (directly when already root) and turns its output into
  `RunEvent`s for the app and the command line. `Run.stop()` writes `stop` to
  the helper's stdin. It also updates the user's own Flatpak installation in
  process, since that must not run as root.
- **`history.py`** reads the history the helper writes; **`settings.py`**
  keeps preferences in `~/.config/system-update/settings.json`.

### The helper's protocol

Each line on the helper's stdout is one JSON object with an `event` key:

| Event | Fields | Meaning |
| --- | --- | --- |
| `log` | `path` | the log file of this run |
| `start` | `task` | a task started |
| `progress` | `task`, `fraction` (0–1), `text` | for apt from `APT::Status-Fd`, for snaps from the snapd change |
| `output` | `task`, `text` | one line of command output |
| `message` | `level` (`info`, `ok`, `warning`, `error`), `text` | the helper's own notes, also written to the log |
| `waiting` | `task`, `holders`, `waited`, `limit` | another program holds the apt/dpkg locks |
| `done` | `task`, `state` (`done`, `warning`, `failed`, `skipped`), `count`, `freed`, `message`, `items` | a task finished |
| `stopping` | | a `stop` request was received |
| `fatal` | `reason` (`busy`, `offline`, `helper`), `text` | the run could not start |
| `finished` | `reboot_required`, `reboot_packages`, `problems`, `stopped`, `log` | the run is over |

Exit status: 0 success, 1 some task failed, 2 bad arguments or not root, 3
could not start. pkexec itself exits with 126 when authentication is
cancelled and 127 when it is refused; `engine.AUTH_MESSAGES` turns these into
readable messages.

### Rules the helper follows

These come from `system-update.sh`, and every change to `helper.py` must keep
them:

- **Never prompt.** Commands run with stdin closed and a non-interactive
  environment (`command_env()`); apt keeps existing configuration files.
- **Never interrupt apt or dpkg.** They run without a time limit, in their own
  session so Ctrl+C in a terminal can't reach them, and the helper ignores
  SIGINT and SIGHUP. Only snap, Flatpak and fwupd clients get a timeout,
  because a service owns their transaction.
- **One run at a time.** `acquire_instance_lock()` takes
  `/run/lock/system-update.lock` with `flock`, the same lock the script took.
  `wait_for_apt()` waits for processes that have apt's lock files open.
- **Log like the script did.** `/var/log/system-update/update-YYYYMMDD-HHMMSS.log`
  with `date | [LEVEL] message` lines; the 10 newest are kept. The helper makes
  the folder `root:adm 2750` so administrators can read the logs.

### Privileges and polkit

`data/io.github.wakedog.SystemUpdate.policy` has two actions for the one
program `/usr/libexec/system-update/system-update-helper`, chosen by its first
argument (the `org.freedesktop.policykit.exec.argv1` annotation):

- `…SystemUpdate.refresh`: `allow_active` is `yes`, so the person at the
  computer can check for updates without a password, as with Ubuntu's own
  Software Updater. The `refresh` command accepts nothing but `--firmware`.
- `…SystemUpdate.upgrade`: `auth_admin_keep`, so the password is asked once
  and remembered briefly.

When running from the source tree the helper is `system_update/helper.py`,
which the policy does not cover. pkexec then shows its generic "run a program
as administrator" prompt for both commands. That is expected, and it is why
the app only refreshes by itself when installed. Install the `.deb` to test
the real prompts.

## Adding an update source or maintenance step

For example, a "Docker images" maintenance step. Items marked *(tested)* are
enforced by a test or by `make check`.

1. **Helper task.** In `helper.py`, add the id to `UPDATE_TASKS` or
   `CLEANUP_TASKS` (the order there is the order steps run), add a title to
   `TITLES`, and write `task_docker(ctx) -> TaskResult`. Run commands with
   `ctx.run([...fixed arguments...], "docker", timeout=...)`, and return
   `count`, `freed` and a short `message`. Raise `TaskError` when it fails.
   Register it in `TASK_FUNCTIONS`.
2. **Task definition.** Add a `Task` to `updates.TASKS` in the same position
   *(tested)*, with a symbolic icon *(tested)*, a description, `default=False`
   if it could surprise people, and `available=` when it needs a tool that may
   be missing.
3. **Check.** For an update source, add a `check_docker() -> SourceStatus` to
   `updates.CHECKS`. For a maintenance step, you can add an estimate to
   `estimate_cleanup()`. Put any parsing in `helper.py` so both sides share it.
4. **Command line.** Add a `--no-docker` option in `cli.build_parser()` and
   `cli_plan()` if people should be able to skip it, and add new options to
   `data/system-update.bash-completion` *(tested)*.
5. **Docs** (not tested): the step table in the README, the `upgrade` section
   of the man page (`data/system-update.1`), and the AppStream `<description>`.
6. **Tests:** parsers go in `tests/test_helper.py`; use a fake command made with
   `write_script()` (see `AptTaskTests`) rather than the real tool. Tests use
   a temporary `HOME` and point the helper's system paths into it
   (`tests/support.py`); never touch real system files in tests.
7. `make check`, then try it: `python3 -m system_update upgrade --dry-run`.

## Settings

New preferences go in the `Settings` dataclass in `settings.py`, with limits
in `LIMITS` (numbers, clamped on load) or names in `FLAGS` (booleans). If the
helper needs the value, add an argument with a strict `_bounded_int` range to
`helper.parse_args`, pass it from `engine.Run._run_helper`, and add it to the
`Plan`. Add a row in `gui/dialogs.PreferencesDialog` and an option in
`cli.py`.

## GUI notes

- Keep compatibility with libadwaita 1.5. Newer widgets are used only after a
  feature check, for example `Adw.Spinner`, and `Adw.ShortcutsDialog` behind
  `hasattr`. Follow the same pattern for anything newer.
- Work in background threads must hand results to GTK with
  `GLib.idle_add`. Never touch widgets from a thread. Command output arrives
  fast, so `MainWindow._on_run_event_threaded` batches it and flushes a few
  times a second.
- Colors are in `gui/style.css`. Icon colors mix a palette color with
  `@window_fg_color` so they suit both the light and dark style; check both.
- While updating, the window asks GNOME not to log out or suspend
  (`Gtk.Application.inhibit`), and refuses to close.

## Screenshots and a GUI smoke test

`tools/screenshots.py` renders every screen with sample data on a hidden GTK
Broadway display and writes PNGs to `build/screenshots/`. It needs
`gtk4-broadwayd` (package `libgtk-4-bin`). After visible UI changes, run
`tools/screenshots.py --update-readme` to also refresh the four images the
README uses in `data/screenshots/`, and commit those.

## Releasing a new version

1. Bump `__version__` in `system_update/__init__.py` and `VERSION` in
   `system_update/helper.py` *(tested)*.
2. Add a `<release version="X.Y.Z" date="YYYY-MM-DD">` entry at the top of
   `<releases>` in the AppStream metadata *(tested)*, and update
   `RELEASE_NOTES` in `gui/dialogs.py` to match.
3. Update the version and month in the first line of `data/system-update.1`
   *(tested)*, and the `.deb` file name in the README install section.
4. Add an entry at the top of the changelog written by `packaging/build-deb.sh`.
5. `make check`, then `make deb`, install it and run a real update.
6. Commit, then tag and push:

   ```bash
   git tag v1.0.1
   git push origin main v1.0.1
   ```

   The **Release** workflow checks that the tag matches `__version__`, runs
   the checks, builds the `.deb` and attaches it to a new GitHub Release.

## Changing the app ID or GitHub account

The app ID `io.github.wakedog.SystemUpdate` is the reverse domain of
`wakedog.github.io` and appears in file names under `data/`, in
`system_update/__init__.py`, the `Makefile`, `packaging/build-deb.sh` and the
polkit action ids. The GitHub URL is `HOMEPAGE` in `system_update/__init__.py`,
the `<url>` entries in the metainfo, the policy's `<vendor_url>` and
`Homepage:` in `build-deb.sh`. To move the project, replace both everywhere:

```bash
git grep -l 'wakedog' | xargs sed -i 's/wakedog/NEWNAME/g'
for f in $(git ls-files 'data/*wakedog*'); do git mv "$f" "${f//wakedog/NEWNAME}"; done
make check
```

## Known gaps

- A real update through pkexec, as root, has been tested with fake commands
  and a fake helper, but should be tried once on a real install after any
  change to `helper.py` or the polkit policy.
- Flatpak support is tested against recorded output only; the development
  machine has no Flatpak.
- The snapd refresh and revision removal use snapd's REST API; their
  progress parsing is tested against recorded responses.
- Ubuntu 24.04 is covered by CI for tests and imports, but the GUI has only
  been used on 26.04.
- There are no translations yet. User-visible strings are plain Python
  strings, not wrapped in gettext.

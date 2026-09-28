# Contributing

Thanks for helping improve System Update.

- **Bugs and ideas:** open an [issue](https://github.com/wakedog/system-update/issues).
  For bugs, include the output of **About → Troubleshooting → Debug
  Information** in the app, and the log of the run that went wrong
  (**Main menu → Open Log Folder**, or `/var/log/system-update/`).
- **Code changes:** read [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for setup,
  how the code is organized and how to add a step. Run `make check`
  before opening a pull request. CI runs the same checks on Ubuntu 24.04.

Ground rules:

- Checking for updates never changes anything, and nothing is installed
  unless the user asked for it.
- Code that runs as root lives only in `system_update/helper.py`. It uses the
  standard library only and never accepts paths, commands or package names
  from its caller.
- Nothing may stop to ask a question, and apt or dpkg must never be
  interrupted: no timeouts on them, and no signals.
- Stay compatible with Ubuntu 24.04 (Python 3.12, GTK 4.14, libadwaita 1.5).

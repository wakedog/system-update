"""Small helpers shared by the command-line and graphical interfaces."""

from __future__ import annotations

import time


def format_size(size: int | None) -> str:
    """Format a byte count the same way GLib.format_size() does (SI units)."""
    if size is None:
        return "Unknown"
    if size < 1000:
        return "1 byte" if size == 1 else f"{size} bytes"
    value = float(size)
    for unit in ("kB", "MB", "GB", "TB", "PB"):
        value /= 1000
        if value < 1000 or unit == "PB":
            return f"{value:.1f} {unit}"
    raise AssertionError("unreachable")


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    word = singular if count == 1 else (plural_form or singular + "s")
    return f"{count:,} {word}"


def format_duration(seconds: float) -> str:
    """"45 seconds", "3 minutes 20 seconds", "1 hour 5 minutes"."""
    seconds = max(0, round(seconds))
    if seconds < 60:
        return plural(seconds, "second")
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        text = plural(minutes, "minute")
        return f"{text} {plural(seconds, 'second')}" if seconds and minutes < 10 else text
    hours, minutes = divmod(minutes, 60)
    return plural(hours, "hour") + (f" {plural(minutes, 'minute')}" if minutes else "")


def format_ago(timestamp: float, now: float | None = None) -> str:
    """"just now", "5 minutes ago", "3 hours ago", "2 days ago"."""
    elapsed = (now if now is not None else time.time()) - timestamp
    if elapsed < 60:
        return "just now"
    if elapsed < 3600:
        return plural(int(elapsed // 60), "minute") + " ago"
    if elapsed < 86400:
        return plural(int(elapsed // 3600), "hour") + " ago"
    days = int(elapsed // 86400)
    return "yesterday" if days == 1 else f"{days} days ago"

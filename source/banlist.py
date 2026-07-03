"""Startup ban check.

Refuses to load the manager if any Steam account that has signed in on this
machine matches an entry in banlist.txt (bundled into the exe at build time).

No-op in source runs: when sys._MEIPASS is missing, check() returns None.
"""
from __future__ import annotations

import sys
from pathlib import Path

_BANLIST_NAME = "banlist.txt"


def _banlist_path() -> Path | None:
    """Return the bundled banlist.txt path, or None when running from source."""
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return None
    p = Path(meipass) / _BANLIST_NAME
    return p if p.is_file() else None


def _parse(path: Path) -> dict:
    """Return {steamid64: reason}.  Skips blank lines and '#' comments."""
    out: dict = {}
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        sid, _, reason = line.partition(":")
        sid = sid.strip()
        if sid.isdigit() and len(sid) == 17:
            out[sid] = reason.strip() or "(no reason given)"
    return out


def check():
    """Return (persona_name, reason) for the first banned account that has
    signed in on this machine, or None if none match / check is disabled."""
    path = _banlist_path()
    if path is None:
        return None
    bans = _parse(path)
    if not bans:
        return None
    try:
        from ruse_mod_engine.steam import list_known_steam_users
        users = list_known_steam_users()
    except Exception:
        return None
    for user in users:
        sid = user.get("steamid64", "")
        if sid in bans:
            name = user.get("persona_name") or user.get("account_name") or sid
            return (name, bans[sid])
    return None

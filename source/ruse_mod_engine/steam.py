"""
Steam integration utilities for the R.U.S.E. Mod Manager.

Provides:
  find_steam_path()         — locate the Steam installation root
  find_ruse_game_dirs()     — locate R.U.S.E. COMPAT and R.U.S.E. install dirs
  find_steam_profile_dirs() — locate Steam userdata profile dirs for a given app
  list_known_steam_users()  — Steam accounts that have signed in on this machine
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# Steam App IDs for R.U.S.E.
RUSE_COMPAT_APPID = "21970"   # Original / R.U.S.E. COMPAT
RUSE_PUBLIC_APPID = "190852"  # Steam re-release / R.U.S.E.

_STEAM_DEFAULT_PATHS = [
    Path("C:/Program Files (x86)/Steam"),
    Path("C:/Program Files/Steam"),
]


def find_steam_path() -> Optional[Path]:
    """Return the Steam installation root directory, or None if not found.

    Checks the Windows registry first, then falls back to common install paths.
    """
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
        val, _ = winreg.QueryValueEx(key, "SteamPath")
        winreg.CloseKey(key)
        p = Path(val)
        if p.is_dir():
            return p
    except Exception:
        pass

    for candidate in _STEAM_DEFAULT_PATHS:
        if candidate.is_dir():
            return candidate

    return None


def _parse_vdf_paths(vdf_path: Path) -> list:
    """Extract all library root paths from a Steam libraryfolders VDF file."""
    paths = []
    try:
        text = vdf_path.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r'"path"\s+"([^"]+)"', text):
            p = Path(m.group(1))
            if p.is_dir():
                paths.append(p)
    except Exception:
        pass
    return paths


def _find_steam_libraries(steam_path: Path) -> list:
    """Return all Steam library roots, including the Steam install dir itself."""
    libraries = [steam_path]
    for vdf_rel in ("config/libraryfolders.vdf", "steamapps/libraryfolders.vdf"):
        vdf = steam_path / vdf_rel
        if vdf.exists():
            for p in _parse_vdf_paths(vdf):
                if p not in libraries:
                    libraries.append(p)
    return libraries


def _read_appmanifest(library: Path, appid: str) -> Optional[str]:
    """Return the installdir field from an appmanifest ACF, or None."""
    acf = library / "steamapps" / f"appmanifest_{appid}.acf"
    if not acf.exists():
        return None
    try:
        text = acf.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r'"installdir"\s+"([^"]+)"', text)
        return m.group(1) if m else None
    except Exception:
        return None


def find_ruse_game_dirs() -> dict:
    """Find R.U.S.E. game installation directories across all Steam libraries.

    Returns a dict::

        {
            "compat": Path | None,   # R.U.S.E. COMPAT (AppID 21970)
            "public": Path | None,   # R.U.S.E. (AppID 190852)
        }

    Each value is the game root (the folder containing Ruse.exe and Data/)
    or None if that version is not installed.
    """
    steam = find_steam_path()
    if steam is None:
        return {"compat": None, "public": None}

    libraries = _find_steam_libraries(steam)
    result = {"compat": None, "public": None}

    for lib in libraries:
        for key, appid in (("compat", RUSE_COMPAT_APPID), ("public", RUSE_PUBLIC_APPID)):
            if result[key] is not None:
                continue
            installdir = _read_appmanifest(lib, appid)
            if not installdir:
                continue
            candidate = lib / "steamapps" / "common" / installdir
            if (candidate / "Ruse.exe").exists() or (candidate / "Data").is_dir():
                result[key] = candidate

    return result


def find_steam_profile_dirs(appid: str = RUSE_COMPAT_APPID) -> list:
    """Return all Steam userdata profile dirs for the given app that exist.

    Searches Steam/userdata/<account>/<appid>/{local,remote}/.
    """
    steam = find_steam_path()
    if steam is None:
        return []
    userdata = steam / "userdata"
    if not userdata.is_dir():
        return []
    dirs = []
    for account in userdata.iterdir():
        if not account.is_dir():
            continue
        for sub in ("local", "remote"):
            d = account / appid / sub
            if d.is_dir():
                dirs.append(d)
    return dirs


# 17-digit SteamID64s for personal accounts all start with 7656119; this prefix
# lets us pick the user-record headers out of loginusers.vdf without a real parser.
_STEAMID64_RE = re.compile(r'^\s*"(7656119\d{10})"\s*$')
_VDF_KV_RE    = re.compile(r'^\s*"([^"]+)"\s+"([^"]*)"\s*$')


def list_known_steam_users() -> list:
    """Return every Steam account that has signed in on this machine.

    Reads Steam/config/loginusers.vdf and returns one dict per user record::

        {"steamid64": "76561198...", "persona_name": "...",
         "account_name": "...", "most_recent": bool}

    Empty list if Steam isn't installed or loginusers.vdf is missing/unreadable.
    """
    steam = find_steam_path()
    if steam is None:
        return []
    vdf = steam / "config" / "loginusers.vdf"
    if not vdf.is_file():
        return []
    try:
        text = vdf.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []

    users: list = []
    current: dict | None = None
    depth = 0  # brace depth inside the current user record (1 = top level of that user's block)
    for line in text.splitlines():
        s = line.strip()
        if current is None:
            m = _STEAMID64_RE.match(line)
            if m:
                current = {"steamid64": m.group(1), "persona_name": "",
                           "account_name": "", "most_recent": False}
                depth = 0
            continue
        if s == "{":
            depth += 1
            continue
        if s == "}":
            depth -= 1
            if depth <= 0:
                users.append(current)
                current = None
            continue
        kv = _VDF_KV_RE.match(line)
        if not kv:
            continue
        key, val = kv.group(1), kv.group(2)
        if key == "PersonaName":
            current["persona_name"] = val
        elif key == "AccountName":
            current["account_name"] = val
        elif key == "MostRecent":
            current["most_recent"] = (val == "1")
    return users

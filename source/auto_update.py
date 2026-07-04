"""
RUSE Mod Manager — In-Exe Auto-Update
======================================
At startup the exe queries the GitHub releases API for the latest tag. If a strictly newer
version exists, the user is prompted:
  Yes -> download the new (version-named) exe next to the current one, hand its launch to the
         shell (explorer.exe) so it starts OUTSIDE our process/job with no console window, then
         exit.  The new instance deletes the now-stale old exe on startup (cleanup_old_exes) —
         the old exe is a different file, so nothing has to delete the running exe itself.  This
         replaces the old .bat relauncher, whose visible cmd window alarmed users.
  No  -> close the application immediately (sys.exit(0)). That is the entire guard rail.

Silently skipped when:
  - running from source (not a PyInstaller --onefile exe), or _version.py is missing
  - the network call fails / times out / rate-limits
  - the latest tag is <= our embedded version
  - the latest release is missing the expected RUSE_ModManager_v<X>.exe asset

The version is embedded by build.py via _version.py (gitignored, regenerated each build).
"""
import json
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from pathlib import Path
from tkinter import messagebox, ttk

import ui_util

try:
    from i18n import t
except ImportError:
    def t(s, **fmt):
        return s.format(**fmt) if fmt else s


REPO = "LittleGroove/RUSE-Mod-Manager"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET_NAME_TEMPLATE = "RUSE_ModManager_v{version}.exe"
API_TIMEOUT = 4         # seconds — caps how long startup waits for GitHub
DOWNLOAD_TIMEOUT = 30   # seconds — initial-connect timeout for the asset download
DOWNLOAD_CHUNK = 64 * 1024


def current_version():
    """Embedded build version, or None when running from source / no _version.py.

    Gated on sys.frozen too so a dev `python mod_manager.py` never prompts even if a stale
    _version.py sits in the working tree."""
    if not getattr(sys, "frozen", False):
        return None
    try:
        from _version import __version__
        return __version__
    except ImportError:
        return None


def fetch_latest():
    req = urllib.request.Request(
        RELEASES_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "RUSE-ModManager"},
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


_VER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def _parse(v):
    m = _VER_RE.match((v or "").strip())
    return tuple(int(g) for g in m.groups()) if m else None


def is_newer(latest, current):
    a, b = _parse(latest), _parse(current)
    return bool(a and b and a > b)


def _find_exe_asset(release, version):
    target = ASSET_NAME_TEMPLATE.format(version=version)
    for asset in release.get("assets") or []:
        if asset.get("name") == target:
            return asset.get("browser_download_url")
    return None


def prompt_update(parent, current, latest):
    return ui_util.confirm(
        parent,
        t("Update available"),
        t("A new version is available: {current} → {latest}\n\n"
          "Update now? Selecting No will close the application.",
          current=current, latest=latest),
    )


def _download_with_progress(parent, url, dest_path):
    """Stream URL -> dest_path while pumping a small Tk progress Toplevel. Raises on failure."""
    # Non-modal (it pumps parent.update() while downloading); themed_toplevel centres it over the app.
    win = ui_util.themed_toplevel(parent, t("Downloading update..."),
                                  resizable=False, modal=False)
    win.protocol("WM_DELETE_WINDOW", lambda: None)   # close-box does nothing during the swap
    ttk.Label(win, text=t("Downloading update...")).pack(padx=20, pady=(15, 5))
    pb = ttk.Progressbar(win, length=320, mode="indeterminate")
    pb.pack(padx=20, pady=(0, 15))
    pb.start(50)
    win.update()                                     # force it visible + centred before the blocking download

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "RUSE-ModManager"})
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp, open(dest_path, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            if total:
                pb.stop()
                pb.configure(mode="determinate", maximum=total, value=0)
            written = 0
            while True:
                chunk = resp.read(DOWNLOAD_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
                if total:
                    pb.configure(value=written)
                parent.update()
    finally:
        try:
            pb.stop()
            win.destroy()
        except Exception:
            pass


def _update_shortcuts(old_exe, new_exe):
    """Best-effort: rewrite any Desktop / Start Menu / Pinned-to-taskbar .lnk whose TargetPath
    equals the running exe so it points at the new versioned filename — including its icon source
    (our shortcuts use the exe itself as the icon, so it would otherwise blank out once the old exe
    is cleaned up).

    Pure ctypes against IShellLinkW + IPersistFile (the same COM interfaces WScript.Shell uses
    internally).  No PowerShell, cscript, or other scripting host is invoked — works under any
    locked-down policy as long as Windows itself works.  Best-effort: any failure (a corrupt
    .lnk, COM hiccup, missing permissions on a roaming Start Menu entry) is swallowed so a
    stale shortcut never blocks the relaunch."""
    import os
    import ctypes
    from ctypes import wintypes, byref, c_void_p, c_int, c_ulong, c_wchar_p, POINTER, WINFUNCTYPE

    CLSCTX_INPROC_SERVER = 0x1
    STGM_READWRITE       = 0x00000002
    MAX_PATH             = 260

    # ── COM GUID structure + parser ───────────────────────────────────────────
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    try:
        ole32 = ctypes.OleDLL("ole32.dll")
    except OSError:
        return

    def _guid(s):
        g = GUID()
        ole32.CLSIDFromString(c_wchar_p(s), byref(g))
        return g

    try:
        CLSID_ShellLink  = _guid("{00021401-0000-0000-C000-000000000046}")
        IID_IShellLinkW  = _guid("{000214F9-0000-0000-C000-000000000046}")
        IID_IPersistFile = _guid("{0000010B-0000-0000-C000-000000000046}")
    except OSError:
        return

    # ── COM method signatures (this-call: first arg is the COM ptr) ───────────
    # IShellLinkW vtable: 0=QueryInterface, 1=AddRef, 2=Release, 3=GetPath,
    #                     ..., 9=SetWorkingDirectory, ..., 20=SetPath
    # IPersistFile vtable: 0=QI, 1=AddRef, 2=Release, ..., 5=Load, 6=Save
    QI_t          = WINFUNCTYPE(c_int,  c_void_p, POINTER(GUID), POINTER(c_void_p))
    Release_t     = WINFUNCTYPE(c_ulong, c_void_p)
    GetPath_t     = WINFUNCTYPE(c_int,  c_void_p, c_wchar_p, c_int, c_void_p, c_ulong)
    SetWorkDir_t  = WINFUNCTYPE(c_int,  c_void_p, c_wchar_p)
    SetPath_t     = WINFUNCTYPE(c_int,  c_void_p, c_wchar_p)
    GetIconLoc_t  = WINFUNCTYPE(c_int,  c_void_p, c_wchar_p, c_int, POINTER(c_int))
    SetIconLoc_t  = WINFUNCTYPE(c_int,  c_void_p, c_wchar_p, c_int)
    Load_t        = WINFUNCTYPE(c_int,  c_void_p, c_wchar_p, c_ulong)
    Save_t        = WINFUNCTYPE(c_int,  c_void_p, c_wchar_p, wintypes.BOOL)

    SLOT_QI                  = 0
    SLOT_Release             = 2
    SL_SLOT_GetPath          = 3
    SL_SLOT_SetWorkDir       = 9
    SL_SLOT_GetIconLoc       = 16
    SL_SLOT_SetIconLoc       = 17
    SL_SLOT_SetPath          = 20
    PF_SLOT_Load             = 5
    PF_SLOT_Save             = 6
    PTR_SIZE = ctypes.sizeof(c_void_p)

    def _vcall(com_ptr, slot, fntype, *args):
        """Invoke a virtual method by vtable slot on a COM pointer."""
        addr = com_ptr.value if isinstance(com_ptr, c_void_p) else com_ptr
        if not addr:
            return -1
        vtbl_addr = ctypes.cast(addr, POINTER(c_void_p))[0]
        fn_addr = ctypes.cast(vtbl_addr + slot * PTR_SIZE, POINTER(c_void_p))[0]
        return ctypes.cast(fn_addr, fntype)(addr, *args)

    # ── Collect candidate .lnk files in the standard shortcut locations ───────
    locations = []
    for envvar, sub in (("USERPROFILE",  "Desktop"),
                        ("PUBLIC",       "Desktop"),
                        ("APPDATA",      r"Microsoft\Windows\Start Menu\Programs"),
                        ("ProgramData",  r"Microsoft\Windows\Start Menu\Programs"),
                        ("APPDATA",      r"Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar")):
        base = os.environ.get(envvar)
        if base:
            locations.append(Path(base) / sub)

    candidates = []
    for loc in locations:
        try:
            if loc.is_dir():
                candidates.extend(loc.rglob("*.lnk"))
        except Exception:
            pass
    if not candidates:
        return

    if ole32.CoInitialize(None) < 0:
        return

    try:
        old_str = str(old_exe).lower()
        new_str = str(new_exe)
        new_dir = str(new_exe.parent)

        for lnk in candidates:
            sl = c_void_p()
            pf = c_void_p()
            try:
                hr = ole32.CoCreateInstance(byref(CLSID_ShellLink), None,
                                            CLSCTX_INPROC_SERVER,
                                            byref(IID_IShellLinkW),
                                            byref(sl))
                if hr < 0 or not sl.value:
                    continue

                hr = _vcall(sl, SLOT_QI, QI_t, byref(IID_IPersistFile), byref(pf))
                if hr < 0 or not pf.value:
                    continue

                hr = _vcall(pf, PF_SLOT_Load, Load_t, c_wchar_p(str(lnk)), STGM_READWRITE)
                if hr < 0:
                    continue

                buf = ctypes.create_unicode_buffer(MAX_PATH)
                hr = _vcall(sl, SL_SLOT_GetPath, GetPath_t, buf, MAX_PATH, None, 0)
                if hr < 0 or buf.value.lower() != old_str:
                    continue

                if _vcall(sl, SL_SLOT_SetPath, SetPath_t, c_wchar_p(new_str)) < 0:
                    continue
                _vcall(sl, SL_SLOT_SetWorkDir, SetWorkDir_t, c_wchar_p(new_dir))
                # Repoint the icon too: our shortcuts use the exe ITSELF as the icon source, so one
                # left pointing at the old (about-to-be-deleted) exe would show a blank icon after
                # cleanup. Only rewrite when the icon currently resolves to the old exe, so a custom
                # icon a user set by hand is left untouched.
                icon_buf = ctypes.create_unicode_buffer(MAX_PATH)
                icon_idx = c_int(0)
                if _vcall(sl, SL_SLOT_GetIconLoc, GetIconLoc_t, icon_buf, MAX_PATH, byref(icon_idx)) >= 0 \
                        and icon_buf.value.lower() == old_str:
                    _vcall(sl, SL_SLOT_SetIconLoc, SetIconLoc_t, c_wchar_p(new_str), icon_idx.value)
                _vcall(pf, PF_SLOT_Save, Save_t, None, 1)
            except Exception:
                pass   # one bad shortcut shouldn't poison the rest of the sweep
            finally:
                if pf.value:
                    _vcall(pf, SLOT_Release, Release_t)
                if sl.value:
                    _vcall(sl, SLOT_Release, Release_t)
    finally:
        ole32.CoUninitialize()


_NAME_VER_RE = re.compile(r"^RUSE_ModManager_v(\d+\.\d+\.\d+)\.exe(\.tmp)?$", re.IGNORECASE)


def _name_version(name):
    """The (major, minor, patch) tuple encoded in a RUSE_ModManager_v<X>.exe filename, or None."""
    m = _NAME_VER_RE.match(name)
    return _parse(m.group(1)) if m else None


def _delete_with_retry(path, attempts=8, delay=0.4):
    """Best-effort delete that tolerates the previous process still releasing its file lock."""
    for i in range(attempts):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if i < attempts - 1:
                time.sleep(delay)
    return False


def cleanup_old_exes():
    """Delete stale older-version ``RUSE_ModManager_v*.exe`` files next to the running exe — e.g. the
    one a just-applied update replaced (the update launches us, then we remove the predecessor here,
    since a running exe can't delete itself but a NEW exe can delete the OLD one).  Also clears any
    leftover ``*.exe.tmp`` partial downloads.  Best-effort, on a background thread: the previous
    process may briefly still hold its exe lock after the relaunch, so we retry and give up quietly
    if it's still busy (the next launch will get it).  No-op when running from source."""
    if not getattr(sys, "frozen", False):
        return
    try:
        cur = Path(sys.executable).resolve()
        cur_ver = _parse(current_version())
        exe_dir = cur.parent
    except Exception:
        return
    if cur_ver is None:
        return

    def _sweep():
        for p in exe_dir.glob("RUSE_ModManager_v*.exe*"):
            name = p.name
            try:
                if p.resolve() == cur:
                    continue   # never delete ourselves
            except Exception:
                continue
            if name.lower().endswith(".tmp"):
                _delete_with_retry(p)            # partial download — always junk
                continue
            ver = _name_version(name)
            if ver is not None and ver < cur_ver:  # only strictly-older real exes
                _delete_with_retry(p)

    threading.Thread(target=_sweep, daemon=True).start()


def download_and_relaunch(parent, asset_url, latest_version):
    """Yes-path. Download, atomic-swap into final filename, spawn relauncher, exit."""
    exe_path = Path(sys.executable).resolve()
    exe_dir = exe_path.parent
    new_name = ASSET_NAME_TEMPLATE.format(version=latest_version)
    tmp_path = exe_dir / (new_name + ".tmp")
    final_path = exe_dir / new_name

    try:
        _download_with_progress(parent, asset_url, tmp_path)
        if final_path.exists():
            final_path.unlink()
        tmp_path.rename(final_path)
    except Exception as e:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        ui_util.error(
            parent,
            t("Update failed"),
            t("Update failed: {error}", error=str(e)),
        )
        try:
            parent.destroy()
        except Exception:
            pass
        sys.exit(1)

    # Repoint any Desktop / Start Menu / Pinned-to-taskbar shortcuts at the new exe BEFORE we hand
    # off (the old exe gets cleaned up shortly, so stale shortcuts would otherwise break).
    _update_shortcuts(exe_path, final_path)

    # Hand the launch to the running shell so the new exe starts OUTSIDE this process/job — silently
    # (no cmd window — the old .bat flashed one and alarmed users) and without pinning our temp dir.
    # The old exe is a different file; the new instance deletes it on startup via cleanup_old_exes().
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer.exe", str(final_path)], close_fds=True)
        else:
            subprocess.Popen([str(final_path)], cwd=str(exe_dir), close_fds=True)
    except Exception as e:
        # Couldn't relaunch — don't strand the user. Keep the (old) app running so they can restart
        # manually; the downloaded new exe stays in place for next time.
        ui_util.error(
            parent,
            t("Update failed"),
            t("Downloaded the update but couldn't start it: {error}\n\n"
              "Please restart the app manually.", error=str(e)),
        )
        return
    try:
        parent.destroy()
    except Exception:
        pass
    sys.exit(0)


def run_startup_check(parent):
    """Top-level entry. Called from ModManagerApp.__init__ before the main window is shown.

    Skips silently on any prerequisite failure (offline, dev run, no newer release). On a Yes
    the process is replaced before init continues; on a No the process exits.  On the Yes/No paths
    this function does not return (it exits); otherwise it returns and startup continues."""
    # Every launch sweeps stale older-version exes — this is what removes the exe a prior update
    # replaced (the new instance deletes the old one, which the old running process couldn't).
    cleanup_old_exes()
    try:
        current = current_version()
        if not current:
            return
        release = fetch_latest()
        if not release:
            return
        latest = (release.get("tag_name") or "").lstrip("v")
        if not latest or not is_newer(latest, current):
            return
        asset_url = _find_exe_asset(release, latest)
        if not asset_url:
            print(f"[auto_update] release v{latest} has no "
                  f"{ASSET_NAME_TEMPLATE.format(version=latest)} asset; skipping")
            return
        if prompt_update(parent, current, latest):
            download_and_relaunch(parent, asset_url, latest)
        else:
            try:
                parent.destroy()
            except Exception:
                pass
            sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        print(f"[auto_update] unexpected error: {e}; continuing startup")
        return

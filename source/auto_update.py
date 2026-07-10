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
from tkinter import ttk

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
        t("update.update_available"),
        t("update.new_version_available_current_latest",
          current=current, latest=latest),
    )


def _download_with_progress(parent, url, dest_path):
    """Stream URL -> dest_path while pumping a small Tk progress Toplevel. Raises on failure."""
    # Non-modal (it pumps parent.update() while downloading); themed_toplevel centres it over the app.
    win = ui_util.themed_toplevel(parent, t("update.downloading_update"),
                                  resizable=False, modal=False)
    win.protocol("WM_DELETE_WINDOW", lambda: None)   # close-box does nothing during the swap
    ttk.Label(win, text=t("update.downloading_update")).pack(padx=20, pady=(15, 5))
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


def _repoint_shortcuts(new_exe, old_exe=None):
    """Best-effort: rewrite Desktop / Start Menu / Pinned-to-taskbar .lnk shortcuts to point at
    ``new_exe`` — BOTH the target and the icon source (our shortcuts use the exe itself as their
    icon, so an icon left on an old versioned filename shows a blank/broken icon once that exe is
    gone).

    Two modes:
      * update (old_exe set)  — match only shortcuts whose target IS ``old_exe`` (the exe this update
                                replaces) and move them to ``new_exe``.
      * heal   (old_exe None) — match any shortcut whose target is one of OUR versioned exes
                                (RUSE_ModManager_v*.exe) in ``new_exe``'s OWN folder, or that already
                                targets ``new_exe``, and make sure its target AND icon are ``new_exe``.
                                This repairs a shortcut whose icon drifted to a long-gone version
                                (updates before this existed never touched the icon) WITHOUT recreating
                                it — no delete, no regenerate.

    The icon is repointed when it's empty or already points at one of our versioned exes; a genuinely
    custom icon a user set by hand is left alone.  Only shortcuts that actually change are saved.

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
    SLGP_RAWPATH         = 0x4          # read the stored target verbatim — don't resolve/search for it
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

    def _is_our_exe(p):
        try:
            return bool(_NAME_VER_RE.match(os.path.basename(p)))
        except Exception:
            return False

    new_str    = str(new_exe)
    new_low    = new_str.lower()
    new_dir    = str(new_exe.parent)
    new_dir_nc = os.path.normcase(new_dir)
    old_low    = str(old_exe).lower() if old_exe is not None else None

    try:
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
                if _vcall(sl, SL_SLOT_GetPath, GetPath_t, buf, MAX_PATH, None, SLGP_RAWPATH) < 0:
                    continue
                target  = buf.value
                tgt_low = target.lower()

                # Is this one of OUR shortcuts we should touch?
                #   update mode: exactly the exe we're replacing.
                #   heal mode:   any of our versioned exes in this exe's folder, or one already on us.
                if old_low is not None:
                    own = (tgt_low == old_low)
                else:
                    own = (tgt_low == new_low) or \
                          (_is_our_exe(target)
                           and os.path.normcase(os.path.dirname(target)) == new_dir_nc)
                if not own:
                    continue

                changed = False
                if tgt_low != new_low:
                    if _vcall(sl, SL_SLOT_SetPath, SetPath_t, c_wchar_p(new_str)) < 0:
                        continue
                    _vcall(sl, SL_SLOT_SetWorkDir, SetWorkDir_t, c_wchar_p(new_dir))
                    changed = True

                # Repoint the icon when it's empty or points at ONE OF OUR versioned exes — this heals
                # an icon stuck on a long-gone version (e.g. v1.0.131).  A genuinely custom icon the
                # user set by hand is left untouched.  Index 0 = the exe's own default icon.
                icon_buf = ctypes.create_unicode_buffer(MAX_PATH)
                icon_idx = c_int(0)
                if _vcall(sl, SL_SLOT_GetIconLoc, GetIconLoc_t, icon_buf, MAX_PATH, byref(icon_idx)) >= 0:
                    icon = icon_buf.value
                    if icon.lower() != new_low and (icon == "" or _is_our_exe(icon)):
                        if _vcall(sl, SL_SLOT_SetIconLoc, SetIconLoc_t, c_wchar_p(new_str), 0) >= 0:
                            changed = True

                if changed:
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
            t("update.update_failed"),
            t("update.update_failed_error", error=str(e)),
        )
        try:
            parent.destroy()
        except Exception:
            pass
        sys.exit(1)

    # Repoint any Desktop / Start Menu / Pinned-to-taskbar shortcuts at the new exe BEFORE we hand
    # off (the old exe gets cleaned up shortly, so stale shortcuts would otherwise break).
    _repoint_shortcuts(final_path, old_exe=exe_path)

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
            t("update.update_failed"),
            t("update.downloaded_update_but_couldn_t", error=str(e)),
        )
        return
    try:
        parent.destroy()
    except Exception:
        pass
    sys.exit(0)


def heal_shortcuts():
    """On startup, silently repair any of OUR Desktop / Start Menu / taskbar shortcuts whose target or
    icon drifted to a version that's no longer here, pointing them at the exe running right now — no
    recreation, no deletion.  This fixes shortcuts left stale by updates from before the icon-repoint
    existed, without waiting for the next update.  Best-effort, off the main thread; no-op from source."""
    if not getattr(sys, "frozen", False):
        return
    try:
        cur = Path(sys.executable).resolve()
    except Exception:
        return
    threading.Thread(target=lambda: _repoint_shortcuts(cur), daemon=True).start()


def run_startup_check(parent):
    """Top-level entry. Called from ModManagerApp.__init__ before the main window is shown.

    Skips silently on any prerequisite failure (offline, dev run, no newer release). On a Yes
    the process is replaced before init continues; on a No the process exits.  On the Yes/No paths
    this function does not return (it exits); otherwise it returns and startup continues."""
    # Every launch sweeps stale older-version exes — this is what removes the exe a prior update
    # replaced (the new instance deletes the old one, which the old running process couldn't).
    cleanup_old_exes()
    heal_shortcuts()   # repair shortcuts whose icon/target drifted to a gone version (non-destructive)
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

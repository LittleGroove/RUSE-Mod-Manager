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

Two channels.  By default only STABLE releases are considered, because GitHub keeps prereleases out
of /releases/latest.  When the user ticks "beta updates" in Settings, the check reads the releases
LIST instead and takes whichever is higher across stable and beta — so a beta tester still gets a
stable release when that one is newer.  Turning the setting back off never downgrades anyone: the
stable release is simply older, so nothing is offered until stable passes the beta they are on.

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
# The LIST endpoint, used ONLY when the user has opted into betas in Settings.  GitHub defines
# /releases/latest as the newest NON-prerelease release, so a beta can never appear there — which is
# exactly what keeps betas invisible to everyone else.  Opting in means reading the full list and
# picking the highest version across both channels ourselves.  per_page=30 is the API default,
# stated outright: releases come back newest-first and our version counter only ever goes up, so the
# newest 30 always contain the highest version.
RELEASES_LIST_API = f"https://api.github.com/repos/{REPO}/releases?per_page=30"


def _platform_suffix():
    """The OS tag used in the release-asset / local-binary filename. Windows keeps the historical
    '.exe' (existing Windows releases must keep updating); Linux/macOS get an OS tag. One codebase,
    OS-aware, so the updater grabs the right program on each platform."""
    if sys.platform == "win32":
        return ".exe"
    if sys.platform == "darwin":
        return "_macos"
    return "_linux_x86_64"


def platform_asset_name(version):
    """Release-asset / binary basename for THIS OS — e.g. RUSE_ModManager_v1.2.3.exe (Windows) or
    RUSE_ModManager_v1.2.3_linux_x86_64 (Linux). build.py names the built binary the SAME way, so
    sys.executable, the GitHub asset, the download target, and cleanup all agree on one name."""
    return f"RUSE_ModManager_v{version}{_platform_suffix()}"


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


def _fetch_json(url):
    """GET `url` as JSON, or None on ANY failure (offline, timeout, rate limit, garbage body).

    A rate-limit 403 arrives as HTTPError, a subclass of URLError, so it is caught here too and the
    startup check simply skips — same as being offline.
    """
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "RUSE-ModManager"},
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def fetch_latest():
    """The newest STABLE release. GitHub excludes prereleases from /releases/latest for us."""
    return _fetch_json(RELEASES_API)


def fetch_releases():
    """Up to the 30 newest releases, newest-first, INCLUDING prereleases. None on any failure."""
    data = _fetch_json(RELEASES_LIST_API)
    return data if isinstance(data, list) else None


_VER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def _parse(v):
    m = _VER_RE.match((v or "").strip())
    return tuple(int(g) for g in m.groups()) if m else None


def is_newer(latest, current):
    a, b = _parse(latest), _parse(current)
    return bool(a and b and a > b)


def bare_version(tag_version):
    """'1.2.3-beta' -> '1.2.3'.  A plain '1.2.3' comes back unchanged.

    Beta releases are TAGGED v<X.Y.Z>-beta but their assets keep the plain
    RUSE_ModManager_v<X.Y.Z>.exe name, because both channels attach the very same built artifact.
    Everything downstream keys off that bare number — the asset lookup, the filename we save as,
    the stale-exe sweep (_name_version) and the shortcut repoint — so the suffix has to come off
    before any of them see it, or a beta user ends up with a file none of them recognise.
    """
    m = _VER_RE.match((tag_version or "").strip())
    return ".".join(m.groups()) if m else tag_version


def pick_release(releases, include_prerelease):
    """The highest-version release in `releases`, or None.

    Drafts are always skipped; prereleases only count when the user opted in.  The version comes
    from the tag via _parse, which matches by PREFIX, so 'v1.2.3-beta' reads as (1, 2, 3) and a beta
    competes on the SAME counter as stable — which is what makes "whichever is higher wins" work
    across the two channels.

    A tie (stable v1.2.3 and beta v1.2.3-beta both published at a full release) resolves to the
    STABLE entry.  It makes no practical difference — the two carry an identical asset — but it
    makes the choice deterministic instead of dependent on GitHub's list order.
    """
    best, best_key = None, None
    for rel in releases or []:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        pre = bool(rel.get("prerelease"))
        if pre and not include_prerelease:
            continue
        v = _parse((rel.get("tag_name") or "").lstrip("v"))
        if v is None:
            continue
        key = (v, 0 if pre else 1)          # same version -> stable wins
        if best_key is None or key > best_key:
            best, best_key = rel, key
    return best


def _find_exe_asset(release, version):
    """The download URL of the release asset for THIS platform (the .exe on Windows, the Linux binary
    on Linux), or None if this release doesn't carry one for us."""
    target = platform_asset_name(version)
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


# Matches THIS platform's binary name (…​.exe on Windows, …_linux_x86_64 on Linux), plus a .tmp partial.
_NAME_VER_RE = re.compile(
    r"^RUSE_ModManager_v(\d+\.\d+\.\d+)" + re.escape(_platform_suffix()) + r"(\.tmp)?$",
    re.IGNORECASE)


def _name_version(name):
    """The (major, minor, patch) tuple encoded in a platform binary filename, or None."""
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
        for p in exe_dir.glob(f"RUSE_ModManager_v*{_platform_suffix()}*"):
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
    new_name = platform_asset_name(latest_version)
    tmp_path = exe_dir / (new_name + ".tmp")
    final_path = exe_dir / new_name

    try:
        _download_with_progress(parent, asset_url, tmp_path)
        if final_path.exists():
            final_path.unlink()
        tmp_path.rename(final_path)
        # Downloaded files aren't executable on Unix; the freshly-downloaded binary must be +x before
        # we can relaunch it (no-op concept on Windows, where the .exe is runnable as-is).
        if sys.platform != "win32":
            final_path.chmod(0o755)
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
    # off (the old exe gets cleaned up shortly, so stale shortcuts would otherwise break). Windows-only:
    # those shortcut types + the Win32 shell APIs don't exist on Linux/macOS.
    if sys.platform == "win32":
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
    existed, without waiting for the next update.  Best-effort, off the main thread; no-op from source.

    Windows-only: Desktop / Start-Menu / taskbar .lnk shortcuts (and the Win32 ctypes shell APIs
    _repoint_shortcuts uses) don't exist on Linux/macOS, so this is a no-op there."""
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        return
    try:
        cur = Path(sys.executable).resolve()
    except Exception:
        return
    threading.Thread(target=lambda: _repoint_shortcuts(cur), daemon=True).start()


def run_startup_housekeeping():
    """Non-interactive startup chores that need no window and never prompt — safe to call early, while
    the main window is still hidden:
      * sweep the stale older-version exe a prior update replaced (the new instance deletes the old one,
        which the old running process couldn't), plus any leftover *.exe.tmp partial downloads;
      * heal Desktop / Start-Menu / taskbar shortcuts whose icon/target drifted to a gone version.
    Both run on background threads and no-op when running from source."""
    cleanup_old_exes()
    heal_shortcuts()   # repair shortcuts whose icon/target drifted to a gone version (non-destructive)


def check_for_update(app, include_prerelease=False):
    """Window-safe update check.  MUST be scheduled via ``after()`` to run AFTER the main window is shown
    and the event loop is live (see ModManagerApp.__init__) — NOT before the window is deiconified.

    Why the ordering is load-bearing: this shows a modal Yes/No prompt.  When the check used to run
    before the window was shown, that modal's parent was a WITHDRAWN (invisible) window — so the prompt
    had no taskbar button and no reliable way to hold the foreground.  A stray click/scroll could bury
    it, and its ``wait_window`` loop would then block forever: the app sat running with no visible,
    clickable window — the "hangs when there's an update, won't open" bug.  Running here instead gives
    the prompt a real, mapped, foreground parent, so it can't be buried.

    Skips silently on any prerequisite failure (offline, dev run, no newer release, missing asset).  On
    Yes it downloads + relaunches + exits; on No it closes the app; otherwise it returns and the app
    keeps running.  The ``sys.exit`` on the Yes/No paths propagates out of the Tk callback and ends
    ``mainloop`` (tkinter's CallWrapper re-raises SystemExit).

    ``include_prerelease`` is the user's Settings opt-in (mod_manager passes
    ``settings["beta_updates"]``).  It defaults to False so the untouched path — and every existing
    caller — makes exactly the /releases/latest call it always has."""
    try:
        current = current_version()
        if not current:
            return
        if include_prerelease:
            # One request, same API_TIMEOUT budget as the stable path.  Deliberately NO fallback to
            # /releases/latest when this fails: it is the same host, so if the list call didn't come
            # back the other wouldn't either, and a second timeout would stall startup for twice as
            # long right when the window has just appeared.
            release = pick_release(fetch_releases(), include_prerelease=True)
        else:
            release = fetch_latest()   # bounded by API_TIMEOUT; typically well under a second
        if not release:
            return
        latest = (release.get("tag_name") or "").lstrip("v")
        if not latest or not is_newer(latest, current):
            return
        # A beta's tag is v<X.Y.Z>-beta but its ASSET is the plain RUSE_ModManager_v<X.Y.Z>.exe, so
        # drop the suffix now that the comparison is done.  On the stable path this is a no-op.
        latest = bare_version(latest)
        asset_url = _find_exe_asset(release, latest)
        if not asset_url:
            print(f"[auto_update] release v{latest} has no "
                  f"{platform_asset_name(latest)} asset; skipping")
            return
        if not app.winfo_exists():                 # window torn down before the deferred check ran
            return
        if prompt_update(app, current, latest):
            download_and_relaunch(app, asset_url, latest)
        else:
            try:
                app.destroy()
            except Exception:
                pass
            sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        print(f"[auto_update] unexpected error: {e}; continuing startup")
        return

"""
Single-instance guard (Windows) for the R.U.S.E. Mod Manager.
================================================================
Users reported launching the app once but ending up with TWO windows / processes.  A onefile
PyInstaller build always shows a bootloader-parent + app-child PAIR of same-named processes — that
much is normal — but two *windows* means two real instances (a double-click race, or the user
launching twice).  This guard makes the SECOND full instance bow out.

Design goals (all failure-safe — a bug here must NEVER stop the app from launching):
  * Use a NAMED Win32 mutex.  The kernel frees it automatically when the owning process dies, so a
    crash can't leave a stale lock that permanently blocks launches (a lock FILE can).
  * Tolerate the RELAUNCH handoff.  ``_restart_app`` and the auto-updater start the new instance via
    explorer.exe while the OLD one is still shutting down, so the new instance can momentarily see the
    mutex held.  We therefore WAIT briefly for the previous owner to exit before giving up — a genuine
    second launch (owner stays alive) still times out and bows out; a handoff (owner exits in ~1s)
    proceeds.
  * Any exception (non-Windows, ctypes quirk, locked-down policy) → return as if we own it, so the app
    always starts.  Being permissive here is strictly safer than blocking a real launch.
"""
import sys

_MUTEX_NAME = "Global\\RuseModManager.FieldOperations.SingleInstance"
# Title prefix of the main window, used to find an instance that is already running.  Every language
# keeps this literal prefix (only the part after the dash is translated), so matching on it works on
# every UI language.  Matching the TITLE — not the process name — is also what lets us tell a healthy
# instance apart from a wedged one: a wedged instance has no window to find.
_WINDOW_TITLE_PREFIX = "R.U.S.E. MOD MANAGER"
_ERROR_ALREADY_EXISTS = 183
_WAIT_MS_FOR_HANDOFF = 4000     # how long a fresh instance waits for a relaunch predecessor to exit
_WAIT_STEP_MS = 200

# Module-level so the handle lives for the whole process lifetime (releasing it would drop the lock).
_HELD_HANDLE = None


def acquire(wait_for_handoff: bool = True) -> bool:
    """Try to become the single running instance.

    Returns ``True`` if we own the instance (caller should continue launching), ``False`` if another
    live instance already holds it (caller should exit quietly).  Never raises — on any error it
    returns ``True`` so a guard failure can't block a legitimate launch.
    """
    global _HELD_HANDLE
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]

        def _create():
            h = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
            return h, ctypes.get_last_error()

        handle, err = _create()
        if not handle:
            return True    # couldn't even create the object — don't block the launch
        if err != _ERROR_ALREADY_EXISTS:
            _HELD_HANDLE = handle       # we created it first → we're the primary instance
            return True

        # Someone else holds it.  During a relaunch handoff that "someone" is our own predecessor,
        # about to exit — so poll for the lock to free up before concluding a real double-launch.
        kernel32.CloseHandle(handle)
        if not wait_for_handoff:
            return False
        waited = 0
        while waited < _WAIT_MS_FOR_HANDOFF:
            kernel32.Sleep(_WAIT_STEP_MS)
            waited += _WAIT_STEP_MS
            handle, err = _create()
            if handle and err != _ERROR_ALREADY_EXISTS:
                _HELD_HANDLE = handle   # predecessor exited → we take over as primary
                return True
            if handle:
                kernel32.CloseHandle(handle)
        return False       # still held after the grace period → a genuine second instance; bow out
    except Exception:
        return True         # any failure → fail open (launch normally)


def find_existing_window():
    """HWND of a main window belonging to ANOTHER live instance, or ``None`` if there isn't one.

    Used only after :func:`acquire` has already told us somebody else holds the lock, to answer the
    one question that decides what to tell the user:

      * a window comes back  -> a perfectly healthy instance is already running (they double-clicked
        twice, or it's minimised and they forgot).  Raise it; that's what they wanted.
      * nothing comes back   -> the holder is alive but has NO window.  That's the wedged/zombie case,
        and the only way out is Task Manager — so we have to SAY so instead of vanishing.

    Minimised windows count as found (``IsIconic``): a minimised app is healthy, and restoring it is
    exactly the right answer.  Our own process is skipped so we can never match ourselves.  Returns
    ``None`` on non-Windows or any error — the caller then just behaves as it always did."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        me = kernel32.GetCurrentProcessId()
        hits = []

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _lparam):
            # A minimised window reports IsWindowVisible=0, so accept IsIconic too — otherwise we'd
            # call a minimised (perfectly healthy) instance "wedged" and tell the user to kill it.
            if not user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == me:
                return True                     # never match our own window
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.upper().startswith(_WINDOW_TITLE_PREFIX):
                hits.append(hwnd)
                return False                    # first match is enough; stop enumerating
            return True

        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        return hits[0] if hits else None
    except Exception:
        return None


def raise_existing_window() -> bool:
    """Bring an already-running instance's window to the front.  ``True`` if we found one and raised
    it — the caller can then exit quietly, because the user now has the window they asked for.

    ``False`` means there was no window to raise, which for a bow-out caller means the holder is
    wedged and the user needs to be told."""
    hwnd = find_existing_window()
    if not hwnd:
        return False
    try:
        import ctypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.ShowWindow(hwnd, 9)              # SW_RESTORE — un-minimise if it was minimised
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def message_box(title: str, text: str) -> None:
    """Show a NATIVE Win32 message box.  Deliberately not a Tk/``ui_util`` dialog: this is called
    before any window exists, and a Tk dialog with no mapped parent is exactly the thing that wedges
    the app invisibly (see ``ui_util.attach_transient``).  MB_SETFOREGROUND|MB_TOPMOST so it can't
    open behind the window that's already there.  Never raises."""
    if sys.platform != "win32":
        try:
            sys.stderr.write(f"{title}: {text}\n")
        except Exception:
            pass
        return
    try:
        import ctypes
        MB_OK, MB_ICONINFORMATION, MB_SETFOREGROUND, MB_TOPMOST = 0x0, 0x40, 0x10000, 0x40000
        ctypes.windll.user32.MessageBoxW(
            0, text, title, MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST)
    except Exception:
        pass

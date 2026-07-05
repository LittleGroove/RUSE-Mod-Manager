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

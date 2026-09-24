"""
On-disk startup diagnostics.
============================
Until this existed, a shipped build produced NOTHING when it failed to start.  ``build.py`` builds
with ``console=False``, so ``sys.stderr`` is None and every ``print`` / stderr write is a silent
no-op; the root logging handler is not attached until ``_build_ui``, so the ``logging`` calls in
early startup (``_bootstrap_folders`` among them) went nowhere too.  When a user reported "it just
won't open" there was no file to ask them for — the whole investigation had to be done by guessing
at reproductions.

So this writes a small, plain-text log of the startup sequence, and the full traceback of anything
that kills it, to a file we can ask the user to send.

Design rules, each one earned:

  * **Never raise, ever.**  This runs before the UI exists; an exception here would itself become a
    failure-to-start.  Every function swallows everything.
  * **Fall back to another folder.**  The single likeliest cause of a failed startup is that the
    folder the app lives in cannot be written to — which is exactly the case where a log file next
    to the exe cannot be created either.  So we try the launch dir, then LOCALAPPDATA, then the temp
    dir, and keep the first that actually accepts a write.  A log that vanishes in the one case that
    matters would be worse than none.
  * **Truncate on each run, cap the size.**  What matters is the launch that just failed, not
    history, and this must never grow without bound in someone's install folder.
  * **Handler level, not root level.**  :func:`attach_logging` puts a WARNING-level handler on the
    root logger.  ``_build_ui`` later sets the ROOT logger to DEBUG for the in-app log view, and
    Pillow logs a DEBUG line per image operation — without a level on this handler, that flood would
    land in this file too.
"""
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

_FILE_NAME = "startup.log"
_APP_DIR = "RuseModManager"
_MAX_BYTES = 256 * 1024

_path = None          # Path we settled on, or None if nowhere was writable
_ready = False


def _launch_dir():
    """The folder the program lives in — next to the exe when frozen, else next to this file."""
    try:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parent
    except Exception:
        return Path(".")


def _candidate_dirs():
    """Where to try putting the log, best first.

    The launch dir is first because that is where a user will actually find it without being told.
    The other two exist for the read-only / permission-denied / antivirus case, which is precisely
    the case worth logging."""
    yield _launch_dir()
    base = (os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
            or os.environ.get("HOME"))
    if base:
        yield Path(base) / _APP_DIR
    try:
        yield Path(tempfile.gettempdir()) / _APP_DIR
    except Exception:
        pass


def init(version=None):
    """Pick a writable location, start a fresh log, and write the header.  Returns the path or None.

    Called as early as possible in ``__main__`` so that anything after it is covered."""
    global _path, _ready
    if _ready:
        return _path
    _ready = True
    for d in _candidate_dirs():
        try:
            d.mkdir(parents=True, exist_ok=True)
            p = d / _FILE_NAME
            # Truncate now: this run's story is the one worth keeping, and it bounds the file.
            with open(p, "w", encoding="utf-8") as f:
                f.write(f"R.U.S.E. Mod Manager startup log — {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"version   : {version or 'source run'}\n")
                f.write(f"executable: {getattr(sys, 'executable', '?')}\n")
                f.write(f"frozen    : {bool(getattr(sys, 'frozen', False))}\n")
                f.write(f"launch dir: {_launch_dir()}\n")
                f.write(f"cwd       : {os.getcwd()}\n")
                f.write(f"platform  : {sys.platform}\n")
                f.write(f"python    : {sys.version.split()[0]}\n")
                f.write("-" * 72 + "\n")
            _path = p
            return _path
        except Exception:
            continue          # this folder is not writable — try the next one
    _path = None              # nowhere worked; every call below becomes a no-op
    return None


def log(message):
    """Append one timestamped line.  Silent no-op if we never found a writable spot."""
    if _path is None:
        return
    try:
        if _path.stat().st_size > _MAX_BYTES:
            return            # capped: keep the beginning, which is where the startup story is
    except Exception:
        pass
    try:
        with open(_path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')}  {message}\n")
    except Exception:
        pass


def exception(message):
    """Append a line plus the traceback of the exception being handled."""
    log(message)
    if _path is None:
        return
    try:
        with open(_path, "a", encoding="utf-8") as f:
            f.write(traceback.format_exc())
            f.write("-" * 72 + "\n")
    except Exception:
        pass


def path():
    """The log file's path as a string, or ``""`` when there is no log."""
    return str(_path) if _path else ""


def attach_logging():
    """Send WARNING-and-worse from the stdlib ``logging`` root into this file.

    That is what finally gives the early ``logging.getLogger(__name__).exception(...)`` calls
    somewhere to land.  WARNING on the HANDLER, deliberately: ``_build_ui`` sets the root LOGGER to
    DEBUG for the in-app log view, and Pillow emits a DEBUG record per image operation."""
    if _path is None:
        return
    try:
        import logging
        h = logging.FileHandler(str(_path), encoding="utf-8", delay=True)
        h.setLevel(logging.WARNING)
        h.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(name)s  %(message)s",
                                         datefmt="%H:%M:%S"))
        logging.getLogger().addHandler(h)
    except Exception:
        pass

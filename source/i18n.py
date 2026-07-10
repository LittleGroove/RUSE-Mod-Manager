"""Mod Manager UI internationalization.

Every user-facing string is wrapped in ``t("stable.key")`` — a STABLE KEY like ``mgr.deploy_done``,
never the English prose.  The key never changes when the English wording is edited, so a wording
tweak can no longer silently orphan every language's translation (the old English-as-key scheme did
exactly that).  Translations live in per-language files ``lang/<code>.json`` — ``{ key: text }`` —
which are far easier to diff and edit than one giant blob.

``t()`` is DUAL-MODE so the migration stays safe: it accepts either
  * a stable key  — the normal case at every rewritten call site, or
  * an English string — for the handful of *dynamic* sites ``t(some_variable)`` where the argument
    is a runtime value (field/kind/tab labels, card names).  Those Englishes are mapped back to their
    key via a reverse index built from ``us.json`` and translated all the same.

Fallback order for a KEY:  active language → English (``us.json``) → the argument itself.
``us.json`` is always loaded and is complete by construction, so the UI still renders in English if a
translation is missing or the active-language file is absent.  Only if the whole ``lang/`` directory
is gone do raw keys show — the same severity as a missing catalog before.  The files are UTF-8, so
Russian / Japanese / Chinese are fully supported.

The active language is chosen once at startup from ``settings["default_language"]`` (a language-change
restart also hands it to the new process via the ``RUSE_MM_LANG`` env var).  Changing it prompts a
restart, so built widgets never have to be live-swapped.
"""
import json
import os
import sys

# Language codes — must match ruse_mod_engine.dic.LANGUAGES (the in-game language set).
LANGS = ["us", "fr", "ger", "ita", "spa", "pol", "cz", "ru", "jpn", "sc", "dev"]

_strings: dict = {}     # {key: text} for the active language
_us: dict = {}          # {key: english} — always-complete fallback
_rev: dict = {}         # {english: key} — reverse index for dynamic t(variable) sites
_current: str = "us"


def data_dir() -> str:
    """``lang/`` directory: packaged beside the exe (PyInstaller ``_MEIPASS``) or the repo root."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "lang")


def _load_file(code: str) -> dict:
    try:
        with open(os.path.join(data_dir(), code + ".json"), encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _load_active() -> dict:
    """Catalog for the current language, with English fallback if its file is missing/empty."""
    if _current in ("us", "dev"):
        return _us
    return _load_file(_current) or _us


def load(current: str = "us") -> None:
    """Load the English fallback + active language and build the reverse index.  Safe with
    missing/broken files (``t()`` then falls back to English or the argument itself)."""
    global _us, _rev
    _us = _load_file("us")
    set_language(current)                   # sets _current and, now that _us is loaded, _strings too
    _rev = {eng: key for key, eng in _us.items()}


def set_language(code: str) -> None:
    """Set the active language.  If the catalog is already loaded (app running), switch the active
    strings LIVE so a subsequent ``t()`` renders in the new language — e.g. the language-change
    confirmation popup previews in the language being switched to."""
    global _current, _strings
    _current = code if code in LANGS else "us"
    if _us:                                 # catalog loaded → live-switch the active strings
        _strings = _load_active()


def current() -> str:
    return _current


def detect_os_language() -> str:
    """Best-effort: map the OS UI/display language to one of LANGS.  Falls back to 'us' (English) for
    anything we don't ship.  Used only on first launch; the choice then persists in settings."""
    # Windows primary-language IDs (low 10 bits of the LCID) → our codes.
    _WIN = {0x09: "us", 0x0c: "fr", 0x07: "ger", 0x10: "ita", 0x0a: "spa",
            0x15: "pol", 0x05: "cz", 0x19: "ru", 0x11: "jpn", 0x04: "sc"}
    # POSIX locale primary subtag → our codes.
    _POSIX = {"en": "us", "fr": "fr", "de": "ger", "it": "ita", "es": "spa",
              "pl": "pol", "cs": "cz", "ru": "ru", "ja": "jpn", "zh": "sc"}
    code = "us"
    try:
        if sys.platform == "win32":
            import ctypes
            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            code = _WIN.get(lcid & 0x3ff, "us")
        else:
            import locale
            primary = ((locale.getdefaultlocale()[0] or "en").split("_")[0].lower())
            code = _POSIX.get(primary, "us")
    except Exception:
        code = "us"
    return code if code in LANGS else "us"


def t(key, /, **fmt) -> str:
    """Translate ``key`` for the active language.  ``key`` is normally a stable key, but an English
    string is also accepted (dynamic sites) and mapped back via the reverse index.  ``fmt`` fills
    ``{placeholders}`` via :meth:`str.format`.  Fallback: active language → English → the argument."""
    val = _strings.get(key)
    if val is None:
        k2 = _rev.get(key)                 # caller passed English text (dynamic site)?
        if k2 is not None:
            val = _strings.get(k2) or _us.get(k2)
        if val is None:
            val = _us.get(key) or key      # key only in English, else render the argument itself
    if fmt:
        try:
            return val.format(**fmt)
        except Exception:
            return val
    return val

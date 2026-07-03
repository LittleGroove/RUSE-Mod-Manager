"""Mod Manager UI internationalization.

Every user-facing string in the app is wrapped in ``t("English text")`` — English IS the key.  At
runtime ``t()`` returns the active language's translation from ``lang.json``, falling back to the
file's English ('us') entry and finally to the key itself.  That last fallback is the safety net:
the UI always renders (in English) even when ``lang.json`` is missing or a string isn't translated
yet — so wrapping a string is never a breaking change.

``lang.json`` ships with every shipped language populated; any key still missing a translation
falls back to English (and finally the key itself), so wrapping a new string is never breaking —
but every user-facing string MUST still be wrapped so it CAN be translated.  The file is UTF-8, so
the non-Latin scripts in the language list (Russian, Japanese, Chinese) are fully supported.

The active language is chosen once at startup from ``settings["default_language"]`` (a
language-change restart also hands it to the new process via the ``RUSE_MM_LANG`` env var so it
applies even before the settings write is visible).  Changing it prompts a restart (see the
Settings tab), so built widgets never have to be live-swapped.
"""
import json
import os
import sys

# Language codes — must match ruse_mod_engine.dic.LANGUAGES (the in-game language set).
LANGS = ["us", "fr", "ger", "ita", "spa", "pol", "cz", "ru", "jpn", "sc", "dev"]

_strings: dict = {}     # {lang_code: {english_key: translated_text}}
_current: str = "us"


def data_path() -> str:
    """``lang.json`` location: packaged beside the exe (PyInstaller ``_MEIPASS``) or the repo root."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "lang.json")


def load(current: str = "us") -> None:
    """Load ``lang.json`` and set the active language.  Safe with a missing/broken file (``t()`` then
    just returns the English keys)."""
    global _strings
    set_language(current)
    try:
        with open(data_path(), encoding="utf-8") as f:
            _strings = json.load(f)
    except Exception:
        _strings = {}


def set_language(code: str) -> None:
    global _current
    _current = code if code in LANGS else "us"


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
    """Translate ``key`` (an English string) for the active language.  ``fmt`` fills ``{placeholders}``
    via :meth:`str.format`.  Fallback order: active language → file English ('us') → the key itself."""
    cat = _strings.get(_current) or {}
    val = cat.get(key)
    if not val:
        val = (_strings.get("us") or {}).get(key) or key
    if fmt:
        try:
            return val.format(**fmt)
        except Exception:
            return val
    return val

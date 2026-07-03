"""Locate bundled engine data files whether running from source or a PyInstaller exe.

Data under ``ruse_mod_engine/data/`` (the build-id registry, cross-version migration maps, the DSL
and placement catalogs) is collected INTO the exe at ``<_MEIPASS>/ruse_mod_engine/data/``.
``__file__``-relative paths are unreliable when frozen (onefile), so — exactly like ``i18n.data_path``
does for ``lang.json`` — prefer ``sys._MEIPASS`` when frozen and fall back to the source tree.
"""
import sys
from pathlib import Path


def data_dir() -> Path:
    """Absolute path to ``ruse_mod_engine/data/`` (the bundled copy when frozen, the source dir else)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass and getattr(sys, "frozen", False):
        return Path(meipass) / "ruse_mod_engine" / "data"
    return Path(__file__).resolve().parent / "data"

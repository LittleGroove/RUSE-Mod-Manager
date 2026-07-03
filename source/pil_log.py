"""Attribute Pillow's chatty DEBUG logging to whichever editor triggered it.

Both the Raw / Asset editor and the Map editor decode images through Pillow, whose ``PIL``
logger emits a flood of plugin-import (``Importing PngImagePlugin``) and PNG-chunk
(``STREAM b'IHDR' ...``) DEBUG lines on every ``Image.open``.  We deliberately do *not*
suppress those — they're occasionally useful — but on their own they give no hint which window
caused them.  ``install()`` adds a filter to the ``PIL`` logger that prefixes each line with the
editor currently doing image work, so the Mod Manager log makes the source obvious, e.g.::

    DEBUG  [Raw editor] STREAM b'IHDR' 16 13
    DEBUG  [Map editor] Importing PngImagePlugin

Editors mark their work with the ``source()`` context manager (see ``_tgv_image`` / ``_pil_open``
in the Raw editor and ``load_minimap`` / ``_get_icon`` in the Map editor).
"""
import logging
from contextlib import contextmanager

# Single active editor at a time (the editors are mutually-exclusive nested views), so a plain
# module global is enough — set, decode, and reset all happen on the same thread inline.
_source = None


def set_source(label):
    """Mark the editor currently decoding images (e.g. ``"Raw editor"``).  Returns the previous
    label so callers can restore it."""
    global _source
    prev = _source
    _source = label
    return prev


@contextmanager
def source(label):
    """Scope a block of image work to ``label`` for PIL-log attribution."""
    prev = set_source(label)
    try:
        yield
    finally:
        set_source(prev)


class _SourceFilter(logging.Filter):
    """Prefix Pillow's records with the active editor.  Must be attached to the *handler*, not a
    logger: Pillow logs through child loggers (``PIL.PngImagePlugin`` etc.) and a logger's filters
    don't run for records that merely propagate up to it — handler filters do."""
    def filter(self, record):
        if (_source and not getattr(record, "_pil_tagged", False)
                and (record.name == "PIL" or record.name.startswith("PIL."))):
            record.msg = "[%s] %s" % (_source, record.getMessage())
            record.args = ()
            record._pil_tagged = True
        return True


def install(handler):
    """Attach the attribution filter to ``handler`` (the Mod Manager's log handler), once."""
    if not any(isinstance(f, _SourceFilter) for f in handler.filters):
        handler.addFilter(_SourceFilter())

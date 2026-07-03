"""`.xyz` mission-script codec — read, decompile, edit, and RE-EMIT RUSE scripts, in-project.

Container (cracked): b'XYZ0\\n'(5) + b'\\x0d\\xf2\\xb3\\x00'(4 version) + size(3, big-endian) + hash(16) +
zlib(stream) -> a **CPython 2.5.1** marshalled code object (2.5.1 is the Python the GAME embeds; marshal
magic 62131 = 0xf2b3). The 16-byte hash is NOT validated by the engine, so a zeroed hash loads fine.

CRITICAL: the game VM is Python 2.5.1, so a re-emitted script must carry 2.5.1 BYTECODE. The marshal
FORMAT is cross-compatible 2.5–2.7 (xdis reads it either way), but the OPCODES are NOT — compiling
source with any other Python (e.g. 2.7) emits opcodes the 2.5.1 VM misexecutes, which breaks scripts
silently in-game. So every from-source compile MUST use the bundled 2.5.1 interpreter.

Two write paths:
  * `dump_code` / `repack` — re-marshal an EXISTING code object via xdis.marsh (in .venv). Safe: it
    preserves the original 2.5.1 opcodes byte-for-byte; no interpreter needed.
  * `compile_source` — compile brand-new SOURCE TEXT. Python 3 cannot emit 2.5.1 bytecode, so this
    delegates to ruse_mod_engine/script_logic.py, which runs the bundled Python-2.5.1 worker
    (ruse_mod_engine/python251/). `have_native_compiler()` reports whether that interpreter is present.

Read path (decompile) uses xdis.unmarshal + uncompyle6 (both in .venv).
"""
import os  # noqa: F401 (kept for callers/back-compat)
import zlib
import logging

log = logging.getLogger(__name__)

MAGIC = b"XYZ0\n"
VER = b"\x0d\xf2\xb3\x00"
PY_MAGIC = 62131                 # CPython 2.5 marshal magic (0xf2b3) — the version the game embeds


# ── container ────────────────────────────────────────────────────────────────────
def unpack(xyz_bytes):
    """XYZ0 bytes -> {'hash': 16 bytes, 'size': int, 'marshal': bytes}. Tolerates a raw zlib stream
    (no XYZ0 frame) by scanning for the zlib header, for robustness against odd containers."""
    b = xyz_bytes
    if b[:5] == MAGIC:
        size = int.from_bytes(b[9:12], "big")
        marshal_bytes = zlib.decompress(b[28:])
        # The 3-byte size header is advisory — the engine reads the zlib stream, not this field.
        # Some tool-authored .xyz (e.g. ProLution operation effetmaps) ship a WRONG size yet load
        # fine in-game, so trust the actual decompressed bytes rather than reject the file. (Our
        # own pack() always writes the correct size, so a round-trip repairs the header.)
        if len(marshal_bytes) != size:
            log.warning("xyz size header %d != decompressed %d — using actual length", size, len(marshal_bytes))
            size = len(marshal_bytes)
        return {"hash": b[12:28], "size": size, "marshal": marshal_bytes}
    for m in (b"\x78\x9c", b"\x78\xda", b"\x78\x01"):
        i = b.find(m)
        if 0 <= i < 64:
            dec = zlib.decompress(b[i:])
            return {"hash": b"\x00" * 16, "size": len(dec), "marshal": dec}
    raise ValueError("not an XYZ0 container and no zlib stream found")


def pack(marshal_bytes, hash16=b"\x00" * 16, level=9):
    """Wrap a 2.7 marshal blob in the XYZ0 container (hash not validated by the engine)."""
    if len(hash16) != 16:
        raise ValueError("hash16 must be exactly 16 bytes")
    comp = zlib.compress(marshal_bytes, level)
    return MAGIC + VER + len(marshal_bytes).to_bytes(3, "big") + hash16 + comp


# ── code object <-> marshal (xdis; no external interpreter) ───────────────────────
def load_code(marshal_bytes):
    """Unmarshal a 2.7 code object from raw marshal bytes (xdis)."""
    from xdis import unmarshal
    return unmarshal.load_code(marshal_bytes, PY_MAGIC)


def dump_code(code):
    """Marshal a (2.7) code object back to bytes via xdis.marsh — the in-project WRITE path."""
    from xdis import marsh
    return marsh.dumps(code, PY_MAGIC)


def _strip_module_return(source):
    """Drop uncompyle6's module-level RETURN_VALUE artifact — an indent-0 `return`/`return None` that
    every module code object ends with. It is a SyntaxError at module scope ("'return' outside
    function"), so removing it makes the decompiled source recompile as-is. An indent-0 `return` is
    ALWAYS this artifact (a real return can't sit at module level), so stripping it is safe.
    (script_logic.decompile_xyz does the same for its own path.)"""
    return "\n".join(ln for ln in source.split("\n") if ln.rstrip() not in ("return", "return None"))


def decompile(marshal_bytes):
    """2.5.1 marshal -> recompilable Python source (xdis + uncompyle6; module-return artifact stripped)."""
    import io
    from uncompyle6.main import decompile as _decompile
    code = load_code(marshal_bytes)
    buf = io.StringIO()
    # 2.5.1 marshal (magic 62131); uncompyle6 decodes it with the (2,5) opcode table — verified across
    # the whole script corpus (identical source output to the old 2.6/2.7 read hint).
    _decompile(code, bytecode_version=(2, 5), out=buf, magic_int=PY_MAGIC)
    return _strip_module_return(buf.getvalue())


def decompile_xyz(xyz_bytes):
    return decompile(unpack(xyz_bytes)["marshal"])


def repack(xyz_bytes, hash16=None):
    """Decode a code object and RE-EMIT the XYZ0 (unpack -> load -> dump -> pack). Normalises a script
    through the in-project codec; the basis for code-object edits (load_code -> mutate -> dump_code)."""
    u = unpack(xyz_bytes)
    h = hash16 if hash16 is not None else u["hash"]
    return pack(dump_code(load_code(u["marshal"])), h)


# ── from-scratch source compilation (delegates to the bundled Python-2.5.1 path) ──
# script_logic.py owns the hardened 2.5.1 interpreter invocation (ruse_compile251.exe, -E -S, clean
# env). These wrappers keep xyz_compile's API stable while routing EVERY from-source compile through
# 2.5.1 — the only Python whose bytecode the 2.5.1 game VM executes correctly. Lazy import avoids the
# script_logic -> xyz_compile import cycle.
def have_native_compiler():
    """True when the bundled Python 2.5.1 interpreter is present (source compile works)."""
    from . import script_logic
    return script_logic.have_compiler()


def compile_source(source_text, timeout=30):
    """Compile SOURCE TEXT to game-loadable 2.5.1 marshal bytes via the bundled 2.5.1 interpreter.
    Raises FileNotFoundError if it isn't installed, RuntimeError(stderr) on a compile error."""
    from . import script_logic
    return script_logic.compile_source(source_text, timeout=timeout)


def compile_to_xyz(source_text, hash16=b"\x00" * 16):
    """Source text -> ready-to-write XYZ0 bytes (2.5.1 bytecode; needs the bundled interpreter)."""
    return pack(compile_source(source_text), hash16)

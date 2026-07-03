"""Mission-script (.xyz) editing API for the scenario-authoring backend.

Wraps marshal2 + xyz_compile so the editor edits *named values* (money, objective text/LocHash, positions,
camp config, victory thresholds) instead of marshal bytes. A Script loads to a marshal2 Node tree, exposes
its editable constants (with stable paths + decompiled context), lets you set them, and re-emits a
GAME-LOADABLE `.xyz` (byte-identical except your edits — proven in-game).

  s = Script.load(xyz_bytes)
  for c in s.constants(): ...        # editable consts: path, kind, value, function-context
  s.set(path, new_value)             # edit by stable path
  new_xyz = s.dump()                 # game-loadable .xyz
  s.decompile()                      # human-readable source (the editing guide)

Constant kinds returned/accepted:
  'int'   -> Python int          (TYPE_INT 'i')
  'long'  -> Python int          (TYPE_LONG 'l' — also how 64-bit LocHash keys are stored)
  'int64' -> Python int          (TYPE_INT64 'I')
  'float' -> Python float        (TYPE_BINARY_FLOAT 'g')
  'str'   -> bytes               (TYPE_STRING 's')   — interned 't'/'R' are identifiers, not edited here
  'unicode'-> bytes (utf-8)      (TYPE_UNICODE 'u')
"""
from __future__ import annotations
import zlib, struct
from dataclasses import dataclass
from typing import Any, List, Tuple

from . import marshal2, xyz_compile

Path = Tuple

_KIND = {"i": "int", "l": "long", "I": "int64", "g": "float", "s": "str", "u": "unicode"}


@dataclass
class Const:
    path: Path          # stable address into the tree (use with Script.get/set)
    kind: str           # int|long|int64|float|str|unicode
    value: Any
    func: str           # decompiled name of the enclosing code object (context)


def _long_to_int(sign_n, digits):
    n = 0
    for d in reversed(digits):
        n = (n << 15) | d
    return -n if sign_n < 0 else n


def _int_to_long(v):
    sign = -1 if v < 0 else 1
    v = abs(v)
    digits = []
    if v == 0:
        return (0, [])
    while v:
        digits.append(v & 0x7FFF); v >>= 15
    return (sign * len(digits), digits)


def _node_value(n):
    if n.tc == "i": return n.val
    if n.tc == "I": return n.val
    if n.tc == "l": return _long_to_int(*n.val)
    if n.tc == "g": return struct.unpack("<d", n.val)[0]
    if n.tc in ("s", "u", "t"): return bytes(n.val)
    return None


def _set_node_value(n, value):
    if n.tc == "i": n.val = int(value)
    elif n.tc == "I": n.val = int(value)
    elif n.tc == "l": n.val = _int_to_long(int(value))
    elif n.tc == "g": n.val = struct.pack("<d", float(value))
    elif n.tc in ("s", "u", "t"):
        n.val = value if isinstance(value, (bytes, bytearray)) else str(value).encode("utf-8")
    else:
        raise ValueError("cannot set node of type %r" % n.tc)


class Script:
    def __init__(self, tree, header: bytes):
        self.tree = tree            # marshal2 Node (root code object)
        self._header = header       # original 28-byte XYZ0 header (magic+ver+size+hash) — reused on dump

    # ---- io ----
    @classmethod
    def load(cls, xyz: bytes) -> "Script":
        if xyz[:5] == b"XYZ0\n":
            header = bytes(xyz[:28])
            marshal = zlib.decompressobj().decompress(xyz[28:])
        else:                                   # raw zlib (rare)
            header = b""
            marshal = zlib.decompressobj().decompress(xyz[xyz.find(b"\x78\x9c"):])
        s = cls(marshal2.load(marshal), header)
        s._orig = (bytes(xyz), marshal)         # for true-no-op passthrough
        return s

    def dump(self) -> bytes:
        """Re-emit a game-loadable .xyz. The engine does NOT validate the .xyz hash and tolerates the stale
        size field, so we reuse the original XYZ0 header and only the decompressed marshal must be right
        (re-zlib is fine — proven in-game). A true no-op returns the original bytes verbatim."""
        marshal = marshal2.dump(self.tree)
        orig = getattr(self, "_orig", None)
        if orig is not None and marshal == orig[1]:
            return orig[0]                       # unchanged -> byte-identical passthrough
        if self._header:
            return self._header + zlib.compress(marshal, 9)
        return xyz_compile.pack(marshal)

    def decompile(self) -> str:
        return xyz_compile.decompile(marshal2.dump(self.tree))

    # ---- navigation ----
    def get(self, path: Path):
        return _node_value(self._resolve(path))

    def set(self, path: Path, value):
        _set_node_value(self._resolve(path), value)

    def _resolve(self, path: Path):
        node = self.tree
        for step in path:
            node = node.val[step]
        return node

    # ---- editable surface ----
    def constants(self) -> List[Const]:
        """Every editable literal in the script, grouped by enclosing function, with a stable path."""
        out: List[Const] = []
        self._walk_code(self.tree, (), out)
        return out

    def _walk_code(self, code_node, path, out):
        name = bytes(code_node.val["name"].val).decode("utf-8", "replace")
        consts = code_node.val["consts"]                 # '(' tuple node
        for i, c in enumerate(consts.val):
            cpath = path + ("consts", i)
            if c.tc == "c":
                self._walk_code(c, cpath, out)
            elif c.tc in _KIND:
                out.append(Const(cpath, _KIND[c.tc], _node_value(c), name))
            # nested tuples/lists of literals (e.g. position vectors) are reachable too:
            elif c.tc in "([":
                self._walk_container(c, cpath, name, out)

    def _walk_container(self, node, path, func, out):
        for i, c in enumerate(node.val):
            cpath = path + (i,)
            if c.tc in _KIND:
                out.append(Const(cpath, _KIND[c.tc], _node_value(c), func))
            elif c.tc in "([":
                self._walk_container(c, cpath, func, out)
            elif c.tc == "c":
                self._walk_code(c, cpath, out)

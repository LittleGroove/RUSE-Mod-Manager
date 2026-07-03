"""Python-2.x marshal codec (load/dump) for RUSE `.xyz` scripts.

xdis/Python-3 marshal serialize with the Py3.4+ reference system (FLAG_REF, 0x80 bit) which RUSE's
custom Python 2.x cannot read — that's why re-marshalled scripts are rejected. This module loads/dumps
in TRUE 2.x format (TYPE_INTERNED 't' / TYPE_STRINGREF 'R' interning, no FLAG_REF), so a round-tripped
script is BYTE-IDENTICAL to the game's original and therefore game-loadable.

  tree = load(marshal_bytes)   # Node tree; preserves every type code
  marshal_bytes = dump(tree)   # exact 2.x bytes
Edit a value by walking the tree and setting node.val (e.g. an 'i' node's int); type codes are preserved.

Node.tc = marshal type char; Node.val = payload (bytes for strings, int for ints, list[Node] for
containers / code fields).  Interned strings are tc 't' (referenced later as the same Node).
"""
import struct


class Node:
    __slots__ = ("tc", "val")
    def __init__(self, tc, val):
        self.tc = tc        # one of: c ( [ { s t i I l N T F g f u ...
        self.val = val
    def __repr__(self):
        v = self.val
        if isinstance(v, (bytes, bytearray)):
            v = bytes(v[:24])
        return "Node(%r, %r)" % (self.tc, v)


CODE_FIELDS = ("argcount", "nlocals", "stacksize", "flags",   # 4 longs
               "code", "consts", "names", "varnames", "freevars", "cellvars",
               "filename", "name", "firstlineno", "lnotab")


class _R:
    def __init__(self, data):
        self.d = data; self.p = 0; self.refs = []
    def u8(self):
        b = self.d[self.p]; self.p += 1; return b
    def take(self, n):
        s = self.d[self.p:self.p + n]; self.p += n; return s
    def i32(self):
        v = struct.unpack_from("<i", self.d, self.p)[0]; self.p += 4; return v


def load(data):
    return _load(_R(data))


def _load(r):
    tc = chr(r.u8())
    if tc == "N": return Node("N", None)
    if tc == "T": return Node("T", None)
    if tc == "F": return Node("F", None)
    if tc == "0": return Node("0", None)         # NULL
    if tc == ".": return Node(".", None)         # Ellipsis
    if tc == "S": return Node("S", None)         # StopIteration
    if tc == "i":                                # TYPE_INT (32-bit)
        return Node("i", r.i32())
    if tc == "I":                                # TYPE_INT64
        v = struct.unpack_from("<q", r.d, r.p)[0]; r.p += 8; return Node("I", v)
    if tc == "l":                                # TYPE_LONG (arbitrary precision)
        n = r.i32(); digits = [r.i32_u16() if False else None]  # placeholder
        # py2 long: n = number of 15-bit digits (signed by sign of n)
        ndig = abs(n)
        ds = [struct.unpack_from("<H", r.d, r.p + 2 * i)[0] for i in range(ndig)]
        r.p += 2 * ndig
        return Node("l", (n, ds))
    if tc == "g":                                # TYPE_BINARY_FLOAT (8 bytes)
        v = r.take(8); return Node("g", v)
    if tc == "f":                                # TYPE_FLOAT (pstring)
        ln = r.u8(); return Node("f", r.take(ln))
    if tc == "s":                                # TYPE_STRING
        ln = r.i32(); return Node("s", r.take(ln))
    if tc == "t":                                # TYPE_INTERNED  (add to ref table)
        ln = r.i32(); node = Node("t", r.take(ln)); r.refs.append(node); return node
    if tc == "R":                                # TYPE_STRINGREF
        return r.refs[r.i32()]
    if tc == "u":                                # TYPE_UNICODE (utf8 bytes)
        ln = r.i32(); return Node("u", r.take(ln))
    if tc in "([":                               # tuple / list
        n = r.i32(); return Node(tc, [_load(r) for _ in range(n)])
    if tc == "{":                                # dict: key,val pairs until TYPE_NULL
        items = []
        while True:
            k = _load(r)
            if k.tc == "0": break
            items.append((k, _load(r)))
        return Node("{", items)
    if tc in "<>":                               # set / frozenset
        n = r.i32(); return Node(tc, [_load(r) for _ in range(n)])
    if tc == "c":                                # TYPE_CODE
        fields = {}
        fields["argcount"] = r.i32(); fields["nlocals"] = r.i32()
        fields["stacksize"] = r.i32(); fields["flags"] = r.i32()
        for k in CODE_FIELDS[4:]:
            if k == "firstlineno":
                fields[k] = r.i32()
            else:
                fields[k] = _load(r)
        return Node("c", fields)
    raise ValueError("marshal2: unknown type %r at %d" % (tc, r.p - 1))


class _W:
    def __init__(self):
        self.out = bytearray(); self.refs = {}   # id(node)->index
    def u8(self, b): self.out.append(b)
    def i32(self, v): self.out += struct.pack("<i", v)
    def raw(self, b): self.out += b


def dump(node):
    w = _W(); _dump(w, node); return bytes(w.out)


def _dump(w, n):
    tc = n.tc
    if tc in "NTF0.S":
        w.u8(ord(tc)); return
    if tc == "i":
        w.u8(ord("i")); w.i32(n.val); return
    if tc == "I":
        w.u8(ord("I")); w.raw(struct.pack("<q", n.val)); return
    if tc == "l":
        sign_n, ds = n.val
        w.u8(ord("l")); w.i32(sign_n)
        for dgt in ds: w.raw(struct.pack("<H", dgt))
        return
    if tc == "g":
        w.u8(ord("g")); w.raw(n.val); return
    if tc == "f":
        w.u8(ord("f")); w.u8(len(n.val)); w.raw(n.val); return
    if tc == "s":
        w.u8(ord("s")); w.i32(len(n.val)); w.raw(n.val); return
    if tc == "u":
        w.u8(ord("u")); w.i32(len(n.val)); w.raw(n.val); return
    if tc == "t":                                # interned: 't' first time, 'R' after
        idx = w.refs.get(id(n))
        if idx is None:
            w.refs[id(n)] = len(w.refs)
            w.u8(ord("t")); w.i32(len(n.val)); w.raw(n.val)
        else:
            w.u8(ord("R")); w.i32(idx)
        return
    if tc in "([<>":
        w.u8(ord(tc)); w.i32(len(n.val))
        for c in n.val: _dump(w, c)
        return
    if tc == "{":
        w.u8(ord("{"))
        for k, v in n.val:
            _dump(w, k); _dump(w, v)
        w.u8(ord("0"))                            # TYPE_NULL terminator
        return
    if tc == "c":
        f = n.val
        w.u8(ord("c"))
        w.i32(f["argcount"]); w.i32(f["nlocals"]); w.i32(f["stacksize"]); w.i32(f["flags"])
        for k in CODE_FIELDS[4:]:
            if k == "firstlineno":
                w.i32(f[k])
            else:
                _dump(w, f[k])
        return
    raise ValueError("marshal2: cannot dump type %r" % tc)

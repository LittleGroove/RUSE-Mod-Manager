"""
NDF binary reader/writer for RUSE .dat game files.

The NDF binary (ndfbin / gladndfbin) format is Eugen Systems' compiled game-data
format used in RUSE, Wargame, and Steel Division.  A .dat archive contains one or
more .ndfbin files; each .ndfbin holds typed instances of NDF classes (units,
weapons, buildings, AI profiles, etc.).

Layout of an ndfbin file
------------------------
  [40-byte header]      EUG0 magic, CNDF magic, compression flag, TOC0 offset
  [body]                OBJE section — serialised instances (may be zlib-compressed)
  [TOC0 footer]         9 section descriptors: OBJE TOPO CHNK CLAS PROP STRG TRAN IMPR EXPR

Each instance has:
  - a class index (into the CLAS table)
  - a list of property-value pairs; each pair is:
      - property index (into the PROP table)
      - type id + encoded value
  - terminated by the sentinel 0xABABABAB

This module exposes:
  NdfBinary          — top-level parsed file object
  read(data)         — parse bytes → NdfBinary
  write(ndf)         — NdfBinary → bytes
  find_instances()   — helper to locate instances by class name + property match
  set_property()     — set a typed property value on an instance
"""

import base64
import struct
import zlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ── Constants ────────────────────────────────────────────────────────────────

MAGIC_EUG0 = b"EUG0"
MAGIC_CNDF = b"CNDF"
MAGIC_TOC0 = b"TOC0"

PROPERTY_SENTINEL = 0xABABABAB
TRANS_REF_MARKER  = 0xAAAAAAAA
OBJ_REF_MARKER    = 0xBBBBBBBB

COMPRESSED_FLAG   = 0x80
TABLE_NAMES       = ["OBJE", "TOPO", "CHNK", "CLAS", "PROP", "STRG", "TRAN", "IMPR", "EXPR"]


class T:
    """NDF type id constants."""
    Bool       = 0x00
    Int8       = 0x01
    Int32      = 0x02
    UInt32     = 0x03
    Time64     = 0x04   # uint64 seconds since Unix epoch
    Float32    = 0x05
    Float64    = 0x06
    StringRef  = 0x07   # index into STRG string table
    WideStr    = 0x08   # Pascal-style UTF-16 string (length-prefixed)
    Reference  = 0x09   # object ref (0xBBBBBBBB) or trans ref (0xAAAAAAAA)
    Vector3    = 0x0B   # 3 × float32
    Color128   = 0x0C   # 4 × float32 ARGB
    Color32    = 0x0D   # 4 × uint8  ARGB
    TripleInt  = 0x0E   # 3 × int32
    Matrix     = 0x0F   # 16 × float32
    List       = 0x11   # uint32 count, then count × NdfValue
    Map        = 0x12   # uint32 count, then count × (NdfValue key, NdfValue value)
    Long       = 0x13   # int64
    Blob       = 0x14   # uint32 size + raw bytes
    Int16      = 0x18   # int16
    UInt16     = 0x19   # uint16
    Guid       = 0x1A   # 16 bytes
    PathRef    = 0x1C   # index into STRG (filename strings)
    LocHash    = 0x1D   # 8 bytes localisation hash
    ZipBlob    = 0x1E   # compressed blob
    Int2       = 0x1F   # 2 × int32
    Float2     = 0x21   # 2 × float32
    Pair       = 0x22   # NdfValue key + NdfValue value (map-list entry marker)
    Hash       = 0x25   # 16 bytes (MD5)

    # Fixed byte-sizes for the simple scalar types (variable-length types return 0)
    _FIXED_SIZES = {
        Bool: 1, Int8: 1, Int16: 2, UInt16: 2,
        Int32: 4, UInt32: 4, Float32: 4,
        Time64: 8, Float64: 8, Long: 8, LocHash: 8,
        Vector3: 12, TripleInt: 12, Color128: 16,
        Color32: 4, Matrix: 64, Guid: 16, Hash: 16,
        Int2: 8, Float2: 8,
    }

    @classmethod
    def fixed_size(cls, type_id: int) -> int:
        return cls._FIXED_SIZES.get(type_id, 0)

    @classmethod
    def name(cls, type_id: int) -> str:
        names = {v: k for k, v in cls.__dict__.items() if isinstance(v, int) and not k.startswith('_')}
        return names.get(type_id, f"0x{type_id:02X}")


# ── Value dataclasses ────────────────────────────────────────────────────────

@dataclass
class NdfValue:
    """A typed NDF value.  `raw` holds the Python-native representation."""
    type_id: int
    raw: Any      # see decoding table in _decode_value()

    def __repr__(self):
        return f"NdfValue({T.name(self.type_id)}, {self.raw!r})"


@dataclass
class NdfPropertyValue:
    prop_index: int
    value: NdfValue


@dataclass
class NdfInstance:
    class_index: int
    props: List[NdfPropertyValue] = field(default_factory=list)

    def get(self, prop_index: int) -> Optional[NdfValue]:
        for pv in self.props:
            if pv.prop_index == prop_index:
                return pv.value
        return None

    def set(self, prop_index: int, value: NdfValue):
        for pv in self.props:
            if pv.prop_index == prop_index:
                pv.value = value
                return
        self.props.append(NdfPropertyValue(prop_index, value))

    def remove(self, prop_index: int) -> bool:
        """Remove the property with the given index.  Returns True if it existed."""
        for i, pv in enumerate(self.props):
            if pv.prop_index == prop_index:
                del self.props[i]
                return True
        return False


@dataclass
class NdfClass:
    index: int
    name: str


@dataclass
class NdfProperty:
    index: int
    name: str
    class_index: int   # which class owns this property


@dataclass
class NdfBinary:
    classes:    List[NdfClass]    = field(default_factory=list)
    properties: List[NdfProperty] = field(default_factory=list)
    strings:    List[str]         = field(default_factory=list)
    trans:      List[str]         = field(default_factory=list)
    instances:  List[NdfInstance] = field(default_factory=list)
    top_objects: List[int]        = field(default_factory=list)
    import_list: List[int]        = field(default_factory=list)
    export_list: List[int]        = field(default_factory=list)
    is_compressed: bool           = False

    # ── Lookup helpers ───────────────────────────────────────────────────────
    # These name lookups sit in HOT loops (stable-key matching, ObjRef resolution).  Done as linear scans
    # of the class/property tables they were O(instances × properties) on big NDFs — e.g. everything.cpp
    # (63,686 instances × 3,338 properties) spent ~106s JUST computing stable keys, which is the bulk of a
    # convert.  They're memoized below into name→object dicts.  The class/property tables are append-only
    # during a diff/apply (a new class/prop is only ever ADDED, never removed or reordered), so a cheap
    # length check tells us when to rebuild.  Same results (exact match preferred, case-insensitive
    # fallback for Eugen's cross-build re-casing e.g. 'GUID'->'Guid'), ~1000x faster.

    def _ensure_name_indices(self):
        if getattr(self, "_idx_nprops", -1) != len(self.properties):
            by_pc: dict = {}   # (class_index, name) and (class_index, name.lower()) -> NdfProperty
            by_p: dict = {}    # name and name.lower() -> NdfProperty (first wins)
            by_i: dict = {}    # prop_index -> NdfProperty (props are keyed by .index, not list position)
            for p in self.properties:
                by_pc.setdefault((p.class_index, p.name), p)
                by_pc.setdefault((p.class_index, p.name.lower()), p)
                by_p.setdefault(p.name, p)
                by_p.setdefault(p.name.lower(), p)
                by_i.setdefault(p.index, p)
            self._idx_prop_by_class, self._idx_prop_by_name = by_pc, by_p
            self._idx_prop_by_index, self._idx_nprops = by_i, len(self.properties)
        if getattr(self, "_idx_nclasses", -1) != len(self.classes):
            by_c: dict = {}
            for c in self.classes:
                by_c.setdefault(c.name, c)
                by_c.setdefault(c.name.lower(), c)
            self._idx_class_by_name, self._idx_nclasses = by_c, len(self.classes)

    def class_by_name(self, name: str) -> Optional[NdfClass]:
        self._ensure_name_indices()
        c = self._idx_class_by_name.get(name)
        return c if c is not None else self._idx_class_by_name.get(name.lower())

    def prop_by_name(self, name: str) -> Optional[NdfProperty]:
        self._ensure_name_indices()
        p = self._idx_prop_by_name.get(name)
        return p if p is not None else self._idx_prop_by_name.get(name.lower())

    def prop_by_name_and_class(self, prop_name: str, class_index: int) -> Optional[NdfProperty]:
        self._ensure_name_indices()
        p = self._idx_prop_by_class.get((class_index, prop_name))
        return p if p is not None else self._idx_prop_by_class.get((class_index, prop_name.lower()))

    def class_index_by_name(self, name: str) -> Optional[int]:
        """Index of the class named `name` in THIS NDF's class table, or None. Memoized (length-staleness).
        Used to resolve an ObjRef's declared class BY NAME so a build-specific raw class index migrates
        correctly (class tables reorder across game updates)."""
        if getattr(self, "_clsidx_ninst", -1) != len(self.classes):
            self._clsidx = {c.name: i for i, c in enumerate(self.classes)}
            self._clsidx_ninst = len(self.classes)
        return self._clsidx.get(name)

    def prop_by_index(self, prop_index: int) -> Optional[NdfProperty]:
        """NdfProperty whose .index == prop_index (props aren't stored in index order).
        Memoized — replaces a linear scan that ran once per prop-value in the diff."""
        self._ensure_name_indices()
        return self._idx_prop_by_index.get(prop_index)

    def class_instances(self, class_name_or_index) -> List[NdfInstance]:
        if isinstance(class_name_or_index, str):
            cls = self.class_by_name(class_name_or_index)
            if cls is None:
                return []
            idx = cls.index
        else:
            idx = class_name_or_index
        return [inst for inst in self.instances if inst.class_index == idx]

    def get_string(self, index: int) -> str:
        if 0 <= index < len(self.strings):
            return self.strings[index]
        return f"<str#{index}>"

    def get_trans(self, index: int) -> str:
        if 0 <= index < len(self.trans):
            return self.trans[index]
        return f"<trans#{index}>"

    def find_instance_by_key(self, key_prop: str, key_val: str) -> Optional[Tuple[int, "NdfInstance"]]:
        """Return (index, instance) for the first instance whose key_prop resolves to key_val."""
        for idx, inst in enumerate(self.instances):
            p = self.prop_by_name_and_class(key_prop, inst.class_index)
            if p is None:
                continue
            v = inst.get(p.index)
            if v is None:
                continue
            resolved = (self.get_string(v.raw) if v.type_id in (T.StringRef, T.PathRef)
                        else str(v.raw))
            if resolved == key_val:
                return (idx, inst)
        return None

    def _obj_stable_key(self, obj_idx: int) -> Optional[str]:
        """Return the stable key string for the instance at obj_idx, or None."""
        if not (0 <= obj_idx < len(self.instances)):
            return None
        target = self.instances[obj_idx]
        for kp in ("ClassNameForDebug", "DescriptorId"):
            p = self.prop_by_name_and_class(kp, target.class_index)
            if p is None:
                continue
            v = target.get(p.index)
            if v is None:
                continue
            return (self.get_string(v.raw) if v.type_id in (T.StringRef, T.PathRef)
                    else str(v.raw))
        return None

    def resolve_value(self, val: NdfValue) -> Any:
        """Return a human-readable Python representation of val."""
        if val.type_id in (T.StringRef, T.PathRef):
            return self.get_string(val.raw)
        if val.type_id == T.WideStr:
            return val.raw  # already a str
        if val.type_id == T.List:
            return [self.resolve_value(item) for item in val.raw]
        if val.type_id == T.Reference:
            marker, ref = val.raw
            if marker == TRANS_REF_MARKER:
                # `ref` is an IMPORT ORDINAL — resolve to its full path so two NDFs that
                # reference the same import compare equal even if their ordinals differ.
                path = self.import_ordinal_paths().get(ref)
                return f"$trans:{path if path is not None else ref}"
            if marker == OBJ_REF_MARKER:
                obj_idx, cls_idx = ref
                key = self._obj_stable_key(obj_idx)
                if key is not None:
                    return f"$obj:{key}"
                cls = self.classes[cls_idx] if cls_idx < len(self.classes) else None
                cls_name = cls.name if cls else f"cls#{cls_idx}"
                return f"$obj:{cls_name}[{obj_idx}]"
        return val.raw

    def find_instances(
        self,
        class_name: str,
        match: Optional[Dict[str, str]] = None,
    ) -> List[Tuple[int, NdfInstance]]:
        """
        Return [(instance_index, instance), ...] for all instances of class_name
        that satisfy every condition in match ({property_name: expected_str_value}).

        Special match key "_index": match the instance at exactly that position in
        the instance list (integer or string).  When present, no other conditions
        are evaluated and the result is at most one entry.
        """
        cls = self.class_by_name(class_name)
        if cls is None:
            return []

        # Fast-path: positional match by "_index"
        if match and "_index" in match:
            idx = int(match["_index"])
            if 0 <= idx < len(self.instances):
                inst = self.instances[idx]
                if inst.class_index == cls.index:
                    return [(idx, inst)]
            return []

        # Fast-path: durable ANCHOR match — {"anchor": {"root":[keyprop,keyval], "steps":[[label,...],...]}}
        # (H3: addresses a keyless sub-object via a keyed ancestor + path; survives index shifts).
        if match and "anchor" in match:
            idx = self.resolve_anchor(match["anchor"])
            if idx is not None and 0 <= idx < len(self.instances):
                inst = self.instances[idx]
                if inst.class_index == cls.index:
                    return [(idx, inst)]
            return []

        results = []
        for idx, inst in enumerate(self.instances):
            if inst.class_index != cls.index:
                continue
            if match:
                ok = True
                for prop_name, expected in match.items():
                    prop = self.prop_by_name_and_class(prop_name, cls.index)
                    if prop is None:
                        prop = self.prop_by_name(prop_name)
                    if prop is None:
                        ok = False
                        break
                    val = inst.get(prop.index)
                    if val is None:
                        ok = False
                        break
                    resolved = str(self.resolve_value(val))
                    if resolved != expected:
                        ok = False
                        break
                if not ok:
                    continue
            results.append((idx, inst))

        return results

    # ── Anchor addressing (H3): durable addresses for KEYLESS sub-objects ─────────────────────────────
    # A keyless instance (ammo, turret, camera key, ...) has no stable key of its own, so a positional
    # _index breaks across game updates.  Instead address it as its nearest KEYED ancestor + the property/
    # list PATH to reach it ("the Ammunition of weapon X").  build_anchor_index() walks the ObjRef graph
    # (BFS from keyed roots) to derive that path for every reachable keyless instance; resolve_anchor()
    # walks it back on the target NDF.  Proven 99.46% durable across the widest build jump.

    def stable_key(self, inst: NdfInstance):
        """(key_prop, key_val) for this instance, or None — the canonical durable identity.  Matches the
        tree (build_dat_data_tree/build_reference_graph): only STRING values (StringRef/PathRef) count as a
        key — a binary DescriptorId/Name (Hash/Guid/Blob) is NOT a durable string key and must not be used
        (it made resolve_anchor / find_instances miss, forcing a whole-NDF raw fallback)."""
        ci = inst.class_index
        for kp in ("ClassNameForDebug", "DescriptorId"):
            p = self.prop_by_name_and_class(kp, ci)
            if p is not None:
                v = inst.get(p.index)
                if v is not None and v.type_id in (T.StringRef, T.PathRef):
                    return (kp, self.get_string(v.raw))
        for kp in ("_ShortDatabaseName", "Name"):
            p = self.prop_by_name_and_class(kp, ci)
            if p is not None:
                v = inst.get(p.index)
                if v is not None and v.type_id in (T.StringRef, T.PathRef):
                    rv = self.get_string(v.raw)
                    if rv and rv != "None":
                        return (kp, rv)
        return None

    def _walk_objrefs_labeled(self, value: NdfValue, steps: list, out: list):
        """Collect (path_labels, target_idx) for every ObjRef in a value, recursing list/map/pair with a
        label per descent ('[i]' list, '<ki>'/'<vi>' map, 'k'/'v' pair)."""
        t = value.type_id
        if t == T.Reference:
            if value.raw[0] == OBJ_REF_MARKER:
                out.append((tuple(steps), value.raw[1][0]))
        elif t == T.List:
            for i, item in enumerate(value.raw):
                self._walk_objrefs_labeled(item, steps + [f"[{i}]"], out)
        elif t == T.Map:
            for i, (k, v) in enumerate(value.raw):
                self._walk_objrefs_labeled(k, steps + [f"<k{i}>"], out)
                self._walk_objrefs_labeled(v, steps + [f"<v{i}>"], out)
        elif t == T.Pair:
            self._walk_objrefs_labeled(value.raw[0], steps + ["k"], out)
            self._walk_objrefs_labeled(value.raw[1], steps + ["v"], out)

    def _ensure_anchor_index(self):
        """Build {keyless_idx -> {'root':[kp,kv], 'steps':[[label,...],...]}} once (length-staleness)."""
        if getattr(self, "_anchor_ninst", -1) == len(self.instances):
            return
        from collections import deque, Counter
        n = len(self.instances)
        keyval = [self.stable_key(inst) for inst in self.instances]
        # Roots must be GLOBALLY unique by (keyprop, keyval): resolve_anchor looks the root up across ALL
        # instances, so a key shared by several (e.g. _ShortDatabaseName 'FX_Feedback') can't be a durable
        # anchor root — exclude it (its children just stay positional).
        key_count: Counter = Counter(keyval[i] for i in range(n) if keyval[i] is not None)
        keyed = [keyval[i] is not None and key_count[keyval[i]] == 1 for i in range(n)]
        fwd: dict = {}
        for i, inst in enumerate(self.instances):
            for pv in inst.props:
                p = self.prop_by_index(pv.prop_index)
                pname = p.name if p is not None else f"#{pv.prop_index}"
                refs: list = []
                self._walk_objrefs_labeled(pv.value, [pname], refs)
                for label, tgt in refs:
                    if 0 <= tgt < n:
                        fwd.setdefault(i, []).append((tgt, label))
        depth = [-1] * n
        pred = [None] * n
        q = deque(i for i in range(n) if keyed[i])
        for i in q:
            depth[i] = 0
        while q:
            u = q.popleft()
            for v, label in fwd.get(u, ()):
                if depth[v] == -1:
                    depth[v] = depth[u] + 1
                    pred[v] = (u, list(label))
                    q.append(v)
        anchors: dict = {}
        for i in range(n):
            if keyed[i] or depth[i] < 0:
                continue
            steps = []
            cur = i
            while pred[cur] is not None:
                parent, label = pred[cur]
                steps.append(label)
                cur = parent
            steps.reverse()
            rk = keyval[cur]
            if rk is not None:
                anchors[i] = {"root": [rk[0], rk[1]], "steps": steps}
        self._anchor_map = anchors
        self._anchor_ninst = n

    def anchor_of(self, idx: int):
        """The durable anchor for a keyless instance (or None if keyed / unreachable from a keyed root)."""
        self._ensure_anchor_index()
        return self._anchor_map.get(idx)

    def _find_root_idx(self, key_prop: str, key_val: str):
        """Index of the instance whose stable_key == (key_prop, key_val); memoized (unique roots)."""
        if getattr(self, "_keyidx_ninst", -1) != len(self.instances):
            from collections import Counter
            first: dict = {}
            cnt: Counter = Counter()
            for i, inst in enumerate(self.instances):
                k = self.stable_key(inst)
                if k is not None:
                    first.setdefault(k, i)
                    cnt[k] += 1
            # only GLOBALLY-UNIQUE keys are resolvable roots (matches _ensure_anchor_index)
            self._keyidx = {k: i for k, i in first.items() if cnt[k] == 1}
            self._keyidx_ninst = len(self.instances)
        return self._keyidx.get((key_prop, key_val))

    def _nav_to_objref(self, value: NdfValue, descents: list):
        """From a property value, follow list/map/pair descents to an ObjRef; return its target idx or None."""
        for d in descents:
            t = value.type_id
            try:
                if d[:1] == "[" and t == T.List:
                    value = value.raw[int(d[1:-1])]
                elif d[:2] == "<k" and t == T.Map:
                    value = value.raw[int(d[2:-1])][0]
                elif d[:2] == "<v" and t == T.Map:
                    value = value.raw[int(d[2:-1])][1]
                elif d == "k" and t == T.Pair:
                    value = value.raw[0]
                elif d == "v" and t == T.Pair:
                    value = value.raw[1]
                else:
                    return None
            except (IndexError, ValueError, TypeError):
                return None
        if value.type_id == T.Reference and value.raw[0] == OBJ_REF_MARKER:
            return value.raw[1][0]
        return None

    def resolve_anchor(self, anchor: dict):
        """Resolve an anchor {'root':[kp,kv], 'steps':[[propname, descents...], ...]} to an instance index,
        or None if it doesn't resolve on this NDF (root key gone, or a path step broke — a drift signal)."""
        if not anchor:
            return None
        root = anchor.get("root")
        if not root or len(root) < 2:
            return None
        idx = self._find_root_idx(root[0], root[1])
        if idx is None:
            return None
        for step in (anchor.get("steps") or []):
            if not step:
                return None
            inst = self.instances[idx]
            p = self.prop_by_name_and_class(step[0], inst.class_index)
            if p is None:
                return None
            v = inst.get(p.index)
            if v is None:
                return None
            nxt = self._nav_to_objref(v, step[1:])
            if nxt is None or not (0 <= nxt < len(self.instances)):
                return None
            idx = nxt
        return idx

    def set_property(
        self,
        inst: NdfInstance,
        prop_name: str,
        value: NdfValue,
        create: bool = False,
    ) -> bool:
        """Set a named property on an instance.  Returns True if set.

        If the property name isn't in the table at all and ``create`` is True, a new NdfProperty is
        appended (PROP entries are pascal-name + u32 class_index, index implicit by position) so the
        instance gains a brand-new property.  The game's loaders look these up by name (proven for
        gdconstante constants), which is what lets a .rmod ADD a property rather than only change one."""
        prop = self.prop_by_name_and_class(prop_name, inst.class_index)
        if prop is None:
            prop = self.prop_by_name(prop_name)
        if prop is None:
            # Case-insensitive fallback (handles rmod/NDF casing mismatches like Guid vs GUID)
            lower = prop_name.lower()
            for p in self.properties:
                if p.name.lower() == lower and p.class_index == inst.class_index:
                    prop = p
                    break
            if prop is None:
                for p in self.properties:
                    if p.name.lower() == lower:
                        prop = p
                        break
        if prop is None:
            if not create:
                return False
            prop = NdfProperty(index=len(self.properties), name=prop_name,
                               class_index=inst.class_index)
            self.properties.append(prop)
        inst.set(prop.index, value)
        return True

    def ensure_string(self, s: str) -> int:
        """Return the STRG index for s, adding it if not present."""
        try:
            return self.strings.index(s)
        except ValueError:
            self.strings.append(s)
            return len(self.strings) - 1

    def ensure_trans(self, s: str) -> int:
        """Return the TRAN index for s, adding it if not present."""
        try:
            return self.trans.index(s)
        except ValueError:
            self.trans.append(s)
            return len(self.trans) - 1

    # ── import / export tree access (see parse_ref_tree) ─────────────────────
    def _cached_ref_paths(self, which: str, ref_list) -> Dict[int, str]:
        """Memoized ref_ordinal_paths(parse_ref_tree(ref_list), self.trans).

        resolve_value() calls this once PER import-ref value during a diff — on
        everything.cpp that's ~82k calls, each re-parsing the ENTIRE import ref tree
        (the dominant cost after the name-lookup fix: ~872s cumtime).  The import /
        export lists and the trans table are read-only during a diff, so cache on
        their identity+length.  The only mutators (add_import_path / add_export_path /
        reindex_imports) reassign self.import_list to a NEW object, so id() changes and
        the cache self-invalidates.  Callers that mutate the result copy it first.
        Returns a SHARED dict — do not mutate."""
        cache = self.__dict__.get("_ref_path_cache")
        if cache is None:
            cache = self._ref_path_cache = {}
        key = (id(ref_list), len(ref_list), len(self.trans))
        ent = cache.get(which)
        if ent is None or ent[0] != key:
            result = ref_ordinal_paths(parse_ref_tree(ref_list), self.trans)
            cache[which] = (key, result)
            return result
        return ent[1]

    def import_ordinal_paths(self) -> Dict[int, str]:
        """import ordinal -> full path (e.g. 15 -> '$/IA/Cluster/PackProxy_All').
        Instances reference imports BY ORDINAL; this maps them to portable paths.
        SHARED cached dict — copy before mutating (see _cached_ref_paths)."""
        return self._cached_ref_paths("import", self.import_list)

    def export_ordinal_paths(self) -> Dict[int, str]:
        """export ordinal -> full path.  SHARED cached dict — copy before mutating."""
        return self._cached_ref_paths("export", self.export_list)

    def import_path_ordinals(self) -> Dict[str, int]:
        """full path -> import ordinal (inverse of import_ordinal_paths)."""
        return {p: o for o, p in self.import_ordinal_paths().items()}

    def transref_is_legacy_bare(self, ref) -> bool:
        """True if `ref` is a legacy BARE-NAME TransRef value (a TRAN component name, no '$'/'/').

        Such a value resolves via `ensure_trans` to a TRAN index — but the GAME (and our converter) reads a
        TransRef ordinal as an IMPR IMPORT ordinal (deserializer case 9 -> Mgr import table).  A bare name
        therefore only works when its TRAN index COINCIDENTALLY equals the intended import's ordinal on the
        origin build; migrating/reconverting on another build breaks that coincidence and mis-points the
        reference (proven: Balanced Realism's MultiRenderTypeMaterialPack SFX_Impact... -> a wrong import on
        public).  Fix: re-convert the rmod on its ORIGIN build so the value becomes a portable '$/...' path.
        See RE_DATA/EVIDENCE_DOSSIER.md (Evidence #1)."""
        return isinstance(ref, str) and "/" not in ref and not ref.startswith("$")

    def add_import_path(self, path: str) -> int:
        """Ensure `path` is an import; return its ordinal (existing or newly appended).

        New imports get the next free ordinal (max+1), so existing instances'
        TransRef ordinals stay valid.  Rebuilds import_list + adds any missing TRAN
        components.  Same mechanism works for export via add_export_path."""
        cur = dict(self.import_ordinal_paths())  # copy: we mutate, cache dict is shared
        for o, p in cur.items():
            if p == path:
                return o
        new_ord = (max(cur) + 1) if cur else 0
        cur[new_ord] = path
        self.import_list = serialize_ref_tree(build_ref_tree(cur, self.ensure_trans))
        return new_ord

    def add_export_path(self, path: str) -> int:
        cur = dict(self.export_ordinal_paths())  # copy: we mutate, cache dict is shared
        for o, p in cur.items():
            if p == path:
                return o
        new_ord = (max(cur) + 1) if cur else 0
        cur[new_ord] = path
        self.export_list = serialize_ref_tree(build_ref_tree(cur, self.ensure_trans))
        return new_ord

    # ── Import/export ordinal maintenance ────────────────────────────────────

    def iter_values(self):
        """Yield every NdfValue in every instance, recursing into List/Map/Pair."""
        def rec(v):
            yield v
            t = v.type_id
            if t == T.List:
                for it in v.raw:
                    yield from rec(it)
            elif t == T.Map:
                for k, vv in v.raw:
                    yield from rec(k)
                    yield from rec(vv)
            elif t == T.Pair:
                k, vv = v.raw
                yield from rec(k)
                yield from rec(vv)
        for inst in self.instances:
            for pv in inst.props:
                yield from rec(pv.value)

    def remap_trans_refs(self, remap: Dict[int, int]) -> set:
        """Remap every instance TransRef ordinal via `remap` (old -> new), in place.

        Returns the set of used ordinals that had no `remap` entry — i.e. TransRefs
        left dangling by an import removal (the engine would read these out of the
        import table).  An empty set means every reference still resolves."""
        dangling = set()
        for v in self.iter_values():
            if v.type_id == T.Reference and isinstance(v.raw, tuple):
                marker, ref = v.raw
                if marker == TRANS_REF_MARKER and isinstance(ref, int):
                    if ref in remap:
                        if remap[ref] != ref:
                            v.raw = (marker, remap[ref])
                    else:
                        dangling.add(ref)
        return dangling

    def set_import_paths(self, ordered_paths: List[str]) -> Dict[int, int]:
        """Replace the import table with `ordered_paths`, assigning DENSE ordinals
        0..N-1, and remap every instance TransRef to the new ordinals by path identity.

        The engine sizes the import table to the node count and indexes it by ordinal,
        so ordinals MUST be dense — a gap (e.g. left by a removal) or an over-max
        ordinal (left by an append) makes referencing instances read out of bounds.
        Returns the old->new ordinal remap actually applied."""
        old = self.import_ordinal_paths()
        new_by_path = {p: i for i, p in enumerate(ordered_paths)}
        remap = {o: new_by_path[p] for o, p in old.items() if p in new_by_path}
        self.remap_trans_refs(remap)
        dense = {i: p for i, p in enumerate(ordered_paths)}
        self.import_list = serialize_ref_tree(build_ref_tree(dense, self.ensure_trans))
        return remap


# ── Binary reader ─────────────────────────────────────────────────────────────

class _Reader:
    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    def pos(self): return self._pos
    def seek(self, p): self._pos = p
    def remaining(self): return len(self._data) - self._pos

    def read(self, n: int) -> bytes:
        # Reject a negative length outright: a corrupt size field (e.g. a ZipBlob whose `size` is < the
        # fixed header it subtracts) would otherwise slice to b"" AND move the cursor BACKWARD, silently
        # misparsing the rest of the buffer instead of failing.
        if n < 0:
            raise ValueError(f"negative read length {n} at offset {self._pos}")
        b = self._data[self._pos:self._pos + n]
        if len(b) < n:
            raise ValueError(f"Unexpected end of data at offset {self._pos} (need {n}, have {len(b)})")
        self._pos += n
        return b

    def u8(self):  return struct.unpack_from("<B", self.read(1))[0]
    def u16(self): return struct.unpack_from("<H", self.read(2))[0]
    def i16(self): return struct.unpack_from("<h", self.read(2))[0]
    def u32(self): return struct.unpack_from("<I", self.read(4))[0]
    def i32(self): return struct.unpack_from("<i", self.read(4))[0]
    def i64(self): return struct.unpack_from("<q", self.read(8))[0]
    def u64(self): return struct.unpack_from("<Q", self.read(8))[0]
    def f32(self): return struct.unpack_from("<f", self.read(4))[0]
    def f64(self): return struct.unpack_from("<d", self.read(8))[0]

    def pascal_str(self, encoding: str = "utf-8") -> str:
        length = self.u32()
        return self.read(length).decode(encoding, errors="replace")

    def peek_u32(self) -> int:
        b = self._data[self._pos:self._pos + 4]
        if len(b) < 4:
            raise ValueError(f"Unexpected end of data at offset {self._pos} (peek u32)")
        return struct.unpack_from("<I", b)[0]


def _decompress_body(data: bytes) -> bytes:
    """Decompress if needed.  Returns the full uncompressed file bytes."""
    r = _Reader(data)
    magic1 = r.read(4)
    if magic1 != MAGIC_EUG0:
        raise ValueError(f"Expected EUG0, got {magic1!r}")
    r.read(4)  # \x00\x00\x00\x00
    magic2 = r.read(4)
    if magic2 != MAGIC_CNDF:
        raise ValueError(f"Expected CNDF, got {magic2!r}")

    compressed_flag = r.u32()
    is_compressed = (compressed_flag == COMPRESSED_FLAG)

    if is_compressed:
        # Header is first 40 bytes; bytes 40-43 are uncompressedSize; zlib stream at 44.
        # The stream uses Z_FULL_FLUSH blocks (ends with 00 00 FF FF sync marker), so we
        # must use the streaming decompressobj API — zlib.decompress() chokes on flush markers.
        header_bytes = data[:40]
        body_compressed = data[44:]
        import io
        fi = io.BytesIO(body_compressed)
        dobj = zlib.decompressobj(0)
        chunks = []
        try:
            while True:
                buf = dobj.unconsumed_tail or fi.read(65536)
                if not buf:
                    break
                chunk = dobj.decompress(buf)
                if not chunk:
                    break
                chunks.append(chunk)
        except zlib.error as e:                      # corrupt/hostile compressed body — clean caught error
            raise ValueError(f"corrupt compressed NDF body: {e}")
        return header_bytes + b"".join(chunks)
    return data


def _read_toc0(r: _Reader, toc0_offset: int) -> Dict[str, Tuple[int, int]]:
    """Read the TOC0 footer.  Returns {section_name: (offset, size)}."""
    r.seek(toc0_offset)
    magic = r.read(4)
    if magic != MAGIC_TOC0:
        raise ValueError(f"Expected TOC0, got {magic!r}")
    table_count = r.u32()
    tables = {}
    for _ in range(table_count):
        name = r.read(4).decode("ascii", errors="replace")   # a non-ASCII byte must not crash the parse
        r.read(4)   # pad
        offset = r.u32()
        r.read(4)   # pad
        size = r.u32()
        r.read(4)   # pad
        tables[name] = (offset, size)
    return tables


def _read_pascal_str_list(r: _Reader, offset: int, size: int, encoding: str = "utf-8") -> List[str]:
    """Read a section that is a flat list of length-prefixed strings."""
    result = []
    if size == 0:
        return result
    r.seek(offset)
    end = offset + size
    while r.pos() < end:
        s = r.pascal_str(encoding)
        result.append(s)
    return result


def _read_value(r: _Reader, _depth: int = 0) -> NdfValue:
    """Read one NDFType from the stream (typeId + payload)."""
    # Depth guard: List/Map/Pair recurse into _read_value, so a hostile value nested ~1000 deep would
    # overflow the Python stack (RecursionError).  Real NDF values are only a few levels deep; refuse
    # anything past a generous bound so the caller sees a clean ValueError, not a stack crash.
    if _depth > 200:
        raise ValueError(f"NDF value nested too deep (>200) at offset {r.pos()} — refusing")
    type_id = r.u32()

    if type_id == T.Bool:
        return NdfValue(type_id, r.u8())  # preserve raw byte; any non-zero = true
    if type_id == T.Int8:
        return NdfValue(type_id, r.u8())
    if type_id == T.Int16:
        return NdfValue(type_id, r.i16())
    if type_id == T.UInt16:
        return NdfValue(type_id, r.u16())
    if type_id == T.Int32:
        return NdfValue(type_id, r.i32())
    if type_id == T.UInt32:
        return NdfValue(type_id, r.u32())
    if type_id == T.Time64:
        return NdfValue(type_id, r.u64())
    if type_id == T.Long:
        return NdfValue(type_id, r.i64())
    if type_id == T.Float32:
        return NdfValue(type_id, r.f32())
    if type_id == T.Float64:
        return NdfValue(type_id, r.f64())
    if type_id in (T.StringRef, T.PathRef):
        return NdfValue(type_id, r.u32())  # index into STRG
    if type_id == T.WideStr:
        length = r.u32()
        raw = r.read(length)
        return NdfValue(type_id, raw.decode("utf-16-le", errors="replace"))
    if type_id == T.Reference:
        marker = r.u32()
        if marker == TRANS_REF_MARKER:
            ref_idx = r.u32()
            return NdfValue(type_id, (TRANS_REF_MARKER, ref_idx))
        if marker == OBJ_REF_MARKER:
            obj_idx = r.u32()
            cls_idx = r.u32()
            return NdfValue(type_id, (OBJ_REF_MARKER, (obj_idx, cls_idx)))
        return NdfValue(type_id, (marker, r.read(4)))  # unknown ref type
    if type_id == T.Vector3:
        return NdfValue(type_id, (r.f32(), r.f32(), r.f32()))
    if type_id == T.Color128:
        return NdfValue(type_id, (r.f32(), r.f32(), r.f32(), r.f32()))
    if type_id == T.Color32:
        return NdfValue(type_id, tuple(r.read(4)))
    if type_id == T.TripleInt:
        return NdfValue(type_id, (r.i32(), r.i32(), r.i32()))
    if type_id == T.Matrix:
        return NdfValue(type_id, [r.f32() for _ in range(16)])
    if type_id == T.Int2:
        return NdfValue(type_id, (r.i32(), r.i32()))
    if type_id == T.Float2:
        return NdfValue(type_id, (r.f32(), r.f32()))
    if type_id == T.Guid:
        return NdfValue(type_id, r.read(16))
    if type_id == T.Hash:
        return NdfValue(type_id, r.read(16))
    if type_id == T.LocHash:
        return NdfValue(type_id, r.read(8))
    if type_id == T.Blob:
        size = r.u32()
        return NdfValue(type_id, r.read(size))
    if type_id == T.ZipBlob:
        size = r.u32()
        flag = r.u8()
        if flag == 1:
            uncomp_size = r.u32()
            comp_data = r.read(size - 4)
            try:
                body = zlib.decompress(comp_data)
            except Exception:
                body = comp_data  # keep raw on failure
            return NdfValue(type_id, {"flag": flag, "uncomp_size": uncomp_size, "data": body})
        else:
            body = r.read(size - 1)
            return NdfValue(type_id, {"flag": flag, "data": body})
    if type_id == T.List:
        count = r.u32()
        items = [_read_value(r, _depth + 1) for _ in range(count)]
        return NdfValue(type_id, items)
    if type_id == T.Map:
        count = r.u32()
        pairs = []
        for _ in range(count):
            k = _read_value(r, _depth + 1)
            v = _read_value(r, _depth + 1)
            pairs.append((k, v))
        return NdfValue(type_id, pairs)
    if type_id == T.Pair:
        k = _read_value(r, _depth + 1)
        v = _read_value(r, _depth + 1)
        return NdfValue(type_id, (k, v))

    # Unknown type — we cannot safely parse further; store what we have
    raise ValueError(f"Unknown NDF type id 0x{type_id:02X} at offset {r.pos() - 4}")


def _read_instances(r: _Reader, obje_offset: int, obje_size: int) -> List[NdfInstance]:
    """Parse the OBJE section into a list of NdfInstances."""
    instances = []
    if obje_size == 0:
        return instances
    r.seek(obje_offset)
    end = obje_offset + obje_size
    while r.pos() < end:
        class_index = r.u32()
        inst = NdfInstance(class_index=class_index)
        while True:
            prop_index = r.u32()
            if prop_index == PROPERTY_SENTINEL:
                break
            val = _read_value(r)
            inst.props.append(NdfPropertyValue(prop_index, val))
        instances.append(inst)
    return instances


def _read_uint_list(r: _Reader, offset: int, size: int) -> List[int]:
    result = []
    if size == 0:
        return result
    r.seek(offset)
    end = offset + size
    while r.pos() < end:
        result.append(r.u32())
    return result


def _read_properties(r: _Reader, offset: int, size: int) -> List[NdfProperty]:
    """Read the PROP section: each entry is pascal-str name + uint32 class_index."""
    props = []
    if size == 0:
        return props
    r.seek(offset)
    end = offset + size
    idx = 0
    while r.pos() < end:
        name = r.pascal_str("iso-8859-1")
        class_index = r.u32()
        props.append(NdfProperty(index=idx, name=name, class_index=class_index))
        idx += 1
    return props


def read(data: bytes) -> NdfBinary:
    """Parse a .ndfbin / .gladndfbin binary blob into an NdfBinary object."""
    uncompressed = _decompress_body(data)
    r = _Reader(uncompressed)

    # Header
    r.read(4)   # EUG0
    r.read(4)   # \x00\x00\x00\x00
    r.read(4)   # CNDF
    compressed_flag = r.u32()
    toc0_offset = r.u32()
    _unk0 = r.u32()
    _header_size = r.u32()
    _unk2 = r.u32()
    _size = r.u32()
    _unk4 = r.u32()
    _uncomp_size = r.u32()

    is_compressed = (compressed_flag == COMPRESSED_FLAG)

    tables = _read_toc0(r, toc0_offset)

    def tbl(name):
        return tables.get(name, (0, 0))

    classes_raw  = _read_pascal_str_list(r, *tbl("CLAS"), encoding="utf-8")
    ndf_classes  = [NdfClass(index=i, name=n) for i, n in enumerate(classes_raw)]

    ndf_props    = _read_properties(r, *tbl("PROP"))

    strings      = _read_pascal_str_list(r, *tbl("STRG"), encoding="iso-8859-1")
    trans        = _read_pascal_str_list(r, *tbl("TRAN"), encoding="iso-8859-1")

    top_objects  = _read_uint_list(r, *tbl("TOPO"))
    import_list  = _read_uint_list(r, *tbl("IMPR"))
    export_list  = _read_uint_list(r, *tbl("EXPR"))

    instances    = _read_instances(r, *tbl("OBJE"))

    ndf = NdfBinary(
        classes=ndf_classes,
        properties=ndf_props,
        strings=strings,
        trans=trans,
        instances=instances,
        top_objects=top_objects,
        import_list=import_list,
        export_list=export_list,
        is_compressed=is_compressed,
    )
    return ndf


# ── IMPR / EXPR import-export tree ─────────────────────────────────────────────
# The IMPR (import) and EXPR (export) sections are NOT flat lists — they are a TREE
# of path components (ModdingSuite reads them raw and notes "actually more a tree,
# still not fully reversed").  Fully decoded here.
#
# Each node record is a flat run of uint32s:
#     name_index   (u32)   index into the TRAN table = this path component's name
#     ordinal      (u32)   the import/export ordinal for this node, or 0xFFFFFFFF
#                          when the node is a pure namespace (referenced only as a
#                          path prefix).  Instances reference an import/export BY
#                          THIS ORDINAL (not by TRAN index); ordinals are a
#                          contiguous 0..N-1 space per section.
#     child_count  (u32)
#     child_off[]  (u32 × child_count)   each is a BYTE offset, relative to the
#                          start of this record's child-offset array, to a child
#                          node record within the same section.
#
# The full path of a node is its name joined to its parent chain with "/"
# (root is always TRAN[0] == "$").  Round-trips byte-exact across every game NDF.

@dataclass
class NdfRefNode:
    """One node in an IMPR/EXPR path tree."""
    name_index: int                       # TRAN index of this path component
    ordinal: int                          # import/export ordinal, or -1 for a pure namespace
    children: List[int] = field(default_factory=list)   # indices into the node list


def parse_ref_tree(uints: List[int]) -> List["NdfRefNode"]:
    """Decode a flat IMPR/EXPR uint list into a list of NdfRefNode (original order)."""
    recs = []                     # (byte, name, ordinal_raw, [child_byte...])
    byte_to_idx = {}
    i = 0
    n = len(uints)
    while i < n:
        # Bounds-check every field read from the (untrusted) flat list: a truncated record header or a
        # bogus child_count would otherwise IndexError off the end.  Raise a clean ValueError instead so
        # the convert/apply callers fall back to a raw file patch.
        if i + 3 > n:
            raise ValueError("truncated IMPR/EXPR record header")
        byte = i * 4
        name = uints[i]; ordinal_raw = uints[i + 1]; cc = uints[i + 2]
        if i + 3 + cc > n:
            raise ValueError(f"IMPR/EXPR child count {cc} exceeds list length")
        child_slots_byte = (i + 3) * 4
        child_bytes = [child_slots_byte + uints[i + 3 + k] for k in range(cc)]
        byte_to_idx[byte] = len(recs)
        recs.append((byte, name, ordinal_raw, child_bytes))
        i += 3 + cc
    nodes = []
    for _byte, name, ordinal_raw, child_bytes in recs:
        kids = []
        for cb in child_bytes:
            if cb not in byte_to_idx:                # child offset must land on a record boundary
                raise ValueError(f"IMPR/EXPR child offset {cb} is not a record boundary")
            kids.append(byte_to_idx[cb])
        nodes.append(NdfRefNode(
            name_index=name,
            ordinal=(-1 if ordinal_raw == 0xFFFFFFFF else ordinal_raw),
            children=kids,
        ))
    return nodes


def serialize_ref_tree(nodes: List["NdfRefNode"]) -> List[int]:
    """Encode NdfRefNodes back to a flat IMPR/EXPR uint list (inverse of parse_ref_tree)."""
    # Byte offset of each node record (record = 3 + child_count uints).
    offsets = []
    pos = 0
    for nd in nodes:
        offsets.append(pos * 4)
        pos += 3 + len(nd.children)
    out: List[int] = []
    for nd in nodes:
        out.append(nd.name_index)
        out.append(0xFFFFFFFF if nd.ordinal == -1 else nd.ordinal)
        out.append(len(nd.children))
        child_slots_byte = len(out) * 4       # byte offset of the first child slot
        for cidx in nd.children:
            out.append(offsets[cidx] - child_slots_byte)
    return out


def resolve_ref_paths(nodes: List["NdfRefNode"], trans: List[str]) -> Dict[int, str]:
    """node-list-index -> full "/"-joined path (e.g. '$/IA/Cluster/PackMeshSkirmish_JAP')."""
    parent = {}
    for pi, nd in enumerate(nodes):
        for ci in nd.children:
            parent[ci] = pi

    def _name(i):
        ni = nodes[i].name_index
        return trans[ni] if 0 <= ni < len(trans) else f"?{ni}"

    paths = {}
    for i in range(len(nodes)):
        parts, cur = [], i
        seen = set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            parts.append(_name(cur))
            cur = parent.get(cur)
        paths[i] = "/".join(reversed(parts))
    return paths


def ref_ordinal_paths(nodes: List["NdfRefNode"], trans: List[str]) -> Dict[int, str]:
    """import/export ordinal -> full path, for every node that carries an ordinal."""
    paths = resolve_ref_paths(nodes, trans)
    return {nd.ordinal: paths[i] for i, nd in enumerate(nodes) if nd.ordinal != -1}


def build_ref_tree(ordinal_paths: Dict[int, str], trans_lookup) -> List["NdfRefNode"]:
    """Build an IMPR/EXPR node list from {ordinal: path}.

    `trans_lookup(name)` returns/creates a TRAN index for a path component (e.g.
    NdfBinary.ensure_trans).  Intermediate path components become namespace nodes
    (ordinal -1); the leaf/最終 component of each path carries its ordinal.  Nodes are
    emitted parent-before-child (root '$' first) so child offsets are always forward.
    """
    # Trie keyed by full path prefix.
    root_key = "$"
    order = [root_key]                        # path-prefix -> emit order
    node_of = {root_key: NdfRefNode(name_index=trans_lookup("$"), ordinal=-1, children=[])}
    ordinal_of = {}
    for ordinal, path in sorted(ordinal_paths.items()):
        parts = path.split("/")
        if parts and parts[0] == "$":
            parts = parts[1:]
        prefix = root_key
        for comp in parts:
            child_key = prefix + "/" + comp
            if child_key not in node_of:
                node_of[child_key] = NdfRefNode(name_index=trans_lookup(comp), ordinal=-1, children=[])
                order.append(child_key)
            prefix = child_key
        ordinal_of[prefix] = ordinal
    for key, ordinal in ordinal_of.items():
        node_of[key].ordinal = ordinal
    # assemble list in emit order, wiring children by prefix relationship
    index_of = {k: i for i, k in enumerate(order)}
    nodes = [node_of[k] for k in order]
    for k in order:
        parent = k.rsplit("/", 1)[0] if "/" in k else None
        if parent is not None and parent in index_of:
            nodes[index_of[parent]].children.append(index_of[k])
    return nodes


# ── Binary writer ─────────────────────────────────────────────────────────────

class _Writer:
    def __init__(self):
        self._buf = bytearray()

    def pos(self): return len(self._buf)
    def data(self): return bytes(self._buf)

    def write(self, b: bytes): self._buf.extend(b)
    def u8(self, v):  self._buf.extend(struct.pack("<B", v & 0xFF))
    def u16(self, v): self._buf.extend(struct.pack("<H", v & 0xFFFF))
    def i16(self, v): self._buf.extend(struct.pack("<h", v))
    def u32(self, v): self._buf.extend(struct.pack("<I", v & 0xFFFFFFFF))
    def i32(self, v): self._buf.extend(struct.pack("<i", v))
    def i64(self, v): self._buf.extend(struct.pack("<q", v))
    def u64(self, v): self._buf.extend(struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF))
    def f32(self, v): self._buf.extend(struct.pack("<f", v))
    def f64(self, v): self._buf.extend(struct.pack("<d", v))

    def pascal_str(self, s: str, encoding: str = "utf-8"):
        b = s.encode(encoding, errors="replace")
        self.u32(len(b))
        self.write(b)


def _write_value(w: _Writer, val: NdfValue):
    w.u32(val.type_id)
    t = val.type_id
    v = val.raw

    if t == T.Bool:
        # Preserve non-standard raw byte values; Python bools write as 0/1
        w.u8((1 if v else 0) if isinstance(v, bool) else (v & 0xFF))
    elif t == T.Int8:  w.u8(v)
    elif t == T.Int16: w.i16(v)
    elif t == T.UInt16: w.u16(v)
    elif t == T.Int32: w.i32(v)
    elif t == T.UInt32: w.u32(v)
    elif t == T.Time64: w.u64(v)
    elif t == T.Long:  w.i64(v)
    elif t == T.Float32: w.f32(v)
    elif t == T.Float64: w.f64(v)
    elif t in (T.StringRef, T.PathRef): w.u32(v)
    elif t == T.WideStr:
        b = v.encode("utf-16-le")
        w.u32(len(b))
        w.write(b)
    elif t == T.Reference:
        marker, ref = v
        w.u32(marker)
        if marker == TRANS_REF_MARKER:
            w.u32(ref)
        elif marker == OBJ_REF_MARKER:
            obj_idx, cls_idx = ref
            w.u32(obj_idx)
            w.u32(cls_idx)
        else:
            w.write(ref[:4])
    elif t == T.Vector3:
        w.f32(v[0]); w.f32(v[1]); w.f32(v[2])
    elif t == T.Color128:
        w.f32(v[0]); w.f32(v[1]); w.f32(v[2]); w.f32(v[3])
    elif t == T.Color32:
        w.write(bytes(v[:4]))
    elif t == T.TripleInt:
        w.i32(v[0]); w.i32(v[1]); w.i32(v[2])
    elif t == T.Matrix:
        for f in v: w.f32(f)
    elif t == T.Int2:
        w.i32(v[0]); w.i32(v[1])
    elif t == T.Float2:
        w.f32(v[0]); w.f32(v[1])
    elif t == T.Guid:
        w.write(bytes(v))
    elif t == T.Hash:
        w.write(bytes(v))
    elif t == T.LocHash:
        w.write(bytes(v))
    elif t == T.Blob:
        w.u32(len(v))
        w.write(bytes(v))
    elif t == T.ZipBlob:
        flag = v["flag"]
        body = v["data"]
        if flag == 1:
            comp = zlib.compress(body, level=9)
            w.u32(4 + len(comp))   # size field = uncomp_size_field + comp data
            w.u8(1)
            w.u32(len(body))
            w.write(comp)
        else:
            w.u32(len(body))
            w.u8(flag)
            w.write(body)
    elif t == T.List:
        w.u32(len(v))
        for item in v:
            _write_value(w, item)
    elif t == T.Map:
        w.u32(len(v))
        for k, val2 in v:
            _write_value(w, k)
            _write_value(w, val2)
    elif t == T.Pair:
        k, val2 = v
        _write_value(w, k)
        _write_value(w, val2)
    else:
        raise ValueError(f"Cannot serialize unknown NDF type 0x{t:02X}")


def _write_pascal_str_list(strings: List[str], encoding: str = "utf-8") -> bytes:
    w = _Writer()
    for s in strings:
        w.pascal_str(s, encoding)
    return w.data()


def _write_properties(props: List[NdfProperty]) -> bytes:
    w = _Writer()
    for p in props:
        w.pascal_str(p.name, "iso-8859-1")
        w.u32(p.class_index)
    return w.data()


def _write_instances(instances: List[NdfInstance]) -> bytes:
    w = _Writer()
    for inst in instances:
        w.u32(inst.class_index)
        for pv in inst.props:
            w.u32(pv.prop_index)
            _write_value(w, pv.value)
        w.u32(PROPERTY_SENTINEL)
    return w.data()


def _write_uint_list(items: List[int]) -> bytes:
    w = _Writer()
    for i in items:
        w.u32(i)
    return w.data()


def write(ndf: NdfBinary, compress: bool = False) -> bytes:
    """Serialise an NdfBinary back to bytes ready to write into a .dat file."""

    # ── Compile all sections ─────────────────────────────────────────────────
    obje_bytes = _write_instances(ndf.instances)
    topo_bytes = _write_uint_list(ndf.top_objects)
    chnk_bytes = _make_chnk(ndf)
    clas_bytes = _write_pascal_str_list([c.name for c in ndf.classes], "utf-8")
    prop_bytes = _write_properties(ndf.properties)
    strg_bytes = _write_pascal_str_list(ndf.strings, "iso-8859-1")
    tran_bytes = _write_pascal_str_list(ndf.trans, "iso-8859-1")
    impr_bytes = _write_uint_list(ndf.import_list)
    expr_bytes = _write_uint_list(ndf.export_list)

    # Section offsets are always in decompressed space: 40-byte header + body.
    # For compressed output the file layout is header[:40] + uncompsize(4) + zlib_body,
    # but _decompress_body strips the uncompsize field before parsing, so offsets
    # are always relative to a 40-byte header regardless of compression.
    HEADER_SIZE = 40

    # Build body = all section data concatenated
    # Each section offset is relative to start of file (header inclusive)
    sections = [
        ("OBJE", obje_bytes),
        ("TOPO", topo_bytes),
        ("CHNK", chnk_bytes),
        ("CLAS", clas_bytes),
        ("PROP", prop_bytes),
        ("STRG", strg_bytes),
        ("TRAN", tran_bytes),
        ("IMPR", impr_bytes),
        ("EXPR", expr_bytes),
    ]

    # Body = section data; TOC0 comes after
    body_w = _Writer()
    section_offsets = []
    for name, data in sections:
        offset = HEADER_SIZE + body_w.pos()
        section_offsets.append((name, offset, len(data)))
        body_w.write(data)

    # TOC0 footer
    toc0_offset = HEADER_SIZE + body_w.pos()
    toc0_w = _Writer()
    toc0_w.write(b"TOC0")
    toc0_w.u32(len(sections))
    for name, offset, size in section_offsets:
        toc0_w.write(name.encode("ascii"))
        toc0_w.u32(0)     # pad
        toc0_w.u32(offset)
        toc0_w.u32(0)     # pad
        toc0_w.u32(size)
        toc0_w.u32(0)     # pad
    body_w.write(toc0_w.data())

    body = body_w.data()
    full_uncomp_size = HEADER_SIZE + len(body)

    # ── Build header ─────────────────────────────────────────────────────────
    compressed_flag = COMPRESSED_FLAG if compress else 0
    hdr_w = _Writer()
    hdr_w.write(MAGIC_EUG0)
    hdr_w.u32(0)
    hdr_w.write(MAGIC_CNDF)
    hdr_w.u32(compressed_flag)
    hdr_w.u32(toc0_offset)
    hdr_w.u32(0)                        # unk0
    hdr_w.u32(40)                       # headerSize (always 40)
    hdr_w.u32(0)                        # unk2
    hdr_w.u32(HEADER_SIZE + len(body))  # size = full uncompressed file size (header + body)
    hdr_w.u32(0)                        # unk4
    hdr_w.u32(len(body))                # uncompressedSize = body only (used for decompression)

    header = hdr_w.data()   # 44 bytes

    if compress:
        # Eugen's loader expects the stream FLUSHED with Z_SYNC_FLUSH (ends in 00 00 ff ff,
        # no Z_FINISH / no adler32 tail) — exactly like the .kdt / .tgv blobs.  A vanilla
        # zlib.compress() (Z_FINISH) inflates fine in Python but the game's streaming
        # inflater chokes on it → silent load hang.  Match the on-disk framing.
        co = zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS)
        comp_body = co.compress(body) + co.flush(zlib.Z_SYNC_FLUSH)
        return header[:40] + struct.pack("<I", len(body)) + comp_body
    else:
        return header[:40] + body


def _make_chnk(ndf: NdfBinary) -> bytes:
    # CHNK is a single 8-byte entry: (start=0, total_instance_count).
    # Verified from original game files — both everything.cpp.gladndfbin (63704
    # instances) and everythinggdconstanteoriginal.cpp.gladndfbin (1 instance)
    # contain exactly one entry covering the full instance range.
    w = _Writer()
    w.u32(0)
    w.u32(len(ndf.instances))
    return w.data()


# ── Value construction helpers ─────────────────────────────────────────────────

def val_int32(v: int)   -> NdfValue: return NdfValue(T.Int32, int(v))
def val_uint32(v: int)  -> NdfValue: return NdfValue(T.UInt32, int(v))
def val_float32(v: float) -> NdfValue: return NdfValue(T.Float32, float(v))
def val_float64(v: float) -> NdfValue: return NdfValue(T.Float64, float(v))
def val_bool(v: bool)   -> NdfValue: return NdfValue(T.Bool, bool(v))
def val_int16(v: int)   -> NdfValue: return NdfValue(T.Int16, int(v))
def val_uint16(v: int)  -> NdfValue: return NdfValue(T.UInt16, int(v))


def _bytes_from_value(raw_value, expected_len: int, type_name: str) -> bytes:
    """Convert a list-of-ints or hex string to bytes of expected_len."""
    if isinstance(raw_value, (list, tuple)):
        b = bytes(int(x) for x in raw_value)
    elif isinstance(raw_value, str):
        b = bytes.fromhex(raw_value.replace(" ", ""))
    elif isinstance(raw_value, (bytes, bytearray)):
        b = bytes(raw_value)
    else:
        raise ValueError(f"{type_name} value must be a list of ints or a hex string")
    if len(b) != expected_len:
        raise ValueError(f"{type_name} requires exactly {expected_len} bytes, got {len(b)}")
    return b


def make_value(type_name: str, raw_value, _depth: int = 0) -> NdfValue:
    """
    Create an NdfValue from a human-readable type name and a Python value.
    Used when applying .rmod patch files.

    type_name is case-insensitive and matches the T class attribute names,
    e.g. "Int32", "Float32", "Bool", "UInt32", "StringRef", "WideStr", etc.

    Additional types beyond the basic scalars:
      ObjRef   — object reference; raw_value = {"inst": N, "class": C}
      TransRef — trans reference; raw_value = {"trans": N}
      LocHash  — 8-byte localisation hash; raw_value = list of 8 ints or 16-char hex string
      Guid     — 16-byte GUID; raw_value = list of 16 ints or 32-char hex string
      Blob     — raw byte blob; raw_value = list of ints or hex string
      List<T>  — homogeneous list; raw_value = JSON array of T values
      Map<K,V> — key-value map; raw_value = JSON array of [key, val] 2-element arrays
    """
    # Depth guard: List<...>/Map<...>/Pair recurse, so a hostile rmod with a deeply nested type string or
    # value array would overflow the stack (RecursionError).  Real values are shallow — refuse past a
    # generous bound with a clean ValueError instead.
    if _depth > 200:
        raise ValueError(f"rmod value nested too deep (>200) for type {type_name!r}")
    TYPE_MAP = {
        "bool":     T.Bool,
        "int8":     T.Int8,
        "int16":    T.Int16,
        "uint16":   T.UInt16,
        "int32":    T.Int32,
        "uint32":   T.UInt32,
        "long":     T.Long,
        "time64":   T.Time64,
        "float32":  T.Float32,
        "float":    T.Float32,
        "float64":  T.Float64,
        "double":   T.Float64,
        "stringref": T.StringRef,
        "pathref":  T.PathRef,
        "widestr":  T.WideStr,
        "widestring": T.WideStr,
        "vector3":  T.Vector3,
        "color32":  T.Color32,
        "color128": T.Color128,
        "tripleint": T.TripleInt,
        "int2":     T.Int2,
        "float2":   T.Float2,
    }
    key = type_name.lower().strip()

    # Handle "List<ElementType>" — e.g. "List<Int32>" or "List<ObjRef>"
    # Elements may be plain values or {"inst":N,"class":C} dicts for ObjRef.
    if key.startswith("list<") and key.endswith(">"):
        elem_type_name = key[5:-1].strip()
        if not isinstance(raw_value, (list, tuple)):
            raise ValueError(
                f"Value for '{type_name}' must be a JSON array, got {type(raw_value).__name__}"
            )
        items = [make_value(elem_type_name, v, _depth + 1) for v in raw_value]
        return NdfValue(T.List, items)

    # Handle "Map<KeyType,ValType>" — e.g. "Map<ObjRef,ObjRef>"
    if key.startswith("map<") and key.endswith(">"):
        inner = key[4:-1].strip()
        # Find the comma at depth 0 to split key/value types
        depth, split_pos = 0, -1
        for i, ch in enumerate(inner):
            if ch == "<":
                depth += 1
            elif ch == ">":
                depth -= 1
            elif ch == "," and depth == 0:
                split_pos = i
                break
        if split_pos == -1:
            raise ValueError(f"Invalid Map type: {type_name!r}")
        key_type = inner[:split_pos].strip()
        val_type = inner[split_pos + 1:].strip()
        if not isinstance(raw_value, (list, tuple)):
            raise ValueError(f"Value for '{type_name}' must be a JSON array of [key, val] pairs")
        pairs = []
        for pair in raw_value:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValueError("Each Map entry must be a 2-element array [key, val]")
            pairs.append((make_value(key_type, pair[0], _depth + 1),
                          make_value(val_type, pair[1], _depth + 1)))
        return NdfValue(T.Map, pairs)

    # Object reference: {"inst": N, "class": C}
    if key == "objref":
        if not isinstance(raw_value, dict):
            raise ValueError("ObjRef value must be {'inst': N, 'class': C}")
        inst = int(raw_value["inst"])
        cls  = int(raw_value["class"])
        return NdfValue(T.Reference, (OBJ_REF_MARKER, (inst, cls)))

    # Trans reference. Portable form: {"trans": "$/IA/Cluster/PackMesh_All"} — an
    # IMPORT PATH the applier resolves to the target NDF's ordinal (_resolve_string_refs).
    # Fallbacks: {"trans_ordinal": N} (raw import ordinal) / {"trans": N} (legacy int).
    if key == "transref":
        if not isinstance(raw_value, dict):
            raise ValueError("TransRef value must be {'trans': path} or {'trans_ordinal': N}")
        if "trans_ordinal" in raw_value:
            return NdfValue(T.Reference, (TRANS_REF_MARKER, int(raw_value["trans_ordinal"])))
        ref = raw_value["trans"]
        if isinstance(ref, str):
            # String form — resolved to an ordinal against the live NDF in _resolve_string_refs
            return NdfValue(T.Reference, (TRANS_REF_MARKER, ref))
        return NdfValue(T.Reference, (TRANS_REF_MARKER, int(ref)))

    # GUID (16 bytes)
    if key == "guid":
        return NdfValue(T.Guid, _bytes_from_value(raw_value, 16, "Guid"))

    # LocHash (8 bytes)
    if key in ("lochash", "loch"):
        return NdfValue(T.LocHash, _bytes_from_value(raw_value, 8, "LocHash"))

    # Blob (variable bytes) — stored as base64 in rmod files
    if key == "blob":
        if isinstance(raw_value, (list, tuple)):
            b = bytes(int(x) for x in raw_value)
        elif isinstance(raw_value, (bytes, bytearray)):
            b = bytes(raw_value)
        elif isinstance(raw_value, str):
            try:
                b = base64.b64decode(raw_value)
            except Exception:
                b = bytes.fromhex(raw_value.replace(" ", ""))
        else:
            raise ValueError("Blob value must be a base64 string or list of ints")
        return NdfValue(T.Blob, b)

    # ZipBlob — stored as {"flag": N, "data": "<base64>"} in rmod files
    if key == "zipblob":
        if not isinstance(raw_value, dict):
            raise ValueError("ZipBlob value must be a dict with 'flag' and 'data' (base64)")
        flag = int(raw_value.get("flag", 0))
        data = base64.b64decode(raw_value.get("data", ""))
        if flag == 1:
            return NdfValue(T.ZipBlob, {"flag": 1, "uncomp_size": len(data), "data": data})
        return NdfValue(T.ZipBlob, {"flag": flag, "data": data})

    # Matrix — 16 floats stored as a JSON array
    if key == "matrix":
        if not isinstance(raw_value, (list, tuple)) or len(raw_value) != 16:
            raise ValueError("Matrix requires a list of exactly 16 floats")
        return NdfValue(T.Matrix, [float(x) for x in raw_value])

    # Hash — 16 bytes stored as a 32-char hex string
    if key == "hash":
        return NdfValue(T.Hash, _bytes_from_value(raw_value, 16, "Hash"))

    # Pair — two typed values stored as [{"type":..,"value":..}, {"type":..,"value":..}]
    if key == "pair":
        if not isinstance(raw_value, (list, tuple)) or len(raw_value) != 2:
            raise ValueError("Pair requires a 2-element list [key_def, val_def]")
        k_def, v_def = raw_value[0], raw_value[1]
        k_val = make_value(k_def["type"], k_def["value"], _depth + 1)
        v_val = make_value(v_def["type"], v_def["value"], _depth + 1)
        return NdfValue(T.Pair, (k_val, v_val))

    type_id = TYPE_MAP.get(key)
    if type_id is None:
        raise ValueError(f"Unknown type name {type_name!r}. Valid types: {list(TYPE_MAP)}")

    # Coerce raw_value to the correct Python type
    if type_id in (T.Bool,):
        if isinstance(raw_value, str):
            raw_value = 1 if raw_value.lower() in ("true", "1", "yes") else 0
        else:
            raw_value = int(bool(raw_value))  # write as 0 or 1 int
    elif type_id in (T.Int8, T.Int16, T.UInt16, T.Int32, T.UInt32, T.Long, T.Time64):
        raw_value = int(raw_value)
    elif type_id in (T.Float32, T.Float64):
        raw_value = float(raw_value)
    elif type_id in (T.StringRef, T.PathRef):
        # raw_value is expected to be the string content, resolved to index by applier
        pass
    elif type_id == T.WideStr:
        raw_value = str(raw_value)
    elif type_id == T.Vector3:
        if isinstance(raw_value, (list, tuple)):
            raw_value = tuple(float(x) for x in raw_value)
    elif type_id == T.Int2:
        if isinstance(raw_value, (list, tuple)):
            raw_value = tuple(int(x) for x in raw_value)
    elif type_id == T.Float2:
        if isinstance(raw_value, (list, tuple)):
            raw_value = tuple(float(x) for x in raw_value)

    return NdfValue(type_id, raw_value)

"""The normalized edit engine — one place that owns every mutation, so every editor makes the same edit
the same correct way.

Why this module exists.  The edit operations used to be spread between UI dialogs (`ndf_value_editor`), a
handful of callers (`scenario_gen`, `tools_editor`, the scenario tabs) and raw `ndfbin` pokes.  When a link has
no owner it rots silently: the scenario-clone path substitutes the `.scenario` name but NOT the `Scripting_*`
folder, so a cloned mission ships a script its own cluster never puts on the python path — 85 of 85 shipped
clusters, and no test could have caught it because no function owned that link.  Everything here is UI-free,
returns a result describing what it touched, ASSERTS its preconditions instead of documenting them, and has a
test in tools/test_scripts/.

Layers (see our internal scenario notes):
  1. reference primitives  — ObjRef / TransRef / strings / instances / list props, + check_refs
  2. text operations       — the LocHash model: re-text, re-point, mint (all languages at once)
  3. chain / link ops      — bind_guid, point_cluster_at_*, clone_scenario ...     (P1)
  4. script operations     — the script_logic setters + set_next_chapter           (P1)

The invariants asserted in layer 1 are read from the game's own value deserializer (`a game routine` case 9,
our internal scenario notes):

  * An ObjRef stores (obj_idx, class_idx).  The engine uses class_idx to pick WHICH per-class object table to
    index, then reads slot obj_idx from it.  A wrong class therefore reads a DIFFERENT class's table and
    AddRefs whatever is there — this is the main-menu crash, not a cosmetic mislabel.  So an ObjRef's class is
    always coerced from its target, never taken from the caller.
  * A TransRef stores only an ordinal, and the engine indexes the import table with NO bounds check
    (`Mgr[0x89][ordinal]`).  Dense, in-range ordinals are a memory-safety requirement, not tidiness.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import dic as dic_mod
from . import localization as loc_mod
from . import ndfbin as ndfbin_mod
from .ndfbin import OBJ_REF_MARKER, TRANS_REF_MARKER, NdfValue, T


# ── results ──────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Finding:
    """One problem found by a check.  `severity` is 'error' (the game would misbehave or crash) or
    'warning' (suspicious but survivable)."""
    kind: str
    severity: str
    where: str
    detail: str

    def __str__(self):
        return f"[{self.severity}] {self.kind}: {self.where} — {self.detail}"


@dataclass
class EditResult:
    """What an operation actually did.  Operations return this instead of mutating silently so a caller
    (or a test) can assert on the changes rather than re-deriving them."""
    changes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    blobs: Dict[Tuple[str, str], bytes] = field(default_factory=dict)   # (dat_key, path) -> new bytes

    def note(self, msg: str) -> "EditResult":
        self.changes.append(msg)
        return self

    def warn(self, msg: str) -> "EditResult":
        self.warnings.append(msg)
        return self

    @property
    def changed(self) -> bool:
        return bool(self.changes or self.blobs)

    def __bool__(self):
        return self.changed


class EditError(ValueError):
    """A precondition failed.  Raised rather than silently producing data the game cannot load."""


# ══ LAYER 1 — reference primitives ═══════════════════════════════════════════════════════════════════

def objref(ndf, target_idx: int) -> NdfValue:
    """An ObjRef pointing at `target_idx`, class COERCED from the target (see module docstring).

    Thin wrapper over ndfbin.make_objref so callers reach for this instead of hand-building the tuple —
    hand-built ObjRefs are how a stale class gets in."""
    return ndf.make_objref(int(target_idx))


def import_ordinals_are_dense(ndf) -> bool:
    """True when the import ordinals are exactly 0..N-1.  The engine indexes the import table by ordinal
    with no bounds check, so a gap (from a removal) or an over-max ordinal (from a naive append) is an
    out-of-bounds read."""
    ords = sorted(ndf.import_ordinal_paths())
    return ords == list(range(len(ords)))


def transref(ndf, path: str) -> NdfValue:
    """A TransRef pointing at the cross-file object `path` (e.g. '$/Misc/Globals/ChallengePackManager'),
    adding the import if it isn't there yet.

    Refuses to append onto a non-dense import table: `add_import_path` allocates max+1, which is only a
    valid ordinal while the existing ones are contiguous.  Callers hitting this should reindex with
    `ndf.set_import_paths(...)` first (that remaps every existing TransRef by path identity)."""
    if not path or not isinstance(path, str):
        raise EditError("transref needs a non-empty path")
    if not import_ordinals_are_dense(ndf):
        raise EditError(
            "import ordinals are not dense (0..N-1); appending would allocate an out-of-range ordinal. "
            "Reindex with set_import_paths() first")
    ordinal = ndf.add_import_path(path)
    return NdfValue(T.Reference, (TRANS_REF_MARKER, int(ordinal)))


def export_path(ndf, path: str) -> int:
    """Ensure `path` is exported by this NDF, returning its ordinal.

    Exports are how OTHER files reach into this one: mapinfo.cpp names a scenario cluster's subcluster by
    the literal string '$/ClusterMap/MapInstance/LoadStd', and the cluster must export exactly that path or
    the join silently fails (our internal scenario notes)."""
    if not path or not isinstance(path, str):
        raise EditError("export_path needs a non-empty path")
    return ndf.add_export_path(path)


def ensure_string(ndf, s: str) -> NdfValue:
    """A StringRef value for `s`, interning the string if needed."""
    return NdfValue(T.StringRef, ndf.ensure_string(s))


def ensure_path(ndf, p: str) -> NdfValue:
    """A PathRef value for `p`, interning the string if needed.  Same table as StringRef; the distinct type
    is what tells the engine to treat the value as a file path."""
    return NdfValue(T.PathRef, ndf.ensure_string(p))


def set_value(ndf, inst, prop_name: str, value: NdfValue, *, create: bool = False) -> EditResult:
    """Set one property on one instance.  `create` allows adding a property the class doesn't declare yet
    (needed when a cloned record gains a field); default is off so a typo'd property name fails loudly."""
    res = EditResult()
    if not ndf.set_property(inst, prop_name, value, create=create):
        raise EditError(f"property {prop_name!r} not found on this instance's class "
                        f"(pass create=True to add it)")
    res.note(f"set {prop_name}")
    return res


def prop_of(ndf, inst, prop_name: str):
    """The NdfProperty named `prop_name` ON THIS INSTANCE'S OWN CLASS, or None.

    Deliberately does NOT fall back to a whole-table name search.  Property names are far from unique —
    475 of the 741 shipped NDFs reuse at least one name across classes (`FileName`, `NdfTransaction`,
    `SubClusterList`, ...) — while an instance never carries another class's property, in 823,713 shipped
    property values.  So a cross-class fallback cannot produce a right answer, only a plausible wrong one:
    it would write a foreign property index onto the instance.  Lookup IS case-insensitive
    (`prop_by_name_and_class` matches exact then lowercase), which is what keeps `GUID` / `Guid` / `guid`
    working when a build renames it."""
    return ndf.prop_by_name_and_class(prop_name, inst.class_index)


def get_value(ndf, inst, prop_name: str) -> Optional[NdfValue]:
    """The value of `prop_name` on `inst`, or None.  Class-scoped — see prop_of."""
    p = prop_of(ndf, inst, prop_name)
    return inst.get(p.index) if p is not None else None


def clone_instance(ndf, idx: int, *, top: Optional[bool] = None, deep: bool = False) -> int:
    """Clone the instance at `idx`, returning the new index.

    `top` controls top-object registration and defaults to matching the source.  Getting this wrong is not
    cosmetic: a new TMapLoadInfo MUST be a top object to be reachable, while a new registration record must
    NOT be one — a rooted info record hangs the menu (our internal scenario notes)."""
    if not (0 <= idx < len(ndf.instances)):
        raise EditError(f"clone source {idx} out of range")
    return ndf.clone_instance(idx, deep_subobjects=deep, top=top)


def delete_instance(ndf, idx: int) -> EditResult:
    """Delete one instance, remapping every index that shifts.  Warns (rather than failing) when something
    still pointed AT it — the caller usually wants to see the count and decide."""
    res = EditResult()
    removed, dangling = ndf.delete_instances([idx])
    if not removed:
        raise EditError(f"instance {idx} does not exist")
    res.note(f"deleted instance {idx}")
    if dangling:
        res.warn(f"{dangling} ObjRef(s) pointed at the deleted instance and are now dangling")
    return res


def _list_of(ndf, inst, prop_name: str) -> Tuple[NdfValue, list]:
    v = get_value(ndf, inst, prop_name)
    if v is None or v.type_id != T.List:
        raise EditError(f"{prop_name!r} is not a List property on this instance")
    return v, list(v.raw)


def list_append(ndf, inst, prop_name: str, value: NdfValue) -> EditResult:
    """Append to a List property, preserving its type id (rebuilding the value as a bare list would drop
    the List typing and corrupt the write)."""
    v, items = _list_of(ndf, inst, prop_name)
    items.append(value)
    v.raw = items
    return EditResult().note(f"{prop_name}: appended (now {len(items)})")


def list_remove_at(ndf, inst, prop_name: str, pos: int) -> EditResult:
    v, items = _list_of(ndf, inst, prop_name)
    if not (0 <= pos < len(items)):
        raise EditError(f"{prop_name}: index {pos} out of range (0..{len(items) - 1})")
    del items[pos]
    v.raw = items
    return EditResult().note(f"{prop_name}: removed #{pos} (now {len(items)})")


def list_move(ndf, inst, prop_name: str, pos: int, delta: int) -> EditResult:
    """Move one entry within a List property.  Used for menu ordering, where the list ORDER is the order
    the player sees."""
    v, items = _list_of(ndf, inst, prop_name)
    if not (0 <= pos < len(items)):
        raise EditError(f"{prop_name}: index {pos} out of range (0..{len(items) - 1})")
    new = max(0, min(len(items) - 1, pos + delta))
    if new == pos:
        return EditResult()
    items.insert(new, items.pop(pos))
    v.raw = items
    return EditResult().note(f"{prop_name}: moved #{pos} -> #{new}")


# ── integrity check ──────────────────────────────────────────────────────────────────────────────────

def check_refs(ndf, *, label: str = "") -> List[Finding]:
    """Every way a reference in this NDF can be wrong, checked against the deserializer's actual behaviour.

    Run it after any structural edit and before a save.  A clean result means every reference in the file
    resolves the way the engine will read it; it does NOT mean the referenced object is the right one."""
    out: List[Finding] = []
    where0 = label or "ndf"
    n_inst = len(ndf.instances)
    n_str = len(ndf.strings)

    # Import table: ordinals are read as a direct index with no bounds check, so they must be dense.
    imports = ndf.import_ordinal_paths()
    ords = sorted(imports)
    if ords != list(range(len(ords))):
        missing = sorted(set(range(len(ords))) - set(ords)) if ords else []
        out.append(Finding(
            "import-ordinals-not-dense", "error", where0,
            f"import ordinals are {ords[:8]}{'...' if len(ords) > 8 else ''}; expected 0..{len(ords) - 1}"
            + (f" (gaps at {missing[:8]})" if missing else "")))
    n_imports = (max(ords) + 1) if ords else 0

    # Top objects must exist.
    for i in ndf.top_objects:
        if not (0 <= i < n_inst):
            out.append(Finding("top-object-out-of-range", "error", f"{where0} top_objects",
                               f"index {i} but the file has {n_inst} instances"))

    def cls_name(ci):
        return ndf.classes[ci].name if 0 <= ci < len(ndf.classes) else f"cls#{ci}"

    for ii, inst in enumerate(ndf.instances):
        for pv in inst.props:
            prop = ndf.prop_by_index(pv.prop_index)
            pname = prop.name if prop else f"prop#{pv.prop_index}"
            site = f"{where0} #{ii} {cls_name(inst.class_index)}.{pname}"
            # A property belongs to exactly one class, and an instance only ever carries its own class's
            # properties — measured across 823,713 property values in all 741 shipped NDFs, with zero
            # exceptions.  A violation means something resolved a property name to a DIFFERENT class's
            # entry (property names are far from unique: 475 of 741 files reuse at least one name across
            # classes) and wrote its index here.
            if prop is not None and prop.class_index != inst.class_index:
                out.append(Finding(
                    "property-foreign-class", "error", site,
                    f"property {pname!r} belongs to {cls_name(prop.class_index)}, not to this "
                    f"{cls_name(inst.class_index)} instance"))
            for v in _walk(pv.value):
                t = v.type_id
                if t == T.Reference and isinstance(v.raw, tuple):
                    marker, ref = v.raw
                    if marker == TRANS_REF_MARKER:
                        if isinstance(ref, int) and not (0 <= ref < n_imports):
                            out.append(Finding(
                                "transref-out-of-range", "error", site,
                                f"import ordinal {ref} but the import table has {n_imports} entries — "
                                f"the engine indexes it unchecked"))
                        elif isinstance(ref, str) and ndf.transref_is_legacy_bare(ref):
                            out.append(Finding(
                                "transref-legacy-bare-name", "warning", site,
                                f"bare-name TransRef {ref!r} resolves by coincidence only; re-convert on "
                                f"the origin build so it becomes a portable '$/...' path"))
                    elif marker == OBJ_REF_MARKER and isinstance(v.raw[1], tuple):
                        oi, ci = v.raw[1]
                        if oi == -1 or oi == 0xFFFFFFFF:
                            continue                        # explicit NULL, the deserializer's own case
                        if not (0 <= oi < n_inst):
                            out.append(Finding("objref-out-of-range", "error", site,
                                               f"target instance {oi} but the file has {n_inst}"))
                        elif ci != ndf.instances[oi].class_index:
                            out.append(Finding(
                                "objref-class-mismatch", "error", site,
                                f"declares class {cls_name(ci)} but instance {oi} is "
                                f"{cls_name(ndf.instances[oi].class_index)} — the engine would index the "
                                f"wrong class's object table"))
                elif t in (T.StringRef, T.PathRef):
                    if not isinstance(v.raw, int) or not (0 <= v.raw < n_str):
                        out.append(Finding("stringref-out-of-range", "error", site,
                                           f"string index {v.raw!r} but the table has {n_str} entries"))
                elif t == T.LocHash:
                    if not isinstance(v.raw, (bytes, bytearray)) or len(v.raw) != 8:
                        out.append(Finding("lochash-malformed", "error", site,
                                           f"expected 8 bytes, got {v.raw!r}"))
    return out


def _walk(v: NdfValue):
    """Yield `v` and every value nested inside it (List / Map / Pair)."""
    yield v
    t = v.type_id
    if t == T.List:
        for it in v.raw:
            yield from _walk(it)
    elif t == T.Map:
        for k, vv in v.raw:
            yield from _walk(k)
            yield from _walk(vv)
    elif t == T.Pair:
        yield from _walk(v.raw[0])
        yield from _walk(v.raw[1])


# ══ LAYER 2 — text operations (the LocHash model) ════════════════════════════════════════════════════
#
# A LocHash is an OPAQUE 8-byte key, byte-identical to the .dic record key.  It is never text.  There are
# exactly three things an author can want, and conflating them is the classic editor mistake:
#
#   re-text   change what the key says          -> dic.set_entry, in EVERY language
#   re-point  show a different existing string  -> write different key bytes into the property
#   mint      create a new string               -> mint_key + dic.add_entry in every language + re-point
#
# Editing "the current language's .dic" is never right: one key backs all 11 languages, so a single-language
# write leaves the other ten stale.  Display in one language, edit the key.

DEFAULT_LOC_DAT = "loc"
_FALLBACK_FAMILY = "baseunite.dic"      # where a brand-new key goes when the old one resolves nowhere


def _basename(p: str) -> str:
    return p.replace("\\", "/").rsplit("/", 1)[-1].lower()


def loc_index(store, dat_key: str = DEFAULT_LOC_DAT) -> Optional[loc_mod.LocIndex]:
    """Build a LocIndex over EVERY .dic in the store's localization dat.

    All of them, deliberately: LocHash text is spread across baseunite, interface_*, long_description_unite,
    dialog, flash_txt and the per-map dicos, so a partial scan misses most keys.  It is cheap — one archive
    open via read_many.  Returns None when no localization dat is reachable (a nested archive, or no
    project), which callers treat as "fall back to raw hex"."""
    try:
        paths = store.entry_paths(dat_key, ".dic")
        blobs = store.read_many(dat_key, paths) if paths else {}
    except Exception:
        return None
    if not blobs:
        return None
    return loc_mod.LocIndex([(dat_key, p, b) for p, b in blobs.items()])


def loc_langs(idx: loc_mod.LocIndex, key: bytes) -> Dict[str, str]:
    """{language code: text} for `key`, across every .dic it appears in."""
    return {lang: e.string for lang, e in idx.langs_for(bytes(key)).items()}


def loc_display(idx: loc_mod.LocIndex, key: bytes, lang: str) -> Optional[str]:
    """The string to SHOW for `key` in `lang`, falling back through dev/us then any language.  Display is
    single-language on purpose; editing is not."""
    return idx.string_in(bytes(key), lang)


def loc_family_paths(idx: loc_mod.LocIndex, key: Optional[bytes] = None) -> List[Tuple[str, str]]:
    """Every (dat_key, path) .dic sharing the BASENAME of the file(s) `key` lives in, across all languages.

    This is what makes "add the new string everywhere" mean the right thing: a key that lives in
    flash_txt.dic belongs in every language's flash_txt.dic, not in whatever file happened to be open.
    Falls back to the baseunite family when `key` is None or resolves nowhere."""
    names = {_basename(e.path) for e in idx.resolve(bytes(key))} if key else set()
    if names:
        fam = [(dk, p) for (dk, p) in idx.dic_paths() if _basename(p) in names]
        if fam:
            return fam
    return [(dk, p) for (dk, p) in idx.dic_paths() if _basename(p) == _FALLBACK_FAMILY]


def loc_set_text(idx: loc_mod.LocIndex, key: bytes, lang_to_text: Dict[str, str], *,
                 add_missing: bool = True,
                 family: Optional[Sequence[Tuple[str, str]]] = None,
                 staged: Optional[Dict[Tuple[str, str], bytes]] = None) -> Dict[Tuple[str, str], bytes]:
    """Set `key`'s text per language, returning {(dat_key, path): new blob} for the files that changed.

    Unlike LocIndex.set_text (which silently skips any language where the key doesn't exist yet), this ADDS
    the key to languages that are missing it — otherwise a mod that gains a language leaves that language
    permanently blank, and the caller has no way to tell.  Pass add_missing=False for the strict behaviour.

    Pass `staged` (the result of an earlier call) to keep several keys' edits to the SAME file composing
    instead of clobbering one another: without it each call starts from the pristine blob, so writing five
    keys into one .dic leaves only the last one.  The return value is the new accumulator.

    Both underlying .dic writes are surgical: set_entry keeps the record count so no other entry's offset
    moves, add_entry shifts every offset by exactly one record.  Untouched entries stay byte-exact."""
    key = bytes(key)
    out: Dict[Tuple[str, str], bytes] = dict(staged) if staged else {}

    def blob_for(dat_key, path):
        # Prefer a blob we already staged, so several edits to one file compose instead of clobbering.
        return out.get((dat_key, path), idx.blob(dat_key, path))

    existing = idx.langs_for(key)
    for lang, text in lang_to_text.items():
        e = existing.get(lang)
        if e is None:
            continue
        blob = blob_for(e.dat_key, e.path)
        if blob is None or dic_mod.get_entry(blob, key) == text:
            continue
        out[(e.dat_key, e.path)] = dic_mod.set_entry(blob, key, text)

    if add_missing:
        done = set(existing)
        for dat_key, path in (family if family is not None else loc_family_paths(idx, key)):
            lang = loc_mod.lang_from_path(path)
            if lang in done or lang not in lang_to_text:
                continue
            blob = blob_for(dat_key, path)
            if blob is None or key in dic_mod.keys(blob):
                continue
            out[(dat_key, path)] = dic_mod.add_entry(blob, key, lang_to_text[lang])
            done.add(lang)
    return out


def loc_mint(idx: loc_mod.LocIndex, text: str, *,
             family_of: Optional[bytes] = None) -> Tuple[bytes, Dict[Tuple[str, str], bytes], bool]:
    """Create a new localization entry for `text` in every language of its family.

    Returns (key, blobs, existed).  The key is MD5-64 of the text, so an identical string maps to a stable
    key — when that key is already in the tables we return existed=True and no blobs, and the caller should
    simply re-point at it rather than adding a duplicate.

    `family_of` is the key the property currently points at; the new entry lands in the same .dic family
    (see loc_family_paths), which is what puts a new operation title into every language's flash_txt.dic."""
    if not text:
        raise EditError("cannot mint an empty string")
    key = loc_mod.mint_key(text)
    if idx.has_key(key):
        return key, {}, True
    targets = loc_family_paths(idx, family_of)
    if not targets:
        raise EditError("no .dic files found to add the new entry to")
    return key, idx.add_entry(targets, key, text), False


def loc_clone_key(idx: loc_mod.LocIndex, src_key: bytes, seed: str, *,
                  family_of: Optional[bytes] = None,
                  staged: Optional[Dict[Tuple[str, str], bytes]] = None
                  ) -> Tuple[bytes, Dict[Tuple[str, str], bytes]]:
    """Give a COPY its own text: a fresh key carrying the source key's text in every language.

    Returns (new_key, blobs).  Cloning anything that shows text needs this, because the menu tables are
    SHARED — a duplicated operation that keeps pointing at the original's LocHash does not merely display
    the same words, it means editing the copy's briefing rewrites the ORIGINAL's briefing.

    The key is derived from `seed` (an identity such as "map/scenario|LongDescription3"), not from the
    text.  Deriving from the text would be wrong twice over: two copies of one source would collide back
    onto a single shared key, and a copy would stop being idempotent as soon as its words changed.  Seeding
    by identity means re-running the same clone produces the same key."""
    src_key = bytes(src_key)
    new_key = loc_mod.mint_key("cloneof:" + src_key.hex() + "|" + seed)
    per_lang = loc_langs(idx, src_key)
    if not per_lang:
        return new_key, dict(staged) if staged else {}
    family = loc_family_paths(idx, family_of if family_of is not None else src_key)
    blobs = loc_set_text(idx, new_key, per_lang, add_missing=True, family=family, staged=staged)
    return new_key, blobs


def loc_repoint(ndf, inst, prop_name: str, key: bytes) -> EditResult:
    """Aim a property at a different LocHash — i.e. show different text without touching any .dic."""
    key = bytes(key)
    if len(key) != 8:
        raise EditError(f"a LocHash is 8 bytes, got {len(key)}")
    set_value(ndf, inst, prop_name, NdfValue(T.LocHash, key))
    return EditResult().note(f"{prop_name} -> LocHash {key.hex()}")


def loc_write(store, blobs: Dict[Tuple[str, str], bytes]) -> int:
    """Write staged .dic blobs back through the store and mark them dirty.  Returns the file count."""
    for (dat_key, path), blob in blobs.items():
        store.set_raw(dat_key, path, blob)
        store.mark_dirty(dat_key, path)
    return len(blobs)


# ── where-used ───────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Usage:
    """One place a LocHash is referenced.  `source` names the file, `detail` the property or match."""
    source: str
    where: str
    detail: str


def loc_script_literal(key: bytes) -> str:
    """The way a LocHash appears in decompiled mission-script source: the 8 bytes read as a little-endian
    u64 with a long suffix, e.g. Text=1234567890L.  Used to find script usages of a key."""
    return f"{struct.unpack('<Q', bytes(key))[0]}L"


def loc_where_used_ndf(ndf, key: bytes, *, label: str = "ndf") -> List[Usage]:
    """Every property in this NDF whose value is `key`.  Nested values (lists, maps) are included."""
    key = bytes(key)
    out: List[Usage] = []
    for ii, inst in enumerate(ndf.instances):
        cname = (ndf.classes[inst.class_index].name
                 if 0 <= inst.class_index < len(ndf.classes) else f"cls#{inst.class_index}")
        for pv in inst.props:
            prop = ndf.prop_by_index(pv.prop_index)
            pname = prop.name if prop else f"prop#{pv.prop_index}"
            for v in _walk(pv.value):
                if v.type_id == T.LocHash and bytes(v.raw) == key:
                    out.append(Usage(label, f"#{ii} {cname}", pname))
                    break
    return out


@dataclass
class TextEntry:
    """One LocHash a scenario owns, with everything an editor needs to show and change it."""
    key: bytes
    where: List[Usage] = field(default_factory=list)      # what points at it
    langs: Dict[str, str] = field(default_factory=dict)   # language code -> text
    family: List[Tuple[str, str]] = field(default_factory=list)   # the .dic files it belongs in
    world: str = ""                                       # 'menu' (flash_txt) or 'mission' (per-map dico)

    @property
    def key_hex(self) -> str:
        return self.key.hex()

    @property
    def missing_langs(self) -> List[str]:
        """Languages whose .dic in this key's family does NOT carry it — the ones that render blank."""
        have = set(self.langs)
        return sorted({loc_mod.lang_from_path(p) for _dk, p in self.family} - have)

    @property
    def blank_langs(self) -> List[str]:
        """Languages where the key exists but the text is empty — the zero-filled-.dic failure mode."""
        return sorted(lg for lg, s in self.langs.items() if not (s or "").strip())

    def label(self, prefer: str = "us") -> str:
        """The single line to show in a list: the text in the caller's language, else any."""
        return (self.langs.get(prefer) or next((s for s in self.langs.values() if s), "")).replace("\n", " ")


def scenario_text_inventory(idx: loc_mod.LocIndex, *, reg_ndf=None, reg_inst=None,
                            script_source: str = "", mission_dico_keys: Optional[Iterable[bytes]] = None,
                            prefer_lang: str = "us") -> List[TextEntry]:
    """Every piece of text one scenario owns, as LocHash keys rather than per-language strings.

    Three sources, because a scenario's text lives in three places and an editor that shows only one of
    them is the thing we are replacing:
      * the registration record's LocHash properties  -> the menu (name, briefing, intel, victory)
      * the mission script's Text/FoldedText/Message/Texte literals -> objectives, labels, dialogue
      * the keys already present in the scenario's own dico

    Every entry carries its text in EVERY language it has, which languages are missing it, and which are
    present-but-blank — the two ways a shipped mod ends up silently wordless (doc 03 section 4.3).

    Pure: reads the index and the given NDF/source, writes nothing.
    """
    out: Dict[bytes, TextEntry] = {}
    seen_order: Dict[bytes, int] = {}

    def touch(key: bytes, world: str) -> TextEntry:
        key = bytes(key)
        e = out.get(key)
        if e is None:
            e = out[key] = TextEntry(key=key, world=world)
            e.langs = loc_langs(idx, key)
            e.family = loc_family_paths(idx, key)
            seen_order[key] = len(seen_order)
        return e

    if reg_ndf is not None and reg_inst is not None:
        for pv in reg_inst.props:
            if pv.value.type_id != T.LocHash:
                continue
            prop = reg_ndf.prop_by_index(pv.prop_index)
            name = prop.name if prop else "prop#%d" % pv.prop_index
            touch(pv.value.raw, "menu").where.append(Usage("menu entry", "registration", name))

    if script_source:
        import re as _re
        for m in _re.finditer(r"(?:Text|FoldedText|Message|Texte)=(\d+)L", script_source):
            key = struct.pack("<Q", int(m.group(1)))
            e = touch(key, "mission")
            line = script_source.count("\n", 0, m.start()) + 1
            e.where.append(Usage("mission script", "line %d" % line, m.group(0)))

    for key in (mission_dico_keys or ()):
        touch(key, "mission")

    # Menu text first (it is what a player reads before pressing Start), then DOCUMENT ORDER — registration
    # properties as the record declares them, script keys by the line they appear on.
    #
    # Deliberately NOT sorted by the text itself: a key with no words yet would then sort as an empty
    # string and every blank row would clump together, away from the thing it belongs to. Blank keys are
    # ordinary entries that happen to have nothing in them (12 of 24 on a shipped operation), so they have
    # to sit where they occur — both so the editor reads as the whole scenario, and so a key that turns out
    # to be used after all is found next to its neighbours rather than in a pile at one end.
    world_order = {"menu": 0, "mission": 1}
    return sorted(out.values(), key=lambda e: (world_order.get(e.world, 2), seen_order[e.key]))


def loc_where_used_script(source: str, key: bytes, *, label: str = "effetmap.xyz") -> List[Usage]:
    """Every line of decompiled script source that references `key`.  Cheap substring scan — the literal
    form is unambiguous enough that a regex buys nothing."""
    lit = loc_script_literal(key)
    out: List[Usage] = []
    for n, line in enumerate(source.splitlines(), 1):
        if lit in line:
            out.append(Usage(label, f"line {n}", line.strip()[:160]))
    return out


# ══ LAYER 3 — chain / link operations ════════════════════════════════════════════════════════════════
#
# Each of these is ONE named join from the load chain (our internal scenario notes).  They are separate,
# individually tested functions rather than inline string edits inside a bigger routine, because that is
# exactly how the clone bug happened: `_scenario_replacements` rewrote the .scenario reference and forgot
# the Scripting_* folder in 85 of 85 shipped clusters, and no function owned that link, so no test could
# fail.

_CLUSTER_SCENARIO_CLASS = "TScenarioLoader"
_CLUSTER_PYTHON_CLASS = "TClusterAddPythonPath"
_CLUSTER_DICO_CLASS = "TLocalisationDicoResource"
_CLUSTER_TRANSACTION_CLASS = "TNDFTransaction"


def _replace_segment(value: str, old_seg: str, new_seg: str) -> str:
    """Swap one whole path SEGMENT inside a value, case-insensitively, leaving the rest verbatim.

    Segment-wise rather than substring-wise on purpose: replacing `Scripting_challenge` as a substring
    would also corrupt `Scripting_challenge_2`, and the mount prefix (`DataDir:`) must survive."""
    if not value or not old_seg or old_seg.lower() == new_seg.lower():
        return value
    parts = value.replace("/", "\\").split("\\")
    return "\\".join(new_seg if p.lower() == old_seg.lower() else p for p in parts)


def _string_props(ndf, class_name: str, prop_name: str):
    """Yield (instance, NdfValue) for one property across every instance of a class."""
    for _i, inst in ndf.find_instances(class_name):
        p = prop_of(ndf, inst, prop_name)
        if p is None:
            continue
        v = inst.get(p.index)
        if v is not None:
            yield inst, v


def _as_str(ndf, v) -> Optional[str]:
    if v.type_id in (T.StringRef, T.PathRef):
        return ndf.get_string(v.raw)
    if v.type_id == T.WideStr and isinstance(v.raw, str):
        return v.raw
    return None


def _set_str(ndf, v: NdfValue, new: str) -> bool:
    """Point a StringRef/PathRef at `new` (interning it) or set a WideStr in place."""
    if v.type_id in (T.StringRef, T.PathRef):
        v.raw = ndf.ensure_string(new)
        return True
    if v.type_id == T.WideStr:
        v.raw = new
        return True
    return False


def point_cluster_at_scenario(ndf, new_stem: str) -> EditResult:
    """Point a scenario cluster's TScenarioLoader at a different `.scenario` file, by stem."""
    res = EditResult()
    for _inst, v in _string_props(ndf, _CLUSTER_SCENARIO_CLASS, "FileName"):
        cur = _as_str(ndf, v)
        if not cur or not cur.lower().endswith(".scenario"):
            continue
        head, _, _tail = cur.replace("/", "\\").rpartition("\\")
        new = (head + "\\" if head else "") + new_stem + ".scenario"
        if new != cur and _set_str(ndf, v, new):
            res.note("scenario file -> %s" % new)
    if not res.changes:
        res.warn("cluster has no TScenarioLoader.FileName to point")
    return res


def point_cluster_at_script(ndf, old_folder: str, new_folder: str) -> EditResult:
    """Re-point every python path from `old_folder` to `new_folder`.

    THE link the clone path forgot.  TClusterAddPythonPath entries are what put a directory on the
    interpreter's path; an effetmap.xyz anywhere else is never imported, and the game says nothing — it
    just keeps running whatever the still-listed old folder contains."""
    res = EditResult()
    for _inst, v in _string_props(ndf, _CLUSTER_PYTHON_CLASS, "DirectoryList"):
        if v.type_id != T.List:
            continue
        for item in v.raw:
            cur = _as_str(ndf, item)
            if not cur:
                continue
            new = _replace_segment(cur, old_folder, new_folder)
            if new != cur and _set_str(ndf, item, new):
                res.note("python path -> %s" % new)
    if not res.changes:
        res.warn("no python path mentions %r" % old_folder)
    return res


def point_cluster_at_dico(ndf, old_folder: str, new_folder: str) -> EditResult:
    """Re-point every TLocalisationDicoResource whose path runs through `old_folder`.

    This is the in-mission text (Localization.csv -> the per-language .dic).  Shared dicos such as
    VILLE_MULTI.csv carry no folder segment and are correctly left alone."""
    res = EditResult()
    for _inst, v in _string_props(ndf, _CLUSTER_DICO_CLASS, "FileName"):
        cur = _as_str(ndf, v)
        if not cur:
            continue
        new = _replace_segment(cur, old_folder, new_folder)
        if new != cur and _set_str(ndf, v, new):
            res.note("dico -> %s" % new)
    return res


def point_cluster_at_terrain(ndf, new_map_dir: str) -> EditResult:
    """Aim a scenario cluster's ClusterTerrain transaction at a different terrain cluster.

    The geometry caveat is the caller's problem, not this function's: the `.scenario`, `.kdt` and campath
    hold coordinates in the OLD terrain's space, so a re-pointed scenario needs its placements redone."""
    res = EditResult()
    for _i, inst in ndf.find_instances(_CLUSTER_TRANSACTION_CLASS):
        nsp = prop_of(ndf, inst, "NameSpace")
        bnp = prop_of(ndf, inst, "BaseName")
        if nsp is None or bnp is None:
            continue
        ns = inst.get(nsp.index)
        if ns is None or (_as_str(ndf, ns) or "").lower() != "clusterterrain":
            continue
        v = inst.get(bnp.index)
        cur = _as_str(ndf, v) if v is not None else None
        if not cur:
            continue
        # 'Patchable\map\<map dir>\ClusterMap' — swap everything between the 'map' segment and the tail.
        parts = cur.replace("/", "\\").split("\\")
        lowered = [p.lower() for p in parts]
        if "map" not in lowered:
            continue
        i = lowered.index("map")
        new = "\\".join(parts[:i + 1] + new_map_dir.replace("/", "\\").split("\\") + parts[-1:])
        if new != cur and _set_str(ndf, v, new):
            res.note("terrain cluster -> %s" % new)
    if not res.changes:
        res.warn("cluster has no ClusterTerrain transaction to re-point")
    return res


def bind_guid(m_ndf, tmli_inst, g_ndf, info_inst, guid: bytes) -> EditResult:
    """Write the same 16-byte GUID on both sides of the menu join.

    The engine resolves it through a hash map with no diagnostic: a registration record whose GUID is not
    a key in the map-load-info table is skipped silently, so the entry never appears and nothing says why
    (our internal scenario notes)."""
    guid = bytes(guid)
    if len(guid) != 16:
        raise EditError("a GUID is 16 bytes, got %d" % len(guid))
    set_value(m_ndf, tmli_inst, "GUID", NdfValue(T.Guid, guid))
    set_value(g_ndf, info_inst, "GUID", NdfValue(T.Guid, guid))
    return EditResult().note("bound both sides to GUID %s" % guid.hex())


def add_to_pack(g_ndf, kind: str, info_idx: int, pack_idx: Optional[int] = None) -> EditResult:
    """Append a registration record to a menu pack so the populator enumerates it.

    Two shipped invariants are enforced here instead of living in a comment: the record must NOT be a top
    object (the shipped data never roots one, and rooting it hangs the menu), and it must join an EXISTING
    pack (a brand-new pack is never enumerated).  `pack_idx` defaults to the largest pack of that kind."""
    from .scenario_registry import REGISTRY_KINDS
    rk = REGISTRY_KINDS.get(kind)
    if rk is None:
        raise EditError("unknown registration kind %r" % kind)
    if info_idx in g_ndf.top_objects:
        raise EditError("a registration record must not be a top object")
    packs = g_ndf.find_instances(rk.pack_class)
    if not packs:
        raise EditError("no %s exists to add to; a brand-new pack is never enumerated" % rk.pack_class)

    def plist(pinst):
        p = prop_of(g_ndf, pinst, rk.pack_list_prop)
        v = pinst.get(p.index) if p else None
        return v.raw if (v is not None and v.type_id == T.List) else []

    if pack_idx is None:
        pack_idx = max(packs, key=lambda pi: len(plist(pi[1])))[0]
    pinst = g_ndf.instances[pack_idx]
    for item in plist(pinst):
        if (item.type_id == T.Reference and isinstance(item.raw, tuple)
                and isinstance(item.raw[1], tuple) and item.raw[1][0] == info_idx):
            return EditResult().warn("record #%d is already in pack #%d" % (info_idx, pack_idx))
    list_append(g_ndf, pinst, rk.pack_list_prop, objref(g_ndf, info_idx))
    return EditResult().note("appended record #%d to %s #%d" % (info_idx, rk.pack_class, pack_idx))


def place_at_end_of_group(g_ndf, kind: str, pack_idx: int, info_idx: int,
                          prop: Optional[str] = None) -> EditResult:
    """Move a record to the end of ITS OWN group's run in the pack list.

    Menu groups are CONTIGUOUS RUNS of equal group key (CategoryId) in the pack's child list — measured
    true of every shipped pack of all three kinds.  So a record appended at the end of the LIST while
    carrying an existing group's key splits that group into two runs, and the menu renders it detached
    from the group it belongs to.

    This matters most for campaign missions: a new chapter belongs inside its campaign's group, at the end
    of it, not after every other campaign.  A record carrying a brand-new group key is already correct
    where it is — it forms its own run at the end — and this is then a no-op."""
    from .scenario_registry import _pack_refs_and_keys, group_prop_for
    prop = prop or group_prop_for(kind)
    ml, refs, (order, keys) = _pack_refs_and_keys(g_ndf, kind, pack_idx, prop)
    if ml is None or info_idx not in order:
        return EditResult().warn("record #%d is not in pack #%d" % (info_idx, pack_idx))
    pos = order.index(info_idx)
    key = keys[pos]
    others = [i for i, k in enumerate(keys) if k == key and i != pos]
    if not others:
        return EditResult().note("record #%d starts a new group (%r) at the end" % (info_idx, key))
    ref = refs.pop(pos)
    # Re-find the run after the removal, then insert directly after its last member.
    keys2 = keys[:pos] + keys[pos + 1:]
    last = max(i for i, k in enumerate(keys2) if k == key)
    refs.insert(last + 1, ref)
    ml.raw[:] = refs
    return EditResult().note("record #%d moved to the end of group %r (position %d)"
                             % (info_idx, key, last + 1))


def registration_fields(g_ndf, kind: str) -> List[str]:
    """Every property the given registration class actually declares in this NDF."""
    from .scenario_registry import REGISTRY_KINDS
    rk = REGISTRY_KINDS.get(kind)
    if rk is None:
        raise EditError("unknown registration kind %r" % kind)
    cls = g_ndf.class_by_name(rk.info_class)
    if cls is None:
        return []
    return sorted(p.name for p in g_ndf.properties if p.class_index == cls.index)


def set_registration_field(g_ndf, kind: str, info_inst, prop_name: str, value: NdfValue) -> EditResult:
    """Set a field on a registration record, refusing fields that kind does not have.

    The three record classes carry genuinely different fields (our internal scenario notes) — there is
    no LongDescription2 on a campaign chapter, no pop cap on an MP map — so a shared form that writes
    whatever it is handed produces a record the game cannot read back."""
    from .scenario_registry import REGISTRY_KINDS
    rk = REGISTRY_KINDS[kind]
    cls = g_ndf.class_by_name(rk.info_class)
    if cls is None:
        raise EditError("%s is not in this NDF" % rk.info_class)
    if g_ndf.prop_by_name_and_class(prop_name, cls.index) is None:   # class-scoped by construction
        raise EditError("%s has no property %r - check the per-kind field set"
                        % (rk.info_class, prop_name))
    return set_value(g_ndf, info_inst, prop_name, value)

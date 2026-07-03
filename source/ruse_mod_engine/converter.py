"""
Conversion utilities: diff old-style mod .dat files against originals and
produce .rmod patch files.  Used by both mod_manager.py and convert_mod_gui.py.
"""

import base64
import json
import re
import zlib
from pathlib import Path

from . import edata as edata_mod
from . import ndfbin as ndfbin_mod
from . import dic as dic_mod
from . import sdb as sdb_mod
from . import scenario as scenario_mod
from .ndfbin import T


# ── Helpers ───────────────────────────────────────────────────────────────────

def name_to_id(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name.lower()).strip("-")
    return slug or "my-mod"


def sanitize_filename(name: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|]', "", name)
    safe = re.sub(r"\s+", "_", safe.strip())
    return safe or "mod"


# ── Type serialisation ────────────────────────────────────────────────────────

_SCALAR_TYPES = {
    T.Bool:      "Bool",
    T.Int8:      "Int8",
    T.Int16:     "Int16",
    T.UInt16:    "UInt16",
    T.Int32:     "Int32",
    T.UInt32:    "UInt32",
    T.Long:      "Long",
    T.Time64:    "Time64",
    T.Float32:   "Float32",
    T.Float64:   "Float64",
    T.StringRef: "StringRef",
    T.PathRef:   "PathRef",
    T.WideStr:   "WideStr",
}


def val_to_rmod(ndf, val, new_inst_indices=None, orig_ndf=None):
    """Convert NdfValue → (type_name, json_value). Returns None if unsupported.

    new_inst_indices: set of mod-NDF global instance indices that are newly created
    (not present in the OG game). ObjRefs targeting these are stored as
    {"local_id": "inst_N"} so they resolve correctly at apply time regardless of
    raw index differences between the mod file and the OG game.

    orig_ndf: the original (pre-mod) NDF, used to resolve stable keys for OG-range
    ObjRefs. When provided, stable_ref uses the OG key (not the mod key), so refs
    to instances whose ClassNameForDebug was changed by the same rmod still resolve.
    """
    t = val.type_id

    if t == T.Bool:
        return ("Bool", int(val.raw))

    if t in _SCALAR_TYPES and t not in (T.StringRef, T.PathRef, T.WideStr):
        return (_SCALAR_TYPES[t], val.raw)

    if t in (T.StringRef, T.PathRef):
        return (_SCALAR_TYPES[t], str(ndf.resolve_value(val)))

    if t == T.WideStr:
        return ("WideStr", val.raw)

    if t == T.Vector3:
        return ("Vector3", list(val.raw))

    if t == T.Color128:
        return ("Color128", list(val.raw))

    if t == T.Color32:
        return ("Color32", list(val.raw))

    if t == T.TripleInt:
        return ("TripleInt", list(val.raw))

    if t == T.Int2:
        return ("Int2", list(val.raw))

    if t == T.Float2:
        return ("Float2", list(val.raw))

    if t == T.Matrix:
        return ("Matrix", list(val.raw))

    if t == T.Blob:
        return ("Blob", base64.b64encode(val.raw).decode("ascii"))

    if t == T.ZipBlob:
        data = val.raw.get("data", b"")
        flag = val.raw.get("flag", 0)
        return ("ZipBlob", {"flag": flag, "data": base64.b64encode(data).decode("ascii")})

    if t == T.Hash:
        return ("Hash", val.raw.hex())

    if t == T.Pair:
        k_res = val_to_rmod(ndf, val.raw[0], new_inst_indices, orig_ndf)
        v_res = val_to_rmod(ndf, val.raw[1], new_inst_indices, orig_ndf)
        if k_res is None or v_res is None:
            return None
        return ("Pair", [{"type": k_res[0], "value": k_res[1]},
                          {"type": v_res[0], "value": v_res[1]}])

    if t == T.List:
        if not val.raw:
            return ("List<Int32>", [])
        inner = val_to_rmod(ndf, val.raw[0], new_inst_indices, orig_ndf)
        if inner is None:
            return None
        all_items = []
        for item in val.raw:
            r = val_to_rmod(ndf, item, new_inst_indices, orig_ndf)
            if r is None:
                return None
            all_items.append(r)
        # Mixed ObjRef/TransRef lists need per-element type wrappers to be unambiguous
        types_seen = {r[0] for r in all_items}
        if len(types_seen) > 1 and types_seen <= {"ObjRef", "TransRef"}:
            return (f"List<{inner[0]}>",
                    [{"type": r[0], "value": r[1]} for r in all_items])
        return (f"List<{inner[0]}>", [r[1] for r in all_items])

    if t == T.Reference:
        marker, ref = val.raw
        if marker == ndfbin_mod.OBJ_REF_MARKER:
            obj_idx, cls_idx = ref
            # Intra-rmod reference: target is a newly-created instance in this diff.
            # Store as local_id so the applier resolves it via create_map regardless
            # of raw index differences between the mod file and the OG game.
            if new_inst_indices and obj_idx in new_inst_indices:
                return ("ObjRef", {"local_id": f"inst_{obj_idx}", "class": cls_idx})
            # Cross-file stable key: only use stable_ref when the key is unchanged
            # between OG and mod NDF. If the mod renames the instance, the OG key
            # won't be found at apply time (rename patch may already have run), and
            # the mod key won't exist at apply start. Fall back to raw inst index,
            # which is stable for OG-range instances (new creates are always appended).
            if (orig_ndf is not None
                    and 0 <= obj_idx < len(orig_ndf.instances)
                    and 0 <= obj_idx < len(ndf.instances)):
                orig_inst = orig_ndf.instances[obj_idx]
                mod_inst  = ndf.instances[obj_idx]
                for kp in ("ClassNameForDebug", "DescriptorId"):
                    op = orig_ndf.prop_by_name_and_class(kp, orig_inst.class_index)
                    mp = ndf.prop_by_name_and_class(kp, mod_inst.class_index)
                    if op is None or mp is None:
                        continue
                    ov = orig_inst.get(op.index)
                    mv = mod_inst.get(mp.index)
                    if ov is None or mv is None:
                        continue
                    orig_key = str(orig_ndf.resolve_value(ov))
                    mod_key  = str(ndf.resolve_value(mv))
                    if orig_key == mod_key:
                        # Key is stable — safe to use as stable_ref
                        return ("ObjRef", {"stable_ref": kp, "key_val": orig_key, "class": cls_idx})
                    # Key differs (mod renamed this instance) — fall through to inst
                    break
            elif orig_ndf is None and 0 <= obj_idx < len(ndf.instances):
                # No orig available: look up mod key as stable_ref (best-effort)
                target = ndf.instances[obj_idx]
                for kp in ("ClassNameForDebug", "DescriptorId"):
                    p = ndf.prop_by_name_and_class(kp, target.class_index)
                    if p is not None:
                        v = target.get(p.index)
                        if v is not None:
                            return ("ObjRef", {"stable_ref": kp, "key_val": str(ndf.resolve_value(v)), "class": cls_idx})
            # Fall back to raw index (renamed instance or no stable key)
            return ("ObjRef", {"inst": obj_idx, "class": cls_idx})
        if marker == ndfbin_mod.TRANS_REF_MARKER:
            # `ref` is an IMPORT ORDINAL (not a TRAN index) — resolve to the import's
            # full path so it stays portable across NDF files and builds. The applier
            # maps the path back to whatever ordinal the target NDF assigns.
            path = ndf.import_ordinal_paths().get(ref)
            if path is not None:
                return ("TransRef", {"trans": path})
            return ("TransRef", {"trans_ordinal": ref})   # unknown ordinal (rare) — keep raw
        return None

    if t == T.LocHash:
        return ("LocHash", val.raw.hex())

    if t == T.Guid:
        return ("Guid", val.raw.hex())

    if t == T.Map:
        if not val.raw:
            return ("Map<Int32,Int32>", [])
        k0, v0 = val.raw[0]
        ki = val_to_rmod(ndf, k0, new_inst_indices, orig_ndf)
        vi = val_to_rmod(ndf, v0, new_inst_indices, orig_ndf)
        if ki is None or vi is None:
            return None
        items = []
        for k, v in val.raw:
            kr = val_to_rmod(ndf, k, new_inst_indices, orig_ndf)
            vr = val_to_rmod(ndf, v, new_inst_indices, orig_ndf)
            if kr is None or vr is None:
                return None
            items.append([kr[1], vr[1]])
        return (f"Map<{ki[0]},{vi[0]}>", items)

    return None


# ── NDF diff ──────────────────────────────────────────────────────────────────

def _serialize_props(inst, ndf, warn_fn, new_inst_indices=None, orig_ndf=None) -> dict:
    set_props = {}
    for pv in inst.props:
        prop = next((p for p in ndf.properties if p.index == pv.prop_index), None)
        if prop is None:
            continue
        result = val_to_rmod(ndf, pv.value, new_inst_indices, orig_ndf)
        if result is None:
            warn_fn(f"    SKIP unsupported type 0x{pv.value.type_id:02X} "
                    f"on prop '{prop.name}' (new instance)")
            continue
        set_props[prop.name] = {"type": result[0], "value": result[1]}
    return set_props


def _stable_key(inst, ndf):
    """Return (key_name, key_value) for stable matching, or None.

    `_ShortDatabaseName` is checked AFTER ClassNameForDebug/DescriptorId so it only
    ever provides an identity for instances that would otherwise be KEYLESS (it can
    never change matching that already works via the first two). It is the stable
    per-instance name carried by ~1/3 of otherwise-keyless instances (GFX/turret/etc.
    descriptors) — without it, those were matched POSITIONALLY, which drifts across
    the OG->remaster cross-format jump and mis-mapped their indices (issue: migrated
    operation mods crashed because keyless ObjRefs/indices pointed at the wrong
    instance). See docs/design and migrate.match_instances.
    """
    cls_idx = inst.class_index
    # Authoritative keys — behavior unchanged (always used when present).
    for prop_name in ("ClassNameForDebug", "DescriptorId"):
        p = ndf.prop_by_name_and_class(prop_name, cls_idx)
        if p is not None:
            v = inst.get(p.index)
            if v is not None:
                return (prop_name, str(ndf.resolve_value(v)))
    # Name fallbacks for OTHERWISE-KEYLESS instances (GFX/turret/camera-path/etc.).
    # Empty/null values are skipped so null-named placements don't all collide into one key.
    for prop_name in ("_ShortDatabaseName", "Name"):
        p = ndf.prop_by_name_and_class(prop_name, cls_idx)
        if p is not None:
            v = inst.get(p.index)
            if v is not None:
                rv = str(ndf.resolve_value(v))
                if rv and rv != "None":
                    return (prop_name, rv)
    return None


def _diff_instance_pair(orig_inst, mod_inst, orig_ndf, mod_ndf,
                         cls_name, match, warn_fn, new_inst_indices=None):
    """Compare one matched (orig, mod) instance pair and return change dicts."""
    # Build name→value maps (by property name, not index, for cross-file robustness)
    orig_pmap = {}
    for pv in orig_inst.props:
        prop = next((p for p in orig_ndf.properties if p.index == pv.prop_index), None)
        if prop:
            orig_pmap[prop.name] = pv.value

    mod_pmap = {}
    for pv in mod_inst.props:
        prop = next((p for p in mod_ndf.properties if p.index == pv.prop_index), None)
        if prop:
            mod_pmap[prop.name] = pv.value

    changed_set = {}
    for prop_name, cv in mod_pmap.items():
        ov = orig_pmap.get(prop_name)
        if ov is not None and (str(orig_ndf.resolve_value(ov)) ==
                               str(mod_ndf.resolve_value(cv))):
            continue
        result = val_to_rmod(mod_ndf, cv, new_inst_indices, orig_ndf)
        if result is None:
            warn_fn(f"    SKIP unsupported type 0x{cv.type_id:02X} "
                    f"on prop '{prop_name}'")
            continue
        changed_set[prop_name] = {"type": result[0], "value": result[1]}

    removed = [pn for pn in orig_pmap if pn not in mod_pmap]

    changes = []
    if changed_set:
        changes.append({"action": "patch", "table": cls_name,
                        "match": match, "set": changed_set})
    if removed:
        changes.append({"action": "delete_props", "table": cls_name,
                        "match": match, "props": removed})
    return changes


def diff_ndf(orig_ndf, mod_ndf, warn_fn) -> list:
    """Return rmod change dicts for every differing instance.

    Uses a two-phase approach:
    Phase 1 — match all instances and identify which are newly created (not in orig).
    Phase 2 — serialize changes, passing the new-instance index set so that ObjRefs
    pointing to sibling creates are stored as {"local_id": "inst_N"} rather than
    raw mod-file indices (which are meaningless in the OG game file).

    Matching rules (same as before):
    - Instances with a stable key (ClassNameForDebug/DescriptorId) are matched by key.
    - Duplicate keys / orphans are paired positionally or become creates.
    - Unkeyed instances are matched positionally within the class.
    """
    # Group instances per class (class-name → [(global_idx, inst)])
    def _class_groups(ndf):
        groups = {}
        for gi, inst in enumerate(ndf.instances):
            cls = ndf.classes[inst.class_index] if inst.class_index < len(ndf.classes) else None
            if cls is None:
                continue
            groups.setdefault(cls.name, []).append((gi, inst))
        return groups

    orig_groups = _class_groups(orig_ndf)
    mod_groups  = _class_groups(mod_ndf)
    _orig_top_objects: set = set(orig_ndf.top_objects) if orig_ndf else set()
    _mod_top_objects: set  = set(mod_ndf.top_objects) - _orig_top_objects

    # ── Phase 1: match instances, collect assignments, identify creates ────────
    # Each entry: (orig_gi_or_None, orig_inst_or_None, mod_gi, mod_inst, cls_name, match_or_None)
    assignments = []
    new_inst_indices: set = set()  # mod-NDF global indices of truly new instances

    for cls_name, mod_insts in sorted(mod_groups.items()):
        orig_insts = orig_groups.get(cls_name, [])

        orig_by_key = {}
        orig_no_key = []
        for gi, inst in orig_insts:
            key = _stable_key(inst, orig_ndf)
            if key is not None:
                orig_by_key[key] = (gi, inst)
            else:
                orig_no_key.append((gi, inst))

        orig_keys_used = set()
        mod_orphans    = []
        orig_no_key_i  = 0

        for mod_gi, mod_inst in mod_insts:
            key = _stable_key(mod_inst, mod_ndf)
            if key is not None:
                orig_entry = orig_by_key.get(key)
                if orig_entry is not None and key not in orig_keys_used:
                    orig_keys_used.add(key)
                    orig_gi, orig_inst = orig_entry
                    assignments.append((orig_gi, orig_inst, mod_gi, mod_inst,
                                        cls_name, {key[0]: key[1]}))
                else:
                    mod_orphans.append((mod_gi, mod_inst))
            else:
                if orig_no_key_i < len(orig_no_key):
                    orig_gi, orig_inst = orig_no_key[orig_no_key_i]
                    orig_no_key_i += 1
                    assignments.append((orig_gi, orig_inst, mod_gi, mod_inst,
                                        cls_name, {"_index": str(orig_gi)}))
                else:
                    mod_orphans.append((mod_gi, mod_inst))

        orig_orphans_keyed   = [(gi, inst) for gi, inst in orig_insts
                                if _stable_key(inst, orig_ndf) is not None
                                and _stable_key(inst, orig_ndf) not in orig_keys_used]
        orig_orphans_unkeyed = orig_no_key[orig_no_key_i:]
        orig_orphans = sorted(orig_orphans_keyed + orig_orphans_unkeyed, key=lambda x: x[0])

        for (orig_gi, orig_inst), (mod_gi, mod_inst) in zip(orig_orphans, mod_orphans):
            assignments.append((orig_gi, orig_inst, mod_gi, mod_inst,
                                 cls_name, {"_index": str(orig_gi)}))

        for mod_gi, mod_inst in mod_orphans[len(orig_orphans):]:
            assignments.append((None, None, mod_gi, mod_inst, cls_name, None))
            new_inst_indices.add(mod_gi)

        # Instances in orig with no mod counterpart → delete them
        for orig_gi, orig_inst in orig_orphans[len(mod_orphans):]:
            key = _stable_key(orig_inst, orig_ndf)
            match_dict = {key[0]: key[1]} if key else {"_index": str(orig_gi)}
            assignments.append((orig_gi, orig_inst, None, None, cls_name, match_dict))

    # Classes present in ORIG but ENTIRELY ABSENT from MOD (whole class removed
    # between builds, e.g. TClusterLoadDataPack compat-4 -> public) are never visited
    # by the mod_groups loop, so their instances would be silently dropped from the
    # diff — leaving the instance remap short and shifting every later instance wrong.
    for cls_name, orig_insts in orig_groups.items():
        if cls_name in mod_groups:
            continue
        for orig_gi, orig_inst in orig_insts:
            key = _stable_key(orig_inst, orig_ndf)
            match_dict = {key[0]: key[1]} if key else {"_index": str(orig_gi)}
            assignments.append((orig_gi, orig_inst, None, None, cls_name, match_dict))

    # ── Phase 2: generate changes with create-index awareness ─────────────────
    # Separate deletes from patches so we can sort unkeyed deletes in descending
    # index order (avoids cascading index shifts when multiple instances are removed).
    patch_changes  = []
    delete_changes = []  # [(orig_gi, change_dict), ...]

    for orig_gi, orig_inst, mod_gi, mod_inst, cls_name, match in assignments:
        if orig_inst is None:
            # New instance in mod — create
            set_props = _serialize_props(mod_inst, mod_ndf, warn_fn, new_inst_indices, orig_ndf)
            if set_props:
                change_dict = {
                    "action":   "create",
                    "table":    cls_name,
                    "local_id": f"inst_{mod_gi}",
                    "set":      set_props,
                }
                if mod_gi in _mod_top_objects:
                    change_dict["top_object"] = True
                patch_changes.append(change_dict)
        elif mod_inst is None:
            # Instance removed in mod — delete
            delete_changes.append((orig_gi, {"action": "delete", "table": cls_name, "match": match}))
        else:
            patch_changes.extend(_diff_instance_pair(
                orig_inst, mod_inst, orig_ndf, mod_ndf, cls_name, match, warn_fn,
                new_inst_indices))

    # Deletes sorted highest-index first so earlier removals don't shift later targets
    delete_changes.sort(key=lambda x: x[0], reverse=True)
    return patch_changes + [dc for _, dc in delete_changes]


# ── DAT pair conversion ───────────────────────────────────────────────────────

def _impr_expr_differ(orig_ndf, mod_ndf) -> bool:
    """True if IMPR or EXPR sections differ semantically (resolved import/export PATHS).

    The import/export tables are trees of path components; two files' imports differ
    iff their resolved path SETS differ (order/ordinal reshuffles don't count)."""
    ia, ir, ea, er = _diff_import_export(orig_ndf, mod_ndf)
    return bool(ia or ir or ea or er)


def _ndf_semantic_equal(a, b) -> bool:
    """True if two NDFs are equal in the ways an rmod can express: same import/export
    PATH sets, same instance count, and no residual instance diff."""
    if set(ndfbin_mod.ref_ordinal_paths(ndfbin_mod.parse_ref_tree(a.import_list), a.trans).values()) \
       != set(ndfbin_mod.ref_ordinal_paths(ndfbin_mod.parse_ref_tree(b.import_list), b.trans).values()):
        return False
    if set(ndfbin_mod.ref_ordinal_paths(ndfbin_mod.parse_ref_tree(a.export_list), a.trans).values()) \
       != set(ndfbin_mod.ref_ordinal_paths(ndfbin_mod.parse_ref_tree(b.export_list), b.trans).values()):
        return False
    if len(a.instances) != len(b.instances):
        return False
    return len(diff_ndf(a, b, lambda *_: None)) == 0


def _surgical_ndf_roundtrips(orig_bytes, mod_ndf, pg_dict, warn_fn) -> bool:
    """Replay the surgical patch group onto a fresh copy of orig and confirm it
    reproduces `mod_ndf` semantically. This is the lossless-or-raw guarantee: if the
    surgical form can't perfectly recreate the modded NDF, the caller ships raw."""
    try:
        from . import mod_format, applier
        pg = mod_format._parse_patch_group(pg_dict, 0)
        ndf = ndfbin_mod.read(orig_bytes)
        result = applier.ApplyResult(mod_name="surgical-selfcheck")
        applier._apply_ref_edits(ndf, pg, result)
        applier._apply_changes_to_ndf(pg.changes, ndf, result)
        return _ndf_semantic_equal(ndf, mod_ndf)
    except Exception as e:  # noqa: BLE001
        warn_fn(f"  surgical self-check errored ({e})")
        return False


def _diff_import_export(orig_ndf, mod_ndf):
    """Return (import_add, import_remove, export_add, export_remove) as sorted path lists.

    Imports/exports are compared by their resolved full paths ('$/IA/Cluster/PackMesh_All'),
    which are build-independent — so these edits migrate across builds unchanged."""
    def _paths(ndf, lst):
        try:
            return set(ndfbin_mod.ref_ordinal_paths(ndfbin_mod.parse_ref_tree(lst), ndf.trans).values())
        except Exception:
            return set()
    o_imp = _paths(orig_ndf, orig_ndf.import_list); m_imp = _paths(mod_ndf, mod_ndf.import_list)
    o_exp = _paths(orig_ndf, orig_ndf.export_list); m_exp = _paths(mod_ndf, mod_ndf.export_list)
    return (sorted(m_imp - o_imp), sorted(o_imp - m_imp),
            sorted(m_exp - o_exp), sorted(o_exp - m_exp))


def _diff_dic(orig_bytes, mod_bytes):
    """Diff two .dic (TRA) blobs into surgical loc entries [{key, value[, add]}].

    Returns the entry list (changed + added keys only), or None to signal the caller to fall back
    to a raw file patch — used when the blob isn't TRA or a key was REMOVED (rare; can't be expressed
    as a loc entry, so we don't want to silently drop it)."""
    try:
        orig = {k: v for k, v in dic_mod.read(orig_bytes)}
        mod = {k: v for k, v in dic_mod.read(mod_bytes)}
    except Exception:
        return None
    if set(orig) - set(mod):          # a key disappeared — fall back to raw to be safe
        return None
    entries = []
    for k, v in mod.items():
        if k not in orig:
            entries.append({"key": k.hex(), "value": v, "add": True})
        elif orig[k] != v:
            entries.append({"key": k.hex(), "value": v})
    return entries


def _diff_sdb(orig_win, mod_win):
    """Diff two mapinfo.win blobs → (R, [layer dicts]) or None to fall back to a raw file patch.

    Returns None unless ONLY the SDB buffer (buffer4) changed — if the road graph or any other buffer
    differs we don't touch it surgically (that's not RE'd), so the whole file ships raw (lossless).
    For each layer bit that differs, ships the mod's full-grid bitmask (one bit per cell)."""
    po = sdb_mod.split_mapinfo(orig_win)
    pm = sdb_mod.split_mapinfo(mod_win)
    if not po or not pm:
        return None
    oh, ob, ot = po
    mh, mb, mt = pm
    # Buffers 0,1,2 + trailing must be byte-identical (only buffer4/SDB may differ); the header's
    # md5 (bytes 8:24) is EXPECTED to differ when the SDB changes, so compare the header without it.
    if (oh[:8] + oh[24:]) != (mh[:8] + mh[24:]) or ot != mt \
            or ob[0] != mb[0] or ob[1] != mb[1] or ob[2] != mb[2]:
        return None
    if ob[3] == mb[3] or not sdb_mod.is_sdb(ob[3]) or not sdb_mod.is_sdb(mb[3]):
        return None
    # Only the two RUNTIME-meaningful layers are worth shipping (confirmed: the game queries
    # only 0x08 forest/conceal and 0x04 blocked/clear-path; the other bits are never read). Diffing
    # those two via the unified codec + numpy is ~40x faster than the old all-7-layers decode_grid path.
    try:
        import numpy as np
        og = sdb_mod.to_grid(sdb_mod.parse(ob[3]))
        mg = sdb_mod.to_grid(sdb_mod.parse(mb[3]))
        if og[1] != mg[1]:
            return None
        R = og[1]
        ogrid = np.frombuffer(bytes(og[0]), np.uint8)
        mgrid = np.frombuffer(bytes(mg[0]), np.uint8)
        layers = []
        for bit in (sdb_mod.FOREST_BIT, sdb_mod.BLOCKED_BIT):     # 0x08, 0x04
            mb_bits = (mgrid & bit) > 0
            if not np.array_equal(mb_bits, (ogrid & bit) > 0):
                mm = np.packbits(mb_bits.astype(np.uint8), bitorder="little").tobytes()
                layers.append({"bit": int(bit),
                               "mask": base64.b64encode(zlib.compress(mm, 9)).decode("ascii")})
        return (R, layers) if layers else None
    except Exception:                                            # numpy missing → legacy diff
        og = sdb_mod.decode_grid(ob[3]); mg = sdb_mod.decode_grid(mb[3])
        if og["R"] != mg["R"]:
            return None
        R, ogrid, mgrid = mg["R"], og["grid"], mg["grid"]
        layers = []
        for bit in (sdb_mod.FOREST_BIT, sdb_mod.BLOCKED_BIT):
            mm = sdb_mod.pack_layer_mask(mgrid, R, bit)
            if mm != sdb_mod.pack_layer_mask(ogrid, R, bit):
                layers.append({"bit": bit, "mask": base64.b64encode(zlib.compress(mm, 9)).decode("ascii")})
        return (R, layers) if layers else None


def _diff_scenario(orig_scn, mod_scn, warn_fn):
    """Diff two .scenario blobs → a list of NDF change dicts for the EMBEDDED placement NDF, or None to
    fall back to a raw file patch.

    A .scenario = binary zone geometry + an embedded placement NDF (depots / HQ spawns / labels / zone
    objects).  We diff surgically only when the zone geometry + header are byte-identical and only the
    embedded NDF differs (zone geometry isn't semantically diffable).  Returns None when parsing fails,
    the geometry changed, the IMPR/EXPR sections differ, or there's no semantic NDF change."""
    try:
        so = scenario_mod.read(orig_scn)
        sm = scenario_mod.read(mod_scn)
    except Exception as e:
        warn_fn(f"  scenario parse failed ({e}) — raw file patch")
        return None
    # Compare the meaningful header fields only — `unk_hdr` is the stored MD5, which legitimately
    # changes whenever the content (embedded NDF) does, so it must not count as a geometry change.
    def _hdr_sig(h):
        return (h.get("unk0a"), h.get("unk1"), h.get("unk2"))
    if so["zones"] != sm["zones"] or _hdr_sig(so["header"]) != _hdr_sig(sm["header"]):
        return None                                  # zone geometry/header changed — not diffable
    if so["ndf_data"] == sm["ndf_data"]:
        return None                                  # embedded NDF unchanged (shouldn't happen)
    try:
        ondf = ndfbin_mod.read(so["ndf_data"])
        mndf = ndfbin_mod.read(sm["ndf_data"])
    except Exception as e:
        warn_fn(f"  scenario NDF parse failed ({e}) — raw file patch")
        return None
    if _impr_expr_differ(ondf, mndf):
        return None                                  # IMPR/EXPR differ — ship raw (lossless)
    changes = diff_ndf(ondf, mndf, warn_fn)
    return changes or None


def convert_dat_pair(mod_dat: Path, orig_dat: Path, rmod_dat_path: str,
                     log_fn, warn_fn):
    """Compare one modded .dat against its original.
    Returns (patch_groups, file_group_or_None, loc_groups, sdb_groups, scenario_groups).

    Every entry that differs from OG is captured as surgically as possible, and nothing that changed is
    ever silently dropped:
    - NDF files (.ndfbin/.gladndfbin/.truendfbin) with semantic changes → patch group (NDF diff)
    - .dic localization dictionaries → loc group (changed entries only)
    - mapinfo.win where only the AI-terrain SDB changed → sdb group (changed layers only)
    - .scenario where only the embedded placement NDF changed → scenario group (NDF diff)
    - anything else that changed, or any of the above whose surgical diff is empty/unsafe → raw file patch
    """
    mod_edat  = edata_mod.open_dat(str(mod_dat))
    orig_edat = edata_mod.open_dat(str(orig_dat))

    patch_groups     = []
    raw_file_patches = []
    loc_groups       = []  # surgical .dic localization edits
    sdb_groups       = []  # per-layer SDB (mapinfo.win buffer4) edits
    scenario_groups  = []  # surgical embedded-NDF edits inside a .scenario
    changed_count    = 0  # entries that differ from OG (for summary logging)

    for entry_path in mod_edat.list():
        mod_bytes  = mod_edat.get(entry_path)
        orig_bytes = orig_edat.get(entry_path)
        if orig_bytes is None:
            orig_bytes = orig_edat.get(entry_path.replace("\\", "/"))
        if orig_bytes is None:
            orig_bytes = orig_edat.get(entry_path.replace("/", "\\"))

        if orig_bytes is not None and orig_bytes == mod_bytes:
            continue

        changed_count += 1
        entry_fwd   = entry_path.replace("\\", "/")
        entry_lower = entry_fwd.lower()
        is_ndf      = (entry_lower.endswith(".ndfbin") or
                       entry_lower.endswith(".gladndfbin") or
                       entry_lower.endswith(".truendfbin"))

        if is_ndf and orig_bytes is not None:
            log_fn(f"  Diffing NDF: {entry_fwd.split('/')[-1]}")
            try:
                orig_ndf = ndfbin_mod.read(orig_bytes)
                mod_ndf  = ndfbin_mod.read(mod_bytes)
                changes = diff_ndf(orig_ndf, mod_ndf, warn_fn)
                imp_add, imp_rem, exp_add, exp_rem = _diff_import_export(orig_ndf, mod_ndf)
                pg = {"dat": rmod_dat_path, "ndf": entry_fwd, "changes": changes}
                if imp_add: pg["import_add"] = imp_add
                if imp_rem: pg["import_remove"] = imp_rem
                if exp_add: pg["export_add"] = exp_add
                if exp_rem: pg["export_remove"] = exp_rem
                has_edits = bool(changes or imp_add or imp_rem or exp_add or exp_rem)
                # Verify the surgical patch is LOSSLESS: replay it onto orig and compare to
                # mod. Anything the diff can't capture (e.g. a TRAN-only change, an ObjRef we
                # don't model) → ship raw so nothing is silently lost.
                if has_edits and _surgical_ndf_roundtrips(orig_bytes, mod_ndf, pg, warn_fn):
                    log_fn(f"    {len(changes)} change(s), +imp {len(imp_add)}/-{len(imp_rem)} "
                           f"+exp {len(exp_add)}/-{len(exp_rem)}")
                    patch_groups.append(pg)
                else:
                    reason = "no semantic diff" if not has_edits else "surgical diff not lossless"
                    warn_fn(f"  NDF {reason} — using raw file patch: {entry_fwd.split('/')[-1]}")
                    raw_file_patches.append({
                        "path": entry_fwd,
                        "data": base64.b64encode(mod_bytes).decode("ascii"),
                    })
            except Exception as e:
                warn_fn(f"  NDF parse failed ({e}) — adding as raw file patch")
                raw_file_patches.append({
                    "path": entry_fwd,
                    "data": base64.b64encode(mod_bytes).decode("ascii"),
                })
        elif entry_lower.endswith(".dic") and orig_bytes is not None:
            # Localization dictionary — emit a surgical per-entry loc patch instead of a bloated
            # full-file replacement (and so multiple name-mods compose).
            entries = _diff_dic(orig_bytes, mod_bytes)
            if entries:
                log_fn(f"  Loc .dic: {entry_fwd.split('/')[-1]} — {len(entries)} entry change(s)")
                loc_groups.append({"dat": rmod_dat_path, "dic": entry_fwd, "entries": entries})
            else:
                # not TRA, a key was removed, or no semantic change → raw fallback (lossless)
                log_fn(f"  Changed (.dic, raw fallback): {entry_fwd.split('/')[-1]}")
                raw_file_patches.append({
                    "path": entry_fwd,
                    "data": base64.b64encode(mod_bytes).decode("ascii"),
                })
        elif entry_lower.endswith("mapinfo.win") and orig_bytes is not None \
                and (_sdb := _diff_sdb(orig_bytes, mod_bytes)) is not None:
            # Only the AI-terrain SDB changed → ship the changed LAYERS (composable), not the whole win.
            _sdb_R, _sdb_layers = _sdb
            log_fn(f"  SDB layers: {entry_fwd.split('/')[-1]} — {len(_sdb_layers)} layer(s)")
            sdb_groups.append({"dat": rmod_dat_path, "win": entry_fwd,
                               "grid_size": _sdb_R, "layers": _sdb_layers})
        elif entry_lower.endswith(".scenario") and orig_bytes is not None \
                and (_scn := _diff_scenario(orig_bytes, mod_bytes, warn_fn)) is not None:
            # Only the embedded placement NDF changed (zone geometry byte-identical) → ship the NDF
            # diff so scenario placement edits stay small and compose with other mods' edits.
            log_fn(f"  Scenario NDF: {entry_fwd.split('/')[-1]} — {len(_scn)} change(s)")
            scenario_groups.append({"dat": rmod_dat_path, "scenario": entry_fwd, "changes": _scn})
        else:
            label = "New file" if orig_bytes is None else "Changed (non-NDF)"
            log_fn(f"  {label}: {entry_fwd.split('/')[-1]}")
            raw_file_patches.append({
                "path": entry_fwd,
                "data": base64.b64encode(mod_bytes).decode("ascii"),
            })

    if changed_count == 0:
        log_fn(f"  (all {len(mod_edat.list())} entries identical to original — no changes)")

    file_group = ({"dat": rmod_dat_path, "files": raw_file_patches}
                  if raw_file_patches else None)
    return patch_groups, file_group, loc_groups, sdb_groups, scenario_groups


# ── Folder scan ───────────────────────────────────────────────────────────────

def scan_mod_folder(mod_folder: str, game_data_dir: str, default_sub: str = None) -> list:
    """Find .dat files in mod_folder and match them to their originals.
    Returns list of (mod_dat_path, orig_dat_path, rmod_dat_rel_str).

    rmod dat paths are GAME-ROOT-relative, e.g. Data/PC/190852/<dat> (core) or Maps/PC/<dat> (terrain).

    ``game_data_dir`` may be either a GAME-ROOT (contains Data/ — and, for a clean backup, Maps/) or a
    Data-level dir (contains PC/) — both are accepted, so the Mod Editor (which passes a game-root-like
    reference) and the Convert tab (which passes a Data-level reference) share this code.  Likewise the
    mod folder's dats may be laid out as Data/PC/<sub>/<dat> (new Mod Editor projects), PC/<sub>/<dat>
    (typical distributed mods), or a loose <sub>/<dat> / <dat> (legacy) — all are normalized to the
    game's Data-relative path and matched against the originals.

    Maps/PC terrain dats (sibling of Data/) are also matched: their rmod paths are already game-root-
    relative (Maps/PC/<dat>) and the original is found under the game root.

    default_sub: fallback version sub-folder for a loose <dat> that doesn't carry one."""
    mod_root = Path(mod_folder)
    base = Path(game_data_dir)
    # Accept either a game-ROOT (contains Data/ - and, for a backup, Maps/) or a Data-level dir.
    if (base / "Data").is_dir():
        data_dir, root_dir = base / "Data", base
    else:
        data_dir, root_dir = base, base.parent
    results = []
    for mod_dat in sorted(mod_root.rglob("*.dat")):
        relposix = mod_dat.relative_to(mod_root).as_posix()
        low = relposix.lower()
        if low.startswith("maps/"):
            # Terrain dats live in the game's Maps/ folder; rmod paths are game-root-relative, so use
            # the path verbatim and match the original under the game root.
            orig = root_dir / relposix
            if orig.exists():
                results.append((mod_dat, orig, relposix))
            continue
        # Reduce the mod-relative path to one relative to the game's Data/ dir.
        sub = relposix
        if low.startswith("data/"):
            sub = relposix.split("/", 1)[1]             # Data/PC/190852/X.dat -> PC/190852/X.dat
        # Build candidate (original path, rmod_rel) pairs; first that exists on disk wins.  rmod dat
        # paths are GAME-ROOT-relative (Data/PC/<sub>/<dat>), so the Data/ prefix is kept/added.
        if sub.lower().startswith("pc/"):
            cands = [(data_dir / sub, "Data/" + sub)]                 # Data/PC/<sub>/X.dat
        else:
            cands = [(data_dir / "PC" / sub, f"Data/PC/{sub}")]       # loose <sub>/X.dat or bare X.dat
            if default_sub:
                cands.append((data_dir / "PC" / default_sub / sub, f"Data/PC/{default_sub}/{sub}"))
        orig = rmod_rel = None
        for op, rr in cands:
            if op.exists():
                orig, rmod_rel = op, rr
                break
        if orig is None:
            continue
        results.append((mod_dat, orig, rmod_rel))
    return results


# ── Full conversion runner ────────────────────────────────────────────────────

NDF_FILE_EXTS = (".gladndfbin", ".ndfbin", ".truendfbin")


def surgicalize_ndf_file_patches(mod, clean_game_root, log_fn=lambda *_: None,
                                 warn_fn=lambda *_: None) -> dict:
    """Rewrite whole-file NDF replacements in `mod.file_patches` into SURGICAL patch
    groups by diffing each against the clean vanilla NDF (the update/repair-rmod move:
    whole-file → surgical so the migrator can carry it across builds).

    `clean_game_root` holds Data/PC/<ver>/<dat> for the build the rmod targets (a
    backup or clean snapshot). Mutates `mod`: each NDF blob that diffs LOSSLESSLY moves
    from file_patches into mod.patches; blobs whose surgical form can't reproduce the
    original are left raw and flagged. Returns a report."""
    from . import mod_format as _mf
    from .applier import resolve_dat_for_version
    report = {"surgicalized": 0, "kept_raw": 0, "review": []}
    root = Path(clean_game_root)

    _dat_cache = {}
    def _open_clean(dat_rel):
        if dat_rel not in _dat_cache:
            cand = [root / resolve_dat_for_version(dat_rel, "public").replace("/", "\\"),
                    root / dat_rel.replace("/", "\\"),
                    root / "Data" / dat_rel.replace("/", "\\")]
            found = next((p for p in cand if p.is_file()), None)
            _dat_cache[dat_rel] = edata_mod.open_dat(str(found)) if found else None
        return _dat_cache[dat_rel]

    for fg in list(mod.file_patches):
        remaining = []
        for fp in fg.files:
            if not fp.path.lower().endswith(NDF_FILE_EXTS):
                remaining.append(fp); continue
            dat = _open_clean(fg.dat)
            orig_bytes = None
            if dat is not None:
                orig_bytes = dat.get(fp.path) or dat.get(fp.path.replace("/", "\\"))
            if orig_bytes is None:
                warn_fn(f"  no clean {fp.path} under {clean_game_root} — kept raw")
                remaining.append(fp); report["kept_raw"] += 1; continue
            try:
                orig_ndf = ndfbin_mod.read(orig_bytes)
                mod_ndf  = ndfbin_mod.read(fp.data)
            except Exception as e:  # noqa: BLE001
                warn_fn(f"  parse failed {fp.path}: {e} — kept raw")
                remaining.append(fp); report["kept_raw"] += 1; continue
            changes = diff_ndf(orig_ndf, mod_ndf, warn_fn)
            ia, ir, ea, er = _diff_import_export(orig_ndf, mod_ndf)
            pgd = {"dat": fg.dat, "ndf": fp.path, "changes": changes}
            if ia: pgd["import_add"] = ia
            if ir: pgd["import_remove"] = ir
            if ea: pgd["export_add"] = ea
            if er: pgd["export_remove"] = er
            if _surgical_ndf_roundtrips(orig_bytes, mod_ndf, pgd, warn_fn):
                mod.patches.append(_mf._parse_patch_group(pgd, len(mod.patches)))
                report["surgicalized"] += 1
                log_fn(f"  surgicalized {fp.path.split('/')[-1]}: {len(changes)} change(s), "
                       f"+{len(ia)} import(s)")
            else:
                remaining.append(fp); report["kept_raw"] += 1
                report["review"].append(f"{fp.path}: surgical form not lossless — kept raw")
                warn_fn(f"  {fp.path}: not lossless — kept raw (needs RE)")
        fg.files = remaining
    mod.file_patches = [fg for fg in mod.file_patches if fg.files]
    return report


def run_conversion(mod_folder, game_data_dir, output_rmod,
                   name, mod_id, version, author, description,
                   log_fn, warn_fn, default_sub=None, game_version=None) -> bool:
    pairs = scan_mod_folder(mod_folder, game_data_dir, default_sub)
    if not pairs:
        warn_fn("No matching .dat files found between mod folder and game data.")
        return False

    all_patch_groups    = []
    all_file_patches    = []
    all_loc_patches     = []
    all_sdb_patches     = []
    all_scenario_patches = []

    for mod_dat, orig_dat, rmod_dat_path in pairs:
        log_fn(f"\n── {rmod_dat_path} ──")
        patch_groups, file_group, loc_groups, sdb_groups, scenario_groups = convert_dat_pair(
            mod_dat, orig_dat, rmod_dat_path, log_fn, warn_fn)
        all_patch_groups.extend(patch_groups)
        if file_group:
            all_file_patches.append(file_group)
        all_loc_patches.extend(loc_groups)
        all_sdb_patches.extend(sdb_groups)
        all_scenario_patches.extend(scenario_groups)

    total_ndf = sum(len(pg["changes"]) for pg in all_patch_groups)
    total_raw = sum(len(fg["files"])   for fg in all_file_patches)
    total_loc = sum(len(lg["entries"]) for lg in all_loc_patches)
    total_sdb = sum(len(sg["layers"])  for sg in all_sdb_patches)
    total_scn = sum(len(sg["changes"]) for sg in all_scenario_patches)
    log_fn(f"\nTotal: {total_ndf} NDF change(s), {total_scn} scenario NDF change(s), "
           f"{total_loc} loc entry change(s), {total_sdb} SDB layer(s), {total_raw} raw file patch(es)")

    rmod_obj: dict = {
        "$schema": "ruse-mod/v1",
        "id":      mod_id,
        "name":    name,
        "version": version,
        "patches": all_patch_groups,
    }
    # Stamp the GAME BUILD ID this rmod targets. The Mod Manager keys on this field
    # (it must equal the installed build id, e.g. "24003166") to recognise a mod as
    # built for the current game; without it every freshly-converted rmod reads as
    # "untagged" and the manager keeps prompting to update its version. Optional so
    # callers that don't know the build (older tests) still produce a valid rmod.
    if game_version:
        rmod_obj["game_version"] = str(game_version)
    if author:
        rmod_obj["author"] = author
    if description:
        rmod_obj["description"] = description
    if all_loc_patches:
        rmod_obj["loc_patches"] = all_loc_patches
    if all_sdb_patches:
        rmod_obj["sdb_patches"] = all_sdb_patches
    if all_scenario_patches:
        rmod_obj["scenario_patches"] = all_scenario_patches
    if all_file_patches:
        rmod_obj["file_patches"] = all_file_patches

    out = Path(output_rmod)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rmod_obj, f, indent=2, ensure_ascii=False)
    log_fn(f"Wrote: {output_rmod}")

    # Readiness check — surface the issues a migration CANNOT auto-fix (wrong/incomplete scenario,
    # unbindable label position, RCE, camp misconfig). See docs/design/rmod-update-regimen.md. Never
    # let a validator hiccup fail the conversion itself.
    try:
        from . import rmod_validate
        rep = rmod_validate.validate(rmod_obj, target_build=game_version)
        if rep.errors:
            warn_fn(f"  ⚠ {len(rep.errors)} readiness ERROR(s) a migration can't fix — hand edit needed:")
            for finding in rep.errors:
                warn_fn(f"      [{finding.category}] {finding.message}")
                if finding.fix:
                    warn_fn(f"          fix: {finding.fix}")
        elif rep.is_operation:
            log_fn("  readiness: OK (all script tags bind, no crash-usage issues)")
    except Exception as _e:  # noqa: BLE001
        pass
    return True


# ── Update an existing rmod to the current format ─────────────────────────────

def update_rmod(rmod_path: str, clean_root: str, game_version: str,
                log_fn=print, warn_fn=print) -> str:
    """Re-derive one .rmod in the CURRENT format WITHOUT its original mod folder.

    An rmod fully describes a mod's effect relative to the clean game files, so we can reconstruct that
    effect and re-diff it with the latest converter:
      1. APPLY the rmod to the clean originals (``clean_root`` = a game ROOT holding Data/ [+ Maps/]),
         writing the resulting (modded) dats to a temp tree.
      2. RE-RUN ``run_conversion`` on that modded tree vs the same clean originals.

    The fresh rmod is therefore in the current schema/path convention AND uses every current surgical
    trick — e.g. a raw ``file_patches`` entry the old converter couldn't diff (a scenario, an SDB-only
    mapinfo.win, a .dic) becomes a surgical scenario/SDB/loc/NDF patch when the current converter can
    diff it.  No reconversion from the user's original mod folder is needed.

    Overwrites ``rmod_path`` in place (keeping ``<rmod>.bak``); preserves id/name/version/author/
    description/game_version.  Returns ``"updated"`` | ``"unchanged"`` | ``"skipped"`` (clean originals
    couldn't reconstruct it) | ``"failed"``."""
    import os as _os
    import shutil as _shutil
    import tempfile as _tempfile
    from . import applier
    from . import mod_format

    try:
        original = mod_format.load(rmod_path)
    except Exception as e:
        warn_fn(f"  could not parse: {e}")
        return "failed"

    tmp = _tempfile.mkdtemp(prefix="rmod_upd_")
    try:
        modded = _os.path.join(tmp, "modded")
        _os.makedirs(modded, exist_ok=True)
        # 1) Reconstruct the modded game files by applying the rmod to the clean originals.
        res = applier.apply_mod(rmod_path, str(clean_root), output_dir=modded,
                                game_version=game_version, backup=False)
        if not res.ok():
            for e in res.errors[:5]:
                warn_fn(f"  apply failed: {e}")
            return "skipped"
        # 2) Re-diff the modded result against the clean originals with the current converter.
        out_tmp = _os.path.join(tmp, "updated.rmod")
        ok = run_conversion(
            mod_folder=modded, game_data_dir=str(clean_root), output_rmod=out_tmp,
            name=original.name, mod_id=original.id, version=original.version,
            author=original.author, description=original.description,
            log_fn=(lambda *_a: None), warn_fn=warn_fn)
        if not ok or not _os.path.isfile(out_tmp):
            warn_fn("  re-conversion produced no output")
            return "failed"
        new_mod = mod_format.load(out_tmp)
        new_mod.game_version = original.game_version   # run_conversion drops the optional field
        new_text = mod_format.dump(new_mod)
        # 3) No-op when the canonical form is unchanged (already current).
        if mod_format.dump(original) == new_text:
            return "unchanged"
        _shutil.copyfile(rmod_path, rmod_path + ".bak")
        with open(rmod_path, "w", encoding="utf-8") as f:
            f.write(new_text)
        return "updated"
    finally:
        _shutil.rmtree(tmp, ignore_errors=True)


# ── Compat → Public conversion ────────────────────────────────────────────────

def convert_compat_to_public(input_rmod: str, output_rmod: str,
                              log_fn=print, warn_fn=print) -> bool:
    """Translate a .compat.rmod to a fully pre-translated .rmod for the public game.

    All translations are baked in so the applier applies the file verbatim with
    zero runtime fixup:
      - DAT paths:        PC/99/X or PC/1360/X → PC/190852/X
      - NDF paths:        launchermap chain prefix substituted (compat → public)
      - everything.cpp:   compat→old-public index/class tables, then the
                          v23660935 -1-above-46753/220 shift to NEW-public
      - mapinfo.cpp:      TMapLoadInfo / TUIResourceTexture indices table-translated
      - IA_Common paths:  genpython/100000/ → genpython/10000/
      - DataMap paths:    internal chain paths translated to public layout
    """
    from . import path_map as pm

    with open(input_rmod, encoding="utf-8") as f:
        rmod_data = json.load(f)

    out = pm.translate_rmod_data(rmod_data)

    dest = Path(output_rmod)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    total_ndf = sum(len(pg.get("changes", [])) for pg in out.get("patches", []))
    total_raw = sum(len(fg.get("files", []))   for fg in out.get("file_patches", []))
    total_loc = sum(len(lg.get("entries", []))  for lg in out.get("loc_patches", []))
    log_fn(f"Written: {output_rmod}  ({total_ndf} NDF, {total_loc} loc, {total_raw} raw file patches)")
    return True

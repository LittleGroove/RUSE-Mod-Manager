"""rmod index/ObjRef REPAIR + DIAGNOSE by stable-name resolution.

Companion to migrate.py. Where migrate.py composes per-build *transition maps*
(positional reindex between vanilla builds), this module resolves a change's
target instance **by its stable name** (`ClassNameForDebug`/`DescriptorId`/
`_ShortDatabaseName`) directly against each build's vanilla NDF. That is immune
to the keyless-positional-drift bug that mis-mapped GFX/turret descriptors across
the OG->remaster jump (migrated operation mods crashed because a keyless `_index`
or ObjRef pointed at the wrong instance).

Two entry points:

- `diagnose(mod, builds)` — score every `_index`/ObjRef change against each build's
  vanilla NDF (does it land on the declared class / a named instance?). Returns the
  per-build consistency and the **likeliest source build** (the coordinate space the
  rmod's indices are actually in). Detects mislabeled / corrupted mods.

- `repair(mod, builds, source_build, target_build)` — rewrite every `_index` and ObjRef
  `inst` from `source_build` coordinates to `target_build` coordinates BY NAME. Keyless
  instances that are class-consistent at their index are left untouched (stable); ones
  that are neither nameable nor class-consistent are reported (unresolved). All other
  rmod content (set values, file/loc/scenario groups) is left exactly as-is — so a
  hand-tuned mod keeps its edits and only the broken indices are fixed.
"""
import os
from typing import Dict, List, Optional, Tuple

from . import edata, ndfbin
from .converter import _stable_key

# OBJ_REF marker in ndfbin (a serialized ObjRef in an rmod value carries an "inst")
# rmod value_defs represent an ObjRef as a dict with an "inst" key (see migrate._remap_objref_value).


# ── vanilla NDF loading (search the build's PC/<dataver> subdirs) ──────────────
_NDF_CACHE: dict = {}


def load_ndf_at(build_root: str, dat_rel: str, ndf_substr: str):
    """Load one NDF from the EXACT dat path a patch group targets (relative to a build
    root). Critical for OG, which spreads dats across PC/{99,1360,64} — a change's dat
    path names the right one; searching picks the wrong everything.cpp."""
    parts = dat_rel.replace("\\", "/").split("/")
    path = os.path.join(build_root, *parts)
    if not os.path.isfile(path):
        return None
    ck = ("@", path, ndf_substr)
    if ck in _NDF_CACHE:
        return _NDF_CACHE[ck]
    try:
        ed = edata.EdataFile.open(path)
        cand = [x for x in ed.list() if ndf_substr in x.replace("\\", "/").lower()]
        res = ndfbin.read(ed.get(cand[0])) if cand else None
    except Exception:
        res = None
    _NDF_CACHE[ck] = res
    return res


def load_build_ndf(build_root: str, dat_hint: str, ndf_substr: str):
    """Load one NDF asset from a build's dats, searching Data/PC/*/ for the dat that
    holds it (OG spreads dats across PC/99|1360|64; the remaster consolidates to 190852)."""
    ck = (build_root, dat_hint, ndf_substr)
    if ck in _NDF_CACHE:
        return _NDF_CACHE[ck]
    base = os.path.join(build_root, "Data", "PC")
    result = None
    if os.path.isdir(base):
        for sub in sorted(os.listdir(base)):
            d = os.path.join(base, sub)
            if not os.path.isdir(d):
                continue
            for fn in os.listdir(d):
                if dat_hint.lower() in fn.lower() and fn.lower().endswith(".dat"):
                    try:
                        ed = edata.EdataFile.open(os.path.join(d, fn))
                        cand = [x for x in ed.list()
                                if ndf_substr in x.replace("\\", "/").lower()]
                        if cand:
                            result = ndfbin.read(ed.get(cand[0]))
                            break
                    except Exception:
                        continue
            if result is not None:
                break
    _NDF_CACHE[ck] = result
    return result


_OBJ_REF_MARKER = 0xBBBBBBBB  # ndfbin OBJ_REF (ref = (obj_idx, cls_idx)); 0xAAAAAAAA = TRANS_REF


def _iter_objrefs(val):
    """Yield (position, target_obj_idx) for every OBJ_REF inside an ndfbin NdfValue
    (recurses List/Map). Position is the list index (0 for scalars)."""
    tid = val.type_id
    if tid == ndfbin.T.Reference:
        marker, ref = val.raw
        if marker == _OBJ_REF_MARKER:
            yield (0, ref[0] if isinstance(ref, (tuple, list)) else ref)
    elif tid == ndfbin.T.List:
        for i, item in enumerate(val.raw):
            for _, t in _iter_objrefs(item):
                yield (i, t)
    elif tid == ndfbin.T.Map:
        for i, kv in enumerate(val.raw):
            value = kv[1] if isinstance(kv, (tuple, list)) and len(kv) == 2 else kv
            if hasattr(value, "type_id"):
                for _, t in _iter_objrefs(value):
                    yield (i, t)


def _referrer_map(ndf) -> Dict[int, list]:
    """target_inst_idx -> [(referrer_idx, prop_name, position), ...]."""
    rev: Dict[int, list] = {}
    nprops = len(ndf.properties)
    for i, inst in enumerate(ndf.instances):
        for pv in inst.props:
            pname = ndf.properties[pv.prop_index].name if pv.prop_index < nprops else str(pv.prop_index)
            for pos, tgt in _iter_objrefs(pv.value):
                rev.setdefault(tgt, []).append((i, pname, pos))
    return rev


def build_index(ndf):
    """Return (key->idx, idx->key) where key is the stable name when present, else a
    TOPOLOGICAL key derived from a keyed referrer ('the Nth <prop> of <named parent>').
    Lets keyless children (camera-path keys, weapon/ammo sub-objects) match by structure."""
    stable: Dict[int, tuple] = {}
    for i, inst in enumerate(ndf.instances):
        k = _stable_key(inst, ndf)
        if k is not None:
            stable[i] = k
    rev = _referrer_map(ndf)

    idx_to_key: Dict[int, tuple] = dict(stable)

    def topo(i: int, depth: int = 0):
        if i in stable:
            return stable[i]
        if depth > 5:
            return None
        for (ri, pname, pos) in sorted(rev.get(i, [])):
            rk = topo(ri, depth + 1)
            if rk is not None:
                return ("TOPO", rk, pname, pos)
        return None

    for i in range(len(ndf.instances)):
        if i in idx_to_key:
            continue
        tk = topo(i)
        if tk is not None:
            idx_to_key[i] = tk

    key_to_idx: Dict[tuple, int] = {}
    for i, k in idx_to_key.items():
        key_to_idx.setdefault(k, i)
    return key_to_idx, idx_to_key


def _name_index(ndf) -> Dict[tuple, int]:
    """Back-compat shim: stable+topological key -> index."""
    return build_index(ndf)[0]


def _class_at(ndf, idx: int) -> Optional[str]:
    if idx < 0 or idx >= len(ndf.instances):
        return None
    ci = ndf.instances[idx].class_index
    return ndf.classes[ci].name if ci < len(ndf.classes) else None


def _class_prop_names(ndf, class_name: str):
    """Set of property names defined on a class, or None if the class is absent."""
    ci = next((c.index for c in ndf.classes if c.name == class_name), None)
    if ci is None:
        return None
    return {p.name for p in ndf.properties if p.class_index == ci}


# ── per-NDF resolution ────────────────────────────────────────────────────────
def _dat_hint(dat: str) -> str:
    base = dat.replace("\\", "/").split("/")[-1]
    return base[:-4] if base.lower().endswith(".dat") else base


def resolve_group(pg, src_idx_to_key, tgt_key_idx, tgt_ndf) -> dict:
    """Resolve one patch group's _index matches + ObjRef insts src->tgt by stable OR
    topological key. Mutates pg in place. Returns counts."""
    rep = {"fixed": 0, "confirmed": 0, "stable": 0, "prop_renamed": 0, "unresolved": []}

    # Property-NAME translation: a class property recased/renamed across builds
    # (e.g. TChallengeMapInfo Guid<->GUID across the remaster) leaves a change's
    # set_props/del_props keyed by the SOURCE name. The engine ignores the unknown key
    # and the real property stays null -> e.g. a null operation GUID -> load crash.
    # Case-insensitive match against the target class is safe (exactly-one match only).
    tgt_props_cache: dict = {}

    def fix_prop_names(ch):
        tp = tgt_props_cache.get(ch.table)
        if tp is None:
            tp = _class_prop_names(tgt_ndf, ch.table)
            tgt_props_cache[ch.table] = tp if tp is not None else set()
        if not tp:
            return
        lower = {p.lower(): p for p in tp}
        for store in ("set_props", "del_props"):
            d = getattr(ch, store, None)
            if not isinstance(d, dict):
                continue
            for key in list(d.keys()):
                if key not in tp:
                    m = lower.get(key.lower())
                    if m is not None and m != key:
                        d[m] = d.pop(key)
                        rep["prop_renamed"] += 1

    def remap_inst(src_inst_idx: int) -> Optional[int]:
        if src_inst_idx is None:
            return None
        k = src_idx_to_key.get(int(src_inst_idx))
        if k is None:
            return None
        return tgt_key_idx.get(k)

    def remap_value(v):
        # rmod ObjRef value is a dict carrying "inst" (and class). Recurse lists/dicts.
        if isinstance(v, dict) and "inst" in v:
            ni = remap_inst(int(v["inst"]))
            if ni is not None and ni != v["inst"]:
                v = dict(v); v["inst"] = ni
            return v
        if isinstance(v, list):
            return [remap_value(x) for x in v]
        if isinstance(v, dict):
            return {kk: remap_value(vv) for kk, vv in v.items()}
        return v

    for ch in pg.changes:
        # 0) property-name recasing/rename (Guid<->GUID etc.)
        fix_prop_names(ch)
        # 1) the _index match
        if "_index" in ch.match:
            oi = int(ch.match["_index"])
            ni = remap_inst(oi)
            if ni is not None:
                if ni == oi:
                    rep["confirmed"] += 1
                else:
                    ch.match["_index"] = str(ni); rep["fixed"] += 1
            else:
                # keyless: keep if class-consistent at the same index in target (stable),
                # else flag.
                tgt_cls = _class_at(tgt_ndf, oi)
                if tgt_cls == ch.table:
                    rep["stable"] += 1
                else:
                    rep["unresolved"].append(
                        {"table": ch.table, "_index": oi,
                         "tgt_class_at_index": tgt_cls,
                         "reason": "keyless + class mismatch at index"})
        # 2) ObjRef insts inside set_props (recursively)
        for prop_name, vd in list(ch.set_props.items()):
            vd.value = remap_value(vd.value)
    return rep


# ── diagnose: which build are this rmod's indices actually in? ─────────────────
def diagnose(mod, builds: Dict[str, str]) -> dict:
    """builds: {build_id: build_root}. Score each build by how class-consistent the
    rmod's _index matches are against that build's vanilla NDF. The best score is the
    likeliest SOURCE build (the coordinate space the rmod is authored in)."""
    out = {"per_build": {}, "source_build": None}
    # collect (dat_hint, ndf_substr, [(table,_index)...]) per patch group
    groups = []
    for pg in mod.patches:
        idxs = [(ch.table, int(ch.match["_index"]))
                for ch in pg.changes if "_index" in ch.match]
        if idxs:
            groups.append((_dat_hint(pg.dat),
                           pg.ndf.replace("\\", "/").split("/")[-1].lower(), idxs))
    if not groups:
        return out
    best_id, best_score = None, -1.0
    for bid, root in builds.items():
        hit = total = 0
        for dh, ndf_sub, idxs in groups:
            ndf = load_build_ndf(root, dh, ndf_sub)
            if ndf is None:
                continue
            for table, idx in idxs:
                total += 1
                if _class_at(ndf, idx) == table:
                    hit += 1
        score = (hit / total) if total else 0.0
        out["per_build"][bid] = {"consistent": hit, "total": total, "score": round(score, 4)}
        if score > best_score:
            best_id, best_score = bid, score
    out["source_build"] = best_id
    return out


# ── repair: rewrite indices/ObjRefs source_build -> target_build BY NAME ───────
def repair(mod, builds: Dict[str, str], source_build: str, target_build: str) -> dict:
    """Mutates mod's patch-group _index matches + ObjRef insts from source_build to
    target_build coordinates by stable name. Non-NDF groups and all set VALUES are
    left untouched. Returns a per-group report."""
    src_root, tgt_root = builds[source_build], builds[target_build]
    report = {"source_build": source_build, "target_build": target_build,
              "groups": [], "fixed": 0, "confirmed": 0, "stable": 0,
              "prop_renamed": 0, "unresolved": 0}
    for pg in mod.patches:
        dh = _dat_hint(pg.dat)
        ndf_sub = pg.ndf.replace("\\", "/").split("/")[-1].lower()
        # SOURCE: honor the patch group's EXACT dat path (OG spreads across PC/{99,1360,64}).
        src_ndf = load_ndf_at(src_root, pg.dat, ndf_sub) or load_build_ndf(src_root, dh, ndf_sub)
        # TARGET: modern builds consolidate to one PC/<dataver>, so search by dat name.
        tgt_ndf = load_build_ndf(tgt_root, dh, ndf_sub)
        if src_ndf is None or tgt_ndf is None:
            report["groups"].append({"ndf": pg.ndf, "skipped": "vanilla NDF not found"})
            continue
        tgt_key_idx, _ = build_index(tgt_ndf)
        _, src_idx_to_key = build_index(src_ndf)
        gr = resolve_group(pg, src_idx_to_key, tgt_key_idx, tgt_ndf)
        report["fixed"] += gr["fixed"]; report["confirmed"] += gr["confirmed"]
        report["stable"] += gr["stable"]; report["unresolved"] += len(gr["unresolved"])
        report["prop_renamed"] += gr.get("prop_renamed", 0)
        report["groups"].append({"ndf": pg.ndf, **gr})

    # cross-format path rewrite (OG PC/99|1360|64 -> remaster PC/190852). Done AFTER
    # resolution so vanilla loading used the source-coord dat paths. Mirrors convert_rmod.
    if _is_cross_format(source_build, target_build):
        from .applier import resolve_dat_for_version
        try:
            from . import path_map as _pm
        except Exception:
            _pm = None
        # resolve_dat_for_version flattens Data/PC/* to PC/190852 for any remaster
        # build (mode "public"); the OG/compat format keeps its original PC sub-dir.
        # Key off the target's DATA VERSION, not a single hardcoded public build id,
        # so this stays correct as the public branch moves to new builds.
        from . import game_versions as _gv
        mode = "compat" if _gv.dataver_for_build(str(target_build)) == "99" else "public"
        for pg in mod.patches:
            pg.dat = resolve_dat_for_version(pg.dat, mode)
            if _pm is not None:
                try:
                    t = _pm.translate_ndf_path(pg.ndf)
                    if t and t != pg.ndf:
                        pg.ndf = t
                except Exception:
                    pass
        for gn in ("file_patches", "loc_patches", "sdb_patches", "scenario_patches"):
            for g in getattr(mod, gn, []) or []:
                if getattr(g, "dat", None):
                    g.dat = resolve_dat_for_version(g.dat, mode)
        report["cross_format_paths_rewritten"] = True

    mod.game_version = target_build
    return report


def _is_cross_format(from_build: str, to_build: str) -> bool:
    """True when source and target have different data versions (the OG->remaster jump)."""
    try:
        import json
        gv = json.load(open(os.path.join(os.path.dirname(__file__), "data",
                                          "game_versions.json"), encoding="utf-8"))
        b = gv.get("builds", {})
        return b.get(str(from_build), {}).get("dataver") != b.get(str(to_build), {}).get("dataver")
    except Exception:
        return False

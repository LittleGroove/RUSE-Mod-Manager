"""
Phase 2 — new-scenario template generator.

Clones an existing scenario (on the same terrain) under a new name, producing a
complete, registered, loadable scenario:
  DataMap_Win.dat:  test\map\{map}\{new}.scenario
                    test\map\{map}\campath\campaths_{new}.ndfbin
                    test\map\{map}\zonebluff\{new}.kdt
  ZZ_GladPatchableWin.dat:
                    genglad\patchable\scenario\{map}\{newfolder}\clustermap.cpp.gladndfbin
                    genglad\patchable\scenario\{map}\{newfolder}\mapia.cpp.gladndfbin
                    + TMapLoadInfo (mapinfo.cpp), TMultiMapInfo + TMultiPack (globals.cpp)

The KDT and zone geometry are REUSED from the source scenario (new-game-mode-on-
existing-terrain). Editing zone geometry additionally needs the KDT builder.

This module operates on bytes/NDF objects and returns a plan of file
replacements/additions; a thin tool injects them into copies of the dats.
"""
import struct
from . import ndfbin, dic
from .ndfbin import NdfInstance, NdfPropertyValue, NdfValue, T, OBJ_REF_MARKER


# ── Deep instance clone (with ObjRef remapping) — registration enabler ──────────

def _clone_value(ndf, val, remap):
    """Copy an NdfValue, deep-cloning OBJ_REF targets and remapping their indices."""
    if val.type_id == T.Reference:
        marker, ref = val.raw
        if marker == OBJ_REF_MARKER:
            obj_idx, cls = ref
            new_obj = clone_instance_deep(ndf, obj_idx, remap)
            return NdfValue(T.Reference, (OBJ_REF_MARKER, (new_obj, cls)))
        return NdfValue(val.type_id, val.raw)            # trans ref: leave as-is
    if val.type_id == T.List:
        return NdfValue(T.List, [_clone_value(ndf, it, remap) for it in val.raw])
    if val.type_id == T.Map:
        return NdfValue(T.Map, [(_clone_value(ndf, k, remap), _clone_value(ndf, v, remap))
                                for (k, v) in val.raw])
    return NdfValue(val.type_id, val.raw)                # scalar / string


def clone_instance_deep(ndf, src_idx, remap=None):
    """Clone instance src_idx and every instance reachable via OBJ_REFs.
    Appends clones to ndf.instances, remaps refs, returns the new root index."""
    if remap is None:
        remap = {}
    if src_idx in remap:
        return remap[src_idx]
    src = ndf.instances[src_idx]
    new = NdfInstance(class_index=src.class_index)
    new_idx = len(ndf.instances)
    ndf.instances.append(new)
    remap[src_idx] = new_idx
    for pv in src.props:
        new.props.append(NdfPropertyValue(pv.prop_index, _clone_value(ndf, pv.value, remap)))
    return new_idx


# ── scenario <-> glad-folder naming ─────────────────────────────────────────────
# DataMap scenario file: leveldesign{suffix}.scenario  (base suffix = "")
# glad scenario folder:  scenario{suffix}              (base = "scenario")
# campath:               campaths_leveldesign{suffix}.ndfbin
# kdt:                   leveldesign{suffix}.kdt

def _scn_stub(scenario_name: str) -> str:
    """'leveldesign_2v2_v01' -> '_2v2_v01' ; 'leveldesign' -> ''."""
    base = "leveldesign"
    n = scenario_name.lower()
    if n == base:
        return ""
    if n.startswith(base):
        return scenario_name[len(base):]
    return "_" + scenario_name


def datamap_paths(map_dir: str, scenario_name: str) -> dict:
    return {
        "scenario": f"test\\map\\{map_dir}\\{scenario_name}.scenario",
        "campath":  f"test\\map\\{map_dir}\\campath\\campaths_{scenario_name}.ndfbin",
        "kdt":      f"test\\map\\{map_dir}\\zonebluff\\{scenario_name}.kdt",
    }


def glad_scenario_folder(scenario_name: str) -> str:
    stub = _scn_stub(scenario_name)
    return "scenario" + stub


def _scenario_replacements(map_dir: str, src_scn: str, new_scn: str,
                           src_folder: str, new_folder: str) -> list:
    """Substitution pairs that redirect a scenario's runtime references from src to new.

    Two token kinds, both matched case-insensitively:
      * scenario-file stem  (LevelDesign_Normal -> leveldesign_blitztest): catches the
        .scenario / campath / .kdt references wherever they appear.
      * glad scenario folder, ANCHORED by map dir ({map}\\{folder}\\ -> {map}\\{newfolder}\\):
        the bare base folder is literally 'Scenario', so it must never be replaced
        un-anchored.  Anchoring also keeps 'Scenario\\' from touching 'Scenario_TestIA\\'.

    Editor-only refs ('...\\Editor\\map\\{map}\\Scenario\\...', '{map}\\Scenario.ndf')
    point at files absent from the shipped dat, so the runtime skips them; redirecting
    them (a side effect of the folder token) is harmless."""
    reps = []
    if src_scn.lower() != new_scn.lower():
        reps.append((src_scn, new_scn))
    if src_folder.lower() != new_folder.lower():
        a = f"{map_dir}\\{src_folder}\\"; b = f"{map_dir}\\{new_folder}\\"
        reps.append((a, b))
        reps.append((f"{map_dir}\\{src_folder}.", f"{map_dir}\\{new_folder}."))
    return reps


def glad_paths(map_dir: str, folder: str) -> dict:
    """Dat virtual paths for a scenario's cluster NDFs, given the glad folder name
    (e.g. 'scenario' for blitz, 'scenario_2v2_v01' for chess)."""
    base = f"genglad\\patchable\\scenario\\{map_dir}\\{folder}"
    return {
        "clustermap": f"{base}\\clustermap.cpp.gladndfbin",
        "mapia":      f"{base}\\mapia.cpp.gladndfbin",
    }


# ── NDF string substitution (for clustermap/mapia clone) ────────────────────────

def _subst_strings(ndf: "ndfbin.NdfBinary", replacements: list) -> int:
    """Replace substrings inside every string-table entry. replacements: [(old, new)].
    Returns number of strings changed. Case-insensitive, preserves surrounding text."""
    changed = 0
    table = ndf.strings if hasattr(ndf, "strings") else None
    if table is None:
        return 0
    import re
    for i, s in enumerate(table):
        if not isinstance(s, str):
            continue
        new = s
        for old, rep in replacements:
            new = re.sub(re.escape(old), lambda m: rep, new, flags=re.IGNORECASE)
        if new != s:
            table[i] = new
            changed += 1
    return changed


def _subst_inst_strings(ndf, idx_iterable, replacements):
    """Substitute `replacements` in the string-valued props of the given instances,
    recursing into List/Map containers (e.g. TNDFTransactionFileList.Files is a List
    of PathRef). New STRG strings are interned (shared originals are NOT mutated);
    References are left untouched (ref remap is the deep-clone's job)."""
    import re
    def rep_all(s):
        for old, rep in replacements:
            s = re.sub(re.escape(old), lambda m: rep, s, flags=re.IGNORECASE)
        return s
    def rewrite(v):
        """Return a (possibly new) NdfValue with string leaves substituted."""
        if v.type_id in (T.StringRef, T.PathRef):
            old = ndf.get_string(v.raw)
            new = rep_all(old)
            return NdfValue(v.type_id, ndf.ensure_string(new)) if new != old else v
        if v.type_id == T.WideStr and isinstance(v.raw, str):
            new = rep_all(v.raw)
            return NdfValue(T.WideStr, new) if new != v.raw else v
        if v.type_id == T.List:
            items = [rewrite(it) for it in v.raw]
            return NdfValue(T.List, items)
        if v.type_id == T.Map:
            pairs = [(rewrite(k), rewrite(vv)) for k, vv in v.raw]
            return NdfValue(T.Map, pairs)
        return v
    for idx in idx_iterable:
        for pv in ndf.instances[idx].props:
            pv.value = rewrite(pv.value)


def _reachable_strings(ndf, root_idx, max_depth=3):
    """Yield every StringRef/PathRef/WideStr value reachable from an instance by
    following OBJ_REFs (lists/maps included), up to max_depth."""
    seen = set()
    def walk(idx, depth):
        if idx in seen or depth < 0 or not (0 <= idx < len(ndf.instances)):
            return
        seen.add(idx)
        for pv in ndf.instances[idx].props:
            yield from _vals(pv.value, depth)
    def _vals(v, depth):
        if v.type_id in (T.StringRef, T.PathRef):
            yield ndf.get_string(v.raw)
        elif v.type_id == T.WideStr and isinstance(v.raw, str):
            yield v.raw
        elif v.type_id == T.List:
            for it in v.raw:
                yield from _vals(it, depth)
        elif v.type_id == T.Map:
            for k, vv in v.raw:
                yield from _vals(k, depth); yield from _vals(vv, depth)
        elif v.type_id == T.Reference:
            mk, ref = v.raw
            if mk == OBJ_REF_MARKER:
                yield from walk(ref[0], depth - 1)
    yield from walk(root_idx, max_depth)


def _get_prop(ndf, inst, prop_name):
    p = ndf.prop_by_name_and_class(prop_name, inst.class_index) or ndf.prop_by_name(prop_name)
    return inst.get(p.index) if p else None


def _set_raw(ndf, inst, prop_name, new_raw):
    """Replace a prop's raw value, PRESERVING its existing type_id (or skip if absent)."""
    p = ndf.prop_by_name_and_class(prop_name, inst.class_index) or ndf.prop_by_name(prop_name)
    if p is None:
        return False
    cur = inst.get(p.index)
    type_id = cur.type_id if cur is not None else T.StringRef
    inst.set(p.index, NdfValue(type_id, new_raw))
    return True


def _set_str(ndf, inst, prop_name, s):
    """Set a string-valued prop, encoding per its actual type (StringRef/PathRef ->
    STRG index ; WideStr -> str)."""
    p = ndf.prop_by_name_and_class(prop_name, inst.class_index) or ndf.prop_by_name(prop_name)
    if p is None:
        return False
    cur = inst.get(p.index)
    ttype = cur.type_id if cur is not None else T.StringRef
    if ttype in (T.StringRef, T.PathRef):
        inst.set(p.index, NdfValue(ttype, ndf.ensure_string(s)))
    else:  # WideStr or other string-ish
        inst.set(p.index, NdfValue(ttype, s))
    return True


def register_cloned_map(m_ndf, g_ndf, map_dir, src_scn, new_scn, src_folder, new_folder,
                        new_name, new_guid, tracking_id, game_type=None, nb_players=None,
                        new_description=None, substitute=True):
    """Register a cloned scenario as a new MP map.
      m_ndf = mapinfo.cpp NDF   g_ndf = globals.cpp NDF
      src_folder/new_folder = glad scenario folder ('Scenario' / 'scenario_blitztest')
      new_guid = 16 bytes (links TMapLoadInfo <-> TMultiMapInfo)
    The new TMultiMapInfo is appended to the SAME existing TMultiPack that already holds
    the source map, so it is enumerated by TMultiPackManager.MultiPackList (an orphan new
    pack is never seen by the menu).  Returns (new_tmli_idx, new_tmmi_idx, pack_idx)."""
    reps = _scenario_replacements(map_dir, src_scn, new_scn, src_folder, new_folder)

    # source TMapLoadInfo: Path == map_dir AND it reaches the anchored folder path
    # '{map}\{src_folder}\' (this disambiguates the 3 supercrossroads4 maps, etc.).
    needle = f"{map_dir}\\{src_folder}\\".lower()
    src_tmli = None
    for idx, inst in m_ndf.find_instances("TMapLoadInfo"):
        pv = _get_prop(m_ndf, inst, "Path")
        path = m_ndf.get_string(pv.raw) if (pv and pv.type_id in (T.StringRef, T.PathRef)) else None
        if not path or path.lower() != map_dir.lower():
            continue
        if any(needle in s.lower() for s in _reachable_strings(m_ndf, idx)):
            src_tmli = idx; break
    if src_tmli is None:
        raise ValueError(f"source TMapLoadInfo for map '{map_dir}' folder '{src_folder}' not found")
    src_guid = _get_prop(m_ndf, m_ndf.instances[src_tmli], "GUID")
    src_guid_bytes = bytes(src_guid.raw) if src_guid else None

    remap = {}
    new_tmli = clone_instance_deep(m_ndf, src_tmli, remap)
    if substitute:
        _subst_inst_strings(m_ndf, list(remap.values()), reps)   # redirect file refs to new names
    # else: the clone reuses the SOURCE map's files verbatim (alias — for isolation testing)
    _set_str(m_ndf, m_ndf.instances[new_tmli], "Name", new_name)         # StringRef or WideStr
    _set_raw(m_ndf, m_ndf.instances[new_tmli], "GUID", bytes(new_guid))  # blob, type preserved
    if new_tmli not in m_ndf.top_objects:
        m_ndf.top_objects.append(new_tmli)

    # source TMultiMapInfo: matched by GUID == src TMapLoadInfo GUID
    src_tmmi = None
    for idx, inst in g_ndf.find_instances("TMultiMapInfo"):
        gv = _get_prop(g_ndf, inst, "GUID")
        if gv is not None and src_guid_bytes is not None and bytes(gv.raw) == src_guid_bytes:
            src_tmmi = idx; break
    if src_tmmi is None:
        raise ValueError(f"source TMultiMapInfo (GUID {src_guid_bytes.hex() if src_guid_bytes else None}) "
                         "not found — is this map actually in the MP list?")
    new_tmmi = clone_instance_deep(g_ndf, src_tmmi, {})
    gi = g_ndf.instances[new_tmmi]
    _set_raw(g_ndf, gi, "GUID", bytes(new_guid))
    _set_str(g_ndf, gi, "TrackingId", tracking_id)
    # Description is a LocHash (8-byte key into flash_txt.dic), NOT free text — set it as RAW bytes
    # (a key produced by apply_custom_name). _set_str would corrupt the LocHash.
    if new_description is not None: _set_raw(g_ndf, gi, "Description", bytes(new_description))
    if game_type is not None:  _set_raw(g_ndf, gi, "GameType", game_type)
    if nb_players is not None: _set_raw(g_ndf, gi, "NbPlayers", nb_players)
    # NB: TMultiMapInfo are NOT top_objects (0/30 originals are) — they are reachable only
    # via TMultiPack.MultiList.  Adding one as a root makes the menu loader choke (it expects
    # only manager/pack roots).  So DON'T append new_tmmi to g_ndf.top_objects.

    # Append the new TMultiMapInfo to the EXISTING pack that holds the source map (so the
    # menu enumerates it).  Fall back to the largest pack (the main ranked pool).
    def pack_list(pinst):
        ml = _get_prop(g_ndf, pinst, "MultiList")
        return ml.raw if (ml and ml.type_id == T.List) else []
    def list_refs(pinst):
        return [v.raw[1][0] for v in pack_list(pinst)
                if v.type_id == T.Reference and isinstance(v.raw, tuple) and v.raw[0] == OBJ_REF_MARKER]
    packs = g_ndf.find_instances("TMultiPack")
    if not packs:
        raise ValueError("no TMultiPack present")
    target = next(((pi, pin) for pi, pin in packs if src_tmmi in list_refs(pin)), None)
    if target is None:
        target = max(packs, key=lambda p: len(pack_list(p[1])))
    pidx, pinst = target
    new_ml = list(pack_list(pinst)) + [
        NdfValue(T.Reference, (OBJ_REF_MARKER, (new_tmmi, gi.class_index)))]
    _set_raw(g_ndf, pinst, "MultiList", new_ml)   # type_id (List) preserved
    return new_tmli, new_tmmi, pidx


def _subst_widestr(ndf: "ndfbin.NdfBinary", replacements: list) -> int:
    """Substitute inside WideStr prop values (stored inline, not in STRG)."""
    import re
    changed = 0
    for inst in ndf.instances:
        for pv in inst.props:
            v = pv.value
            if v.type_id == ndfbin.T.WideStr and isinstance(v.raw, str):
                new = v.raw
                for old, rep in replacements:
                    new = re.sub(re.escape(old), lambda m: rep, new, flags=re.IGNORECASE)
                if new != v.raw:
                    v.raw = new
                    changed += 1
    return changed


def clone_glad_ndf(src_bytes: bytes, reps: list) -> bytes:
    """Clone a clustermap/mapia NDF, substituting `reps` in all string-table paths
    AND inline WideStr values (the mapia KDT path is WideStr)."""
    ndf = ndfbin.read(src_bytes)
    n1 = _subst_strings(ndf, reps)
    n2 = _subst_widestr(ndf, reps)
    return ndfbin.write(ndf, compress=True), n1 + n2


# ── Top-level orchestrator ──────────────────────────────────────────────────────

MAPINFO_PATH = r"genglad\patchable\mapinfo.cpp.gladndfbin"
GLOBALS_PATH = r"genglad\patchable\misc\globals.cpp.gladndfbin"


def find_flash_dics(zz):
    """All flash_txt.dic virtual paths in ZZ_Win.dat (dev + every translation lang) — these hold
    the MP map-name strings keyed by TMultiMapInfo.Description."""
    return sorted(vp for vp in zz.list()
                  if vp.replace("/", "\\").lower().endswith("\\flash_txt.dic"))


def apply_custom_name(zz, name):
    """Add a brand-new MP map name to every flash_txt.dic under a single fresh key.
    Returns (description_key_8bytes, {vpath: new_dic_bytes}) — caller writes the bytes into
    ZZ_Win.dat and passes the key as register_cloned_map(new_description=key)."""
    paths = find_flash_dics(zz)
    if not paths:
        raise ValueError("no flash_txt.dic found in ZZ_Win.dat")
    blobs = {p: zz.get(p) for p in paths}
    key = dic.free_map_key(*blobs.values())              # one key unused across ALL languages
    out = {p: dic.add_entry(b, key, name) for p, b in blobs.items()}
    return key, out


def generate_scenario(dm, gd, map_dir, src_scn, new_scn, new_name,
                      tracking_id, game_type=None, nb_players=None, new_guid=None,
                      src_folder=None, new_folder=None, new_description=None, zz=None):
    """Build a complete new MP scenario by cloning an existing one on the same terrain.

    dm = DataMap_Win.dat (edata), gd = ZZ_GladPatchableWin.dat (edata).
    src_folder/new_folder = glad scenario folder names; if omitted they are derived from
    the scenario stems (works for the regular 'leveldesign_X' <-> 'scenario_X' maps, but
    irregular maps like blitz — leveldesign_normal lives in the bare 'Scenario' folder —
    MUST pass src_folder explicitly).
    Returns a plan dict: {datamap_add, glad_add, glad_mod, guid} of file paths -> bytes.
    The KDT + zone geometry are REUSED from src (new-game-mode-on-existing-terrain).
    """
    import os
    if new_guid is None:
        new_guid = os.urandom(16)
    if src_folder is None: src_folder = glad_scenario_folder(src_scn)
    if new_folder is None: new_folder = glad_scenario_folder(new_scn)
    reps = _scenario_replacements(map_dir, src_scn, new_scn, src_folder, new_folder)
    src_dp, new_dp = datamap_paths(map_dir, src_scn), datamap_paths(map_dir, new_scn)
    src_gp, new_gp = glad_paths(map_dir, src_folder), glad_paths(map_dir, new_folder)

    datamap_add = {}
    for key in ("scenario", "campath", "kdt"):
        b = dm.get(src_dp[key])
        if b is None:
            raise ValueError(f"source {key} not found: {src_dp[key]}")
        datamap_add[new_dp[key]] = b   # reuse geometry/KDT verbatim

    glad_add = {}
    for key in ("clustermap", "mapia"):
        b = gd.get(src_gp[key])
        if b is None:
            raise ValueError(f"source glad {key} not found: {src_gp[key]}")
        out, _ = clone_glad_ndf(b, reps)
        glad_add[new_gp[key]] = out

    # Custom MP-browser name: add `new_name` to every flash_txt.dic (ZZ_Win.dat) under a fresh
    # LocHash key and point Description at it. Without zz, the clone inherits the source's name.
    zz_mod = {}
    if zz is not None and new_description is None:
        new_description, zz_mod = apply_custom_name(zz, new_name)

    m_ndf = ndfbin.read(gd.get(MAPINFO_PATH))
    g_ndf = ndfbin.read(gd.get(GLOBALS_PATH))
    ids = register_cloned_map(m_ndf, g_ndf, map_dir, src_scn, new_scn, src_folder, new_folder,
                              new_name, new_guid, tracking_id, game_type, nb_players,
                              new_description=new_description)
    glad_mod = {
        MAPINFO_PATH: ndfbin.write(m_ndf, compress=True),
        GLOBALS_PATH: ndfbin.write(g_ndf, compress=True),
    }
    plan = {"datamap_add": datamap_add, "glad_add": glad_add,
            "glad_mod": glad_mod, "guid": new_guid, "ids": ids}
    if zz_mod:
        plan["zz_mod"] = zz_mod            # {vpath: flash_txt.dic bytes} -> write into ZZ_Win.dat
    return plan

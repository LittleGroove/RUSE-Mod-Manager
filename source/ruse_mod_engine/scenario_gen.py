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


def register_cloned_scenario(m_ndf, g_ndf, map_dir, src_scn, new_scn, src_folder, new_folder,
                             new_name, new_guid, tracking_id, kind="mp", set_props=None,
                             new_description=None, loc_props=None, substitute=True):
    """Register a cloned scenario of ANY kind (mp / campaign / operation), driven by the
    scenario_registry.REGISTRY_KINDS descriptor.

      m_ndf = mapinfo.cpp NDF   g_ndf = globals.cpp NDF
      src_folder/new_folder = glad scenario folder (e.g. 'Scenario', 'scenario_chapter1')
      new_guid = 16 bytes (links TMapLoadInfo <-> the kind's info record)
      kind     = 'mp' | 'campaign' | 'operation'
      set_props= {prop_name: raw_value} to apply to the cloned info record (e.g. GameType/NbPlayers
                 for mp, CategoryId for operation). The clone INHERITS all other source fields.

    The new info record is appended to the SAME existing pack that already holds the source one
    (TMultiPack/TChapterPack/TChallengePack), so the menu manager enumerates it (an orphan new pack
    is never shown). Returns (new_tmli_idx, new_info_idx, pack_idx)."""
    from .scenario_registry import REGISTRY_KINDS
    rk = REGISTRY_KINDS[kind]
    reps = _scenario_replacements(map_dir, src_scn, new_scn, src_folder, new_folder)

    # ── (a) source TMapLoadInfo: Path == map_dir AND it reaches the anchored folder path
    #         '{map}\{src_folder}\' (disambiguates same-map scenarios, incl. irregular folders).
    needle = f"{map_dir}\\{src_folder}\\".lower()
    src_tmli = None
    for idx, inst in m_ndf.find_instances("TMapLoadInfo"):
        pv = _get_prop(m_ndf, inst, "Path")
        path = m_ndf.get_string(pv.raw) if (pv and pv.type_id in (T.StringRef, T.PathRef)) else None
        if not path or path.lower() != map_dir.lower():
            continue
        if any(needle in s.lower() for s in _reachable_strings(m_ndf, idx, max_depth=6)):
            src_tmli = idx; break
    if src_tmli is None:
        raise ValueError(f"source TMapLoadInfo for map '{map_dir}' folder '{src_folder}' not found")
    src_guid = _get_prop(m_ndf, m_ndf.instances[src_tmli], "GUID")
    src_guid_bytes = bytes(src_guid.raw) if src_guid else None

    # ── (b) clone TMapLoadInfo (kind-independent), redirect file refs, set Name + GUID, top_object.
    remap = {}
    new_tmli = clone_instance_deep(m_ndf, src_tmli, remap)
    if substitute:
        _subst_inst_strings(m_ndf, list(remap.values()), reps)   # redirect file refs to new names
    _set_str(m_ndf, m_ndf.instances[new_tmli], "Name", new_name)
    _set_raw(m_ndf, m_ndf.instances[new_tmli], "GUID", bytes(new_guid))
    if new_tmli not in m_ndf.top_objects:
        m_ndf.top_objects.append(new_tmli)

    # ── (c) source info record (kind's class), matched by GUID == src TMapLoadInfo GUID.
    src_info = None
    for idx, inst in g_ndf.find_instances(rk.info_class):
        gv = _get_prop(g_ndf, inst, rk.guid_prop)
        if gv is not None and src_guid_bytes is not None and bytes(gv.raw) == src_guid_bytes:
            src_info = idx; break
    if src_info is None:
        raise ValueError(f"source {rk.info_class} (GUID {src_guid_bytes.hex() if src_guid_bytes else None}) "
                         f"not found — is '{map_dir}/{src_scn}' actually a {kind} scenario?")
    new_info = clone_instance_deep(g_ndf, src_info, {})
    gi = g_ndf.instances[new_info]
    _set_raw(g_ndf, gi, rk.guid_prop, bytes(new_guid))
    _set_str(g_ndf, gi, rk.tracking_prop, tracking_id)
    # Description is a LocHash key (NOT free text); set as RAW bytes only if the caller supplies one.
    if new_description is not None:
        _set_raw(g_ndf, gi, "Description", bytes(new_description))
    # operation/campaign selection-screen text: {prop -> LocHash key into flash_txt.dic}
    for prop, key in (loc_props or {}).items():
        _set_raw(g_ndf, gi, prop, bytes(key))
    for prop, val in (set_props or {}).items():
        _set_raw(g_ndf, gi, prop, val)
    # info records are NEVER top_objects — reachable only via their pack (adding a root hangs the menu).

    # ── (d) append the new info record to the pack that holds the source (fallback: largest pack).
    def pack_list(pinst):
        ml = _get_prop(g_ndf, pinst, rk.pack_list_prop)
        return ml.raw if (ml and ml.type_id == T.List) else []
    def list_refs(pinst):
        return [v.raw[1][0] for v in pack_list(pinst)
                if v.type_id == T.Reference and isinstance(v.raw, tuple) and v.raw[0] == OBJ_REF_MARKER]
    packs = g_ndf.find_instances(rk.pack_class)
    if not packs:
        raise ValueError(f"no {rk.pack_class} present")
    target = next(((pi, pin) for pi, pin in packs if src_info in list_refs(pin)), None)
    if target is None:
        target = max(packs, key=lambda p: len(pack_list(p[1])))
    pidx, pinst = target
    new_ml = list(pack_list(pinst)) + [
        NdfValue(T.Reference, (OBJ_REF_MARKER, (new_info, gi.class_index)))]
    _set_raw(g_ndf, pinst, rk.pack_list_prop, new_ml)   # type_id (List) preserved
    return new_tmli, new_info, pidx


def register_cloned_map(m_ndf, g_ndf, map_dir, src_scn, new_scn, src_folder, new_folder,
                        new_name, new_guid, tracking_id, game_type=None, nb_players=None,
                        new_description=None, substitute=True):
    """Back-compat MP wrapper around register_cloned_scenario(kind='mp'). Returns
    (new_tmli_idx, new_tmmi_idx, pack_idx)."""
    set_props = {}
    if game_type is not None:
        set_props["GameType"] = game_type
    if nb_players is not None:
        set_props["NbPlayers"] = nb_players
    return register_cloned_scenario(m_ndf, g_ndf, map_dir, src_scn, new_scn, src_folder, new_folder,
                                    new_name, new_guid, tracking_id, kind="mp", set_props=set_props,
                                    new_description=new_description, substitute=substitute)


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
    the MP/operation/campaign menu-name strings keyed by Description/LongDescription LocHash."""
    return sorted(vp for vp in zz.list()
                  if vp.replace("/", "\\").lower().endswith("\\flash_txt.dic"))


def _dic_lang(vp):
    return vp.replace("/", "\\").split("\\")[-2].lower()


def preferred_flash_dic(zz, lang="us"):
    """The flash_txt.dic to READ display names from — the LOCALIZED one for `lang` (default English/us),
    NOT the 'dev' dic, which carries internal labels like '8 Strategists' / 'Hiver nucleaire'. The game
    shows the localized name ('Strategists'). Falls back: lang -> us -> en -> first non-dev -> first."""
    paths = find_flash_dics(zz)
    if not paths:
        return None
    for want in (lang, "us", "en"):
        for p in paths:
            if _dic_lang(p) == want:
                return p
    nondev = [p for p in paths if _dic_lang(p) != "dev"]
    return nondev[0] if nondev else paths[0]


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


def apply_loc_strings(zz, labeled_strings):
    """Add several brand-new localised strings to every flash_txt.dic, each under its own fresh key.
    `labeled_strings` = [(label, text), ...]. Returns ({label: key_8bytes}, {vpath: new_dic_bytes}).

    This is how OPERATION/CAMPAIGN selection-screen text is set: TChallengeMapInfo/TChapterMapInfo
    Description (the operation name, e.g. 'Anzio'), LongDescription1 (date/place) and LongDescription2
    (briefing) are LocHash keys into THIS dic (genlocalisation\\ww2\\localisation\\...\\flash_txt.dic,
    present in ZZ_Win.dat). Unlike the MP map LABEL (external loc), these resolve in the shipped dats."""
    paths = find_flash_dics(zz)
    if not paths:
        raise ValueError("no flash_txt.dic found in ZZ_Win.dat")
    blobs = {p: zz.get(p) for p in paths}
    keys = {}
    for label, text in labeled_strings:
        key = dic.free_map_key(*blobs.values())          # fresh key (recomputed as blobs grow)
        keys[label] = key
        blobs = {p: dic.add_entry(b, key, text) for p, b in blobs.items()}
    return keys, blobs


class _IaAdapter:
    """Give a raw edata archive the entry_paths()/read_many()/get_raw() shape the engine modules expect."""
    def __init__(self, ia):
        self._ia = ia

    def entry_paths(self, _dat_key, suffix=""):
        names = self._ia.list()
        return [p for p in names if p.lower().endswith(suffix.lower())] if suffix else list(names)

    def read_many(self, _dat_key, paths):
        # Use the wrapped object's own bulk read when it has one. Looping get() is fine for a raw edata
        # archive (parsed once, then cheap) but ruinous through a ModProject, where every get_raw
        # re-opens the .dat - and the LocHash index reads ~1000 .dic files out of a 2.4 GB archive.
        bulk = getattr(self._ia, "read_many", None)
        if callable(bulk):
            try:
                return bulk(paths)
            except TypeError:
                pass
        out = {}
        for p in paths:
            b = self._ia.get(p)
            if b is not None:
                out[p] = b
        return out

    def get_raw(self, _dat_key, path):
        return self._ia.get(path)


def _clone_record_text(zz, g_ndf, info_idx, seed_prefix, skip_props, already=None):
    """Give a cloned registration record its OWN text keys.

    A cloned record inherits the source's LocHash values, and the menu text tables are SHARED across the
    whole game — so a duplicated operation does not merely LOOK like the original, it writes through to it:
    editing the copy's intelligence report rewrites the original's.  Every LocHash property that was not
    explicitly given new text gets a fresh key carrying a copy of the source's words in every language.

    Returns ({vpath: blob}, {prop: new_key}).  The blobs are keyed by PLAIN archive path to match the
    rest of the plan — `edits.loc_set_text` keys its result by (dat_key, path) because it can span dats,
    and merging the two shapes into one dict silently produces entries nothing can write."""
    from . import edits as _edits
    idx = _edits.loc_index(_IaAdapter(zz), dat_key="loc")
    if idx is None:
        return {}, {}
    inst = g_ndf.instances[info_idx]
    # Seed with whatever an earlier pass already staged for the same files (apply_loc_strings writes the
    # user-supplied name/subtitle/briefing into flash_txt.dic).  Without this the clone-text pass starts
    # from the pristine blob and silently drops those entries.
    staged = {("loc", p): b for p, b in (already or {}).items()}
    minted = {}
    for pv in list(inst.props):
        if pv.value.type_id != T.LocHash:
            continue
        prop = g_ndf.prop_by_index(pv.prop_index)
        name = prop.name if prop else str(pv.prop_index)
        if name in skip_props:
            continue
        src_key = bytes(pv.value.raw)
        if not _edits.loc_langs(idx, src_key):
            continue          # the source key resolves to no text at all; leave the property alone
        # `staged` is threaded through so several keys landing in the SAME .dic compose; without it each
        # call starts from the pristine blob and only the last property survives.
        new_key, staged = _edits.loc_clone_key(idx, src_key, "%s|%s" % (seed_prefix, name), staged=staged)
        minted[name] = new_key
        _set_raw(g_ndf, inst, name, new_key)
    return {path: blob for (_dat_key, path), blob in staged.items()}, minted


def _cluster_scripting_folders(ndf, map_dir: str) -> list:
    """The trailing folder of every python path the cluster adds THAT BELONGS TO THIS MAP.

    The map filter is essential: a cluster also puts the shared framework packs on the path
    (`DataDir:\\CodeIA\\Python\\Eugen`, `EugenSolo`, `EugenPatchable`, the sound pack...).  Those are the
    game's own libraries — re-pointing one at a scenario folder would unload the entire scripting
    framework.  Only `...\\Test\\map\\<map dir>\\<folder>` is the scenario's own script directory.

    Reading it beats deriving it from the scenario name, which the irregular shipped pairings break."""
    want = "\\map\\" + map_dir.replace("/", "\\").lower() + "\\"
    out = []
    for _i, inst in ndf.find_instances("TClusterAddPythonPath"):
        p = ndf.prop_by_name_and_class("DirectoryList", inst.class_index) or \
            ndf.prop_by_name("DirectoryList")
        v = inst.get(p.index) if p is not None else None
        if v is None or v.type_id != T.List:
            continue
        for item in v.raw:
            if item.type_id not in (T.StringRef, T.PathRef):
                continue
            s = ndf.get_string(item.raw)
            if not s:
                continue
            n = s.replace("/", "\\").rstrip("\\")
            if want not in (n.lower() + "\\"):
                continue
            tail = n.rsplit("\\", 1)[-1]
            if tail and tail not in out:
                out.append(tail)
    return out


def _clone_scenario_dicos(zz, map_dir: str, old_folder: str, new_folder: str) -> dict:
    """Copy every per-language .dic that lives under the scenario's scripting folder to the new folder.

    Without this the cloned cluster names a Localization.dic that does not exist, so every objective line
    renders blank — the same failure mode as the zero-filled .dic gotcha, just reached a different way."""
    if old_folder.lower() == new_folder.lower():
        return {}
    want = "\\%s\\%s\\" % (map_dir.replace("/", "\\").lower(), old_folder.lower())
    out = {}
    for p in zz.list():
        n = p.replace("/", "\\").lower()
        if want not in n or not n.endswith(".dic"):
            continue
        blob = zz.get(p)
        if blob is None:
            continue
        head, _, tail = p.replace("/", "\\").rpartition("\\")
        parts = head.split("\\")
        parts = [new_folder if s.lower() == old_folder.lower() else s for s in parts]
        out["\\".join(parts) + "\\" + tail] = blob
    return out


def scripting_folder(scenario_name: str) -> str:
    """CONVENTIONAL scripting-folder name for a scenario stem ('leveldesign_chapter1' ->
    'Scripting_chapter1').  Only valid for a folder we are CREATING — for an existing scenario, read the
    real folder off its cluster (scenario_chain.ClusterInfo.scripting_folders), because the shipped data
    has irregular pairings the convention gets wrong."""
    return "Scripting" + _scn_stub(scenario_name)


def _script_paths(map_dir, scenario_name, buildid):
    """Paired .xyz script vpath in IA_Common.dat for a scenario (campaign/operation only).

    `buildid` is DISCOVERED from the dat, never assumed: the shipped public build uses `genpython\\1000\\`,
    and guessing wrong does not error — the lookup simply finds nothing, which used to present as
    'this scenario has no script' and made the clone ship no script at all."""
    return "genpython\\%s\\test\\map\\%s\\%s\\effetmap.xyz" % (
        buildid, map_dir, scripting_folder(scenario_name))


def _script_folder_path(map_dir, folder, buildid):
    return "genpython\\%s\\test\\map\\%s\\%s\\effetmap.xyz" % (buildid, map_dir, folder)


def generate_scenario(dm, gd, map_dir, src_scn, new_scn, new_name,
                      tracking_id, kind="mp", game_type=None, nb_players=None, new_guid=None,
                      src_folder=None, new_folder=None, new_description=None, zz=None,
                      ia=None, set_props=None, op_name=None, op_subtitle=None, op_briefing=None,
                      menu_group="same"):
    """Build a complete new scenario of any kind by cloning an existing one on the same terrain.

    dm = DataMap_Win.dat, gd = ZZ_GladPatchableWin.dat (edata). kind = mp|campaign|operation.
    src_folder/new_folder = glad scenario folder names; if omitted they are derived by CONVENTION from
    the scenario stems — irregular maps (e.g. supercrossroads4's bare 'Scenario' folder loads
    leveldesign_normal) MUST pass src_folder explicitly (the editor reads it from the binding).
    For campaign/operation, pass `ia` (IA_Common.dat) to clone the paired .xyz mission script verbatim
    (the script binds to placements by Name, which the clone preserves).

    Returns a plan: {datamap_add, glad_add, glad_mod, guid, ids[, ia_add][, zz_mod]} of vpath -> bytes.
    The KDT + zone geometry + (for campaign/op) the script are REUSED from src (clone-and-tweak).
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
            if key == "scenario":
                raise ValueError(f"source scenario not found: {src_dp[key]}")
            continue       # campath/kdt are optional on some scenarios
        datamap_add[new_dp[key]] = b   # reuse geometry/KDT verbatim

    # ── The cluster is not a text substitution.  Rewriting the strings redirects the .scenario reference
    # (and a pile of dead Editor-only refs) but leaves the SCRIPTING folder pointing at the source, in all
    # 85 shipped clusters — so the clone would ship a script the game never puts on its python path and
    # keep running the source's.  The scripting folder is therefore read off the SOURCE cluster and
    # re-pointed explicitly, through the named link operations in `edits`.
    from . import edits as _edits
    from . import scenario_chain as _chain

    src_cluster_bytes = gd.get(src_gp["clustermap"])
    if src_cluster_bytes is None:
        raise ValueError(f"source glad clustermap not found: {src_gp['clustermap']}")
    try:
        src_folders = _cluster_scripting_folders(ndfbin.read(src_cluster_bytes), map_dir)
    except Exception:
        src_folders = []
    new_script_folder = scripting_folder(new_scn)

    glad_add = {}
    relink = {"script": [], "dico": []}
    for key in ("clustermap", "mapia"):
        b = gd.get(src_gp[key])
        if b is None:
            raise ValueError(f"source glad {key} not found: {src_gp[key]}")
        ndf = ndfbin.read(b)
        _subst_strings(ndf, reps)
        _subst_widestr(ndf, reps)
        if key == "clustermap":
            for old_folder in src_folders:
                if old_folder.lower() != new_script_folder.lower():
                    relink["script"] += _edits.point_cluster_at_script(
                        ndf, old_folder, new_script_folder).changes
                    relink["dico"] += _edits.point_cluster_at_dico(
                        ndf, old_folder, new_script_folder).changes
        glad_add[new_gp[key]] = ndfbin.write(ndf, compress=True)

    # campaign/operation: clone the paired mission script into the folder the NEW cluster now names.
    buildid = _chain.discover_script_buildid(_IaAdapter(ia)) if ia is not None else "1000"
    ia_add = {}
    if kind in ("campaign", "operation") and ia is not None:
        sb = None
        for folder in (src_folders or [scripting_folder(src_scn)]):
            p = _script_folder_path(map_dir, folder, buildid)
            sb = ia.get(p) or ia.get(p.replace("\\", "/"))
            if sb is not None:
                break
        if sb is None:
            raise ValueError(
                "no effetmap.xyz found for %s/%s under %s (build id %s) — a campaign or operation cannot "
                "run without its script" % (map_dir, src_scn, src_folders or "?", buildid))
        ia_add[_script_folder_path(map_dir, new_script_folder, buildid)] = sb

    # the in-mission text tables live under the scripting folder too, so they move with it
    loc_add = {}
    if zz is not None and src_folders:
        loc_add = _clone_scenario_dicos(zz, map_dir, src_folders[0], new_script_folder)

    zz_mod = {}
    loc_props = None
    # Operation/campaign selection-screen text -> flash_txt.dic (present + writable). Sets the operation
    # NAME (Description), date/place (LongDescription1) and briefing (LongDescription2).
    if kind in ("operation", "campaign") and zz is not None and op_name:
        labeled = [("Description", op_name)]
        if op_subtitle:
            labeled.append(("LongDescription1", op_subtitle))
        if op_briefing:
            labeled.append(("LongDescription2", op_briefing))
        keys, zz_mod = apply_loc_strings(zz, labeled)
        loc_props = keys
    elif zz is not None and new_description is None and kind == "mp":
        new_description, zz_mod = apply_custom_name(zz, new_name)

    m_ndf = ndfbin.read(gd.get(MAPINFO_PATH))
    g_ndf = ndfbin.read(gd.get(GLOBALS_PATH))
    sp = dict(set_props or {})
    if kind == "mp":
        if game_type is not None: sp.setdefault("GameType", game_type)
        if nb_players is not None: sp.setdefault("NbPlayers", nb_players)
    ids = register_cloned_scenario(m_ndf, g_ndf, map_dir, src_scn, new_scn, src_folder, new_folder,
                                   new_name, new_guid, tracking_id, kind=kind, set_props=sp,
                                   new_description=new_description, loc_props=loc_props)

    # Where the copy sits in the menu.  A clone inherits the source's group, which puts custom content in
    # among the shipped missions; 'new' gives it a group of its own at the end of the list instead
    # (next_group_value = max existing + 1).  The group key is CategoryId for all three kinds.
    if menu_group and menu_group != "same":
        from .scenario_registry import next_group_value, group_prop_for
        prop = group_prop_for(kind)
        value = next_group_value(g_ndf, kind, ids[2]) if menu_group == "new" else int(menu_group)
        _set_raw(g_ndf, g_ndf.instances[ids[1]], prop, value)
    # Groups are contiguous runs in the pack list, so a record carrying an EXISTING group's key has to sit
    # inside that run, not after every other group.  This is the normal case for a campaign mission: it
    # belongs at the end of its own campaign's group, never at the end of the whole chapter list.
    from . import edits as _edits2
    _edits2.place_at_end_of_group(g_ndf, kind, ids[2], ids[1])
    # Every remaining LocHash on the cloned record still points at the SOURCE's text.  Give the copy its
    # own keys (seeded by its identity, so re-cloning is idempotent) carrying a copy of the source's words
    # in every language — otherwise editing the copy's briefing rewrites the original's.
    minted = {}
    if zz is not None:
        zz_mod, minted = _clone_record_text(
            zz, g_ndf, ids[1], "%s\\%s" % (map_dir, new_scn), set(loc_props or {}), already=zz_mod)

    glad_mod = {
        MAPINFO_PATH: ndfbin.write(m_ndf, compress=True),
        GLOBALS_PATH: ndfbin.write(g_ndf, compress=True),
    }
    plan = {"datamap_add": datamap_add, "glad_add": glad_add,
            "glad_mod": glad_mod, "guid": new_guid, "ids": ids,
            "relink": relink, "script_folder": new_script_folder, "buildid": buildid,
            "text_keys": minted}
    if ia_add:
        plan["ia_add"] = ia_add            # {vpath: .xyz bytes} -> write into IA_Common.dat
    # The per-language in-mission .dic files move with the scripting folder, so they are ADDS in the same
    # dat the flash_txt edits land in.  Keep them separate from zz_mod: those are edits to existing files,
    # these are new entries.
    if loc_add:
        plan["zz_add"] = loc_add
    if zz_mod:
        plan["zz_mod"] = zz_mod            # {vpath: flash_txt.dic bytes} -> write into ZZ_Win.dat
    return plan

"""Scenario classification — the read-only layer that pairs every map/scenario file with the registry
record that types it (multiplayer / campaign / operation / unbound).

A scenario's kind is decided entirely by which info record's GUID matches its TMapLoadInfo:
  TMapLoadInfo (mapinfo.cpp, GUID-keyed, top_object)
    --GUID--> TMultiMapInfo (mp)  | TChapterMapInfo (campaign) | TChallengeMapInfo (operation)
    each held by a pack via a child-ref list, enumerated by one manager (see REGISTRY_KINDS).
The TMapLoadInfo reaches the scenario FILE through ClusterLoads -> a reachable string
'Scenario\\<map>\\scenario_<suffix>\\ClusterMap'; folder 'scenario_<suffix>' <-> file
'leveldesign_<suffix>.scenario' <-> script 'scripting_<suffix>'. (All RE-confirmed; see
docs/map_editor/registry_kinds.md + scripting.md.)

This module is the productionised form of ai_unit_tests/claude/build_scenario_catalog.py. It is pure
read: it never mutates the dats. The write side (registering a NEW scenario of any kind) lives in
scenario_gen.py and consumes the same REGISTRY_KINDS table.
"""

import os
import re
from dataclasses import dataclass, field
from typing import Optional, List, Dict

from . import ndfbin
from .ndfbin import T, OBJ_REF_MARKER
from .scenario_gen import _get_prop, _set_raw, _reachable_strings, MAPINFO_PATH, GLOBALS_PATH


# ── Registry descriptor — one row per kind; drives both classify (here) and register (scenario_gen) ──
@dataclass(frozen=True)
class RegistryKind:
    kind: str                 # "mp" | "campaign" | "operation"
    info_class: str           # globals.cpp class GUID-linked to TMapLoadInfo
    guid_prop: str            # GUID property name (literally "GUID" on every class)
    pack_class: str           # class whose child-list holds the info records
    pack_list_prop: str       # the child-ref List property on the pack
    manager_class: str        # top-level manager enumerating the packs
    manager_list_prop: str    # the pack-ref List property on the manager
    tracking_prop: str        # human id ("MP31"/"M01"/"CH26")
    info_is_top_object: bool  # info records are NEVER top_objects in the shipped data


REGISTRY_KINDS: Dict[str, RegistryKind] = {
    "mp": RegistryKind(
        "mp", "TMultiMapInfo", "GUID",
        "TMultiPack", "MultiList", "TMultiPackManager", "MultiPackList",
        "TrackingId", False),
    "campaign": RegistryKind(
        "campaign", "TChapterMapInfo", "GUID",
        "TChapterPack", "ChapterList", "TChapterPackManager", "ChapterPackList",
        "TrackingId", False),
    "operation": RegistryKind(
        "operation", "TChallengeMapInfo", "GUID",
        "TChallengePack", "ChallengeList", "TChallengePackManager", "ChallengePackList",
        "TrackingId", False),
}

# detail fields worth surfacing per kind (best-effort; absent ones are skipped)
_DETAIL_PROPS = {
    "mp": ("TrackingId", "GameType", "NbPlayers", "GameModeMulti", "MapSize",
           "DispoLadder1v1", "DispoLadder2v2", "DispoMulti2Teams", "DispoMulti3Teams",
           "DispoMulti4Teams", "DispoMultiFFA"),
    "campaign": ("TrackingId", "ChapterId", "CategoryId", "NbSecondaryObjectives",
                 "PopCapPlayer", "PopCapIA"),
    "operation": ("TrackingId", "CategoryId", "NbPlayers", "PopCapPlayer", "PopCapIA", "PrivilegeId"),
}

_INFO_CLASS_TO_KIND = {rk.info_class: k for k, rk in REGISTRY_KINDS.items()}


@dataclass
class ScenarioBinding:
    """One typed pairing of a terrain + scenario file + the registry record that types it."""
    map_dir: str
    scenario_name: Optional[str]          # "leveldesign[_suffix]" stem, or None if registry-only
    kind: str                             # "mp" | "campaign" | "operation" | "unbound"
    tmli_idx: Optional[int] = None
    info_idx: Optional[int] = None
    info_class: Optional[str] = None
    guid: Optional[str] = None            # hex
    name: Optional[str] = None            # TMapLoadInfo.Name (free text)
    tracking_id: Optional[str] = None
    glad_folder: Optional[str] = None     # "scenario[_suffix]"
    pack_idx: Optional[int] = None
    detail: dict = field(default_factory=dict)
    files: dict = field(default_factory=dict)   # {scenario,campath,kdt,script: vpath} + *_exists
    has_file: bool = True                 # scenario file present in DataMap
    has_script: bool = False
    # ALL registry records that reference this scenario file (a file can be MP + campaign + operation
    # at once, or be reused by a test harness). The fields above mirror the PRIMARY registration.
    registrations: list = field(default_factory=list)

    @property
    def kinds(self) -> set:
        """Every kind this scenario file is registered as (usually one)."""
        return {r["kind"] for r in self.registrations} or {self.kind}

    @property
    def suffix(self) -> str:
        s = self.scenario_name or ""
        return s[len("leveldesign"):] if s.startswith("leveldesign") else ""


# ── linkage helpers ──────────────────────────────────────────────────────────────
_REACH_RE = re.compile(r"Scenario\\([^\\]+)\\(scenario[^\\]*)\\ClusterMap", re.I)


def _guid_hex(ndf, inst, prop="GUID") -> Optional[str]:
    gv = _get_prop(ndf, inst, prop)
    if gv is None:
        return None
    if gv.type_id in (T.Guid, T.Hash) or (gv.type_id == T.Blob and len(gv.raw) == 16):
        return bytes(gv.raw).hex()
    return None


def _str_prop(ndf, inst, prop) -> Optional[str]:
    v = _get_prop(ndf, inst, prop)
    if v is None:
        return None
    if v.type_id in (T.StringRef, T.PathRef):
        return ndf.get_string(v.raw)
    return v.raw


def folder_to_stem(folder: str) -> str:
    """CONVENTION fallback: glad folder 'scenario' -> 'leveldesign'; 'scenario_chapter1' ->
    'leveldesign_chapter1'.  NOTE this is only a heuristic — irregular maps break it (e.g.
    supercrossroads4's bare 'Scenario' folder loads 'leveldesign_normal', and m03_italie_demo_final's
    'scenario_demo_final' loads 'leveldesign'). The authoritative source is the .scenario filename
    named inside the folder's clustermap NDF (see _scenario_stems_from_clustermap)."""
    suffix = folder[len("scenario"):] if folder.lower().startswith("scenario") else ""
    return "leveldesign" + suffix


_CLUSTERMAP_RE = re.compile(
    r"genglad\\patchable\\scenario\\([^\\]+)\\([^\\]+)\\clustermap\.cpp\.gladndfbin$", re.I)


def _clustermap_index(gd) -> Dict[tuple, str]:
    """{(map_lower, folder_lower): actual_clustermap_vpath} from the glad dat's entry list."""
    out = {}
    try:
        names = gd.list()
    except Exception:
        return out
    for vp in names:
        v = vp.replace("/", "\\")
        m = _CLUSTERMAP_RE.match(v)
        if m:
            out[(m.group(1).lower(), m.group(2).lower())] = vp
    return out


def _scenario_stems_from_clustermap(gd, vpath, cache) -> list:
    """The .scenario stem(s) the clustermap NDF actually loads (authoritative). Cached per vpath."""
    if vpath in cache:
        return cache[vpath]
    stems = []
    try:
        ndf = ndfbin.read(gd.get(vpath))
        seen = set()
        def add(s):
            if s and s.lower().endswith(".scenario"):
                stem = s.replace("/", "\\").split("\\")[-1][:-len(".scenario")]
                if stem.lower() not in seen:
                    seen.add(stem.lower()); stems.append(stem)
        for s in ndf.strings:
            add(s)
        for inst in ndf.instances:
            for pv in inst.props:
                v = pv.value
                if v.type_id == T.WideStr and isinstance(v.raw, str):
                    add(v.raw)
    except Exception:
        stems = []
    cache[vpath] = stems
    return stems


def _datamap_paths(map_dir: str, stem: str) -> dict:
    return {
        "scenario": "test\\map\\%s\\%s.scenario" % (map_dir, stem),
        "campath": "test\\map\\%s\\campath\\campaths_%s.ndfbin" % (map_dir, stem),
        "kdt": "test\\map\\%s\\zonebluff\\%s.kdt" % (map_dir, stem),
    }


def _script_path(map_dir: str, stem: str, buildid: str = "10000") -> str:
    suffix = stem[len("leveldesign"):] if stem.startswith("leveldesign") else ""
    return "genpython\\%s\\test\\map\\%s\\scripting%s\\effetmap.xyz" % (buildid, map_dir, suffix)


def _pack_index_for_info(g_ndf, rk: RegistryKind, info_idx: int) -> Optional[int]:
    """Index of the pack whose child-ref list contains info_idx."""
    for pidx, pinst in g_ndf.find_instances(rk.pack_class):
        ml = _get_prop(g_ndf, pinst, rk.pack_list_prop)
        if ml is None or ml.type_id != T.List:
            continue
        for v in ml.raw:
            if (v.type_id == T.Reference and isinstance(v.raw, tuple)
                    and v.raw[0] == OBJ_REF_MARKER and v.raw[1][0] == info_idx):
                return pidx
    return None


def _scenario_files(dm) -> List[tuple]:
    out = []
    for vp in dm.list():
        v = vp.replace("/", "\\")
        m = re.match(r"test\\map\\([^\\]+)\\([^\\]+)\.scenario$", v, re.I)
        if m:
            out.append((m.group(1), m.group(2), vp))
    return sorted(out)


def open_registry(gd):
    """Read (mapinfo.cpp, globals.cpp) NDFs from an opened ZZ_GladPatchableWin.dat edata."""
    m_ndf = ndfbin.read(gd.get(MAPINFO_PATH))
    g_ndf = ndfbin.read(gd.get(GLOBALS_PATH))
    return m_ndf, g_ndf


# ── public API ───────────────────────────────────────────────────────────────────
def build_bindings(m_ndf, g_ndf, dm, gd=None, ia=None) -> Dict[str, List[ScenarioBinding]]:
    """Classify every scenario in the build. Returns {map_dir: [ScenarioBinding]} covering every
    scenario FILE present in DataMap (incl. 'unbound' files in no menu) plus any registry entry whose
    scenario file is missing (has_file=False).

    `gd` = the opened ZZ_GladPatchableWin.dat (or an adapter with .list()/.get()). When given, the
    scenario file each TMapLoadInfo points at is read AUTHORITATIVELY from the folder's clustermap NDF
    (handles irregular maps like supercrossroads4). Without it we fall back to the folder-name
    convention (which mis-binds those maps). `ia` (IA_Common.dat) populates has_script."""
    ia_set = None
    if ia is not None:
        ia_set = {p.replace("/", "\\").lower() for p in ia.list()}
    # existing scenario files (for disambiguating multi-candidate clustermaps)
    dm_files = {(mp.lower(), st.lower()) for mp, st, _ in _scenario_files(dm)}
    cl_index = _clustermap_index(gd) if gd is not None else {}
    cl_cache = {}
    # 1) GUID -> (kind, info_idx, info_class, tracking, detail, pack_idx)
    guid_info = {}
    for kind, rk in REGISTRY_KINDS.items():
        for idx, inst in g_ndf.find_instances(rk.info_class):
            gh = _guid_hex(g_ndf, inst, rk.guid_prop)
            if not gh:
                continue
            detail = {}
            for p in _DETAIL_PROPS[kind]:
                v = _get_prop(g_ndf, inst, p)
                if v is not None:
                    detail[p] = ndf_val(g_ndf, v)
            guid_info[gh] = {
                "kind": kind, "info_idx": idx, "info_class": rk.info_class,
                "tracking": _str_prop(g_ndf, inst, rk.tracking_prop),
                "detail": detail,
                "pack_idx": _pack_index_for_info(g_ndf, rk, idx),
            }

    # 2) TMapLoadInfo -> registration, GROUPED BY scenario file. A single scenario file can carry
    #    SEVERAL registry records (proven: m02_tunisie's 'leveldesign_chapter1' is referenced by both
    #    the M02 campaign chapter AND a 'testauto' harness; in general a file may be MP + campaign +
    #    operation at once). So we collect a LIST per (map, stem) and never overwrite.
    reg_by_file = {}        # (map_lower, stem_lower) -> [registration dict, ...]
    for idx, inst in m_ndf.find_instances("TMapLoadInfo"):
        path = _str_prop(m_ndf, inst, "Path")
        name = _str_prop(m_ndf, inst, "Name")
        gh = _guid_hex(m_ndf, inst, "GUID")
        folder = None
        for s in _reachable_strings(m_ndf, idx, max_depth=6):
            mm = _REACH_RE.search(s)
            if mm:
                folder = mm.group(2)
                break
        if not (path and folder):
            continue
        # Authoritative stem from the folder's clustermap NDF; fall back to the folder convention.
        stem = None
        cl_vp = cl_index.get((path.lower(), folder.lower()))
        if cl_vp is not None:
            cands = _scenario_stems_from_clustermap(gd, cl_vp, cl_cache)
            stem = next((c for c in cands if (path.lower(), c.lower()) in dm_files),
                        cands[0] if cands else None)
        if stem is None:
            stem = folder_to_stem(folder)
        info = guid_info.get(gh)
        reg_by_file.setdefault((path.lower(), stem.lower()), []).append({
            "tmli_idx": idx, "name": name, "guid": gh, "folder": folder,
            "map": path, "stem": stem,
            "kind": info["kind"] if info else "unbound",
            "info_idx": info["info_idx"] if info else None,
            "info_class": info["info_class"] if info else None,
            "tracking": info["tracking"] if info else None,
            "detail": info["detail"] if info else {},
            "pack_idx": info["pack_idx"] if info else None,
        })

    # 3) every scenario file -> one binding (merging all its registrations)
    out: Dict[str, List[ScenarioBinding]] = {}
    seen_reg = set()
    for map_dir, stem, vp in _scenario_files(dm):
        key = (map_dir.lower(), stem.lower())
        regs = reg_by_file.get(key, [])
        out.setdefault(map_dir, []).append(_make_binding(map_dir, stem, regs, True, ia_set))
        if regs:
            seen_reg.add(key)

    # 4) registrations whose scenario file is missing (registry-without-file)
    for key, regs in reg_by_file.items():
        if key in seen_reg:
            continue
        out.setdefault(regs[0]["map"], []).append(
            _make_binding(regs[0]["map"], regs[0]["stem"], regs, False, ia_set))

    return out


# kind priority when one scenario file carries multiple registry records (pick the "primary" identity)
_KIND_PRIORITY = {"campaign": 3, "operation": 2, "mp": 1, "unbound": 0}


def ndf_val(ndf, v):
    if v.type_id in (T.StringRef, T.PathRef):
        return ndf.get_string(v.raw)
    if v.type_id in (T.Guid, T.Hash):
        return bytes(v.raw).hex()
    return v.raw


def _make_binding(map_dir, stem, regs, has_file, ia_set=None) -> ScenarioBinding:
    """Build one binding for a scenario file from ALL the registry records (regs) that point at it.
    The PRIMARY identity is the highest-priority typed record (campaign > operation > mp > unbound)."""
    regs = regs or []
    primary = max(regs, key=lambda r: _KIND_PRIORITY.get(r["kind"], 0)) if regs else None
    files = _datamap_paths(map_dir, stem)
    script = _script_path(map_dir, stem)
    files["script"] = script
    has_script = (script.lower() in ia_set) if ia_set is not None else False
    p = primary or {}
    return ScenarioBinding(
        map_dir=map_dir,
        scenario_name=stem,
        kind=p.get("kind", "unbound"),
        tmli_idx=p.get("tmli_idx"),
        info_idx=p.get("info_idx"),
        info_class=p.get("info_class"),
        guid=p.get("guid"),
        name=p.get("name"),
        tracking_id=p.get("tracking"),
        glad_folder=p.get("folder"),
        pack_idx=p.get("pack_idx"),
        detail=p.get("detail", {}),
        files=files,
        has_file=has_file,
        has_script=has_script,
        registrations=regs,
    )


def classify_map(gd, dm, map_dir: str, ia=None) -> List[ScenarioBinding]:
    """Convenience: classify one terrain. For repeated use, call build_bindings once and cache.
    `gd` is the opened ZZ_GladPatchableWin.dat — used both for the registry NDFs and (authoritatively)
    the per-folder clustermaps."""
    m_ndf, g_ndf = open_registry(gd)
    return build_bindings(m_ndf, g_ndf, dm, gd=gd, ia=ia).get(map_dir, [])


# ── selection-list ordering (the order entries appear in the in-game menu) ─────────
def _pack_list_value(g_ndf, kind, pack_idx):
    rk = REGISTRY_KINDS[kind]
    if pack_idx is None or pack_idx >= len(g_ndf.instances):
        return None
    return _get_prop(g_ndf, g_ndf.instances[pack_idx], rk.pack_list_prop)


def pack_child_order(g_ndf, kind, pack_idx) -> List[int]:
    """Ordered [info_idx, ...] in the kind's pack child-ref list — i.e. the menu order. The position of
    a scenario's info record in this list is where it shows up in the selection screen."""
    ml = _pack_list_value(g_ndf, kind, pack_idx)
    if ml is None or ml.type_id != T.List:
        return []
    out = []
    for v in ml.raw:
        if (v.type_id == T.Reference and isinstance(v.raw, tuple)
                and v.raw[0] == OBJ_REF_MARKER):
            out.append(v.raw[1][0])
    return out


def manager_pack_order(g_ndf, kind) -> List[int]:
    """Ordered [pack_idx, ...] the kind's manager enumerates (TMultiPackManager.MultiPackList etc.).
    The MP menu is split across 3 packs (base / DLC / pre-order) — all enumerated here."""
    rk = REGISTRY_KINDS[kind]
    mgrs = g_ndf.find_instances(rk.manager_class)
    if not mgrs:
        return []
    ml = _get_prop(g_ndf, g_ndf.instances[mgrs[0][0]], rk.manager_list_prop)
    if ml is None or ml.type_id != T.List:
        return []
    return [v.raw[1][0] for v in ml.raw
            if v.type_id == T.Reference and isinstance(v.raw, tuple) and v.raw[0] == OBJ_REF_MARKER]


def full_menu_order(g_ndf, kind) -> List[tuple]:
    """The WHOLE selection list as the menu shows it: [(info_idx, pack_idx), ...] across EVERY pack the
    manager enumerates, in order. For MP this is all 30 maps (base 24 + DLC 3 + pre-order 3), not just
    one pack. The game GROUPS by CategoryId (mp: None/1/2/3 = 2 / 3-4 / 6 / 8 players)."""
    out = []
    for pidx in manager_pack_order(g_ndf, kind):
        for info_idx in pack_child_order(g_ndf, kind, pidx):
            out.append((info_idx, pidx))
    return out


def move_in_pack(g_ndf, kind, pack_idx, info_idx, delta) -> Optional[int]:
    """Move the entry for info_idx by `delta` slots within its pack list (reorders the menu, ignoring
    groups). Mutates the list in place (caller marks the globals NDF dirty). Returns new position."""
    ml = _pack_list_value(g_ndf, kind, pack_idx)
    if ml is None or ml.type_id != T.List:
        return None
    lst = list(ml.raw)
    pos = next((i for i, v in enumerate(lst)
                if v.type_id == T.Reference and isinstance(v.raw, tuple)
                and v.raw[0] == OBJ_REF_MARKER and v.raw[1][0] == info_idx), None)
    if pos is None:
        return None
    new = max(0, min(len(lst) - 1, pos + delta))
    if new != pos:
        lst.insert(new, lst.pop(pos))
        ml.raw[:] = lst
    return new


# ── group-aware ordering ──────────────────────────────────────────────────────────────────
# The menu groups entries by **CategoryId** for ALL kinds — RE-confirmed (NOT NbPlayers):
#   mp        -> CategoryId  (None=2-player/1v1, 1=3-4 players, 2=6 players, 3=8 players — note 3&4 MERGE)
#   operation -> CategoryId  (1=1v1, 2=1-vs-All, 3=Cooperative)
#   campaign  -> CategoryId  (mission groups 1..6)
# (NbPlayers correlates for MP but does NOT define the group — Cat 1 holds both 3p and 4p maps. The
#  group LABEL is engine-derived; for MP we compute a player-count label from the bucket's members.)
GROUP_PROP = {"mp": "CategoryId", "operation": "CategoryId", "campaign": "CategoryId"}
OPERATION_CATEGORIES = {1: "1v1", 2: "1 vs All", 3: "Cooperative"}
# MP CategoryId -> the player-count group label (RE-confirmed; cat 1 MERGES 3- and 4-player maps).
MP_CATEGORY_LABELS = {None: "2 players", 1: "3 - 4 players", 2: "6 players", 3: "8 players"}
# CategoryId -> canonical NbPlayers, per kind, so the player count CORRELATES with the group the user
# picks (RE'd: e.g. an Operation in 'Cooperative' has 2-3 players, not 1). Campaign omitted (no count).
# Ambiguous categories keep an already-valid existing count instead of forcing one.
# MP player count -> its CategoryId list group (None=2p base; 3&4 share group 1). Used by the GAME MODES
# section so changing the player count can OFFER to move the map to the matching group. The list group is
# VISUAL ONLY — modders can also set it freely in the Mission Logic tab without changing the player count.
MP_CATEGORY_FOR_NB = {2: None, 3: 1, 4: 1, 6: 2, 8: 3}


def group_prop_for(kind) -> str:
    return GROUP_PROP.get(kind, "CategoryId")


def group_label(kind, value) -> str:
    if kind == "operation":
        return OPERATION_CATEGORIES.get(value, "Category %s" % value)
    if kind == "campaign":
        return "Mission group %s" % (value if value is not None else "intro")
    if kind == "mp":
        return MP_CATEGORY_LABELS.get(value, "%s players" % value)
    return "Group %s" % value


def mp_category_for_nbplayers(nb):
    """The CategoryId an MP map carries to land in the selection-list group for its player count
    (None = 2-player base group; 3 & 4 share group 1). Falls back to the nearest bucket for odd counts."""
    if nb in MP_CATEGORY_FOR_NB:
        return MP_CATEGORY_FOR_NB[nb]
    if nb is None or nb <= 2:
        return None
    if nb <= 5:
        return 1
    if nb <= 6:
        return 2
    return 3


def find_pack_of(g_ndf, kind, info_idx) -> Optional[int]:
    """The pack_idx whose child-list contains info_idx (which pack a registry record lives in), or None."""
    for pidx in manager_pack_order(g_ndf, kind):
        if info_idx in pack_child_order(g_ndf, kind, pidx):
            return pidx
    return None


def _group_key(g_ndf, info_idx, prop):
    v = _get_prop(g_ndf, g_ndf.instances[info_idx], prop)
    return v.raw if v is not None else None


def _pack_refs_and_keys(g_ndf, kind, pack_idx, prop):
    ml = _pack_list_value(g_ndf, kind, pack_idx)
    if ml is None or ml.type_id != T.List:
        return None, [], []
    refs = list(ml.raw)
    order = [v.raw[1][0] if (v.type_id == T.Reference and isinstance(v.raw, tuple)
             and v.raw[0] == OBJ_REF_MARKER) else None for v in refs]
    keys = [_group_key(g_ndf, i, prop) if i is not None else None for i in order]
    return ml, refs, (order, keys)


def _group_span(keys, pos):
    """[start, end) of the maximal contiguous run of keys equal to keys[pos] around pos."""
    k = keys[pos]; s = pos; e = pos
    while s - 1 >= 0 and keys[s - 1] == k:
        s -= 1
    while e + 1 < len(keys) and keys[e + 1] == k:
        e += 1
    return s, e + 1


def move_within_group(g_ndf, kind, pack_idx, info_idx, delta, prop=None) -> Optional[int]:
    """Move info_idx by `delta` but CLAMPED to its contiguous group (e.g. stays among 8-player maps,
    or within its difficulty/mission group). Group key defaults to the kind's GROUP_PROP."""
    prop = prop or group_prop_for(kind)
    ml, refs, (order, keys) = _pack_refs_and_keys(g_ndf, kind, pack_idx, prop)
    if ml is None or info_idx not in order:
        return None
    pos = order.index(info_idx)
    s, e = _group_span(keys, pos)
    new = max(s, min(e - 1, pos + delta))
    if new != pos:
        refs.insert(new, refs.pop(pos)); ml.raw[:] = refs
    return new


def move_group(g_ndf, kind, pack_idx, info_idx, delta, prop=None) -> Optional[bool]:
    """Swap the whole contiguous group block containing info_idx with the adjacent one in the pack list.

    NOTE: this does NOT control in-game group order. The game orders groups by the KEY VALUE itself
    (player count ascending / category), regardless of pack order — proven by the DLC packs being stored
    key-unsorted yet still rendering in the correct groups. So this is NOT exposed in the editor; group
    membership is changed via `assign_group` (which sets the key). Kept for completeness/tests only.
    Returns True if swapped, False at the edge, None on error."""
    prop = prop or group_prop_for(kind)
    ml, refs, (order, keys) = _pack_refs_and_keys(g_ndf, kind, pack_idx, prop)
    if ml is None or info_idx not in order:
        return None
    # split into contiguous group blocks
    groups = []
    i = 0
    while i < len(refs):
        j = i
        while j < len(refs) and keys[j] == keys[i]:
            j += 1
        groups.append(refs[i:j]); i = j
    # which group block holds info_idx
    pos = order.index(info_idx)
    c = 0; gi = None
    for k, blk in enumerate(groups):
        if c <= pos < c + len(blk):
            gi = k; break
        c += len(blk)
    nj = gi + delta
    if nj < 0 or nj >= len(groups):
        return False
    groups[gi], groups[nj] = groups[nj], groups[gi]
    new = []
    for blk in groups:
        new.extend(blk)
    ml.raw[:] = new
    return True


def group_values(g_ndf, kind, pack_idx, prop=None) -> List:
    """The distinct group-key values present in a pack, in first-seen order (the groups themselves)."""
    prop = prop or group_prop_for(kind)
    _ml, _refs, (order, keys) = _pack_refs_and_keys(g_ndf, kind, pack_idx, prop)
    seen = []
    for k in keys:
        if k not in seen:
            seen.append(k)
    return seen


def next_group_value(g_ndf, kind, pack_idx, prop=None) -> int:
    """A fresh group value (max existing integer group key + 1) — for a modder's NEW group, so custom
    content sits in its own difficulty/mission group instead of mixing with the official ones."""
    vals = [v for v in group_values(g_ndf, kind, pack_idx, prop) if isinstance(v, int)]
    return (max(vals) + 1) if vals else 1


def assign_group(g_ndf, kind, pack_idx, info_idx, value, prop=None) -> Optional[int]:
    """Put info_idx into the group `value` (a new one creates a new group): set its group-key property,
    then relocate its pack entry to be contiguous with that group (end of that run, or end of pack if
    the value is new). Returns the new pack position. Mark globals dirty after."""
    prop = prop or group_prop_for(kind)
    inst = g_ndf.instances[info_idx]
    p = g_ndf.prop_by_name_and_class(prop, inst.class_index)
    if value is None:
        # the "base" group is keyed by an ABSENT property (MP's 2-player maps have no CategoryId) —
        # match the shipped data by removing it, not by writing a null.
        if p is not None:
            inst.remove(p.index)
    elif p is not None and inst.get(p.index) is not None:
        _set_raw(g_ndf, inst, prop, value)
    else:                       # property absent on this record — add it (e.g. a never-categorised entry)
        g_ndf.set_property(inst, prop, ndfbin.NdfValue(T.Int32, int(value)), create=True)
    ml, refs, (order, keys) = _pack_refs_and_keys(g_ndf, kind, pack_idx, prop)
    if ml is None or info_idx not in order:
        return None
    pos = order.index(info_idx)
    r = refs.pop(pos)
    keys2 = keys[:pos] + keys[pos + 1:]
    last = max((i for i, k in enumerate(keys2) if k == value), default=None)
    insert_at = (last + 1) if last is not None else len(refs)
    refs.insert(insert_at, r)
    ml.raw[:] = refs
    return insert_at

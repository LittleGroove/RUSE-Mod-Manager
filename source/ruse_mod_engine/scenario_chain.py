"""The scenario load chain, walked from the DATA rather than from filename conventions.

What the game actually does to get from a menu entry to a playable mission (our internal scenario notes):

    registration record (globals.cpp)
        GUID ═══ hash-map key ═══► TMapLoadInfo (mapinfo.cpp)
                                     ClusterLoads = MAP { 'Std' -> ref, 'WithoutRun' -> ref, ... }
                                        each -> TClusterWithNDFLoadedSubCluster
                                                  NdfTransaction.BaseName -> the SCENARIO cluster
                                                  SubClusterName          -> a path that cluster EXPORTS
        scenario cluster (genglad\\patchable\\scenario\\<map>\\<folder>\\clustermap.cpp.gladndfbin)
             TScenarioLoader.FileName        -> the .scenario  (placements + zones)
             TClusterAddPythonPath.DirectoryList -> where effetmap.xyz is found
             TLocalisationDicoResource.FileName  -> the in-mission .dic
             TNDFTransaction(NameSpace='ClusterTerrain').BaseName -> the TERRAIN cluster
        terrain cluster (genglad\\patchable\\map\\<map>\\clustermap.cpp.gladndfbin)
             TClusterMountMapDataPack.DataPack -> the terrain .dat

Why content-driven.  The convention-based walker in scenario_registry mis-handles the real data three ways,
all measured on the shipped build: it assumes a single-segment map dir (so the ten `flat/*` maps vanish), it
assumes folder name implies scenario name (17 of 102 scenario files load through another folder's cluster),
and it hardcodes the genpython build id (the live public build uses 1000, not 10000, so has_script came back
False for all 92 bindings).  Reading the cluster's own properties has none of those failure modes.

Every node reports whether it RESOLVES, and why not.  That is the point: the joins in this chain fail
silently in-game — an unmatched GUID means no menu entry with no error, and a script that is not under one of
the cluster's python paths simply never runs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import ndfbin as ndfbin_mod
from .ndfbin import T

# Dat keys, matching ModProject.DAT_FILES.
GLAD, MAPS, SCRIPTS, LOC = "gameplay", "maps", "scripts", "loc"

MAPINFO_PATH = r"genglad\patchable\mapinfo.cpp.gladndfbin"
GLOBALS_PATH = r"genglad\patchable\misc\globals.cpp.gladndfbin"

_SCN_CLUSTER_RE = re.compile(
    r"^genglad[\\/]patchable[\\/]scenario[\\/](?P<rest>.+)[\\/]clustermap\.cpp\.gladndfbin$", re.I)
_MAP_CLUSTER_RE = re.compile(
    r"^genglad[\\/]patchable[\\/]map[\\/](?P<map>.+)[\\/]clustermap\.cpp\.gladndfbin$", re.I)
_SCN_FILE_RE = re.compile(r"^test[\\/]map[\\/](?P<map>.+)[\\/](?P<stem>[^\\/]+)\.scenario$", re.I)

# Anything of the form "<mount>:\..." — DataDir:, MapDat:, DatasMap: ... — is a mount prefix, not a path.
_MOUNT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*:[\\/]")

OK, MISSING, MISMATCH, UNKNOWN = "ok", "missing", "mismatch", "unknown"


def _n(p: str) -> str:
    """Normalize a dat/NDF path for comparison: backslashes, lowercase, no leading separator."""
    return (p or "").replace("/", "\\").lstrip("\\").lower()


def _unmount(v: str) -> str:
    """Strip a 'DataDir:\\' / 'MapDat:\\' style mount prefix, leaving the archive-relative path."""
    return _MOUNT_RE.sub("", v or "")


# ── the walked tree ──────────────────────────────────────────────────────────────────────────────────

@dataclass
class ChainNode:
    kind: str                 # 'registration' | 'mapload' | 'clusterentry' | 'scenario-cluster' | ...
    label: str                # short human name for the step
    detail: str = ""          # the real path / value this node stands for
    status: str = UNKNOWN     # OK | MISSING | MISMATCH | UNKNOWN
    reason: str = ""          # plain-language why, when it is not OK
    ref: str = ""             # a resolved dat entry path, when there is one
    optional: bool = False    # absent BY DESIGN — see problems()
    children: List["ChainNode"] = field(default_factory=list)

    def add(self, node: "ChainNode") -> "ChainNode":
        self.children.append(node)
        return node

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def problems(self) -> List["ChainNode"]:
        """Links that are genuinely broken.

        Excludes absences the shipped data has too: a scenario declares dico files it does not always
        ship, and reporting those as faults would put a red mark on every operation in the game — which
        teaches a user to ignore red, and then the one that matters goes unread. See `optional`."""
        return [n for n in self.walk() if n.status in (MISSING, MISMATCH) and not n.optional]

    def render(self, indent: int = 0) -> str:
        mark = {OK: "ok  ", MISSING: "MISS", MISMATCH: "MISM", UNKNOWN: "?   "}[self.status]
        line = f"{'  ' * indent}{mark} {self.label}"
        if self.detail:
            line += f"  [{self.detail}]"
        if self.reason:
            line += f"  <- {self.reason}"
        return "\n".join([line] + [c.render(indent + 1) for c in self.children])


# ── store access ─────────────────────────────────────────────────────────────────────────────────────

class _Idx:
    """Cached, normalized entry-path index per dat, so a walk does not re-list an archive per lookup."""

    def __init__(self, store):
        self.store = store
        self._paths: Dict[str, List[str]] = {}
        self._norm: Dict[str, Dict[str, str]] = {}

    def paths(self, dat_key: str) -> List[str]:
        if dat_key not in self._paths:
            try:
                self._paths[dat_key] = list(self.store.entry_paths(dat_key))
            except Exception:
                self._paths[dat_key] = []
            self._norm[dat_key] = {_n(p): p for p in self._paths[dat_key]}
        return self._paths[dat_key]

    def exact(self, dat_key: str, path: str) -> Optional[str]:
        self.paths(dat_key)
        return self._norm[dat_key].get(_n(path))

    def ending(self, dat_key: str, suffix: str) -> List[str]:
        s = _n(suffix)
        return [p for p in self.paths(dat_key) if _n(p).endswith(s)]


def discover_script_buildid(store, default: str = "1000") -> str:
    """The `genpython\\<buildid>\\` segment THIS install uses.

    Never hardcode it.  The public build ships 1000; other branches differ, and getting it wrong is
    invisible — the lookup just returns nothing, so a scenario looks script-less and a clone silently
    ships no script at all."""
    idx = store if isinstance(store, _Idx) else _Idx(store)
    for p in idx.paths(SCRIPTS):
        parts = _n(p).split("\\")
        if len(parts) >= 2 and parts[0] == "genpython" and parts[1].isdigit():
            return parts[1]
    return default


def script_path(map_dir: str, scripting_folder: str, buildid: str) -> str:
    """Archive path of the effetmap for a given scripting FOLDER (not a scenario stem — the folder is what
    the cluster actually puts on the python path)."""
    return "genpython\\%s\\test\\map\\%s\\%s\\effetmap.xyz" % (buildid, map_dir, scripting_folder)


# ── cluster reading ──────────────────────────────────────────────────────────────────────────────────

@dataclass
class ClusterInfo:
    vpath: str
    map_dir: str = ""
    folder: str = ""
    scenario_file: Optional[str] = None            # TScenarioLoader.FileName, verbatim
    python_dirs: List[str] = field(default_factory=list)
    dico_files: List[str] = field(default_factory=list)
    terrain_basename: Optional[str] = None         # TNDFTransaction(NameSpace='ClusterTerrain').BaseName
    terrain_subcluster: Optional[str] = None       # the '$/ClusterTerrain/...' path it asks that cluster for
    exports: Dict[int, str] = field(default_factory=dict)
    transactions: List[Tuple[str, str]] = field(default_factory=list)   # (BaseName, NameSpace)
    terrain_dat: Optional[str] = None              # terrain clusters only: TClusterMountMapDataPack.DataPack

    @property
    def scenario_stem(self) -> Optional[str]:
        if not self.scenario_file:
            return None
        return _unmount(self.scenario_file).replace("/", "\\").rsplit("\\", 1)[-1][:-len(".scenario")]

    @property
    def map_python_dirs(self) -> List[str]:
        """Only the python paths belonging to THIS map.

        A cluster also puts the game's own framework packs on the path (`CodeIA\\Python\\Eugen`,
        `EugenSolo`, `EugenPatchable`, the sound pack).  Those are shared libraries, not this scenario's
        script directory, and treating them as one is how a re-point would unload the whole framework."""
        if not self.map_dir:
            return list(self.python_dirs)
        want = "\\map\\" + self.map_dir.replace("/", "\\").lower() + "\\"
        return [d for d in self.python_dirs
                if want in (_unmount(d).replace("/", "\\").rstrip("\\").lower() + "\\")]

    @property
    def scripting_folders(self) -> List[str]:
        """The trailing folder of each of THIS MAP's python paths — where effetmap.xyz has to live to be
        importable."""
        return [_unmount(d).replace("/", "\\").rstrip("\\").rsplit("\\", 1)[-1]
                for d in self.map_python_dirs]


def _sval(ndf, v) -> Optional[str]:
    if v is None:
        return None
    if v.type_id in (T.StringRef, T.PathRef):
        return ndf.get_string(v.raw)
    if v.type_id == T.WideStr and isinstance(v.raw, str):
        return v.raw
    return None


def _prop(ndf, inst, name):
    p = ndf.prop_by_name_and_class(name, inst.class_index) or ndf.prop_by_name(name)
    return inst.get(p.index) if p is not None else None


def read_cluster(store, vpath: str) -> Optional[ClusterInfo]:
    """Parse the parts of a clustermap NDF the chain depends on.  Returns None if it cannot be read."""
    try:
        raw = store.get_raw(GLAD, vpath)
        if raw is None:
            return None
        ndf = ndfbin_mod.read(raw)
    except Exception:
        return None

    info = ClusterInfo(vpath=vpath)
    m = _SCN_CLUSTER_RE.match(vpath.replace("/", "\\"))
    if m:
        rest = m.group("rest").replace("/", "\\")
        info.map_dir, _, info.folder = rest.rpartition("\\")
    else:
        mm = _MAP_CLUSTER_RE.match(vpath.replace("/", "\\"))
        if mm:
            info.map_dir = mm.group("map").replace("/", "\\")

    for _i, inst in ndf.find_instances("TScenarioLoader"):
        info.scenario_file = _sval(ndf, _prop(ndf, inst, "FileName")) or info.scenario_file
    for _i, inst in ndf.find_instances("TClusterAddPythonPath"):
        v = _prop(ndf, inst, "DirectoryList")
        if v is not None and v.type_id == T.List:
            for item in v.raw:
                s = _sval(ndf, item)
                if s:
                    info.python_dirs.append(s)
    for _i, inst in ndf.find_instances("TLocalisationDicoResource"):
        s = _sval(ndf, _prop(ndf, inst, "FileName"))
        if s:
            info.dico_files.append(s)
    for _i, inst in ndf.find_instances("TClusterMountMapDataPack"):
        info.terrain_dat = _sval(ndf, _prop(ndf, inst, "DataPack")) or info.terrain_dat

    # Cross-cluster references: a TNDFTransaction names another cluster by BaseName + NameSpace, and a
    # TClusterWithNDFLoadedSubCluster names WHICH exported subcluster of it to run.
    trans_by_idx = {}
    for i, inst in ndf.find_instances("TNDFTransaction"):
        base = _sval(ndf, _prop(ndf, inst, "BaseName"))
        ns = _sval(ndf, _prop(ndf, inst, "NameSpace"))
        trans_by_idx[i] = (base, ns)
        if base or ns:
            info.transactions.append((base or "", ns or ""))
        if ns and ns.lower() == "clusterterrain":
            info.terrain_basename = base
    for _i, inst in ndf.find_instances("TClusterWithNDFLoadedSubCluster"):
        sub = _sval(ndf, _prop(ndf, inst, "SubClusterName"))
        tv = _prop(ndf, inst, "NdfTransaction")
        tgt = None
        if tv is not None and tv.type_id == T.Reference and isinstance(tv.raw, tuple) \
                and isinstance(tv.raw[1], tuple):
            tgt = trans_by_idx.get(tv.raw[1][0])
        if sub and tgt and (tgt[1] or "").lower() == "clusterterrain":
            info.terrain_subcluster = sub

    try:
        info.exports = dict(ndf.export_ordinal_paths())
    except Exception:
        info.exports = {}
    return info


def scenario_cluster_paths(store) -> Dict[Tuple[str, str], str]:
    """{(map_dir_lower, folder_lower): vpath} for every scenario cluster.

    The map dir is everything between `scenario\\` and the final folder, so NESTED dirs such as
    `flat/afrique` are handled — the single-segment regex in scenario_registry drops all ten of them."""
    idx = store if isinstance(store, _Idx) else _Idx(store)
    out: Dict[Tuple[str, str], str] = {}
    for p in idx.paths(GLAD):
        m = _SCN_CLUSTER_RE.match(p.replace("/", "\\"))
        if not m:
            continue
        rest = m.group("rest").replace("/", "\\")
        map_dir, _, folder = rest.rpartition("\\")
        if map_dir and folder:
            out[(map_dir.lower(), folder.lower())] = p
    return out


def terrain_cluster_path(store, map_dir: str) -> Optional[str]:
    idx = store if isinstance(store, _Idx) else _Idx(store)
    return idx.exact(GLAD, "genglad\\patchable\\map\\%s\\clustermap.cpp.gladndfbin" % map_dir)


def scenario_files(store) -> List[Tuple[str, str, str]]:
    """[(map_dir, stem, vpath)] for every .scenario in DataMap, nested map dirs included."""
    idx = store if isinstance(store, _Idx) else _Idx(store)
    out = []
    for p in idx.paths(MAPS):
        m = _SCN_FILE_RE.match(p.replace("/", "\\"))
        if m:
            out.append((m.group("map"), m.group("stem"), p))
    return sorted(out)


def clusters_for_scenario(store, map_dir: str, stem: str) -> List[ClusterInfo]:
    """Every scenario cluster that LOADS `<map_dir>/<stem>.scenario`, found by reading each candidate's
    TScenarioLoader rather than by guessing the folder name.  Sorted by folder, so the result is stable.

    Reading rather than guessing matters twice over.  It handles the shipped scenarios whose folder name
    does not match their scenario name (supercrossroads4's bare `Scenario` folder loads
    `leveldesign_normal`).  And it refuses to invent an answer for a `.scenario` file that NO cluster
    names — `chess\\leveldesign_challenge` and `supercrossroads4\\leveldesign_challenge` are both orphans in
    DataMap, and the folder convention "resolves" them to a script that actually belongs to a different
    scenario on the same map.  A wrong answer is worse than none here.

    A file really can have more than one cluster: `m02_tunisie\\leveldesign_chapter1` is loaded by both
    `scenario_chapter1` and the `scenario_testauto` harness."""
    idx = store if isinstance(store, _Idx) else _Idx(store)
    want = stem.lower()
    out = []
    for (mdir, folder), vpath in sorted(scenario_cluster_paths(idx).items()):
        if mdir != map_dir.lower():
            continue
        info = read_cluster(idx.store, vpath)
        if info is not None and (info.scenario_stem or "").lower() == want:
            out.append(info)
    return out


def cluster_for_scenario(store, map_dir: str, stem: str) -> Optional[ClusterInfo]:
    """The PRIMARY cluster for a scenario: the one whose folder suffix matches the scenario's own suffix
    (`scenario_chapter1` for `leveldesign_chapter1`), else the first by folder name.  Deterministic, so a
    walk of the same data always reports the same chain."""
    cands = clusters_for_scenario(store, map_dir, stem)
    if not cands:
        return None
    suffix = stem[len("leveldesign"):].lower() if stem.lower().startswith("leveldesign") else ""
    for c in cands:
        folder_suffix = c.folder[len("scenario"):].lower() if c.folder.lower().startswith("scenario") else ""
        if folder_suffix == suffix:
            return c
    return cands[0]


# ── the walk ─────────────────────────────────────────────────────────────────────────────────────────

def walk(store, map_dir: str, stem: str) -> ChainNode:
    """Walk the whole chain for one scenario and report every join.

    Direction of travel is scenario -> cluster -> map-load-info -> registration, because that is the
    direction an author works in: the files exist and the question is whether the game can reach them."""
    idx = _Idx(store)
    buildid = discover_script_buildid(idx)
    root = ChainNode("scenario", f"{map_dir} / {stem}", status=OK)

    # 1. the .scenario file itself
    scn_entry = idx.exact(MAPS, "test\\map\\%s\\%s.scenario" % (map_dir, stem))
    root.add(ChainNode("scenario-file", "scenario file",
                       detail="test\\map\\%s\\%s.scenario" % (map_dir, stem),
                       status=OK if scn_entry else MISSING, ref=scn_entry or "",
                       reason="" if scn_entry else "not present in DataMap"))

    # 2. the scenario cluster that loads it
    info = cluster_for_scenario(idx, map_dir, stem)
    if info is None:
        root.add(ChainNode("scenario-cluster", "scenario cluster", status=MISSING,
                           reason="no clustermap on this map names this .scenario, so the game has no "
                                  "way to load it"))
        root.status = MISSING
        return root
    cl = root.add(ChainNode("scenario-cluster", "scenario cluster", detail=info.vpath, status=OK,
                            ref=info.vpath))

    # 2a. what the cluster says it loads
    cl.add(ChainNode("cluster-scenario", "names the .scenario", detail=info.scenario_file or "",
                     status=OK if info.scenario_file else MISSING,
                     reason="" if info.scenario_file else "cluster has no TScenarioLoader"))

    # 2b. python path -> the mission script.  A script that is not under one of these folders never runs.
    if not info.python_dirs:
        cl.add(ChainNode("script", "mission script", status=UNKNOWN,
                         reason="cluster adds no python path"))
    else:
        found = None
        for folder in info.scripting_folders:
            p = idx.exact(SCRIPTS, script_path(map_dir, folder, buildid))
            if p:
                found = (folder, p)
                break
        if found:
            cl.add(ChainNode("script", "mission script", detail=found[1], status=OK, ref=found[1]))
        else:
            cl.add(ChainNode("script", "mission script",
                             detail=", ".join(info.scripting_folders), status=MISSING,
                             reason="no effetmap.xyz under any python path this cluster adds "
                                    "(genpython\\%s\\test\\map\\%s\\<folder>\\)" % (buildid, map_dir)))

    # 2c. the in-mission text tables
    for csv in info.dico_files:
        stem_dic = _n(_unmount(csv))
        if stem_dic.endswith(".csv"):
            stem_dic = stem_dic[:-4] + ".dic"
        hits = idx.ending(LOC, "\\" + stem_dic) or idx.ending(LOC, stem_dic)
        langs = sorted({_lang_of(h) for h in hits})
        # Localization.csv is where objective text lives and every listed campaign/operation ships it
        # (38/38 measured), so a miss there is a real fault.  The others are optional by design:
        # Dialog.csv is cutscene speech and is campaign-only (24/24 campaigns, 0/14 operations), and the
        # base scripting\ dico on an MP map is declared but not shipped.  Flagging those as faults would
        # mark every operation in the game red.
        required = _n(stem_dic).endswith("localization.dic")
        node = cl.add(ChainNode("dico", "in-mission text", detail=csv,
                                status=OK if hits else MISSING,
                                ref=hits[0] if hits else "",
                                optional=not required and not hits,
                                reason="" if hits else
                                ("no .dic for it in any language, so this text is blank in game"
                                 if required else
                                 "not shipped for this scenario - normal unless the mission uses it")))
        if hits:
            node.add(ChainNode("dico-langs", "languages", detail=",".join(langs), status=OK))

    # 2d. the terrain
    tnode = cl.add(ChainNode("terrain-cluster", "terrain cluster",
                             detail=info.terrain_basename or "",
                             status=MISSING if not info.terrain_basename else UNKNOWN,
                             reason="" if info.terrain_basename
                                    else "cluster does not reference a ClusterTerrain namespace"))
    if info.terrain_basename:
        tpath = idx.exact(GLAD, "genglad\\" + _unmount(info.terrain_basename) + ".cpp.gladndfbin")
        if not tpath:
            tnode.status, tnode.reason = MISSING, "no terrain clustermap at that path"
        else:
            tnode.status, tnode.ref = OK, tpath
            tinfo = read_cluster(store, tpath)
            if tinfo is not None:
                # the subcluster the scenario asks for must be EXPORTED by the terrain cluster
                if info.terrain_subcluster:
                    exported = info.terrain_subcluster in set(tinfo.exports.values())
                    tnode.add(ChainNode(
                        "terrain-subcluster", "subcluster", detail=info.terrain_subcluster,
                        status=OK if exported else MISMATCH,
                        reason="" if exported else "the terrain cluster does not export this path, so the "
                                                   "join cannot resolve"))
                tnode.add(ChainNode("terrain-dat", "terrain data", detail=tinfo.terrain_dat or "",
                                    status=OK if tinfo.terrain_dat else MISSING,
                                    reason="" if tinfo.terrain_dat
                                           else "terrain cluster mounts no map data pack"))

    # 3. TMapLoadInfo -> which ClusterLoads entry points at this cluster
    m_ndf = _ndf(store, GLAD, MAPINFO_PATH)
    tmli = _find_mapload_for(m_ndf, info) if m_ndf is not None else None
    if tmli is None:
        root.add(ChainNode("mapload", "map slot (TMapLoadInfo)", status=MISSING,
                           reason="no TMapLoadInfo loads this cluster, so nothing can launch it"))
        return root
    tmli_idx, guid_hex, entries = tmli
    want_base = _n(_unmount(_cluster_basename_of(info)))
    ml = root.add(ChainNode("mapload", "map slot (TMapLoadInfo)",
                            detail="#%d GUID %s" % (tmli_idx, guid_hex or "(none)"),
                            status=OK if guid_hex else MISSING,
                            reason="" if guid_hex else "the slot has no GUID, so no menu entry can bind"))
    exported = set(info.exports.values())
    for name, base, sub in entries:
        node = ml.add(ChainNode("cluster-entry", "ClusterLoads[%s]" % name,
                                detail="%s -> %s" % (base or "?", sub or "?"), status=OK))
        # The subcluster is named by STRING and must be in the target cluster's export table, or the
        # join silently fails (our internal scenario notes 3.1).  Only entries pointing at
        # THIS cluster can be checked here; entries pointing elsewhere are another scenario's business.
        if base is not None and _n(_unmount(base)) == want_base and sub and sub not in exported:
            node.status = MISMATCH
            node.reason = "the scenario cluster does not export %s, so this entry cannot resolve" % sub

    # 4. the registration record that owns the GUID
    g_ndf = _ndf(store, GLAD, GLOBALS_PATH)
    if guid_hex and g_ndf is not None:
        reg = _find_registration(g_ndf, guid_hex)
        if reg is None:
            root.add(ChainNode("registration", "menu entry", status=MISSING,
                               reason="no TChallengeMapInfo / TChapterMapInfo / TMultiMapInfo carries this "
                                      "GUID, so the map is loadable but never listed"))
        else:
            kind, ridx, pack_idx = reg
            rn = root.add(ChainNode("registration", "menu entry (%s)" % kind,
                                    detail="#%d" % ridx, status=OK))
            rn.add(ChainNode("pack", "listed in a menu pack",
                             detail="pack #%s" % pack_idx if pack_idx is not None else "",
                             status=OK if pack_idx is not None else MISSING,
                             reason="" if pack_idx is not None
                                    else "the record is in no pack, so the menu never enumerates it"))
    return root


def _lang_of(path: str) -> str:
    from .localization import lang_from_path
    return lang_from_path(path)


def _ndf(store, dat_key, path):
    try:
        getter = getattr(store, "get_ndf", None)
        if getter is not None:
            return getter(dat_key, path)
        raw = store.get_raw(dat_key, path)
        return ndfbin_mod.read(raw) if raw else None
    except Exception:
        return None


def _guid_hex(ndf, inst, prop="GUID"):
    v = _prop(ndf, inst, prop)
    if v is None:
        return None
    if v.type_id in (T.Guid, T.Hash) or (v.type_id == T.Blob and len(v.raw) == 16):
        return bytes(v.raw).hex()
    return None


def _find_mapload_for(m_ndf, info: ClusterInfo):
    """(instance index, GUID hex, [(entry name, cluster BaseName, SubClusterName)]) for the TMapLoadInfo
    whose ClusterLoads reach `info`'s cluster.  Reads EVERY ClusterLoads entry — there are several
    ('Std', 'WithoutRun'), and taking only the first is how a walker loses half the chain."""
    want = _n(_unmount(_cluster_basename_of(info)))
    for idx, inst in m_ndf.find_instances("TMapLoadInfo"):
        v = _prop(m_ndf, inst, "ClusterLoads")
        if v is None or v.type_id != T.Map:
            continue
        entries = []
        matched = False
        for k, ref in v.raw:
            name = _sval(m_ndf, k) or ""
            base = sub = None
            if ref.type_id == T.Reference and isinstance(ref.raw, tuple) and isinstance(ref.raw[1], tuple):
                sc = m_ndf.instances[ref.raw[1][0]] if ref.raw[1][0] < len(m_ndf.instances) else None
                if sc is not None:
                    sub = _sval(m_ndf, _prop(m_ndf, sc, "SubClusterName"))
                    tv = _prop(m_ndf, sc, "NdfTransaction")
                    if tv is not None and tv.type_id == T.Reference and isinstance(tv.raw, tuple) \
                            and isinstance(tv.raw[1], tuple):
                        ti = tv.raw[1][0]
                        if ti < len(m_ndf.instances):
                            base = _sval(m_ndf, _prop(m_ndf, m_ndf.instances[ti], "BaseName"))
            entries.append((name, base, sub))
            if base and _n(_unmount(base)) == want:
                matched = True
        if matched:
            return idx, _guid_hex(m_ndf, inst), entries
    return None


def _cluster_basename_of(info: ClusterInfo) -> str:
    """The `Patchable\\Scenario\\<map>\\<folder>\\ClusterMap` name a TMapLoadInfo would use for this
    cluster, derived from its archive path."""
    p = info.vpath.replace("/", "\\")
    p = re.sub(r"^genglad\\", "", p, flags=re.I)
    return re.sub(r"\.cpp\.gladndfbin$", "", p, flags=re.I)


# Class/pack names come from scenario_registry.REGISTRY_KINDS rather than a second copy here, so the two
# modules can never drift apart on what a "campaign" record is.
def _registry_kinds():
    from .scenario_registry import REGISTRY_KINDS
    return REGISTRY_KINDS


def _find_registration(g_ndf, guid_hex: str):
    """(kind, instance index, pack index or None) for the record carrying `guid_hex`."""
    for kind, rk in _registry_kinds().items():
        for idx, inst in g_ndf.find_instances(rk.info_class):
            if _guid_hex(g_ndf, inst, rk.guid_prop) == guid_hex:
                return kind, idx, _pack_containing(g_ndf, kind, idx)
    return None


def _pack_containing(g_ndf, kind: str, info_idx: int) -> Optional[int]:
    rk = _registry_kinds()[kind]
    pack_cls, list_prop = rk.pack_class, rk.pack_list_prop
    for pidx, pinst in g_ndf.find_instances(pack_cls):
        v = _prop(g_ndf, pinst, list_prop)
        if v is None or v.type_id != T.List:
            continue
        for item in v.raw:
            if (item.type_id == T.Reference and isinstance(item.raw, tuple)
                    and isinstance(item.raw[1], tuple) and item.raw[1][0] == info_idx):
                return pidx
    return None

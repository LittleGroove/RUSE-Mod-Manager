"""Direct build-to-build version maps: does a mod's target differ between the build it was made for and the
build it's being deployed on?

Replaces the per-rmod `verify` field (which would bloat every rmod +3-10%) with ONE shared, precomputed map
per build-PAIR (built by tools/test_scripts/build_version_maps.py from the DAT_DATA_TREE).  The maps are
DIRECT (never chained), so A->C is exact.  A map lists, per NDF, the instances whose VALUE CHANGED between
the two builds (by durable identity: key / anchor / position), plus added/removed and index-shifts.

Runtime use (H2b + the compat dropdown): given a mod stamped for build A, deploying on build B, `stale_changes`
returns the changes whose target value moved between A and B — surface them ("this edit is based on a value
the game changed"), but still apply (false positives beat false negatives).
"""
import gzip
import json
import os
from typing import List, Optional, Tuple

from . import _resources


def maps_dir(override: Optional[str] = None):
    return override if override else str(_resources.data_dir() / "version_maps")


def map_path(a: str, b: str, override: Optional[str] = None) -> str:
    lo, hi = sorted([str(a), str(b)], key=lambda x: int(x))
    return os.path.join(maps_dir(override), f"v{lo}_v{hi}.json")


def _norm_dat(dat: str) -> str:
    """A map dat-key ('Data__PC__190852__ZZ_GladPatchableWin.dat') and a mod's pg.dat
    ('PC/190852/ZZ_GladPatchableWin.dat') to a common form."""
    d = dat.replace("\\", "/").replace("__", "/")
    if d.startswith("Data/"):
        d = d[len("Data/"):]
    return d.strip("/").lower()


def _norm_ndf(ndf: str) -> str:
    return ndf.replace("\\", "/").strip("/").lower()


class VersionMap:
    def __init__(self, raw: dict):
        self.a = raw.get("a")
        self.b = raw.get("b")
        # re-key ndf entries by (norm_dat, norm_ndf) so mod pg.dat/pg.ndf resolve regardless of slash/prefix
        self._ndfs = {}
        for key, entry in raw.get("ndfs", {}).items():
            dat, _, ndf = key.partition("::")
            self._ndfs[(_norm_dat(dat), _norm_ndf(ndf))] = entry

    def _entry(self, dat: str, ndf: str) -> dict:
        return self._ndfs.get((_norm_dat(dat), _norm_ndf(ndf)), {})

    def _changed(self, dat: str, ndf: str) -> dict:
        return self._entry(dat, ndf).get("changed", {})

    def remap_for(self, dat: str, ndf: str, from_build: str) -> dict:
        """{old_index: new_index} to translate positional refs from `from_build` to the other build.
        The stored remap is a->b (a = smaller build id); invert if from_build is b."""
        raw = self._entry(dat, ndf).get("remap", [])
        if str(from_build) == str(self.a):
            return {ia: ib for ia, ib in raw}
        return {ib: ia for ia, ib in raw}

    def target_changed(self, dat: str, ndf: str, match: Optional[dict]) -> bool:
        """Did the instance a change targets differ in value between the two builds?"""
        ch = self._changed(dat, ndf)
        if not ch:
            return False
        if match and "_index" in match:
            try:
                return int(match["_index"]) in set(ch.get("p", []))
            except (TypeError, ValueError):
                return False
        for _kp, kv in (match or {}).items():
            if kv in ch.get("k", []):
                return True
        return False


def load(a: str, b: str, override: Optional[str] = None) -> Optional[VersionMap]:
    p = map_path(a, b, override)
    for cand in (p + ".gz", p):   # prefer gzipped (shipped form), fall back to plain (dev)
        if os.path.isfile(cand):
            opener = gzip.open if cand.endswith(".gz") else open
            with opener(cand, "rt", encoding="utf-8") as f:
                return VersionMap(json.load(f))
    return None


def _remap_objref_insts(value, remap: dict) -> int:
    """Translate ObjRef {'inst': N} indices in a set-block value (recursing lists/pairs/maps). Returns count."""
    n = 0
    if isinstance(value, dict):
        if "inst" in value and "stable_ref" not in value and "local_id" not in value:
            try:
                oi = int(value["inst"])
                if oi in remap:
                    value["inst"] = remap[oi]; n += 1
            except (TypeError, ValueError):
                pass
        else:
            for v in value.values():
                n += _remap_objref_insts(v, remap)
    elif isinstance(value, (list, tuple)):
        for v in value:
            n += _remap_objref_insts(v, remap)
    return n


def remap_mod(mod, from_build: str, to_build: str, override: Optional[str] = None):
    """Return (translated_mod, report) — a COPY of mod with positional _index matches and ObjRef inst indices
    translated from from_build to to_build via the direct map, so the applier can run it verbatim on the
    target build.  report = {"map": bool, "remapped": int, "stale": [(pg, ch)], "from": .., "to": ..}.
    No-op copy when same build or no map (report["map"] False)."""
    import copy as _copy
    rep = {"map": False, "remapped": 0, "stale": [], "from": str(from_build), "to": str(to_build)}
    if not from_build or not to_build or str(from_build) == str(to_build):
        return mod, rep
    vmap = load(str(from_build), str(to_build), override)
    if vmap is None:
        return mod, rep
    rep["map"] = True
    new = _copy.deepcopy(mod)
    for pg in new.patches:
        remap = vmap.remap_for(pg.dat, pg.ndf, from_build)
        for ch in pg.changes:
            if ch.action in ("patch", "delete_props") and vmap.target_changed(pg.dat, pg.ndf, ch.match):
                rep["stale"].append((pg, ch))       # flag on the ORIGINAL (pre-remap) match
            if ch.match and "_index" in ch.match:
                try:
                    oi = int(ch.match["_index"])
                    if oi in remap and remap[oi] != oi:
                        ch.match["_index"] = str(remap[oi]); rep["remapped"] += 1
                except (TypeError, ValueError):
                    pass
            for vd in (ch.set_props or {}).values():
                rep["remapped"] += _remap_objref_insts(getattr(vd, "value", None), remap)
    return new, rep


def stale_changes(mod, from_build: str, to_build: str,
                  override: Optional[str] = None) -> List[Tuple]:
    """[(patch_group, change)] for patch/delete_props changes whose target value differs between builds.

    Empty when: same build, no map available, or nothing changed.  A missing map is NOT an error — it just
    means we can't check (caller may warn separately)."""
    if not from_build or not to_build or str(from_build) == str(to_build):
        return []
    vmap = load(str(from_build), str(to_build), override)
    if vmap is None:
        return []
    out = []
    for pg in mod.patches:
        for ch in pg.changes:
            if ch.action in ("patch", "delete_props") and vmap.target_changed(pg.dat, pg.ndf, ch.match):
                out.append((pg, ch))
    return out

"""ruse_mod_engine/migrate.py
================================================================================
Data-driven rmod migration across MODERN game versions (issue #14).

The Mod Manager keeps a vanilla snapshot of every game version (see
Update-RusePublic.ps1 / versions.json). When the game updates, instances inside
the .dat NDF files get re-indexed (added/removed/reordered). An rmod authored for
an older build then references stale instance indices and can crash the game.

This module makes rmods portable across modern builds by:

  1. release_timeline()   - order versions the way PLAYERS receive them. Release
                            order is the branch NUMBER, not the build id (compat 3
                            can be an OLDER build than compat 2 — a dev revert).
  2. index_remap()        - for one NDF file, match instances between two vanilla
                            builds and produce old-index -> new-index (+ removed).
  3. generate_transition_map() - do that for every changed NDF in a build->build
                            step and save migrations/v<from>_to_v<to>.json.
  4. compose_maps()       - chain consecutive steps into one cumulative remap.
  5. migrate_rmod()       - write that cumulative remap into each patch group's
                            index_map[<target build>] (NON-DESTRUCTIVE — the
                            applier already translates _index matches and ObjRef
                            inst values through index_map at apply time). Anything
                            that can't be safely migrated (a referenced instance
                            that was DELETED in the new build, an unsupported
                            ObjRef shape) is reported, never silently mistranslated.

The OG/legacy compat build is a different file format and is handled by the
dedicated compat->public converter, not by this module.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from . import edata, ndfbin
from .converter import _stable_key   # reuse the EXACT stable-key logic the differ uses

NDF_EXTS = (".ndfbin", ".truendfbin", ".gladndfbin")

# Migration maps SHIP WITH the engine (end users migrate rmods without the multi-GB
# vanilla snapshots). Generation is a maintainer step that reads the snapshots under
# sources/RUSE and writes the committed JSON here.
from ._resources import data_dir
DATA_DIR = data_dir() / "migrations"


# ── release-order chronology ────────────────────────────────────────────────
def branch_order_key(branch: str):
    """(rank, number) placing a branch in the user-release timeline, or None.
    'compat'==compat 1; 'public' is newest. Shared with catalog_version_changes."""
    b = branch.strip().lower()
    if b == "public":
        return (2, 0)
    m = re.match(r"^compat[\s_-]*v?0*(\d*)$", b)
    if m:
        return (1, int(m.group(1)) if m.group(1) else 1)
    return None


def _primary_dataver(root: Path):
    pc = root / "Data" / "PC"
    dirs = [d for d in pc.glob("*") if d.is_dir()] if pc.exists() else []
    if not dirs:
        return None
    numeric = [d for d in dirs if d.name.isdigit()]
    pool = [d for d in (numeric or dirs) if list(d.glob("*.dat"))] or (numeric or dirs)
    return max(pool, key=lambda d: (len(list(d.glob("*.dat"))), int(d.name) if d.name.isdigit() else -1))


def release_timeline(sources, include_og: bool = False):
    """Ordered modern versions oldest->newest.

    Returns list of dicts {branch, buildid, folder, root, dataver, data_dir}.
    Excludes the OG/legacy compat baseline (different data version) unless
    include_og=True. `sources` is the snapshot root holding versions.json.
    """
    sources = Path(sources)
    manifest = json.loads((sources / "versions.json").read_text(encoding="utf-8-sig"))
    placeable = []
    for name, info in manifest.get("branches", {}).items():
        k = branch_order_key(name)
        if k is None:
            continue
        placeable.append((k, name, info))
    placeable.sort(key=lambda t: t[0])

    out = []
    for _, name, info in placeable:
        folder = info.get("folder") or f"v{info.get('buildid')}"
        root = sources / folder
        dv = _primary_dataver(root)
        if dv is None:
            continue
        out.append({"branch": name, "buildid": str(info.get("buildid")), "folder": folder,
                    "root": root, "dataver": dv.name, "data_dir": dv})

    if out and not include_og:
        ref = next((e["dataver"] for e in out if e["branch"] == "public"), out[-1]["dataver"])
        out = [e for e in out if e["dataver"] == ref]
    return out


def registry_timeline(include_og: bool = False):
    """RUNTIME timeline from the SHIPPED game_versions registry — no snapshots needed,
    so it works in the built exe. Entries: {branch, buildid, dataver} (no root/data_dir;
    those are only needed for map GENERATION, which is a dev step)."""
    from . import game_versions as _gv
    placeable = []
    for bid, info in _gv.known_builds().items():
        k = branch_order_key(info.get("branch", ""))
        if k is None:
            continue
        placeable.append((k, {"branch": info.get("branch", ""), "buildid": str(bid),
                              "dataver": info.get("dataver", "190852")}))
    placeable.sort(key=lambda t: t[0])
    out = [e for _, e in placeable]
    if out and not include_og:
        ref = next((e["dataver"] for e in out if e["branch"] == "public"), out[-1]["dataver"])
        out = [e for e in out if e["dataver"] == ref]
    return out


def resolve_timeline(sources=None, include_og: bool = False):
    """The version timeline for conversion. DEV: from the snapshots under `sources`
    (full, with data dirs for generation). RUNTIME/exe: from the shipped registry."""
    if sources:
        try:
            tl = release_timeline(sources, include_og=include_og)
            if tl:
                return tl
        except Exception:
            pass
    return registry_timeline(include_og=include_og)


def build_index(timeline) -> dict:
    """buildid -> timeline entry."""
    return {e["buildid"]: e for e in timeline}


# ── instance matching (mirrors converter.diff_ndf Phase 1) ───────────────────
# Kept as a faithful copy so converter.diff_ndf — battle-tested — is not touched.
# test_migrate.py asserts this stays consistent with the differ.
def match_instances(orig_ndf, mod_ndf):
    """Match instances between two NDF trees.

    Returns (assignments, new_inst_indices) where each assignment is
    (orig_gi|None, orig_inst|None, mod_gi|None, mod_inst|None, cls_name, match|None).
    """
    def _class_groups(ndf):
        groups = {}
        for gi, inst in enumerate(ndf.instances):
            cls = ndf.classes[inst.class_index] if inst.class_index < len(ndf.classes) else None
            if cls is None:
                continue
            groups.setdefault(cls.name, []).append((gi, inst))
        return groups

    orig_groups = _class_groups(orig_ndf)
    mod_groups = _class_groups(mod_ndf)
    assignments = []
    new_inst_indices: set = set()

    for cls_name, mod_insts in sorted(mod_groups.items()):
        orig_insts = orig_groups.get(cls_name, [])
        orig_by_key, orig_no_key = {}, []
        for gi, inst in orig_insts:
            key = _stable_key(inst, orig_ndf)
            (orig_no_key.append((gi, inst)) if key is None else orig_by_key.setdefault(key, (gi, inst)))

        orig_keys_used, mod_orphans, orig_no_key_i = set(), [], 0
        for mod_gi, mod_inst in mod_insts:
            key = _stable_key(mod_inst, mod_ndf)
            if key is not None:
                orig_entry = orig_by_key.get(key)
                if orig_entry is not None and key not in orig_keys_used:
                    orig_keys_used.add(key)
                    orig_gi, orig_inst = orig_entry
                    assignments.append((orig_gi, orig_inst, mod_gi, mod_inst, cls_name, {key[0]: key[1]}))
                else:
                    mod_orphans.append((mod_gi, mod_inst))
            else:
                if orig_no_key_i < len(orig_no_key):
                    orig_gi, orig_inst = orig_no_key[orig_no_key_i]
                    orig_no_key_i += 1
                    assignments.append((orig_gi, orig_inst, mod_gi, mod_inst, cls_name, {"_index": str(orig_gi)}))
                else:
                    mod_orphans.append((mod_gi, mod_inst))

        orig_orphans_keyed = [(gi, inst) for gi, inst in orig_insts
                              if _stable_key(inst, orig_ndf) is not None
                              and _stable_key(inst, orig_ndf) not in orig_keys_used]
        orig_orphans_unkeyed = orig_no_key[orig_no_key_i:]
        orig_orphans = sorted(orig_orphans_keyed + orig_orphans_unkeyed, key=lambda x: x[0])

        for (orig_gi, orig_inst), (mod_gi, mod_inst) in zip(orig_orphans, mod_orphans):
            assignments.append((orig_gi, orig_inst, mod_gi, mod_inst, cls_name, {"_index": str(orig_gi)}))
        for mod_gi, mod_inst in mod_orphans[len(orig_orphans):]:
            assignments.append((None, None, mod_gi, mod_inst, cls_name, None))
            new_inst_indices.add(mod_gi)
        for orig_gi, orig_inst in orig_orphans[len(mod_orphans):]:
            key = _stable_key(orig_inst, orig_ndf)
            match_dict = {key[0]: key[1]} if key else {"_index": str(orig_gi)}
            assignments.append((orig_gi, orig_inst, None, None, cls_name, match_dict))

    # Classes present in ORIG but ENTIRELY ABSENT from MOD (the whole class was
    # removed between builds, e.g. TClusterLoadDataPack compat-4 -> public) are never
    # visited by the loop above (it iterates mod_groups), so their instances would be
    # silently ignored instead of marked removed — leaving the remap short by that count
    # and shifting every later instance's index by the wrong offset.
    for cls_name, orig_insts in orig_groups.items():
        if cls_name in mod_groups:
            continue
        for orig_gi, orig_inst in orig_insts:
            key = _stable_key(orig_inst, orig_ndf)
            match_dict = {key[0]: key[1]} if key else {"_index": str(orig_gi)}
            assignments.append((orig_gi, orig_inst, None, None, cls_name, match_dict))

    return assignments, new_inst_indices


def index_remap(orig_ndf, new_ndf) -> dict:
    """old-instance-index -> new-instance-index between two vanilla NDFs.

    Returns {matched:{old:new}, removed:[old...], created:[new...],
             class_map:{old_ci:new_ci}, removed_keys:[[keyprop,val]...]}.
    """
    assignments, _ = match_instances(orig_ndf, new_ndf)
    matched, removed, created = {}, [], []
    for o_gi, o_inst, m_gi, m_inst, _cls, _match in assignments:
        if o_gi is not None and m_gi is not None:
            matched[o_gi] = m_gi
        elif o_gi is not None:
            removed.append(o_gi)
        elif m_gi is not None:
            created.append(m_gi)

    new_by_name = {c.name: c.index for c in new_ndf.classes}
    class_map = {c.index: new_by_name[c.name] for c in orig_ndf.classes
                 if c.name in new_by_name and new_by_name[c.name] != c.index}

    new_keys = set()
    for inst in new_ndf.instances:
        k = _stable_key(inst, new_ndf)
        if k:
            new_keys.add(k)
    removed_keys = []
    for gi in removed:
        k = _stable_key(orig_ndf.instances[gi], orig_ndf)
        if k and k not in new_keys:
            removed_keys.append([k[0], k[1]])

    return {"matched": matched, "removed": sorted(removed), "created": sorted(created),
            "class_map": class_map, "removed_keys": removed_keys}


def segments_from_matched(matched: dict) -> list:
    """Compress {old:new} into [{from,to,offset}] runs of constant (new-old) offset.
    The applier uses these to shift ObjRef 'inst' values."""
    if not matched:
        return []
    items = sorted((int(o), int(n)) for o, n in matched.items())
    segs = []
    run_lo = run_hi = items[0][0]
    run_off = items[0][1] - items[0][0]
    prev = items[0][0]
    for o, n in items[1:]:
        off = n - o
        if o == prev + 1 and off == run_off:
            run_hi = o
        else:
            segs.append({"from": run_lo, "to": run_hi, "offset": run_off})
            run_lo = run_hi = o
            run_off = off
        prev = o
    segs.append({"from": run_lo, "to": run_hi, "offset": run_off})
    # drop zero-offset segments (no-op) to keep maps small
    return [s for s in segs if s["offset"] != 0]


# ── transition map generation ───────────────────────────────────────────────
def _file_key(dat_name: str, ndf_path: str) -> str:
    return f"{Path(dat_name).name}|{ndf_path.replace(chr(92), '/').lower()}"


def _og_dat_locations(root: Path) -> dict:
    """OG dats are spread across several PC/<n> dirs (99/1360/64) that the remaster
    consolidates under 190852. Map dat-basename -> [paths] across every PC dir."""
    locs = {}
    pc = root / "Data" / "PC"
    if pc.exists():
        for d in pc.glob("*"):
            if d.is_dir():
                for dat in d.glob("*.dat"):
                    locs.setdefault(dat.name, []).append(dat)
    return locs


def _remap_entry(o, n, rm):
    changed = {k: v for k, v in rm["matched"].items() if k != v}
    if not changed and not rm["removed"] and not rm["class_map"]:
        return None
    return {
        "inst_remap": {str(k): v for k, v in changed.items()},
        "_instance_segments": segments_from_matched(rm["matched"]),
        "_class_map": {str(k): v for k, v in rm["class_map"].items()},
        "removed": rm["removed"],
        "removed_keys": rm["removed_keys"],
    }


def generate_transition_map(from_entry: dict, to_entry: dict, log_fn=lambda *_: None) -> dict:
    """Compute the remap for every changed NDF asset between two builds.

    Same-data-version steps (the modern lineage) match dats by basename in the one
    data dir and NDF assets by identical path. The OG->remaster step is cross-format:
    OG dats live across several PC/<n> dirs and some NDF internal paths were renamed,
    so we search all OG dat locations and translate each OG path via path_map to find
    its compat-2 counterpart. Map keys stay in FROM (OG) coordinates so an OG rmod
    looks up cleanly.
    """
    files = {}
    cross = from_entry["dataver"] != to_entry["dataver"]
    to_dats = {p.name: p for p in to_entry["data_dir"].glob("*.dat")}

    if not cross:
        from_dats = {p.name: p for p in from_entry["data_dir"].glob("*.dat")}
        pairs = [(name, [from_dats[name]], to_dats[name]) for name in sorted(set(from_dats) & set(to_dats))]
        translate = lambda vp: vp                          # identical paths
    else:
        from . import path_map as _pm
        og_locs = _og_dat_locations(from_entry["root"])
        pairs = [(name, og_locs[name], to_dats[name]) for name in sorted(set(og_locs) & set(to_dats))]
        def translate(vp):
            try:
                t = _pm.translate_ndf_path(vp)
                return t or vp
            except Exception:
                return vp

    for name, from_paths, to_dat in pairs:
        try:
            ned = edata.open_dat(str(to_dat))
        except Exception as e:  # noqa: BLE001
            log_fn(f"  SKIP dat {name}: {e}")
            continue
        n_assets = {vp.replace("\\", "/").lower(): vp for vp in ned.list()}
        for from_path in from_paths:
            try:
                oed = edata.open_dat(str(from_path))
            except Exception as e:  # noqa: BLE001
                log_fn(f"  SKIP dat {name} @ {from_path}: {e}")
                continue
            for ovp in oed.list():
                if not ovp.lower().endswith(NDF_EXTS):
                    continue
                tkey = translate(ovp).replace("\\", "/").lower()
                nvp = n_assets.get(tkey)
                if nvp is None:
                    continue
                ob, nb = oed.get(ovp), ned.get(nvp)
                if ob == nb:
                    continue
                try:
                    o, n = ndfbin.read(ob), ndfbin.read(nb)
                except Exception as e:  # noqa: BLE001
                    log_fn(f"  SKIP ndf {name}:{ovp}: parse failed {e}")
                    continue
                entry = _remap_entry(o, n, index_remap(o, n))
                if entry is None:
                    continue
                files[_file_key(name, ovp)] = entry
                log_fn(f"  {name}:{ovp}: {len(entry['inst_remap'])} reindexed, {len(entry['removed'])} removed")
    return {"from_build": from_entry["buildid"], "to_build": to_entry["buildid"],
            "from_branch": from_entry["branch"], "to_branch": to_entry["branch"],
            "files": files}


def transition_map_path(maps_dir, from_build: str, to_build: str) -> Path:
    return Path(maps_dir) / f"v{from_build}_to_v{to_build}.json"


def save_transition_map(maps_dir, tmap: dict) -> Path:
    p = transition_map_path(maps_dir, tmap["from_build"], tmap["to_build"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(tmap, indent=2), encoding="utf-8")
    return p


def generate_all_maps(sources, maps_dir=DATA_DIR, include_og: bool = False,
                      regenerate: bool = False, log_fn=print) -> list:
    """Generate (and save) a transition map for every consecutive modern step.

    Maintainer step: reads the vanilla snapshots under `sources` and writes the
    shippable JSON maps under `maps_dir` (default: the engine data dir).
    """
    tl = release_timeline(sources, include_og=include_og)
    out = []
    for a, b in zip(tl, tl[1:]):
        p = transition_map_path(maps_dir, a["buildid"], b["buildid"])
        if p.exists() and not regenerate:
            log_fn(f"== {a['branch']} -> {b['branch']}: cached")
            out.append(p); continue
        log_fn(f"== {a['branch']} v{a['buildid']} -> {b['branch']} v{b['buildid']} ==")
        out.append(save_transition_map(maps_dir, generate_transition_map(a, b, log_fn)))
    return out


# ── composition (chaining steps) ────────────────────────────────────────────
def compose_file(a: dict, b: dict) -> dict:
    """Compose two per-file remaps A->B then B->C into A->C."""
    a_seg = a.get("_instance_segments", [])
    b_seg = b.get("_instance_segments", [])
    a_rm = a.get("inst_remap", {})
    b_rm = b.get("inst_remap", {})
    b_removed = set(b.get("removed", []))

    def b_of(idx):
        # apply A->B (idx may be unchanged) then check removed in B->C step
        mid = int(a_rm.get(str(idx), idx + _seg_offset(idx, a_seg)))
        return mid

    def _seg_offset(idx, segs):
        for s in segs:
            if s["from"] <= idx <= s["to"]:
                return s["offset"]
        return 0

    # Build composed matched over the union of A's domain
    domain = set(int(k) for k in a_rm) | {s_i for s in a_seg for s_i in range(s["from"], s["to"] + 1)}
    # also include identity indices implied by B step segments that A leaves unchanged
    domain |= set(int(k) for k in b_rm)
    matched = {}
    removed = list(a.get("removed", []))
    for idx in sorted(domain):
        mid = b_of(idx)
        if mid in b_removed:
            removed.append(idx)
            continue
        final = int(b_rm.get(str(mid), mid + _seg_offset(mid, b_seg)))
        if final != idx:
            matched[idx] = final
    # carry B->C removals mapped back is not needed for ObjRef survivors; keep removed_keys union
    removed_keys = a.get("removed_keys", []) + b.get("removed_keys", [])
    class_map = {}
    a_cm = {int(k): int(v) for k, v in a.get("_class_map", {}).items()}
    b_cm = {int(k): int(v) for k, v in b.get("_class_map", {}).items()}
    for k in set(a_cm) | set(b_cm):
        mid = a_cm.get(k, k)
        class_map[k] = b_cm.get(mid, mid)
    class_map = {k: v for k, v in class_map.items() if k != v}
    return {
        "inst_remap": {str(k): v for k, v in matched.items()},
        "_instance_segments": segments_from_matched(matched),
        "_class_map": {str(k): v for k, v in class_map.items()},
        "removed": sorted(set(removed)),
        "removed_keys": removed_keys,
    }


def compose_chain(tmaps: list) -> dict:
    """Compose an ordered list of transition maps into one {file_key: remap}."""
    composed = {}
    all_keys = set()
    for t in tmaps:
        all_keys |= set(t["files"].keys())
    for key in all_keys:
        acc = None
        for t in tmaps:
            step = t["files"].get(key)
            if step is None:
                continue
            acc = step if acc is None else compose_file(acc, step)
        if acc is not None:
            composed[key] = acc
    return composed


# ── rmod migration ──────────────────────────────────────────────────────────
def _find_chain(timeline, from_build: str, to_build: str):
    """Return the slice of transition steps (entry pairs) from->to, or None."""
    ids = [e["buildid"] for e in timeline]
    if from_build not in ids or to_build not in ids:
        return None
    i, j = ids.index(from_build), ids.index(to_build)
    if i >= j:
        return []                                          # already at/after target
    return list(zip(timeline[i:j], timeline[i + 1:j + 1]))


def migrate_rmod(mod, from_build: str, to_build: str, sources, *,
                 maps_dir=DATA_DIR, regenerate: bool = False):
    """Make `mod` (a mod_format.RuseMod) applicable on build `to_build`.

    Writes a cumulative index_map[to_build] into every patch group, derived from
    the per-step transition maps in `maps_dir` (generated from `sources` on demand
    if missing or regenerate). Returns a report dict. NON-DESTRUCTIVE: original
    change data is untouched; the applier consumes index_map[to_build] when
    game_version==to_build.
    """
    report = {"from": from_build, "to": to_build, "groups": 0, "files_mapped": 0,
              "reindexed": 0, "review": []}

    def flag(severity, where, reason):
        report["review"].append({"severity": severity, "where": where, "reason": reason})

    tl = resolve_timeline(sources)
    chain = _find_chain(tl, from_build, to_build)
    if chain is None:
        flag("SCRIPT", "chain", f"build {from_build} or {to_build} not in the release timeline; "
             "cannot determine migration path")
        return report
    if not chain:
        flag("INFO", "chain", "rmod is already at/after the target build; no migration needed")
        return report

    # load each step's SHIPPED transition map (generation is dev-only, needs snapshots)
    tmaps = []
    for a, b in chain:
        p = transition_map_path(maps_dir, a["buildid"], b["buildid"])
        if (regenerate or not p.exists()) and a.get("data_dir") is not None:
            save_transition_map(maps_dir, generate_transition_map(a, b))   # dev only
        if not p.exists():
            flag("SCRIPT", "chain", f"no shipped mapping for v{a['buildid']} -> v{b['buildid']} "
                 f"({a['branch']} -> {b['branch']}); cannot migrate")
            return report
        tmaps.append(json.loads(p.read_text(encoding="utf-8")))

    composed = compose_chain(tmaps)
    removed_keys_global = {tuple(k) for t in tmaps for k in
                           (kk for f in t["files"].values() for kk in f.get("removed_keys", []))}

    for pg in mod.patches:
        report["groups"] += 1
        key = _file_key(pg.dat, pg.ndf)
        fmap = composed.get(key)
        # validate stable-ref matches whose target was deleted somewhere in the chain
        for ch in pg.changes:
            for kp in ("ClassNameForDebug", "DescriptorId"):
                if kp in ch.match and (kp, ch.match[kp]) in removed_keys_global:
                    flag("REVIEW", f"{pg.ndf}:{ch.match[kp]}",
                         f"matched instance was DELETED in a later build; '{ch.action}' will no-op")
        if fmap is None:
            continue                                       # this file unchanged across the chain
        report["files_mapped"] += 1
        report["reindexed"] += len(fmap.get("inst_remap", {}))
        # flag positional refs that point at deleted instances
        removed = set(fmap.get("removed", []))
        for ch in pg.changes:
            if "_index" in ch.match and int(ch.match["_index"]) in removed:
                flag("REVIEW", f"{pg.ndf}:_index {ch.match['_index']}",
                     "positional target was DELETED in the new build; change cannot be migrated")
        pg.index_map[to_build] = {
            **{k: v for k, v in fmap.get("inst_remap", {}).items()},
            "_instance_segments": fmap.get("_instance_segments", []),
            "_class_map": fmap.get("_class_map", {}),
        }

    # stamp the rmod so future migrations know where it now applies
    if not mod.game_version:
        mod.game_version = from_build
    return report


# ── rewrite-mode converter (produces a NATIVE rmod in the target coordinates) ─
def invert_file_map(fmap: dict) -> dict:
    """Invert an A->B per-file remap into B->A (for backward conversion)."""
    inv = {int(v): int(k) for k, v in fmap.get("inst_remap", {}).items()}
    return {
        "inst_remap": {str(k): v for k, v in inv.items()},
        "_instance_segments": segments_from_matched(inv),
        "_class_map": {str(v): int(k) for k, v in fmap.get("_class_map", {}).items()},
        "removed": [],                      # B->A: A had no extra; B-only instances handled per-ref
        "removed_keys": fmap.get("removed_keys", []),
    }


def _chain_composed(sources, from_build, to_build, maps_dir, regenerate, include_og):
    """Composed per-file remap for ANY direction along the timeline (forward or
    inverted), plus (from_dataver, to_dataver). Uses the runtime-safe timeline (shipped
    registry in the exe, snapshots in dev). Maps are SHIPPED — at runtime a missing map
    is an error (we never generate without the dev snapshots); only regenerate=True (dev)
    builds maps from snapshots."""
    tl = resolve_timeline(sources, include_og=include_og)
    ids = [e["buildid"] for e in tl]
    if from_build not in ids or to_build not in ids:
        raise ValueError(f"build {from_build} or {to_build} not in timeline {ids}")
    fi, ti = ids.index(from_build), ids.index(to_build)
    from_dv = tl[fi]["dataver"]; to_dv = tl[ti]["dataver"]
    if fi == ti:
        return {}, from_dv, to_dv
    forward = fi < ti
    lo, hi = (fi, ti) if forward else (ti, fi)
    steps = list(zip(tl[lo:hi], tl[lo + 1:hi + 1]))
    tmaps = []
    for a, b in steps:
        p = transition_map_path(maps_dir, a["buildid"], b["buildid"])
        # dev (timeline entries carry snapshot data_dirs) may generate; runtime (registry
        # entries, no data_dir) only consumes shipped maps.
        if (regenerate or not p.exists()) and a.get("data_dir") is not None:
            save_transition_map(maps_dir, generate_transition_map(a, b))
        if not p.exists():
            raise ValueError(f"no shipped mapping for v{a['buildid']} -> v{b['buildid']} "
                             f"({a['branch']} -> {b['branch']})")
        tmaps.append(json.loads(p.read_text(encoding="utf-8")))
    composed = compose_chain(tmaps)
    if not forward:
        composed = {k: invert_file_map(v) for k, v in composed.items()}
    return composed, from_dv, to_dv


def _remap_objref_value(value, remap: dict):
    """Recursively rewrite ObjRef 'inst' indices in a serialized rmod value."""
    if isinstance(value, dict) and "inst" in value:
        new = dict(value)
        new["inst"] = remap.get(int(value["inst"]), value["inst"])
        return new
    if isinstance(value, list):
        return [_remap_objref_value(v, remap) for v in value]
    return value


def convert_rmod(mod, from_build: str, to_build: str, sources, *,
                 maps_dir=DATA_DIR, regenerate: bool = False, include_og: bool = True):
    """Produce `mod` NATIVELY in `to_build` coordinates (rewrite mode).

    Rewrites each patch group's dat data-version path, translates NDF paths across
    the OG->remaster jump, and remaps every positional _index match and ObjRef inst
    value via the EXACT computed per-build-pair map (no hardcoded offsets). Works in
    ANY direction (forward composes steps; backward inverts them). Mutates `mod` in
    place and returns a report; changes whose target was deleted are dropped+flagged.
    """
    from . import path_map as _pm
    report = {"from": from_build, "to": to_build, "groups": 0, "reindexed": 0,
              "dropped": 0, "review": []}

    def flag(sev, where, reason):
        report["review"].append({"severity": sev, "where": where, "reason": reason})

    try:
        composed, from_dv, to_dv = _chain_composed(sources, from_build, to_build,
                                                   maps_dir, regenerate, include_og)
    except ValueError as e:
        report["error"] = str(e)
        flag("SCRIPT", "chain", str(e))
        return report
    cross_format = (from_dv != to_dv)

    for pg in mod.patches:
        report["groups"] += 1
        fmap = composed.get(_file_key(pg.dat, pg.ndf))      # key in FROM coordinates
        remap = {int(k): int(v) for k, v in fmap["inst_remap"].items()} if fmap else {}
        removed = set(fmap.get("removed", [])) if fmap else set()

        kept = []
        for ch in pg.changes:
            if "_index" in ch.match:
                oi = int(ch.match["_index"])
                if oi in removed:
                    report["dropped"] += 1
                    flag("REVIEW", f"{pg.ndf}:_index {oi}",
                         "target instance does not exist in the destination build; change dropped")
                    continue
                if oi in remap:
                    ch.match["_index"] = str(remap[oi]); report["reindexed"] += 1
            # remap ObjRef inst values inside set_props
            for vd in ch.set_props.values():
                vd.value = _remap_objref_value(vd.value, remap)
            kept.append(ch)
        pg.changes = kept

        # rewrite coordinates across the OG->remaster jump. OG dats are spread over
        # several PC/<n> dirs (99/1360/64) that the remaster consolidates under
        # 190852, so reuse the proven path logic instead of a naive data-version swap.
        if cross_format:
            from .applier import resolve_dat_for_version
            pg.dat = resolve_dat_for_version(pg.dat, "public")
            try:
                translated = _pm.translate_ndf_path(pg.ndf)
                if translated and translated != pg.ndf:
                    pg.ndf = translated
            except Exception:
                flag("SCRIPT", pg.ndf, "no NDF path translation available for this format jump")
        pg.index_map = {}                                   # native now; no apply-time translation

    # The OG->remaster jump also relocates the dats that file/loc/sdb/scenario groups
    # target (PC/99|1360|64 -> 190852). Those carry no instance indices, just rewrite
    # the path. (Modern same-data-version steps leave them untouched.)
    if cross_format:
        from .applier import resolve_dat_for_version
        for grp_name in ("file_patches", "loc_patches", "sdb_patches", "scenario_patches"):
            for g in getattr(mod, grp_name, []) or []:
                if getattr(g, "dat", None):
                    g.dat = resolve_dat_for_version(g.dat, "public")

    mod.game_version = to_build
    return report

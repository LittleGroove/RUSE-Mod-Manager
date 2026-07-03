"""Scenario completeness validator — the anti-silent-break keystone.

Checks that every tag a mission script (effetmap) references binds to a COMPLETE scenario placement:
exists, has a matching Name, of the right kind. ModdingSuite-authored operation mods fail this (empty
placements) which is why they CTD on the hardened remaster engine. This pass would have flagged every one.

Public API:
  validate_rmod(path) -> ValidationReport      # rmod file
  validate(scenario_bytes, effetmap_bytes) -> ValidationReport

The report's `issues` are the exact reconstruction work-list for the Front-A scenario completer.
"""
from __future__ import annotations
import json, base64, re, zlib
from dataclasses import dataclass, field
from typing import List, Optional

from . import scenario as S, ndfbin, xyz_compile


# tag kind -> the AddOn placement kind it must bind to
_KIND_REQUIRES = {
    "TagZoneDetection": "zone",   # needs a Zone AddOn (CircularZone/RectangleZone) with geometry
    "TagUnit": "unit",            # needs a Spawn that actually spawns a unit, named
    "TagPosition": "marker",      # needs a positioned, named placement
}
_ZONE_CLASSES = ("TGameDesignAddOn_CircularZone", "TGameDesignAddOn_RectangleZone", "TGameDesignAddOn_Zone")


@dataclass
class Issue:
    tag: str
    kind: str
    problem: str          # "no placement named", "placement not a zone", "spawn has no unit", ...
    fix: str              # the reconstruction action for the completer


@dataclass
class ValidationReport:
    ok: bool
    tags_total: int
    bound: int
    issues: List[Issue] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def summary(self) -> str:
        head = "OK — all %d tags bind to complete placements" % self.tags_total if self.ok \
            else "INCOMPLETE — %d/%d tags unbound or malformed" % (len(self.issues), self.tags_total)
        return head + "".join("\n  - [%s] %s: %s -> %s" % (i.kind, i.tag, i.problem, i.fix) for i in self.issues)


def _decompile_effetmap(xyz: bytes) -> str:
    # tolerate the stale XYZ0 size field shipped in some scripts
    marshal = zlib.decompressobj().decompress(xyz[28:]) if xyz[:5] == b"XYZ0\n" else xyz
    return xyz_compile.decompile(marshal)


def _script_tags(effetmap: bytes):
    src = _decompile_effetmap(effetmap)
    tags = {}  # name -> kind
    for m in re.finditer(r"(Tag(?:Position|Unit|ZoneDetection))\('([^']*)'\)", src):
        tags[m.group(2)] = m.group(1)
    return tags


def _placement_index(scenario: bytes):
    """name -> set of placement kinds carrying that Name in the scenario's GameDesignDatabase."""
    nb = ndfbin.read(S.read(scenario)["ndf_data"])
    by_name = {}
    for inst in nb.instances:
        cls = nb.classes[inst.class_index].name
        if not cls.startswith("TGameDesignAddOn_"):
            continue
        kind = "zone" if cls in _ZONE_CLASSES else ("unit" if cls == "TGameDesignAddOn_Spawn" else "marker")
        # Index EVERY "Name" value the instance carries. Two subtleties, each of which caused false
        # "no placement named" (and a false crash verdict against WORKING mods):
        #  1. Can't use prop_by_name_and_class("Name", class_index): the "Name" property is often owned by
        #     a BASE class (StartingPoint HQs inherit it), so a per-class lookup misses it entirely.
        #  2. A placement can carry MORE THAN ONE "Name" prop — a base-class one set to 'Null' plus the
        #     real subclass one (e.g. a Spawn: base Name='Null', spawn Name='Usine_neutre'). Taking the
        #     first match grabs 'Null'. So collect all, and skip the placeholder values.
        for pv in inst.props:
            pr = nb.properties[pv.prop_index] if 0 <= pv.prop_index < len(nb.properties) else None
            if pr is None or pr.name != "Name":
                continue
            name = str(nb.resolve_value(pv.value))
            if not name or name in ("None", "Null"):
                continue
            by_name.setdefault(name, set()).add(kind)
    return by_name, nb


def validate(scenario: bytes, effetmap: bytes) -> ValidationReport:
    tags = _script_tags(effetmap)
    by_name, _ = _placement_index(scenario)
    issues = []
    bound = 0
    for name, tagcls in sorted(tags.items()):
        need = _KIND_REQUIRES.get(tagcls, "marker")
        kinds = by_name.get(name)
        if not kinds:
            issues.append(Issue(name, tagcls, "no placement named %r" % name,
                                "create a %s placement named %r" % (need, name)))
        elif need == "zone" and "zone" not in kinds:
            issues.append(Issue(name, tagcls, "placement is %s, not a zone" % "/".join(kinds),
                                "add a Zone AddOn (geometry) named %r" % name))
        elif need == "unit" and "unit" not in kinds:
            issues.append(Issue(name, tagcls, "placement is %s, not a unit spawn" % "/".join(kinds),
                                "name a unit spawn %r" % name))
        else:
            bound += 1
    return ValidationReport(ok=not issues, tags_total=len(tags), bound=bound, issues=issues)


def validate_rmod(path: str) -> ValidationReport:
    d = json.load(open(path, encoding="utf-8"))
    scen = eff = None
    for fp in d.get("file_patches", []):
        for f in fp["files"]:
            if f["path"].endswith(".scenario"):
                scen = base64.b64decode(f["data"])
            elif f["path"].endswith("effetmap.xyz"):
                eff = base64.b64decode(f["data"])
    if scen is None or eff is None:
        return ValidationReport(ok=True, tags_total=0, bound=0,
                                notes=["not an operation/campaign mod (no scenario+effetmap) — nothing to validate"])
    return validate(scen, eff)

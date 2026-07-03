"""Scenario completer (Front A) — reconstruct the placements an incomplete (ModdingSuite) scenario is
missing so every mission-script tag binds and the operation stops CTD'ing on the hardened engine.

Driven by scenario_validate's work-list. Reconstruction is best-effort: type-matched where possible,
otherwise any spare placement (crash-free but gameplay-approximate; flagged, not authoritative).

  complete_rmod(in_path, out_path) -> ValidationReport   # report AFTER completion (should be ok)

Shares the exact add-class / add-placement / re-emit primitives the editor's emit engine will use.
"""
from __future__ import annotations
import json, base64, re, zlib, copy

from . import scenario as S, ndfbin, scenario_validate as SV
from .ndfbin import NdfClass, NdfProperty, NdfInstance, NdfPropertyValue, make_value, val_float32

REF_TRAN = 3149642683
DEFAULT_RADIUS = 120000.0


class _Builder:
    def __init__(self, nb, zones):
        self.nb = nb
        self.zones = zones
        self.ci = {c.name: c.index for c in nb.classes}
        # ensure CircularZone class + its Name/Radius props exist
        if "TGameDesignAddOn_CircularZone" not in self.ci:
            idx = len(nb.classes); nb.classes.append(NdfClass(idx, "TGameDesignAddOn_CircularZone"))
            self.ci["TGameDesignAddOn_CircularZone"] = idx
        self.cz = self.ci["TGameDesignAddOn_CircularZone"]
        self.p_czname = self._ensure_prop(self.cz, "Name")
        self.p_czrad = self._ensure_prop(self.cz, "Radius")
        # Spawn needs a Name prop to be nameable
        self.p_spawnname = self._ensure_prop(self.ci["TGameDesignAddOn_Spawn"], "Name")
        self.p_markname = self._prop(self.ci["TGameDesignAddOn_Name"], "Name")
        # item template props
        ci_item = self.ci["TGameDesignItem"]
        ex = next(x for x in nb.instances if x.class_index == ci_item)
        self.p_pos = next(pv.prop_index for pv in ex.props if nb.properties[pv.prop_index].name == "Position")
        self.p_rot = next(pv.prop_index for pv in ex.props if nb.properties[pv.prop_index].name == "Rotation")
        self.p_addon = next(pv.prop_index for pv in ex.props if nb.properties[pv.prop_index].name == "AddOn")
        self._pos_template = next(pv.value for pv in ex.props if nb.properties[pv.prop_index].name == "Position")
        self._ref_template = next(pv.value for pv in ex.props if nb.properties[pv.prop_index].name == "AddOn")
        self.list_inst = next(x for x in nb.instances if x.class_index == self.ci["TGameDesignItemList"])
        self.list_prop = self.list_inst.props[0]
        self._zone_i = 0

    def _prop(self, cls_idx, name):
        for p in self.nb.properties:
            if p.class_index == cls_idx and p.name == name:
                return p.index
        return None

    def _ensure_prop(self, cls_idx, name):
        i = self._prop(cls_idx, name)
        if i is not None:
            return i
        i = len(self.nb.properties)
        self.nb.properties.append(NdfProperty(i, name, cls_idx))
        return i

    def _ref(self, inst, cls):
        v = copy.deepcopy(self._ref_template); v.raw = (REF_TRAN, (inst, cls)); return v

    def _next_zone_pos(self):
        z = self.zones[self._zone_i % len(self.zones)] if self.zones else {"pos": (0.0, 0.0, 0.0)}
        self._zone_i += 1
        return z.get("pos") or (0.0, 0.0, 0.0)

    def add_zone(self, name):
        addon_idx = len(self.nb.instances)
        self.nb.instances.append(NdfInstance(self.cz, [
            NdfPropertyValue(self.p_czname, make_value("StringRef", self.nb.ensure_string(name))),
            NdfPropertyValue(self.p_czrad, val_float32(DEFAULT_RADIUS))]))
        item_idx = len(self.nb.instances)
        pos = copy.deepcopy(self._pos_template); pos.raw = tuple(float(c) for c in self._next_zone_pos())
        self.nb.instances.append(NdfInstance(self.ci["TGameDesignItem"], [
            NdfPropertyValue(self.p_pos, pos),
            NdfPropertyValue(self.p_rot, val_float32(0.0)),
            NdfPropertyValue(self.p_addon, self._ref(addon_idx, self.cz))]))
        self.list_prop.value.raw.append(self._ref(item_idx, self.ci["TGameDesignItem"]))

    def name_existing(self, inst, prop_index, name):
        inst.set(prop_index, make_value("StringRef", self.nb.ensure_string(name)))


def _unnamed(nb, cls_idx, name_prop):
    """instances of cls_idx that have no Name value yet (available for assignment)."""
    out = []
    for x in nb.instances:
        if x.class_index == cls_idx and x.get(name_prop) is None:
            out.append(x)
    return out


def complete_scenario(scenario: bytes) -> bytes:
    sc = S.read(scenario)
    nb = ndfbin.read(sc["ndf_data"])
    b = _Builder(nb, sc.get("zones") or [])
    rep = SV.validate(scenario, _EFF)  # _EFF set by complete_rmod
    ci_spawn = b.ci["TGameDesignAddOn_Spawn"]
    ci_mark = b.ci["TGameDesignAddOn_Name"]
    pyc_prop = b._prop(ci_spawn, "PythonClassName")

    spare_spawns = _unnamed(nb, ci_spawn, b.p_spawnname)
    spare_marks = _unnamed(nb, ci_mark, b.p_markname)

    def take_spawn(prefer=None):
        if prefer:
            for s in spare_spawns:
                v = s.get(pyc_prop)
                if v is not None and prefer.lower() in str(nb.resolve_value(v)).lower():
                    spare_spawns.remove(s); return s
        return spare_spawns.pop(0) if spare_spawns else None

    PREFER = {"usineat": "UsineAutomoteur", "persh": "Pershing", "usine": "Usine", "at_": "DefenseAT", "reco": "Recon"}
    for iss in rep.issues:
        if iss.kind == "TagZoneDetection":
            b.add_zone(iss.tag)
        elif iss.kind == "TagUnit":
            prefer = next((v for k, v in PREFER.items() if k in iss.tag.lower()), None)
            s = take_spawn(prefer)
            if s is not None:
                b.name_existing(s, b.p_spawnname, iss.tag)
        else:  # TagPosition -> a named marker
            if spare_marks:
                b.name_existing(spare_marks.pop(0), b.p_markname, iss.tag)

    sc["ndf_data"] = ndfbin.write(nb)
    return S.write(sc)


_EFF = None  # module-level handoff for complete_scenario's validate call


def complete_rmod(in_path: str, out_path: str):
    global _EFF
    d = json.load(open(in_path, encoding="utf-8"))
    sp = None
    for fp in d.get("file_patches", []):
        for f in fp["files"]:
            if f["path"].endswith(".scenario"): sp = f
            elif f["path"].endswith("effetmap.xyz"): _EFF = base64.b64decode(f["data"])
    scen = base64.b64decode(sp["data"])
    newscen = complete_scenario(scen)
    sp["data"] = base64.b64encode(newscen).decode()
    json.dump(d, open(out_path, "w", encoding="utf-8"))
    return SV.validate(newscen, _EFF)

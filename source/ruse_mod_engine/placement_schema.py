"""Placement schema — what every scenario placement class can hold, so the editor can present TYPED
widgets for every field (including ones currently nil/absent), let users ADD behaviour-driving
properties, and CREATE any placement from a corpus template (no "must already exist in this scenario").

Data-derived: the field set / types / value domains here are the union observed across ALL 92 shipped
scenarios (ai_unit_tests/claude/survey_placements.py -> placement_survey.json). The human overlay
(camp/supply meanings, the WarmupCamPath enum) is curated. See docs/map_editor/placement_schema.md.

This module is engine-only (no tkinter): the editor maps each field's `widget` to a typed entry/combo
and to the existing _commit_* helpers. It is the reusable core intended to extend to the other mod
editor windows later.
"""
from dataclasses import dataclass, field
from typing import Optional, List, Callable

from . import ndfbin
from .ndfbin import T, NdfInstance, NdfClass, NdfProperty, NdfPropertyValue, NdfValue, OBJ_REF_MARKER


# ── value decoders / domains (curated overlay) ───────────────────────────────────
def camp_label(v):
    """Camp Int32 meaning. -1 = neutral/visible; None = no Camp (engine default); N = camp N
    (camps are defined in the paired .xyz script; the number ties a placement to one)."""
    if v is None:
        return "(no camp)"
    if v == -1:
        return "-1 (neutral)"
    if v == -2:
        return "-2 (despawn)"
    return "camp %d" % v


def supply_label(v):
    """Depot ChampInteger -> in-game supply (the game multiplies by 9; atomic units by 27)."""
    if v is None:
        return "(unset)"
    try:
        return "%d  (~%d supply)" % (v, int(v) * 9)
    except Exception:
        return str(v)


CAMP_CHOICES = [None, -1, 1, 2, 3, 4, 5, 6, 7, 8, 9]              # Camp / AllianceNum domain
PRIORITY_CHOICES = [None, 1, 2, 3, 4, 5, 6, 7, 8]                 # AlliancePriority (None = FFA/solo seat)
WARMUP_CHOICES = [None] + ["Warmup_J%d" % n for n in range(1, 9)]

# Eugen nations (from the DSL _enum_for_game_play.Nationalite) — informational; placements bind to a
# camp NUMBER, the camp's nation is set in the script.
NATIONS = ["Allemagne", "EU", "France", "Italie", "Japon", "RU", "URSS"]


# ── schema dataclasses ───────────────────────────────────────────────────────────
@dataclass
class PlacementFieldSpec:
    prop: str                       # NDF property name on the AddOn (or "Rotation" on the item)
    type_id: int                    # ndfbin.T.* to write
    widget: str                     # camp|priority|int|float|pyclass|stringref|widestr|warmup|vector3|rotation
    label: str = ""                 # display label (defaults to prop)
    required: bool = False          # always present on shipped instances of this class
    addable: bool = True            # may be added when absent (drives behaviour)
    choices: Optional[list] = None  # enum domain (for camp/priority/warmup/etc.)
    default: object = None          # value to seed when the user ADDS this field
    on_item: bool = False           # property lives on the TGameDesignItem, not the AddOn (Rotation)
    help: str = ""
    decode: Optional[Callable] = None   # value -> human suffix string

    def __post_init__(self):
        if not self.label:
            self.label = self.prop


@dataclass
class PlacementClassSchema:
    kind: str                       # editor kind: depot|unit|building|spawn|hq|ville|montagne|name|circle|rect
    addon_class: str                # TGameDesignAddOn_*
    fields: List[PlacementFieldSpec]
    py_prefix: Optional[str] = None  # for Spawn sub-kinds: required PythonClassName shape (None = any)
    note: str = ""

    def field(self, prop):
        return next((f for f in self.fields if f.prop == prop), None)


# shared field builders
def _camp():
    return PlacementFieldSpec("Camp", T.Int32, "camp", "Camp", required=False, addable=True,
                              choices=CAMP_CHOICES, decode=camp_label,
                              help="Which camp owns this (camps live in the .xyz script). Blank = none.")


def _name(required=False, help="Name the script references this placement by (TagPosition/TagUnit/"
                               "TagZoneDetection). Give it a unique name to drive it from a mission script."):
    return PlacementFieldSpec("Name", T.StringRef, "stringref", "Name", required=required,
                              addable=True, help=help)


def _pyclass():
    return PlacementFieldSpec("PythonClassName", T.StringRef, "pyclass", "PythonClassName",
                              required=True, addable=False,
                              help="The exact unit/building/entity class this spawn instantiates.")


def _rotation():
    return PlacementFieldSpec("Rotation", T.Float32, "rotation", "Rotation (rad)", on_item=True,
                              addable=True, default=0.0, help="Facing in radians (item-level).")


def _champtexte():
    return PlacementFieldSpec("ChampTexte", T.WideStr, "widestr", "ChampTexte (text)", addable=True,
                              default="", help="Rare text label carried on this spawn.")


def _champinteger_raw():
    return PlacementFieldSpec("ChampInteger", T.Int32, "int", "ChampInteger (raw)", addable=True,
                              default=0, help="Advanced raw integer. Depots use it for supply; on "
                                              "units/buildings its meaning is non-standard — leave unset "
                                              "unless cloning a value you understand.")


# ── the schema (grounded in placement_survey.json across all 92 scenarios) ───────
SCHEMA = {
    "depot": PlacementClassSchema(
        "depot", "TGameDesignAddOn_Spawn",
        py_prefix="front.batiment_depot.DalleBatimentDepot",
        fields=[
            PlacementFieldSpec("PythonClassName", T.StringRef, "stringref", "PythonClassName",
                               required=True, addable=False,
                               default="front.batiment_depot.DalleBatimentDepot"),
            _camp(),
            PlacementFieldSpec("ChampInteger", T.Int32, "int", "Supply (ChampInteger)", addable=True,
                               default=8, decode=supply_label,
                               help="Supply amount. The game multiplies this by ~9."),
            _champtexte(),
            _name(help="Optional label; depots rarely need a script name."),
            _rotation(),
        ],
        note="Supply depot. Camp -1 = neutral/capturable."),
    "unit": PlacementClassSchema(
        "unit", "TGameDesignAddOn_Spawn", py_prefix="Unit_",
        fields=[_pyclass(), _camp(), _name(), _champinteger_raw(), _rotation()],
        note="Pre-placed unit (campaign/operation). Name it to drive it from the script."),
    "building": PlacementClassSchema(
        "building", "TGameDesignAddOn_Spawn", py_prefix="Building_",
        fields=[_pyclass(), _camp(), _name(), _champinteger_raw(), _champtexte(), _rotation()],
        note="Pre-placed building/defence (incl. scripted HQs as Building_Headquarter*)."),
    "spawn": PlacementClassSchema(
        "spawn", "TGameDesignAddOn_Spawn",
        fields=[_pyclass(), _camp(), _name(), _rotation()],
        note="Any other camp-owned scenario entity."),
    "hq": PlacementClassSchema(
        "hq", "TGameDesignAddOn_StartingPoint",
        fields=[
            PlacementFieldSpec("AllianceNum", T.Int32, "camp", "Alliance", required=True, addable=True,
                               default=1, choices=CAMP_CHOICES,
                               help="Team/alliance number this start belongs to."),
            PlacementFieldSpec("AlliancePriority", T.Int32, "priority", "Priority (slot)", addable=True,
                               default=1, choices=PRIORITY_CHOICES,
                               help="Slot within the team. Absent = FFA / solo seat (vanilla encoding)."),
            PlacementFieldSpec("Azimut", T.Float32, "float", "Camera azimut", addable=True, default=0.0),
            PlacementFieldSpec("Site", T.Float32, "float", "Camera site", addable=True, default=0.0),
            PlacementFieldSpec("WarmupCamPath", T.StringRef, "warmup", "Warmup camera path",
                               addable=True, choices=WARMUP_CHOICES,
                               help="Named TCameraPath used as the start camera (campath ndfbin)."),
            PlacementFieldSpec("PositionCamera", T.Vector3, "vector3", "Camera position",
                               addable=False, help="Resting-camera world position (moves with the HQ)."),
            _name(help="Optional name (some campaign HQs are named, e.g. HQ_Joueur)."),
        ],
        note="Player start / HQ. Team modes use (Alliance, Priority); FFA/solo omit Priority."),
    "ville": PlacementClassSchema(
        "ville", "TGameDesignAddOn_LabelVille",
        fields=[PlacementFieldSpec("ChampTexte", T.WideStr, "widestr", "City label", addable=True,
                                   default="", help="The city name shown on the map."),
                _name()],
        note="City name label."),
    "montagne": PlacementClassSchema(
        "montagne", "TGameDesignAddOn_LabelMontagne",
        fields=[PlacementFieldSpec("ChampTexte", T.WideStr, "widestr", "Mountain label", addable=True,
                                   default="")],
        note="Mountain name label."),
    "name": PlacementClassSchema(
        "name", "TGameDesignAddOn_Name",
        fields=[_name(required=True,
                      help="A named point — pure script anchor (TagPosition). 16k of these ship; "
                           "they are how missions reference map locations.")],
        note="Named point / waypoint (script anchor)."),
    "circle": PlacementClassSchema(
        "circle", "TGameDesignAddOn_CircularZone",
        fields=[_name(help="Zone name — scripts reference it via TagZoneDetection."),
                PlacementFieldSpec("Radius", T.Float32, "float", "Radius", required=True, addable=True,
                                   default=50000.0)],
        note="Circular trigger/detection zone."),
    "rect": PlacementClassSchema(
        "rect", "TGameDesignAddOn_RectangleZone",
        fields=[_name(help="Zone name — scripts reference it via TagZoneDetection."),
                PlacementFieldSpec("Width", T.Float32, "float", "Width", required=True, addable=True,
                                   default=50000.0),
                PlacementFieldSpec("Height", T.Float32, "float", "Height", required=True, addable=True,
                                   default=50000.0)],
        note="Rectangular trigger/detection zone."),
}

# Default template sources (map, scenario) to bootstrap creation when the current scenario has no
# instance of the requested kind. (From survey_placements.py `template` field — campaign/op scenarios
# carry every class.) The editor falls back to scanning other scenarios if these aren't present.
TEMPLATE_SOURCES = {
    "depot":    ("alpha", "leveldesign"),
    "hq":       ("alpha", "leveldesign"),
    "unit":     ("alpha", "leveldesign_bh"),
    "building": ("alpha", "leveldesign_bh"),
    "spawn":    ("alpha", "leveldesign_bh"),
    "ville":    ("alpha", "leveldesign"),
    "montagne": ("battleofthebulge", "leveldesign"),
    "name":     ("alpha", "leveldesign_bh"),
    "circle":   ("alpha", "leveldesign_bh"),
    "rect":     ("alpha", "leveldesign_bh"),
}

KIND_ORDER = ["hq", "depot", "unit", "building", "spawn", "name", "circle", "rect", "ville", "montagne"]


# ── cross-NDF template import (bootstrap library) ────────────────────────────────
# A scenario with no HQ/building of a given class has no CLAS/PROP entry for it, so you can't clone or
# construct one in-place. import_placement copies a (TGameDesignItem, AddOn) pair from a DONOR scenario
# NDF into the target NDF, translating class / property / string / object-reference indices. This is
# what lets the editor place an HQ or pre-placed building into a scenario that didn't already contain one.
TRANS_REF_MARKER = getattr(ndfbin, "TRANS_REF_MARKER", 0xAAAAAAAA)


def _ensure_class(dst, name) -> int:
    c = dst.class_by_name(name)
    if c is not None:
        return c.index
    idx = len(dst.classes)
    dst.classes.append(NdfClass(index=idx, name=name))
    return idx


def _ensure_prop(dst, class_index, name) -> int:
    p = dst.prop_by_name_and_class(name, class_index)
    if p is not None:
        return p.index
    idx = len(dst.properties)
    dst.properties.append(NdfProperty(index=idx, name=name, class_index=class_index))
    return idx


def _src_prop_name(src, prop_index):
    for p in src.properties:
        if p.index == prop_index:
            return p.name
    return None


def _translate_value(dst, src, val, idx_map):
    tid = val.type_id
    if tid in (T.StringRef, T.PathRef):
        return NdfValue(tid, dst.ensure_string(src.get_string(val.raw)))
    if tid == T.Reference:
        marker, ref = val.raw
        if marker == OBJ_REF_MARKER:
            oi, _ci = ref
            if oi in idx_map:
                noi = idx_map[oi]
                return NdfValue(tid, (marker, (noi, dst.instances[noi].class_index)))
            return None     # reference to an instance we didn't import — drop the property
        if marker == TRANS_REF_MARKER:
            return NdfValue(tid, (marker, dst.ensure_trans(src.get_trans(ref))))
        return NdfValue(tid, val.raw)
    if tid == T.List:
        out = []
        for it in val.raw:
            tv = _translate_value(dst, src, it, idx_map)
            if tv is not None:
                out.append(tv)
        return NdfValue(tid, out)
    if tid == T.Pair:
        return NdfValue(tid, (_translate_value(dst, src, val.raw[0], idx_map),
                              _translate_value(dst, src, val.raw[1], idx_map)))
    if tid == T.Map:
        return NdfValue(tid, [(_translate_value(dst, src, k, idx_map),
                               _translate_value(dst, src, v, idx_map)) for k, v in val.raw])
    return NdfValue(tid, val.raw)      # scalars / vector3 / blob / guid / lochash — copy verbatim


def import_placement(dst, src, src_item_idx, src_addon_idx):
    """Copy the (item, addon) pair at the given SRC indices into DST. Appends two instances to
    dst.instances (NOT to top_objects, NOT yet to the item list — the caller links the item). Ensures
    the classes + properties exist in dst, re-interns strings/trans, and remaps the item->addon ref.
    Returns (new_item_idx, new_addon_idx)."""
    idx_map = {}
    for sidx in (src_addon_idx, src_item_idx):      # addon first so the item's AddOn ref resolves
        sinst = src.instances[sidx]
        cidx = _ensure_class(dst, src.classes[sinst.class_index].name)
        dst.instances.append(NdfInstance(class_index=cidx, props=[]))
        idx_map[sidx] = len(dst.instances) - 1
    for sidx in (src_addon_idx, src_item_idx):
        sinst = src.instances[sidx]
        ninst = dst.instances[idx_map[sidx]]
        for pv in sinst.props:
            pname = _src_prop_name(src, pv.prop_index)
            if pname is None:
                continue
            tv = _translate_value(dst, src, pv.value, idx_map)
            if tv is None:
                continue
            ninst.props.append(NdfPropertyValue(_ensure_prop(dst, ninst.class_index, pname), tv))
    return idx_map[src_item_idx], idx_map[src_addon_idx]


def schema_for(kind) -> Optional[PlacementClassSchema]:
    return SCHEMA.get(kind)


def all_kinds() -> List[str]:
    return [k for k in KIND_ORDER if k in SCHEMA]

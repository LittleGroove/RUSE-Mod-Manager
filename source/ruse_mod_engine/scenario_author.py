"""Scenario placement-authoring backend for the editor.

Loads a `.scenario`'s embedded GameDesignDatabase NDF into a typed Placement list, supports add / set /
remove, and re-emits a GAME-ACCEPTED `.scenario` (the re-emit path is proven in-game). This is the
MapView's model; it pairs with `script_edit` (mission logic) and `scenario_validate` (completeness).

  sc = Scenario.load(scenario_bytes)
  for p in sc.placements: ...                 # kind, name, position, props
  sc.add_zone('zone_ger', (x,y,z), radius=120000)
  sc.add_marker('spawn_reward_01', (x,y,z))
  sc.name_spawn(existing_placement, 'persh_01')
  new_scenario = sc.emit()

Placement kinds: unit | building | depot | spawn | marker | zone | hq
"""
from __future__ import annotations
import copy
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from . import scenario as S, ndfbin
from .ndfbin import NdfClass, NdfProperty, NdfInstance, NdfPropertyValue, make_value, val_float32

REF_TRAN = 3149642683
_ADDON = {
    "spawn": "TGameDesignAddOn_Spawn",
    "marker": "TGameDesignAddOn_Name",
    "zone": "TGameDesignAddOn_CircularZone",
    "rect": "TGameDesignAddOn_RectangleZone",
    "hq": "TGameDesignAddOn_StartingPoint",
}


@dataclass
class Placement:
    kind: str                       # unit|building|depot|spawn|marker|zone|hq
    name: Optional[str]
    position: Tuple[float, float, float]
    rotation: float = 0.0
    pyclass: Optional[str] = None   # spawn: the unit/building PythonClassName
    camp: Optional[int] = None
    radius: Optional[float] = None  # zone(circle)
    width: Optional[float] = None   # zone(rect)
    height: Optional[float] = None
    _item: int = -1                 # NDF TGameDesignItem instance index (internal)
    _addon: int = -1                # NDF AddOn instance index (internal)


def _spawn_kind(pyclass: Optional[str]) -> str:
    if not pyclass:
        return "spawn"
    p = pyclass.lower()
    if "dallebatimentdepot" in p or "depot" in p:
        return "depot"
    if "building_" in p:
        return "building"
    if "unit_" in p:
        return "unit"
    return "spawn"


class Scenario:
    def __init__(self, sc: dict):
        self._sc = sc
        self.nb = ndfbin.read(sc["ndf_data"])
        self.zones = sc.get("zones") or []
        self._index()

    @classmethod
    def load(cls, scenario_bytes: bytes) -> "Scenario":
        return cls(S.read(scenario_bytes))

    # ---- indexing ----
    def _ci(self, name): return next((c.index for c in self.nb.classes if c.name == name), None)

    def _prop(self, cls_idx, name):
        return next((p.index for p in self.nb.properties if p.class_index == cls_idx and p.name == name), None)

    def _index(self):
        nb = self.nb
        self.placements: List[Placement] = []
        ci_item = self._ci("TGameDesignItem")
        if ci_item is None:
            return
        p_pos = self._prop(ci_item, "Position"); p_rot = self._prop(ci_item, "Rotation")
        p_addon = self._prop(ci_item, "AddOn")
        # capture a Reference value template (ndfbin.make_value has no 'Reference' type)
        _ex = next((x for x in nb.instances if x.class_index == ci_item), None)
        if _ex is not None:
            self._ref_template = _ex.get(p_addon)
        cls_name = {c.index: c.name for c in nb.classes}
        for i, inst in enumerate(nb.instances):
            if inst.class_index != ci_item:
                continue
            pos = inst.get(p_pos); rot = inst.get(p_rot); ref = inst.get(p_addon)
            position = tuple(pos.raw) if pos else (0.0, 0.0, 0.0)
            rotation = rot.raw if rot else 0.0
            addon_idx = ref.raw[1][0] if ref else -1
            pl = self._addon_to_placement(addon_idx, cls_name)
            pl.position = position; pl.rotation = rotation; pl._item = i; pl._addon = addon_idx
            self.placements.append(pl)

    def _addon_to_placement(self, addon_idx, cls_name) -> Placement:
        nb = self.nb
        if addon_idx < 0 or addon_idx >= len(nb.instances):
            return Placement("spawn", None, (0, 0, 0))
        addon = nb.instances[addon_idx]
        cn = cls_name.get(addon.class_index, "")
        ci = addon.class_index
        def g(prop):
            pi = self._prop(ci, prop)
            v = addon.get(pi) if pi is not None else None
            return nb.resolve_value(v) if v is not None else None
        name = g("Name")
        name = str(name) if name not in (None, "None", "") else None
        if cn == "TGameDesignAddOn_Spawn":
            pyc = g("PythonClassName")
            return Placement(_spawn_kind(str(pyc) if pyc else None), name, (0, 0, 0),
                             pyclass=str(pyc) if pyc else None,
                             camp=int(g("Camp")) if g("Camp") is not None else None)
        if cn == "TGameDesignAddOn_CircularZone":
            r = g("Radius"); return Placement("zone", name, (0, 0, 0), radius=float(r) if r else None)
        if cn == "TGameDesignAddOn_RectangleZone":
            return Placement("zone", name, (0, 0, 0),
                             width=float(g("Width")) if g("Width") else None,
                             height=float(g("Height")) if g("Height") else None)
        if cn == "TGameDesignAddOn_StartingPoint":
            return Placement("hq", name, (0, 0, 0))
        return Placement("marker", name, (0, 0, 0))

    # ---- low-level NDF builder primitives (shared with the completer) ----
    def _ensure_class(self, name):
        ci = self._ci(name)
        if ci is not None:
            return ci
        ci = len(self.nb.classes); self.nb.classes.append(NdfClass(ci, name)); return ci

    def _ensure_prop(self, cls_idx, name):
        i = self._prop(cls_idx, name)
        if i is not None:
            return i
        i = len(self.nb.properties); self.nb.properties.append(NdfProperty(i, name, cls_idx)); return i

    def _ref(self, inst, cls):
        v = copy.deepcopy(self._ref_template); v.raw = (REF_TRAN, (inst, cls)); return v

    def _new_item(self, addon_idx, addon_cls, position, rotation=0.0):
        nb = self.nb
        ci_item = self._ci("TGameDesignItem")
        p_pos = self._prop(ci_item, "Position"); p_rot = self._prop(ci_item, "Rotation")
        p_addon = self._prop(ci_item, "AddOn")
        # copy a Vector3 value template for Position
        ex = next(x for x in nb.instances if x.class_index == ci_item)
        posv = copy.deepcopy(ex.get(p_pos)); posv.raw = tuple(float(c) for c in position)
        item_idx = len(nb.instances)
        nb.instances.append(NdfInstance(ci_item, [
            NdfPropertyValue(p_pos, posv),
            NdfPropertyValue(p_rot, val_float32(float(rotation))),
            NdfPropertyValue(p_addon, self._ref(addon_idx, addon_cls))]))
        # register in the root list
        lst = next(x for x in nb.instances if x.class_index == self._ci("TGameDesignItemList"))
        lst.props[0].value.raw.append(self._ref(item_idx, ci_item))
        return item_idx

    # ---- authoring operations ----
    def add_zone(self, name, position, radius=120000.0):
        cz = self._ensure_class("TGameDesignAddOn_CircularZone")
        pn = self._ensure_prop(cz, "Name"); pr = self._ensure_prop(cz, "Radius")
        idx = len(self.nb.instances)
        self.nb.instances.append(NdfInstance(cz, [
            NdfPropertyValue(pn, make_value("StringRef", self.nb.ensure_string(name))),
            NdfPropertyValue(pr, val_float32(float(radius)))]))
        self._new_item(idx, cz, position)
        self._index()

    def add_marker(self, name, position):
        cn = self._ensure_class("TGameDesignAddOn_Name")
        pn = self._ensure_prop(cn, "Name")
        idx = len(self.nb.instances)
        self.nb.instances.append(NdfInstance(cn, [
            NdfPropertyValue(pn, make_value("StringRef", self.nb.ensure_string(name)))]))
        self._new_item(idx, cn, position)
        self._index()

    def add_spawn(self, pyclass, position, camp=None, name=None, rotation=0.0):
        cs = self._ensure_class("TGameDesignAddOn_Spawn")
        p_pyc = self._ensure_prop(cs, "PythonClassName")
        props = [NdfPropertyValue(p_pyc, make_value("StringRef", self.nb.ensure_string(pyclass)))]
        if camp is not None:
            props.append(NdfPropertyValue(self._ensure_prop(cs, "Camp"), make_value("Int32", int(camp))))
        if name:
            props.append(NdfPropertyValue(self._ensure_prop(cs, "Name"), make_value("StringRef", self.nb.ensure_string(name))))
        idx = len(self.nb.instances)
        self.nb.instances.append(NdfInstance(cs, props))
        self._new_item(idx, cs, position, rotation)
        self._index()

    def set_name(self, placement: Placement, name: str):
        """Give an existing placement a Name (e.g., to bind a script tag to it)."""
        addon = self.nb.instances[placement._addon]
        pn = self._ensure_prop(addon.class_index, "Name")
        addon.set(pn, make_value("StringRef", self.nb.ensure_string(name)))
        self._index()

    # ---- scalar-property mutators (drag-move / inspector edits) ----
    def _vector3(self, xyz):
        ci_item = self._ci("TGameDesignItem")
        ex = next(x for x in self.nb.instances if x.class_index == ci_item)
        v = copy.deepcopy(ex.get(self._prop(ci_item, "Position")))
        v.raw = tuple(float(c) for c in xyz); return v

    def set_position(self, placement: Placement, xyz):
        item = self.nb.instances[placement._item]
        item.set(self._prop(self._ci("TGameDesignItem"), "Position"), self._vector3(xyz))
        self._index()

    def set_rotation(self, placement: Placement, rotation):
        item = self.nb.instances[placement._item]
        item.set(self._prop(self._ci("TGameDesignItem"), "Rotation"), val_float32(float(rotation)))
        self._index()

    def set_camp(self, placement: Placement, camp):
        addon = self.nb.instances[placement._addon]
        addon.set(self._ensure_prop(addon.class_index, "Camp"), make_value("Int32", int(camp)))
        self._index()

    def set_zone_radius(self, placement: Placement, radius):
        addon = self.nb.instances[placement._addon]
        addon.set(self._ensure_prop(addon.class_index, "Radius"), val_float32(float(radius)))
        self._index()

    def set_zone_rect(self, placement: Placement, width, height):
        addon = self.nb.instances[placement._addon]
        addon.set(self._ensure_prop(addon.class_index, "Width"), val_float32(float(width)))
        addon.set(self._ensure_prop(addon.class_index, "Height"), val_float32(float(height)))
        self._index()

    # ---- undo capture for item-level edits (addon-level capture is above) ----
    def item_props_copy(self, placement: Placement):
        return copy.deepcopy(self.nb.instances[placement._item].props)

    def restore_item_props(self, placement: Placement, props):
        self.nb.instances[placement._item].props = props
        self._index()

    # ---- undo support (adds only APPEND, so a snapshot is just the list lengths) ----
    def _itemlist(self):
        return next(x for x in self.nb.instances if x.class_index == self._ci("TGameDesignItemList"))

    def snapshot(self):
        return (len(self.nb.instances), len(self.nb.classes), len(self.nb.properties),
                len(self.nb.strings), len(self._itemlist().props[0].value.raw))

    def restore(self, snap):
        ni, nc, npr, ns, nl = snap
        del self.nb.instances[ni:]; del self.nb.classes[nc:]
        del self.nb.properties[npr:]; del self.nb.strings[ns:]
        del self._itemlist().props[0].value.raw[nl:]
        self._index()

    def list_entry_index(self, placement: Placement) -> int:
        """Position of a placement's item-ref in the root ItemList (for index-safe remove/re-add)."""
        refs = self._itemlist().props[0].value.raw
        for i, r in enumerate(refs):
            if getattr(r, "raw", (None, (None,)))[1][0] == placement._item:
                return i
        return -1

    def remove_list_entry(self, pos: int):
        """Index-safe 'delete': drop the item from the root list (instances stay, unreferenced)."""
        entry = self._itemlist().props[0].value.raw[pos]
        del self._itemlist().props[0].value.raw[pos]
        self._index()
        return entry

    def insert_list_entry(self, pos: int, entry):
        self._itemlist().props[0].value.raw.insert(pos, entry)
        self._index()

    def addon_props_copy(self, placement: Placement):
        # DEEP copy: set_name may mutate an existing NdfPropertyValue.value in place, so a shallow copy
        # would share (and lose) the old value. Deep copy keeps the pre-edit values for undo.
        return copy.deepcopy(self.nb.instances[placement._addon].props)

    def restore_addon_props(self, placement: Placement, props):
        self.nb.instances[placement._addon].props = props
        self._index()

    # ---- emit / validate ----
    def emit(self) -> bytes:
        self._sc["ndf_data"] = ndfbin.write(self.nb)
        return S.write(self._sc)

    def validate(self, effetmap_bytes):
        from . import scenario_validate as V
        return V.validate(self.emit(), effetmap_bytes)

"""Command / undo-redo layer for ScenarioDocument — the last backend piece before the GUI.

Every mutation is a Command (do/undo). The History stack gives the editor undo/redo. Mutations go through
the document's command methods (add_zone/add_marker/add_spawn/set_placement_name/remove_placement/
set_script_const/add_text); the raw .scenario/.script models stay read-accessible.

Undo strategy is cheap + exact:
  - scenario ADDS only ever APPEND (instances/classes/props/strings/list) -> undo = truncate to snapshot.
  - scenario SET-NAME modifies one pre-existing AddOn -> undo = restore that AddOn's props + truncate.
  - scenario REMOVE drops only the root-list entry (index-safe; instances stay unreferenced) -> undo = re-insert.
  - script SET-CONST is one tree node -> undo = restore old value.
  - add-text appends a loc entry -> undo = pop it.
"""
from __future__ import annotations
from typing import List, Optional


class Command:
    label = "edit"
    def do(self, doc): raise NotImplementedError
    def undo(self, doc): raise NotImplementedError


class History:
    def __init__(self):
        self._undo: List[Command] = []
        self._redo: List[Command] = []

    def run(self, doc, cmd: Command):
        cmd.do(doc); self._undo.append(cmd); self._redo.clear(); return cmd

    def undo(self, doc) -> Optional[str]:
        if not self._undo: return None
        c = self._undo.pop(); c.undo(doc); self._redo.append(c); return c.label

    def redo(self, doc) -> Optional[str]:
        if not self._redo: return None
        c = self._redo.pop(); c.do(doc); self._undo.append(c); return c.label

    def can_undo(self): return bool(self._undo)
    def can_redo(self): return bool(self._redo)
    def undo_label(self): return self._undo[-1].label if self._undo else None
    def redo_label(self): return self._redo[-1].label if self._redo else None
    def clear(self): self._undo.clear(); self._redo.clear()


# ---- script ----
class SetScriptConst(Command):
    def __init__(self, path, value):
        self.path, self.value, self._old = path, value, None
        self.label = "set constant"
    def do(self, doc):
        self._old = doc.script.get(self.path); doc.script.set(self.path, self.value)
    def undo(self, doc):
        doc.script.set(self.path, self._old)


# ---- scenario adds (truncate-undo) ----
class _AddBase(Command):
    def __init__(self): self._snap = None
    def _add(self, doc): raise NotImplementedError
    def do(self, doc):
        self._snap = doc.scenario.snapshot(); self._add(doc)
    def undo(self, doc):
        doc.scenario.restore(self._snap)


class AddZone(_AddBase):
    def __init__(self, name, position, radius=120000.0):
        super().__init__(); self.name, self.position, self.radius = name, position, radius
        self.label = "add zone %s" % name
    def _add(self, doc): doc.scenario.add_zone(self.name, self.position, self.radius)


class AddMarker(_AddBase):
    def __init__(self, name, position):
        super().__init__(); self.name, self.position = name, position
        self.label = "add marker %s" % name
    def _add(self, doc): doc.scenario.add_marker(self.name, self.position)


class AddSpawn(_AddBase):
    def __init__(self, pyclass, position, camp=None, name=None, rotation=0.0):
        super().__init__(); self.kw = dict(pyclass=pyclass, position=position, camp=camp, name=name, rotation=rotation)
        self.label = "add spawn %s" % pyclass
    def _add(self, doc): doc.scenario.add_spawn(**self.kw)


# ---- scenario set-name (restore the modified AddOn) ----
class SetPlacementName(Command):
    def __init__(self, placement, name):
        self.placement, self.name = placement, name
        self._snap = None; self._old_props = None
        self.label = "name placement %s" % name
    def do(self, doc):
        self._snap = doc.scenario.snapshot()
        self._old_props = doc.scenario.addon_props_copy(self.placement)
        doc.scenario.set_name(self.placement, self.name)
    def undo(self, doc):
        doc.scenario.restore_addon_props(self.placement, self._old_props)
        doc.scenario.restore(self._snap)


# ---- scalar-property mutators (drag-move / inspector); restore the modified instance + truncate ----
class _ModifyItem(Command):
    """Edits live on the parent TGameDesignItem (position / rotation)."""
    def __init__(self, placement): self.placement = placement; self._snap = None; self._props = None
    def _apply(self, doc): raise NotImplementedError
    def do(self, doc):
        self._snap = doc.scenario.snapshot()
        self._props = doc.scenario.item_props_copy(self.placement)
        self._apply(doc)
    def undo(self, doc):
        doc.scenario.restore_item_props(self.placement, self._props)
        doc.scenario.restore(self._snap)


class _ModifyAddon(Command):
    """Edits live on the AddOn (camp / zone geometry)."""
    def __init__(self, placement): self.placement = placement; self._snap = None; self._props = None
    def _apply(self, doc): raise NotImplementedError
    def do(self, doc):
        self._snap = doc.scenario.snapshot()
        self._props = doc.scenario.addon_props_copy(self.placement)
        self._apply(doc)
    def undo(self, doc):
        doc.scenario.restore_addon_props(self.placement, self._props)
        doc.scenario.restore(self._snap)


class SetPosition(_ModifyItem):
    def __init__(self, placement, xyz):
        super().__init__(placement); self.xyz = xyz; self.label = "move placement"
    def _apply(self, doc): doc.scenario.set_position(self.placement, self.xyz)


class SetRotation(_ModifyItem):
    def __init__(self, placement, rotation):
        super().__init__(placement); self.rotation = rotation; self.label = "rotate placement"
    def _apply(self, doc): doc.scenario.set_rotation(self.placement, self.rotation)


class SetCamp(_ModifyAddon):
    def __init__(self, placement, camp):
        super().__init__(placement); self.camp = camp; self.label = "set camp"
    def _apply(self, doc): doc.scenario.set_camp(self.placement, self.camp)


class SetZoneGeometry(_ModifyAddon):
    def __init__(self, placement, radius=None, width=None, height=None):
        super().__init__(placement); self.radius, self.width, self.height = radius, width, height
        self.label = "resize zone"
    def _apply(self, doc):
        if self.radius is not None:
            doc.scenario.set_zone_radius(self.placement, self.radius)
        else:
            doc.scenario.set_zone_rect(self.placement, self.width, self.height)


# ---- scenario remove (index-safe list-entry drop) ----
class RemovePlacement(Command):
    def __init__(self, placement):
        self.placement = placement; self._pos = -1; self._entry = None
        self.label = "remove placement"
    def do(self, doc):
        self._pos = doc.scenario.list_entry_index(self.placement)
        self._entry = doc.scenario.remove_list_entry(self._pos)
    def undo(self, doc):
        doc.scenario.insert_list_entry(self._pos, self._entry)


# ---- localization ----
class AddText(Command):
    def __init__(self, key_hash, text, **kw):
        self.key_hash, self.text, self.kw = key_hash, text, kw
        self._token = None
        self.label = "add text"
    def do(self, doc):
        self._token = doc._add_text_raw(self.key_hash, self.text, **self.kw)
    def undo(self, doc):
        kind = self._token[0]
        if kind == "file":
            _, target, old = self._token
            target.data = old
        else:
            _, grp, created = self._token
            if created:
                doc._mod.loc_patches.remove(grp)
            else:
                grp.entries.pop()

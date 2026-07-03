"""ScenarioDocument — the editor's top-level model.

One operation/campaign mission mod (rmod) presented as editable parts:
  .scenario   -> Scenario   (placements/zones; scenario_author)
  .script     -> Script     (mission logic constants; script_edit)
  registration (globals.cpp TChallengeMapInfo/Pack)              (registration)
  .dic text   -> loc entries (objective/name strings)

The GUI edits `.scenario` and `.script` in place, calls `validate()` (the anti-silent-break gate), and
`save(path)` to write a deployable rmod. `register_operation(...)` wires a NEW operation into the menu.

  doc = ScenarioDocument.from_rmod("Operation.rmod")
  doc.script.set(path, 50000)          # edit logic
  doc.scenario.add_zone("zone_x", pos) # edit placements
  print(doc.validate().summary())      # completeness check
  doc.save("Operation_edited.rmod")
"""
from __future__ import annotations
from typing import Optional, List

from . import mod_format, scenario_validate
from .scenario_author import Scenario
from .script_edit import Script
from .mod_format import ModPatchGroup, ModLocGroup, ModLocEntry
from .registration import OperationReg, operation_registration, current_challenge_list
from . import scenario_commands as _cmd


class ScenarioDocument:
    def __init__(self, mod: mod_format.RuseMod):
        self._mod = mod
        self._scen_file = self._find(".scenario")
        self._eff_file = self._find("effetmap.xyz")
        self.scenario: Optional[Scenario] = Scenario.load(self._scen_file.data) if self._scen_file else None
        self.script: Optional[Script] = Script.load(self._eff_file.data) if self._eff_file else None
        self.history = _cmd.History()

    # ---- io ----
    @classmethod
    def from_rmod(cls, path: str) -> "ScenarioDocument":
        return cls(mod_format.load(path))

    def _find(self, suffix):
        for g in self._mod.file_patches:
            for f in g.files:
                if f.path.endswith(suffix):
                    return f
        return None

    def _flush(self):
        """Write the edited models back into the rmod's file payloads."""
        if self.scenario and self._scen_file:
            self._scen_file.data = self.scenario.emit()
        if self.script and self._eff_file:
            self._eff_file.data = self.script.dump()

    def save(self, path: str):
        self._flush()
        mod_format.save(self._mod, path)

    # ---- metadata ----
    @property
    def name(self): return self._mod.name
    @name.setter
    def name(self, v): self._mod.name = v
    @property
    def game_version(self): return self._mod.game_version

    # ---- validation (gate before save/deploy) ----
    def validate(self):
        """Completeness check: every script tag binds to a complete placement. Returns a ValidationReport
        (or None if this isn't an operation/campaign mod)."""
        if not (self.scenario and self.script):
            return None
        return scenario_validate.validate(self.scenario.emit(), self.script.dump())

    def is_complete(self) -> bool:
        rep = self.validate()
        return rep is None or rep.ok

    # ---- undoable editing API (the GUI calls these; raw .scenario/.script stay read-accessible) ----
    def add_zone(self, name, position, radius=120000.0):
        return self.history.run(self, _cmd.AddZone(name, position, radius))
    def add_marker(self, name, position):
        return self.history.run(self, _cmd.AddMarker(name, position))
    def add_spawn(self, pyclass, position, camp=None, name=None, rotation=0.0):
        return self.history.run(self, _cmd.AddSpawn(pyclass, position, camp, name, rotation))
    def set_placement_name(self, placement, name):
        return self.history.run(self, _cmd.SetPlacementName(placement, name))
    def set_position(self, placement, xyz):
        return self.history.run(self, _cmd.SetPosition(placement, xyz))
    def set_rotation(self, placement, rotation):
        return self.history.run(self, _cmd.SetRotation(placement, rotation))
    def set_camp(self, placement, camp):
        return self.history.run(self, _cmd.SetCamp(placement, camp))
    def set_zone_geometry(self, placement, radius=None, width=None, height=None):
        return self.history.run(self, _cmd.SetZoneGeometry(placement, radius, width, height))
    def remove_placement(self, placement):
        return self.history.run(self, _cmd.RemovePlacement(placement))
    def set_script_const(self, path, value):
        return self.history.run(self, _cmd.SetScriptConst(path, value))
    def add_text(self, key_hash, text, **kw):
        return self.history.run(self, _cmd.AddText(key_hash, text, **kw))

    def undo(self): return self.history.undo(self)
    def redo(self): return self.history.redo(self)
    def can_undo(self): return self.history.can_undo()
    def can_redo(self): return self.history.can_redo()

    # ---- localization (raw — used by the AddText command) ----
    def _find_dic_file(self, dic_path):
        leaf = dic_path.split("/")[-1] if dic_path else ".dic"
        for g in self._mod.file_patches:
            for f in g.files:
                if f.path.endswith(".dic") and (dic_path is None or f.path.replace("\\", "/").endswith(leaf)):
                    return f
        return None

    def _add_text_raw(self, key_hash: str, text: str, dic_path: str = None, dat: str = None):
        """Make a LocHash resolve to `text`. Bakes into the mod's SHIPPED .dic file (self-contained,
        verifiable) when the mod ships that .dic; else falls back to a loc_patch (edits the game's .dic).
        Returns an undo token for the AddText command."""
        from . import dic as _dic
        target = self._find_dic_file(dic_path)
        key = bytes.fromhex(key_hash)
        if target is not None:
            old = target.data
            writer = _dic.set_entry if _dic.get_entry(old, key) is not None else _dic.add_entry
            target.data = writer(old, key, text)
            return ("file", target, old)
        grp = None; created = False
        for g in self._mod.loc_patches:
            if dic_path is None or g.dic == dic_path:
                grp = g; break
        if grp is None:
            grp = ModLocGroup(dat=dat or "Data/PC/190852/ZZ_Win.dat",
                              dic=dic_path or "genlocalisation/ww2/localisation/generated/us/flash_txt.dic",
                              entries=[])
            self._mod.loc_patches.append(grp); created = True
        grp.entries.append(ModLocEntry(key=key_hash, value=text, add=True))
        return ("loc", grp, created)

    # ---- registration (make a NEW operation appear in-menu) ----
    def register_operation(self, reg: OperationReg, target_globals_ndf,
                           globals_dat: str = None, globals_ndf_path: str = None):
        """Add the globals.cpp change-set that registers this operation. `target_globals_ndf` = the target
        build's parsed globals.cpp (to append to the existing ChallengeList)."""
        existing = current_challenge_list(target_globals_ndf)
        changes = operation_registration(reg, existing)
        self._mod.patches.append(ModPatchGroup(
            dat=globals_dat or "Data/PC/190852/ZZ_GladPatchableWin.dat",
            ndf=globals_ndf_path or "genglad/patchable/misc/globals.cpp.gladndfbin",
            changes=changes, create_if_missing=False, index_map=None))

    # ---- summary ----
    def summary(self) -> str:
        parts = ["ScenarioDocument %r (build %s)" % (self.name, self.game_version)]
        if self.scenario:
            from collections import Counter
            k = Counter(p.kind for p in self.scenario.placements)
            parts.append("  scenario: %d placements %s" % (len(self.scenario.placements), dict(k)))
        if self.script:
            parts.append("  script: %d editable constants" % len(self.script.constants()))
        rep = self.validate()
        if rep is not None:
            parts.append("  validation: " + ("COMPLETE" if rep.ok else "%d unbound tags" % len(rep.issues)))
        return "\n".join(parts)

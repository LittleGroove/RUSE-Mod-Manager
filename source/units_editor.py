"""
RUSE Units / Buildings / Weapons editor (tabbed).

Opened from the Mod Editor hub with a live ModProject; edits the shared ``everything.cpp`` NDF held
by the project so changes accumulate until you Save (per-window Save writes the whole project .dat).

Tabs:
  Units & Buildings — grouped by faction (Nationalite) and build menu (Factory). Edit unit stats,
                      the build menu, the upgrade chain, AND the linked weapon stats inline.
  Weapons           — every TAmmunition (the real weapon stats), labelled by the units that use it.

Weapon stats live on a shared TAmmunition reached via
  unit.WeaponDescriptor -> TWeaponDescriptor -> turret -> TMountedWeaponDescriptor -> Ammunition,
so editing a weapon affects every unit that shares that ammo.
"""
import copy
import os
import re
import sys
import tkinter as tk
from tkinter import ttk, messagebox

REPO = os.path.dirname(os.path.abspath(__file__))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
from ruse_mod_engine import mod_project as mp_mod  # noqa: E402
from ruse_mod_engine import ndfbin as ndfbin_mod  # noqa: E402
from ruse_mod_engine import dic as dic_mod  # noqa: E402
from ruse_mod_engine import clone as clone_mod  # noqa: E402
from i18n import t  # noqa: E402
import ui_util  # noqa: E402  — pixel-accurate, language-aware widget sizing

# ── Theme (mirrors mod_manager.py palette) ──────────────────────────────────────
_R_BG        = "#08101c"
_R_BG_PANEL  = "#0e1a2a"
_R_BG_WIDGET = "#060d18"
_R_BORDER    = "#243a5c"
_R_GOLD      = "#c8a020"
_R_GOLD_BRT  = "#e0c030"
_R_TEXT      = "#ccd8e8"
_R_TEXT_DIM  = "#3e5878"
_R_SEL_BG    = "#1a3060"
_F_MAIN = ("Courier New", 9)
_F_BOLD = ("Courier New", 9, "bold")
_F_HEAD = ("Courier New", 10, "bold")

_DESC_CLASSES = [
    ("TUniteAuSolDescriptor", "Ground", "unit"),
    ("TAvionDescriptor",      "Air",    "unit"),
    ("TInfanterieDescriptor", "Infantry", "unit"),
    ("TBatimentDescriptor",   "Building", "building"),
]

# Faction = the integer Nationalite property (US / neutral carry none).
_NATION_BY_VALUE = {1: "Germany", 2: "UK", 3: "France", 4: "Italy", 5: "USSR", 6: "Japan"}
_FACTIONS = ["All", "US", "Germany", "France", "UK", "USSR", "Italy", "Japan"]

# Build-menu category = the production building (the Factory value); confirmed vs the rusepedia.
_FACTORY_TYPE = {
    8: "Barracks", 9: "Airfield", 10: "Armor", 11: "Anti-Tank",
    12: "Prototype", 13: "Artillery & AA", 3: "Turret / Defense",
}
_TYPE_FACTORY = {v: k for k, v in _FACTORY_TYPE.items()}
_MENU_CHOICES = ["Barracks", "Armor", "Anti-Tank", "Artillery & AA", "Airfield",
                 "Prototype", "Turret / Defense"]
_CATEGORIES = ["All"] + _MENU_CHOICES + ["Buildings", "Other"]

_UPG_NONE = t("(standalone - not an upgrade)")

# kind: "scalar" | "list" (per-game-mode ints) | "boollist" (per-game-mode 0/1) | "factory" (menu dropdown)
# 5-element lists are indexed [0]=1945, [1]=1942, [2]=1939, [3]=Total War, [4]=Nuclear War.
_FIELDS = [
    (t("HP (SeuilMort)"),                  "SeuilMort",          "scalar"),
    (t("Pinned threshold (SeuilPinned)"),  "SeuilPinned",        "scalar"),
    (t("Speed (VitesseLineaire)"),         "VitesseLineaire",    "scalar"),
    (t("Combat speed (VitesseCombat)"),    "VitesseCombat",      "scalar"),
    (t("Acceleration (MaxAcceleration)"),  "MaxAcceleration",    "scalar"),
    (t("Deceleration (MaxDeceleration)"),  "MaxDeceleration",    "scalar"),
    (t("U-turn time (TempsDemiTour)"),     "TempsDemiTour",      "scalar"),
    (t("Road speed bonus (SpeedBonusOnRoad)"), "SpeedBonusOnRoad", "scalar"),
    (t("Vision range (DetectionBase)"),    "DetectionBase",      "scalar"),
    (t("Air vision (PorteeVisionVolant)"), "PorteeVisionVolant", "scalar"),
    (t("Radar signature (SignatureRadar)"), "SignatureRadar",    "scalar"),
    (t("Air attack range (PorteeAttackReflexAir)"), "PorteeAttackReflexAir", "scalar"),
    (t("Ground attack range (PorteeAttackReflexSol)"), "PorteeAttackReflexSol", "scalar"),
    (t("Build time (ProductionTime)"),     "ProductionTime",     "scalar"),
    (t("Price per game mode (ProductionPrice)"), "ProductionPrice", "list"),
    (t("Build menu (Factory)"),            "Factory",            "factory"),
    (t("Menu slot (PositionInMenu)"),      "PositionInMenu",     "scalar"),
    (t("Show in game mode (ShowInMenu) [1945,1942,1939,TotalWar,NuclearWar]"),
                                            "ShowInMenu",         "boollist"),
    (t("Upgrade price (UpgradePrice)"),    "UpgradePrice",       "scalar"),
    (t("Upgrade time (UpgradeTime)"),      "UpgradeTime",        "scalar"),
    (t("Building type (TypeBatiment)"),    "TypeBatiment",       "scalar"),
    (t("Build menu id (Menu)"),            "Menu",               "scalar"),
]

# Weapon stats (on the shared TAmmunition).
_AMMO_FIELDS = [
    (t("Damage (Puissance)"),            "Puissance",                       "scalar"),
    (t("Max range (PorteeMaximale)"),    "PorteeMaximale",                  "scalar"),
    (t("Min range (PorteeMinimale)"),    "PorteeMinimale",                  "scalar"),
    (t("Time between shots (TempsEntreDeuxTirs)"), "TempsEntreDeuxTirs",    "scalar"),
    (t("Shots per volley (NbTirParSalves)"), "NbTirParSalves",              "scalar"),
    (t("Reload between volleys (TempsEntreDeuxSalves)"), "TempsEntreDeuxSalves", "scalar"),
    (t("Dispersion (AngleDispersion)"),  "AngleDispersion",                 "scalar"),
    (t("Pin radius (RayonPinned)"),      "RayonPinned",                     "scalar"),
    (t("% direct fire (PourcentageTirDirect)"), "PourcentageTirDirect",     "scalar"),
    (t("% direct moving (PourcentageTirDirectEnMouvement)"), "PourcentageTirDirectEnMouvement", "scalar"),
    (t("Indirect fire 0/1 (TirIndirect)"), "TirIndirect",                   "scalar"),
    (t("Allow ambush 0/1 (AllowAmbushShot)"), "AllowAmbushShot",            "scalar"),
    (t("Weapon level (Level)"),          "Level",                           "scalar"),
    (t("Weapon class (Arme)"),           "Arme",                            "scalar"),
    (t("Projectile type (ProjectileType)"), "ProjectileType",               "scalar"),
]

# Numeric value types that the generic "other fields" section will expose.
_NUM_TIDS = {ndfbin_mod.T.Bool, ndfbin_mod.T.Int8, ndfbin_mod.T.Int16, ndfbin_mod.T.UInt16,
             ndfbin_mod.T.Int32, ndfbin_mod.T.UInt32, ndfbin_mod.T.Long,
             ndfbin_mod.T.Float32, ndfbin_mod.T.Float64}
# Never auto-expose these as editable (identity/reference fields handled elsewhere).
# Nationalite is INTENTIONALLY exposed: the Migrate dialog is the polished path, but power users
# need to see + tweak the raw int (e.g. set to 7+ to probe what the engine does with unknown nations).
_OTHER_SKIP = {"DescriptorId", "TrackingId", "AmmunitionId", "IconeType", "Key",
               "ClassNameForDebug", "UpgradeRequire", "IsUpgrade"}
_COVERED = {p for _, p, _ in _FIELDS} | _OTHER_SKIP

# Standard-unit conversion for distance/speed/accel fields (issue #6).  The raw game value and the
# standard-unit value are shown in TWO linked boxes — edit either and the other updates live (before
# Apply); the RAW value is still what gets committed.  Base factors: 260 raw = 1 metre, 130 raw =
# 1 km/h.  `_CONV_SPECS[kind]` = (raw units per 1 displayed unit, unit label, decimals shown):
#   distance → km  (260 * 1000),  speed → km/h (130),  accel → m/s² (260, but per time²).
_UNIT_CONV = {
    "VitesseLineaire": "speed", "VitesseCombat": "speed",
    "MaxAcceleration": "accel", "MaxDeceleration": "accel",
    "DetectionBase": "distance", "PorteeVisionVolant": "distance",
    "PorteeAttackReflexAir": "distance", "PorteeAttackReflexSol": "distance",
    "PorteeMaximale": "distance", "PorteeMinimale": "distance",
}
_CONV_SPECS = {
    "speed":    (130.0,    "km/h", 2),
    "accel":    (260.0,    "m/s²", 3),
    "distance": (260000.0, "km",   3),
}


def _nation_from_value(raw) -> str:
    try:
        return _NATION_BY_VALUE.get(int(raw), "US")
    except (TypeError, ValueError):
        return "US"


class UnitsEditorWindow(tk.Frame):
    """Embedded as a nested in-tab view (it used to be a Toplevel) — the Mod Editor hosts it and
    provides the Back button, so it carries no window chrome of its own."""
    def __init__(self, master, project: "mp_mod.ModProject", on_change=None, default_lang="us"):
        super().__init__(master)
        self.project = project
        self._on_change = on_change
        self._name_default_lang = default_lang or "us"
        self.configure(background=_R_BG)

        self._ndf = None
        self._descs = []
        self._shown = []
        self._sel = None
        self._field_rows = []
        self._row_defaults = {}   # id(current NdfValue) -> its clean-backup NdfValue (for Reset)
        self._upg_combo_var = None
        self._upg_candidates = {}
        self._upg_orig = None
        self._upg_require_pidx = None
        self._upg_isup_var = None
        self._upg_isup_orig = None
        self._upg_isup_chk = None
        # weapons tab
        self._ammo = []
        self._wpn_shown = []
        self._wpn_sel = None
        self._wpn_rows = []
        self._next_ammo_id = 1000   # counter for unique ids on cloned weapons
        # unit display names live in ZZ_Win.dat baseunite.dic (per language), keyed by the
        # descriptor's NameInMenuToken (an 8-byte LocHash). Edited one language at a time.
        self._name_lang_paths = {}   # lang code -> baseunite.dic entry path (per language)
        self._name_lang_order = []   # lang codes in display order (settings default first)
        self._name_key = None        # selected unit's NameInMenuToken bytes (or None)
        self._name_var = None        # the name entry for the currently-selected language
        self._name_lang_var = None   # the language dropdown var (friendly name)
        self._name_cur_lang = None   # currently-selected language code
        self._name_pending = {}      # lang code -> typed name (accumulated until Apply)
        self._name_orig_by_lang = {}  # lang code -> original .dic name (cache, per selected unit)
        self._name_clean_by_lang = {}  # lang code -> clean-backup .dic name (default, per selected unit)
        self._name_cur_lbl = None    # the dim 'current value' label (updated on language switch)
        self._name_def_lbl = None    # the gold 'default value' label (updated on language switch)
        # in-game name lookup for the LIST (token bytes -> name), parsed once per language. The list
        # shows the localized name picked in the "Name:" dropdown next to the internal descriptor name.
        self._loc_names = {}         # lang code -> {NameInMenuToken bytes: in-game name}
        self._list_lang_code = self._name_default_lang   # language shown in the unit list

        try:
            self._ndf = project.everything()
        except Exception as e:
            messagebox.showerror(t("Units Editor"),
                                 t("Could not load the gameplay data:\n{e}", e=e), parent=self)
            self.after(10, self.destroy)
            return

        self._index_descriptors()
        self._index_buildings()
        self._index_weapons()
        self._index_clean_defaults()
        self._index_name_dics()
        self._build_ui()
        self._apply_filter()
        self._wpn_apply_filter()

    # ── data helpers ──────────────────────────────────────────────────────────

    def _prop_index(self, class_index, prop_name):
        p = self._ndf.prop_by_name_and_class(prop_name, class_index)
        if p is None:
            p = self._ndf.prop_by_name(prop_name)
        return p.index if p else None

    def _prop_value(self, inst, prop_name):
        pidx = self._prop_index(inst.class_index, prop_name)
        return inst.get(pidx) if pidx is not None else None

    def _pname(self, prop_index):
        return (self._ndf.properties[prop_index].name
                if 0 <= prop_index < len(self._ndf.properties) else "?")

    def _nation_of_inst(self, inst):
        v = self._prop_value(inst, "Nationalite")
        return "US" if v is None else _nation_from_value(v.raw)

    def _category_of(self, inst, kind):
        if kind == "building":
            return "Buildings"
        v = self._prop_value(inst, "Factory")
        if v is None or not isinstance(v.raw, int):
            return "Other"
        return _FACTORY_TYPE.get(v.raw, "Other")

    def _name_of(self, inst):
        pidx = self._prop_index(inst.class_index, "ClassNameForDebug")
        if pidx is None:
            return ""
        v = inst.get(pidx)
        if v is None:
            return ""
        return self._ndf.get_string(v.raw) if isinstance(v.raw, int) else str(v.raw)

    # ObjRef navigation (for the weapon chain) ────────────────────────────────

    def _follow(self, val):
        if val is None or val.type_id != ndfbin_mod.T.Reference:
            return None, None
        marker, ref = val.raw
        if marker != ndfbin_mod.OBJ_REF_MARKER:
            return None, None
        oi, _cls = ref
        if 0 <= oi < len(self._ndf.instances):
            return self._ndf.instances[oi], oi
        return None, None

    def _list_refs(self, inst, substr):
        out = []
        for p in inst.props:
            if substr in self._pname(p.prop_index) and p.value.type_id == ndfbin_mod.T.List:
                for it in p.value.raw:
                    out.append(self._follow(it))
        return out

    def _ammo_for_unit(self, inst):
        """[(ammo_instance, ammo_index)] reached through the weapon chain."""
        out = []
        wd, _ = self._follow(self._prop_value(inst, "WeaponDescriptor"))
        if wd is None:
            return out
        for tur, _ in self._list_refs(wd, "Turret"):
            if tur is None:
                continue
            for mw, _ in self._list_refs(tur, "MountedWeapon"):
                if mw is None:
                    continue
                am, ai = self._follow(self._prop_value(mw, "Ammunition"))
                if am is not None and ai not in [o[1] for o in out]:
                    out.append((am, ai))
        return out

    # ── property mutation helpers (class-bound) ───────────────────────────────

    def _set_bool_prop(self, inst, name, value):
        v = self._prop_value(inst, name)
        if v is not None:
            v.raw = bool(value)
        elif value:
            p = self._ndf.prop_by_name_and_class(name, inst.class_index)
            if p is not None:
                inst.set(p.index, ndfbin_mod.NdfValue(ndfbin_mod.T.Bool, True))

    def _add_int_prop_if_missing(self, inst, name, default):
        p = self._ndf.prop_by_name_and_class(name, inst.class_index)
        if p is not None and inst.get(p.index) is None:
            inst.set(p.index, ndfbin_mod.NdfValue(ndfbin_mod.T.Int32, int(default)))

    def _remove_prop(self, inst, name):
        p = self._ndf.prop_by_name_and_class(name, inst.class_index)
        if p is not None and inst.get(p.index) is not None:
            inst.remove(p.index)

    # ── weapon duplicate / set / remove ───────────────────────────────────────
    # Only the TAmmunition is shared between units (each unit has its own WeaponDescriptor /
    # turret / mounted weapon), so cloning the ammo + repointing this unit's mounted weapon(s)
    # gives the unit a private weapon without affecting anyone else.

    def _unit_weapon_nodes(self, unit):
        """[(mounted_weapon_inst, ammo_inst, ammo_idx)] across the unit's weapon chain."""
        nodes = []
        wd, _ = self._follow(self._prop_value(unit, "WeaponDescriptor"))
        if wd is None:
            return nodes
        for tur, _ in self._list_refs(wd, "Turret"):
            if tur is None:
                continue
            for mw, _ in self._list_refs(tur, "MountedWeapon"):
                if mw is None:
                    continue
                am, ai = self._follow(self._prop_value(mw, "Ammunition"))
                nodes.append((mw, am, ai))
        return nodes

    def _clone_ammo(self, src, src_idx):
        clone = copy.deepcopy(src)
        aidp = self._prop_index(clone.class_index, "AmmunitionId")
        if aidp is not None and clone.get(aidp) is not None:
            self._next_ammo_id += 1
            clone.get(aidp).raw = self._next_ammo_id
        new_idx = len(self._ndf.instances)
        self._ndf.instances.append(clone)
        if src_idx in set(self._ndf.top_objects):   # ammo are top objects — keep the clone one too
            self._ndf.top_objects.append(new_idx)
        return clone, new_idx

    def _set_mw_ammo(self, mw, ammo_idx, ammo_cls):
        p = self._prop_index(mw.class_index, "Ammunition")
        if p is not None:
            mw.set(p, ndfbin_mod.NdfValue(ndfbin_mod.T.Reference,
                                          (ndfbin_mod.OBJ_REF_MARKER, (ammo_idx, ammo_cls))))

    def _duplicate_unit(self):
        """Clone the selected unit or building into a new descriptor (own ClassNameForDebug,
        DescriptorId, NameInMenuToken, display name). Nation is inherited from the source — change
        it manually in the raw-properties section to assign the clone to a different nation."""
        if not self._sel:
            return
        src = self._sel["inst"]
        src_idx = self._sel["inst_index"]
        src_name = self._sel["name"] or "(unnamed)"
        try:
            new_idx, new_cn, new_token = clone_mod.clone_descriptor(self._ndf, src_idx)
        except Exception as e:
            messagebox.showerror(t("Duplicate"),
                                 t("Could not duplicate {name}:\n{e}", name=src_name, e=e),
                                 parent=self)
            return

        dic_written = 0
        if new_token is not None:
            display = (self._name_orig(self._name_default_lang) or src_name) + " (copy)"
            try:
                dic_written = dic_mod.add_baseunite_entries(self.project, new_token, display)
            except Exception:
                pass

        self.project.mark_dirty("gameplay", mp_mod.EVERYTHING_PATH)
        self._index_descriptors()
        self._index_weapons()
        self._notify()
        self._search_var.set("")
        self._apply_filter()
        tgt = next((i for i, d in enumerate(self._shown) if d["inst_index"] == new_idx), None)
        if tgt is not None:
            self._lb.selection_clear(0, tk.END)
            self._lb.selection_set(tgt)
            self._lb.see(tgt)
            self._sel = self._shown[tgt]
            self._render_fields()
        messagebox.showinfo(
            t("Duplicate"),
            t("Cloned {src} -> {dst}. Display name added to {n} language(s). Edit Nationalite in "
              "the raw-properties section to assign it to a different nation.",
              src=src_name, dst=new_cn, n=dic_written),
            parent=self)

    def _migrate_dialog(self):
        """Pop a small modal Nation picker; on OK, call clone.migrate_descriptor and refresh."""
        if not self._sel:
            return
        src = self._sel["inst"]
        src_name = self._sel["name"] or "(unnamed)"
        cur_nat = self._sel["nation"]   # already resolved string, e.g. "Germany" or "US"

        win = tk.Toplevel(self)
        win.title(t("Migrate {name}", name=src_name))
        win.configure(background=_R_BG_PANEL)
        win.transient(self); win.grab_set()
        win.geometry("420x260")
        win.minsize(380, 220)

        # Pack the button row FIRST with side=bottom so it ALWAYS reserves space, even if the
        # description label below wraps onto extra lines. Otherwise top-packed widgets squeeze
        # the bottom frame off-screen on smaller window heights.
        br = tk.Frame(win, background=_R_BG_PANEL); br.pack(fill="x", side="bottom", pady=8, padx=8)

        target_var = tk.StringVar(value=cur_nat if cur_nat in _FACTIONS else "US")

        def do_migrate():
            target = target_var.get()
            win.destroy()
            self._do_migrate(target)

        ttk.Button(br, text=t("Migrate"), command=do_migrate).pack(side="right", padx=4)
        ttk.Button(br, text=t("Cancel"), command=win.destroy).pack(side="right")

        # Now the rest of the dialog content — it can grow vertically without hiding the buttons.
        tk.Label(win, text=t("Currently: {nat}", nat=cur_nat), background=_R_BG_PANEL,
                 foreground=_R_TEXT_DIM, font=_F_MAIN).pack(anchor="w", padx=12, pady=(12, 2))
        tk.Label(win, text=t("Migrate to:"), background=_R_BG_PANEL, foreground=_R_GOLD,
                 font=_F_BOLD).pack(anchor="w", padx=12, pady=(8, 2))
        options = [n for n in _FACTIONS if n != "All"]   # actual nations only, no filter sentinel
        mig_cb = ttk.Combobox(win, textvariable=target_var, values=options, width=20,
                              state="readonly")
        mig_cb.pack(anchor="w", padx=12)
        ui_util.fit_combobox(mig_cb)
        tk.Label(win, text=t("USA = removes the Nationalite property. Other nations set it to the "
                             "matching int. PositionInMenu is auto-reassigned to a free slot in the "
                             "target nation's factory; cross-nation UpgradeRequire is cleared."),
                 background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN,
                 wraplength=380, justify="left").pack(anchor="w", padx=12, pady=(8, 4))

    def _do_migrate(self, target_label: str):
        """Execute the migrate for the current selection to nation `target_label` (e.g. 'Germany',
        'US'). Split out of `_migrate_dialog` so headless tests can drive it without the modal."""
        if not self._sel:
            return
        src_name = self._sel["name"] or "(unnamed)"
        target_int = {v: k for k, v in _NATION_BY_VALUE.items()}.get(target_label)
        try:
            result = clone_mod.migrate_descriptor(self._ndf, self._sel["inst_index"], target_int)
        except Exception as e:
            messagebox.showerror(t("Migrate"),
                                 t("Could not migrate {name}:\n{e}", name=src_name, e=e),
                                 parent=self)
            return
        if not result["changed"]:
            messagebox.showinfo(t("Migrate"),
                                t("{name} is already in {nat} — nothing to do.",
                                  name=src_name, nat=target_label), parent=self)
            return
        self.project.mark_dirty("gameplay", mp_mod.EVERYTHING_PATH)
        sel_idx = self._sel["inst_index"]
        self._index_descriptors()
        self._notify()
        self._search_var.set("")
        # follow the migrated unit by retargeting the faction filter to its new nation
        if target_label in _FACTIONS:
            self._faction_var.set(target_label)
        self._apply_filter()
        tgt = next((i for i, d in enumerate(self._shown) if d["inst_index"] == sel_idx), None)
        if tgt is not None:
            self._lb.selection_clear(0, tk.END)
            self._lb.selection_set(tgt)
            self._lb.see(tgt)
            self._sel = self._shown[tgt]
            self._render_fields()
        slot_msg = (t(" PositionInMenu moved to {slot}.", slot=result["new_slot"])
                    if result["new_slot"] is not None else "")
        upg_msg = t(" Upgrade chain cleared (parent was in another nation).") \
            if result["upgrade_cleared"] else ""
        messagebox.showinfo(t("Migrate"),
                            t("Migrated {name} -> {nat}.{slot}{upg}",
                              name=src_name, nat=target_label, slot=slot_msg, upg=upg_msg),
                            parent=self)

    def _duplicate_ammo(self):
        """Clone the selected ammo into a new ammo (own values), on the Ammo tab."""
        if self._wpn_sel is None:
            return
        clone, new_idx = self._clone_ammo(self._wpn_sel["inst"], self._wpn_sel["idx"])
        new_id = self._prop_value(clone, "AmmunitionId")
        self.project.mark_dirty("gameplay", mp_mod.EVERYTHING_PATH)
        self._index_weapons()
        self._notify()
        # re-render the Units tab so its weapon ammo dropdowns pick up the new clone
        if self._sel:
            self._keep_scroll(self._fields_frame, self._render_fields)
        self._wpn_search_var.set("")    # clear filter so the new clone is visible
        self._wpn_apply_filter()
        tgt = next((i for i, a in enumerate(self._wpn_shown) if a["idx"] == new_idx), None)
        if tgt is not None:
            self._wpn_lb.selection_clear(0, tk.END)
            self._wpn_lb.selection_set(tgt)
            self._wpn_lb.see(tgt)
            self._wpn_sel = self._wpn_shown[tgt]
            self._render_wpn_fields()
        messagebox.showinfo(
            t("Duplicate ammo"),
            t("Created Ammo #{ammo_id} (a copy). Edit it here, then assign it to "
              "a unit's weapon on the Units tab (\"Set weapon's ammo to\").",
              ammo_id=(new_id.raw if new_id else '?')), parent=self)

    def _commit_weapon_ammo(self):
        """Apply each weapon's chosen ammo from the inline dropdowns (issue #7 — done on the main
        Apply, not a per-weapon 'Set ammo' button).  Returns the number of weapons re-pointed."""
        n = 0
        for mw, var, orig in getattr(self, "_wpn_ammo_pending", []):
            cur = var.get()
            if cur == orig:
                continue
            s = cur.lstrip("#").strip()
            a = next((a for a in self._ammo if str(a["id"]) == s), None)
            if a is None:
                continue
            self._set_mw_ammo(mw, a["idx"], a["inst"].class_index)
            n += 1
        return n

    def _remove_weapon(self):
        if not self._sel:
            return
        unit = self._sel["inst"]
        p = self._prop_index(unit.class_index, "WeaponDescriptor")
        if p is not None and unit.get(p) is not None:
            if not messagebox.askyesno(t("Remove weapon"),
                                       t("Remove the weapon from {name} "
                                         "(it will be unable to attack)?",
                                         name=self._sel['name']), parent=self):
                return
            unit.remove(p)
            self._after_weapon_change()

    def _after_weapon_change(self):
        self.project.mark_dirty("gameplay", mp_mod.EVERYTHING_PATH)
        self._index_weapons()
        self._wpn_apply_filter()
        if self._sel:
            self._render_fields()
        self._notify()

    # ── indexing ──────────────────────────────────────────────────────────────

    def _enum_class(self, cls_name):
        cls = self._ndf.class_by_name(cls_name)
        if cls is None:
            return []
        return [(i, inst) for i, inst in enumerate(self._ndf.instances)
                if inst.class_index == cls.index]

    def _index_descriptors(self):
        self._descs = []
        for cls_name, cls_label, kind in _DESC_CLASSES:
            for idx, inst in self._enum_class(cls_name):
                name = self._name_of(inst)
                self._descs.append({
                    "inst": inst, "inst_index": idx, "name": name,
                    "nation": self._nation_of_inst(inst), "cls_label": cls_label, "kind": kind,
                    "category": self._category_of(inst, kind),
                    "name_token": self._name_token(inst),   # 8-byte LocHash for in-game name lookup
                })
        self._descs.sort(key=lambda d: (d["nation"], d["category"], d["name"].lower()))

    def _index_buildings(self):
        """Enumerate TBatimentDescriptor instances (every actual factory/depot/HQ — INCLUDING any
        custom buildings added by the loaded mod). Replaces the abstract 'Build menu' category
        filter so the user can see, per specific building, which units are wired to it. Units appear
        under a building iff `unit.Nationalite == building.Nationalite AND unit.Factory ==
        building.TypeBatiment` — see memory/project_nation_building_wiring.md."""
        self._buildings = []
        for idx, inst in self._enum_class("TBatimentDescriptor"):
            tb = self._prop_value(inst, "TypeBatiment")
            if tb is None or not isinstance(tb.raw, int):
                continue   # not a real factory — generic platform stub etc.
            self._buildings.append({
                "inst": inst, "idx": idx,
                "name": self._name_of(inst),
                "nation": self._nation_of_inst(inst),
                "type_batiment": tb.raw,
            })
        self._buildings.sort(key=lambda b: (b["nation"], b["type_batiment"], b["name"].lower()))

    def _building_options(self, faction: str) -> list:
        """Labels for the toolbar filter dropdown, and the matching filter map (self._filter_specs:
        label -> ('type', tb) | ('building', b)).  Three groups:
          • 'All buildings'  — no filter
          • BUILDING TYPES   — filter every faction's units of one TypeBatiment (e.g. 'Any Armor — all
                               factions (TB=10)').  This is the issue #2 request: filter by TB# across
                               factions (and ANDed with the Faction box when one is chosen).
          • SPECIFIC buildings — the exact factory (Nat + TypeBatiment), to tell apart siblings like
                               ExperimentalFactoryGR vs Usine_Atomique_GER which both serve TB=12.
        All groups are scoped to the chosen faction."""
        self._filter_specs = {}
        opts = ["All buildings"]
        # Building-type entries: one per distinct TypeBatiment present in the (faction-scoped) buildings.
        types = sorted({b["type_batiment"] for b in self._buildings
                        if faction == "All" or b["nation"] == faction})
        for tb in types:
            label = self._type_label(tb)
            self._filter_specs[label] = ("type", tb)
            opts.append(label)
        # Specific-building entries.
        for b in self._buildings:
            if faction != "All" and b["nation"] != faction:
                continue
            label = self._building_label(b)
            self._filter_specs[label] = ("building", b)
            opts.append(label)
        return opts

    @staticmethod
    def _type_label(tb) -> str:
        name = _FACTORY_TYPE.get(tb)
        if name:
            return t("Any {name} — all factions (TB={tb})", name=name, tb=tb)
        return t("Any building type TB={tb} — all factions", tb=tb)

    @staticmethod
    def _building_label(b) -> str:
        return f"{b['nation']}: {b['name']} (TB={b['type_batiment']})"

    @staticmethod
    def _fit_dropdown(combo):
        """Size these long-value dropdowns (building/type/upgrade names) to fit their content —
        issue #5.1.  Pixel-accurate and language-aware (see ui_util), so it's correct in every
        language instead of guessing a character count that truncates Cyrillic/CJK."""
        ui_util.fit_combobox(combo, maximum=60)

    def _building_by_label(self, label: str):
        """Return the building dict for a SPECIFIC-building label, or None for anything else
        (used by the per-unit Factory dropdown, which only lists specific buildings)."""
        if not label or label == "All buildings":
            return None
        for b in self._buildings:
            if self._building_label(b) == label:
                return b
        return None

    def _filter_spec(self, label: str):
        """Resolve the toolbar filter dropdown's label to ('type', tb) | ('building', b) | None."""
        return getattr(self, "_filter_specs", {}).get(label)

    def _buildings_in_nation(self, nation_label: str) -> list:
        """Buildings belonging to a nation (used by the per-unit Factory dropdown). Includes the
        generic 'US/none' bucket if nation_label == 'US'."""
        return [b for b in self._buildings if b["nation"] == nation_label]

    def _buildings_for_factory(self, nation_label: str, factory_int: int) -> list:
        """Buildings of `nation_label` that the engine treats as hosts of units with
        Factory == factory_int (the Nat + TypeBatiment join). Multiple matches are normal — e.g.
        Germany has both ExperimentalFactoryGR and Usine_Atomique_GER for Factory=12."""
        return [b for b in self._buildings_in_nation(nation_label)
                if b["type_batiment"] == factory_int]

    # ── unit display names (ZZ_Win.dat baseunite.dic, keyed by NameInMenuToken LocHash) ──────────

    def _index_name_dics(self):
        """Map each language's baseunite.dic in the loc dat (lang code -> entry path). Display order
        puts the settings default first, then the canonical language order. No-ops (name editing
        disabled) if ZZ_Win.dat isn't available."""
        self._name_lang_paths = {}
        try:
            paths = self.project.entry_paths("loc", "baseunite.dic")
        except Exception:
            paths = []
        for p in paths:
            pl = p.replace("/", "\\").lower()
            m = re.search(r"translations\\([^\\]+)\\baseunite", pl)
            code = m.group(1) if m else ("dev" if "\\dev\\" in pl else None)
            if code:
                self._name_lang_paths[code] = p
        order = [c for c, _n in dic_mod.LANGUAGES if c in self._name_lang_paths]
        order += [c for c in self._name_lang_paths if c not in order]   # any unexpected extras
        if self._name_default_lang in order:                            # default language first
            order.remove(self._name_default_lang)
            order.insert(0, self._name_default_lang)
        self._name_lang_order = order

    def _loc_name_map(self, lang):
        """{NameInMenuToken bytes -> in-game name} for `lang`, parsed once from its baseunite.dic.
        Empty dict if that language's .dic isn't available. Cached; cleared when a name is edited."""
        if lang in self._loc_names:
            return self._loc_names[lang]
        table = {}
        path = self._name_lang_paths.get(lang)
        if path:
            try:
                for key, name in dic_mod.read(self.project.get_raw("loc", path)):
                    table[key] = name
            except Exception:
                pass
        self._loc_names[lang] = table
        return table

    def _loc_name(self, d, lang):
        """The `lang` in-game display name for descriptor `d`, or '' if it has none."""
        tok = d.get("name_token")
        if not tok:
            return ""
        return self._loc_name_map(lang).get(tok, "")

    def _name_token(self, inst):
        """The unit's NameInMenuToken (8-byte LocHash) as bytes, or None."""
        pidx = self._prop_index(inst.class_index, "NameInMenuToken")
        if pidx is None:
            return None
        v = inst.get(pidx)
        if v is None or not isinstance(v.raw, (bytes, bytearray)):
            return None
        return bytes(v.raw)

    def _name_orig(self, lang):
        """Original (on-disk) name for the selected unit's key in `lang`, or None if absent. Cached
        per selected unit (reset in _render_name)."""
        if lang in self._name_orig_by_lang:
            return self._name_orig_by_lang[lang]
        val = None
        path = self._name_lang_paths.get(lang)
        if path and self._name_key:
            try:
                val = dic_mod.get_entry(self.project.get_raw("loc", path), self._name_key)
            except Exception:
                val = None
        self._name_orig_by_lang[lang] = val
        return val

    def _name_default(self, lang):
        """The DEFAULT (clean-backup) in-game name for the selected unit's key in `lang`, or None.
        Read straight from the pristine ZZ_Win.dat so it shows the original even after the mod's own
        name edit has been saved. Cached per selected unit (reset in _render_name)."""
        if lang in self._name_clean_by_lang:
            return self._name_clean_by_lang[lang]
        val = None
        path = self._name_lang_paths.get(lang)
        if path and self._name_key:
            try:
                raw = self.project.clean_raw("loc", path)
                if raw is not None:
                    val = dic_mod.get_entry(raw, self._name_key)
            except Exception:
                val = None
        self._name_clean_by_lang[lang] = val
        return val

    def _index_weapons(self):
        users = {}
        for d in self._descs:
            if d["kind"] != "unit":
                continue
            for _am, ai in self._ammo_for_unit(d["inst"]):
                users.setdefault(ai, []).append(d["name"])
        self._ammo = []
        for idx, inst in self._enum_class("TAmmunition"):
            idv = self._prop_value(inst, "AmmunitionId")
            self._ammo.append({"inst": inst, "idx": idx,
                               "id": idv.raw if idv is not None else "?",
                               "users": users.get(idx, [])})
        self._ammo.sort(key=lambda a: a["id"] if isinstance(a["id"], int) else 1 << 62)
        ids = [a["id"] for a in self._ammo if isinstance(a["id"], int)]
        self._next_ammo_id = max(ids) if ids else 1000

    # ── default (clean-backup) values ─────────────────────────────────────────
    # The editor shows the ORIGINAL game value beside the (possibly edited) current one. The original
    # comes from the pristine backup for the build this project targets, parsed into a SEPARATE NDF.
    # Instances are matched by STABLE identity (ClassNameForDebug for units/buildings, AmmunitionId
    # for ammo), NOT by instance index — clones append new instances and shift indices, so an index
    # match would line up the wrong descriptor. A cloned/new thing has no clean match → no default.

    def _index_clean_defaults(self):
        """Build the clean-backup lookup: a separate NDF of the pristine everything.cpp, plus an
        identity→instance map. No-ops gracefully (no default column) when no backup is available."""
        self._clean_ndf = None
        self._clean_by_id = {}
        try:
            cn = self.project.clean_everything()
        except Exception:
            cn = None
        if cn is None:
            return
        self._clean_ndf = cn
        for cls_name, _lbl, _kind in _DESC_CLASSES:
            cls = cn.class_by_name(cls_name)
            if cls is None:
                continue
            for inst in cn.instances:
                if inst.class_index != cls.index:
                    continue
                name = self._clean_str(inst, "ClassNameForDebug")
                if name:
                    self._clean_by_id[("name", name)] = inst
        acls = cn.class_by_name("TAmmunition")
        if acls is not None:
            for inst in cn.instances:
                if inst.class_index != acls.index:
                    continue
                aid = self._clean_value(inst, "AmmunitionId")
                if aid is not None and isinstance(aid.raw, int):
                    self._clean_by_id[("ammo", aid.raw)] = inst

    def _clean_value(self, clean_inst, prop_name):
        """An NdfValue for `prop_name` on a CLEAN-NDF instance (its property table is separate from
        the editable NDF's, so resolve the prop against the clean NDF)."""
        if clean_inst is None or self._clean_ndf is None:
            return None
        p = self._clean_ndf.prop_by_name_and_class(prop_name, clean_inst.class_index)
        if p is None:
            p = self._clean_ndf.prop_by_name(prop_name)
        return clean_inst.get(p.index) if p else None

    def _clean_str(self, clean_inst, prop_name):
        """A StringRef-or-string property on a clean-NDF instance, resolved to text (or None)."""
        v = self._clean_value(clean_inst, prop_name)
        if v is None:
            return None
        return self._clean_ndf.get_string(v.raw) if isinstance(v.raw, int) else str(v.raw)

    def _clean_match(self, inst):
        """The clean-backup instance matching `inst` (from the editable NDF) by stable identity, or
        None when there's no backup or the thing is new (a clone)."""
        if self._clean_ndf is None:
            return None
        aid = self._prop_value(inst, "AmmunitionId")
        if aid is not None and isinstance(aid.raw, int):
            m = self._clean_by_id.get(("ammo", aid.raw))
            if m is not None:
                return m
        name = self._name_of(inst)
        return self._clean_by_id.get(("name", name)) if name else None

    def _default_str(self, clean_inst, prop, kind):
        """The formatted DEFAULT value of `prop` on `clean_inst`, or None when there's no clean
        match / the property is absent. Formatted with the same `_fmt_value` as the current value so
        an unchanged field reads identically and is suppressed by the caller."""
        v = self._clean_value(clean_inst, prop)
        if v is None:
            return None
        try:
            return self._fmt_value(v, kind)
        except Exception:
            return None

    # ── UI ────────────────────────────────────────────────────────────────────

    def _scrollable(self, parent):
        # Shared scroll wrapper (issue #12): content that fits stays pinned to the top, and the mouse
        # wheel scrolls the widget under the pointer (e.g. an inner notes box), not this whole canvas.
        return ui_util.make_scrollable(parent, bg=_R_BG_PANEL)

    def _keep_scroll(self, frame, fn):
        """Run a re-render fn that rebuilds `frame`, restoring its scroll position after."""
        cv = getattr(frame, "_canvas", None)
        pos = cv.yview()[0] if cv is not None else None
        fn()
        if cv is not None and pos is not None:
            frame.update_idletasks()
            cv.yview_moveto(pos)

    def _build_ui(self):
        self._nb = ttk.Notebook(self)
        self._nb.pack(fill="both", expand=True, padx=4, pady=(6, 2))
        t1 = ttk.Frame(self._nb)
        t2 = ttk.Frame(self._nb)
        self._nb.add(t1, text=t("  Units & Buildings  "))
        self._nb.add(t2, text=t("  Ammo  "))
        self._build_units_tab(t1)
        self._build_weapons_tab(t2)

        bottom = tk.Frame(self, background=_R_BG)
        bottom.pack(fill="x", padx=8, pady=(0, 2))
        ttk.Button(bottom, text=t("Save mod (.dat)"), command=self._save_to_mod).pack(side="left")
        self._save_status = tk.Label(bottom, text="", background=_R_BG,
                                     foreground=_R_TEXT_DIM, font=_F_MAIN)
        self._save_status.pack(side="right")
        tk.Label(self, text=t("Apply commits the current selection's edits into the project. "
                              "“Save mod (.dat)” writes ALL accumulated changes to the mod's .dat. "
                              "Weapon stats are shared — editing one affects every unit that uses it."),
                 background=_R_BG, foreground=_R_TEXT_DIM, font=_F_MAIN, justify="left",
                 wraplength=1100).pack(fill="x", padx=8, pady=(0, 6))

    def _build_units_tab(self, parent):
        bar = tk.Frame(parent, background=_R_BG_PANEL)
        bar.pack(fill="x", padx=4, pady=(6, 4))
        tk.Label(bar, text=t("Faction:"), background=_R_BG_PANEL, foreground=_R_GOLD,
                 font=_F_BOLD).pack(side="left")
        self._faction_var = tk.StringVar(value="All")
        fac = ttk.Combobox(bar, textvariable=self._faction_var, values=_FACTIONS,
                           width=12, state="readonly")
        fac.pack(side="left", padx=(4, 10))
        ui_util.fit_combobox(fac, minimum=8)
        # Changing faction refreshes the building dropdown to that faction's buildings, then refilters.
        fac.bind("<<ComboboxSelected>>", lambda *_: self._on_faction_changed())
        # Hidden back-compat var — older tests/probes set this to a category name (e.g. "Armor") to
        # narrow the unit list. We honor it as a fallback in _apply_filter.
        self._type_var = tk.StringVar(value="All")
        tk.Label(bar, text=t("Building / Type:"), background=_R_BG_PANEL, foreground=_R_GOLD,
                 font=_F_BOLD).pack(side="left")
        self._building_var = tk.StringVar(value="All buildings")
        self._building_combo = ttk.Combobox(bar, textvariable=self._building_var,
                                            values=self._building_options("All"),
                                            width=40, state="readonly")
        self._building_combo.pack(side="left", padx=(4, 10))
        self._fit_dropdown(self._building_combo)   # full building/type names in the list (issue #5.1)
        self._building_combo.bind("<<ComboboxSelected>>", lambda *_: self._apply_filter())
        # Localisation: pick which language's in-game names appear next to the internal names. Only
        # shown when at least one baseunite.dic language is available in the loaded data.
        langs = self._name_lang_order or []
        if langs:
            start = self._list_lang_code if self._list_lang_code in langs else langs[0]
            self._list_lang_code = start
            tk.Label(bar, text=t("Name:"), background=_R_BG_PANEL, foreground=_R_GOLD,
                     font=_F_BOLD).pack(side="left")
            self._list_lang_var = tk.StringVar(value=dic_mod.lang_label(start))
            lang_combo = ttk.Combobox(bar, textvariable=self._list_lang_var, state="readonly",
                                      values=[dic_mod.lang_label(c) for c in langs], width=14)
            lang_combo.pack(side="left", padx=(4, 10))
            ui_util.fit_combobox(lang_combo)
            lang_combo.bind("<<ComboboxSelected>>", lambda *_: self._on_list_lang())
        tk.Label(bar, text=t("Search:"), background=_R_BG_PANEL, foreground=_R_GOLD,
                 font=_F_BOLD).pack(side="left")
        self._search_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self._search_var, width=20).pack(side="left", padx=4)
        self._search_var.trace_add("write", lambda *_: self._apply_filter())
        self._count_lbl = tk.Label(bar, text="", background=_R_BG_PANEL,
                                   foreground=_R_TEXT_DIM, font=_F_MAIN)
        self._count_lbl.pack(side="right")

        body = tk.Frame(parent, background=_R_BG)
        body.pack(fill="both", expand=True, padx=4, pady=2)
        left = tk.Frame(body, background=_R_BG_PANEL)
        left.pack(side="left", fill="y")
        lwrap = tk.Frame(left, background=_R_BG_PANEL)
        lwrap.pack(fill="y", expand=True)
        # Wide enough to show the localized name + internal name side by side; a horizontal
        # scrollbar covers the occasional row that's longer than the visible width.
        self._lb = tk.Listbox(lwrap, width=52, activestyle="none", background=_R_BG_WIDGET,
                              foreground=_R_TEXT, selectbackground=_R_SEL_BG,
                              selectforeground=_R_GOLD_BRT, font=_F_MAIN, exportselection=False)
        sb = ttk.Scrollbar(lwrap, orient="vertical", command=self._lb.yview)
        hsb = ttk.Scrollbar(lwrap, orient="horizontal", command=self._lb.xview)
        self._lb.configure(yscrollcommand=sb.set, xscrollcommand=hsb.set)
        self._lb.grid(row=0, column=0, sticky="ns")
        sb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        lwrap.rowconfigure(0, weight=1)
        # Debounced load + instant 'Loading …' feedback: a click-drag down the list no longer fires a
        # heavy render for every row it passes; only the settled selection loads, and it always matches
        # the highlighted row (issue: selecting a unit was laggy and could load the wrong one).
        ui_util.debounce_load(self._lb, self._on_select, on_peek=self._peek_unit)

        right = tk.Frame(body, background=_R_BG_PANEL)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self._hdr = tk.Label(right, text=t("Select a unit or building"), background=_R_BG_PANEL,
                             foreground=_R_GOLD_BRT, font=_F_HEAD, anchor="w", justify="left")
        self._hdr.pack(fill="x", padx=6, pady=(4, 0))
        self._sub = tk.Label(right, text="", background=_R_BG_PANEL, foreground=_R_TEXT_DIM,
                             font=_F_MAIN, anchor="w", justify="left")
        self._sub.pack(fill="x", padx=6)
        btnrow = tk.Frame(right, background=_R_BG_PANEL)
        btnrow.pack(side="bottom", fill="x", padx=6, pady=(0, 6))
        self._apply_btn = ttk.Button(btnrow, text=t("Apply changes to this unit"),
                                     command=self._apply_edits, state="disabled")
        self._apply_btn.pack(side="left")
        self._reset_btn = ttk.Button(btnrow, text=t("Reset to defaults"),
                                     command=self._reset_to_defaults, state="disabled")
        self._reset_btn.pack(side="left", padx=8)
        self._dup_btn = ttk.Button(btnrow, text=t("Duplicate this unit"),
                                   command=self._duplicate_unit, state="disabled")
        self._dup_btn.pack(side="left", padx=8)
        self._mig_btn = ttk.Button(btnrow, text=t("Migrate to nation..."),
                                   command=self._migrate_dialog, state="disabled")
        self._mig_btn.pack(side="left")
        self._status = tk.Label(btnrow, text="", background=_R_BG_PANEL,
                                foreground=_R_TEXT_DIM, font=_F_MAIN)
        self._status.pack(side="right")
        fwrap = tk.Frame(right, background=_R_BG_PANEL)
        fwrap.pack(fill="both", expand=True, padx=2, pady=4)
        self._fields_frame = self._scrollable(fwrap)

    def _build_weapons_tab(self, parent):
        bar = tk.Frame(parent, background=_R_BG_PANEL)
        bar.pack(fill="x", padx=4, pady=(6, 4))
        tk.Label(bar, text=t("Search ammo (id or unit):"), background=_R_BG_PANEL,
                 foreground=_R_GOLD, font=_F_BOLD).pack(side="left")
        self._wpn_search_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self._wpn_search_var, width=24).pack(side="left", padx=4)
        self._wpn_search_var.trace_add("write", lambda *_: self._wpn_apply_filter())
        self._wpn_count_lbl = tk.Label(bar, text="", background=_R_BG_PANEL,
                                       foreground=_R_TEXT_DIM, font=_F_MAIN)
        self._wpn_count_lbl.pack(side="right")

        body = tk.Frame(parent, background=_R_BG)
        body.pack(fill="both", expand=True, padx=4, pady=2)
        left = tk.Frame(body, background=_R_BG_PANEL)
        left.pack(side="left", fill="y")
        lwrap = tk.Frame(left, background=_R_BG_PANEL)
        lwrap.pack(fill="y", expand=True)
        self._wpn_lb = tk.Listbox(lwrap, width=46, activestyle="none", background=_R_BG_WIDGET,
                                  foreground=_R_TEXT, selectbackground=_R_SEL_BG,
                                  selectforeground=_R_GOLD_BRT, font=_F_MAIN, exportselection=False)
        ui_util.with_scrollbars(lwrap, self._wpn_lb)   # horizontal scroll for long weapon labels (#5.4)
        ui_util.debounce_load(self._wpn_lb, self._on_wpn_select, on_peek=self._peek_wpn)

        right = tk.Frame(body, background=_R_BG_PANEL)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self._wpn_hdr = tk.Label(right, text=t("Select an ammo"), background=_R_BG_PANEL,
                                 foreground=_R_GOLD_BRT, font=_F_HEAD, anchor="w", justify="left")
        self._wpn_hdr.pack(fill="x", padx=6, pady=(4, 0))
        self._wpn_sub = tk.Label(right, text="", background=_R_BG_PANEL, foreground=_R_TEXT_DIM,
                                 font=_F_MAIN, anchor="w", justify="left", wraplength=560)
        self._wpn_sub.pack(fill="x", padx=6)
        wbtn = tk.Frame(right, background=_R_BG_PANEL)
        wbtn.pack(side="bottom", fill="x", padx=6, pady=(0, 6))
        self._wpn_apply_btn = ttk.Button(wbtn, text=t("Apply changes to this ammo"),
                                         command=self._apply_wpn, state="disabled")
        self._wpn_apply_btn.pack(side="left")
        self._wpn_reset_btn = ttk.Button(wbtn, text=t("Reset to defaults"),
                                         command=self._reset_wpn_defaults, state="disabled")
        self._wpn_reset_btn.pack(side="left", padx=8)
        self._wpn_dup_btn = ttk.Button(wbtn, text=t("Duplicate this ammo"),
                                       command=self._duplicate_ammo, state="disabled")
        self._wpn_dup_btn.pack(side="left", padx=8)
        self._wpn_status = tk.Label(wbtn, text="", background=_R_BG_PANEL,
                                    foreground=_R_TEXT_DIM, font=_F_MAIN)
        self._wpn_status.pack(side="right")
        wf = tk.Frame(right, background=_R_BG_PANEL)
        wf.pack(fill="both", expand=True, padx=2, pady=4)
        self._wpn_fields_frame = self._scrollable(wf)

    # ── shared rendering ──────────────────────────────────────────────────────

    def _fmt_value(self, val, kind="scalar"):
        raw = val.raw
        if kind == "factory":
            return _FACTORY_TYPE.get(raw, str(raw)) if isinstance(raw, int) else str(raw)
        if kind == "boollist" and isinstance(raw, list):
            return ", ".join("1" if getattr(e, "raw", e) else "0" for e in raw)
        if isinstance(raw, list):
            return ", ".join(str(getattr(e, "raw", e)) for e in raw)
        return str(raw)

    def _section(self, container, title):
        tk.Label(container, text=title, anchor="w", background=_R_BG_PANEL,
                 foreground=_R_GOLD_BRT, font=_F_BOLD).pack(fill="x", padx=2, pady=(10, 2))

    # Four columns spanning the FULL width: a wrapping label pinned to the LEFT edge (column 0
    # stretches), the DEFAULT (clean-backup) preview, the CURRENT-value preview, then the entry pinned
    # to the RIGHT edge. The preview columns truncate-with-ellipsis and reveal the full value on hover
    # (ui_util.value_cell), so a wide value is never silently clipped. The label column gives up room.
    _DEF_W = 18         # chars: default (clean-backup) preview column — shown only when it differs
    _VAL_W = 22         # chars: current-value preview column
    _ENT_W = 28         # chars: editable entry / combobox

    @staticmethod
    def _wrap_label(parent, text, fg):
        lab = tk.Label(parent, text=text, anchor="w", justify="left", background=_R_BG_PANEL,
                       foreground=fg, font=_F_MAIN)
        lab.grid(row=0, column=0, sticky="ew", padx=(2, 14))
        lab.bind("<Configure>", lambda e, L=lab: L.configure(wraplength=max(e.width - 4, 80)))
        return lab

    def _col_header(self, container):
        hdr = tk.Frame(container, background=_R_BG_PANEL)
        hdr.pack(fill="x", pady=(0, 2))
        hdr.grid_columnconfigure(0, weight=1)
        tk.Label(hdr, text=t("Field"), anchor="w", background=_R_BG_PANEL,
                 foreground=_R_GOLD, font=_F_BOLD).grid(row=0, column=0, sticky="w", padx=(2, 14))
        tk.Label(hdr, text=t("Default"), width=self._DEF_W, anchor="e", background=_R_BG_PANEL,
                 foreground=_R_GOLD, font=_F_BOLD).grid(row=0, column=1, sticky="e", padx=(0, 10))
        tk.Label(hdr, text=t("Current"), width=self._VAL_W, anchor="e", background=_R_BG_PANEL,
                 foreground=_R_GOLD, font=_F_BOLD).grid(row=0, column=2, sticky="e", padx=(0, 12))
        tk.Label(hdr, text=t("New value"), width=self._ENT_W, anchor="e", background=_R_BG_PANEL,
                 foreground=_R_GOLD, font=_F_BOLD).grid(row=0, column=3, sticky="e", padx=(0, 4))

    def _factory_display(self, raw):
        """Rich 'Factory' value for display: ``<raw> (<menu type>): <hosting building(s)>`` — the raw
        value, which build menu it is, AND the actual building(s) that host it for the selected unit's
        nation (the Nat + TypeBatiment join can match several). Falls back to the bare type/raw when a
        unit isn't selected or the value isn't an int."""
        if self._sel is None or not isinstance(raw, int):
            return _FACTORY_TYPE.get(raw, str(raw)) if isinstance(raw, int) else str(raw)
        hosts = self._buildings_for_factory(self._sel["nation"], raw)
        host_names = ", ".join(b["name"] for b in hosts) if hosts else "(no matching building)"
        return f"{raw} ({_FACTORY_TYPE.get(raw, '?')}): {host_names}"

    def _add_field_row(self, container, rows, label, prop, kind, val, clean_inst=None):
        row = tk.Frame(container, background=_R_BG_PANEL)
        row.pack(fill="x", pady=2)
        row.grid_columnconfigure(0, weight=1)
        self._wrap_label(row, label, _R_TEXT)
        cur = self._fmt_value(val, kind)
        default_val = self._clean_value(clean_inst, prop)
        if default_val is not None:
            self._row_defaults[id(val)] = default_val   # remembered for the Reset-to-defaults button
        # CURRENT value text — column 2. The factory row shows the rich 'raw (type): buildings' form
        # (which menu + which actual building host this unit); every other row shows the plain value.
        cur_display = self._factory_display(val.raw) if kind == "factory" else cur
        # DEFAULT (clean-backup) value — column 1, shown ONLY when it differs from the current value
        # (an unchanged field stays clean; an edited field's original stands out). The factory default
        # uses the SAME rich form as the current cell so both read 'raw (type): buildings', not a bare
        # type word; differs is judged on the raw value. Truncates with an ellipsis + reveals the full
        # value on hover, so a wide default is never clipped.
        if kind == "factory":
            if (default_val is not None and isinstance(default_val.raw, int)
                    and default_val.raw != (val.raw if isinstance(val.raw, int) else None)):
                ui_util.value_cell(row, self._factory_display(default_val.raw), self._DEF_W,
                                   background=_R_BG_PANEL, foreground=_R_GOLD, font=_F_MAIN
                                   ).grid(row=0, column=1, sticky="e", padx=(0, 10))
        else:
            default_str = None
            if default_val is not None:
                try:
                    default_str = self._fmt_value(default_val, kind)
                except Exception:
                    default_str = None
            if default_str is not None and default_str != cur:
                ui_util.value_cell(row, default_str, self._DEF_W, background=_R_BG_PANEL,
                                   foreground=_R_GOLD, font=_F_MAIN
                                   ).grid(row=0, column=1, sticky="e", padx=(0, 10))
        # CURRENT value — column 2. The factory row wraps its (intentionally long) host list in full;
        # every other row truncates-with-tooltip to keep the grid aligned (ui_util.value_cell).
        if kind == "factory":
            tk.Label(row, text=cur_display, anchor="e", background=_R_BG_PANEL,
                     foreground=_R_TEXT_DIM, font=_F_MAIN, wraplength=480, justify="right"
                     ).grid(row=0, column=2, sticky="e", padx=(0, 12))
        else:
            ui_util.value_cell(row, cur_display, self._VAL_W, background=_R_BG_PANEL,
                               foreground=_R_TEXT_DIM, font=_F_MAIN
                               ).grid(row=0, column=2, sticky="e", padx=(0, 12))
        var = tk.StringVar(value=cur)
        if kind == "factory":
            # List actual buildings in the unit's nation so the user picks a real factory rather
            # than an abstract category name. Selecting a building writes its TypeBatiment to Factory.
            nation = self._sel["nation"] if self._sel is not None else "All"
            options = [self._building_label(b) for b in self._buildings_in_nation(nation)]
            cb = ttk.Combobox(row, textvariable=var, values=options, width=self._ENT_W,
                              state="readonly")
            cb.grid(row=0, column=3, sticky="e", padx=(0, 4))
            self._fit_dropdown(cb)   # show full building names in the drop-down (issue #5.1)
        else:
            ttk.Entry(row, textvariable=var, width=self._ENT_W).grid(row=0, column=3, sticky="e", padx=(0, 4))
        # Distance/speed/accel fields: a second, EDITABLE box in standard units (km / km/h / m/s²),
        # two-way-linked to the raw box — edit either and the other updates live (issue #6).
        if prop in _UNIT_CONV:
            self._add_conv_input(row, prop, val, var)
        rows.append((prop, kind, val, var, cur))
        # Zebra-stripe: alternate row shade so the eye can follow a row to its value box (issue #5.2).
        ui_util.apply_row_bg(row, ui_util.row_bg(getattr(self, "_zebra_i", 0), _R_BG_PANEL))
        self._zebra_i = getattr(self, "_zebra_i", 0) + 1

    def _add_conv_input(self, row, prop, val, var):
        """Add a SECOND editable box (standard units: km / km/h / m/s²) on the field row, two-way
        bound to the raw-value box `var`: edit either and the other updates live, before Apply.  The
        RAW value remains what gets committed — this box just lets the user enter standard units
        instead of doing the maths (issue #6)."""
        factor, unit, dec = _CONV_SPECS[_UNIT_CONV[prop]]
        is_int = isinstance(val.raw, int)

        def raw_to_disp(s):
            try:
                return "%.*f" % (dec, float(str(s).strip()) / factor)
            except (TypeError, ValueError):
                return ""
        conv_var = tk.StringVar(value=raw_to_disp(var.get()))

        cf = tk.Frame(row, background=_R_BG_PANEL)
        cf.grid(row=1, column=1, columnspan=3, sticky="e", padx=(0, 4), pady=(0, 1))
        tk.Label(cf, text="=", background=_R_BG_PANEL, foreground=_R_TEXT_DIM,
                 font=_F_MAIN).pack(side="left", padx=(0, 3))
        ttk.Entry(cf, textvariable=conv_var, width=12).pack(side="left")
        tk.Label(cf, text=unit, background=_R_BG_PANEL, foreground=_R_TEXT_DIM,
                 font=_F_MAIN).pack(side="left", padx=(3, 0))

        guard = {"on": False}   # stop the two traces ping-ponging each other

        def on_raw(*_a):
            if guard["on"]:
                return
            guard["on"] = True
            try:
                conv_var.set(raw_to_disp(var.get()))
            finally:
                guard["on"] = False

        def on_conv(*_a):
            if guard["on"]:
                return
            try:
                d = float(conv_var.get().strip())
            except (TypeError, ValueError):
                return                       # blank / mid-edit — leave the raw box alone
            guard["on"] = True
            try:
                raw = d * factor
                var.set(str(int(round(raw)) if is_int else raw))
            finally:
                guard["on"] = False
        var.trace_add("write", on_raw)
        conv_var.trace_add("write", on_conv)

    def _notes_section(self, container, key, hint=None):
        """A modder's 'Notes' box (issue #5.6) for the thing identified by `key`, auto-saving into the
        project's notes.json on focus-out / panel rebuild (no extra click)."""
        ui_util.notes_section(
            container, lambda text, k=key: self.project.set_note(k, text),
            panel_bg=_R_BG_PANEL, widget_bg=_R_BG_WIDGET, text_fg=_R_TEXT, dim_fg=_R_TEXT_DIM,
            gold=_R_GOLD, font=_F_MAIN, font_bold=_F_BOLD,
            initial=self.project.get_note(key), label=t("Notes"), hint=hint)

    def _render_group(self, container, rows, inst, specs):
        any_shown = False
        clean_inst = self._clean_match(inst)
        for label, prop, kind in specs:
            pidx = self._prop_index(inst.class_index, prop)
            if pidx is None:
                continue
            val = inst.get(pidx)
            if val is None:
                continue
            self._add_field_row(container, rows, label, prop, kind, val, clean_inst)
            any_shown = True
        return any_shown

    def _render_other(self, container, rows, inst, covered):
        """Expose every remaining numeric (Int/Float/Bool) property by its raw name as editable;
        list non-numeric props (ObjRef / List / LocHash / String) read-only so the user can see
        every property the descriptor actually carries (no silent drops)."""
        shown = False
        non_numeric = []
        clean_inst = self._clean_match(inst)
        for pvv in inst.props:
            name = self._pname(pvv.prop_index)
            if name in covered:
                continue
            if pvv.value.type_id in _NUM_TIDS:
                self._add_field_row(container, rows, name, name, "scalar", pvv.value, clean_inst)
                shown = True
            else:
                non_numeric.append((name, pvv.value))
        if non_numeric:
            tk.Label(container, text=t("Non-editable (ref / list / hash) — present on descriptor:"),
                     background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN,
                     anchor="w").pack(fill="x", padx=2, pady=(6, 1))
            for name, val in sorted(non_numeric, key=lambda x: x[0].lower()):
                tval = ndfbin_mod.T.name(val.type_id)
                if val.type_id == ndfbin_mod.T.List and isinstance(val.raw, list):
                    summary = f"List[{len(val.raw)}]"
                elif val.type_id == ndfbin_mod.T.Reference and isinstance(val.raw, tuple):
                    marker, ref = val.raw
                    if marker == ndfbin_mod.OBJ_REF_MARKER and isinstance(ref, tuple):
                        oi = ref[0]
                        if 0 <= oi < len(self._ndf.instances):
                            tgt_cls = self._ndf.classes[self._ndf.instances[oi].class_index].name
                            summary = f"-> #{oi} ({tgt_cls})"
                        else:
                            summary = f"-> #{oi} (out of range)"
                    else:
                        summary = f"ref({marker:#x})"
                elif val.type_id == ndfbin_mod.T.StringRef and isinstance(val.raw, int):
                    summary = f"STR:{self._ndf.get_string(val.raw)!r}"
                elif val.type_id == ndfbin_mod.T.LocHash and isinstance(val.raw, (bytes, bytearray)):
                    summary = f"hash:{bytes(val.raw).hex()}"
                else:
                    summary = repr(val.raw)[:60]
                tk.Label(container, text=f"  {name} ({tval}) = {summary}", anchor="w",
                         background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN,
                         wraplength=600, justify="left").pack(fill="x", padx=2)
            shown = True
        if not shown:
            tk.Label(container, text=t("(none)"), background=_R_BG_PANEL,
                     foreground=_R_TEXT_DIM, font=_F_MAIN).pack(anchor="w", padx=2)
        return shown

    # ── Units tab ───────────────────────────────────────────────────────────────

    def _on_faction_changed(self):
        """Refresh the Building dropdown options to the new faction's buildings, then refilter.
        Resets the building selection so users don't end up with a building from a different
        nation than what they just picked."""
        fac = self._faction_var.get()
        self._building_combo.configure(values=self._building_options(fac))
        self._fit_dropdown(self._building_combo)   # keep the list wide after the values change
        self._building_var.set("All buildings")
        self._apply_filter()

    def _on_list_lang(self):
        """The list's localisation dropdown changed — re-list with the new language's names,
        keeping the current selection visible."""
        self._list_lang_code = dic_mod.LANG_CODE.get(
            self._list_lang_var.get(), self._name_default_lang)
        self._apply_filter(keep_sel=True)

    def _apply_filter(self, keep_sel=False):
        fac = self._faction_var.get()
        spec = self._filter_spec(self._building_var.get())
        cat = self._type_var.get()   # legacy/back-compat category filter
        q = self._search_var.get().strip().lower()
        lang = self._list_lang_code
        prev = self._sel if keep_sel else None
        self._shown = []
        locs = []                          # localized name per shown row (parallel to _shown)
        for d in self._descs:
            if fac != "All" and d["nation"] != fac:
                continue
            if spec is not None:
                # Building/type filter takes priority over the abstract category — show only the units
                # the engine would actually display in that menu (the Factory == TypeBatiment join).
                # The Faction box is applied above, so a building-type filter spans every faction
                # unless a faction is also chosen (then it's ANDed).
                kind, val = spec
                if d["kind"] == "building":
                    continue   # buildings aren't 'in' a build menu
                fac_v = self._prop_value(d["inst"], "Factory")
                if fac_v is None:
                    continue
                if kind == "type":
                    if fac_v.raw != val:           # val is the TypeBatiment (TB#), any faction
                        continue
                else:                               # ('building', b): exact Nat + TypeBatiment
                    b = val
                    if d["nation"] != b["nation"] or fac_v.raw != b["type_batiment"]:
                        continue
            elif cat != "All" and d["category"] != cat:
                continue
            loc = self._loc_name(d, lang)
            # Search matches either the internal name or the localized in-game name.
            if q and q not in d["name"].lower() and q not in loc.lower():
                continue
            self._shown.append(d)
            locs.append(loc)
        # Two aligned columns (localized name, internal name) when any localized name is present;
        # otherwise just the internal name. Courier New keeps the columns lined up.
        col = min(max((len(l) for l in locs), default=0), 28)
        self._lb.delete(0, tk.END)
        for d, loc in zip(self._shown, locs):
            tag = d['cls_label'][0]
            internal = d['name'] or t("(unnamed)")
            if col:
                self._lb.insert(tk.END, "[%s] %-*s  %s" % (tag, col, loc, internal))
            else:
                self._lb.insert(tk.END, t("[{tag}] {name}", tag=tag, name=internal))
        self._count_lbl.configure(text=t("{shown} of {total}",
                                         shown=len(self._shown), total=len(self._descs)))
        # Restore the previous selection if it survived the refilter (e.g. language switch).
        if prev is not None and prev in self._shown:
            i = self._shown.index(prev)
            self._lb.selection_set(i)
            self._lb.see(i)
            self._sel = prev
        else:
            self._clear_fields()

    def _peek_unit(self):
        """Instant feedback the moment a list row is selected, BEFORE the (slower) field render —
        shows 'Loading <name>…' in the header so the click visibly registers."""
        sel = self._lb.curselection()
        if sel and 0 <= sel[0] < len(self._shown):
            d = self._shown[sel[0]]
            self._hdr.configure(text=t("Loading {name}…", name=d["name"] or t("(unnamed)")))

    def _on_select(self, _=None):
        sel = self._lb.curselection()
        if not sel:
            return
        self._sel = self._shown[sel[0]]
        self._render_fields()

    def _clear_fields(self):
        for w in self._fields_frame.winfo_children():
            w.destroy()
        self._field_rows = []
        self._upg_combo_var = None
        self._upg_candidates = {}
        self._upg_orig = None
        self._upg_require_pidx = None
        self._upg_isup_var = None
        self._upg_isup_orig = None
        self._upg_isup_chk = None
        self._sel = None
        self._apply_btn.configure(state="disabled")
        self._reset_btn.configure(state="disabled")
        self._dup_btn.configure(state="disabled")
        self._mig_btn.configure(state="disabled")
        self._hdr.configure(text=t("Select a unit or building"))
        self._sub.configure(text="")
        self._status.configure(text="")

    def _render_fields(self):
        for w in self._fields_frame.winfo_children():
            w.destroy()
        self._field_rows = []
        self._zebra_i = 0   # zebra-stripe counter for the stat/value rows (issue #5.2)
        self._wpn_ammo_pending = []   # (mounted_weapon, dropdown var, orig) — committed on Apply (#7)
        d = self._sel
        inst = d["inst"]
        self._hdr.configure(text=d["name"] or t("(unnamed)"))
        # Show the raw Nationalite int alongside the friendly name so the user can confirm at a
        # glance what's actually written on the descriptor (the prop is hidden from the raw fields
        # section because it's managed via the Migrate dialog).
        nat_v = self._prop_value(inst, "Nationalite")
        nat_raw = "absent" if nat_v is None else f"{nat_v.raw}"
        self._sub.configure(text=t("{cls} · {nation} (Nationalite={nat_raw}) · {category} · "
                                   "instance #{idx}",
                                   cls=d['cls_label'], nation=d['nation'], nat_raw=nat_raw,
                                   category=d['category'], idx=d['inst_index']))

        self._render_name(inst)

        self._section(self._fields_frame, t("Unit / building stats"))
        self._col_header(self._fields_frame)
        self._render_group(self._fields_frame, self._field_rows, inst, _FIELDS)

        self._render_upgrade(inst, d)

        # Inline weapons: each mounted weapon has its OWN ammo, set per weapon.
        nodes = self._unit_weapon_nodes(inst) if d["kind"] == "unit" else []
        if nodes:
            self._section(self._fields_frame, t("Weapons — pick each weapon's ammo (applied on Apply)"))
            ammo_vals = [f"#{a['id']}" for a in self._ammo]
            for wi, (mw, am, ai) in enumerate(nodes):
                idv = self._prop_value(am, "AmmunitionId") if am is not None else None
                tagv = self._prop_value(mw, "EffectTag")
                wname = (self._ndf.get_string(tagv.raw)
                         if tagv is not None and isinstance(tagv.raw, int)
                         else t("Weapon {n}", n=wi + 1))
                row = tk.Frame(self._fields_frame, background=_R_BG_PANEL)
                row.pack(fill="x", pady=2)
                row.grid_columnconfigure(0, weight=1)
                self._wrap_label(row, wname, _R_TEXT)
                tk.Label(row, text=t("ammo #{ammo_id}", ammo_id=(idv.raw if idv else '?')),
                         width=self._VAL_W, anchor="e",
                         background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN
                         ).grid(row=0, column=2, sticky="e", padx=(0, 12))
                ctrl = tk.Frame(row, background=_R_BG_PANEL)
                ctrl.grid(row=0, column=3, sticky="e", padx=(0, 4))
                var = tk.StringVar(value=f"#{idv.raw}" if idv else "")
                ammo_cb = ttk.Combobox(ctrl, textvariable=var, values=ammo_vals, width=9,
                                       state="readonly")
                ammo_cb.pack(side="left")
                ui_util.fit_combobox(ammo_cb, minimum=9, maximum=20)
                # No per-weapon "Set ammo" button — the chosen ammo is applied on the main Apply (#7).
                self._wpn_ammo_pending.append((mw, var, var.get()))
            rm = tk.Frame(self._fields_frame, background=_R_BG_PANEL)
            rm.pack(fill="x", pady=1)
            ttk.Button(rm, text=t("Remove all weapons from this unit"),
                       command=self._remove_weapon).pack(side="left", padx=2)
            tk.Label(self._fields_frame,
                     text=t("Each weapon fires its own ammo. To make a unique weapon, duplicate an ammo "
                            "on the Ammo tab, pick it for the weapon here, then click Apply. Editing a "
                            "shared ammo below affects every unit using it."),
                     background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN,
                     wraplength=560, justify="left").pack(anchor="w", padx=2)
            # editable stats for each DISTINCT ammo this unit uses
            ammo_covered = {p for _, p, _ in _AMMO_FIELDS} | _OTHER_SKIP
            seen = set()
            for mw, am, ai in nodes:
                if am is None or ai in seen:
                    continue
                seen.add(ai)
                users = next((a["users"] for a in self._ammo if a["idx"] == ai), [])
                shared = (t("  (shared by {n} units)", n=len(users)) if len(users) > 1
                          else t("  (private to this unit)"))
                idv = self._prop_value(am, "AmmunitionId")
                self._section(self._fields_frame,
                              t("Ammo #{ammo_id}{shared} stats",
                                ammo_id=(idv.raw if idv else '?'), shared=shared))
                self._render_group(self._fields_frame, self._field_rows, am, _AMMO_FIELDS)
                self._render_other(self._fields_frame, self._field_rows, am, ammo_covered)

        # Catch-all: any other numeric field on the unit not shown above
        self._section(self._fields_frame, t("Other unit fields (raw)"))
        self._render_other(self._fields_frame, self._field_rows, inst, _COVERED)

        # Modder's notes for this unit (saved in the mod project, not the game data) — issue #5.6.
        self._notes_section(self._fields_frame, "unit:" + (d["name"] or ""),
                            hint=t("Private notes for this unit — saved with the mod project, "
                                   "auto-saved as you click away."))

        self._apply_btn.configure(state="normal")
        self._reset_btn.configure(state="normal")
        self._dup_btn.configure(state="normal")
        self._mig_btn.configure(state="normal")
        self._status.configure(text="")

    def _render_name(self, inst):
        """Editable in-game display name, ONE language at a time. The name lives in ZZ_Win.dat
        baseunite.dic (one per language), keyed by NameInMenuToken. A language dropdown (defaulting to
        the settings language) switches which language's name the entry shows; typed values for each
        language accumulate in _name_pending until Apply."""
        self._name_var = None
        self._name_lang_var = None
        self._name_cur_lbl = None
        self._name_def_lbl = None
        self._name_cur_lang = None
        self._name_pending = {}
        self._name_orig_by_lang = {}
        self._name_clean_by_lang = {}
        self._name_key = self._name_token(inst)
        self._section(self._fields_frame, t("Display name (in-game)"))
        row = tk.Frame(self._fields_frame, background=_R_BG_PANEL)
        row.pack(fill="x", pady=2)
        row.grid_columnconfigure(0, weight=1)
        if not self._name_lang_paths:
            self._wrap_label(row, t("Name editing needs ZZ_Win.dat (not in this mod's sources)"), _R_TEXT_DIM)
            return
        if self._name_key is None:
            self._wrap_label(row, t("(this descriptor has no NameInMenuToken)"), _R_TEXT_DIM)
            return
        langs = [c for c in self._name_lang_order if self._name_orig(c) is not None]
        if not langs:
            self._wrap_label(row, t("(name not found in baseunite.dic for this unit)"), _R_TEXT_DIM)
            return
        start = self._name_default_lang if self._name_default_lang in langs else langs[0]
        self._name_cur_lang = start
        cur = self._name_orig(start) or ""
        dflt = self._name_default(start) or ""

        # name row: label | default | current value | entry
        self._wrap_label(row, t("Unit name"), _R_TEXT)
        self._name_def_lbl = tk.Label(row, text=(ui_util.truncate(dflt, self._DEF_W) if dflt != cur else ""),
                                      width=self._DEF_W, anchor="e", background=_R_BG_PANEL,
                                      foreground=_R_GOLD, font=_F_MAIN)
        self._name_def_lbl.grid(row=0, column=1, sticky="e", padx=(0, 10))
        ui_util.tooltip(self._name_def_lbl, lambda: self._name_default(self._name_cur_lang) or "")
        self._name_cur_lbl = tk.Label(row, text=ui_util.truncate(cur, self._VAL_W), width=self._VAL_W,
                                      anchor="e", background=_R_BG_PANEL, foreground=_R_TEXT_DIM,
                                      font=_F_MAIN)
        self._name_cur_lbl.grid(row=0, column=2, sticky="e", padx=(0, 12))
        ui_util.tooltip(self._name_cur_lbl, lambda: self._name_orig(self._name_cur_lang) or "")
        self._name_var = tk.StringVar(value=cur)
        ttk.Entry(row, textvariable=self._name_var, width=self._ENT_W).grid(
            row=0, column=3, sticky="e", padx=(0, 4))

        # language selector row
        lrow = tk.Frame(self._fields_frame, background=_R_BG_PANEL)
        lrow.pack(fill="x", pady=(0, 2))
        lrow.grid_columnconfigure(0, weight=1)
        self._wrap_label(lrow, t("Language"), _R_TEXT_DIM)
        self._name_lang_var = tk.StringVar(value=dic_mod.lang_label(start))
        cb = ttk.Combobox(lrow, textvariable=self._name_lang_var, state="readonly",
                          values=[dic_mod.lang_label(c) for c in langs], width=self._ENT_W - 2)
        cb.grid(row=0, column=3, sticky="e", padx=(0, 4))
        ui_util.fit_combobox(cb)
        cb.bind("<<ComboboxSelected>>", lambda e: self._on_name_lang())

        tk.Label(self._fields_frame,
                 text=t("Pick a language, set the name, then pick another to set more — Apply commits "
                        "every language you changed into the mod's ZZ_Win.dat."),
                 background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN, anchor="w",
                 justify="left", wraplength=520).pack(anchor="w", padx=2, pady=(0, 2))

    def _on_name_lang(self):
        """Language dropdown changed: stash what's typed for the old language, load the new one."""
        if self._name_var is None:
            return
        if self._name_cur_lang is not None:
            self._name_pending[self._name_cur_lang] = self._name_var.get()
        new_code = dic_mod.LANG_CODE.get(self._name_lang_var.get(), self._name_cur_lang)
        self._name_cur_lang = new_code
        orig = self._name_orig(new_code) or ""
        self._name_var.set(self._name_pending.get(new_code, orig))
        if self._name_cur_lbl is not None:
            self._name_cur_lbl.configure(text=ui_util.truncate(orig, self._VAL_W))
        if self._name_def_lbl is not None:
            dflt = self._name_default(new_code) or ""
            self._name_def_lbl.configure(text=(ui_util.truncate(dflt, self._DEF_W)
                                               if dflt != orig else ""))

    def _render_upgrade(self, inst, d):
        self._upg_combo_var = None
        self._upg_candidates = {}
        self._upg_orig = None
        self._upg_isup_var = None
        self._upg_isup_orig = None
        self._upg_isup_chk = None
        self._upg_require_pidx = self._prop_index(inst.class_index, "UpgradeRequire")
        if d["kind"] != "unit" or self._upg_require_pidx is None:
            return
        ur_val = inst.get(self._upg_require_pidx)
        has_parent = ur_val is not None and ur_val.raw not in (None, 0)
        iu_val = self._prop_value(inst, "IsUpgrade")
        cur_isup = bool(iu_val.raw) if iu_val is not None else False
        cur = (str(self._ndf.resolve_value(ur_val)).replace("$obj:", "")
               if has_parent else _UPG_NONE)
        cands = {}
        for d2 in self._descs:
            if (d2 is not d and d2["kind"] == "unit"
                    and d2["nation"] == d["nation"] and d2["category"] == d["category"]):
                cands[d2["name"]] = (d2["inst_index"], d2["inst"].class_index)
        if has_parent:
            try:
                _m, (oi, ci) = ur_val.raw
                cands.setdefault(cur, (oi, ci))
            except Exception:
                pass
        self._upg_candidates = cands
        values = [_UPG_NONE] + sorted(cands.keys())

        self._section(self._fields_frame, t("Upgrade chain"))
        row0 = tk.Frame(self._fields_frame, background=_R_BG_PANEL)
        row0.pack(fill="x", pady=2)
        row0.grid_columnconfigure(0, weight=1)
        self._wrap_label(row0, t("Is an upgrade"), _R_TEXT)
        self._upg_isup_orig = cur_isup
        self._upg_isup_var = tk.BooleanVar(value=(True if has_parent else cur_isup))
        self._upg_isup_chk = tk.Checkbutton(
            row0, text=t("upgradable (adds price/time 50; hidden until researched)"),
            variable=self._upg_isup_var, background=_R_BG_PANEL, foreground=_R_TEXT,
            selectcolor=_R_BG_WIDGET, font=_F_MAIN, activebackground=_R_BG_PANEL,
            activeforeground=_R_GOLD_BRT)
        self._upg_isup_chk.grid(row=0, column=1, columnspan=3, sticky="e", padx=(0, 4))
        if has_parent:
            self._upg_isup_chk.configure(state="disabled")

        row = tk.Frame(self._fields_frame, background=_R_BG_PANEL)
        row.pack(fill="x", pady=2)
        row.grid_columnconfigure(0, weight=1)
        self._wrap_label(row, t("Upgrades from"), _R_TEXT)
        self._upg_combo_var = tk.StringVar(value=cur if cur in values else _UPG_NONE)
        self._upg_orig = self._upg_combo_var.get()
        combo = ttk.Combobox(row, textvariable=self._upg_combo_var, values=values, width=self._ENT_W,
                             state="readonly")
        combo.grid(row=0, column=3, sticky="e", padx=(0, 4))
        self._fit_dropdown(combo)   # full parent-unit names in the drop-down (issue #5.1)
        combo.bind("<<ComboboxSelected>>", lambda *_: self._on_upg_combo_change())

    def _on_upg_combo_change(self):
        if self._upg_isup_chk is None or self._upg_combo_var is None:
            return
        if self._upg_combo_var.get() != _UPG_NONE:
            self._upg_isup_var.set(True)
            self._upg_isup_chk.configure(state="disabled")
        else:
            self._upg_isup_chk.configure(state="normal")

    # ── Weapons tab ───────────────────────────────────────────────────────────

    def _wpn_label(self, a):
        # ammo's own name = its id (it has no readable name); users shown on selection
        return t("Ammo #{ammo_id}", ammo_id=a['id'])

    def _wpn_apply_filter(self):
        q = self._wpn_search_var.get().strip().lower()
        self._wpn_shown = []
        for a in self._ammo:
            hay = (str(a["id"]) + " " + " ".join(a["users"])).lower()
            if q and q not in hay:
                continue
            self._wpn_shown.append(a)
        self._wpn_lb.delete(0, tk.END)
        for a in self._wpn_shown:
            self._wpn_lb.insert(tk.END, self._wpn_label(a))
        self._wpn_count_lbl.configure(text=t("{shown} of {total}",
                                             shown=len(self._wpn_shown), total=len(self._ammo)))
        self._clear_wpn_fields()

    def _peek_wpn(self):
        """Instant 'Loading ammo …' feedback before the ammo field render."""
        sel = self._wpn_lb.curselection()
        if sel and 0 <= sel[0] < len(self._wpn_shown):
            self._wpn_hdr.configure(text=t("Loading ammo #{ammo_id}…",
                                           ammo_id=self._wpn_shown[sel[0]]['id']))

    def _on_wpn_select(self, _=None):
        sel = self._wpn_lb.curselection()
        if not sel:
            return
        self._wpn_sel = self._wpn_shown[sel[0]]
        self._render_wpn_fields()

    def _clear_wpn_fields(self):
        for w in self._wpn_fields_frame.winfo_children():
            w.destroy()
        self._wpn_rows = []
        self._wpn_sel = None
        self._wpn_apply_btn.configure(state="disabled")
        self._wpn_reset_btn.configure(state="disabled")
        self._wpn_dup_btn.configure(state="disabled")
        self._wpn_hdr.configure(text=t("Select a weapon"))
        self._wpn_sub.configure(text="")
        self._wpn_status.configure(text="")

    def _render_wpn_fields(self):
        for w in self._wpn_fields_frame.winfo_children():
            w.destroy()
        self._wpn_rows = []
        a = self._wpn_sel
        inst = a["inst"]
        self._wpn_hdr.configure(text=t("Ammo #{ammo_id}", ammo_id=a['id']))
        users = a["users"]
        self._wpn_sub.configure(
            text=(t("Used by: ") + (", ".join(users) if users else t("(no indexed unit)")))
            + (t("   — editing affects all of them") if len(users) > 1 else ""))
        self._col_header(self._wpn_fields_frame)
        self._render_group(self._wpn_fields_frame, self._wpn_rows, inst, _AMMO_FIELDS)
        self._section(self._wpn_fields_frame, t("Other ammo fields (raw)"))
        self._render_other(self._wpn_fields_frame, self._wpn_rows, inst,
                           {p for _, p, _ in _AMMO_FIELDS} | _OTHER_SKIP)
        # Modder's notes for this ammo (e.g. why a duplicated ammo exists) — issue #5.6.
        self._notes_section(self._wpn_fields_frame, "ammo:" + str(a["id"]),
                            hint=t("Private notes for this ammo — e.g. what a duplicated ammo is for. "
                                   "Saved with the mod project, auto-saved as you click away."))
        self._wpn_apply_btn.configure(state="normal")
        self._wpn_reset_btn.configure(state="normal")
        self._wpn_dup_btn.configure(state="normal")
        self._wpn_status.configure(text="")

    def _reset_wpn_defaults(self):
        """Revert this ammo's stat fields to their default (clean-backup) values (shared ammo affects
        every unit that uses it). Recoverable — nothing is written to disk until Save."""
        if self._wpn_sel is None or not self._wpn_rows:
            return
        if not self._row_defaults:
            messagebox.showinfo(
                t("Reset to defaults"),
                t("No default values are available — this needs a clean backup of the game for this "
                  "version. Create one in the Mod Manager tab."), parent=self)
            return
        if not messagebox.askyesno(
                t("Reset to defaults"),
                t("Reset every field on this ammo back to its default (clean-backup) value? This ammo "
                  "may be shared by several units. Nothing is written to disk until you Save."),
                parent=self):
            return
        changed = self._revert_rows(self._wpn_rows)
        if changed:
            self.project.mark_dirty("gameplay", mp_mod.EVERYTHING_PATH)
            self._index_weapons()
            self._notify()
            self._keep_scroll(self._wpn_fields_frame, self._render_wpn_fields)
        self._wpn_status.configure(text=t("Reset {n} field(s) to default", n=changed))

    def _apply_wpn(self):
        if self._wpn_sel is None:
            return
        changed, errors = self._commit_rows(self._wpn_rows)
        if errors:
            messagebox.showerror(t("Invalid value(s)"), "\n".join(errors), parent=self)
        if changed:
            self.project.mark_dirty("gameplay", mp_mod.EVERYTHING_PATH)
            self._keep_scroll(self._wpn_fields_frame, self._render_wpn_fields)  # show set values
            self._notify()
        self._wpn_status.configure(
            text=(t("Applied {n} change(s)", n=changed) if changed else t("No changes to apply")))

    # ── apply / commit ────────────────────────────────────────────────────────

    @staticmethod
    def _coerce(old, text):
        text = text.strip()
        if isinstance(old, bool):
            return text.lower() in ("1", "true", "yes", "on")
        if isinstance(old, float):
            return float(text)
        if isinstance(old, int):
            return int(float(text))
        return text

    def _commit_rows(self, rows):
        changed = 0
        errors = []
        for prop, kind, val, var, orig_str in rows:
            new_str = var.get().strip()
            if new_str == orig_str:
                continue
            try:
                if kind == "factory":
                    # Accept either a building label ("US: Building_VehiculeFactory (TB=10)") OR a
                    # legacy category name ("Armor") for backward compatibility with older tests.
                    bldg = self._building_by_label(new_str)
                    if bldg is not None:
                        val.raw = bldg["type_batiment"]
                        changed += 1
                    elif new_str in _TYPE_FACTORY:
                        val.raw = _TYPE_FACTORY[new_str]
                        changed += 1
                    else:
                        errors.append(t("{prop}: unknown building / build menu '{value}'",
                                        prop=prop, value=new_str))
                        continue
                elif kind in ("list", "boollist"):
                    elems = val.raw
                    if not isinstance(elems, list) or not elems:
                        continue
                    parts = [p for p in (s.strip() for s in new_str.replace(";", ",").split(",")) if p]
                    if not parts:
                        continue

                    def conv(x):
                        return (x.lower() in ("1", "true", "yes", "on")) if kind == "boollist" else int(float(x))
                    if len(parts) == 1:
                        v0 = conv(parts[0])
                        for e in elems:
                            e.raw = v0
                    else:
                        for e, p in zip(elems, parts):
                            e.raw = conv(p)
                    changed += 1
                else:
                    val.raw = self._coerce(val.raw, new_str)
                    changed += 1
            except Exception as e:
                errors.append(t("{prop}: {e}", prop=prop, e=e))
        return changed, errors

    def _revert_rows(self, rows):
        """Copy each field's clean-backup default onto its current value — a type-safe raw copy (lists
        are deep-copied so the live value never aliases the clean NDF's list). Returns the count of
        fields that actually changed."""
        changed = 0
        for prop, kind, val, var, orig_str in rows:
            dv = self._row_defaults.get(id(val))
            if dv is None:
                continue
            before = self._fmt_value(val, kind)
            val.raw = copy.deepcopy(dv.raw) if isinstance(dv.raw, list) else dv.raw
            if self._fmt_value(val, kind) != before:
                changed += 1
        return changed

    def _reset_to_defaults(self):
        """Revert every value field on this unit's page to its default (clean-backup) value. Resets the
        unit/building stats, the linked ammo stats, and the raw numeric fields; the display name,
        upgrade chain, and weapon assignment are left alone (they aren't simple value fields).
        Recoverable — nothing is written to disk until Save."""
        if not self._sel or not self._field_rows:
            return
        if not self._row_defaults:
            messagebox.showinfo(
                t("Reset to defaults"),
                t("No default values are available — this needs a clean backup of the game for this "
                  "version. Create one in the Mod Manager tab."), parent=self)
            return
        if not messagebox.askyesno(
                t("Reset to defaults"),
                t("Reset every field on this page back to its default (clean-backup) value? Unsaved "
                  "edits to these fields are discarded. Nothing is written to disk until you Save."),
                parent=self):
            return
        changed = self._revert_rows(self._field_rows)
        if changed:
            self.project.mark_dirty("gameplay", mp_mod.EVERYTHING_PATH)
            self._notify()
            self._keep_scroll(self._fields_frame, self._render_fields)
        self._status.configure(text=t("Reset {n} field(s) to default", n=changed))

    def _apply_edits(self):
        if not self._sel:
            return
        changed, errors = self._commit_rows(self._field_rows)

        # Upgrade chain: parent + independent IsUpgrade toggle; price/time follow IsUpgrade.
        if self._upg_combo_var is not None:
            sel = self._upg_combo_var.get()
            want_parent = sel != _UPG_NONE
            want_isup = True if want_parent else bool(self._upg_isup_var.get())
            if sel != self._upg_orig or want_isup != self._upg_isup_orig:
                try:
                    inst = self._sel["inst"]
                    if want_parent:
                        oi, ci = self._upg_candidates[sel]
                        inst.set(self._upg_require_pidx,
                                 ndfbin_mod.NdfValue(ndfbin_mod.T.Reference,
                                                     (ndfbin_mod.OBJ_REF_MARKER, (oi, ci))))
                    elif self._upg_require_pidx is not None and inst.get(self._upg_require_pidx) is not None:
                        inst.remove(self._upg_require_pidx)
                    self._set_bool_prop(inst, "IsUpgrade", want_isup)
                    if want_isup and not self._upg_isup_orig:
                        self._add_int_prop_if_missing(inst, "UpgradePrice", 50)
                        self._add_int_prop_if_missing(inst, "UpgradeTime", 50)
                    elif self._upg_isup_orig and not want_isup:
                        self._remove_prop(inst, "UpgradePrice")
                        self._remove_prop(inst, "UpgradeTime")
                    changed += 1
                except Exception as e:
                    errors.append(t("Upgrade: {e}", e=e))

        # Display name(s) — per-language edits in baseunite.dic (ZZ_Win.dat), tracked separately so a
        # name-only edit doesn't needlessly mark the gameplay dat dirty.
        name_changed = 0
        if self._name_var is not None and self._name_key is not None:
            if self._name_cur_lang is not None:        # capture the currently-shown language
                self._name_pending[self._name_cur_lang] = self._name_var.get()
            for lang, typed in self._name_pending.items():
                orig = self._name_orig(lang)
                if orig is None or typed == orig:
                    continue
                path = self._name_lang_paths.get(lang)
                try:
                    blob = self.project.get_raw("loc", path)
                    self.project.set_raw("loc", path, dic_mod.set_entry(blob, self._name_key, typed))
                    self._name_orig_by_lang[lang] = typed   # new baseline (so re-apply is a no-op)
                    self._loc_names.pop(lang, None)         # drop stale list cache for this language
                    name_changed += 1
                except Exception:
                    errors.append(t("Name [{lang}]: could not write into baseunite.dic",
                                    lang=dic_mod.lang_label(lang)))

        # Weapon ammo: apply the inline dropdowns here (issue #7 — no separate "Set ammo" button).
        wpn_changed = self._commit_weapon_ammo()

        if errors:
            messagebox.showerror(t("Invalid value(s)"), "\n".join(errors), parent=self)
        if changed or wpn_changed:
            self.project.mark_dirty("gameplay", mp_mod.EVERYTHING_PATH)
        if wpn_changed:
            self._index_weapons()        # weapon→ammo links changed — refresh the Ammo tab + inline stats
            self._wpn_apply_filter()
        if changed or name_changed or wpn_changed:
            self._keep_scroll(self._fields_frame, self._render_fields)  # show set values
            if name_changed:
                self._apply_filter(keep_sel=True)   # reflect the edited name in the list
            self._notify()
        total = changed + name_changed + wpn_changed
        self._status.configure(
            text=(t("Applied {total} change(s) · {pending} file(s) pending",
                    total=total, pending=self.project.dirty_count())
                  if total else t("No changes to apply")))

    def _notify(self):
        if self._on_change:
            try:
                self._on_change()
            except Exception:
                pass

    def _save_to_mod(self):
        # commit pending edits in whichever tab(s) have a selection first
        if self._sel and self._field_rows:
            self._apply_edits()
        if self._wpn_sel and self._wpn_rows:
            self._apply_wpn()
        if not self.project.is_dirty():
            messagebox.showinfo(t("Save mod"), t("No pending changes to save."), parent=self)
            return
        try:
            written = self.project.save_all()
        except Exception as e:
            messagebox.showerror(
                t("Save failed"),
                t("{e}\n\nTip: set the Game Root in Settings — it's needed to obtain the base "
                  "game file the first time a mod touches it.", e=e), parent=self)
            return
        self._notify()
        self._save_status.configure(text=t("Saved all changes to the mod"))
        messagebox.showinfo(t("Saved"), t("Saved all mod changes to:\n\n") + "\n".join(written),
                            parent=self)


# Standalone note (launched from the Mod Editor hub in mod_manager.py)
if __name__ == "__main__":
    print("UnitsEditorWindow is launched from the Mod Editor hub in mod_manager.py")

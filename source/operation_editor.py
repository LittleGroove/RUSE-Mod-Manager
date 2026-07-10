"""Mission Logic tab — an EMBEDDABLE frame (beside the "Map Editor" tab) for editing HOW a scenario is
presented and plays. Works for EVERY kind:

  * ALL kinds (mp / campaign / operation): the SELECTION-LIST registration — the display name shown in
    the menu, and the entry's POSITION/order in that menu list (read + reorder, visualised without the
    game; confirms a duplicated scenario actually sits in the list that makes it appear in-game).
  * campaign / operation only: the mission itself — date/briefing, difficulty, game rules, camps + AI,
    objectives + victory/defeat, and the generated mission `.xyz` (plus a raw `.xyz` script editor).

Map placement lives in the other tab; the map/scenario selection above both tabs drives this one via
`set_binding()`. Stages into the mod project; the user clicks "Save to mod" above to write.
"""
import tkinter as tk
from tkinter import ttk, scrolledtext

from ruse_mod_engine import operation_script as ops
from ruse_mod_engine import xyz_compile as xyzc
import operation_forms as opf
from ruse_mod_engine import scenario_gen
from ruse_mod_engine import scenario_registry as SR
from ruse_mod_engine import dic as dicmod
from ruse_mod_engine.ndfbin import T
from i18n import t
import ui_util

import theme                # single source of truth for the palette; local names kept unchanged
_BG = theme.BG; _PANEL = theme.PANEL; _WIDGET = theme.WIDGET; _TEXT = theme.TEXT; _DIM = theme.DIM
_GOLD = theme.GOLD; _GOLD_BRT = theme.GOLD_BRT; _SEL = theme.SEL_BG
_F = theme.F; _FB = theme.FB


def _blk_label(block):
    """Friendly one-line label for a Block (uses the catalog label)."""
    from ruse_mod_engine import dsl_catalog as _dc
    if not isinstance(block, ops.Block):
        return "(none)"
    rec = _dc.info_for(block.cls) or {}
    return rec.get("label", block.cls)


class _ProjDat:
    def __init__(self, project, key):
        self.project = project; self.key = key
    def list(self):
        try:
            return self.project.entry_paths(self.key)
        except Exception:
            return []
    def get(self, vp):
        try:
            return self.project.get_raw(self.key, vp)
        except Exception:
            return None


class OperationEditorFrame(tk.Frame):
    def __init__(self, parent, project, binding=None, on_done=None, open_script_cb=None):
        super().__init__(parent, background=_BG)
        self.project = project
        self.on_done = on_done
        self.open_script_cb = open_script_cb       # () -> open the raw .xyz editor for the current scenario
        self.binding = None
        self.op = ops.starter_operation()
        self._list_idxs = []                       # info_idx per row in the selection-list visualiser
        self._build_ui()
        self.set_binding(binding)

    # ── (re)load on scenario change ────────────────────────────────────────────────
    def set_binding(self, binding):
        self.binding = binding
        kind = getattr(binding, "kind", None)
        bound = binding is not None and kind in ("mp", "campaign", "operation")
        mission = kind in ("campaign", "operation")
        if not bound:
            self._content.pack_forget()
            self._ph_lbl.config(text=t(
                "op.select_registered_map_mission_operation"))
            self._placeholder.pack(fill="both", expand=True)
            return
        self._placeholder.pack_forget()
        self._content.pack(fill="both", expand=True)
        self._title_lbl.config(text=t("op.mission_logic_map_dir_scenario",
                               map_dir=binding.map_dir, scenario_name=binding.scenario_name, kind=kind))
        # mission-only sections show only for campaign/operation
        if mission:
            self._op_only.pack(fill="both", expand=True, pady=(4, 0))
        else:
            self._op_only.pack_forget()
        # List grouping is VISUAL ONLY, so all kinds can freely regroup / make new groups here. (For MP,
        # changing the player count in the GAME MODES section also OFFERS to auto-move it to the matching
        # group — but you can still override that placement here.)
        self._group_row.pack(fill="x", padx=4, pady=(0, 3))
        if kind == "mp":
            self._reorder_note.config(text=t("op.orders_within_group_groups_are"))
        else:
            self._reorder_note.config(text=t("op.orders_within_group_use_group"))
        self.op = ops.starter_operation()
        if hasattr(self, "_ov_cond"):
            self._ov_cond = None; self._ov_succ = []; self._ov_fail = []; self._refresh_obj_cond_lbl()
        self._reload_lists()
        self._load_existing_text()
        self._refresh_select_list()

    def _glob_ndf(self):
        return self.project.get_ndf("gameplay", scenario_gen.GLOBALS_PATH)

    def _info_inst(self, g):
        i = getattr(self.binding, "info_idx", None)
        return g.instances[i] if (i is not None and i < len(g.instances)) else None

    def _loc_blob(self):
        # READ names from the LOCALIZED dic the game shows (English/us by default), NOT the 'dev' dic —
        # dev carries internal labels ('8 Strategists', 'Hiver nucleaire'); us is the clean menu name.
        zz = _ProjDat(self.project, "loc")
        vp = scenario_gen.preferred_flash_dic(zz, getattr(self, "_lang", "us"))
        return zz.get(vp) if vp else None

    def _resolve_name(self, g, info_idx, blob):
        inst = g.instances[info_idx]
        v = SR._get_prop(g, inst, "Description")
        if v is not None and v.type_id == T.LocHash and blob is not None:
            s = dicmod.get_entry(blob, bytes(v.raw))
            if s:
                return s
        tr = SR._str_prop(g, inst, "TrackingId")
        return tr or ("#%d" % info_idx)

    def _load_existing_text(self):
        try:
            g = self._glob_ndf(); inst = self._info_inst(g); blob = self._loc_blob()
            self._name_v.set(""); self._sub_v.set(""); self._brief_set("")
            if inst is not None:
                if inst is not None and blob is not None:
                    def res(p):
                        v = SR._get_prop(g, inst, p)
                        return dicmod.get_entry(blob, bytes(v.raw)) if (v and v.type_id == T.LocHash) else None
                    if res("Description"):
                        self._name_v.set(res("Description"))
                    if res("LongDescription1"):
                        self._sub_v.set(res("LongDescription1"))
                    if res("LongDescription2"):
                        self._brief_set(res("LongDescription2"))
            d = self.binding.detail or {}
            self._players_v.set(str(d.get("NbPlayers", 1)))
        except Exception:
            pass

    # ── selection-list visualiser ───────────────────────────────────────────────────
    def _refresh_select_list(self):
        """Show the WHOLE menu list (all packs the manager enumerates), grouped by the KIND's grouping
        key (mp=player count; operation=mode category 1v1/1vAll/Coop; campaign=mission group), with this
        scenario marked. This is the in-game selection order, visualised without launching."""
        self._list_lb.delete(0, "end"); self._list_idxs = []
        b = self.binding
        if b is None or b.pack_idx is None:
            self._pos_lbl.config(text=t("op.not_menu_list")); return
        try:
            g = self._glob_ndf(); blob = self._loc_blob()
            full = SR.full_menu_order(g, b.kind)
            own = SR.pack_child_order(g, b.kind, b.pack_idx)
            gprop = SR.group_prop_for(b.kind)
        except Exception:
            full, own, gprop = [], [], "NbPlayers"
        # MERGE by group key across ALL packs — the DLC/pre-order packs just append into the group they
        # belong to, so in-game there's ONE header per player-count / mode group (not one per pack).
        buckets = {}; key_order = []
        for info_idx, _pidx in full:
            v = SR._get_prop(g, g.instances[info_idx], gprop); key = v.raw if v is not None else None
            if key not in buckets:
                buckets[key] = []; key_order.append(key)
            buckets[key].append(info_idx)
        cur_row = None
        for key in key_order:
            self._list_lb.insert("end", t("op.label", label=SR.group_label(b.kind, key)))
            self._list_idxs.append(None)
            for info_idx in buckets[key]:
                name = self._resolve_name(g, info_idx, blob)
                self._list_lb.insert("end", "%s%s" % ("►  " if info_idx == b.info_idx else "     ", name))
                self._list_idxs.append(info_idx)
                if info_idx == b.info_idx:
                    cur_row = len(self._list_idxs) - 1
        if cur_row is not None:
            self._list_lb.selection_clear(0, "end"); self._list_lb.selection_set(cur_row); self._list_lb.see(cur_row)
            pos = (own.index(b.info_idx) + 1) if b.info_idx in own else 0
            cv = SR._group_key(g, b.info_idx, gprop)
            self._pos_lbl.config(text=t("op.position_pos_total_its_pack",
                                 pos=pos, total=len(own), group=SR.group_label(b.kind, cv), full=len(full),
                                 npacks=len(set(p for _i, p in full))))
            self._refresh_group_combo(g)
        else:
            self._pos_lbl.config(text=t("op.not_kind_menu_won_t", kind=b.kind))

    def _refresh_group_combo(self, g):
        b = self.binding
        try:
            self._group_vals = SR.group_values(g, b.kind, b.pack_idx)
            self._group_cb["values"] = [SR.group_label(b.kind, v) for v in self._group_vals]
            cur = SR._group_key(g, b.info_idx, SR.group_prop_for(b.kind))
            if cur in self._group_vals:
                self._group_cb.current(self._group_vals.index(cur))
        except Exception:
            self._group_vals = []; self._group_cb["values"] = []

    def _do_reorder(self, fn):
        b = self.binding
        if b is None or b.pack_idx is None or b.info_idx is None:
            return
        try:
            g = self._glob_ndf()
            if fn(g, b) is not None:
                self.project.mark_dirty("gameplay", scenario_gen.GLOBALS_PATH)
                self._refresh_select_list()
        except Exception as e:
            ui_util.error(self, t("op.reorder"), str(e))

    def _move_in_group(self, delta):
        self._do_reorder(lambda g, b: SR.move_within_group(g, b.kind, b.pack_idx, b.info_idx, delta))

    def _assign_to_group(self):
        b = self.binding; i = self._group_cb.current()
        if b is None or i < 0 or i >= len(self._group_vals):
            return
        self._do_assign(self._group_vals[i])

    def _new_group(self):
        b = self.binding
        if b is None or b.pack_idx is None:
            return
        try:
            nv = SR.next_group_value(self._glob_ndf(), b.kind, b.pack_idx)
        except Exception:
            return
        if ui_util.confirm(self, t("op.new_group_2"), t("op.create_new_group_key_value", nv=nv)):
            self._do_assign(nv)

    def _do_assign(self, val):
        b = self.binding
        if b is None or b.pack_idx is None or b.info_idx is None:
            return
        try:
            SR.assign_group(self._glob_ndf(), b.kind, b.pack_idx, b.info_idx, val)
            self.project.mark_dirty("gameplay", scenario_gen.GLOBALS_PATH)
            self._refresh_select_list()
        except Exception as e:
            ui_util.error(self, t("op.group"), str(e))

    # ── UI ───────────────────────────────────────────────────────────────────────────
    def _lbl(self, parent, text):
        return tk.Label(parent, text=text, background=_PANEL, foreground=_TEXT, font=_F, anchor="w")

    def _entry(self, parent, var, width=24):
        return tk.Entry(parent, textvariable=var, width=width, background=_WIDGET, foreground=_TEXT,
                        insertbackground=_TEXT, font=_F, relief="flat", highlightthickness=1,
                        highlightbackground="#243748")

    def _build_ui(self):
        bar = tk.Frame(self, background=_PANEL); bar.pack(side="top", fill="x")
        self._title_lbl = tk.Label(bar, text=t("op.mission_logic"), background=_PANEL, foreground=_GOLD,
                                   font=("Courier New", 11, "bold"))
        self._title_lbl.pack(side="left", padx=10, pady=8)
        tk.Button(bar, text=t("op.preview_script"), command=self._preview, background="#122030",
                  foreground=_TEXT, font=_FB, relief="flat").pack(side="right", padx=4, pady=6)
        tk.Button(bar, text=t("op.apply_mod_project"), command=self._apply, background="#163048",
                  foreground=_GOLD_BRT, font=_FB, relief="flat").pack(side="right", padx=(4, 10), pady=6)

        self._placeholder = tk.Frame(self, background=_BG)
        self._ph_lbl = tk.Label(self._placeholder, text="", background=_BG, foreground=_DIM,
                                font=("Courier New", 10), justify="left", wraplength=620)
        self._ph_lbl.pack(padx=30, pady=40, anchor="nw")

        self._content = tk.Frame(self, background=_BG)
        left = tk.Frame(self._content, background=_PANEL); left.pack(side="left", fill="both", expand=True)
        right = tk.Frame(self._content, background=_PANEL); right.pack(side="right", fill="both", expand=True)

        # ── REGISTRATION + SELECTION LIST (all kinds) ──────────────────────────────
        self._name_v = tk.StringVar()
        reg = tk.LabelFrame(left, text=t("op.selection_screen_entry"), background=_PANEL, foreground=_GOLD,
                            font=_FB); reg.pack(fill="x", padx=8, pady=(8, 4))
        rn = tk.Frame(reg, background=_PANEL); rn.pack(fill="x", pady=2)
        self._lbl(rn, t("common.display_name")).pack(side="left", padx=(4, 6)); self._entry(rn, self._name_v, 34).pack(side="left")
        tk.Label(reg, text=t("op.order_menu_list_drag_free"),
                 background=_PANEL, foreground=_DIM, font=_F).pack(anchor="w", padx=4, pady=(4, 0))
        lr = tk.Frame(reg, background=_PANEL); lr.pack(fill="x", padx=4)
        self._list_lb = tk.Listbox(lr, height=8, background=_WIDGET, foreground=_TEXT, selectbackground=_SEL,
                                   selectforeground=_GOLD_BRT, font=_F, highlightthickness=0, exportselection=False)
        self._list_lb.pack(side="left", fill="both", expand=True, pady=2)
        mv = tk.Frame(lr, background=_PANEL); mv.pack(side="left", fill="y", padx=(4, 0))
        # Reorder WITHIN the group only. Group ORDER is decided by the game from the key value (player
        # count / mode category) — it can't be set by list order (proof: the DLC packs are stored
        # unsorted yet still render in the right groups). To change which group this is in, use the
        # 'Group' controls below (they change its key value).
        tk.Button(mv, text=t("op.up"), command=lambda: self._move_in_group(-1), background="#122030",
                  foreground=_TEXT, font=_FB, relief="flat", width=9).pack(pady=(2, 1))
        tk.Button(mv, text=t("op.down"), command=lambda: self._move_in_group(+1), background="#122030",
                  foreground=_TEXT, font=_FB, relief="flat", width=9).pack()
        self._pos_lbl = tk.Label(reg, text="", background=_PANEL, foreground="#a0c0e0", font=_F,
                                 anchor="w", justify="left", wraplength=440)
        self._pos_lbl.pack(anchor="w", padx=4, pady=(2, 2))
        self._reorder_note = tk.Label(reg, text="", background=_PANEL, foreground=_DIM, font=_F,
                                      justify="left", wraplength=440)
        self._reorder_note.pack(anchor="w", padx=4)
        # group assignment: move into an existing group, or create a brand-new one (for custom content).
        # HIDDEN for MP — an MP map's group (player count) is owned by the Game Modes section of the Map
        # Editor tab, so it can't be set arbitrarily here. Shown for campaign/operation (their category IS
        # set here). Stored as an attribute so set_binding can pack/forget it per kind.
        self._group_row = grow = tk.Frame(reg, background=_PANEL); grow.pack(fill="x", padx=4, pady=(0, 3))
        tk.Label(grow, text=t("op.group_2"), background=_PANEL, foreground=_TEXT, font=_F).pack(side="left")
        self._group_cb = ttk.Combobox(grow, state="readonly", width=16, font=_F); self._group_cb.pack(side="left", padx=4)
        self._group_vals = []
        tk.Button(grow, text=t("op.move_into_group"), command=self._assign_to_group, background="#122030",
                  foreground=_TEXT, font=_FB, relief="flat").pack(side="left")
        tk.Button(grow, text=t("op.new_group"), command=self._new_group, background="#163048",
                  foreground=_GOLD_BRT, font=_FB, relief="flat").pack(side="left", padx=4)

        # ── MISSION-ONLY (campaign/operation): date/briefing/difficulty, rules, camps, objectives ──
        self._op_only = tk.Frame(self._content, background=_BG)   # (re)packed by set_binding
        # NOTE: parented to _content so it sits beside left/right; we pack/forget it as a block.
        opl = tk.Frame(self._op_only, background=_PANEL); opl.pack(side="left", fill="both", expand=True)
        opr = tk.Frame(self._op_only, background=_PANEL); opr.pack(side="right", fill="both", expand=True)

        self._sub_v = tk.StringVar(); self._diff_v = tk.StringVar(value="1"); self._players_v = tk.StringVar(value="1")
        det = tk.LabelFrame(opl, text=t("op.briefing_difficulty"), background=_PANEL, foreground=_GOLD, font=_FB)
        det.pack(fill="x", padx=8, pady=(8, 4))
        rs = tk.Frame(det, background=_PANEL); rs.pack(fill="x", pady=2)
        self._lbl(rs, t("op.date_place")).pack(side="left", padx=(4, 6)); self._entry(rs, self._sub_v, 30).pack(side="left")
        rbf = tk.Frame(det, background=_PANEL); rbf.pack(fill="x", pady=2)
        self._lbl(rbf, t("op.briefing")).pack(side="left", padx=(4, 6), anchor="n")
        self._brief = scrolledtext.ScrolledText(rbf, width=38, height=4, background=_WIDGET, foreground=_TEXT,
                                                 insertbackground=_TEXT, font=_F, relief="flat",
                                                 highlightthickness=1, highlightbackground="#243748", wrap="word")
        self._brief.pack(side="left", fill="x", expand=True)
        rd = tk.Frame(det, background=_PANEL); rd.pack(fill="x", pady=2)
        self._lbl(rd, t("op.players")).pack(side="left", padx=(4, 6)); self._entry(rd, self._players_v, 4).pack(side="left")
        tk.Label(det, text=t("op.mode_mission_group_menu_grouping"), background=_PANEL, foreground=_DIM, font=_F,
                 justify="left", wraplength=420).pack(anchor="w", padx=4)

        self._fow_v = tk.IntVar(value=1); self._time_v = tk.StringVar(value="1500")
        self._score_v = tk.BooleanVar(value=True); self._ruse_hud_v = tk.BooleanVar(value=True)
        gr = tk.LabelFrame(opl, text=t("op.game_rules"), background=_PANEL, foreground=_GOLD, font=_FB)
        gr.pack(fill="x", padx=8, pady=4)
        r = tk.Frame(gr, background=_PANEL); r.pack(fill="x", pady=2)
        self._lbl(r, t("op.time_s")).pack(side="left", padx=(4, 6)); self._entry(r, self._time_v, 8).pack(side="left")
        tk.Checkbutton(r, text=t("op.fog_war"), variable=self._fow_v, onvalue=1, offvalue=0, background=_PANEL,
                       foreground=_TEXT, selectcolor=_WIDGET, font=_F, activebackground=_PANEL).pack(side="left", padx=(12, 0))
        tk.Checkbutton(r, text=t("op.use_score"), variable=self._score_v, background=_PANEL, foreground=_TEXT,
                       selectcolor=_WIDGET, font=_F, activebackground=_PANEL).pack(side="left", padx=8)
        tk.Checkbutton(gr, text=t("op.show_ruse_zones_hud"), variable=self._ruse_hud_v, background=_PANEL,
                       foreground=_TEXT, selectcolor=_WIDGET, font=_F, activebackground=_PANEL).pack(anchor="w", padx=4)

        cf = tk.LabelFrame(opl, text=t("op.camps_players_ai"), background=_PANEL, foreground=_GOLD, font=_FB)
        cf.pack(fill="both", expand=True, padx=8, pady=4)
        self._camp_lb = tk.Listbox(cf, height=5, background=_WIDGET, foreground=_TEXT, selectbackground=_SEL,
                                   selectforeground=_GOLD_BRT, font=_F, highlightthickness=0, exportselection=False)
        self._camp_lb.pack(fill="x", padx=4, pady=2); self._camp_lb.bind("<<ListboxSelect>>", self._on_camp_pick)
        cform = tk.Frame(cf, background=_PANEL); cform.pack(fill="x", padx=4)
        self._cv_var = tk.StringVar(); self._cv_alli = tk.StringVar(value="1")
        self._cv_nat = tk.StringVar(value="EU"); self._cv_ia = tk.StringVar(value="Player"); self._cv_ai = tk.BooleanVar(value=False)
        cg = tk.Frame(cform, background=_PANEL); cg.pack(fill="x")
        self._lbl(cg, t("op.var")).grid(row=0, column=0, sticky="w"); self._entry(cg, self._cv_var, 14).grid(row=0, column=1, padx=2)
        self._lbl(cg, t("op.alliance")).grid(row=0, column=2, sticky="w"); self._entry(cg, self._cv_alli, 4).grid(row=0, column=3, padx=2)
        self._lbl(cg, t("op.nation")).grid(row=1, column=0, sticky="w")
        ttk.Combobox(cg, textvariable=self._cv_nat, values=ops.NATIONS, state="readonly", width=12, font=_F).grid(row=1, column=1, padx=2)
        self._lbl(cg, t("op.ai_level")).grid(row=1, column=2, sticky="w")
        ttk.Combobox(cg, textvariable=self._cv_ia, values=ops.AI_LEVELS, state="readonly", width=10, font=_F).grid(row=1, column=3, padx=2)
        tk.Checkbutton(cform, text=t("op.activate_full_ai_descriptoriaall"), variable=self._cv_ai, background=_PANEL,
                       foreground=_TEXT, selectcolor=_WIDGET, font=_F, activebackground=_PANEL).pack(anchor="w")
        cbb = tk.Frame(cform, background=_PANEL); cbb.pack(fill="x", pady=2)
        tk.Button(cbb, text=t("op.add_update"), command=self._save_camp, background="#122030", foreground=_TEXT, font=_FB, relief="flat").pack(side="left")
        tk.Button(cbb, text=t("op.remove"), command=self._remove_camp, background="#122030", foreground=_TEXT, font=_FB, relief="flat").pack(side="left", padx=4)

        of = tk.LabelFrame(opr, text=t("op.objectives_progression_victory"), background=_PANEL, foreground=_GOLD, font=_FB)
        of.pack(fill="both", expand=True, padx=8, pady=8)
        tk.Button(of, text=t("op.start_from_template_2"), command=self._pick_template, background="#163048",
                  foreground=_GOLD_BRT, font=_FB, relief="flat").pack(anchor="w", padx=4, pady=(2, 0))
        self._obj_lb = tk.Listbox(of, height=7, background=_WIDGET, foreground=_TEXT, selectbackground=_SEL,
                                  selectforeground=_GOLD_BRT, font=_F, highlightthickness=0, exportselection=False)
        self._obj_lb.pack(fill="x", padx=4, pady=2); self._obj_lb.bind("<<ListboxSelect>>", self._on_obj_pick)
        oform = tk.Frame(of, background=_PANEL); oform.pack(fill="x", padx=4)
        self._ov_tid = tk.StringVar(value="CH_OB01"); self._ov_kind = tk.StringVar(value="eliminate_camp")
        self._ov_target = tk.StringVar(); self._ov_zone = tk.StringVar()
        self._ov_secs = tk.StringVar(value="600"); self._ov_count = tk.StringVar(value="1")
        self._ov_score = tk.StringVar(value="0"); self._ov_vic = tk.BooleanVar(value=False)
        self._ov_def = tk.BooleanVar(value=False)
        self._ov_cond = None; self._ov_succ = []; self._ov_fail = []   # composable overrides
        og = tk.Frame(oform, background=_PANEL); og.pack(fill="x")
        self._lbl(og, t("op.tracking")).grid(row=0, column=0, sticky="w"); self._entry(og, self._ov_tid, 12).grid(row=0, column=1, padx=2)
        self._lbl(og, t("op.kind")).grid(row=0, column=2, sticky="w")
        ttk.Combobox(og, textvariable=self._ov_kind, values=list(ops.OBJECTIVE_KINDS), state="readonly", width=14, font=_F).grid(row=0, column=3, padx=2)
        self._lbl(og, t("op.target_camp")).grid(row=1, column=0, sticky="w"); self._entry(og, self._ov_target, 12).grid(row=1, column=1, padx=2)
        self._lbl(og, t("op.zone_tag")).grid(row=1, column=2, sticky="w"); self._entry(og, self._ov_zone, 12).grid(row=1, column=3, padx=2)
        self._lbl(og, t("op.seconds")).grid(row=2, column=0, sticky="w"); self._entry(og, self._ov_secs, 8).grid(row=2, column=1, padx=2)
        self._lbl(og, t("op.count")).grid(row=2, column=2, sticky="w"); self._entry(og, self._ov_count, 8).grid(row=2, column=3, padx=2)
        self._lbl(og, t("op.score")).grid(row=3, column=0, sticky="w"); self._entry(og, self._ov_score, 8).grid(row=3, column=1, padx=2)
        tk.Checkbutton(og, text=t("op.win_2"), variable=self._ov_vic, background=_PANEL, foreground=_TEXT,
                       selectcolor=_WIDGET, font=_F, activebackground=_PANEL).grid(row=3, column=2, sticky="w")
        tk.Checkbutton(og, text=t("op.lose_2"), variable=self._ov_def, background=_PANEL, foreground=_TEXT,
                       selectcolor=_WIDGET, font=_F, activebackground=_PANEL).grid(row=3, column=3, sticky="w")
        # composable overrides: a custom condition tree + success/fail action lists (any DSL block)
        cbtns = tk.Frame(oform, background=_PANEL); cbtns.pack(fill="x", pady=(2, 0))
        self._ov_cond_lbl = tk.Label(cbtns, text=t("op.condition_kind_above"), background=_PANEL,
                                     foreground=_DIM, font=_F)
        self._ov_cond_lbl.pack(side="left")
        tk.Button(cbtns, text=t("op.custom_condition"), command=self._obj_set_condition, background="#122030",
                  foreground=_TEXT, font=_F, relief="flat").pack(side="right")
        abtns = tk.Frame(oform, background=_PANEL); abtns.pack(fill="x")
        tk.Button(abtns, text=t("op.win_actions"), command=lambda: self._obj_actions("succ"),
                  background="#122030", foreground=_TEXT, font=_F, relief="flat").pack(side="left")
        tk.Button(abtns, text=t("op.lose_actions"), command=lambda: self._obj_actions("fail"),
                  background="#122030", foreground=_TEXT, font=_F, relief="flat").pack(side="left", padx=4)
        obb = tk.Frame(oform, background=_PANEL); obb.pack(fill="x", pady=2)
        tk.Button(obb, text=t("op.add_update"), command=self._save_obj, background="#122030", foreground=_TEXT, font=_FB, relief="flat").pack(side="left")
        tk.Button(obb, text=t("op.remove"), command=self._remove_obj, background="#122030", foreground=_TEXT, font=_FB, relief="flat").pack(side="left", padx=4)

        # ── scripted events + AI directives (the comprehensive layer) ───────────────────
        ef = tk.LabelFrame(opr, text=t("op.scripted_events_ai_when_do"), background=_PANEL,
                           foreground=_GOLD, font=_FB)
        ef.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        tk.Label(ef, text=t("op.events_when_condition_met_run"),
                 background=_PANEL, foreground=_DIM, font=_F, wraplength=420, justify="left").pack(anchor="w", padx=4)
        self._ev_lb = tk.Listbox(ef, height=4, background=_WIDGET, foreground=_TEXT, selectbackground=_SEL,
                                 selectforeground=_GOLD_BRT, font=_F, highlightthickness=0, exportselection=False)
        self._ev_lb.pack(fill="x", padx=4, pady=2)
        evb = tk.Frame(ef, background=_PANEL); evb.pack(fill="x", padx=4)
        tk.Button(evb, text=t("op.new_event"), command=self._new_event, background="#163048",
                  foreground=_GOLD_BRT, font=_FB, relief="flat").pack(side="left")
        tk.Button(evb, text=t("common.edit_2"), command=self._edit_event, background="#122030", foreground=_TEXT,
                  font=_FB, relief="flat").pack(side="left", padx=4)
        tk.Button(evb, text=t("op.remove"), command=self._remove_event, background="#122030", foreground=_TEXT,
                  font=_FB, relief="flat").pack(side="left")
        self._ai_lb = tk.Listbox(ef, height=3, background=_WIDGET, foreground=_TEXT, selectbackground=_SEL,
                                 selectforeground=_GOLD_BRT, font=_F, highlightthickness=0, exportselection=False)
        self._ai_lb.pack(fill="x", padx=4, pady=(6, 2))
        aib = tk.Frame(ef, background=_PANEL); aib.pack(fill="x", padx=4)
        tk.Button(aib, text=t("op.add_ai_directive"), command=self._new_ai, background="#163048",
                  foreground=_GOLD_BRT, font=_FB, relief="flat").pack(side="left")
        tk.Button(aib, text=t("op.remove"), command=self._remove_ai, background="#122030", foreground=_TEXT,
                  font=_FB, relief="flat").pack(side="left", padx=4)
        tk.Button(ef, text=t("op.edit_raw_script_xyz"), command=self._edit_raw_script, background="#163048",
                  foreground=_GOLD_BRT, font=_FB, relief="flat").pack(anchor="w", padx=6, pady=(6, 2))
        tk.Label(ef, text=t("op.add_update_these_panels_build"),
                 background=_PANEL, foreground=_DIM, font=_F, justify="left", wraplength=420).pack(anchor="w", padx=6)

    def _edit_raw_script(self):
        if self.open_script_cb:
            self.open_script_cb()
        else:
            ui_util.info(self, t("op.raw_script"), t("op.raw_xyz_editing_isn_t"))

    # ── brief / camps / objectives ──────────────────────────────────────────────────
    def _brief_get(self):
        return self._brief.get("1.0", "end").rstrip("\n")
    def _brief_set(self, s):
        self._brief.delete("1.0", "end"); self._brief.insert("1.0", s)

    def _reload_lists(self):
        self._camp_lb.delete(0, "end")
        for c in self.op.camps:
            self._camp_lb.insert("end", "%s  ·  A%d %s %s" % (c.var, c.alliance, c.nationalite, c.niveau_ia))
        self._obj_lb.delete(0, "end")
        for o in self.op.objectives:
            kindlbl = t("op.custom") if o.condition is not None else (o.kind or "?")
            tags = (("  +%d" % o.bonus_score) if o.bonus_score else "") + (t("op.win") if o.is_victory else "") + \
                   (t("op.lose") if o.is_defeat else "")
            self._obj_lb.insert("end", "%s  ·  %s%s" % (o.tracking_id, kindlbl, tags))
        if hasattr(self, "_ev_lb"):
            self._ev_lb.delete(0, "end")
            for ev in self.op.events:
                self._ev_lb.insert("end", t("op.name_n_action_s_rep",
                                   name=ev.name, n=len(ev.actions), rep=("  ⟳" if ev.repeat else "")))
            self._ai_lb.delete(0, "end")
            for d in self.op.ai:
                self._ai_lb.insert("end", "%s  ·  %s" % (d.camp, ", ".join(_blk_label(b) for b in d.blocks)))

    def _on_camp_pick(self, _=None):
        s = self._camp_lb.curselection()
        if not s:
            return
        c = self.op.camps[s[0]]
        self._cv_var.set(c.var); self._cv_alli.set(str(c.alliance)); self._cv_nat.set(c.nationalite)
        self._cv_ia.set(c.niveau_ia); self._cv_ai.set(c.activate_full_ai)

    def _save_camp(self):
        var = self._cv_var.get().strip()
        if not var:
            ui_util.info(self, t("op.camp"), t("op.enter_camp_var_name")); return
        try:
            alli = int(self._cv_alli.get())
        except ValueError:
            ui_util.info(self, t("op.camp"), t("op.alliance_must_integer")); return
        c = ops.Camp(var=var, alliance=alli, nationalite=self._cv_nat.get(), niveau_ia=self._cv_ia.get(),
                     activate_full_ai=self._cv_ai.get(), couleur="Color_01_Blue" if alli == 1 else "Color_02_Red",
                     partage_camion=(self._cv_ia.get() == "Player"), player_name_challenge=(self._cv_ia.get() == "Player"))
        ex = next((i for i, x in enumerate(self.op.camps) if x.var == var), None)
        if ex is not None:
            self.op.camps[ex] = c
        else:
            self.op.camps.append(c)
        self._reload_lists()

    def _remove_camp(self):
        s = self._camp_lb.curselection()
        if s:
            del self.op.camps[s[0]]; self._reload_lists()

    def _on_obj_pick(self, _=None):
        s = self._obj_lb.curselection()
        if not s:
            return
        o = self.op.objectives[s[0]]
        self._ov_tid.set(o.tracking_id); self._ov_kind.set(o.kind or "eliminate_camp")
        self._ov_target.set(o.target_camp or ""); self._ov_zone.set(o.zone_tag or "")
        self._ov_secs.set(str(o.seconds)); self._ov_count.set(str(o.count))
        self._ov_score.set(str(o.bonus_score)); self._ov_vic.set(o.is_victory); self._ov_def.set(o.is_defeat)
        self._ov_cond = o.condition; self._ov_succ = list(o.actions_success); self._ov_fail = list(o.actions_fail)
        self._refresh_obj_cond_lbl()

    def _refresh_obj_cond_lbl(self):
        self._ov_cond_lbl.config(text=t("op.condition_label", label=_blk_label(self._ov_cond)) if self._ov_cond
                                 else t("op.condition_kind_above"))

    def _obj_set_condition(self):
        blk = opf.block_picker_dialog(self, "condition", self._providers(), self._ov_cond)
        self._ov_cond = blk
        self._refresh_obj_cond_lbl()

    def _obj_actions(self, which):
        lst = self._ov_succ if which == "succ" else self._ov_fail
        self._edit_action_list(t("op.which_actions", which=(t("op.win_3") if which == "succ" else t("op.lose_3"))), lst)

    def _save_obj(self):
        tid = self._ov_tid.get().strip()
        if not tid:
            ui_util.info(self, t("op.objective"), t("op.enter_tracking_id")); return
        try:
            o = ops.Objective(tracking_id=tid, bonus_score=int(self._ov_score.get() or 0),
                              is_victory=self._ov_vic.get(), is_defeat=self._ov_def.get(),
                              condition=self._ov_cond, actions_success=list(self._ov_succ),
                              actions_fail=list(self._ov_fail),
                              kind=(None if self._ov_cond is not None else self._ov_kind.get()),
                              target_camp=self._ov_target.get().strip() or None,
                              zone_tag=self._ov_zone.get().strip() or None,
                              seconds=int(self._ov_secs.get() or 600), count=int(self._ov_count.get() or 1))
        except ValueError:
            ui_util.info(self, t("op.objective"), t("op.seconds_count_score_must_integers")); return
        ex = next((i for i, x in enumerate(self.op.objectives) if x.tracking_id == tid), None)
        if ex is not None:
            self.op.objectives[ex] = o
        else:
            self.op.objectives.append(o)
        self._ov_cond = None; self._ov_succ = []; self._ov_fail = []; self._refresh_obj_cond_lbl()
        self._reload_lists()

    def _remove_obj(self):
        s = self._obj_lb.curselection()
        if s:
            del self.op.objectives[s[0]]; self._reload_lists()

    def _pick_template(self):
        """Replace the working operation with a pre-built template (the user then tweaks it)."""
        top = ui_util.themed_toplevel(self, t("op.start_from_template"), size=(360, 240), resizable=True)
        tk.Label(top, text=t("op.pick_starting_point_can_edit"),
                 background=_PANEL, foreground=_TEXT, font=_F, wraplength=330, justify="left").pack(
                     anchor="w", padx=8, pady=6)
        lb = tk.Listbox(top, background=_WIDGET, foreground=_TEXT, font=_F, selectbackground=_SEL)
        lb.pack(fill="both", expand=True, padx=8, pady=4)
        for label, _f in ops.TEMPLATES:
            lb.insert("end", label)

        def use():
            s = lb.curselection()
            if not s:
                return
            if ui_util.confirm(top, t("op.template"), t("op.replace_current_operation_template_unsav")):
                self.op = ops.TEMPLATES[s[0]][1]()
                self._ov_cond = None; self._ov_succ = []; self._ov_fail = []; self._refresh_obj_cond_lbl()
                self._reload_lists()
                top.destroy()
        tk.Button(top, text=t("op.use_template"), command=use, background="#163048", foreground=_GOLD_BRT,
                  font=_FB, relief="flat").pack(side="right", padx=8, pady=6)
        tk.Button(top, text=t("common.cancel"), command=top.destroy, background="#122030", foreground=_TEXT,
                  font=_FB, relief="flat").pack(side="right", pady=6)

    # ── providers + composable event/AI/action builders ──────────────────────────────
    def _providers(self):
        """Live choices for the catalog forms: camps, declared vars, and tag Names from this scenario."""
        return opf.Providers(
            tags=lambda kind: self._scenario_tag_names(kind),
            camps=lambda: [c.var for c in self.op.camps],
            variables=lambda kind: [v.name for v in self.op.variables])

    def _scenario_tag_names(self, kind):
        """Placement Names usable as tags. v1: the tags already referenced in the op (editable combo lets
        the user type new Names they know from the Map Editor). Real placement-Name scan: future."""
        seen = {"pos": list(self.op.position_tags), "zone": list(self.op.zone_tags),
                "unit": list(self.op.unit_tags)}.get(kind, [])
        for o in self.op.objectives:
            if kind == "zone" and o.zone_tag:
                seen.append(o.zone_tag)
        return sorted(set(seen))

    def _edit_action_list(self, title, lst):
        """Modal: add/edit/remove a list of action Blocks in place."""
        top = ui_util.themed_toplevel(self, title, size=(420, 320), resizable=True)
        lb = tk.Listbox(top, background=_WIDGET, foreground=_TEXT, font=_F, selectbackground=_SEL)
        lb.pack(fill="both", expand=True, padx=6, pady=6)
        for b in lst:
            lb.insert("end", _blk_label(b))

        def add():
            blk = opf.block_picker_dialog(top, "action", self._providers())
            if blk is not None:
                lst.append(blk); lb.insert("end", _blk_label(blk))

        def edit():
            s = lb.curselection()
            if s:
                blk = opf.block_picker_dialog(top, "action", self._providers(), lst[s[0]])
                if blk is not None:
                    lst[s[0]] = blk; lb.delete(s[0]); lb.insert(s[0], _blk_label(blk))

        def remove():
            s = lb.curselection()
            if s:
                lst.pop(s[0]); lb.delete(s[0])
        bb = tk.Frame(top, background=_PANEL); bb.pack(fill="x", padx=6, pady=6)
        for txt, cmd in ((t("op.add"), add), (t("common.edit"), edit), (t("op.remove"), remove)):
            tk.Button(bb, text=txt, command=cmd, background="#122030", foreground=_TEXT, font=_FB,
                      relief="flat").pack(side="left", padx=2)
        tk.Button(bb, text=t("op.done"), command=top.destroy, background="#163048", foreground=_GOLD_BRT,
                  font=_FB, relief="flat").pack(side="right")
        self.wait_window(top)

    def _new_event(self, existing=None):
        name = existing.name if existing else ("event_%d" % (len(self.op.events) + 1))
        cond = opf.block_picker_dialog(self, "condition", self._providers(),
                                       existing.condition if existing else None)
        if cond is None:
            return
        ev = existing or ops.Event(name, condition=cond)
        ev.condition = cond
        self._edit_action_list(t("op.event_name_actions", name=name), ev.actions)
        if existing is None:
            self.op.events.append(ev)
        self._reload_lists()

    def _edit_event(self):
        s = self._ev_lb.curselection()
        if s:
            self._new_event(self.op.events[s[0]])

    def _remove_event(self):
        s = self._ev_lb.curselection()
        if s:
            del self.op.events[s[0]]; self._reload_lists()

    def _new_ai(self):
        if not self.op.camps:
            ui_util.info(self, t("common.ai"), t("op.add_camp_first")); return
        camp = self.op.camps[0].var
        blk = opf.block_picker_dialog(self, "action", self._providers())   # AI* are kind=ai (in 'action' set)
        if blk is None:
            return
        # pick which camp this applies to (use the block's Camp param if set, else first camp)
        camp = blk.params.get("Camp") or camp
        self.op.ai.append(ops.AIDirective(camp, [blk]))
        self._reload_lists()

    def _remove_ai(self):
        s = self._ai_lb.curselection()
        if s:
            del self.op.ai[s[0]]; self._reload_lists()

    # ── build + apply ────────────────────────────────────────────────────────────────
    def _sync_rules(self):
        try:
            self.op.rules.game_time = int(self._time_v.get())
        except ValueError:
            pass
        self.op.rules.fow = self._fow_v.get()
        self.op.rules.use_score = self._score_v.get()
        self.op.rules.ruse_zones_hud = self._ruse_hud_v.get()
        self.op.zone_tags = sorted({o.zone_tag for o in self.op.objectives if o.zone_tag})
        self.op.defeat_on_no_units = next((c.var for c in self.op.camps if c.niveau_ia == "Player"), None)

    def _preview(self):
        if self.binding is None or self.binding.kind not in ("campaign", "operation"):
            ui_util.info(self, t("common.preview"), t("op.script_preview_applies_campaign_operatio"))
            return
        self._sync_rules()
        src = ops.generate_source(self.op)
        w = ui_util.themed_toplevel(self, t("op.generated_mission_script_preview"), size=(760, 620), resizable=True, modal=False)
        txt = scrolledtext.ScrolledText(w, background=_WIDGET, foreground=_TEXT, insertbackground=_TEXT,
                                        font=("Consolas", 9), wrap="none")
        txt.pack(fill="both", expand=True); txt.insert("1.0", src); txt.config(state="disabled")

    def _apply(self):
        b = self.binding
        if b is None:
            return
        mission = b.kind in ("campaign", "operation")
        notes = []
        g = self._glob_ndf(); inst = self._info_inst(g)
        # selection-screen text (name for all kinds; date/briefing for missions)
        labeled = []
        if self._name_v.get().strip():
            labeled.append(("Description", self._name_v.get().strip()))
        if mission and self._sub_v.get().strip():
            labeled.append(("LongDescription1", self._sub_v.get().strip()))
        if mission and self._brief_get().strip():
            labeled.append(("LongDescription2", self._brief_get().strip()))
        if labeled and inst is not None:
            try:
                keys, blobs = scenario_gen.apply_loc_strings(_ProjDat(self.project, "loc"), labeled)
                for vp, data in blobs.items():
                    self.project.set_raw("loc", vp, data)
                for prop, key in keys.items():
                    scenario_gen._set_raw(g, inst, prop, bytes(key))
                self.project.mark_dirty("gameplay", scenario_gen.GLOBALS_PATH)
                notes.append(t("op.name_text_n", n=len(labeled)))
            except Exception as e:
                notes.append(t("op.text_failed_err", err=e))
        if mission and inst is not None:
            try:
                if self._players_v.get().strip() and g.prop_by_name_and_class("NbPlayers", inst.class_index):
                    scenario_gen._set_raw(g, inst, "NbPlayers", int(self._players_v.get()))
                    self.project.mark_dirty("gameplay", scenario_gen.GLOBALS_PATH)
                    notes.append(t("op.players_2"))
            except Exception as e:
                notes.append(t("op.players_failed_err", err=e))
        if mission:
            self._sync_rules()
            src = ops.generate_source(self.op)
            suffix = b.scenario_name[len("leveldesign"):] if b.scenario_name.startswith("leveldesign") else ""
            sp = "genpython\\10000\\test\\map\\%s\\scripting%s\\effetmap.xyz" % (b.map_dir, suffix)
            if xyzc.have_native_compiler():
                try:
                    self.project.set_raw("scripts", sp, xyzc.compile_to_xyz(src))
                    notes.append(t("op.compiled_mission_script"))
                except Exception as e:
                    notes.append(t("op.script_compile_failed_err", err=e))
            else:
                notes.append(t("op.script_not_compiled_no_ruse"))
        notes.append(t("op.list_position_applied_live_via"))
        if self.on_done:
            self.on_done()
        ui_util.info(self, t("op.applied"), t("op.staged_into_mod_project") + "\n  - ".join(notes) +
                            t("op.click_save_mod_above_write"))

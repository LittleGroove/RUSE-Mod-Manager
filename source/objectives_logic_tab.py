"""'Objectives & Logic' tab — edit how the selected operation PLAYS: each objective's score, trigger condition,
tunable threshold, AND the on-screen / menu text the player reads (with the %d->variable wiring made visible).

Two edit targets, both staged into the MOD PROJECT (persisted by its "Save to mod"):
  - LOGIC (score, threshold) -> the 'scripts' dat, via the GOLDEN PATH (decompile -> edit source -> Python 2.5.1
    recompile -> set_raw). See docs/operation_editor/05-editing-a-script.md.
  - DISPLAY TEXT (DrawLabel Text / FoldedText) -> the 'loc' dat (per-map localization.dic, per language). The
    number a player sees is a %d in that string, filled at runtime from the DrawLabel's ListVariables — for a
    counter objective that variable IS the threshold, so the editor shows the link. See memory
    project-objective-text-localization.

Binding-driven (follows the top Map/Scenario dropdowns). Doc 03 View 3 (forms); Timeline + Graph views to follow.
"""
import struct
import tkinter as tk
from tkinter import ttk

import ui_util
from i18n import t
from ruse_mod_engine import dic as dicmod
from ruse_mod_engine import script_logic as SL

import theme                # single source of truth for the palette; local names kept unchanged
_BG = theme.BG; _PANEL = theme.PANEL; _WIDGET = theme.WIDGET; _TEXT = theme.TEXT; _DIM = theme.DIM
_GOLD = theme.GOLD; _GOLD_BRT = theme.GOLD_BRT; _WARN = "#e0a44a"; _LINK = "#6fb0d8"
_F = theme.F; _FB = theme.FB; _FS = theme.FS

_CMP = {
    "OperatorIntegerMoreOrEqual": "≥", "OperatorIntegerStrictlyMore": ">",
    "OperatorIntegerLessOrEqual": "≤", "OperatorIntegerStrictlyLess": "<",
    "OperatorIntegerEqual": "=", "OperatorIntegerDifferent": "≠",
}


class _ProjDat:
    def __init__(self, project, key):
        self.project = project; self.key = key

    def list(self):
        try:
            return self.project.entry_paths(self.key)
        except Exception:
            return []


def _script_suffix(scenario_name):
    for pre in ("leveldesign", "scenario"):
        if scenario_name and scenario_name.lower().startswith(pre):
            return scenario_name[len(pre):]
    return ""


def _hkey(lochash):
    return struct.pack("<Q", int(str(lochash).rstrip("L"))) if lochash else None


class ObjectivesLogicFrame(tk.Frame):
    def __init__(self, parent, project, on_done=None, **_kw):
        super().__init__(parent, background=_BG)
        self.project = project
        self.on_done = on_done
        self.binding = None
        self._ep = None          # cached effetmap vpath
        self._xyz = None         # cached original .xyz bytes
        self._src = None         # cached decompiled source (the logic edit surface)
        self._obj_rows = []      # per objective: edit vars + originals
        self._timer_rows = []    # mission-time edit rows (GameTime + VariableTimers)
        self._gr = None          # parsed GameRules
        self._gr_widgets = []    # mission-rules edit widgets
        self._build()

    # ---- project data access ----
    def _lang(self):
        return self._lang_v.get() if hasattr(self, "_lang_v") else "us"

    def _effetmap_path(self):
        b = self.binding
        want = ("\\map\\%s\\scripting%s\\effetmap.xyz" % (b.map_dir, _script_suffix(b.scenario_name))).lower()
        return next((v for v in _ProjDat(self.project, "scripts").list()
                     if v.lower().replace("/", "\\").endswith(want)), None)

    def _loc_path(self, lang=None):
        """The given language's localization.dic for this scenario (objective display text)."""
        b = self.binding
        lang = lang or self._lang()
        want = ("\\map\\%s\\scripting%s\\localization.dic" % (b.map_dir, _script_suffix(b.scenario_name))).lower()
        seg = ("\\translations\\%s\\" % lang).lower()
        cands = [v for v in _ProjDat(self.project, "loc").list() if v.lower().replace("/", "\\").endswith(want)]
        return next((v for v in cands if seg in v.lower().replace("/", "\\")), (cands[0] if cands else None))

    def _loc_blob(self):
        vp = self._loc_path()
        if not vp:
            return None
        try:
            return self.project.get_raw("loc", vp)
        except Exception:
            return None

    def _resolve(self, blob, lochash):
        if not blob or blob[:3] != b"TRA" or not lochash:
            return ""
        try:
            return dicmod.get_entry(blob, _hkey(lochash)) or ""
        except Exception:
            return ""

    # ---- layout ----
    def _build(self):
        head = tk.Frame(self, background=_PANEL); head.pack(side="top", fill="x")
        self._hdr = tk.Label(head, text=t("common.follows_map_scenario_selection_above"),
                             background=_PANEL, foreground=_GOLD_BRT, font=_FB, anchor="w")
        self._hdr.pack(side="left", padx=8, pady=6)
        tk.Button(head, text=t("common.apply_project"), command=self._apply, background="#122030",
                  foreground=_GOLD_BRT, font=_FB, relief="flat").pack(side="right", padx=8)
        # language selector — display text is per-language; logic (score/threshold) is not
        self._lang_v = tk.StringVar(value="us")
        cb = ttk.Combobox(head, state="readonly", width=6, font=_F, textvariable=self._lang_v,
                          values=[c for c, _ in dicmod.LANGUAGES])
        cb.pack(side="right", padx=(2, 6))
        cb.bind("<<ComboboxSelected>>", lambda e: self._render_objectives())
        tk.Label(head, text=t("obj.text_language"), background=_PANEL, foreground=_GOLD,
                 font=_FB).pack(side="right", padx=(8, 2))

        body = tk.Frame(self, background=_BG); body.pack(side="top", fill="both", expand=True, padx=10, pady=8)
        tk.Label(body, text=t("obj.objectives_display_text_score_trigger"),
                 background=_BG, foreground=_GOLD, font=_FB).pack(anchor="w")
        holder = tk.Frame(body, background=_BG); holder.pack(fill="both", expand=True)
        self._objframe = ui_util.make_scrollable(holder, bg=_BG)

        self._status = tk.Label(self, text=t("common.select_map_scenario_above"),
                                background=_BG, foreground=_DIM, font=_F, anchor="w")
        self._status.pack(side="bottom", fill="x", padx=10, pady=4)

    def _toast(self, msg, err=False, warn=False):
        try:
            self._status.config(text=msg, foreground=("#e06a6a" if err else _WARN if warn else _DIM))
            self.update_idletasks()
        except Exception:
            pass

    # ---- binding-driven load ----
    def set_binding(self, binding):
        self.binding = binding
        for w in self._objframe.winfo_children():
            w.destroy()
        self._obj_rows = []
        if binding is None:
            self._hdr.config(text=t("common.no_scenario_selected"))
            self._ep = self._xyz = self._src = None
            self._toast(t("common.select_map_scenario_above")); return
        kind = getattr(binding, "kind", "")
        self._hdr.config(text=t("common.map_scn_kind").format(
            map=binding.map_dir, scn=binding.scenario_name, kind=kind))
        if kind not in ("operation", "campaign"):
            self._ep = self._xyz = self._src = None
            self._toast(t("obj.multiplayer_map_scenario_no_mission")); return
        if not SL.have_compiler():
            self._toast(t("obj.python_2_5_1_recompiler"), err=True); return
        ep = self._effetmap_path()
        if not ep:
            self._ep = self._xyz = self._src = None
            self._toast(t("common.no_mission_script_found_scenario")); return
        if ep != self._ep:
            self._toast(t("obj.reading_mission_script_decompiling_takes"))
            try:
                self._xyz = self.project.get_raw("scripts", ep)
                self._src = SL.decompile_xyz(self._xyz)
                self._ep = ep
            except Exception as e:
                self._ep = self._xyz = self._src = None
                self._toast(t("common.could_not_read_mission_script").format(e=e), err=True); return
        self._render_objectives()

    def _wiring_note(self, o):
        """Which variables/timers fill the on-screen %d/%s, flagging the threshold/timer link."""
        thr = o.get("threshold_ir")
        bits = []
        for v in (o.get("list_vars") or []):
            bits.append("%d←" + v + (t("obj.threshold") if v == thr else ""))
        for v in (o.get("timer_vars") or []):
            bits.append("%s←" + v + (t("obj.timer") if v == thr else ""))
        return (t("obj.screen_2") + ", ".join(bits)) if bits else ""

    def _render_rewards(self, card, o):
        """Show what happens on success/failure: numeric consequences (money/units/score) get an editable
        field; outcomes (VICTORY/DEFEAT) + chained objectives show as read-only badges. Returns edit rows."""
        rrows = []
        _badge = {"win": "#7fd17f", "lose": "#e06a6a", "objective": _LINK}
        for key, head, hcol in (("rewards", t("obj.success"), _LINK), ("fail_rewards", t("obj.fail"), _WARN)):
            rws = o.get(key) or []
            if not rws:
                continue
            rr = tk.Frame(card, background=_PANEL); rr.pack(fill="x", padx=(34, 6), pady=(0, 1))
            tk.Label(rr, text=head, background=_PANEL, foreground=hcol, font=_FS).pack(side="left")
            for r in rws:
                if r.get("field") and r.get("value") is not None:
                    tk.Label(rr, text=" " + r["label"] + ":", background=_PANEL, foreground=_GOLD,
                             font=_FS).pack(side="left")
                    var = tk.StringVar(value=str(r["value"]))
                    tk.Entry(rr, textvariable=var, background=_WIDGET, foreground=_TEXT, insertbackground=_TEXT,
                             font=_FS, width=7).pack(side="left", padx=(1, 4))
                    rrows.append({"ir": r["ir"], "field": r["field"], "var": var, "orig": str(r["value"])})
                else:
                    tk.Label(rr, text="  " + r["label"], background=_PANEL,
                             foreground=_badge.get(r["cat"], _DIM), font=_FS).pack(side="left")
        return rrows

    _GR_ENUMS = {
        "GameTimeMode": (["Countdown", "Clock", "Off"], "_enum_for_game_play.TGameTimerMode."),
        "PopCapLimit": (["Undefined", "NoPopCap", "PopCapSkirmish", "PopCapSolo"],
                        "_enum_for_game_play.TPopCapConfig."),
    }
    _GR_BOOLS = ("UseScore", "ConstructionNecessiteHQ", "DisableCapture", "DisableBazooka",
                 "ScoreIgnoreCapture", "MaxScoreVictory", "MinScoreDefeat", "DefaiteArmy")

    def _render_gamerules(self):
        """The match ruleset (GameRules) — edited via set_kwarg/set_enum_field on the rules IR, recompiled."""
        self._gr_widgets = []; self._gr = None
        try:
            gr = SL.parse_gamerules(self._src)
        except Exception:
            gr = None
        if not gr:
            return
        self._gr = gr
        card = tk.Frame(self._objframe, background=_PANEL, highlightbackground=_GOLD,
                        highlightthickness=1); card.pack(fill="x", expand=True, pady=(0, 6), padx=1)
        tk.Label(card, text=t("obj.mission_rules_r").format(r=gr["type"]), background=_PANEL,
                 foreground=_GOLD_BRT, font=_FB).pack(anchor="w", padx=6, pady=(3, 1))
        for field, (domain, prefix) in self._GR_ENUMS.items():
            if field in gr["enums"]:
                cur = gr["enums"][field].rsplit(".", 1)[-1]
                r = tk.Frame(card, background=_PANEL); r.pack(fill="x", padx=6, pady=1)
                tk.Label(r, text=field + ":", background=_PANEL, foreground=_GOLD, font=_F,
                         width=20, anchor="w").pack(side="left")
                var = tk.StringVar(value=cur)
                ttk.Combobox(r, state="readonly", width=14, font=_FS, textvariable=var,
                             values=domain).pack(side="left")
                self._gr_widgets.append({"field": field, "kind": "enum", "var": var, "orig": cur, "prefix": prefix})
        for field in ("MaxScore", "MinScore"):
            if field in gr["scalars"]:
                r = tk.Frame(card, background=_PANEL); r.pack(fill="x", padx=6, pady=1)
                tk.Label(r, text=field + ":", background=_PANEL, foreground=_GOLD, font=_F,
                         width=20, anchor="w").pack(side="left")
                var = tk.StringVar(value=gr["scalars"][field])
                tk.Entry(r, textvariable=var, background=_WIDGET, foreground=_TEXT, insertbackground=_TEXT,
                         font=_F, width=10).pack(side="left")
                self._gr_widgets.append({"field": field, "kind": "scalar", "var": var, "orig": gr["scalars"][field]})
        bools = [f for f in self._GR_BOOLS if f in gr["scalars"]]
        if bools:
            wrap = tk.Frame(card, background=_PANEL); wrap.pack(fill="x", padx=6, pady=(1, 4))
            for f in bools:
                var = tk.BooleanVar(value=(gr["scalars"][f] == "True"))
                tk.Checkbutton(wrap, text=f, variable=var, background=_PANEL, foreground=_TEXT,
                               selectcolor=_WIDGET, font=_FS, activebackground=_PANEL,
                               activeforeground=_GOLD).pack(side="left", padx=1)
                self._gr_widgets.append({"field": f, "kind": "bool", "var": var, "orig": gr["scalars"][f]})

    def _render_objectives(self):
        for w in self._objframe.winfo_children():
            w.destroy()
        self._obj_rows = []
        if not self._src:
            return
        try:
            objs = SL.parse_objectives(self._src)
        except Exception as e:
            self._toast(t("obj.could_not_parse_objectives_e").format(e=e), err=True); return
        loc = self._loc_blob()
        # ── Mission time (countdown GameTime + VariableTimers), shown/edited as M:SS ──
        self._timer_rows = []
        try:
            tm = SL.parse_timers(self._src)
        except Exception:
            tm = {"game": None, "timers": []}
        if tm["game"] or tm["timers"]:
            mc = tk.Frame(self._objframe, background=_PANEL, highlightbackground=_GOLD,
                          highlightthickness=1); mc.pack(fill="x", expand=True, pady=(0, 6), padx=1)
            tk.Label(mc, text=t("obj.mission_time_m_ss"), background=_PANEL, foreground=_GOLD_BRT,
                     font=_FB).pack(anchor="w", padx=6, pady=(3, 1))

            def _timerow(label, ir, field, seconds, note):
                r = tk.Frame(mc, background=_PANEL); r.pack(fill="x", padx=6, pady=1)
                tk.Label(r, text=label, background=_PANEL, foreground=_GOLD, font=_F,
                         width=18, anchor="w").pack(side="left")
                var = tk.StringVar(value=SL.secs_to_mmss(seconds))
                tk.Entry(r, textvariable=var, background=_WIDGET, foreground=_TEXT, insertbackground=_TEXT,
                         font=_F, width=8).pack(side="left", padx=2)
                if note:
                    tk.Label(r, text=note, background=_PANEL, foreground=_DIM, font=_FS).pack(side="left", padx=(6, 0))
                self._timer_rows.append({"ir": ir, "field": field, "var": var, "orig": var.get()})

            if tm["game"]:
                _timerow(t("obj.time_limit_countdown"), tm["game"]["ir"], tm["game"]["field"],
                         tm["game"]["seconds"], "")
            for i, tt in enumerate(tm["timers"], 1):
                marks = [m for m, on in ((t("obj.screen"), tt["shown"]), (t("obj.gates_objectives"), tt["gates"])) if on]
                _timerow(t("obj.timer_n").format(n=i), tt["ir"], tt["field"], tt["seconds"], " · ".join(marks))
        self._render_gamerules()
        from collections import Counter
        used = Counter(o["threshold_ir"] for o in objs if o.get("threshold_ir"))
        n_thr = 0
        for idx, o in enumerate(objs, 1):
            shared = bool(o.get("threshold_ir") and used[o["threshold_ir"]] > 1)
            card = tk.Frame(self._objframe, background=_PANEL, highlightbackground="#1b2c3e",
                            highlightthickness=1); card.pack(fill="x", expand=True, pady=3, padx=1)
            # row 1: on-screen display text (editable -> .dic)
            r1 = tk.Frame(card, background=_PANEL); r1.pack(fill="x", padx=6, pady=(4, 1))
            tk.Label(r1, text="%2d" % idx, background=_PANEL, foreground=_DIM, font=_FB,
                     width=3, anchor="w").pack(side="left")
            tv = tk.StringVar(value=self._resolve(loc, o.get("label_hash")))
            tk.Entry(r1, textvariable=tv, background=_WIDGET, foreground=_GOLD_BRT, insertbackground=_TEXT,
                     font=_FB).pack(side="left", fill="x", expand=True)
            cmp_glyph = _CMP.get(o.get("comparison"), o.get("comparison") or "")
            trig = (o.get("condition_type") or t("common.none")) + ((" " + cmp_glyph) if cmp_glyph else "")
            tk.Label(r1, text=trig, background=_PANEL, foreground=_DIM, font=_FS, anchor="e").pack(side="right")
            # row 2: score + threshold
            r2 = tk.Frame(card, background=_PANEL); r2.pack(fill="x", padx=6, pady=(0, 1))
            tk.Label(r2, text=t("obj.score"), background=_PANEL, foreground=_GOLD, font=_F).pack(side="left")
            sval = o.get("score") or ""
            sv = tk.StringVar(value=sval)
            tk.Entry(r2, textvariable=sv, background=_WIDGET, foreground=_TEXT, insertbackground=_TEXT,
                     font=_F, width=8).pack(side="left", padx=(2, 12))
            kind = o.get("threshold_kind")
            thr_v = None; thr_orig = ""
            if kind in ("target", "literal") and o.get("threshold_ir"):
                tk.Label(r2, text=(t("obj.target") if kind == "target" else t("obj.compare")),
                         background=_PANEL, foreground=_GOLD, font=_F).pack(side="left")
                thr_orig = str(o["threshold"])
                thr_v = tk.StringVar(value=thr_orig)
                tk.Entry(r2, textvariable=thr_v, background=_WIDGET, foreground=_TEXT, insertbackground=_TEXT,
                         font=_F, width=8).pack(side="left", padx=2)
                if shared:
                    tk.Label(r2, text=t("obj.shared"), background=_PANEL, foreground=_WARN,
                             font=_FS).pack(side="left", padx=(4, 0))
                n_thr += 1
            elif kind == "timer":
                tk.Label(r2, text=t("obj.time_gated_mmss_edit_mission").format(
                    mmss=SL.secs_to_mmss(o["threshold"])), background=_PANEL, foreground=_LINK,
                    font=_FS).pack(side="left")
            elif kind == "flag":
                tk.Label(r2, text=t("obj.triggers_event_not_numeric_target"),
                         background=_PANEL, foreground=_DIM, font=_FS).pack(side="left")
            elif o.get("condition_type"):
                tk.Label(r2, text=t("obj.trigger_c").format(c=o["condition_type"]),
                         background=_PANEL, foreground=_DIM, font=_FS).pack(side="left")
            # row 3: the %d -> variable wiring (makes the threshold<->display link visible)
            note = self._wiring_note(o)
            if note:
                tk.Label(card, text=note, background=_PANEL,
                         foreground=(_LINK if o.get("threshold_in_text") else _DIM),
                         font=_FS, anchor="w").pack(fill="x", padx=(34, 6), pady=(0, 1))
            # row 4: folded / menu text (editable -> .dic) when present
            fv = None; forig = ""
            forig = self._resolve(loc, o.get("folded_hash"))
            if o.get("folded_hash") and (forig or o.get("folded_vars")):
                r4 = tk.Frame(card, background=_PANEL); r4.pack(fill="x", padx=6, pady=(0, 4))
                tk.Label(r4, text=t("obj.menu_text"), background=_PANEL, foreground=_GOLD,
                         font=_FS).pack(side="left")
                fv = tk.StringVar(value=forig)
                tk.Entry(r4, textvariable=fv, background=_WIDGET, foreground=_TEXT, insertbackground=_TEXT,
                         font=_FS).pack(side="left", fill="x", expand=True, padx=(2, 0))
            else:
                tk.Frame(card, background=_PANEL, height=2).pack()
            reward_rows = self._render_rewards(card, o)
            self._obj_rows.append({
                "obj": o,
                "score_var": sv, "score_orig": sval,
                "thr_var": thr_v, "thr_orig": thr_orig,
                "text_var": tv, "text_orig": tv.get(), "text_hash": o.get("label_hash"),
                "folded_var": fv, "folded_orig": forig, "folded_hash": o.get("folded_hash"),
                "reward_rows": reward_rows,
            })
        self._toast(t("obj.loaded_n_objectives_k_editable").format(n=len(objs), k=n_thr, lang=self._lang()))

    # ---- apply: logic edits -> recompile 'scripts' dat; text edits -> 'loc' dat ----
    def _apply(self):
        if not self._src or not self._ep:
            self._toast(t("common.select_operation_scenario_first"), err=True); return
        notes = []
        # 1) LOGIC: score + threshold -> edit source -> recompile
        src = self._src; logic_edits = 0
        try:
            for r in self._obj_rows:
                o = r["obj"]
                sv = r["score_var"].get().strip()
                if sv != r["score_orig"] and sv.lstrip("-").isdigit():
                    src = SL.set_score(src, o["ir"], int(sv)); logic_edits += 1
                if r["thr_var"] is not None:
                    tvv = r["thr_var"].get().strip()
                    if tvv != r["thr_orig"] and tvv.lstrip("-").isdigit():
                        src = SL.set_threshold(src, o, int(tvv)); logic_edits += 1
                for rr in r.get("reward_rows", []):   # money/units/score consequence amounts
                    nv = rr["var"].get().strip()
                    if nv != rr["orig"] and nv.lstrip("-").replace(".", "", 1).isdigit():
                        src = SL.set_kwarg(src, rr["ir"], rr["field"], nv); logic_edits += 1
            for tr in self._timer_rows:          # mission time (GameTime / VariableTimer.Duree), M:SS -> secs
                nv = tr["var"].get().strip()
                if nv != tr["orig"]:
                    src = SL.set_seconds(src, tr["ir"], tr["field"], SL.mmss_to_secs(nv)); logic_edits += 1
            for w in self._gr_widgets:           # mission rules (GameRules scalars / bools / enums)
                nv = ("True" if w["var"].get() else "False") if w["kind"] == "bool" else w["var"].get().strip()
                if nv == w["orig"]:
                    continue
                if w["kind"] == "enum":
                    src = SL.set_enum_field(src, self._gr["ir"], w["field"], w["prefix"] + nv)
                elif w["kind"] == "scalar" and not nv.lstrip("-").isdigit():
                    continue
                else:
                    src = SL.set_kwarg(src, self._gr["ir"], w["field"], nv)
                logic_edits += 1
        except Exception as e:
            self._toast(t("obj.logic_edit_failed_e").format(e=e), err=True); return
        if logic_edits:
            self._toast(t("obj.recompiling_mission_script_python_2"))
            try:
                new_xyz = SL.recompile_source_to_xyz(src, xyz_for_meta=self._xyz)
                self.project.set_raw("scripts", self._ep, new_xyz)
                self.project.mark_dirty("scripts", self._ep)
                self._src = src
                for r in self._obj_rows:
                    r["score_orig"] = r["score_var"].get().strip()
                    if r["thr_var"] is not None:
                        r["thr_orig"] = r["thr_var"].get().strip()
                    for rr in r.get("reward_rows", []):
                        rr["orig"] = rr["var"].get().strip()
                for tr in self._timer_rows:
                    tr["orig"] = tr["var"].get()
                for w in self._gr_widgets:
                    w["orig"] = ("True" if w["var"].get() else "False") if w["kind"] == "bool" else w["var"].get().strip()
                notes.append(t("obj.n_logic_edit_s").format(n=logic_edits))
            except Exception as e:
                self._toast(t("common.recompile_failed_e").format(e=e), err=True); return
        # 2) DISPLAY TEXT: Text / FoldedText -> the selected language's localization.dic
        lang = self._lang()
        lp = self._loc_path(lang)
        text_edits = 0
        if lp:
            try:
                blob = self.project.get_raw("loc", lp)
                if blob and blob[:3] == b"TRA":
                    def put(b, lochash, txt):
                        key = _hkey(lochash)
                        return (dicmod.set_entry(b, key, txt) if dicmod.get_entry(b, key) is not None
                                else dicmod.add_entry(b, key, txt))
                    for r in self._obj_rows:
                        if r["text_hash"] and r["text_var"].get() != r["text_orig"]:
                            blob = put(blob, r["text_hash"], r["text_var"].get()); text_edits += 1
                        if r["folded_var"] is not None and r["folded_hash"] \
                                and r["folded_var"].get() != r["folded_orig"]:
                            blob = put(blob, r["folded_hash"], r["folded_var"].get()); text_edits += 1
                    if text_edits:
                        self.project.set_raw("loc", lp, blob)
                        for r in self._obj_rows:
                            r["text_orig"] = r["text_var"].get()
                            if r["folded_var"] is not None:
                                r["folded_orig"] = r["folded_var"].get()
                        notes.append(t("obj.n_text_edit_s_lang").format(n=text_edits, lang=lang))
                elif any(r["text_var"].get() != r["text_orig"] for r in self._obj_rows):
                    notes.append(t("obj.text_no_lang_dic").format(lang=lang))
            except Exception as e:
                notes.append(t("obj.text_failed_e").format(e=e))
        if not notes:
            self._toast(t("obj.no_changes_apply")); return
        self._toast(t("common.applied_notes_use_save_mod").format(notes=", ".join(notes)))
        if self.on_done:
            try:
                self.on_done()
            except Exception:
                pass

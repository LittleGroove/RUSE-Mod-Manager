"""'Author' tab — CREATE new mission logic (point-and-click), the GUI over the script_emit emitter. Compose a
"WHEN <trigger> DO <actions>" event; the emitter builds the nodes, registers reactive ones into PostInit, refuses
RCE, then recompiles into the project's 'scripts' dat (the 2.5.1 golden path). Doc 09 Phase A/C — the first
authoring surface. See docs/operation_editor/06-09.

Read-from / save-to the MOD PROJECT (decompile cached, shared with the other logic tabs). Binding-driven.
"""
import tkinter as tk
from tkinter import ttk

import ui_util
from i18n import t
from ruse_mod_engine import script_logic as SL
from ruse_mod_engine import script_emit as SE

import theme                # single source of truth for the palette; local names kept unchanged
_BG = theme.BG; _PANEL = theme.PANEL; _WIDGET = theme.WIDGET; _TEXT = theme.TEXT; _DIM = theme.DIM
_GOLD = theme.GOLD; _GOLD_BRT = theme.GOLD_BRT; _OK = "#7fd17f"
_F = theme.F; _FB = theme.FB; _FS = theme.FS

_TRIGGERS = ("After a delay", "When the mission timer ends")
_ACTIONS = ("Give money to camp", "Set camp score", "Declare VICTORY", "Declare DEFEAT")


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


class AuthorFrame(tk.Frame):
    def __init__(self, parent, project, **_kw):
        super().__init__(parent, background=_BG)
        self.project = project
        self.binding = None
        self._ep = None
        self._xyz = None
        self._src = None
        self._camps = []      # [(ir, label)]
        self._timers = []     # [(ir, secs)]
        self._action_rows = []
        self._build()

    def _effetmap_path(self):
        b = self.binding
        want = ("\\map\\%s\\scripting%s\\effetmap.xyz" % (b.map_dir, _script_suffix(b.scenario_name))).lower()
        return next((v for v in _ProjDat(self.project, "scripts").list()
                     if v.lower().replace("/", "\\").endswith(want)), None)

    # ---- layout ----
    def _build(self):
        head = tk.Frame(self, background=_PANEL); head.pack(side="top", fill="x")
        self._hdr = tk.Label(head, text=t("common.follows_map_scenario_selection_above"),
                             background=_PANEL, foreground=_GOLD_BRT, font=_FB, anchor="w")
        self._hdr.pack(side="left", padx=8, pady=6)
        self._add_btn = tk.Button(head, text=t("author.add_event_mission"), command=self._add_event,
                                  background="#122030", foreground=_GOLD_BRT, font=_FB, relief="flat")
        self._add_btn.pack(side="right", padx=8)

        body = tk.Frame(self, background=_BG); body.pack(side="top", fill="both", expand=True, padx=10, pady=8)
        # WHEN
        tk.Label(body, text=t("author.when_trigger"), background=_BG, foreground=_GOLD, font=_FB).pack(anchor="w")
        trow = tk.Frame(body, background=_BG); trow.pack(fill="x", pady=(0, 8))
        self._trig_v = tk.StringVar(value=_TRIGGERS[0])
        tcb = ttk.Combobox(trow, state="readonly", width=24, font=_F, textvariable=self._trig_v, values=_TRIGGERS)
        tcb.pack(side="left"); tcb.bind("<<ComboboxSelected>>", lambda e: self._sync_trigger())
        self._trig_param_lbl = tk.Label(trow, text=t("author.delay_m_ss"), background=_BG, foreground=_GOLD, font=_F)
        self._trig_param_lbl.pack(side="left", padx=(10, 2))
        self._trig_param = tk.StringVar(value="1:00")
        self._trig_param_e = tk.Entry(trow, textvariable=self._trig_param, background=_WIDGET, foreground=_TEXT,
                                      insertbackground=_TEXT, font=_F, width=10)
        self._trig_param_e.pack(side="left")
        self._trig_note = tk.Label(trow, text="", background=_BG, foreground=_DIM, font=_FS)
        self._trig_note.pack(side="left", padx=(8, 0))

        # DO
        tk.Label(body, text=t("author.do_actions"), background=_BG, foreground=_GOLD, font=_FB).pack(anchor="w")
        self._actions_holder = tk.Frame(body, background=_BG); self._actions_holder.pack(fill="x")
        tk.Button(body, text=t("author.add_action"), command=self._add_action_row, background="#122030",
                  foreground=_TEXT, font=_FS, relief="flat").pack(anchor="w", pady=(2, 8))

        tk.Label(body, text=t("author.events_added_session"), background=_BG, foreground=_GOLD,
                 font=_FB).pack(anchor="w")
        logholder = tk.Frame(body, background=_BG); logholder.pack(fill="both", expand=True)
        self._log = tk.Text(logholder, background=_WIDGET, foreground=_OK, font=_FS, height=6, wrap="word",
                            relief="flat")
        ui_util.with_scrollbars(logholder, self._log, hbar=False, vbar=True)
        self._log.configure(state="disabled")

        self._status = tk.Label(self, text=t("common.select_map_scenario_above"), background=_BG,
                                foreground=_DIM, font=_F, anchor="w")
        self._status.pack(side="bottom", fill="x", padx=10, pady=4)

    def _toast(self, msg, err=False, ok=False):
        try:
            self._status.config(text=msg, foreground=("#e06a6a" if err else _OK if ok else _DIM))
            self.update_idletasks()
        except Exception:
            pass

    def _logline(self, msg):
        self._log.configure(state="normal"); self._log.insert("end", "• " + msg + "\n")
        self._log.see("end"); self._log.configure(state="disabled")

    # ---- action rows ----
    def _camp_values(self):
        return [lbl for _, lbl in self._camps] or [t("author.no_camps")]

    def _add_action_row(self):
        row = tk.Frame(self._actions_holder, background=_BG); row.pack(fill="x", pady=1)
        tv = tk.StringVar(value=_ACTIONS[0])
        ttk.Combobox(row, state="readonly", width=18, font=_F, textvariable=tv,
                     values=_ACTIONS).pack(side="left")
        cv = tk.StringVar(value=(self._camp_values()[0]))
        cb = ttk.Combobox(row, state="readonly", width=22, font=_FS, textvariable=cv, values=self._camp_values())
        cb.pack(side="left", padx=4)
        vv = tk.StringVar(value="1000")
        ve = tk.Entry(row, textvariable=vv, background=_WIDGET, foreground=_TEXT, insertbackground=_TEXT,
                      font=_F, width=8)
        ve.pack(side="left")
        rec = {"frame": row, "type": tv, "camp": cv, "value": vv}
        tk.Button(row, text="✕", command=lambda: self._del_action_row(rec), background=_BG,
                  foreground=_DIM, font=_FS, relief="flat").pack(side="left", padx=4)
        self._action_rows.append(rec)

    def _del_action_row(self, rec):
        try:
            rec["frame"].destroy(); self._action_rows.remove(rec)
        except Exception:
            pass

    def _sync_trigger(self):
        if self._trig_v.get() == _TRIGGERS[0]:   # delay
            self._trig_param_lbl.config(text=t("author.delay_m_ss")); self._trig_param_e.config(state="normal")
            self._trig_note.config(text="")
        else:                                     # timer ends
            self._trig_param_e.config(state="disabled")
            self._trig_param_lbl.config(text=t("author.uses"))
            self._trig_note.config(text=(t("author.mission_timer_mmss").format(mmss=SL.secs_to_mmss(self._timers[0][1]))
                                         if self._timers else t("author.scenario_has_no_mission_timer_2")))

    # ---- binding ----
    def set_binding(self, binding):
        self.binding = binding
        for r in list(self._action_rows):
            self._del_action_row(r)
        if binding is None or getattr(binding, "kind", "") not in ("operation", "campaign"):
            self._hdr.config(text=t("author.select_operation_scenario"))
            self._ep = self._src = None
            self._toast(t("author.author_works_operation_campaign_scenario")); return
        self._hdr.config(text=t("author.map_scn").format(map=binding.map_dir, scn=binding.scenario_name))
        if not SE and not SL.have_compiler():
            self._toast(t("author.python_2_5_1_recompiler"), err=True); return
        ep = self._effetmap_path()
        if not ep:
            self._ep = self._src = None
            self._toast(t("author.no_mission_script_scenario")); return
        self._toast(t("common.reading_mission_script"))
        try:
            self._xyz = self.project.get_raw("scripts", ep)
            self._src = SL.decompile_xyz(self._xyz)
            self._ep = ep
        except Exception as e:
            self._ep = self._src = None
            self._toast(t("common.could_not_read_mission_script").format(e=e), err=True); return
        m = SE.ScriptModel(self._src)
        self._camps = [(ir, "%s (%s/%s)" % (ir, kw.get("NiveauIA", "?"), kw.get("Nationalite", "?")))
                       for ir, kw in m.find_camps()]
        self._timers = m.find_timers()
        self._add_action_row()
        self._sync_trigger()
        self._toast(t("author.compose_when_do_event_then"))

    # ---- the build ----
    def _add_event(self):
        if not self._src or not self._ep:
            self._toast(t("common.select_operation_scenario_first"), err=True); return
        try:
            m = SE.ScriptModel(self._src)
            action_irs = []; summary = []
            for r in self._action_rows:
                typ = r["type"].get()
                camp = r["camp"].get().split(" ")[0] if r["camp"].get().startswith("IR_") else None
                val = r["value"].get().strip()
                if typ == "Give money to camp":
                    action_irs.append(m.donne_ressource(camp, int(val))); summary.append("give %s $%s" % (camp, val))
                elif typ == "Set camp score":
                    action_irs.append(m.set_score(camp, int(val))); summary.append("set %s score %s" % (camp, val))
                elif typ == "Declare VICTORY":
                    action_irs.append(m.declenche_victoire_challenge()); summary.append("VICTORY")
                elif typ == "Declare DEFEAT":
                    action_irs.append(m.declenche_defaite()); summary.append("DEFEAT")
            if not action_irs:
                self._toast(t("author.add_least_one_action"), err=True); return
            trig = self._trig_v.get()
            if trig == _TRIGGERS[0]:
                secs = SL.mmss_to_secs(self._trig_param.get())
                m.add_timed_branch(secs, action_irs); when = "after %s" % SL.secs_to_mmss(secs)
            else:
                if not self._timers:
                    self._toast(t("author.scenario_has_no_mission_timer"), err=True); return
                cond = m.condition_timer(m.operator("Equal", 0), self._timers[0][0])
                m.add_event(cond, action_irs); when = "when timer ends"
        except ValueError:
            self._toast(t("author.check_numeric_fields_delay_amounts"), err=True); return
        except Exception as e:
            self._toast(t("author.build_failed_e").format(e=e), err=True); return
        issues = m.validate()
        if issues:
            self._toast(t("author.blocked_i").format(i=issues[0]), err=True); return
        self._toast(t("author.recompiling_python_2_5_1"))
        try:
            new_xyz = SL.recompile_source_to_xyz(m.emit(), xyz_for_meta=self._xyz)
            self.project.set_raw("scripts", self._ep, new_xyz)
            self.project.mark_dirty("scripts", self._ep)
        except Exception as e:
            self._toast(t("common.recompile_failed_e").format(e=e), err=True); return
        self._src = m.emit(); self._xyz = new_xyz   # stack further events on the updated script
        self._logline("WHEN %s  →  DO %s" % (when, ", ".join(summary)))
        self._toast(t("author.event_added_recompiled_use_save"), ok=True)

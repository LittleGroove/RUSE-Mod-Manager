"""'Names & Descriptions' tab — curate the player-facing TEXT for the currently-selected Map + Scenario,
editing the MOD PROJECT's dats (read from the clean backup / prior edits, staged via set_raw, persisted by
the project's "Save to mod"). No standalone export — saving is the project's job.

Binding-driven (follows the top Map/Scenario dropdowns via set_binding), mirroring operation_editor's schema:
  - menu/selection NAME (every kind): TChallengeMapInfo.Description LocHash -> flash_txt.dic (apply_loc_strings)
  - operation/campaign: + Description/briefing + each objective's text (effetmap LocHashes -> localization.dic)
"""
import struct
import tkinter as tk
from tkinter import ttk

import ui_util
from i18n import t
from ruse_mod_engine import scenario_gen
from ruse_mod_engine import dic as dicmod
from ruse_mod_engine import scenario_registry as SR
from ruse_mod_engine.ndfbin import T
from ruse_mod_engine.script_edit import Script

import theme                # single source of truth for the palette; local names kept unchanged
_BG = theme.BG; _PANEL = theme.PANEL; _WIDGET = theme.WIDGET; _TEXT = theme.TEXT; _DIM = theme.DIM
_GOLD = theme.GOLD; _GOLD_BRT = theme.GOLD_BRT
_F = theme.F; _FB = theme.FB


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


def _script_suffix(scenario_name):
    for pre in ("leveldesign", "scenario"):
        if scenario_name and scenario_name.lower().startswith(pre):
            return scenario_name[len(pre):]
    return ""


def _balanced(s, i):
    """(start, end) of the balanced (...) at/after index i."""
    j = s.index("(", i); st = j; d = 0
    while j < len(s):
        if s[j] == "(":
            d += 1
        elif s[j] == ")":
            d -= 1
            if d == 0:
                return st, j
        j += 1
    return st, len(s)


# enclosing descriptor type -> friendly category (from survey_all_operations: ops are all DrawLabel;
# campaigns add PlayDialogSimplifie=dialogue, CutsceneDialog=cutscene).
_CAT = {"DrawLabel": "Objective / label", "PlayDialogSimplifie": "Dialogue", "CutsceneDialog": "Cutscene"}
_CAT_ORDER = ["Objective", "Objective / label", "Dialogue", "Cutscene"]


class NamesDescriptionsFrame(tk.Frame):
    def __init__(self, parent, project, on_done=None, **_kw):
        super().__init__(parent, background=_BG)
        self.project = project
        self.on_done = on_done
        self.binding = None
        self._obj_rows = []      # [(text_key_hex, StringVar)]
        self._build()

    # ---- project data access (mirrors operation_editor) ----
    def _glob_ndf(self):
        return self.project.get_ndf("gameplay", scenario_gen.GLOBALS_PATH)

    def _info_inst(self, g):
        i = getattr(self.binding, "info_idx", None)
        return g.instances[i] if (g is not None and i is not None and i < len(g.instances)) else None

    def _lang(self):
        return self._lang_v.get() if hasattr(self, "_lang_v") else "us"

    def _flash_path(self):
        """The selected language's flash_txt.dic (name/description text)."""
        return scenario_gen.preferred_flash_dic(_ProjDat(self.project, "loc"), self._lang())

    def _flash_blob(self):
        vp = self._flash_path()
        return _ProjDat(self.project, "loc").get(vp) if vp else None

    def _prop_key(self, g, inst, prop):
        """The 8-byte LocHash a registration prop points at (or None)."""
        v = SR._get_prop(g, inst, prop)
        return bytes(v.raw) if (v is not None and v.type_id == T.LocHash) else None

    def _effetmap_path(self):
        b = self.binding
        suf = _script_suffix(b.scenario_name)
        zz = _ProjDat(self.project, "scripts")
        want = ("\\map\\%s\\scripting%s\\effetmap.xyz" % (b.map_dir, suf)).lower()
        return next((v for v in zz.list() if v.lower().replace("/", "\\").endswith(want)), None)

    def _loc_path(self):
        """The selected language's localization.dic for this scenario (objective text)."""
        b = self.binding
        suf = _script_suffix(b.scenario_name)
        want = ("\\map\\%s\\scripting%s\\localization.dic" % (b.map_dir, suf)).lower()
        lang_seg = ("\\translations\\%s\\" % self._lang()).lower()
        zz = _ProjDat(self.project, "loc")
        cands = [v for v in zz.list() if v.lower().replace("/", "\\").endswith(want)]
        return next((v for v in cands if lang_seg in v.lower().replace("/", "\\")), (cands[0] if cands else None))

    # ---- layout ----
    def _text_field(self, label, height):
        """A labelled, scrollable multi-line text box packed into the op-block (via ui_util)."""
        tk.Label(self._opblock, text=label, background=_BG, foreground=_GOLD, font=_FB).pack(anchor="w")
        holder = tk.Frame(self._opblock, background=_BG); holder.pack(anchor="w", fill="x", pady=(0, 8))
        txt = tk.Text(holder, height=height, background=_WIDGET, foreground=_TEXT,
                      insertbackground=_TEXT, font=_F, wrap="word")
        ui_util.with_scrollbars(holder, txt, hbar=False, vbar=True)
        return txt

    def _build(self):
        head = tk.Frame(self, background=_PANEL); head.pack(side="top", fill="x")
        self._hdr = tk.Label(head, text=t("common.follows_map_scenario_selection_above"),
                             background=_PANEL, foreground=_GOLD_BRT, font=_FB, anchor="w")
        self._hdr.pack(side="left", padx=8, pady=6)
        tk.Button(head, text=t("common.apply_project"), command=self._apply, background="#122030",
                  foreground=_GOLD_BRT, font=_FB, relief="flat").pack(side="right", padx=8)
        # language selector — edit text for ANY language, independent of the editor's UI language
        self._lang_v = tk.StringVar(value="us")
        cb = ttk.Combobox(head, state="readonly", width=6, font=_F, textvariable=self._lang_v,
                          values=[c for c, _ in dicmod.LANGUAGES])
        cb.pack(side="right", padx=(2, 6))
        cb.bind("<<ComboboxSelected>>", lambda e: self.set_binding(self.binding))
        tk.Label(head, text=t("names.language"), background=_PANEL, foreground=_GOLD,
                 font=_FB).pack(side="right", padx=(8, 2))

        self._body = tk.Frame(self, background=_BG); self._body.pack(side="top", fill="both", expand=True, padx=10, pady=8)
        tk.Label(self._body, text=t("names.menu_selection_name"), background=_BG, foreground=_GOLD, font=_FB).pack(anchor="w")
        self._name_v = tk.StringVar(value="")
        tk.Entry(self._body, textvariable=self._name_v, background=_WIDGET, foreground=_TEXT,
                 insertbackground=_TEXT, font=_F).pack(anchor="w", fill="x", pady=(0, 8))

        self._opblock = tk.Frame(self._body, background=_BG)
        # description has two parts: a short TITLE LINE then the (possibly long) body
        tk.Label(self._opblock, text=t("names.description_title_line"), background=_BG,
                 foreground=_GOLD, font=_FB).pack(anchor="w")
        self._title_v = tk.StringVar(value="")
        tk.Entry(self._opblock, textvariable=self._title_v, background=_WIDGET, foreground=_TEXT,
                 insertbackground=_TEXT, font=_F).pack(anchor="w", fill="x", pady=(0, 8))
        self._desc = self._text_field(t("names.description_body"), height=4)
        self._intel = self._text_field(t("names.intelligence_report"), height=4)
        self._victory = self._text_field(t("names.victory_condition"), height=2)
        # objectives — make_scrollable stretches inner to full width so the entries fill the row
        tk.Label(self._opblock, text=t("names.mission_text_grouped_type_objectives"),
                 background=_BG, foreground=_GOLD, font=_FB).pack(anchor="w")
        objholder = tk.Frame(self._opblock, background=_BG); objholder.pack(fill="both", expand=True)
        self._objframe = ui_util.make_scrollable(objholder, bg=_BG)

        self._status = tk.Label(self, text=t("common.select_map_scenario_above"),
                                background=_BG, foreground=_DIM, font=_F, anchor="w")
        self._status.pack(side="bottom", fill="x", padx=10, pady=4)

    def _toast(self, msg, err=False):
        try:
            self._status.config(text=msg, foreground=("#e06a6a" if err else _DIM))
        except Exception:
            pass

    # ---- binding-driven load ----
    def set_binding(self, binding):
        self.binding = binding
        self._obj_rows = []
        for w in self._objframe.winfo_children():
            w.destroy()
        self._name_v.set("")
        self._title_v.set("")
        for w in (self._desc, self._intel, self._victory):
            w.delete("1.0", "end")
        if binding is None:
            self._hdr.config(text=t("common.no_scenario_selected")); self._opblock.pack_forget()
            self._toast(t("common.select_map_scenario_above")); return
        kind = getattr(binding, "kind", "")
        self._hdr.config(text=t("common.map_scn_kind").format(
            map=binding.map_dir, scn=binding.scenario_name, kind=kind))

        # NAME (every kind) from the registration instance + flash_txt
        try:
            g = self._glob_ndf(); inst = self._info_inst(g); blob = self._flash_blob()
            if inst is not None and blob is not None:
                def res(p):
                    vv = SR._get_prop(g, inst, p)
                    return (dicmod.get_entry(blob, bytes(vv.raw)) or "") if (vv is not None and vv.type_id == T.LocHash) else ""
                self._name_v.set(res("Description"))         # operation name
                self._title_v.set(res("LongDescription1"))   # description title line
                self._desc.insert("1.0", res("LongDescription2"))     # description body (long)
                self._intel.insert("1.0", res("LongDescription3"))    # Intelligence Report
                self._victory.insert("1.0", res("LongDescription4"))  # Victory Condition
        except Exception:
            pass

        if kind in ("operation", "campaign"):
            self._opblock.pack(fill="both", expand=True)
            self._load_objectives()
        else:
            self._opblock.pack_forget()
            self._toast(t("names.multiplayer_map_scenario_only_menu"))

    def _load_objectives(self):
        try:
            ep = self._effetmap_path()
            if not ep:
                self._toast(t("common.no_mission_script_found_scenario")); return
            src = Script.load(self.project.get_raw("scripts", ep)).decompile()
        except Exception as e:
            self._toast(t("common.could_not_read_mission_script").format(e=e), err=True); return
        import re
        from collections import Counter
        # CATEGORIZE each text field by its enclosing descriptor (+ GereObjectif containment = Objective).
        gere = [_balanced(src, m.start()) for m in re.finditer(r"DescriptorGereObjectif\w*\(", src)]
        descr = [(m.start(), m.group(1)) for m in re.finditer(r"Descriptor(\w+)", src)]

        def enclosing(pos):
            nm = None
            for p, n in descr:
                if p <= pos:
                    nm = n
                else:
                    break
            return nm

        def category(pos, name):
            if any(a <= pos <= b for a, b in gere):
                return "Objective"
            return _CAT.get(name, name or "Other")
        items = []; seen = set()
        for m in re.finditer(r"(?:Text|FoldedText|Message|Texte)=([0-9]+)L", src):
            hk = struct.pack("<Q", int(m.group(1))).hex()
            if hk in seen:
                continue
            seen.add(hk)
            items.append((hk, category(m.start(), enclosing(m.start()))))
        # current text from the SELECTED language's localization.dic
        cur = {}
        lp = self._loc_path()
        if lp:
            b = self.project.get_raw("loc", lp)
            if b and b[:3] == b"TRA":
                for hk, _ in items:
                    cur[hk] = dicmod.get_entry(b, bytes.fromhex(hk)) or ""
        shown = [(hk, cat) for hk, cat in items if cur.get(hk, "").strip()]
        n_empty = len(items) - len(shown)
        shown.sort(key=lambda x: (_CAT_ORDER.index(x[1]) if x[1] in _CAT_ORDER else len(_CAT_ORDER)))
        # render grouped by category, with a header per group
        cur_cat = None; idx = 0
        for hk, cat in shown:
            if cat != cur_cat:
                cur_cat = cat
                tk.Label(self._objframe, text=cat, background=_BG, foreground=_GOLD_BRT,
                         font=_FB, anchor="w").pack(fill="x", pady=(6, 1))
            idx += 1
            row = tk.Frame(self._objframe, background=_BG); row.pack(fill="x", pady=1)
            tk.Label(row, text="%2d" % idx, background=_BG, foreground=_DIM,
                     font=_F, width=4, anchor="w").pack(side="left")
            var = tk.StringVar(value=cur.get(hk, ""))
            tk.Entry(row, textvariable=var, background=_WIDGET, foreground=_TEXT,
                     insertbackground=_TEXT, font=_F).pack(side="left", fill="x", expand=True, padx=4)
            self._obj_rows.append((hk, var))
        bycat = Counter(c for _, c in shown)
        msg = t("names.loaded_n_text_entries_lang").format(
            n=len(self._obj_rows), lang=self._lang(),
            b=", ".join("%s×%d" % (c, n) for c, n in bycat.most_common()))
        if n_empty:
            msg += " " + t("names.k_unused").format(k=n_empty)
        self._toast(msg)

    # ---- apply (stage into the project; the project's Save persists) ----
    def _apply(self):
        b = self.binding
        if b is None:
            self._toast(t("names.select_scenario_first"), err=True); return
        notes = []
        lang = self._lang()

        def _put(blob, key, txt):
            return (dicmod.set_entry(blob, key, txt) if dicmod.get_entry(blob, key) is not None
                    else dicmod.add_entry(blob, key, txt))

        # NAME + description -> the SELECTED language's flash_txt, under each registration prop's LocHash.
        # Props that already have a LocHash are edited per-language; brand-new fields allocate (all langs).
        try:
            g = self._glob_ndf(); inst = self._info_inst(g)
            fields = [("Description", self._name_v.get().strip())]
            if b.kind in ("operation", "campaign"):
                fields += [("LongDescription1", self._title_v.get().strip()),
                           ("LongDescription2", self._desc.get("1.0", "end").strip()),
                           ("LongDescription3", self._intel.get("1.0", "end").strip()),
                           ("LongDescription4", self._victory.get("1.0", "end").strip())]
            if inst is not None:
                fp = self._flash_path()
                fblob = self.project.get_raw("loc", fp)
                to_alloc = []
                changed = False
                for prop, val in fields:
                    if not val:
                        continue
                    key = self._prop_key(g, inst, prop)
                    if key is not None and fblob and fblob[:3] == b"TRA":
                        fblob = _put(fblob, key, val); changed = True
                    elif key is None:
                        to_alloc.append((prop, val))
                if changed:
                    self.project.set_raw("loc", fp, fblob)
                if to_alloc:   # new fields with no LocHash yet → allocate across all langs + set the prop
                    keys, blobs = scenario_gen.apply_loc_strings(_ProjDat(self.project, "loc"), to_alloc)
                    for vp, data in blobs.items():
                        self.project.set_raw("loc", vp, data)
                    for prop, key in keys.items():
                        scenario_gen._set_raw(g, inst, prop, bytes(key))
                    self.project.mark_dirty("gameplay", scenario_gen.GLOBALS_PATH)
                notes.append(t("names.name_text_lang").format(lang=lang))
        except Exception as e:
            notes.append(t("names.name_failed_e").format(e=e))

        # OBJECTIVE text -> the SELECTED language's localization.dic
        if self._obj_rows:
            try:
                lp = self._loc_path()
                blob = self.project.get_raw("loc", lp) if lp else None
                if blob and blob[:3] == b"TRA":
                    for hk, var in self._obj_rows:
                        if var.get().strip():
                            blob = _put(blob, bytes.fromhex(hk), var.get())
                    self.project.set_raw("loc", lp, blob)
                    notes.append(t("names.objectives_lang").format(lang=lang))
                else:
                    notes.append(t("names.objectives_no_lang_dic").format(lang=lang))
            except Exception as e:
                notes.append(t("names.objectives_failed_e").format(e=e))
        self._toast(t("common.applied_notes_use_save_mod").format(notes=", ".join(notes)))
        if self.on_done:
            try:
                self.on_done()
            except Exception:
                pass

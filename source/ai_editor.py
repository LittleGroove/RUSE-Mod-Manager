"""
RUSE AI editor — tune the (C++) skirmish AI via its NDF data in everything.cpp.

Opened from the Mod Editor hub with a live ModProject; edits the shared everything.cpp NDF so changes
accumulate and the window's Save writes them to the mod's .dat.

Tabs:
  AI Profiles        — the 10 TIAProfil (Default + 3 difficulties + 7 personalities), grouped behaviour fields.
  Difficulty Handicap— TAISpecificBonus (the AI cheat bonus, scoped by difficulty/profile).
  Ruse Cards         — TBluffCardDescriptor (deception card durations / availability).

RE: difficulties 0=Easy/1=Medium/2=Hard; profiles 0=Regular/1=Air Force/2=Howitzer/3=Prototype/
4=Blitzkrieg/5=Turtle/6=Random (see docs/modding/ai.md). The C++ engine reads these directly.
"""
import io
import os
import re
import sys
import zlib
import tkinter as tk
from tkinter import ttk, messagebox

_PY25_MAGIC = 62131          # Python 2.5 marshal magic (the .xyz are 2.5 bytecode)
_PY25_VER = (2, 5, 4)

REPO = os.path.dirname(os.path.abspath(__file__))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
from ruse_mod_engine import edata as edata_mod  # noqa: E402
from ruse_mod_engine import mod_project as mp_mod  # noqa: E402
from ruse_mod_engine import ndfbin as ndfbin_mod  # noqa: E402
from i18n import t  # noqa: E402
import ui_util  # noqa: E402  — zebra-striping (issue #5.2)

_R_BG, _R_BG_PANEL, _R_BG_WIDGET = "#08101c", "#0e1a2a", "#060d18"
_R_GOLD, _R_GOLD_BRT, _R_TEXT, _R_TEXT_DIM, _R_SEL_BG = "#c8a020", "#e0c030", "#ccd8e8", "#3e5878", "#1a3060"
_F_MAIN = ("Courier New", 9)
_F_BOLD = ("Courier New", 9, "bold")
_F_HEAD = ("Courier New", 10, "bold")

_DIFF_NAMES = [t("Easy"), t("Medium"), t("Hard")]
_PROFILE_NAMES = [t("Regular"), t("Air Force"), t("Howitzer"), t("Prototype"), t("Blitzkrieg"), t("Turtle"), t("Random")]

# TIAProfil fields grouped for the UI. kind inferred from the value (scalar/bool); all are numbers.
_PROFILE_GROUPS = [
    (t("Attack / offense"), [
        (t("Attack activation time (s)"), "AttaqueTempsActivation"),
        (t("Max offensive missions (-1=inf)"), "OffensiveNbMissionMax"),
        (t("Attack launch factor"), "MissionFacteurLancementAttaque"),
        (t("Commit (enough-to-destroy) factor"), "MissionFacteurEnoughToDestroy"),
    ]),
    (t("Defense"), [
        (t("Threat distance"), "DefenseDistanceMenace"),
        (t("Urgent distance"), "DefenseDistanceUrgent"),
        (t("Max units per position"), "DefenseMaxUnitOnPosition"),
        (t("Max defense missions"), "DefenseNbMissionMax"),
    ]),
    (t("Harassment"), [
        (t("Harassment active (0/1)"), "HarcelementActif"),
        (t("Upgrade for harassment (0/1)"), "UpgradeActifPourHarcelement"),
    ]),
    (t("Economy"), [
        (t("Cash-reserve activation (s)"), "CashReserveTempsActivation"),
        (t("% money reserved for admin"), "PercentMoneyToReserveForBatimentAdmin"),
        (t("% money used for idle build"), "PercentMoneyToUseForIdle"),
        (t("Money bonus (cheat)"), "DeviseBonusIA"),
        (t("Income bonus (cheat)"), "IncomeBonusIA"),
        (t("Extra admin bldgs before deactivate"), "NbBatimentAdministratifEnPlusAvantDesactivation"),
    ]),
    (t("Logistics / depots"), [
        (t("Extra depots before deactivate"), "DepotNbEnPlusAvantDesactivation"),
        (t("Min depot value for truck factory"), "ValueDepotMinForTruckFactory"),
        (t("Min minutes left for truck factory"), "MinutesLeftMinForTruckFactory"),
        (t("Min depots for truck factory"), "NbDepotMinForTruckFactory"),
        (t("Units = dangerous depot"), "NbUnitsDangerousDepot"),
        (t("Depot group distance"), "SeuilDistanceGroupeDepot"),
        (t("Truck factory distance"), "SeuilDistanceTruckFactory"),
    ]),
    (t("Production (idle build counts)"), [
        (t("Max production vs enemy unit"), "NbMaxProductionForEnnemyUnit"),
        (t("Idle infantry"), "NbProdIdleInfanterie"),
        (t("Idle tanks"), "NbProdIdleTank"),
        (t("Idle anti-tank"), "NbProdIdleAntitank"),
        (t("Idle artillery"), "NbProdIdleArti"),
        (t("Idle AA (DCA)"), "NbProdIdleDCA"),
        (t("Idle fighters"), "NbProdIdleChasseur"),
        (t("Idle bombers"), "NbProdIdleBomber"),
        (t("Idle fighter-bombers"), "NbProdIdleChasseurBomber"),
        (t("Idle can launch research (0/1)"), "ProdIdleCanLaunchResearch"),
    ]),
    (t("Unit-type weighting"), [
        (t("Aircraft weight"), "BonusUnitesAvions"),
        (t("Artillery weight"), "BonusUnitesArtillerie"),
        (t("Experimental weight"), "BonusUnitesExperimentales"),
        (t("Research weight"), "BonusUnitesRecherche"),
        (t("On-the-fly building weight"), "BonusBatimentOTF"),
    ]),
    (t("Deception (ruse cards)"), [
        (t("% open with a manip card"), "PourcentChanceUtiliserCarteManipAuDebut"),
        (t("Cards in reserve (difficulty)"), "NbCarteDansLaReservePourLaDifficulte"),
        (t("Cards in reserve (profile)"), "NbCarteDansLaReservePourLeProfil"),
    ]),
    (t("Retaliation"), [
        (t("Combatant retaliation factor"), "RepresailleFacteurUniteCombattante"),
        (t("Non-combatant retaliation factor"), "RepresailleFacteurUniteNonCombattante"),
        (t("Retaliation timeout (s)"), "TimeOutRepresailles"),
    ]),
    (t("Intel / stealth"), [
        (t("Chance to spot fakes/bluffs"), "ProbaRepereFake"),
        (t("Invisible-unit memory (s)"), "TempsMemorisationUnitInvisible"),
        (t("Camouflaged-building activation (s)"), "TempsActivationBatimentCamoufle"),
    ]),
]

_BONUS_FIELDS = [
    (t("Bonus value (the cheat amount)"), "BonusValue", "scalar"),
    (t("Consider stack as one enemy (0/1)"), "ConsiderStackAsOneEnnemy", "scalar"),
    (t("AI difficulties scope (0=Easy,1=Med,2=Hard)"), "AIDifficulties", "list"),
    (t("AI profiles scope (0..6)"), "AIProfiles", "list"),
    (t("War modes scope"), "WarModes", "list"),
    (t("Unit IDs scope"), "UnitIDs", "list"),
]

_CARD_FIELDS = [
    (t("Effect duration (LifeDuration s)"), "LifeDuration", "scalar"),
    (t("Shown in menu (0/1)"), "ShowInMenu", "scalar"),
    (t("Menu slot"), "PositionInMenu", "scalar"),
]


class AIEditorWindow(tk.Frame):
    """Embedded as a nested in-tab view (formerly a Toplevel); the Mod Editor hosts it + the Back bar."""
    def __init__(self, master, project: "mp_mod.ModProject", on_change=None):
        super().__init__(master)
        self.project = project
        self._on_change = on_change
        self.configure(background=_R_BG)
        self._ndf = None
        self._profiles = []     # [(label, inst)]
        self._bonuses = []      # [(label, inst)]
        self._cards = []        # [(label, inst)]
        self._scripts = []      # [(label, path)] from IA_Common.dat
        self._scripts_arc = None
        try:
            self._ndf = project.everything()
        except Exception as e:
            messagebox.showerror(t("AI Editor"), t("Could not load gameplay data:\n{e}", e=e), parent=self)
            self.after(10, self.destroy)
            return
        self._index()
        self._index_scripts()
        self._build_ui()

    # ── data ──────────────────────────────────────────────────────────────────

    def _pidx(self, cls_index, name):
        p = self._ndf.prop_by_name_and_class(name, cls_index) or self._ndf.prop_by_name(name)
        return p.index if p else None

    def _pv(self, inst, name):
        pi = self._pidx(inst.class_index, name)
        return inst.get(pi) if pi is not None else None

    def _follow(self, val):
        if val is None or val.type_id != ndfbin_mod.T.Reference:
            return None
        m, r = val.raw
        return self._ndf.instances[r[0]] if m == ndfbin_mod.OBJ_REF_MARKER and 0 <= r[0] < len(self._ndf.instances) else None

    def _list_items(self, inst, prop):
        v = self._pv(inst, prop)
        return list(v.raw) if v is not None and v.type_id == ndfbin_mod.T.List else []

    def _index(self):
        # profiles via TIAProfilList -> Default/Difficulty/Profil config lists
        self._profiles = []
        pls = self._ndf.class_instances("TIAProfilList")
        if pls:
            cfg_lists = []
            for pv in pls[0].props:
                if pv.value.type_id == ndfbin_mod.T.List:
                    cfg_lists = [self._follow(it) for it in pv.value.raw]
                    break
            group_label = ["Default", "Difficulty", "AI"]
            for gi, cl in enumerate(cfg_lists):
                if cl is None:
                    continue
                for oi, it in enumerate(self._list_items(cl, "Items")):
                    cfg = self._follow(it)
                    if cfg is None:
                        continue
                    prof = self._follow(self._pv(cfg, "OverridenParams"))
                    if prof is None:
                        continue
                    if gi == 1:
                        lbl = t("Difficulty: {name}", name=_DIFF_NAMES[oi] if oi < len(_DIFF_NAMES) else oi)
                    elif gi == 2:
                        lbl = t("AI: {name}", name=_PROFILE_NAMES[oi] if oi < len(_PROFILE_NAMES) else oi)
                    else:
                        lbl = t("Default")
                    self._profiles.append((lbl, prof))
        if not self._profiles:   # fallback
            self._profiles = [(t("Profile {i}", i=i), p) for i, p in enumerate(self._ndf.class_instances("TIAProfil"))]

        # difficulty handicap bonuses
        self._bonuses = []
        bls = self._ndf.class_instances("TAISpecificBonusList")
        seen = set()
        for bl in bls:
            for it in self._list_items(bl, "SpecificBonusList"):
                b = self._follow(it)
                if b is not None and id(b) not in seen:
                    seen.add(id(b))
                    self._bonuses.append((t("Bonus {n}", n=len(self._bonuses)), b))
        for i, b in enumerate(self._ndf.class_instances("TAISpecificBonus")):
            if id(b) not in seen:
                self._bonuses.append((f"Bonus {len(self._bonuses)}", b))

        # ruse cards
        self._cards = []
        for c in self._ndf.class_instances("TBluffCardDescriptor"):
            pos = self._pv(c, "PositionInMenu")
            self._cards.append((t("Card (slot {slot})", slot=pos.raw if pos else '?'), c))
        self._cards.sort(key=lambda t: (self._pv(t[1], "PositionInMenu").raw
                                        if self._pv(t[1], "PositionInMenu") else 1 << 30))

    # ── UI ──────────────────────────────────────────────────────────────────────

    def _scrollable(self, parent):
        canvas = tk.Canvas(parent, background=_R_BG_PANEL, highlightthickness=0)
        sb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, background=_R_BG_PANEL)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        canvas.bind("<Enter>", lambda e: canvas.bind_all(
            "<MouseWheel>", lambda ev: canvas.yview_scroll(int(-ev.delta / 120), "units")))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        inner._canvas = canvas   # so re-renders can preserve the scroll position
        return inner

    def _keep_scroll(self, frame, fn):
        """Run a re-render fn that rebuilds `frame`, restoring its scroll position after."""
        cv = getattr(frame, "_canvas", None)
        pos = cv.yview()[0] if cv is not None else None
        fn()
        if cv is not None and pos is not None:
            frame.update_idletasks()
            cv.yview_moveto(pos)

    def _build_ui(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=4, pady=(6, 2))
        t1, t2, t3, t4 = ttk.Frame(nb), ttk.Frame(nb), ttk.Frame(nb), ttk.Frame(nb)
        nb.add(t1, text=t("  AI Profiles  "))
        nb.add(t2, text=t("  Difficulty Handicap  "))
        nb.add(t3, text=t("  Ruse Cards  "))
        nb.add(t4, text=t("  AI Scripts  "))
        self._prof = self._build_list_tab(t1, self._profiles, self._render_profile, t("AI profile"))
        self._bon = self._build_list_tab(t2, self._bonuses, self._render_bonus, t("difficulty bonus"))
        self._card = self._build_list_tab(t3, self._cards, self._render_card, t("ruse card"))
        self._build_scripts_tab(t4)

        bottom = tk.Frame(self, background=_R_BG)
        bottom.pack(fill="x", padx=8, pady=(0, 2))
        ttk.Button(bottom, text=t("Save mod (.dat)"), command=self._save).pack(side="left")
        self._save_status = tk.Label(bottom, text="", background=_R_BG, foreground=_R_TEXT_DIM, font=_F_MAIN)
        self._save_status.pack(side="right")
        tk.Label(self, text=t("Apply commits the current selection into the project. “Save mod (.dat)” "
                              "writes ALL accumulated changes to the mod's .dat. These TIAProfil / "
                              "TAISpecificBonus values are read by the C++ skirmish AI."),
                 background=_R_BG, foreground=_R_TEXT_DIM, font=_F_MAIN, justify="left",
                 wraplength=1020).pack(fill="x", padx=8, pady=(0, 6))

    def _build_list_tab(self, parent, items, render_fn, what):
        """Generic list|editor tab. Returns a dict of its widgets/state."""
        st = {"items": items, "render": render_fn, "rows": [], "sel": None}
        body = tk.Frame(parent, background=_R_BG)
        body.pack(fill="both", expand=True, padx=4, pady=4)
        left = tk.Frame(body, background=_R_BG_PANEL)
        left.pack(side="left", fill="y")
        lb = tk.Listbox(left, width=26, activestyle="none", background=_R_BG_WIDGET, foreground=_R_TEXT,
                        selectbackground=_R_SEL_BG, selectforeground=_R_GOLD_BRT, font=_F_MAIN,
                        exportselection=False)
        ui_util.with_scrollbars(left, lb)   # horizontal scroll for long names (issue #5.4)
        for lbl, _inst in items:
            lb.insert(tk.END, lbl)
        st["lb"] = lb

        right = tk.Frame(body, background=_R_BG_PANEL)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        st["hdr"] = tk.Label(right, text=t("Select a {what}", what=what), background=_R_BG_PANEL,
                             foreground=_R_GOLD_BRT, font=_F_HEAD, anchor="w")
        st["hdr"].pack(fill="x", padx=6, pady=(4, 0))
        btn = tk.Frame(right, background=_R_BG_PANEL)
        btn.pack(side="bottom", fill="x", padx=6, pady=(0, 6))
        st["apply"] = ttk.Button(btn, text=t("Apply changes"), state="disabled",
                                 command=lambda s=st: self._apply(s))
        st["apply"].pack(side="left")
        st["status"] = tk.Label(btn, text="", background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN)
        st["status"].pack(side="right")
        holder = tk.Frame(right, background=_R_BG_PANEL)
        holder.pack(fill="both", expand=True, padx=2, pady=4)
        st["fields"] = self._scrollable(holder)
        lb.bind("<<ListboxSelect>>", lambda e, s=st: self._on_select(s))
        return st

    # ── AI scripts tab (IA_Common .xyz) ───────────────────────────────────────

    def _index_scripts(self):
        self._scripts = []
        try:
            src = self.project.read_source("scripts")   # mod-folder else backup else game IA_Common.dat
            if not os.path.isfile(src):
                return
            self._scripts_arc = edata_mod.open_dat(str(src))
        except Exception:
            self._scripts_arc = None
            return
        for p in sorted(self._scripts_arc.list()):
            if p.lower().endswith(".xyz"):
                parts = p.replace("\\", "/").split("/")
                # label: <map>/<scriptfile>  (drop the long genpython/.../test/map prefix)
                lbl = "/".join(parts[-3:]) if len(parts) >= 3 else p
                self._scripts.append((lbl, p))

    def _build_scripts_tab(self, parent):
        if not self._scripts:
            tk.Label(parent, text=t("Could not read IA_Common.dat (set the Game Root in Settings, or create "
                                    "a backup). The AI/mission scripts live there."),
                     background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN,
                     wraplength=700, justify="left").pack(padx=12, pady=12, anchor="w")
            return
        body = tk.Frame(parent, background=_R_BG)
        body.pack(fill="both", expand=True, padx=4, pady=4)
        left = tk.Frame(body, background=_R_BG_PANEL)
        left.pack(side="left", fill="y")
        lb = tk.Listbox(left, width=40, activestyle="none", background=_R_BG_WIDGET, foreground=_R_TEXT,
                        selectbackground=_R_SEL_BG, selectforeground=_R_GOLD_BRT, font=_F_MAIN,
                        exportselection=False)
        ui_util.with_scrollbars(left, lb)   # horizontal scroll for long script names (issue #5.4)
        for lbl, _p in self._scripts:
            lb.insert(tk.END, lbl)
        self._scripts_lb = lb

        right = tk.Frame(body, background=_R_BG_PANEL)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        top = tk.Frame(right, background=_R_BG_PANEL)
        top.pack(fill="x", padx=6, pady=(4, 0))
        self._scripts_hdr = tk.Label(top, text=t("Select a script to decompile"),
                                     background=_R_BG_PANEL, foreground=_R_GOLD_BRT, font=_F_HEAD, anchor="w")
        self._scripts_hdr.pack(side="left")
        ttk.Button(top, text=t("Export…"), command=self._export_script).pack(side="right")
        txtwrap = tk.Frame(right, background=_R_BG_PANEL)
        txtwrap.pack(fill="both", expand=True, padx=6, pady=4)
        self._scripts_txt = tk.Text(txtwrap, background=_R_BG_WIDGET, foreground=_R_TEXT, font=_F_MAIN,
                                    wrap="none", relief="flat", insertbackground=_R_GOLD)
        tsb = ttk.Scrollbar(txtwrap, orient="vertical", command=self._scripts_txt.yview)
        self._scripts_txt.configure(yscrollcommand=tsb.set, state="disabled")
        tsb.pack(side="right", fill="y")
        self._scripts_txt.pack(side="left", fill="both", expand=True)
        tk.Label(right, text=t("{n} AI/mission scripts (IA_Common.dat), shown as full "
                               "decompiled Python source (uncompyle6, Py2.5). Read-only for now — saving "
                               "edited script behaviour needs the Py2.5 re-compile (the .xyz container "
                               "repack is already cracked). Framework library = ZZ_Win.dat .ipk packs.",
                               n=len(self._scripts)),
                 background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN, justify="left",
                 wraplength=620).pack(fill="x", padx=6, pady=(0, 4))
        lb.bind("<<ListboxSelect>>", self._on_script_select)

    def _decompress_xyz(self, raw):
        for magic in (b"\x78\x9c", b"\x78\xda", b"\x78\x01"):
            i = raw.find(magic)
            if 0 <= i < 64:
                return zlib.decompress(raw[i:])
        raise ValueError("no zlib stream found")

    def _analyze_script(self, path, dec):
        out = [t("Path : {path}", path=path),
               t("Body : {n} bytes (Python 2.5 marshalled code)", n=len(dec)), ""]
        names, strings = set(), []
        try:
            from xdis import unmarshal
            code = unmarshal.load_code(dec, _PY25_MAGIC)

            def walk(co):
                for n in getattr(co, "co_names", []) or []:
                    names.add(str(n))
                for c in getattr(co, "co_consts", []) or []:
                    if hasattr(c, "co_consts"):
                        walk(c)
                    elif isinstance(c, str) and c.strip():
                        strings.append(c)
            walk(code)
        except Exception as e:
            out.append(t("(xdis unmarshal failed: {e} — showing raw strings)", e=e))
            strings = [m.decode("latin1") for m in re.findall(rb"[ -~]{4,}", dec)]
        dsl_pref = ("Descriptor", "Condition", "Comportement", "Contrainte", "Sequential",
                    "Simultaneous", "Competition", "IfThenElse", "Wait", "SetVariable",
                    "Incremente", "Add", "Transfert", "Niveau", "Active", "Donne", "Bloque")
        imports = sorted(n for n in names if "." in n or n in
                         ("defines", "missions", "production", "strategic_ia", "testauto",
                          "manipulation_card", "leveldesign", "leveldesignsolo", "leveldesigntest"))
        dsl = sorted(n for n in names if any(n.startswith(p) for p in dsl_pref))
        other = sorted(n for n in names if n not in set(imports) and n not in set(dsl))
        out.append(t("Imports / framework used:"))
        out += ["   " + i for i in imports] or ["   " + t("(none)")]
        out.append("")
        out.append(t("AI / DSL actions & conditions called:"))
        out += ["   " + d for d in dsl] or ["   " + t("(none)")]
        out.append("")
        out.append(t("String literals ({n}; first 80):", n=len(strings)))
        out += ["   " + repr(s) for s in strings[:80]]
        if other:
            out.append("")
            out.append(t("Other referenced names:"))
            out.append("   " + ", ".join(other[:60]))
        return "\n".join(out)

    def _selected_script(self):
        sel = self._scripts_lb.curselection()
        return self._scripts[sel[0]] if sel else None

    def _decompile_source(self, path, dec):
        """Full Python source via uncompyle6 (Py2.5); falls back to a structural analysis."""
        try:
            from xdis import unmarshal
            from uncompyle6.main import decompile
            co = unmarshal.load_code(dec, _PY25_MAGIC)
            out = io.StringIO()
            decompile(co, _PY25_VER, out, magic_int=_PY25_MAGIC)
            src = out.getvalue()
            if src and src.strip():
                return src
        except Exception as e:
            return (t("# Full decompile failed: {e}\n"
                      "# Showing structural analysis instead.\n\n", e=e)
                    + self._analyze_script(path, dec))
        return self._analyze_script(path, dec)

    def _set_script_text(self, text):
        self._scripts_txt.configure(state="normal")
        self._scripts_txt.delete("1.0", tk.END)
        self._scripts_txt.insert("1.0", text)
        self._scripts_txt.configure(state="disabled")

    def _on_script_select(self, _=None):
        s = self._selected_script()
        if not s or self._scripts_arc is None:
            return
        lbl, path = s
        self._scripts_hdr.configure(text=lbl)
        self._set_script_text(t("Decompiling…"))
        self.update_idletasks()
        try:
            dec = self._decompress_xyz(bytes(self._scripts_arc.get(path)))
            text = self._decompile_source(path, dec)
        except Exception as e:
            text = t("Could not read/decompress {path}:\n{e}", path=path, e=e)
        self._set_script_text(text)

    def _export_script(self):
        s = self._selected_script()
        if not s or self._scripts_arc is None:
            messagebox.showinfo(t("Export"), t("Select a script first."), parent=self)
            return
        lbl, path = s
        outdir = os.path.join(REPO, "test_output", "ai_scripts")
        os.makedirs(outdir, exist_ok=True)
        base = os.path.basename(path).rsplit(".", 1)[0]
        try:
            raw = bytes(self._scripts_arc.get(path))
            dec = self._decompress_xyz(raw)
            with open(os.path.join(outdir, base + ".py"), "w", encoding="utf-8") as f:
                f.write(self._decompile_source(path, dec))
            with open(os.path.join(outdir, base + ".marshal.bin"), "wb") as f:
                f.write(dec)
        except Exception as e:
            messagebox.showerror(t("Export"), str(e), parent=self)
            return
        messagebox.showinfo(t("Export"), t("Wrote decompiled .py + decompressed marshal to:\n{outdir}", outdir=outdir), parent=self)

    # ── rendering helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _fmt(val, kind):
        raw = val.raw
        if kind == "list" and isinstance(raw, list):
            return ", ".join(str(getattr(e, "raw", e)) for e in raw)
        return str(raw)

    # Three columns spanning the FULL width: a wrapping label pinned to the LEFT edge (column 0
    # stretches), the current-value preview, then the entry pinned to the RIGHT edge. The value
    # columns are wide enough to show full floats / short lists (the label column gives up the room).
    _VAL_W = 22         # chars: current-value preview column
    _ENT_W = 28         # chars: editable entry

    @staticmethod
    def _wrap_label(parent, text, fg):
        lab = tk.Label(parent, text=text, anchor="w", justify="left", background=_R_BG_PANEL,
                       foreground=fg, font=_F_MAIN)
        lab.grid(row=0, column=0, sticky="ew", padx=(2, 14))
        lab.bind("<Configure>", lambda e, L=lab: L.configure(wraplength=max(e.width - 4, 80)))
        return lab

    def _row(self, container, rows, label, prop, kind, val):
        r = tk.Frame(container, background=_R_BG_PANEL)
        r.pack(fill="x", pady=2)
        r.grid_columnconfigure(0, weight=1)
        self._wrap_label(r, label, _R_TEXT)
        cur = self._fmt(val, kind)
        tk.Label(r, text=cur[:self._VAL_W], width=self._VAL_W, anchor="e", background=_R_BG_PANEL,
                 foreground=_R_TEXT_DIM, font=_F_MAIN).grid(row=0, column=1, sticky="e", padx=(0, 12))
        var = tk.StringVar(value=cur)
        ttk.Entry(r, textvariable=var, width=self._ENT_W).grid(row=0, column=2, sticky="e", padx=(0, 4))
        rows.append((prop, kind, val, var, cur))
        ui_util.apply_row_bg(r, ui_util.row_bg(getattr(self, "_zeb", 0), _R_BG_PANEL))  # issue #5.2
        self._zeb = getattr(self, "_zeb", 0) + 1

    def _section(self, container, title):
        tk.Label(container, text=title, anchor="w", background=_R_BG_PANEL,
                 foreground=_R_GOLD_BRT, font=_F_BOLD).pack(fill="x", padx=2, pady=(8, 2))

    def _notes_section(self, container, key, hint=None):
        """A modder's 'Notes' box (issue #5.6) for the AI item identified by `key`, auto-saving into
        the project's notes.json on focus-out / panel rebuild."""
        ui_util.notes_section(
            container, lambda text, k=key: self.project.set_note(k, text),
            panel_bg=_R_BG_PANEL, widget_bg=_R_BG_WIDGET, text_fg=_R_TEXT, dim_fg=_R_TEXT_DIM,
            gold=_R_GOLD, font=_F_MAIN, font_bold=_F_BOLD,
            initial=self.project.get_note(key), label=t("Notes"), hint=hint)

    def _on_select(self, st):
        sel = st["lb"].curselection()
        if not sel:
            return
        st["sel"] = st["items"][sel[0]][1]
        for w in st["fields"].winfo_children():
            w.destroy()
        st["rows"] = []
        self._zeb = 0   # zebra-stripe counter for this render (issue #5.2)
        st["render"](st, st["sel"])
        st["apply"].configure(state="normal")
        st["status"].configure(text="")

    def _render_profile(self, st, inst):
        st["hdr"].configure(text=next((l for l, i in self._profiles if i is inst), t("AI profile")))
        shown = set()
        for gtitle, fields in _PROFILE_GROUPS:
            present = [(lbl, prop) for lbl, prop in fields if self._pv(inst, prop) is not None]
            if not present:
                continue
            self._section(st["fields"], gtitle)
            for lbl, prop in present:
                val = self._pv(inst, prop)
                self._row(st["fields"], st["rows"], lbl, prop, "scalar", val)
                shown.add(prop)
        # any other numeric field not grouped
        extra = [pv for pv in inst.props
                 if self._ndf.properties[pv.prop_index].name not in shown
                 and pv.value.type_id in _NUM_TIDS]
        if extra:
            self._section(st["fields"], t("Other"))
            for pv in extra:
                nm = self._ndf.properties[pv.prop_index].name
                self._row(st["fields"], st["rows"], nm, nm, "scalar", pv.value)
        label = next((l for l, i in self._profiles if i is inst), "")
        self._notes_section(st["fields"], "ai_profile:" + label,
                            hint=t("Private notes for this AI profile — saved with the mod project."))

    def _render_bonus(self, st, inst):
        st["hdr"].configure(text=t("AI difficulty handicap (cheat bonus)"))
        for lbl, prop, kind in _BONUS_FIELDS:
            val = self._pv(inst, prop)
            if val is not None:
                self._row(st["fields"], st["rows"], lbl, prop, kind, val)
        tk.Label(st["fields"], text=t("Scope lists target which difficulties/profiles get the bonus. "
                                      "Difficulties: 0=Easy,1=Medium,2=Hard. Profiles: 0=Regular,1=Air "
                                      "Force,2=Howitzer,3=Prototype,4=Blitzkrieg,5=Turtle,6=Random."),
                 background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN, justify="left",
                 wraplength=560).pack(anchor="w", padx=2, pady=(8, 0))
        label = next((l for l, i in self._bonuses if i is inst), "")
        self._notes_section(st["fields"], "ai_bonus:" + label,
                            hint=t("Private notes for this difficulty bonus — saved with the mod project."))

    def _render_card(self, st, inst):
        st["hdr"].configure(text=next((l for l, i in self._cards if i is inst), t("Ruse card")))
        for lbl, prop, kind in _CARD_FIELDS:
            val = self._pv(inst, prop)
            if val is not None:
                self._row(st["fields"], st["rows"], lbl, prop, kind, val)
        label = next((l for l, i in self._cards if i is inst), "")
        self._notes_section(st["fields"], "ai_card:" + label,
                            hint=t("Private notes for this ruse card — saved with the mod project."))

    # ── apply / save ─────────────────────────────────────────────────────────────

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

    def _apply(self, st):
        if st["sel"] is None:
            return
        changed, errors = 0, []
        for prop, kind, val, var, orig in st["rows"]:
            new = var.get().strip()
            if new == orig:
                continue
            try:
                if kind == "list":
                    elems = val.raw
                    if not isinstance(elems, list):
                        continue
                    parts = [p for p in (s.strip() for s in new.replace(";", ",").split(",")) if p]
                    if len(parts) == 1 and elems:
                        v0 = int(float(parts[0]))
                        for e in elems:
                            e.raw = v0
                    else:
                        for e, p in zip(elems, parts):
                            e.raw = int(float(p))
                    changed += 1
                else:
                    val.raw = self._coerce(val.raw, new)
                    changed += 1
            except Exception as e:
                errors.append(f"{prop}: {e}")
        if errors:
            messagebox.showerror(t("Invalid value(s)"), "\n".join(errors), parent=self)
        if changed:
            self.project.mark_dirty("gameplay", mp_mod.EVERYTHING_PATH)
            self._keep_scroll(st["fields"], lambda: self._on_select(st))  # show set values
            if self._on_change:
                try:
                    self._on_change()
                except Exception:
                    pass
        st["status"].configure(text=(t("Applied {n} change(s)", n=changed) if changed else t("No changes")))

    def _save(self):
        # Commit any pending (typed-but-not-applied) edits across all tabs first, so what's saved
        # == what's shown.
        for st in (self._prof, self._bon, self._card):
            if st.get("sel") is not None and st.get("rows"):
                self._apply(st)
        if not self.project.is_dirty():
            messagebox.showinfo(t("Save mod"), t("No pending changes to save."), parent=self)
            return
        try:
            written = self.project.save_all()
        except Exception as e:
            messagebox.showerror(t("Save failed"),
                                 t("{e}\n\nTip: set the Game Root in Settings.", e=e), parent=self)
            return
        if self._on_change:
            try:
                self._on_change()
            except Exception:
                pass
        self._save_status.configure(text=t("Saved all changes to the mod"))
        messagebox.showinfo(t("Saved"), t("Saved mod changes to:\n\n{paths}", paths="\n".join(written)), parent=self)


_NUM_TIDS = {ndfbin_mod.T.Bool, ndfbin_mod.T.Int8, ndfbin_mod.T.Int16, ndfbin_mod.T.UInt16,
             ndfbin_mod.T.Int32, ndfbin_mod.T.UInt32, ndfbin_mod.T.Long,
             ndfbin_mod.T.Float32, ndfbin_mod.T.Float64}


if __name__ == "__main__":
    print("AIEditorWindow is launched from the Mod Editor hub in mod_manager.py")

"""
RUSE Economy editor — money, supply, production limits, pop cap, card pool, and economy buildings.

Opened from the Mod Editor hub with a live ModProject; edits the shared gameplay dat (the global
constants in gdconstanteoriginal.cpp + the depot/admin/truck-factory descriptors in everything.cpp),
so changes accumulate and the window's Save writes them to the mod's .dat.

RE: the economy is global-constant driven (TTunableConstante). Income = QteDeviseInitiale (start) +
TempsGenAutoDevises/QuantiteGenAutoDevises (auto tick) + supply convoys (QteDeviseParCamion ...).
"""
import os
import sys
import tkinter as tk
from tkinter import ttk, messagebox

REPO = os.path.dirname(os.path.abspath(__file__))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
from ruse_mod_engine import mod_project as mp_mod  # noqa: E402
from ruse_mod_engine import ndfbin as ndfbin_mod  # noqa: E402
from i18n import t  # noqa: E402
import ui_util  # noqa: E402  — zebra-striping + language-aware sizing

_R_BG, _R_BG_PANEL, _R_BG_WIDGET = "#08101c", "#0e1a2a", "#060d18"
_R_GOLD, _R_GOLD_BRT, _R_TEXT, _R_TEXT_DIM, _R_SEL_BG = "#c8a020", "#e0c030", "#ccd8e8", "#3e5878", "#1a3060"
_F_MAIN = ("Courier New", 9)
_F_BOLD = ("Courier New", 9, "bold")
_F_HEAD = ("Courier New", 10, "bold")

# (label, prop, kind) — kind: scalar | list. gdconstante (TTunableConstante) economy constants.
_GLOBAL_GROUPS = [
    (t("Money & income"), [
        (t("Starting money (QteDeviseInitiale)"), "QteDeviseInitiale", "scalar"),
        (t("Auto-income interval, s (TempsGenAutoDevises)"), "TempsGenAutoDevises", "scalar"),
        (t("Auto-income amount (QuantiteGenAutoDevises)"), "QuantiteGenAutoDevises", "scalar"),
        (t("Extra money on Easy (StockDeviseSupplementaireFacile)"), "StockDeviseSupplementaireFacile", "scalar"),
    ]),
    # A depot has NO per-building wallet in the data; both its per-convoy payout AND its TOTAL supply pool
    # come from these convoy constants (confirmed in-game). money/convoy = NbCamionParConvoi x
    # QteDeviseParCamion; depot TOTAL = that x ~25 (a fixed convoy count, exe-side, not moddable here).
    # Observed: the mod raising the product took depot total ~250 -> ~1500, so BOTH vars scale the total.
    (t("Depot money output (supply convoys)"), [
        (t("Money each truck delivers (QteDeviseParCamion)"), "QteDeviseParCamion", "scalar"),
        (t("Trucks per convoy (NbCamionParConvoi) — also scales depot total"), "NbCamionParConvoi", "scalar"),
        (t("=> money per convoy = trucks x money/truck (read-only)"), "_derived_per_convoy", "derived"),
        (t("=> depot TOTAL supply ~ per-convoy x 25 convoys (read-only)"), "_derived_depot_total", "derived"),
        (t("Seconds between convoys (TempsENtreDeuxConvois)"), "TempsENtreDeuxConvois", "scalar"),
        (t("Min truck spacing (TempsENtreDeuxCamionsEnConvoiMin)"), "TempsENtreDeuxCamionsEnConvoiMin", "scalar"),
        (t("Max truck spacing (TempsENtreDeuxCamionsEnConvoiMax)"), "TempsENtreDeuxCamionsEnConvoiMax", "scalar"),
        (t("Depot nearly-depleted ratio (RatioForDepotNearlyDepleted)"), "RatioForDepotNearlyDepleted", "scalar"),
    ]),
    (t("Building value (AI/score)"), [
        (t("Depot added value (BP_DepotAddedValue)"), "BP_DepotAddedValue", "scalar"),
        (t("HQ value (BP_HqValue)"), "BP_HqValue", "scalar"),
    ]),
    (t("Production limits"), [
        (t("Min production time (MinProductionTime)"), "MinProductionTime", "scalar"),
        (t("Max buildings producing (MaximumBatimentProduction)"), "MaximumBatimentProduction", "scalar"),
        (t("Max production queue (MaxProductionQueueSize)"), "MaxProductionQueueSize", "scalar"),
        (t("Max bldg+techno at once (MaxBatimentAndTechnoProductionSimultaneous)"),
         "MaxBatimentAndTechnoProductionSimultaneous", "scalar"),
        (t("Virtual factory queue slots (VirtualFactoryQueueMaximumSlot)"), "VirtualFactoryQueueMaximumSlot", "scalar"),
        (t("Planes per airfield (NbAvionsParAeroport)"), "NbAvionsParAeroport", "scalar"),
    ]),
    (t("AI production queue & attack trigger"), [
        (t("Army value to force an attack (ArmyValueForceLaunchAttack)"), "ArmyValueForceLaunchAttack", "scalar"),
        (t("Max waiting production requests (MaxWaitingRequest)"), "MaxWaitingRequest", "scalar"),
        (t("Waiting reqs before new factory (NbMaxWaitingRequestBeforeRequestingNewFactory)"),
         "NbMaxWaitingRequestBeforeRequestingNewFactory", "scalar"),
        (t("Max time waiting request, s (MaxTimeWaitingRequest)"), "MaxTimeWaitingRequest", "scalar"),
        (t("Cancel stale waiting requests (CheckAndCancelWaitingRequest, 0/1)"), "CheckAndCancelWaitingRequest", "scalar"),
    ]),
    (t("Decoy / fake-building (bluff)"), [
        (t("Decoy units min, general offensive (NbMin_UniteLeurre_OffensiveGenerale)"), "NbMin_UniteLeurre_OffensiveGenerale", "scalar"),
        (t("Decoy units max, general offensive (NbMax_UniteLeurre_OffensiveGenerale)"), "NbMax_UniteLeurre_OffensiveGenerale", "scalar"),
        (t("Decoy units min, air offensive"), "NbMin_UniteLeurre_OffensiveAerienne", "scalar"),
        (t("Decoy units max, air offensive"), "NbMax_UniteLeurre_OffensiveAerienne", "scalar"),
        (t("Decoy units min, armor offensive"), "NbMin_UniteLeurre_OffensiveBlinde", "scalar"),
        (t("Decoy units max, armor offensive"), "NbMax_UniteLeurre_OffensiveBlinde", "scalar"),
        (t("Fake-building construction delay min, s (ConstructionDelayForFakeBuildingsMin)"), "ConstructionDelayForFakeBuildingsMin", "scalar"),
        (t("Fake-building construction delay max, s (ConstructionDelayForFakeBuildingsMax)"), "ConstructionDelayForFakeBuildingsMax", "scalar"),
    ]),
    (t("Population cap"), [
        (t("Total pop cap (PopCapTotal)"), "PopCapTotal", "scalar"),
        (t("Pop cap per alliance (PopCapPerAlliance)"), "PopCapPerAlliance", "scalar"),
        (t("Pop cap per player (PopCapPerPlayer)"), "PopCapPerPlayer", "scalar"),
        (t("Ghost cap (GhostCap)"), "GhostCap", "scalar"),
    ]),
    (t("Deception-card pool"), [
        (t("Max cards in pool (NbMaxCardsInPool)"), "NbMaxCardsInPool", "scalar"),
        (t("Max cards per zone (MaxNbCardsPerZoneByAlliance)"), "MaxNbCardsPerZoneByAlliance", "scalar"),
        (t("Initial cards, alliance size 1"), "NbInitialCardsInPoolForAllianceTaille_1", "scalar"),
        (t("Initial cards, alliance size 2"), "NbInitialCardsInPoolForAllianceTaille_2", "scalar"),
        (t("Initial cards, alliance size 3"), "NbInitialCardsInPoolForAllianceTaille_3", "scalar"),
        (t("Initial cards, alliance size 4"), "NbInitialCardsInPoolForAllianceTaille_4", "scalar"),
        (t("Min army value to use manip card (MinArmyValueToUseManipulationCard)"),
         "MinArmyValueToUseManipulationCard", "scalar"),
        (t("New-card time thresholds, size 1 (list)"), "PaliersTempsToChooseNewCardForAllianceTaille_1", "list"),
        (t("New-card time thresholds, size 2 (list)"), "PaliersTempsToChooseNewCardForAllianceTaille_2", "list"),
        (t("New-card time thresholds, size 3 (list)"), "PaliersTempsToChooseNewCardForAllianceTaille_3", "list"),
        (t("New-card time thresholds, size 4 (list)"), "PaliersTempsToChooseNewCardForAllianceTaille_4", "list"),
    ]),
]

_BLDG_FIELDS = [
    (t("Cost (all phases, ProductionPrice)"), "ProductionPrice", "list"),
    (t("Build time (ProductionTime)"), "ProductionTime", "scalar"),
    (t("HP (SeuilMort)"), "SeuilMort", "scalar"),
    (t("Show in menu (per phase)"), "ShowInMenu", "boollist"),
    (t("Is depot for AI (IsDepotForIA, 0/1)"), "IsDepotForIA", "scalar"),
    (t("Distance to road (DistanceToRoad)"), "DistanceToRoad", "scalar"),
    (t("Vision (DetectionBase)"), "DetectionBase", "scalar"),
]

# Fixed number of convoys a depot dispenses before depleting (exe-side, not in gdconstante). Observed
# in-game: depot TOTAL supply = NbCamionParConvoi x QteDeviseParCamion x this. (6 x 10 x 25 = 1500.)
_DEPOT_CONVOYS = 25

# Numeric scalar NDF types we can edit as a plain entry (used by the "raw" catch-all sections).
_T = ndfbin_mod.T
_NUM_SCALARS = {int(_T.Bool), int(_T.Int8), int(_T.Int16), int(_T.Int32),
                int(_T.UInt16), int(_T.UInt32), int(_T.Long), int(_T.Float32), int(_T.Float64)}


class EconomyEditorWindow(tk.Frame):
    """Embedded as a nested in-tab view (formerly a Toplevel); the Mod Editor hosts it + the Back bar."""
    def __init__(self, master, project: "mp_mod.ModProject", on_change=None):
        super().__init__(master)
        self.project = project
        self._on_change = on_change
        self.configure(background=_R_BG)
        self._ndf = None      # everything.cpp (buildings)
        self._gd = None       # gdconstanteoriginal.cpp (global constants)
        self._gd_obj = None
        self._bldgs = []      # [(label, inst)]
        try:
            self._ndf = project.everything()
            self._gd = project.get_ndf("gameplay", mp_mod.GDCONSTANTE_PATH)
            self._gd_obj = self._gd.instances[0]
        except Exception as e:
            messagebox.showerror(t("Economy Editor"), t("Could not load gameplay data:\n{e}", e=e), parent=self)
            self.after(10, self.destroy)
            return
        self._index_buildings()
        self._build_ui()

    # ── data ──────────────────────────────────────────────────────────────────

    def _pv(self, ndf, inst, name):
        p = ndf.prop_by_name_and_class(name, inst.class_index) or ndf.prop_by_name(name)
        return inst.get(p.index) if p else None

    def _descr_name(self, inst):
        v = self._pv(self._ndf, inst, "ClassNameForDebug")
        return self._ndf.get_string(v.raw) if v and isinstance(v.raw, int) else ""

    def _index_buildings(self):
        self._bldgs = []
        for inst in self._ndf.class_instances("TBatimentDescriptor"):
            n = self._descr_name(inst)
            if any(k in n for k in ("Depot", "Administratif", "TruckFactory")) \
                    and "Leurre" not in n and "Fake" not in n:
                self._bldgs.append((n, inst))
        self._bldgs.sort(key=lambda t: t[0])

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
        self._nb = nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=4, pady=(6, 2))
        t1, t2 = ttk.Frame(nb), ttk.Frame(nb)
        nb.add(t1, text=t("  Global Economy  "))
        nb.add(t2, text=t("  Economy Buildings  "))
        self._build_global_tab(t1)
        self._build_bldg_tab(t2)

        bottom = tk.Frame(self, background=_R_BG)
        bottom.pack(fill="x", padx=8, pady=(0, 2))
        ttk.Button(bottom, text=t("Save mod (.dat)"), command=self._save).pack(side="left")
        self._save_status = tk.Label(bottom, text="", background=_R_BG, foreground=_R_TEXT_DIM, font=_F_MAIN)
        self._save_status.pack(side="right")
        tk.Label(self, text=t("Global Economy edits gdconstante; Economy Buildings edits the depot/admin/truck "
                              "descriptors. Apply commits into the project; “Save mod (.dat)” writes everything "
                              "to the mod's .dat. Income = starting money + auto-income tick + supply convoys."),
                 background=_R_BG, foreground=_R_TEXT_DIM, font=_F_MAIN, justify="left",
                 wraplength=980).pack(fill="x", padx=8, pady=(0, 6))

    def _build_global_tab(self, parent):
        top = tk.Frame(parent, background=_R_BG_PANEL)
        top.pack(side="bottom", fill="x", padx=6, pady=(0, 6))
        self._g_apply = ttk.Button(top, text=t("Apply global economy changes"), command=self._apply_global)
        self._g_apply.pack(side="left")
        self._g_status = tk.Label(top, text="", background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN)
        self._g_status.pack(side="right")
        holder = tk.Frame(parent, background=_R_BG_PANEL)
        holder.pack(fill="both", expand=True, padx=4, pady=4)
        self._g_frame = self._scrollable(holder)
        self._populate_global()

    def _populate_global(self):
        frame = self._g_frame
        for w in frame.winfo_children():
            w.destroy()
        self._g_rows = []
        self._derived = []
        self._zeb = 0   # zebra-stripe counter for this render (issue #5.2)
        covered = {p for _t, flds in _GLOBAL_GROUPS for _l, p, k in flds if k != "derived"}
        for gtitle, fields in _GLOBAL_GROUPS:
            present = []
            for l, p, k in fields:
                if k == "derived" or self._pv(self._gd, self._gd_obj, p) is not None:
                    present.append((l, p, k))
            if not present:
                continue
            self._section(frame, gtitle)
            for lbl, prop, kind in present:
                if kind == "derived":
                    self._derived_row(frame, lbl, prop)
                else:
                    self._row(frame, self._g_rows, lbl, prop, kind, self._pv(self._gd, self._gd_obj, prop))
        self._append_raw_constants(frame, covered)

    def _build_bldg_tab(self, parent):
        body = tk.Frame(parent, background=_R_BG)
        body.pack(fill="both", expand=True, padx=4, pady=4)
        left = tk.Frame(body, background=_R_BG_PANEL)
        left.pack(side="left", fill="y")
        self._b_lb = tk.Listbox(left, width=34, activestyle="none", background=_R_BG_WIDGET, foreground=_R_TEXT,
                                selectbackground=_R_SEL_BG, selectforeground=_R_GOLD_BRT, font=_F_MAIN,
                                exportselection=False)
        ui_util.with_scrollbars(left, self._b_lb)   # horizontal scroll for long building names (#5.4)
        for lbl, _i in self._bldgs:
            self._b_lb.insert(tk.END, lbl)
        right = tk.Frame(body, background=_R_BG_PANEL)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self._b_hdr = tk.Label(right, text=t("Select an economy building"), background=_R_BG_PANEL,
                               foreground=_R_GOLD_BRT, font=_F_HEAD, anchor="w")
        self._b_hdr.pack(fill="x", padx=6, pady=(4, 0))
        bb = tk.Frame(right, background=_R_BG_PANEL)
        bb.pack(side="bottom", fill="x", padx=6, pady=(0, 6))
        self._b_apply = ttk.Button(bb, text=t("Apply changes"), state="disabled", command=self._apply_bldg)
        self._b_apply.pack(side="left")
        self._b_status = tk.Label(bb, text="", background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN)
        self._b_status.pack(side="right")
        holder = tk.Frame(right, background=_R_BG_PANEL)
        holder.pack(fill="both", expand=True, padx=2, pady=4)
        self._b_fields = self._scrollable(holder)
        self._b_rows = []
        self._b_sel = None
        self._b_lb.bind("<<ListboxSelect>>", self._on_bldg_select)

    # ── field helpers ─────────────────────────────────────────────────────────

    def _section(self, container, title):
        tk.Label(container, text=title, anchor="w", background=_R_BG_PANEL,
                 foreground=_R_GOLD_BRT, font=_F_HEAD).pack(fill="x", padx=2, pady=(14, 4))
        sep = tk.Frame(container, background=_R_SEL_BG, height=1)
        sep.pack(fill="x", padx=2, pady=(0, 4))

    @staticmethod
    def _fmt(val, kind):
        raw = val.raw
        if kind in ("list", "boollist") and isinstance(raw, list):
            if kind == "boollist":
                return ", ".join("1" if getattr(e, "raw", e) else "0" for e in raw)
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

    @staticmethod
    def _raw_kind(val):
        """Classify a value for the raw catch-all: 'scalar' | 'boollist' | 'list', or None (skip)."""
        tid = val.type_id
        if tid in _NUM_SCALARS:
            return "scalar"
        if tid == int(_T.List):
            elems = val.raw
            if isinstance(elems, list) and elems:
                sample = getattr(elems[0], "raw", elems[0])
                if isinstance(sample, bool):
                    return "boollist"
                if isinstance(sample, (int, float)):
                    return "list"
        return None

    def _derived_row(self, container, label, key):
        r = tk.Frame(container, background=_R_BG_PANEL)
        r.pack(fill="x", pady=2)
        r.grid_columnconfigure(0, weight=1)
        self._wrap_label(r, label, _R_TEXT_DIM)
        out = tk.Label(r, text="", width=self._ENT_W, anchor="e", background=_R_BG_PANEL,
                       foreground=_R_GOLD, font=_F_BOLD)
        out.grid(row=0, column=2, sticky="e", padx=(0, 4))
        self._derived.append((key, out))
        ui_util.apply_row_bg(r, ui_util.row_bg(getattr(self, "_zeb", 0), _R_BG_PANEL))  # issue #5.2
        self._zeb = getattr(self, "_zeb", 0) + 1
        self._refresh_derived()

    def _refresh_derived(self):
        for key, widget in getattr(self, "_derived", []):
            txt = ""
            t = self._pv(self._gd, self._gd_obj, "QteDeviseParCamion")
            n = self._pv(self._gd, self._gd_obj, "NbCamionParConvoi")
            if t is not None and n is not None:
                try:
                    per_convoy = int(t.raw) * int(n.raw)
                    if key == "_derived_per_convoy":
                        txt = str(per_convoy)
                    elif key == "_derived_depot_total":
                        txt = str(per_convoy * _DEPOT_CONVOYS)
                except Exception:
                    txt = ""
            widget.configure(text=txt)

    def _append_raw_constants(self, frame, covered):
        gi2n = {p.index: p.name for p in self._gd.properties}
        extras = []
        for pv in self._gd_obj.props:
            nm = gi2n.get(pv.prop_index, "")
            if not nm or nm in covered:
                continue
            kind = self._raw_kind(pv.value)
            if kind:
                extras.append((nm, kind, pv.value))
        if not extras:
            return
        extras.sort(key=lambda t: t[0].lower())
        self._section(frame, t("All other global constants ({n} — advanced, not just economy)", n=len(extras)))
        for nm, kind, val in extras:
            self._row(frame, self._g_rows, nm, nm, kind, val)

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

    def _commit(self, rows):
        changed, errors = 0, []
        for prop, kind, val, var, orig in rows:
            new = var.get().strip()
            if new == orig:
                continue
            try:
                if kind in ("list", "boollist"):
                    elems = val.raw
                    if not isinstance(elems, list) or not elems:
                        continue
                    parts = [p for p in (s.strip() for s in new.replace(";", ",").split(",")) if p]
                    if not parts:
                        continue

                    def conv(x, e):
                        if kind == "boollist":
                            return x.lower() in ("1", "true", "yes", "on")
                        return float(x) if isinstance(getattr(e, "raw", 0), float) else int(float(x))
                    if len(parts) == 1:
                        for e in elems:
                            e.raw = conv(parts[0], e)
                    else:
                        for e, p in zip(elems, parts):
                            e.raw = conv(p, e)
                    changed += 1
                else:
                    val.raw = self._coerce(val.raw, new)
                    changed += 1
            except Exception as e:
                errors.append(f"{prop}: {e}")
        return changed, errors

    # ── apply / save ─────────────────────────────────────────────────────────────

    def _apply_global(self):
        changed, errors = self._commit(self._g_rows)
        if errors:
            messagebox.showerror(t("Invalid value(s)"), "\n".join(errors), parent=self)
        if changed:
            self.project.mark_dirty("gameplay", mp_mod.GDCONSTANTE_PATH)
            self._keep_scroll(self._g_frame, self._populate_global)  # show the set values
            self._notify()
        else:
            self._refresh_derived()
        self._g_status.configure(text=(t("Applied {changed} change(s)", changed=changed) if changed else t("No changes")))

    def _on_bldg_select(self, _=None):
        sel = self._b_lb.curselection()
        if not sel:
            return
        self._b_sel = self._bldgs[sel[0]][1]
        for w in self._b_fields.winfo_children():
            w.destroy()
        self._b_rows = []
        self._zeb = 0   # zebra-stripe counter for this render (issue #5.2)
        self._b_hdr.configure(text=self._bldgs[sel[0]][0])
        self._section(self._b_fields, t("Building economy / stats"))
        covered = set()
        for lbl, prop, kind in _BLDG_FIELDS:
            val = self._pv(self._ndf, self._b_sel, prop)
            if val is not None:
                self._row(self._b_fields, self._b_rows, lbl, prop, kind, val)
            covered.add(prop)
        # catch-all: every other numeric field on this building, by raw name
        i2n = {p.index: p.name for p in self._ndf.properties}
        extras = []
        for pv in self._b_sel.props:
            nm = i2n.get(pv.prop_index, "")
            if not nm or nm in covered:
                continue
            kind = self._raw_kind(pv.value)
            if kind:
                extras.append((nm, kind, pv.value))
        if extras:
            extras.sort(key=lambda t: t[0].lower())
            self._section(self._b_fields, t("Other numeric fields ({n} — raw, advanced)", n=len(extras)))
            for nm, kind, val in extras:
                self._row(self._b_fields, self._b_rows, nm, nm, kind, val)
        self._b_apply.configure(state="normal")
        self._b_status.configure(text="")

    def _apply_bldg(self):
        if self._b_sel is None:
            return
        changed, errors = self._commit(self._b_rows)
        if errors:
            messagebox.showerror(t("Invalid value(s)"), "\n".join(errors), parent=self)
        if changed:
            self.project.mark_dirty("gameplay", mp_mod.EVERYTHING_PATH)
            self._keep_scroll(self._b_fields, self._on_bldg_select)  # show set values, keep position
            self._notify()
        self._b_status.configure(text=(t("Applied {changed} change(s)", changed=changed) if changed else t("No changes")))

    def _notify(self):
        if self._on_change:
            try:
                self._on_change()
            except Exception:
                pass

    def _save(self):
        # Commit any pending (typed-but-not-applied) edits first, so what's saved == what's shown.
        if self._g_rows:
            self._apply_global()
        if self._b_sel is not None and self._b_rows:
            self._apply_bldg()
        if not self.project.is_dirty():
            messagebox.showinfo(t("Save mod"), t("No pending changes to save."), parent=self)
            return
        try:
            written = self.project.save_all()
        except Exception as e:
            messagebox.showerror(t("Save failed"), t("{e}\n\nTip: set the Game Root in Settings.", e=e), parent=self)
            return
        self._notify()
        self._save_status.configure(text=t("Saved all changes to the mod"))
        messagebox.showinfo(t("Saved"), t("Saved mod changes to:\n\n{paths}", paths="\n".join(written)), parent=self)


if __name__ == "__main__":
    print("EconomyEditorWindow is launched from the Mod Editor hub in mod_manager.py")

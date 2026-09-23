"""'Load & Files' tab — the chain that takes a menu entry to a playable mission, walked and checked.

Nothing in the editor represented this before, which is why the failures it catches were the ones that
cost the most time: they are all SILENT in game.

  * A registration record whose GUID is not a key in the map-load-info table is skipped by the menu
    populator with no error and no entry — the mod simply is not there (our internal scenario notes).
  * A scenario cluster that does not export the subcluster path mapinfo names cannot resolve, likewise
    silently (doc 12 section 3.1).
  * An effetmap.xyz that is not under a python path THIS cluster adds is never imported; the game keeps
    running whatever the still-listed folder contains (doc 14 section 4).

So the tab's job is to make each join VISIBLE and say the specific reason when one does not hold. It reads
`scenario_chain.walk`, the same walker that sweeps all 102 shipped scenarios in re_chain_sweep, so what it
shows here is what that sweep asserts there.

Read-first by design: this step is where you find out WHY something is wrong. Editing a join is done at the
step that owns it, and each node offers the jump.
"""
import tkinter as tk
from tkinter import ttk

import theme
import ui_util
from i18n import t
from ruse_mod_engine import scenario_chain as chain

_BG, _PANEL, _WIDGET = theme.BG, theme.PANEL, theme.WIDGET
_TEXT, _DIM, _GOLD, _GOLD_BRT = theme.TEXT, theme.DIM, theme.GOLD, theme.GOLD_BRT
_F, _FB, _FS = theme.F, theme.FB, theme.FS

STEP_INDEX, STEP_TOTAL = 2, 5

# Which walk step owns each kind of node, so "this is broken" can hand you to where it is fixed.
_OWNER_STEP = {
    "registration": 1, "pack": 1,
    "mapload": 2, "cluster-entry": 2, "scenario-cluster": 2,
    "terrain-cluster": 2, "terrain-subcluster": 2, "terrain-dat": 2, "cluster-scenario": 2,
    "scenario-file": 3,
    "script": 4,
    "dico": 5, "dico-langs": 5,
}

_STATUS_TO_STEP = {
    chain.OK: ui_util.STEP_OK,
    chain.MISSING: ui_util.STEP_BAD,
    chain.MISMATCH: ui_util.STEP_BAD,
    chain.UNKNOWN: ui_util.STEP_WARN,
}


class _ProjStore:
    """The project seen as the flat store scenario_chain expects."""

    def __init__(self, project):
        self.project = project

    def entry_paths(self, dat_key, suffix=""):
        try:
            return self.project.entry_paths(dat_key, suffix)
        except Exception:
            return []

    def get_raw(self, dat_key, path):
        try:
            return self.project.get_raw(dat_key, path)
        except Exception:
            return None

    def get_ndf(self, dat_key, path):
        return self.project.get_ndf(dat_key, path)


class LoadFilesFrame(tk.Frame):
    def __init__(self, parent, project, on_go=None, **_kw):
        super().__init__(parent, background=_BG)
        self.project = project
        self.binding = None
        self._store = _ProjStore(project)
        self._on_go = on_go
        self._nodes = {}        # tree iid -> ChainNode
        self._tree_root = None  # the walked ChainNode
        self._build(on_go)

    # ── layout ───────────────────────────────────────────────────────────────────────────────────
    def _build(self, on_go):
        self.step = ui_util.WalkStep(
            self, index=STEP_INDEX, total=STEP_TOTAL, title=t("chain.step_title"),
            decides=t("chain.step_decides"), on_go=on_go, translate=t).pack()

        body = tk.Frame(self, background=_BG)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        left = tk.Frame(body, background=_BG, width=620)
        left.pack(side="left", fill="both")
        left.pack_propagate(False)
        self._tree = ttk.Treeview(left, columns=("what", "detail"), show="tree headings",
                                  selectmode="browse")
        self._tree.heading("#0", text=t("chain.col_step"))
        self._tree.heading("what", text=t("chain.col_state"))
        self._tree.heading("detail", text=t("chain.col_points_at"))
        self._tree.column("#0", width=210, stretch=False)
        self._tree.column("what", width=90, stretch=False)
        self._tree.column("detail", width=300, stretch=True)
        ui_util.with_scrollbars(left, self._tree)
        self._tree.bind("<<TreeviewSelect>>", self._on_pick)
        for state, colour in ((chain.OK, theme.GREEN), (chain.MISSING, theme.RED),
                              (chain.MISMATCH, theme.RED), (chain.UNKNOWN, _GOLD),
                              ("optional", _DIM)):
            self._tree.tag_configure(state, foreground=colour)

        right = tk.Frame(body, background=_BG)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self._hdr = tk.Label(right, text=t("chain.pick_a_step"), background=_BG, foreground=_GOLD_BRT,
                             font=_FB, anchor="w", justify="left", wraplength=460)
        self._hdr.pack(fill="x")
        self._why = tk.Label(right, text="", background=_BG, foreground=_TEXT, font=_F,
                             anchor="w", justify="left", wraplength=460)
        self._why.pack(fill="x", pady=(4, 2))
        self._path = tk.Label(right, text="", background=_BG, foreground=_DIM, font=_FS,
                              anchor="w", justify="left", wraplength=460)
        self._path.pack(fill="x")
        self._jump_holder = tk.Frame(right, background=_BG)
        self._jump_holder.pack(fill="x", pady=(8, 0))

    # ── binding-driven load ──────────────────────────────────────────────────────────────────────
    def set_binding(self, binding):
        self.binding = binding
        for i in self._tree.get_children():
            self._tree.delete(i)
        self._nodes = {}
        self._clear_detail()
        if binding is None:
            self.step.set_status(ui_util.STEP_IDLE, t("chain.no_scenario"))
            return
        try:
            self._walk()
        except Exception as e:
            self.step.set_status(ui_util.STEP_BAD, t("chain.walk_failed", e=e))

    def _walk(self):
        b = self.binding
        self._tree_root = chain.walk(self._store, b.map_dir, b.scenario_name)
        self._insert(self._tree_root, "")
        for iid in self._tree.get_children(""):
            self._expand_all(iid)

        problems = self._tree_root.problems()
        if not problems:
            self.step.set_status(ui_util.STEP_OK, t("chain.all_resolve"))
        else:
            first = problems[0]
            self.step.set_status(ui_util.STEP_BAD, t("chain.n_broken",
                                                     n=len(problems), first=first.reason or first.label))

    def _insert(self, node, parent):
        # An absence that is normal renders neutrally, not red. A red mark on every operation in the game
        # (every one of them declares a Dialog dico it does not ship) trains a user to ignore red.
        tag = "optional" if node.optional else node.status
        iid = self._tree.insert(parent, "end", text=node.label,
                                values=(self._state_text(node.status, node.optional), node.detail),
                                tags=(tag,), open=True)
        self._nodes[iid] = node
        for child in node.children:
            self._insert(child, iid)
        return iid

    def _expand_all(self, iid):
        self._tree.item(iid, open=True)
        for c in self._tree.get_children(iid):
            self._expand_all(c)

    @staticmethod
    def _state_text(status, optional=False):
        if optional:
            return t("chain.state_not_used")
        return {chain.OK: t("chain.state_ok"), chain.MISSING: t("chain.state_missing"),
                chain.MISMATCH: t("chain.state_mismatch")}.get(status, t("chain.state_unknown"))

    # ── the selected node ────────────────────────────────────────────────────────────────────────
    def _clear_detail(self):
        self._hdr.config(text=t("chain.pick_a_step"))
        self._why.config(text="")
        self._path.config(text="")
        for w in self._jump_holder.winfo_children():
            w.destroy()

    def _on_pick(self, _=None):
        sel = self._tree.selection()
        if not sel:
            return
        node = self._nodes.get(sel[0])
        if node is None:
            return
        self._clear_detail()
        self._hdr.config(text=node.label)
        # The reason, in the node's own words, or a plain "this one is fine" — never a bare status code.
        broken = node.status in (chain.MISSING, chain.MISMATCH) and not node.optional
        self._why.config(text=(node.reason or t("chain.resolves_fine")),
                         foreground=(theme.RED if broken else (_DIM if node.optional else _TEXT)))
        self._path.config(text=(node.ref or node.detail or ""))

        owner = _OWNER_STEP.get(node.kind)
        if owner is not None and self._on_go is not None and owner != STEP_INDEX:
            tk.Button(self._jump_holder, text=t("chain.open_step", step=owner),
                      command=lambda s=owner: self._on_go(s), background=theme.BTN,
                      foreground=_GOLD_BRT, activebackground=theme.BTN_ACT, font=_FB,
                      relief="flat", padx=10).pack(anchor="w")

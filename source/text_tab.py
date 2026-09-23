"""'Text' tab — every piece of text a scenario owns, edited as a LocHash rather than one language at a time.

The tab this replaces (names_descriptions_tab) had a language dropdown and wrote ONE `.dic` per save. That
is backwards: a LocHash is a single opaque key that backs all 11 languages, so editing "the current
language" leaves the other ten stale and nothing says so. It also filtered out entries whose text was empty
and printed a bare "k unused" count — hiding the FoldedText slots, which are a real authoring feature that
7 shipped missions fill (our internal scenario notes).

So: one row per key, in document order (menu fields as the record declares them, then script keys by line),
blank rows shown as ordinary rows because they are ordinary rows. Selecting one shows every language at
once and writes them together.

The four operations, matching the Raw Asset Editor's LocHash manager exactly — same engine calls, so they
cannot drift apart:

    Save text   rewrite this key in every language you edited, ADDING it to any that lack it
    Mint new    a brand-new key carrying this text in every language, and point the property at it
    Re-point    aim the property at a different existing key
    Raw hex     the escape hatch

Editing the WORDS works for every entry — that is a `.dic` write. Changing WHICH key a thing points at is
split by where the pointer lives: a menu field's key is a property on the registration record and is
changed here, while a mission line's key is a literal compiled into the `.xyz`, so it is a script edit
(`script_logic.set_label_hash` + recompile) and belongs on the Mission Script step. Rather than accept the
click and then explain, the tab disables those two buttons for a script entry, says why on the entry, and
offers the jump to that step.

Binding-driven: follows the Map/Scenario selection above the tabs via set_binding(). Stages into the mod
project; the project's "Save to mod" writes it.
"""
import tkinter as tk
from tkinter import ttk

import theme
import ui_util
from i18n import t
from ruse_mod_engine import dic as dic_mod
from ruse_mod_engine import edits
from ruse_mod_engine import localization as loc_mod
from ruse_mod_engine import scenario_chain as chain
from ruse_mod_engine import script_logic

_BG, _PANEL, _WIDGET = theme.BG, theme.PANEL, theme.WIDGET
_TEXT, _DIM, _GOLD, _GOLD_BRT = theme.TEXT, theme.DIM, theme.GOLD, theme.GOLD_BRT
_F, _FB, _FS = theme.F, theme.FB, theme.FS

STEP_INDEX, STEP_TOTAL = 5, 5


class _ProjStore:
    """The project seen as the flat store the engine's loc_* helpers expect."""

    def __init__(self, project):
        self.project = project

    def entry_paths(self, dat_key, suffix=""):
        try:
            return self.project.entry_paths(dat_key, suffix)
        except Exception:
            return []

    def read_many(self, dat_key, paths):
        try:
            return self.project.read_many(dat_key, paths)
        except Exception:
            return {}

    def get_raw(self, dat_key, path):
        try:
            return self.project.get_raw(dat_key, path)
        except Exception:
            return None

    def set_raw(self, dat_key, path, data):
        self.project.set_raw(dat_key, path, data)

    def mark_dirty(self, dat_key, path):
        try:
            self.project.mark_dirty(dat_key, path)
        except Exception:
            pass


class TextFrame(tk.Frame):
    def __init__(self, parent, project, on_go=None, **_kw):
        super().__init__(parent, background=_BG)
        self.project = project
        self.binding = None
        self._store = _ProjStore(project)
        self._idx = None            # LocIndex over every .dic in the loc dat
        self._entries = []          # [TextEntry] in document order
        self._sel = None            # the selected TextEntry
        self._boxes = {}            # language code -> Text widget
        self._staged = {}           # (dat_key, path) -> blob, so several keys compose before saving
        self._build(on_go)

    # ── layout ───────────────────────────────────────────────────────────────────────────────────
    def _build(self, on_go):
        self.step = ui_util.WalkStep(
            self, index=STEP_INDEX, total=STEP_TOTAL, title=t("text.step_title"),
            decides=t("text.step_decides"), on_go=on_go, translate=t).pack()

        body = tk.Frame(self, background=_BG)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        # left: the keys this scenario owns
        left = tk.Frame(body, background=_BG, width=430)
        left.pack(side="left", fill="both")
        left.pack_propagate(False)
        cols = ("where", "text")
        self._tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
        self._tree.heading("where", text=t("text.col_where"))
        self._tree.heading("text", text=t("text.col_text"))
        self._tree.column("where", width=150, stretch=False)
        self._tree.column("text", width=250, stretch=True)
        ui_util.with_scrollbars(left, self._tree)
        self._tree.bind("<<TreeviewSelect>>", self._on_pick)

        # right: that key in every language
        right = tk.Frame(body, background=_BG)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self._hdr = tk.Label(right, text=t("text.pick_an_entry"), background=_BG, foreground=_GOLD_BRT,
                             font=_FB, anchor="w", justify="left")
        self._hdr.pack(fill="x")
        self._used = tk.Label(right, text="", background=_BG, foreground=_DIM, font=_FS,
                              anchor="w", justify="left", wraplength=560)
        self._used.pack(fill="x", pady=(0, 4))
        self._langs_holder = ui_util.make_scrollable(right, bg=_BG)

        bar = tk.Frame(self, background=_PANEL)
        bar.pack(side="bottom", fill="x")
        self._bar = bar
        self._b_save = self._btn(bar, t("text.save_text"), self._save_text)
        self._b_mint = self._btn(bar, t("text.mint_new"), self._mint)
        self._b_point = self._btn(bar, t("text.repoint"), self._repoint)
        self._b_raw = self._btn(bar, t("text.raw_hex"), self._raw_hex)
        # Which key a MISSION line uses is a literal compiled into the .xyz, so changing it is a script
        # edit (script_logic.set_label_hash + recompile), not a .dic edit. That belongs on the Mission
        # Script step. Offer the jump there rather than making the user find it.
        self._b_goto = self._btn(bar, t("text.goto_script"), lambda: on_go and on_go(4))
        ui_util.flow(bar, [self._b_save, self._b_mint, self._b_point, self._b_raw, self._b_goto])
        self._on_go = on_go
        self._set_ops_for(None)

    @staticmethod
    def _btn(parent, label, command):
        return tk.Button(parent, text=label, command=command, background=theme.BTN,
                         foreground=_GOLD_BRT, activebackground=theme.BTN_ACT, font=_FB,
                         relief="flat", padx=10)

    # ── binding-driven load ──────────────────────────────────────────────────────────────────────
    def set_binding(self, binding):
        self.binding = binding
        self._sel = None
        self._staged = {}
        for i in self._tree.get_children():
            self._tree.delete(i)
        self._clear_langs()
        if binding is None:
            self.step.set_status(ui_util.STEP_IDLE, t("text.no_scenario"))
            return
        try:
            self._reload()
        except Exception as e:                       # never leave the tab blank with no reason shown
            self.step.set_status(ui_util.STEP_BAD, t("text.load_failed", e=e))

    def _reload(self):
        b = self.binding
        self._idx = edits.loc_index(self._store)
        if self._idx is None:
            self.step.set_status(ui_util.STEP_BAD, t("text.no_loc_dat"))
            return

        reg_ndf = reg_inst = None
        if getattr(b, "info_idx", None) is not None:
            try:
                reg_ndf = self.project.get_ndf("gameplay", chain.GLOBALS_PATH)
                reg_inst = reg_ndf.instances[b.info_idx]
            except Exception:
                reg_ndf = reg_inst = None

        # The script comes from the CHAIN, not a filename guess: the folder a cluster actually puts on the
        # python path is the one whose effetmap runs (our internal scenario notes).
        source = ""
        try:
            tree = chain.walk(self._store, b.map_dir, b.scenario_name)
            hit = next((n for n in tree.walk() if n.kind == "script" and n.status == chain.OK), None)
            if hit is not None:
                source = script_logic.decompile_xyz(self._store.get_raw("scripts", hit.ref))
        except Exception:
            source = ""

        self._entries = edits.scenario_text_inventory(
            self._idx, reg_ndf=reg_ndf, reg_inst=reg_inst, script_source=source)
        self._render_rows()

        n_menu = sum(1 for e in self._entries if e.world == "menu")
        n_mission = len(self._entries) - n_menu
        langs = len(self._idx.available_langs())
        if not self._entries:
            self.step.set_status(ui_util.STEP_WARN, t("text.nothing_found"))
        elif not source and getattr(b, "kind", "") in ("operation", "campaign"):
            self.step.set_status(ui_util.STEP_WARN, t("text.no_script_text", menu=n_menu))
        else:
            self.step.set_status(ui_util.STEP_OK, t("text.loaded",
                                                    n=len(self._entries), menu=n_menu,
                                                    mission=n_mission, langs=langs))

    def _render_rows(self):
        self._rows = {}
        for e in self._entries:
            w = e.where[0] if e.where else None
            where = w.detail if w else e.key_hex[:8]
            if w is not None and w.source == "mission script":
                where = "%s  %s" % (w.detail.split("=")[0], w.where)
            iid = self._tree.insert("", "end", values=(where, e.label().strip() or ""))
            self._rows[iid] = e
        ui_util.stripe_treeview(self._tree, _WIDGET)
        ui_util.retag_treeview(self._tree)

    # ── the selected key, in every language ──────────────────────────────────────────────────────
    def _clear_langs(self):
        for w in self._langs_holder.winfo_children():
            w.destroy()
        self._boxes = {}

    def _on_pick(self, _=None):
        sel = self._tree.selection()
        if not sel:
            return
        self._commit_boxes()                  # keep edits when moving between entries
        self._sel = self._rows.get(sel[0])
        self._show_selected()

    def _set_ops_for(self, entry):
        """Enable only the operations that can actually apply to this entry, so the buttons show what is
        possible instead of accepting a click and then explaining why it did nothing.

        Editing the WORDS works for any entry — that is a .dic write. Changing WHICH key a property points
        at only works for menu text here: a mission line's key is a literal inside the compiled .xyz, so
        re-pointing it is a script edit and lives on the Mission Script step."""
        is_menu = bool(entry is not None and any(u.source == "menu entry" for u in entry.where))
        have = entry is not None
        for b, on in ((self._b_save, have), (self._b_mint, is_menu),
                      (self._b_point, is_menu), (self._b_raw, is_menu)):
            b.config(state=("normal" if on else "disabled"))
        script_only = have and not is_menu
        self._b_goto.config(state=("normal" if (script_only and self._on_go) else "disabled"))
        for b in (self._b_point, self._b_raw):
            ui_util.tooltip(b, t("text.repoint_script_only") if script_only else t("text.repoint_tip"))

    def _show_selected(self):
        self._clear_langs()
        e = self._sel
        self._set_ops_for(e)
        if e is None:
            return
        self._hdr.config(text=t("text.key_header", key=e.key_hex))
        is_menu = any(u.source == "menu entry" for u in e.where)
        self._used.config(text=(t("text.used_by", where="; ".join(
            "%s %s (%s)" % (u.source, u.where, u.detail) for u in e.where) or t("text.used_by_nothing"))
            + ("" if is_menu else "  — " + t("text.script_key_note"))))

        # Every language in the key's family, INCLUDING those that do not have it yet — those are the
        # ones that render blank in game, and they are only fixable if they are visible.
        fam_langs = sorted({loc_mod.lang_from_path(p) for _dk, p in e.family} | set(e.langs),
                           key=lambda c: [x for x, _ in dic_mod.LANGUAGES].index(c)
                           if c in [x for x, _ in dic_mod.LANGUAGES] else 99)
        for code in fam_langs:
            row = tk.Frame(self._langs_holder, background=_BG)
            row.pack(fill="x", pady=1)
            present = code in e.langs
            label = dic_mod.lang_label(code)
            tk.Label(row, text=label, background=_BG, font=_FS, width=18, anchor="w",
                     foreground=(_GOLD if present else theme.RED)).pack(side="left")
            box = tk.Text(row, height=2, background=_WIDGET, foreground=_TEXT, insertbackground=_TEXT,
                          font=_F, wrap="word", relief="flat", highlightthickness=1,
                          highlightbackground=theme.BORDER)
            box.pack(side="left", fill="x", expand=True)
            box.insert("1.0", e.langs.get(code, ""))
            self._boxes[code] = box
            if not present:
                ui_util.tooltip(box, t("text.missing_here"))

    def _commit_boxes(self):
        """Fold the visible boxes back into the selected entry, so switching rows does not lose typing."""
        if self._sel is None or not self._boxes:
            return
        for code, box in self._boxes.items():
            try:
                self._sel.langs[code] = box.get("1.0", "end-1c")
            except tk.TclError:
                pass

    # ── the four operations ──────────────────────────────────────────────────────────────────────
    def _need(self):
        if self._idx is None or self._sel is None:
            ui_util.info(self, t("text.step_title"), t("text.pick_an_entry"))
            return False
        return True

    def _save_text(self):
        if not self._need():
            return
        self._commit_boxes()
        e = self._sel
        # add_missing: a language whose .dic lacks this key is ADDED rather than skipped. Skipping is how
        # a mod ends up permanently blank in one language with nothing to say so.
        blobs = edits.loc_set_text(self._idx, e.key, dict(e.langs), add_missing=True,
                                   family=e.family, staged=self._staged)
        self._staged = blobs
        n = edits.loc_write(self._store, blobs)
        self._idx = edits.loc_index(self._store)
        self._staged = {}
        e.langs = edits.loc_langs(self._idx, e.key)
        self._refresh_row(e)
        self.step.set_status(ui_util.STEP_OK, t("text.saved", files=n, langs=len(e.langs)))

    def _mint(self):
        if not self._need():
            return
        self._commit_boxes()
        seed = (self._sel.langs.get("us") or next((s for s in self._sel.langs.values() if s), "")).strip()
        text = self._prompt(t("text.mint_new"), t("text.mint_prompt"), seed)
        if not text:
            return
        try:
            key, blobs, existed = edits.loc_mint(self._idx, text, family_of=self._sel.key)
        except edits.EditError as ex:
            ui_util.error(self, t("text.mint_new"), str(ex))
            return
        if existed and not ui_util.confirm(self, t("text.mint_new"),
                                           t("text.mint_exists", key=key.hex())):
            return
        if blobs:
            edits.loc_write(self._store, blobs)
        self._repoint_to(key)
        self.step.set_status(ui_util.STEP_OK,
                             t("text.minted", key=key.hex(), files=len(blobs)))

    def _repoint(self):
        if not self._need():
            return
        picked = self._pick_existing()
        if picked is not None:
            self._repoint_to(picked)

    def _raw_hex(self):
        if not self._need():
            return
        cur = self._sel.key_hex
        s = self._prompt(t("text.raw_hex"), t("text.raw_prompt"), cur)
        if not s:
            return
        try:
            key = bytes.fromhex(s.strip())
            if len(key) != 8:
                raise ValueError
        except ValueError:
            ui_util.error(self, t("text.raw_hex"), t("text.raw_bad"))
            return
        self._repoint_to(key)

    def _repoint_to(self, key):
        """Point the PROPERTY this entry came from at a different key. Only meaningful for menu text —
        a script literal lives in the compiled .xyz and is changed on the script step, not here."""
        e = self._sel
        menu_use = next((u for u in e.where if u.source == "menu entry"), None)
        if menu_use is None:
            ui_util.info(self, t("text.repoint"), t("text.repoint_script_only"))
            return
        try:
            g = self.project.get_ndf("gameplay", chain.GLOBALS_PATH)
            inst = g.instances[self.binding.info_idx]
            edits.loc_repoint(g, inst, menu_use.detail, key)
            self.project.mark_dirty("gameplay", chain.GLOBALS_PATH)
        except Exception as ex:
            ui_util.error(self, t("text.repoint"), str(ex))
            return
        self._reload()
        self.step.set_status(ui_util.STEP_OK, t("text.repointed", prop=menu_use.detail, key=key.hex()))

    # ── small dialogs ────────────────────────────────────────────────────────────────────────────
    def _prompt(self, title, label, initial=""):
        out = {"v": None}
        dlg = ui_util.themed_toplevel(self, title, size=(560, 170), resizable=True)
        tk.Label(dlg, text=label, background=_PANEL, foreground=_TEXT, font=_F,
                 wraplength=520, justify="left").pack(anchor="w", padx=10, pady=(10, 4))
        var = tk.StringVar(value=initial)
        ent = ttk.Entry(dlg, textvariable=var, width=70)
        ent.pack(fill="x", padx=10)
        row = tk.Frame(dlg, background=_PANEL)
        row.pack(pady=10)

        def ok():
            out["v"] = var.get().strip()
            dlg.destroy()
        ui_util.flow(row, [self._btn(row, t("common.ok"), ok),
                           self._btn(row, t("common.cancel"), dlg.destroy)])
        ent.focus_set()
        dlg.bind("<Return>", lambda *_: ok())
        dlg.bind("<Escape>", lambda *_: dlg.destroy())
        dlg.wait_window()
        return out["v"]

    def _pick_existing(self):
        """Search every string in the game and pick one to point at. Shown in the manager's configured
        language, the same way the Raw editor shows it."""
        out = {"k": None}
        dlg = ui_util.themed_toplevel(self, t("text.repoint"), size=(760, 520), resizable=True)
        var = tk.StringVar()
        top = tk.Frame(dlg, background=_PANEL)
        top.pack(fill="x", padx=8, pady=6)
        tk.Label(top, text=t("text.search"), background=_PANEL, foreground=_GOLD,
                 font=_FB).pack(side="left")
        ttk.Entry(top, textvariable=var, width=48).pack(side="left", fill="x", expand=True, padx=6)
        tree = ttk.Treeview(dlg, columns=("key", "s"), show="headings", selectmode="browse")
        tree.heading("key", text=t("text.col_key"))
        tree.heading("s", text=t("text.col_text"))
        tree.column("key", width=150, stretch=False)
        holder = tk.Frame(dlg, background=_PANEL)
        holder.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        tree.pack(in_=holder, fill="both", expand=True)
        rows = {}

        def search(*_):
            for i in tree.get_children():
                tree.delete(i)
            rows.clear()
            for ent in self._idx.search(var.get(), limit=300):
                iid = tree.insert("", "end", values=(ent.key_hex, ent.string.replace("\n", " ")[:120]))
                rows[iid] = ent.key
            ui_util.stripe_treeview(tree, _WIDGET)
            ui_util.retag_treeview(tree)

        def ok():
            sel = tree.selection()
            if sel:
                out["k"] = rows.get(sel[0])
            dlg.destroy()
        var.trace_add("write", lambda *_: search())
        row = tk.Frame(dlg, background=_PANEL)
        row.pack(pady=(0, 8))
        ui_util.flow(row, [self._btn(row, t("common.ok"), ok),
                           self._btn(row, t("common.cancel"), dlg.destroy)])
        search()
        dlg.wait_window()
        return out["k"]

    def _refresh_row(self, entry):
        for iid, e in self._rows.items():
            if e is entry:
                vals = list(self._tree.item(iid, "values"))
                vals[1] = entry.label().strip()
                self._tree.item(iid, values=vals)
                break

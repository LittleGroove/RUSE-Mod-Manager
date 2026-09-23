"""'Menu Entry' tab — whether this scenario appears in the main menu, where in the list, and what the
player reads before pressing Start.

Leads with a VERDICT, because the way this step fails is silent. The menu populator probes a hash map with
each record's GUID and skips any that is not a key in the map-load-info table — no error, no entry, no
indication (our internal scenario notes). A record in no pack is likewise never enumerated. So the
first thing the tab says is whether the game will list this at all, and why not when it will not.

Fields are read from the RECORD'S OWN CLASS rather than a hardcoded list, because the three registration
records genuinely differ (doc 13 section 2): an operation has LongDescription1..4, a campaign chapter has a
single LongDescription plus ChapterId, and only MP has GameType/MapSize/the six Dispo flags. A shared form
that offered every field to every kind would write properties the game cannot read back — `edits`
refuses those, and this tab avoids ever asking.

Text is shown here but owned by the Text step: these are LocHash keys backing all 11 languages, so the
words are edited there and this tab links to it.
"""
import tkinter as tk
from tkinter import ttk

import theme
import ui_util
from i18n import t
from ruse_mod_engine import edits
from ruse_mod_engine import scenario_chain as chain
from ruse_mod_engine import scenario_registry as SR
from ruse_mod_engine.ndfbin import NdfValue, T

_BG, _PANEL, _WIDGET = theme.BG, theme.PANEL, theme.WIDGET
_TEXT, _DIM, _GOLD, _GOLD_BRT = theme.TEXT, theme.DIM, theme.GOLD, theme.GOLD_BRT
_F, _FB, _FS = theme.F, theme.FB, theme.FS

STEP_INDEX, STEP_TOTAL = 1, 5

# Friendly labels for the fields we understand. Anything else still renders, under its raw name — the
# field list comes from the record's class, so a build that adds a property does not need a code change.
_LABELS = {
    "TrackingId": "menu.f_tracking", "CategoryId": "menu.f_category", "ChapterId": "menu.f_chapter",
    "NbPlayers": "menu.f_players", "NbSecondaryObjectives": "menu.f_secondary",
    "PopCapPlayer": "menu.f_popcap_player", "PopCapIA": "menu.f_popcap_ai",
    "BonusTime": "menu.f_bonus_time", "BonusSurvival": "menu.f_bonus_survival",
    "PlayersColors": "menu.f_seat_colours", "PlayersNations": "menu.f_seat_nations",
    "GameType": "menu.f_gametype", "GameModeMulti": "menu.f_gamemode", "MapSize": "menu.f_mapsize",
    "RewardId": "menu.f_reward", "PrivilegeId": "menu.f_privilege",
    "DispoLadder1v1": "menu.f_ladder1v1", "DispoLadder2v2": "menu.f_ladder2v2",
    "DispoMulti2Teams": "menu.f_multi2", "DispoMulti3Teams": "menu.f_multi3",
    "DispoMulti4Teams": "menu.f_multi4", "DispoMultiFFA": "menu.f_ffa",
}
# Text properties live here but are EDITED on the Text step (they are LocHash keys, not strings).
_TEXT_PROPS = ("Description", "LongDescription", "LongDescription1", "LongDescription2",
               "LongDescription3", "LongDescription4")
_TEXT_STEP = 5


def _kind_label(kind):
    """The kind as a reader would say it. Interpolating the raw key gave "a operation"."""
    return {"operation": t("menu.kind_operation"), "campaign": t("menu.kind_campaign"),
            "mp": t("menu.kind_mp")}.get(kind, kind)


class _ProjStore:
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


class MenuEntryFrame(tk.Frame):
    def __init__(self, parent, project, on_go=None, on_pick=None, **_kw):
        super().__init__(parent, background=_BG)
        self.project = project
        self.binding = None
        self._store = _ProjStore(project)
        self._on_go = on_go
        self._on_pick = on_pick   # host: switch the whole walk to the entry the user clicked
        self._g = None            # globals.cpp NDF
        self._inst = None         # this scenario's registration record
        self._vars = {}           # property name -> tk var
        self._list_idx = []       # listbox row -> info_idx
        self._syncing = False     # guard: re-rendering the list must not look like a user click
        self._build(on_go)

    # ── layout ───────────────────────────────────────────────────────────────────────────────────
    def _build(self, on_go):
        self.step = ui_util.WalkStep(
            self, index=STEP_INDEX, total=STEP_TOTAL, title=t("menu.step_title"),
            decides=t("menu.step_decides"), on_go=on_go, translate=t).pack()

        body = tk.Frame(self, background=_BG)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        # left: the menu list as the player sees it, with this entry marked
        left = tk.Frame(body, background=_BG, width=430)
        left.pack(side="left", fill="both")
        left.pack_propagate(False)
        tk.Label(left, text=t("menu.list_header"), background=_BG, foreground=_GOLD,
                 font=_FB, anchor="w").pack(fill="x")
        # with_scrollbars grids, so the widget needs a holder of its own: a frame that mixes pack and
        # grid is a Tk error on some builds and a subtly wrong layout on the rest.
        lholder = tk.Frame(left, background=_BG)
        lholder.pack(fill="both", expand=True)
        self._list = tk.Listbox(lholder, background=_WIDGET, foreground=_TEXT, font=_F,
                                selectbackground=theme.SEL_BG, selectforeground=theme.SEL_FG,
                                relief="flat", highlightthickness=0, activestyle="none")
        ui_util.with_scrollbars(lholder, self._list, hbar=False)
        # The list is the menu as the player sees it, so clicking a row has to MOVE you to that entry.
        # Without this it highlighted the row and did nothing, which reads as the editor being stuck on
        # one scenario.
        #
        # Through debounce_load, like every other heavy list in the app, and NOT a raw <<ListboxSelect>>:
        # that virtual event fires once per row as you arrow through the list (measured), and opening an
        # entry re-selects the scenario and rebuilds all five steps. Holding Down over the 30 multiplayer
        # rows would be 30 full walk reloads. Debounced, only the row you settle on is opened.
        ui_util.debounce_load(self._list, self._on_row_click, on_peek=self._peek_row)
        order = tk.Frame(left, background=_BG)
        order.pack(fill="x", pady=(4, 0))
        ui_util.flow(order, [
            self._btn(order, t("menu.move_up"), lambda: self._move(-1)),
            self._btn(order, t("menu.move_down"), lambda: self._move(1)),
            self._btn(order, t("menu.new_group"), self._new_group),
        ])

        # right: the record's own fields
        right = tk.Frame(body, background=_BG)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self._kind_lbl = tk.Label(right, text="", background=_BG, foreground=_GOLD_BRT, font=_FB,
                                  anchor="w")
        self._kind_lbl.pack(fill="x")
        self._fields = ui_util.make_scrollable(right, bg=_BG)

        bar = tk.Frame(self, background=_PANEL)
        bar.pack(side="bottom", fill="x")
        ui_util.flow(bar, [self._btn(bar, t("menu.apply"), self._apply)])

    @staticmethod
    def _btn(parent, label, command):
        return tk.Button(parent, text=label, command=command, background=theme.BTN,
                         foreground=_GOLD_BRT, activebackground=theme.BTN_ACT, font=_FB,
                         relief="flat", padx=10)

    # ── binding-driven load ──────────────────────────────────────────────────────────────────────
    def set_binding(self, binding):
        self.binding = binding
        self._list.delete(0, "end")
        self._list_idx = []
        for w in self._fields.winfo_children():
            w.destroy()
        self._vars = {}
        self._g = self._inst = None
        if binding is None:
            self.step.set_status(ui_util.STEP_IDLE, t("menu.no_scenario"))
            self._kind_lbl.config(text="")
            return
        try:
            self._load()
        except Exception as e:
            self.step.set_status(ui_util.STEP_BAD, t("menu.load_failed", e=e))

    def _load(self):
        b = self.binding
        if b.kind not in SR.REGISTRY_KINDS or b.info_idx is None:
            self._kind_lbl.config(text=t("menu.unregistered_title"))
            self.step.set_status(ui_util.STEP_WARN, t("menu.unregistered"))
            return
        self._g = self.project.get_ndf("gameplay", chain.GLOBALS_PATH)
        self._inst = self._g.instances[b.info_idx]
        self._kind_lbl.config(text=t("menu.record_of_kind",
                                     kind=_kind_label(b.kind), cls=SR.REGISTRY_KINDS[b.kind].info_class))
        self._verdict()
        self._render_list()
        self._render_fields()

    # ── the verdict: will the game list this at all? ─────────────────────────────────────────────
    def _verdict(self):
        """The menu populator silently drops a record whose GUID matches no map slot, and never
        enumerates one that is in no pack. Both are invisible in game, so they are said out loud here."""
        b = self.binding
        tree = chain.walk(self._store, b.map_dir, b.scenario_name)
        by = {n.kind: n for n in tree.walk()}
        reg, pack, slot = by.get("registration"), by.get("pack"), by.get("mapload")
        if slot is None or slot.status != chain.OK:
            self.step.set_status(ui_util.STEP_BAD, t("menu.verdict_no_slot"))
        elif reg is None or reg.status != chain.OK:
            self.step.set_status(ui_util.STEP_BAD, t("menu.verdict_guid_unmatched"))
        elif pack is None or pack.status != chain.OK:
            self.step.set_status(ui_util.STEP_BAD, t("menu.verdict_no_pack"))
        else:
            self.step.set_status(ui_util.STEP_OK, t("menu.verdict_ok", kind=_kind_label(b.kind)))

    # ── the menu list ────────────────────────────────────────────────────────────────────────────
    def _render_list(self):
        self._syncing = True
        try:
            self._render_list_inner()
        finally:
            self._syncing = False

    def _render_list_inner(self):
        self._list.delete(0, "end")
        self._list_idx = []
        b = self.binding
        try:
            full = SR.full_menu_order(self._g, b.kind)
        except Exception:
            full = []
        prop = SR.group_prop_for(b.kind)
        last_group = object()
        for info_idx, _pack_idx in full:
            key = SR._group_key(self._g, info_idx, prop)
            if key != last_group:
                last_group = key
                self._list.insert("end", "  " + SR.group_label(b.kind, key))
                self._list.itemconfig("end", foreground=_GOLD)
                self._list_idx.append(None)
            mark = ">" if info_idx == b.info_idx else " "
            self._list.insert("end", " %s %s" % (mark, self._entry_label(info_idx)))
            if info_idx == b.info_idx:
                self._list.itemconfig("end", foreground=_GOLD_BRT)
                self._list.selection_clear(0, "end")
                self._list.selection_set("end")
                self._list.see("end")
            self._list_idx.append(info_idx)

    def _peek_row(self):
        """Instant feedback that the pick registered, before the (slow) walk reload.

        Opening an entry re-selects the scenario and rebuilds all five steps, which is long enough that
        without this the click looks ignored — the same reason every other heavy list in the app peeks.
        """
        if self._syncing or self._g is None:
            return
        sel = self._list.curselection()
        if not sel:
            return
        row = int(sel[0])
        info_idx = self._list_idx[row] if row < len(self._list_idx) else None
        if info_idx is None or (self.binding is not None and info_idx == self.binding.info_idx):
            return
        self.step.set_status(ui_util.STEP_IDLE,
                             t("common.loading_name", name=self._entry_label(info_idx)))

    def _on_row_click(self, _evt=None):
        """Clicking a row moves the walk to that menu entry.

        Every menu row belongs to a real scenario (measured: all 68 shipped rows across the three kinds
        resolve to a binding), so this always has somewhere to go. It asks the HOST to change the
        selection rather than swapping this tab's record on its own — the other four steps follow the same
        selection, and a Menu Entry tab quietly editing a different scenario than Load & Files or Text is
        showing you two missions at once.
        """
        if self._syncing or self._g is None or self.binding is None:
            return
        sel = self._list.curselection()
        if not sel:
            return
        row = int(sel[0])
        info_idx = self._list_idx[row] if row < len(self._list_idx) else None
        if info_idx is None:                      # a group heading, not an entry
            self._restore_selection()
            return
        if info_idx == self.binding.info_idx:     # already the one being edited
            return
        moved = False
        if self._on_pick is not None:
            try:
                moved = bool(self._on_pick(info_idx))
            except Exception:
                moved = False
        if not moved:
            # Nothing changed, so the highlight must not claim otherwise.
            self._restore_selection()
            self.step.set_status(ui_util.STEP_WARN, t("menu.pick_failed"))

    def _restore_selection(self):
        """Put the highlight back on the entry actually being edited."""
        self._syncing = True
        try:
            self._list.selection_clear(0, "end")
            b = self.binding
            for row, idx in enumerate(self._list_idx):
                if b is not None and idx == b.info_idx:
                    self._list.selection_set(row)
                    self._list.see(row)
                    break
        finally:
            self._syncing = False

    def _entry_label(self, info_idx):
        """A row's name: its TrackingId, which is the stable human id the game uses (MP31/M01/CH26)."""
        try:
            inst = self._g.instances[info_idx]
            tid = SR._str_prop(self._g, inst, "TrackingId")
            return tid or ("#%d" % info_idx)
        except Exception:
            return "#%d" % info_idx

    def _move(self, delta):
        if self._g is None or self.binding.pack_idx is None:
            return
        # Clamped to the entry's own group: menu groups are contiguous runs, and a record that leaves its
        # run splits the group in two (our internal scenario notes, and edits.place_at_end_of_group).
        SR.move_within_group(self._g, self.binding.kind, self.binding.pack_idx,
                             self.binding.info_idx, delta)
        self.project.mark_dirty("gameplay", chain.GLOBALS_PATH)
        self._render_list()
        self.step.set_status(ui_util.STEP_OK, t("menu.moved"))

    def _new_group(self):
        if self._g is None or self.binding.pack_idx is None:
            return
        val = SR.next_group_value(self._g, self.binding.kind, self.binding.pack_idx)
        SR.assign_group(self._g, self.binding.kind, self.binding.pack_idx, self.binding.info_idx, val)
        edits.place_at_end_of_group(self._g, self.binding.kind, self.binding.pack_idx,
                                    self.binding.info_idx)
        self.project.mark_dirty("gameplay", chain.GLOBALS_PATH)
        self._render_list()
        self._render_fields()
        self.step.set_status(ui_util.STEP_OK, t("menu.new_group_done", value=val))

    # ── the record's fields, read from its own class ─────────────────────────────────────────────
    def _render_fields(self):
        for w in self._fields.winfo_children():
            w.destroy()
        self._vars = {}
        have = {}
        for pv in self._inst.props:
            p = self._g.prop_by_index(pv.prop_index)
            if p is not None:
                have[p.name] = pv.value

        for name in _TEXT_PROPS:
            if name in have:
                self._text_row(name, have[name])
        for name, val in have.items():
            if name in _TEXT_PROPS or name == "GUID":
                continue
            self._field_row(name, val)

    def _label_for(self, name):
        key = _LABELS.get(name)
        return t(key) if key else name

    def _text_row(self, name, value):
        row = tk.Frame(self._fields, background=_BG)
        row.pack(fill="x", pady=1)
        tk.Label(row, text=self._label_for(name), background=_BG, foreground=_GOLD, font=_FS,
                 width=22, anchor="w").pack(side="left")
        key = bytes(value.raw) if value.type_id == T.LocHash else b""
        tk.Label(row, text=t("menu.text_lives_on_step", key=key.hex()[:8]), background=_BG,
                 foreground=_DIM, font=_FS, anchor="w").pack(side="left", fill="x", expand=True)
        if self._on_go is not None:
            self._btn(row, t("menu.open_text"), lambda: self._on_go(_TEXT_STEP)).pack(side="right")

    def _field_row(self, name, value):
        row = tk.Frame(self._fields, background=_BG)
        row.pack(fill="x", pady=1)
        tk.Label(row, text=self._label_for(name), background=_BG, foreground=_GOLD, font=_FS,
                 width=22, anchor="w").pack(side="left")
        if value.type_id == T.Bool:
            var = tk.BooleanVar(value=bool(value.raw))
            tk.Checkbutton(row, variable=var, background=_BG, activebackground=_BG,
                           selectcolor=_WIDGET).pack(side="left")
        elif value.type_id == T.List:
            var = tk.StringVar(value=", ".join(str(getattr(x, "raw", x)) for x in value.raw))
            ttk.Entry(row, textvariable=var, width=30).pack(side="left", fill="x", expand=True)
        elif value.type_id in (T.StringRef, T.PathRef):
            var = tk.StringVar(value=self._g.get_string(value.raw))
            ttk.Entry(row, textvariable=var, width=30).pack(side="left", fill="x", expand=True)
        elif value.type_id in (T.Int32, T.UInt32, T.Int16, T.UInt16, T.Int8):
            var = tk.StringVar(value=str(value.raw))
            ttk.Entry(row, textvariable=var, width=12).pack(side="left")
        else:
            tk.Label(row, text=t("menu.not_editable_here", type=T.name(value.type_id)), background=_BG,
                     foreground=_DIM, font=_FS, anchor="w").pack(side="left", fill="x", expand=True)
            return
        self._vars[name] = (var, value.type_id)

    def _apply(self):
        """Write the edited fields back. Every write goes through edits.set_registration_field, which
        refuses a property this record's class does not declare — so a value can never be written into a
        field the game will not read back."""
        if self._g is None or self._inst is None:
            return
        wrote, bad = 0, []
        for name, (var, type_id) in self._vars.items():
            try:
                new = self._value_from(var, type_id, name)
            except ValueError:
                bad.append(name)
                continue
            if new is None:
                continue
            try:
                edits.set_registration_field(self._g, self.binding.kind, self._inst, name, new)
                wrote += 1
            except edits.EditError as e:
                bad.append("%s (%s)" % (name, e))
        if wrote:
            self.project.mark_dirty("gameplay", chain.GLOBALS_PATH)
        if bad:
            self.step.set_status(ui_util.STEP_BAD, t("menu.apply_bad", n=wrote, bad=", ".join(bad[:3])))
        else:
            self.step.set_status(ui_util.STEP_OK, t("menu.applied", n=wrote))
        self._verdict() if not bad else None

    def _value_from(self, var, type_id, name):
        raw = var.get()
        if type_id == T.Bool:
            return NdfValue(T.Bool, 1 if raw else 0)
        if type_id in (T.Int32, T.UInt32, T.Int16, T.UInt16, T.Int8):
            s = str(raw).strip()
            if s == "":
                return None
            return NdfValue(type_id, int(s))
        if type_id in (T.StringRef, T.PathRef):
            return NdfValue(type_id, self._g.ensure_string(str(raw)))
        if type_id == T.List:
            parts = [p.strip() for p in str(raw).split(",") if p.strip()]
            return NdfValue(T.List, [NdfValue(T.Int32, int(p)) for p in parts])
        return None

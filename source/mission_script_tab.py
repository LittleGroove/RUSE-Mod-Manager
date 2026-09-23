"""'Mission Rules & Script' tab — how the mission plays, and how it ends.

Folds four tabs into one step. Objectives & Logic, Timeline and Node Graph were three views of the same
decompiled `effetmap.xyz` where only one of them edited and only three fields; Author was a WHEN/DO builder
with 2 triggers and 4 actions against a DSL of 1,133 classes. They are the same object, so they are one
step: an outline to navigate by, a guided pane to change the selected thing, and the source itself.

Two things this step is honest about that its predecessors were not.

RULES ARE NOT WHAT THEY LOOK LIKE. `GameRulesChallenge.__init__` accepts MinScore/MaxScore and then
hard-codes `min_score_defeat = max_score_victory = defaite_perte_Army = False` (our internal scenario notes). So in an operation or campaign mission NOTHING ends the mission except an explicit terminal
descriptor — the score fields are bookkeeping. The old tab presented them as if they ended the mission.
Multiplayer is the opposite: it runs the shared `effetmapmultiplayer` rules, where those fields do decide.

HOW IT ENDS IS A FIRST-CLASS THING. The terminal descriptors are the only exits, and
`DescriptorDeclencheVictoire.Chapter` is what the Next Mission button launches — so a campaign's running
order lives half here and half in the menu list, and this step says so.

Editing goes through the proven path: decompile -> edit source -> recompile with the bundled Python 2.5.1
-> set_raw. A form edit and a hand edit are the same edit against the same text, so the panes cannot
disagree.
"""
import re
import tkinter as tk
from tkinter import ttk

import theme
import ui_util
from i18n import t
from ruse_mod_engine import dsl_catalog as catalog
from ruse_mod_engine import scenario_chain as chain
from ruse_mod_engine import script_logic as SL

_BG, _PANEL, _WIDGET = theme.BG, theme.PANEL, theme.WIDGET
_TEXT, _DIM, _GOLD, _GOLD_BRT = theme.TEXT, theme.DIM, theme.GOLD, theme.GOLD_BRT
_F, _FB, _FS = theme.F, theme.FB, theme.FS

STEP_INDEX, STEP_TOTAL = 4, 5
_TEXT_STEP, _MENU_STEP = 5, 1

# Colour by what a beat DOES, so the outline is readable at a glance (kept from the Timeline view).
_CAT_COLOUR = {
    "objective": _GOLD_BRT, "win": "#7fd17f", "lose": "#e06a6a", "economy": "#6fb0d8",
    "units": "#e0a44a", "tech": "#b08fd8", "ai": "#d88f6f", "score": _GOLD,
    "av": "#7a8aa0", "ui": "#7a8aa0", "time": "#6fd8c0", "state": "#9aa0b0",
    "setup": "#5a6a80", "flow": _GOLD, "wait": "#6fd8c0", "other": "#8a98ac",
}
# The name prefixes parse_flow strips. Resolving them back is what gives 99.4% of nodes a catalog entry.
_PREFIXES = ("", "Descriptor", "Condition", "Variable", "Tag", "Operator")

# Fields a GameRulesChallenge accepts but the engine then forces off — shown, but never as if they work.
_INERT_ON_CHALLENGE = ("MinScore", "MaxScore", "UseScore")


def _humanise(type_name):
    """A readable name for a class the catalog has no friendly label for yet.

    Only 43 of the 1,133 DSL classes are curated, so most beats would otherwise read as
    `DescriptorAddCampHqToUnitGroup`. Stripping the prefix and splitting the camel case gives
    "Add camp hq to unit group" — not as good as a written label, but never a raw class name."""
    import re as _re
    n = type_name
    for p in ("Descriptor", "Condition", "Variable", "Operator", "Tag"):
        if n.startswith(p) and len(n) > len(p):
            n = n[len(p):]
            break
    words = _re.sub(r"(?<!^)(?=[A-Z][a-z])|(?<=[a-z])(?=[A-Z])", " ", n).split()
    return " ".join(words).capitalize() if words else type_name


def _catalog_record(type_name):
    for p in _PREFIXES:
        rec = catalog.info_for(p + type_name)
        if rec:
            return rec, p + type_name
    return None, type_name


def _display_name(type_name):
    """The name to show for a beat.

    An UNCURATED catalog entry stores `label` equal to the class name, so taking the label blindly puts
    `DescriptorHudElementsVisibility` on screen. Every class a shipped mission uses is curated now, but the
    other ~950 in the catalog are not, so the label is used only when it is actually a label and everything
    else is humanised."""
    rec, full = _catalog_record(type_name)
    label = (rec or {}).get("label")
    if label and label != full:
        return label
    return _humanise(full)


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


class MissionScriptFrame(tk.Frame):
    def __init__(self, parent, project, on_go=None, **_kw):
        super().__init__(parent, background=_BG)
        self.project = project
        self.binding = None
        self._store = _ProjStore(project)
        self._on_go = on_go
        self._src = ""            # decompiled source, the single source of truth
        self._path = None         # archive path of the .xyz
        self._irs = {}            # IR name -> {type, body}
        self._nodes = {}          # tree iid -> flow node
        self._sel = None
        self._vars = {}           # kwarg -> (var, ir)
        self._dirty = False
        self._build(on_go)

    # ── layout ───────────────────────────────────────────────────────────────────────────────────
    def _build(self, on_go):
        self.step = ui_util.WalkStep(
            self, index=STEP_INDEX, total=STEP_TOTAL, title=t("script.step_title"),
            decides=t("script.step_decides"), on_go=on_go, translate=t).pack()

        # rules + how it ends, above the outline: they are the frame everything else sits in
        top = tk.Frame(self, background=_BG)
        top.pack(fill="x", padx=8)
        self._rules = tk.Label(top, text="", background=_BG, foreground=_TEXT, font=_FS,
                               anchor="w", justify="left", wraplength=1080)
        self._rules.pack(fill="x")
        self._ends = tk.Frame(top, background=_BG)
        self._ends.pack(fill="x", pady=(2, 4))

        body = tk.Frame(self, background=_BG)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        left = tk.Frame(body, background=_BG, width=380)
        left.pack(side="left", fill="both")
        left.pack_propagate(False)
        self._tree = ttk.Treeview(left, show="tree", selectmode="browse")
        ui_util.with_scrollbars(left, self._tree)
        self._tree.bind("<<TreeviewSelect>>", self._on_pick)
        for cat, colour in _CAT_COLOUR.items():
            self._tree.tag_configure(cat, foreground=colour)

        mid = tk.Frame(body, background=_BG, width=380)
        mid.pack(side="left", fill="both", padx=(8, 0))
        mid.pack_propagate(False)
        self._what = tk.Label(mid, text=t("script.pick_a_beat"), background=_BG, foreground=_GOLD_BRT,
                              font=_FB, anchor="w", justify="left", wraplength=360)
        self._what.pack(fill="x")
        self._help = tk.Label(mid, text="", background=_BG, foreground=_DIM, font=_FS,
                              anchor="w", justify="left", wraplength=360)
        self._help.pack(fill="x", pady=(2, 4))
        self._fields = ui_util.make_scrollable(mid, bg=_BG)

        right = tk.Frame(body, background=_BG)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        tk.Label(right, text=t("script.source_header"), background=_BG, foreground=_GOLD,
                 font=_FS, anchor="w").pack(fill="x")
        tholder = tk.Frame(right, background=_BG)
        tholder.pack(fill="both", expand=True)
        self._text = tk.Text(tholder, background=_WIDGET, foreground=_TEXT, insertbackground=_GOLD_BRT,
                             font=("Consolas", 9), wrap="none", relief="flat", undo=True)
        ui_util.with_scrollbars(tholder, self._text)
        self._text.bind("<<Modified>>", self._on_typed)

        bar = tk.Frame(self, background=_PANEL)
        bar.pack(side="bottom", fill="x")
        self._b_apply = self._btn(bar, t("script.apply_field"), self._apply_fields)
        self._b_save = self._btn(bar, t("script.save_script"), self._save)
        self._b_check = self._btn(bar, t("script.check"), self._check)
        ui_util.flow(bar, [self._b_apply, self._b_save, self._b_check])

    @staticmethod
    def _btn(parent, label, command):
        return tk.Button(parent, text=label, command=command, background=theme.BTN,
                         foreground=_GOLD_BRT, activebackground=theme.BTN_ACT, font=_FB,
                         relief="flat", padx=10)

    # ── binding-driven load ──────────────────────────────────────────────────────────────────────
    def set_binding(self, binding):
        self.binding = binding
        self._src, self._path, self._irs, self._sel = "", None, {}, None
        self._dirty = False
        for i in self._tree.get_children():
            self._tree.delete(i)
        self._nodes = {}
        self._text.delete("1.0", "end")
        self._clear_fields()
        for w in self._ends.winfo_children():
            w.destroy()
        self._rules.config(text="")
        if binding is None:
            self.step.set_status(ui_util.STEP_IDLE, t("script.no_scenario"))
            return
        try:
            self._load()
        except Exception as e:
            self.step.set_status(ui_util.STEP_BAD, t("script.load_failed", e=e))

    def _load(self):
        b = self.binding
        tree = chain.walk(self._store, b.map_dir, b.scenario_name)
        hit = next((n for n in tree.walk() if n.kind == "script" and n.status == chain.OK), None)
        if hit is None:
            # An MP map legitimately has none: it runs the shared effetmapmultiplayer rules.
            self._rules.config(text=t("script.mp_shared_rules") if b.kind == "mp"
                               else t("script.no_script_for_kind"))
            self.step.set_status(ui_util.STEP_WARN if b.kind == "mp" else ui_util.STEP_BAD,
                                 t("script.no_script"))
            return
        self._path = hit.ref
        self._src = SL.decompile_xyz(self._store.get_raw("scripts", self._path))
        self._irs = SL.ir_assignments(self._src)
        self._text.delete("1.0", "end")
        self._text.insert("1.0", self._src)
        self._text.edit_modified(False)
        self._render_rules()
        self._render_ends()
        self._render_outline()
        self.step.set_status(ui_util.STEP_OK, t("script.loaded", n=len(self._nodes),
                                                lines=self._src.count("\n") + 1))

    # ── the rules, said honestly ─────────────────────────────────────────────────────────────────
    def _render_rules(self):
        rules = SL.parse_gamerules(self._src)
        if not rules:
            self._rules.config(text=t("script.no_rules_found"))
            return
        sc = rules.get("scalars", {})
        time = sc.get("GameTime")
        pretty = SL.secs_to_mmss(int(time)) if (time or "").lstrip("-").isdigit() else time
        base = t("script.rules_line", type=rules["type"], time=pretty,
                 mode=(rules.get("enums", {}).get("GameTimeMode", "") or "").rsplit(".", 1)[-1])
        if rules["type"] == "GameRulesChallenge":
            inert = [k for k in _INERT_ON_CHALLENGE if k in sc]
            base += "  " + t("script.rules_inert", fields=", ".join(inert) or "score")
        self._rules.config(text=base)

    # ── how it ends ──────────────────────────────────────────────────────────────────────────────
    def _render_ends(self):
        for w in self._ends.winfo_children():
            w.destroy()
        ends = self._terminals()
        if not ends:
            tk.Label(self._ends, text=t("script.no_terminal"), background=_BG, foreground=theme.RED,
                     font=_FS, anchor="w", wraplength=1080, justify="left").pack(fill="x")
            return
        row = tk.Frame(self._ends, background=_BG)
        row.pack(fill="x")
        tk.Label(row, text=t("script.ends_with"), background=_BG, foreground=_GOLD, font=_FS,
                 anchor="w").pack(side="left")
        for ir, typ, chapter in ends:
            label = {"DescriptorDeclencheVictoire": t("script.end_victory_campaign"),
                     "DescriptorDeclencheVictoireChallenge": t("script.end_victory_op"),
                     "DescriptorDeclencheDefaite": t("script.end_defeat")}.get(typ, typ)
            if chapter is not None:
                label += " " + t("script.next_mission", n=chapter)
            tk.Label(row, text=" [%s]" % label, background=_BG, font=_FS,
                     foreground=("#7fd17f" if "Victoire" in typ else "#e06a6a")).pack(side="left")
        if any(c is not None for _i, _t, c in ends) and self._on_go is not None:
            self._btn(row, t("script.check_order"), lambda: self._on_go(_MENU_STEP)).pack(side="right")

    def _terminals(self):
        """(ir, type, Chapter) for every terminal descriptor. These are the ONLY ways a solo mission
        ends, and Chapter is what the Next Mission button launches."""
        out = []
        for ir, info in self._irs.items():
            typ = info["type"].rsplit(".", 1)[-1]
            if "Declenche" not in typ:
                continue
            kw = SL.scalar_kwargs(info["body"])
            ch = kw.get("Chapter")
            out.append((ir, typ, int(ch) if (ch or "").isdigit() else None))
        return out

    # ── the outline ──────────────────────────────────────────────────────────────────────────────
    def _render_outline(self):
        for i in self._tree.get_children():
            self._tree.delete(i)
        self._nodes = {}
        root = SL.parse_flow(self._src)
        if root:
            self._insert_node(root, "")

    def _insert_node(self, node, parent):
        cat = node.get("cat") or {"flow": "flow", "wait": "wait"}.get(node.get("kind"), "other")
        iid = self._tree.insert(parent, "end", text=self._node_label(node), tags=(cat,),
                                open=(self._tree.get_children(parent) == () and parent == ""))
        self._nodes[iid] = node
        for c in node.get("children") or []:
            self._insert_node(c, iid)
        return iid

    @staticmethod
    def _node_label(node):
        ty = node.get("type") or "?"
        name = _display_name(ty)
        if node.get("duree"):
            return "%s  %ss" % (name, node["duree"])
        if node.get("condition_type"):
            return "%s: %s" % (name, node["condition_type"])
        return name

    # ── the guided pane ──────────────────────────────────────────────────────────────────────────
    def _clear_fields(self):
        for w in self._fields.winfo_children():
            w.destroy()
        self._vars = {}

    def _on_pick(self, _=None):
        sel = self._tree.selection()
        if not sel:
            return
        node = self._nodes.get(sel[0])
        if node is None:
            return
        self._sel = node
        self._show_node(node)
        self._scroll_to(node.get("ir"))

    def _show_node(self, node):
        self._clear_fields()
        ty = node.get("type") or "?"
        rec, full = _catalog_record(ty)
        self._what.config(text=_display_name(ty))
        self._help.config(text=((rec or {}).get("help") or t("script.no_help", cls=full)))

        info = self._irs.get(node.get("ir"))
        if info is None:
            return
        # Both halves of a block's simple fields: scalar_kwargs cannot see dotted enum values, so on its
        # own it hides the nation, difficulty, AI level and behaviour settings entirely.
        kw = SL.scalar_kwargs(info["body"])
        enums = SL.enum_kwargs(info["body"])
        kw.update(enums)
        params = {p.get("name"): p for p in ((rec or {}).get("params") or [])}
        for name, value in kw.items():
            spec = params.get(name, {})
            row = tk.Frame(self._fields, background=_BG)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=name, background=_BG, foreground=_GOLD, font=_FS,
                     width=18, anchor="w").pack(side="left")
            # A `loc` param is a LocHash: its WORDS are edited on the Text step, so do not offer a box
            # that looks like it edits text when it only edits a key.
            if spec.get("type") == "loc":
                tk.Label(row, text=t("script.text_on_step"), background=_BG, foreground=_DIM,
                         font=_FS, anchor="w").pack(side="left", fill="x", expand=True)
                if self._on_go is not None:
                    self._btn(row, t("script.open_text"),
                              lambda: self._on_go(_TEXT_STEP)).pack(side="right")
                continue

            var = tk.StringVar(value=value)
            members = self._enum_members(spec, name in enums and value or None)
            if spec.get("type") == "bool" or value in ("True", "False"):
                # A yes/no field is a tick box. The variable still holds the SOURCE text, so applying an
                # edit stays the same single code path as every other field.
                tk.Checkbutton(row, variable=var, onvalue="True", offvalue="False", background=_BG,
                               activebackground=_BG, selectcolor=_WIDGET,
                               foreground=_TEXT).pack(side="left")
            elif members:
                prefix = value.rsplit(".", 1)[0]
                show = tk.StringVar(value=value.rsplit(".", 1)[-1])
                cb = ttk.Combobox(row, textvariable=show, values=members, state="readonly", width=22)
                cb.pack(side="left", fill="x", expand=True)
                cb.bind("<<ComboboxSelected>>",
                        lambda _e, s=show, v=var, pre=prefix: v.set("%s.%s" % (pre, s.get())))
            else:
                ttk.Entry(row, textvariable=var, width=18).pack(side="left", fill="x", expand=True)
            if spec.get("help"):
                tk.Label(row, text=spec["help"], background=_BG, foreground=_DIM, font=_FS,
                         anchor="w", wraplength=220).pack(side="left", padx=(6, 0))
            self._vars[name] = (var, node.get("ir"))

    def _enum_members(self, spec, dotted_value):
        """The choices for an enum field, or [] if it is not one we know the domain for.

        The domain is taken from the curated parameter when it names one, otherwise from the value already
        in the script — `_enum_for_game_play.Nationalite.EU` names its own domain, so a field can offer the
        right list even where the catalog does not spell it out.
        """
        dom = spec.get("enum_domain")
        if not dom and dotted_value and dotted_value.count(".") >= 2:
            dom = dotted_value.rsplit(".", 2)[-2]
        if not dom:
            return []
        try:
            return list(catalog.enum_members(dom) or [])
        except Exception:
            return []

    def _scroll_to(self, ir):
        if not ir:
            return
        idx = self._text.search(r"^%s\s*=" % ir, "1.0", stopindex="end", regexp=True)
        if idx:
            self._text.see(idx)
            self._text.tag_remove("sel", "1.0", "end")
            self._text.tag_add("sel", idx, "%s lineend" % idx)

    # ── edits ────────────────────────────────────────────────────────────────────────────────────
    def _on_typed(self, _=None):
        if self._text.edit_modified():
            self._dirty = True
            self._text.edit_modified(False)

    def _apply_fields(self):
        """Write the guided pane's values INTO THE SOURCE, so the form and the text are one edit."""
        if not self._vars:
            return
        src = self._text.get("1.0", "end-1c")
        n = 0
        for name, (var, ir) in self._vars.items():
            new = var.get().strip()
            if not new:
                continue
            # A dotted value is an enum and has its own setter: set_kwarg's value pattern does not accept
            # dotted values, so sending one there would silently change nothing.
            try:
                if re.match(r"^[A-Za-z_]\w*(?:\.\w+)+$", new):
                    out = SL.set_enum_field(src, ir, name, new)
                else:
                    out = SL.set_kwarg(src, ir, name, new)
            except Exception:
                continue
            if out != src:
                src, n = out, n + 1
        if n:
            self._text.delete("1.0", "end")
            self._text.insert("1.0", src)
            self._src = src
            self._irs = SL.ir_assignments(src)
            self._dirty = True
            self._render_rules()
            self._render_ends()
            self.step.set_status(ui_util.STEP_OK, t("script.applied", n=n))
        else:
            self.step.set_status(ui_util.STEP_WARN, t("script.applied_none"))

    def _check(self):
        """Read-only pass: compile the source and bind its tags against the placements, without writing."""
        src = self._text.get("1.0", "end-1c")
        problems = []
        try:
            SL.compile_source(src)
        except Exception as e:
            self.step.set_status(ui_util.STEP_BAD, t("script.wont_compile", e=str(e)[:160]))
            return
        if not [1 for _ir, typ, _c in self._terminals_of(src) if "Declenche" in typ]:
            problems.append(t("script.problem_no_terminal"))
        rules = SL.parse_gamerules(src)
        if rules and rules["type"] == "GameRulesChallenge":
            hot = [k for k in _INERT_ON_CHALLENGE if k in rules.get("scalars", {})]
            if hot:
                problems.append(t("script.problem_inert", fields=", ".join(hot)))
        if problems:
            self.step.set_status(ui_util.STEP_WARN, "  ".join(problems))
        else:
            self.step.set_status(ui_util.STEP_OK, t("script.check_ok"))

    def _terminals_of(self, src):
        out = []
        for ir, info in SL.ir_assignments(src).items():
            typ = info["type"].rsplit(".", 1)[-1]
            if "Declenche" in typ:
                kw = SL.scalar_kwargs(info["body"])
                ch = kw.get("Chapter")
                out.append((ir, typ, int(ch) if (ch or "").isdigit() else None))
        return out

    def _save(self):
        """Recompile and stage. Gated on the script COMPILING — writing a .xyz the game cannot load is
        the one failure that takes the whole mission down, so it is never staged unchecked."""
        if self._path is None:
            return
        src = self._text.get("1.0", "end-1c")
        try:
            xyz = SL.recompile_source_to_xyz(src, xyz_for_meta=self._store.get_raw("scripts", self._path))
        except Exception as e:
            self.step.set_status(ui_util.STEP_BAD, t("script.wont_compile", e=str(e)[:160]))
            return
        self.project.set_raw("scripts", self._path, xyz)
        self._src = src
        self._irs = SL.ir_assignments(src)
        self._dirty = False
        self._render_outline()
        self.step.set_status(ui_util.STEP_OK, t("script.saved"))

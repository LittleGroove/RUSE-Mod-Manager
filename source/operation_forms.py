"""Catalog-driven tkinter form factory for the Mission Logic editor.

Given a DSL class name, `BlockForm` renders one widget per catalog param (`dsl_catalog.params_for`) chosen
by the param TYPE — bool->Checkbutton, int/float/text/loc->Entry, enum->Combobox, tag->placement picker,
camp_ref/ref->Combobox, unit_type->unit-catalog picker, block_ref->recursive sub-block builder, list_of->
add/remove list. `read()` returns a `{param: value/Ref/Block/[...]}` dict ready for an `operation_script.Block`.
This is the reusable core behind the Objectives / Events / AI panels and the generic "advanced block" form;
the future node-graph can reuse the same widgets.

A `Providers` bundle supplies the live choices (placement Names/zones, camp vars, declared variables) from
the bound scenario so pickers show real handles instead of free text.
"""
import tkinter as tk
from tkinter import ttk

import ui_util
from i18n import t as _t

from ruse_mod_engine import operation_script as ops
from ruse_mod_engine import dsl_catalog
try:
    from ruse_mod_engine import placement_catalog as pcat
except Exception:
    pcat = None

import theme                # single source of truth for the palette; local names kept unchanged
_PANEL = theme.PANEL; _WIDGET = theme.WIDGET; _TEXT = theme.TEXT; _DIM = theme.DIM
_GOLD = theme.GOLD; _GOLD_BRT = theme.GOLD_BRT; _SEL = theme.SEL_BG
_F = theme.F; _FB = theme.FB

# block_kind -> catalog kinds offered in the block picker
_BLOCK_KINDS = {"condition": {"condition"}, "action": {"action", "control", "ai", "objective"},
                "operator": {"operator"}, "label": {"action"}}
_ELEM_TO_BLOCK = {"action": "action", "condition": "condition"}
_ELEM_TO_TAG = {"tag_pos": "pos", "tag_zone": "zone", "tag_unit": "unit"}


class Providers:
    """Live choice sources from the bound scenario. Each is a zero/one-arg callable returning a list."""
    def __init__(self, tags=None, camps=None, variables=None):
        self._tags = tags or (lambda kind: [])
        self._camps = camps or (lambda: [])
        self._vars = variables or (lambda kind: [])

    def tags(self, kind):       # kind = 'pos'|'zone'|'unit'
        try:
            return list(self._tags(kind))
        except Exception:
            return []

    def camps(self):
        try:
            return list(self._camps())
        except Exception:
            return []

    def variables(self, kind):  # kind = 'var'|'group'|'timer'
        try:
            return list(self._vars(kind))
        except Exception:
            return []


def _mk_label(parent, text, gold=False):
    return tk.Label(parent, text=text, background=_PANEL, foreground=(_GOLD if gold else _TEXT),
                    font=_F, anchor="w", justify="left")


def _mk_entry(parent, var, width=20):
    return tk.Entry(parent, textvariable=var, width=width, background=_WIDGET, foreground=_TEXT,
                    insertbackground=_TEXT, font=_F, relief="flat", highlightthickness=1,
                    highlightbackground=_SEL, highlightcolor=_GOLD)


def _mk_combo(parent, var, values, width=18):
    cb = ttk.Combobox(parent, textvariable=var, values=list(values), font=_F, width=width)
    return cb


def _mk_btn(parent, text, cmd, gold=False):
    return tk.Button(parent, text=text, command=cmd, background="#163048" if gold else "#122030",
                     foreground=_GOLD_BRT if gold else _TEXT, font=_FB, relief="flat")


class BlockForm(tk.Frame):
    """A scrollable form for one DSL class. Lays out param rows; `read()` -> {param: value}."""
    def __init__(self, parent, class_name, providers, values=None):
        super().__init__(parent, background=_PANEL)
        self.class_name = class_name
        self.prov = providers
        self.params = dsl_catalog.params_for(class_name)
        self._rows = {}        # param name -> ("kind", getter_callable)
        values = values or {}
        rec = dsl_catalog.info_for(class_name) or {}
        if rec.get("help"):
            tk.Label(self, text=rec["help"], background=_PANEL, foreground=_DIM, font=_F, anchor="w",
                     wraplength=440, justify="left").pack(anchor="w", padx=4, pady=(2, 4))
        for p in self.params:
            self._build_row(p, values.get(p["name"]))

    # ── row builders by param type ───────────────────────────────────────────────────
    def _row_frame(self, p):
        fr = tk.Frame(self, background=_PANEL); fr.pack(fill="x", padx=4, pady=1)
        req = " *" if p.get("required") else ""
        _mk_label(fr, p["name"] + req + ":").pack(side="left")
        return fr

    def _help(self, fr, p):
        if p.get("help"):
            tk.Label(self, text="   " + p["help"], background=_PANEL, foreground=_DIM, font=_F,
                     anchor="w", wraplength=420, justify="left").pack(anchor="w", padx=4)

    def _build_row(self, p, val):
        t = p.get("type", "object")
        name = p["name"]
        if t == "bool":
            fr = self._row_frame(p)
            v = tk.BooleanVar(value=bool(val) if val is not None else bool(p.get("default")))
            tk.Checkbutton(fr, variable=v, background=_PANEL, activebackground=_PANEL,
                           selectcolor=_WIDGET).pack(side="left")
            self._rows[name] = ("bool", lambda v=v: v.get())
        elif t in ("int", "float", "text", "loc"):
            fr = self._row_frame(p)
            sval = "" if val is None else str(val)
            var = tk.StringVar(value=sval)
            _mk_entry(fr, var, width=24 if t in ("text", "loc") else 12).pack(side="left")
            self._rows[name] = (t, lambda var=var, t=t: _coerce(var.get(), t))
        elif t == "enum":
            fr = self._row_frame(p)
            members = dsl_catalog.enum_members(p.get("enum_domain") or "")
            var = tk.StringVar(value=val or (p.get("default") or ""))
            _mk_combo(fr, var, members).pack(side="left")
            if not dsl_catalog.enum_is_complete(p.get("enum_domain") or ""):
                tk.Label(fr, text=_t("op.type_if_not_listed"), background=_PANEL, foreground=_DIM,
                         font=_F).pack(side="left", padx=4)
            self._rows[name] = ("enum", lambda var=var: var.get().strip() or None)
        elif t == "tag":
            fr = self._row_frame(p)
            tagk = p.get("tag_kind", "pos")
            var = tk.StringVar(value=(val.name if isinstance(val, ops.Ref) else (val or "")))
            _mk_combo(fr, var, self.prov.tags(tagk)).pack(side="left")
            self._rows[name] = ("tag", lambda var=var, tagk=tagk: _ref_tag(var.get(), tagk))
        elif t == "camp_ref":
            fr = self._row_frame(p)
            var = tk.StringVar(value=(val or ""))
            _mk_combo(fr, var, self.prov.camps()).pack(side="left")
            self._rows[name] = ("camp", lambda var=var: var.get().strip() or None)
        elif t == "ref":
            fr = self._row_frame(p)
            rk = p.get("ref_kind", "var")
            var = tk.StringVar(value=(val.name if isinstance(val, ops.Ref) else (val or "")))
            _mk_combo(fr, var, self.prov.variables(rk)).pack(side="left")
            self._rows[name] = ("ref", lambda var=var, rk=rk: _ref_var(var.get(), rk))
        elif t == "unit_type":
            fr = self._row_frame(p)
            var = tk.StringVar(value=(val or ""))
            _mk_entry(fr, var, width=22).pack(side="left")
            _mk_btn(fr, _t("op.pick"), lambda var=var: _pick_unit(self, var)).pack(side="left", padx=3)
            self._rows[name] = ("unit", lambda var=var: var.get().strip() or None)
        elif t == "block_ref":
            self._rows[name] = self._build_block_ref(p, val)
        elif t == "list_of":
            self._rows[name] = self._build_list(p, val)
        else:                                            # object / unknown -> raw expression entry
            fr = self._row_frame(p)
            var = tk.StringVar(value=("" if val is None else str(val)))
            _mk_entry(fr, var, width=22).pack(side="left")
            tk.Label(fr, text=_t("op.advanced_raw"), background=_PANEL, foreground=_DIM, font=_F).pack(side="left")
            self._rows[name] = ("object", lambda var=var: (var.get().strip() or None))
        self._help(None, p)

    def _build_block_ref(self, p, val):
        fr = self._row_frame(p)
        state = {"block": val if isinstance(val, ops.Block) else None}
        summ = tk.Label(fr, text=_summ(state["block"]), background=_WIDGET, foreground=_TEXT, font=_F,
                        anchor="w", width=28)
        summ.pack(side="left")

        def edit():
            blk = block_picker_dialog(self, p.get("block_kind", "action"), self.prov, state["block"])
            if blk is not None:
                state["block"] = blk; summ.config(text=_summ(blk))

        def clear():
            state["block"] = None; summ.config(text=_summ(None))
        _mk_btn(fr, _t("common.edit_2"), edit).pack(side="left", padx=3)
        _mk_btn(fr, "✕", clear).pack(side="left")
        return ("block", lambda: state["block"])

    def _build_list(self, p, val):
        fr = tk.Frame(self, background=_PANEL); fr.pack(fill="x", padx=4, pady=1)
        _mk_label(fr, p["name"] + (" *" if p.get("required") else "") + ":").pack(anchor="w")
        elem = p.get("elem", "object")
        items = list(val) if isinstance(val, list) else []
        lb = tk.Listbox(fr, height=3, background=_WIDGET, foreground=_TEXT, font=_F,
                        selectbackground=_SEL, highlightthickness=0)
        lb.pack(side="left", fill="x", expand=True)
        for it in items:
            lb.insert("end", _summ(it) if isinstance(it, ops.Block) else str(getattr(it, "name", it)))
        bb = tk.Frame(fr, background=_PANEL); bb.pack(side="left", padx=3)

        def add():
            it = _make_elem(self, elem, self.prov)
            if it is not None:
                items.append(it)
                lb.insert("end", _summ(it) if isinstance(it, ops.Block) else str(getattr(it, "name", it)))

        def remove():
            sel = lb.curselection()
            if sel:
                items.pop(sel[0]); lb.delete(sel[0])
        _mk_btn(bb, _t("op.add"), add).pack(fill="x")
        _mk_btn(bb, _t("op.rem"), remove).pack(fill="x", pady=2)
        return ("list", lambda: list(items))

    def read(self):
        """{param: value} for the params the user set (scalars with values, plus any block/ref/list).
        Unset params are omitted — the generator fills them from catalog defaults."""
        out = {}
        for name, (kind, getter) in self._rows.items():
            v = getter()
            if kind == "bool":
                out[name] = v
            elif kind == "list":
                if v:
                    out[name] = v
            elif v is not None:
                out[name] = v
        return out

    def errors(self):
        missing = []
        vals = self.read()
        for p in self.params:
            if p.get("required") and p["name"] not in vals:
                missing.append(p["name"])
        return missing


# ── value coercion / ref builders ────────────────────────────────────────────────────
def _coerce(s, t):
    s = (s or "").strip()
    if s == "":
        return None
    if t == "int":
        try:
            return int(s)
        except ValueError:
            return None
    if t == "float":
        try:
            return float(s)
        except ValueError:
            return None
    return s


def _ref_tag(name, tag_kind):
    name = (name or "").strip()
    return ops.Ref("tag_" + tag_kind, name) if name else None


def _ref_var(name, ref_kind):
    return (name or "").strip() or None   # emitter treats ref values as raw declared-var names


def _summ(block):
    if not isinstance(block, ops.Block):
        return _t("common.none")
    rec = dsl_catalog.info_for(block.cls) or {}
    return rec.get("label", block.cls)


def _make_elem(parent, elem, prov):
    """Create one list element of the given elem kind."""
    if elem in _ELEM_TO_BLOCK:
        return block_picker_dialog(parent, _ELEM_TO_BLOCK[elem], prov)
    if elem in _ELEM_TO_TAG:
        name = _simple_pick(parent, _t("op.tag_elem", elem=elem), prov.tags(_ELEM_TO_TAG[elem]))
        return ops.Ref(elem, name) if name else None
    if elem == "unit_type":
        var = tk.StringVar()
        if _pick_unit(parent, var):
            return var.get() or None
        return None
    return _simple_text(parent, _t("common.value"))


# ── dialogs ──────────────────────────────────────────────────────────────────────────
def _modal(parent, title, w=480, h=440):
    # One display framework: delegate to ui_util.themed_toplevel instead of hand-rolling a Toplevel, so
    # these op-editor pickers share the exact same theming, centring, minsize floor and (with)draw→reveal
    # path as every other popup.  The caller still fills `top` with content and calls wait_window(top);
    # on_escape stays False so each dialog keeps its own Cancel/close behaviour.
    return ui_util.themed_toplevel(parent, title, size=(w, h), modal=True, resizable=True)


def pick_class_dialog(parent, kinds=None, title=None):
    """Searchable class picker (curated first). kinds = set of catalog kinds, or None for ALL 1,133."""
    if title is None:
        title = _t("op.pick_block")
    top = _modal(parent, title, 460, 420)
    result = {"cls": None}
    qv = tk.StringVar()
    bar = tk.Frame(top, background=_PANEL); bar.pack(fill="x", padx=6, pady=4)
    _mk_label(bar, _t("common.search")).pack(side="left")
    _mk_entry(bar, qv, width=30).pack(side="left", padx=4)
    lb = tk.Listbox(top, background=_WIDGET, foreground=_TEXT, font=_F, selectbackground=_SEL,
                    highlightthickness=0)
    lb.pack(fill="both", expand=True, padx=6, pady=4)
    names = []

    def refresh(*_):
        lb.delete(0, "end"); names.clear()
        q = qv.get().strip()
        seen = set()
        for kind in (sorted(kinds) if kinds else [None]):
            for nm, rec in dsl_catalog.search(kind=kind, query=q, limit=400):
                if nm in seen:
                    continue
                seen.add(nm); names.append(nm)
                star = "★ " if rec.get("curated") else "   "
                lb.insert("end", "%s%s  —  %s" % (star, rec.get("label", nm), nm))
    qv.trace_add("write", refresh); refresh()

    def ok():
        sel = lb.curselection()
        if sel:
            result["cls"] = names[sel[0]]; top.destroy()
    lb.bind("<Double-Button-1>", lambda e: ok())
    bb = tk.Frame(top, background=_PANEL); bb.pack(fill="x", padx=6, pady=6)
    _mk_btn(bb, _t("common.cancel"), top.destroy).pack(side="right")
    _mk_btn(bb, _t("op.select"), ok, gold=True).pack(side="right", padx=4)
    parent.wait_window(top)
    return result["cls"]


def block_picker_dialog(parent, block_kind, providers, existing=None):
    """Pick a class (filtered by block_kind) then fill its form -> ops.Block (or None)."""
    cls = existing.cls if isinstance(existing, ops.Block) else \
        pick_class_dialog(parent, _BLOCK_KINDS.get(block_kind), _t("op.pick_block_kind", block_kind=block_kind))
    if not cls:
        return None
    top = _modal(parent, _summ(ops.Block(cls)), 500, 480)
    result = {"block": None}
    canvas = tk.Canvas(top, background=_PANEL, highlightthickness=0)
    sb = ttk.Scrollbar(top, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas, background=_PANEL)
    inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=inner, anchor="nw"); canvas.configure(yscrollcommand=sb.set)
    canvas.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
    form = BlockForm(inner, cls, providers, values=(existing.params if isinstance(existing, ops.Block) else None))
    form.pack(fill="both", expand=True)

    def ok():
        result["block"] = ops.Block(cls, form.read()); top.destroy()
    bb = tk.Frame(top, background=_PANEL); bb.pack(side="bottom", fill="x", padx=6, pady=6)
    _mk_btn(bb, _t("common.cancel"), top.destroy).pack(side="right")
    _mk_btn(bb, _t("common.ok"), ok, gold=True).pack(side="right", padx=4)
    parent.wait_window(top)
    return result["block"]


def _pick_unit(parent, var):
    """Unit/building class picker reusing the placement catalog (457 classes). Sets `var`."""
    if pcat is None:
        return _simple_text_into(parent, _t("op.unit_class_e_g_unit"), var)
    top = _modal(parent, _t("op.pick_unit_building"), 460, 420)
    ok = {"v": False}
    qv = tk.StringVar()
    bar = tk.Frame(top, background=_PANEL); bar.pack(fill="x", padx=6, pady=4)
    _mk_label(bar, _t("common.search")).pack(side="left"); _mk_entry(bar, qv, width=28).pack(side="left", padx=4)
    lb = tk.Listbox(top, background=_WIDGET, foreground=_TEXT, font=_F, selectbackground=_SEL,
                    highlightthickness=0)
    lb.pack(fill="both", expand=True, padx=6, pady=4)
    names = []

    def refresh(*_):
        lb.delete(0, "end"); names.clear()
        for nm, info in pcat.search(query=qv.get().strip(), limit=400):
            names.append(nm); lb.insert("end", "%s   (%s)" % (nm, info.get("category", "")))
    qv.trace_add("write", refresh); refresh()

    def choose():
        sel = lb.curselection()
        if sel:
            var.set(names[sel[0]]); ok["v"] = True; top.destroy()
    lb.bind("<Double-Button-1>", lambda e: choose())
    bb = tk.Frame(top, background=_PANEL); bb.pack(fill="x", padx=6, pady=6)
    _mk_btn(bb, _t("common.cancel"), top.destroy).pack(side="right")
    _mk_btn(bb, _t("op.select"), choose, gold=True).pack(side="right", padx=4)
    parent.wait_window(top)
    return ok["v"]


def _simple_pick(parent, title, values):
    top = _modal(parent, title, 320, 320)
    res = {"v": None}
    lb = tk.Listbox(top, background=_WIDGET, foreground=_TEXT, font=_F, selectbackground=_SEL)
    lb.pack(fill="both", expand=True, padx=6, pady=6)
    for v in values:
        lb.insert("end", v)

    def ok():
        sel = lb.curselection()
        if sel:
            res["v"] = values[sel[0]]
        top.destroy()
    _mk_btn(top, _t("op.select"), ok, gold=True).pack(side="bottom", pady=6)
    lb.bind("<Double-Button-1>", lambda e: ok())
    parent.wait_window(top)
    return res["v"]


def _simple_text(parent, title):
    top = _modal(parent, title, 320, 110)
    var = tk.StringVar(); res = {"v": None}
    _mk_entry(top, var, width=36).pack(padx=8, pady=8)

    def ok():
        res["v"] = var.get().strip() or None; top.destroy()
    _mk_btn(top, _t("common.ok"), ok, gold=True).pack(pady=4)
    parent.wait_window(top)
    return res["v"]


def _simple_text_into(parent, title, var):
    v = _simple_text(parent, title)
    if v is not None:
        var.set(v)
        return True
    return False

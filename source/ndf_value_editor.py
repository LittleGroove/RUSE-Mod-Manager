"""Shared NDF value-editing UI — the single home for editing any NDF property value.

This module owns the NDF value-type helpers (labels, formatting, parsing) AND `NdfValueEditorMixin`,
a set of dialogs that turn one `NdfValue` into another: scalar/byte/matrix authoring, the ObjRef /
TransRef target picker, List / Map / Pair / ZipBlob editors, and the LocHash localization manager.

The LocHash manager's LOGIC lives in `ruse_mod_engine.edits` (layer 2) — index, family resolution,
set-text-across-languages, mint, write-back — so the scenario editors perform the byte-identical
operation instead of reimplementing it.  Only the dialogs and the Tk refresh hooks stay here.

Any editor window (the Raw Asset Editor, the Units & Buildings editor, future editors) inherits the
mixin so they all edit values the SAME, already-tested way.  A host class must provide:

  self._v_ndf            -> the NdfBinary being edited
  self._store            -> a dat store exposing entry_paths/get_raw/set_raw/read_many/mark_dirty by
                            dat_key (used only by the LocHash manager for cross-dat .dic access); may be
                            a store whose "loc" dat isn't reachable, in which case LocHash falls back to
                            raw-hex editing
  self._loc_index        -> attribute the LocHash manager caches its LocIndex in (init to None)
  self._notify()         -> called after a .dic write (refresh/dirty hooks); no-op is fine
  self._update_status()  -> called after a .dic write; no-op is fine
  self._mk_text(parent)  -> returns (frame, Text widget) themed for multi-line editing
  self._mk_listbox(parent, **kw) -> a themed Listbox

Every dialog RETURNS a new NdfValue (or None if cancelled); the host commits it.  Nothing here mutates
the host's instance list or marks anything dirty except the LocHash .dic writes (which go through the
store and call _notify/_update_status).
"""
import os
import sys

import tkinter as tk
from tkinter import ttk

REPO = os.path.dirname(os.path.abspath(__file__))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
from ruse_mod_engine import ndfbin as ndfbin_mod    # noqa: E402
from ruse_mod_engine import dic as dic_mod          # noqa: E402
from ruse_mod_engine import edits as edits_mod      # noqa: E402
from i18n import t                                    # noqa: E402
import i18n                                           # noqa: E402
import ui_util                                        # noqa: E402
import theme                                          # noqa: E402

import theme                # single source of truth for the palette; local _R_* names kept unchanged
_R_BG, _R_BG_PANEL, _R_BG_WIDGET = theme.BG, theme.PANEL, theme.WIDGET
_R_GOLD, _R_GOLD_BRT = theme.GOLD, theme.GOLD_BRT
_R_TEXT, _R_TEXT_DIM = theme.TEXT, theme.DIM
_R_SEL_BG, _R_SEL_FG = theme.SEL_BG, theme.SEL_FG
_R_GREEN, _R_RED, _R_BORDER = theme.GREEN, theme.RED, theme.BORDER
_F_MAIN = theme.F
_F_BOLD = theme.FB
_F_HEAD = theme.FHEAD


_T = ndfbin_mod.T
_TYPE_NAMES = {
    _T.Bool: "Bool", _T.Int8: "Int8", _T.Int16: "Int16", _T.UInt16: "UInt16",
    _T.Int32: "Int32", _T.UInt32: "UInt32", _T.Long: "Long",
    _T.Float32: "Float32", _T.Float64: "Float64",
    _T.StringRef: "StringRef", _T.PathRef: "PathRef", _T.WideStr: "WideStr",
    _T.Vector3: "Vector3", _T.Color128: "Color128", _T.Color32: "Color32",
    # Container / complex types — friendly labels so the Type column & edit dialog
    # never show a bare "0x11".  These are NOT in _EDIT_TYPES (see below): they get
    # the List collection editor or a read-only info dialog, never the scalar box.
    _T.List: "List", _T.Map: "Map", _T.Reference: "ObjRef", _T.LocHash: "LocHash",
    _T.Blob: "Blob", _T.Guid: "Guid", _T.Time64: "Time64", _T.Matrix: "Matrix",
    _T.Pair: "Pair", _T.Hash: "Hash", _T.ZipBlob: "ZipBlob",
    _T.Int2: "Int2", _T.Float2: "Float2", _T.TripleInt: "TripleInt",
}
# Scalar types the scalar edit dialog can author.  Containers (List/Map/…) are handled
# elsewhere and deliberately excluded so they never open the scalar box.
_EDIT_TYPES = ["Bool", "Int8", "Int16", "UInt16", "Int32", "UInt32", "Long",
               "Float32", "Float64", "StringRef", "PathRef", "WideStr",
               "Vector3", "Color128", "Color32", "TripleInt", "Int2", "Float2"]
# Fixed byte length for hex-authored types (Blob is variable, so it's not here).
_BYTE_TYPE_LEN = {"Guid": 16, "Hash": 16, "LocHash": 8}
# Full set the (extended) scalar/simple dialog can author, including the byte/hex + Time64/Matrix
# types the old modding suite exposed.  Reference/Map/Pair/ZipBlob/List get dedicated editors.
_AUTHOR_TYPES = _EDIT_TYPES + ["Time64", "Matrix", "Guid", "Hash", "LocHash", "Blob"]
# Per-type entry hint shown in the scalar dialog so the expected format is obvious.
_TYPE_HINT = {
    "Bool": "0 or 1", "Vector3": "x, y, z", "Color128": "r, g, b, a  (floats)",
    "Color32": "r, g, b, a  (0-255)", "TripleInt": "a, b, c", "Int2": "a, b",
    "Float2": "a, b", "Matrix": "16 floats, comma-separated",
    "Guid": "32 hex chars", "Hash": "32 hex chars", "LocHash": "16 hex chars",
    "Blob": "hex bytes (any length)",
}
# Max characters a formatted value string shows in the Properties value column before it's capped
# (the full value is always available in the edit dialog).  Generous so whole lists/strings show.
_FMT_VAL_MAX = 800
# Element types a List collection editor can author per-row (single-cell scalars).
# List<T> of anything else (ObjRef, nested List/Map, Vector3/Color, …) is shown read-only.
_SCALAR_ELEM_TYPES = frozenset((
    _T.Bool, _T.Int8, _T.Int16, _T.UInt16, _T.Int32, _T.UInt32, _T.Long,
    _T.Float32, _T.Float64, _T.StringRef, _T.PathRef, _T.WideStr,
))


def _type_label(type_id: int) -> str:
    """Friendly name for an NDF type id (falls back to the engine's attribute name)."""
    return _TYPE_NAMES.get(type_id, ndfbin_mod.T.name(type_id))


# Ordered friendly names the List collection editor offers for its element type.
_SCALAR_ELEM_NAMES = [_TYPE_NAMES[t] for t in (
    _T.Bool, _T.Int8, _T.Int16, _T.UInt16, _T.Int32, _T.UInt32, _T.Long,
    _T.Float32, _T.Float64, _T.StringRef, _T.PathRef, _T.WideStr)]

def _fmt_val(val, ndf) -> str:
    OBJ = ndfbin_mod.OBJ_REF_MARKER
    TRF = ndfbin_mod.TRANS_REF_MARKER
    t, r = val.type_id, val.raw
    if t == _T.Reference:
        marker, ref = r
        if marker == OBJ:
            obj_idx, cls_idx = ref
            cn = next((c.name for c in ndf.classes if c.index == cls_idx), str(cls_idx))
            return f"ObjRef(inst={obj_idx}, {cn})"
        if marker == TRF:
            return f"TransRef({ref})"
        return f"Ref(0x{marker:08X})"
    if t == _T.List:
        if not r:
            return "[]"
        # Show the WHOLE list, not just the first few — the value column stretches with the window.
        # Only a pathologically long list is capped (by total length) so one row can't get absurd.
        parts, total = [], 0
        for i, x in enumerate(r):
            s = _fmt_val(x, ndf)
            parts.append(s)
            total += len(s) + 2
            if total > _FMT_VAL_MAX and i + 1 < len(r):
                return "[" + ", ".join(parts) + f" …+{len(r) - i - 1}]"
        return "[" + ", ".join(parts) + "]"
    if t == _T.Map:
        return f"Map{{{len(r)} entries}}"
    if t in (_T.StringRef, _T.PathRef):
        return repr(ndf.resolve_value(val))
    return repr(r)[:_FMT_VAL_MAX]

def _parse_val(raw: str, type_name: str):
    try:
        if type_name == "Bool":
            return 1 if raw.strip().lower() in ("1", "true", "yes", "on") else 0
        if type_name in ("Int8", "Int16", "UInt16", "Int32", "UInt32", "Long", "Time64"):
            return int(raw.strip())
        if type_name in ("Float32", "Float64"):
            return float(raw.strip())
        if type_name in ("StringRef", "PathRef", "WideStr"):
            return raw
        # Float-packed tuples (writer emits f32).  Int-packed tuples are handled below — mixing them
        # up is an EDIT-INDUCED corruption: Color32/TripleInt/Int2 serialize as ints (bytes()/i32),
        # so a float here raises on save.
        if type_name in ("Vector3", "Color128", "Float2", "Matrix"):
            vals = [float(x.strip()) for x in raw.strip("[]()").split(",")]
            if type_name == "Matrix" and len(vals) != 16:
                return None
            return vals
        if type_name in ("Color32", "TripleInt", "Int2"):
            return [int(float(x.strip())) for x in raw.strip("[]()").split(",")]
        # Byte types authored as a hex string (make_value's _bytes_from_value accepts hex / validates
        # length).  Blob is variable length; the others are fixed.
        if type_name in ("Guid", "Hash", "LocHash", "Blob"):
            hexs = raw.strip().replace(" ", "").replace("0x", "")
            int(hexs or "0", 16)                 # validates it's hex
            if len(hexs) % 2 != 0:
                return None
            need = _BYTE_TYPE_LEN.get(type_name)
            if need is not None and len(hexs) != need * 2:
                return None
            return hexs
    except Exception:
        pass
    return None

def _list_elem_info(pv):
    """Inspect a List NdfValue and decide how (or whether) its elements can be edited.

    Returns ``(elem_type_name, editable, reason)``:
      • empty list      → (None, True, "")                — user picks the element type
      • uniform scalar  → ("UInt32"/…, True, "")          — full per-row editing
      • mixed types     → (None, False, <reason>)         — read-only
      • uniform complex → (label, False, <reason>)         — read-only (ObjRef/nested/…)
    """
    elems = pv.value.raw or []
    if not elems:
        return None, True, ""
    type_ids = {e.type_id for e in elems}
    if len(type_ids) > 1:
        return None, False, t("tools.list_mixes_element_types_can")
    tid = next(iter(type_ids))
    if tid in _SCALAR_ELEM_TYPES:
        return _type_label(tid), True, ""
    return (_type_label(tid), False,
            t("tools.editing_lists_kind_isn_t",
              kind=_type_label(tid)))


class NdfValueEditorMixin:
    """Value-editing dialogs shared by every editor window (see module docstring for the host
    contract).  Each method takes/returns an NdfValue; the host commits the result."""

    # ── default host helpers — a host that already defines these keeps its own (MRO) ───────────────
    def _update_status(self):
        pass

    def _notify(self):
        pass

    def _mk_listbox(self, parent, **kw):
        return tk.Listbox(parent, activestyle="none", exportselection=False,
                          background=_R_BG_WIDGET, foreground=_R_TEXT, selectbackground=_R_SEL_BG,
                          selectforeground=_R_SEL_FG, font=_F_MAIN, relief="flat",
                          highlightthickness=1, highlightcolor=_R_BORDER, highlightbackground=_R_BORDER,
                          **kw)

    def _mk_text(self, parent):
        frame = tk.Frame(parent, background=_R_BG_WIDGET)
        txt = tk.Text(frame, wrap="none", background=_R_BG_WIDGET, foreground=_R_TEXT,
                      insertbackground=_R_GOLD, font=_F_MAIN, relief="flat",
                      highlightthickness=1, highlightcolor=_R_BORDER, highlightbackground=_R_BORDER)
        ysb = ttk.Scrollbar(frame, orient="vertical", command=txt.yview)
        xsb = ttk.Scrollbar(frame, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set, state="disabled")
        ysb.pack(side="right", fill="y")
        xsb.pack(side="bottom", fill="x")
        txt.pack(side="left", fill="both", expand=True)
        return frame, txt

    def _inst_pick_label(self, i, cls_by_idx):
        """'[idx] ClassName  stable-key' label for an instance in a picker/list."""
        inst = self._v_ndf.instances[i]
        cls = cls_by_idx.get(inst.class_index, f"cls#{inst.class_index}")
        sk = self._v_ndf.stable_key(inst)
        return f"[{i}] {cls}" + (f"  {sk[1]}" if sk else "")

    def _pick_instance(self, title, initial_idx=None, restrict_class_index=None):
        """Modal target picker: filter by class + free-text search, pick one instance.  Returns the
        chosen engine instance index, or None if cancelled.  Used for ObjRef repoint, List<ObjRef> add
        and the 'follow to a specific instance' flows."""
        ndf = self._v_ndf
        if ndf is None:
            return None
        cls_by_idx = {c.index: c.name for c in ndf.classes}
        class_names = sorted({c.name for c in ndf.classes})
        result = {"idx": None}
        CAP = 3000

        dlg = ui_util.themed_toplevel(self, title, min_size=(480, 460), resizable=True)
        pad = {"padx": 8, "pady": 4}
        top = tk.Frame(dlg, background=_R_BG_PANEL)
        top.pack(fill="x", **pad)
        tk.Label(top, text=t("tools.class"), background=_R_BG_PANEL, foreground=_R_TEXT,
                 font=_F_MAIN).pack(side="left")
        default_cls = t("tools.any")
        if initial_idx is not None and 0 <= initial_idx < len(ndf.instances):
            default_cls = cls_by_idx.get(ndf.instances[initial_idx].class_index, t("tools.any"))
        elif restrict_class_index is not None:
            default_cls = cls_by_idx.get(restrict_class_index, t("tools.any"))
        cls_var = tk.StringVar(value=default_cls)
        cls_cb = ttk.Combobox(top, textvariable=cls_var, values=[t("tools.any")] + class_names,
                              width=28, state="readonly")
        cls_cb.pack(side="left", padx=6)
        ui_util.fit_combobox(cls_cb)

        srow = tk.Frame(dlg, background=_R_BG_PANEL)
        srow.pack(fill="x", **pad)
        tk.Label(srow, text=t("text.search"), background=_R_BG_PANEL, foreground=_R_TEXT,
                 font=_F_MAIN).pack(side="left")
        search_var = tk.StringVar()
        ttk.Entry(srow, textvariable=search_var).pack(side="left", fill="x", expand=True, padx=6)

        count_lbl = tk.Label(dlg, text="", background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN)
        count_lbl.pack(anchor="w", **pad)
        lh = tk.Frame(dlg, background=_R_BG_PANEL)
        lh.pack(fill="both", expand=True, **pad)
        lb = self._mk_listbox(lh, selectmode="browse")
        ui_util.with_scrollbars(lh, lb)
        idx_map = []           # listbox row -> engine instance index

        def repopulate(*_):
            want_cls = cls_var.get()
            q = search_var.get().lower().strip()
            lb.delete(0, tk.END)
            idx_map.clear()
            shown = 0
            total = 0
            for i, inst in enumerate(ndf.instances):
                if want_cls != t("tools.any") and cls_by_idx.get(inst.class_index) != want_cls:
                    continue
                label = self._inst_pick_label(i, cls_by_idx)
                if q and q not in label.lower():
                    continue
                total += 1
                if shown < CAP:
                    lb.insert(tk.END, label)
                    idx_map.append(i)
                    if i == initial_idx:
                        lb.selection_clear(0, tk.END)
                        lb.selection_set(shown)
                        lb.see(shown)
                    shown += 1
            count_lbl.config(text=t("tools.showing_shown_total", shown=shown, total=total)
                             + (t("tools.narrow_search_see_more") if total > CAP else ""))

        cls_cb.bind("<<ComboboxSelected>>", repopulate)
        search_var.trace_add("write", lambda *_: repopulate())
        repopulate()

        def choose():
            sel = lb.curselection()
            if not sel:
                ui_util.info(dlg, title, t("tools.select_instance_first"))
                return
            result["idx"] = idx_map[sel[0]]
            dlg.destroy()

        lb.bind("<Double-Button-1>", lambda *_: choose())
        bf = tk.Frame(dlg, background=_R_BG_PANEL)
        bf.pack(fill="x", **pad)
        ttk.Button(bf, text=t("op.select"), command=choose).pack(side="left", padx=8)
        ttk.Button(bf, text=t("common.cancel"), command=dlg.destroy).pack(side="left", padx=8)
        dlg.bind("<Escape>", lambda *_: dlg.destroy())
        dlg.wait_window()
        return result["idx"]

    def _pick_class(self, title):
        """Pick a class name from this NDF's class table.  Returns the class name or None."""
        ndf = self._v_ndf
        if ndf is None:
            return None
        names = sorted({c.name for c in ndf.classes})
        result = {"name": None}
        dlg = ui_util.themed_toplevel(self, title, min_size=(380, 420), resizable=True)
        pad = {"padx": 8, "pady": 4}
        sv = tk.StringVar()
        ttk.Entry(dlg, textvariable=sv).pack(fill="x", **pad)
        lh = tk.Frame(dlg, background=_R_BG_PANEL)
        lh.pack(fill="both", expand=True, **pad)
        lb = self._mk_listbox(lh, selectmode="browse")
        ui_util.with_scrollbars(lh, lb)
        shown = []

        def repop(*_):
            q = sv.get().lower().strip()
            lb.delete(0, tk.END)
            shown.clear()
            for n in names:
                if not q or q in n.lower():
                    lb.insert(tk.END, n)
                    shown.append(n)
        sv.trace_add("write", lambda *_: repop())
        repop()

        def choose():
            sel = lb.curselection()
            if not sel:
                return
            result["name"] = shown[sel[0]]
            dlg.destroy()
        lb.bind("<Double-Button-1>", lambda *_: choose())
        bf = tk.Frame(dlg, background=_R_BG_PANEL)
        bf.pack(fill="x", **pad)
        ttk.Button(bf, text=t("op.select"), command=choose).pack(side="left", padx=8)
        ttk.Button(bf, text=t("common.cancel"), command=dlg.destroy).pack(side="left", padx=8)
        dlg.bind("<Escape>", lambda *_: dlg.destroy())
        dlg.wait_window()
        return result["name"]

    # ── value-returning editors (no commit) — the recursive backbone ────────────────────────────────

    def _edit_value_standalone(self, value, title):
        """Edit ONE NdfValue and return a NEW NdfValue (or None if cancelled).  Dispatches by type so
        list elements / map keys / map values / pair halves are all edited the same way, recursively."""
        tid = value.type_id
        if tid == _T.Reference:
            return self._objref_pick_value(value, title)
        if tid == _T.Map:
            return self._map_edit_value(value, title)
        if tid == _T.Pair:
            return self._pair_edit_value(value, title)
        if tid == _T.List:
            return self._list_edit_value(value, title)
        if tid == _T.ZipBlob:
            return self._zipblob_edit_value(value, title)
        if tid == _T.LocHash:
            return self._lochash_edit_value(value, title)
        tname = _type_label(tid)
        if tname in _AUTHOR_TYPES:
            return self._scalar_edit_value(value, title)
        ui_util.info(self, title, t("tools.type_type_isn_t_editable", type=tname))
        return None

    def _scalar_edit_value(self, value, title, default_type=None):
        """Small type+value dialog for a scalar / byte value.  Returns a new NdfValue or None."""
        ndf = self._v_ndf
        cur_t = _type_label(value.type_id) if value is not None else (default_type or "Int32")
        if cur_t not in _AUTHOR_TYPES:
            cur_t = default_type or "Int32"
        result = {"val": None}
        dlg = ui_util.themed_toplevel(self, title, resizable=True)
        pad = {"padx": 8, "pady": 4}
        tk.Label(dlg, text=title, background=_R_BG_PANEL, foreground=_R_GOLD_BRT,
                 font=_F_HEAD).grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        tk.Label(dlg, text=t("tools.type_2"), background=_R_BG_PANEL, foreground=_R_TEXT,
                 font=_F_MAIN).grid(row=1, column=0, sticky="e", **pad)
        tvar = tk.StringVar(value=cur_t)
        tcb = ttk.Combobox(dlg, textvariable=tvar, values=_AUTHOR_TYPES, width=14, state="readonly")
        tcb.grid(row=1, column=1, sticky="w", **pad)
        ui_util.fit_combobox(tcb)
        tk.Label(dlg, text=t("tools.new_value"), background=_R_BG_PANEL, foreground=_R_TEXT,
                 font=_F_MAIN).grid(row=2, column=0, sticky="e", **pad)
        nv = tk.StringVar(value=self._scalar_initial_text(value))
        ent = ttk.Entry(dlg, textvariable=nv, width=42)
        ent.grid(row=2, column=1, sticky="ew", **pad)
        hint = tk.Label(dlg, text=_TYPE_HINT.get(tvar.get(), ""), background=_R_BG_PANEL,
                        foreground=_R_TEXT_DIM, font=_F_MAIN)
        hint.grid(row=3, column=1, sticky="w", **pad)
        tcb.bind("<<ComboboxSelected>>", lambda *_: hint.config(text=_TYPE_HINT.get(tvar.get(), "")))

        def ok():
            built = self._build_scalar_value(tvar.get(), nv.get())
            if built is None:
                ui_util.error(dlg, title, t("tools.could_not_parse_raw_as",
                                            raw=nv.get(), type_name=tvar.get()))
                return
            result["val"] = built
            dlg.destroy()
        bf = tk.Frame(dlg, background=_R_BG_PANEL)
        bf.grid(row=4, column=0, columnspan=2, pady=8)
        ttk.Button(bf, text=t("tools.apply"), command=ok).pack(side="left", padx=8)
        ttk.Button(bf, text=t("common.cancel"), command=dlg.destroy).pack(side="left", padx=8)
        ent.focus_set()
        dlg.bind("<Return>", lambda *_: ok())
        dlg.bind("<Escape>", lambda *_: dlg.destroy())
        dlg.wait_window()
        return result["val"]

    def _scalar_initial_text(self, value):
        """Prefill text for the scalar box from an existing value (empty for a fresh add)."""
        if value is None:
            return ""
        ndf = self._v_ndf
        tid = value.type_id
        if tid in (_T.StringRef, _T.PathRef):
            return str(ndf.resolve_value(value))
        if tid in (_T.Guid, _T.Hash, _T.LocHash, _T.Blob):
            return bytes(value.raw).hex()
        if tid in (_T.Vector3, _T.Color128, _T.Color32, _T.TripleInt, _T.Int2, _T.Float2, _T.Matrix):
            try:
                return ", ".join(str(x) for x in value.raw)
            except Exception:
                return ""
        return str(value.raw)

    def _build_scalar_value(self, type_name, raw_text):
        """Parse + build one scalar/byte NdfValue via the proven make_value path (StringRef/PathRef get a
        real STRG index).  Returns an NdfValue or None on parse failure."""
        parsed = _parse_val(raw_text, type_name)
        if parsed is None:
            return None
        try:
            nv = ndfbin_mod.make_value(type_name, parsed)
            if nv.type_id in (_T.StringRef, _T.PathRef):
                nv.raw = self._v_ndf.ensure_string(parsed)
        except Exception:
            return None
        return nv

    def _objref_pick_value(self, value, title):
        """Repoint an object/trans reference and return a new Reference NdfValue (or None).  ObjRef targets
        go through NdfBinary.make_objref, which COERCES the declared class from the chosen target — the
        proven invariant that prevents a stale-class main-menu crash."""
        ndf = self._v_ndf
        marker, ref = value.raw
        if marker == ndfbin_mod.OBJ_REF_MARKER:
            cur = ref[0] if isinstance(ref, tuple) else None
            new_idx = self._pick_instance(t("tools.repoint_reference_pick_target"), initial_idx=cur)
            if new_idx is None:
                return None
            try:
                return ndf.make_objref(new_idx)
            except Exception as e:
                ui_util.error(self, title, str(e))
                return None
        if marker == ndfbin_mod.TRANS_REF_MARKER:
            return self._transref_pick_value(value, title)
        ui_util.info(self, title, t("tools.reference_kind_can_t_repointed"))
        return None

    def _transref_pick_value(self, value, title):
        """Repoint a trans (import) reference to a different import path.  Returns a new Reference or None."""
        ndf = self._v_ndf
        paths = ndf.import_ordinal_paths()          # {ordinal: path}
        by_path = {p: o for o, p in paths.items()}
        cur_ord = value.raw[1] if isinstance(value.raw[1], int) else None
        cur_path = paths.get(cur_ord, "")
        result = {"val": None}
        dlg = ui_util.themed_toplevel(self, title, min_size=(460, 200), resizable=True)
        pad = {"padx": 8, "pady": 4}
        tk.Label(dlg, text=t("tools.import_path"), background=_R_BG_PANEL, foreground=_R_TEXT,
                 font=_F_MAIN).grid(row=0, column=0, sticky="e", **pad)
        pv = tk.StringVar(value=cur_path)
        cb = ttk.Combobox(dlg, textvariable=pv, values=sorted(by_path), width=48, state="readonly")
        cb.grid(row=0, column=1, sticky="ew", **pad)
        ui_util.fit_combobox(cb)
        tk.Label(dlg, text=t("tools.new_path"), background=_R_BG_PANEL, foreground=_R_TEXT,
                 font=_F_MAIN).grid(row=1, column=0, sticky="e", **pad)
        newp = tk.StringVar()
        ttk.Entry(dlg, textvariable=newp, width=48).grid(row=1, column=1, sticky="ew", **pad)
        tk.Label(dlg, text=t("tools.new_path_added_import_table"),
                 background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN).grid(
            row=2, column=1, sticky="w", **pad)

        def ok():
            chosen = newp.get().strip() or pv.get().strip()
            if not chosen:
                ui_util.info(dlg, title, t("tools.pick_enter_import_path"))
                return
            try:
                ordv = ndf.add_import_path(chosen) if chosen not in by_path else by_path[chosen]
            except Exception as e:
                ui_util.error(dlg, title, str(e))
                return
            result["val"] = ndfbin_mod.NdfValue(_T.Reference, (ndfbin_mod.TRANS_REF_MARKER, int(ordv)))
            dlg.destroy()
        bf = tk.Frame(dlg, background=_R_BG_PANEL)
        bf.grid(row=3, column=0, columnspan=2, pady=8)
        ttk.Button(bf, text=t("tools.apply"), command=ok).pack(side="left", padx=8)
        ttk.Button(bf, text=t("common.cancel"), command=dlg.destroy).pack(side="left", padx=8)
        dlg.bind("<Escape>", lambda *_: dlg.destroy())
        dlg.wait_window()
        return result["val"]

    def _author_value(self, title, default_type=None, allow_ref=True):
        """Author a brand-new value (for adding a list element or a map key/value).  Offers all scalar/
        byte types plus ObjRef.  Returns a fully-built NdfValue (STRG-interned, class-coerced) or None."""
        ndf = self._v_ndf
        types = list(_AUTHOR_TYPES) + ([t("tools.objref")] if allow_ref else [])
        start = default_type if default_type in types else (t("tools.objref") if (allow_ref and default_type == "ObjRef") else "Int32")
        result = {"val": None}
        dlg = ui_util.themed_toplevel(self, title, resizable=True)
        pad = {"padx": 8, "pady": 4}
        tk.Label(dlg, text=title, background=_R_BG_PANEL, foreground=_R_GOLD_BRT,
                 font=_F_HEAD).grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        tk.Label(dlg, text=t("tools.type_2"), background=_R_BG_PANEL, foreground=_R_TEXT,
                 font=_F_MAIN).grid(row=1, column=0, sticky="e", **pad)
        tvar = tk.StringVar(value=start)
        tcb = ttk.Combobox(dlg, textvariable=tvar, values=types, width=14, state="readonly")
        tcb.grid(row=1, column=1, sticky="w", **pad)
        ui_util.fit_combobox(tcb)
        tk.Label(dlg, text=t("tools.new_value"), background=_R_BG_PANEL, foreground=_R_TEXT,
                 font=_F_MAIN).grid(row=2, column=0, sticky="e", **pad)
        nv = tk.StringVar()
        ent = ttk.Entry(dlg, textvariable=nv, width=42)
        ent.grid(row=2, column=1, sticky="ew", **pad)
        hint = tk.Label(dlg, text=_TYPE_HINT.get(tvar.get(), ""), background=_R_BG_PANEL,
                        foreground=_R_TEXT_DIM, font=_F_MAIN)
        hint.grid(row=3, column=1, sticky="w", **pad)
        ref_state = {"idx": None}

        def refresh_mode(*_):
            if tvar.get() == t("tools.objref"):
                hint.config(text=(t("tools.target_i", i=ref_state["idx"]) if ref_state["idx"] is not None
                                  else t("tools.no_target_picked")))
                ent.config(state="disabled")
            else:
                ent.config(state="normal")
                hint.config(text=_TYPE_HINT.get(tvar.get(), ""))
        tcb.bind("<<ComboboxSelected>>", refresh_mode)

        def pick_ref():
            i = self._pick_instance(t("tools.pick_reference_target"))
            if i is not None:
                ref_state["idx"] = i
                tvar.set(t("tools.objref"))
                refresh_mode()
        ttk.Button(dlg, text=t("tools.pick_target"), command=pick_ref).grid(row=4, column=1, sticky="w", **pad)

        def ok():
            if tvar.get() == t("tools.objref"):
                if ref_state["idx"] is None:
                    ui_util.info(dlg, title, t("tools.pick_reference_target_first"))
                    return
                try:
                    result["val"] = ndf.make_objref(ref_state["idx"])
                except Exception as e:
                    ui_util.error(dlg, title, str(e))
                    return
            else:
                built = self._build_scalar_value(tvar.get(), nv.get())
                if built is None:
                    ui_util.error(dlg, title, t("tools.could_not_parse_raw_as",
                                                raw=nv.get(), type_name=tvar.get()))
                    return
                result["val"] = built
            dlg.destroy()
        refresh_mode()
        bf = tk.Frame(dlg, background=_R_BG_PANEL)
        bf.grid(row=5, column=0, columnspan=2, pady=8)
        ttk.Button(bf, text=t("tools.add"), command=ok).pack(side="left", padx=8)
        ttk.Button(bf, text=t("common.cancel"), command=dlg.destroy).pack(side="left", padx=8)
        ent.focus_set()
        dlg.bind("<Escape>", lambda *_: dlg.destroy())
        dlg.wait_window()
        return result["val"]

    def _list_edit_value(self, value, title):
        """Generic collection editor over a list of NdfValue elements of ANY type (ObjRef, nested, mixed).
        Add / Edit / Duplicate / Delete / Move; each element edited via the recursive backbone.  Returns a
        new List NdfValue or None."""
        import copy
        ndf = self._v_ndf
        elems = [copy.deepcopy(e) for e in (value.raw or [])]
        default_elem_type = _type_label(elems[0].type_id) if elems else None
        result = {"val": None}
        dlg = ui_util.themed_toplevel(self, title, min_size=(460, 380), resizable=True)
        pad = {"padx": 8, "pady": 4}
        tk.Label(dlg, text=title, background=_R_BG_PANEL, foreground=_R_GOLD_BRT,
                 font=_F_HEAD).pack(anchor="w", **pad)
        lh = tk.Frame(dlg, background=_R_BG_PANEL)
        lh.pack(fill="both", expand=True, **pad)
        tree = ttk.Treeview(lh, columns=("idx", "type", "value"), show="headings", selectmode="browse")
        tree.heading("idx", text="#"); tree.heading("type", text=t("tools.type"))
        tree.heading("value", text=t("common.value"))
        tree.column("idx", width=42, anchor="e", stretch=False)
        tree.column("type", width=90, stretch=False); tree.column("value", width=280, stretch=True)
        ui_util.with_scrollbars(lh, tree)

        def repaint(sel=None):
            for it in tree.get_children():
                tree.delete(it)
            for i, e in enumerate(elems):
                tree.insert("", tk.END, iid=str(i),
                            values=(i, _type_label(e.type_id), _fmt_val(e, ndf)[:120]))
            ui_util.stripe_treeview(tree, _R_BG_WIDGET); ui_util.retag_treeview(tree)
            if sel is not None and 0 <= sel < len(elems):
                tree.selection_set(str(sel)); tree.see(str(sel))
        repaint()

        def cur():
            s = tree.selection()
            return int(s[0]) if s else None

        def add():
            nv = self._author_value(t("tools.add_list_element"), default_type=default_elem_type)
            if nv is not None:
                elems.append(nv); repaint(len(elems) - 1)

        def edit():
            i = cur()
            if i is None:
                return
            nv = self._edit_value_standalone(elems[i], t("tools.edit_element_i", i=i))
            if nv is not None:
                elems[i] = nv; repaint(i)

        def dup():
            i = cur()
            if i is None:
                return
            elems.insert(i + 1, copy.deepcopy(elems[i])); repaint(i + 1)

        def dele():
            i = cur()
            if i is None:
                return
            del elems[i]; repaint(min(i, len(elems) - 1))

        def move(d):
            i = cur()
            if i is None or not (0 <= i + d < len(elems)):
                return
            elems[i], elems[i + d] = elems[i + d], elems[i]; repaint(i + d)

        tree.bind("<Double-Button-1>", lambda *_: edit())

        def apply():
            result["val"] = ndfbin_mod.NdfValue(_T.List, elems)
            dlg.destroy()
        bf = tk.Frame(dlg, background=_R_BG_PANEL)
        bf.pack(fill="x", **pad)
        btns = [ttk.Button(bf, text=t("tools.add"), command=add),
                ttk.Button(bf, text=t("common.edit"), command=edit),
                ttk.Button(bf, text=t("common.duplicate"), command=dup),
                ttk.Button(bf, text=t("tools.delete"), command=dele),
                ttk.Button(bf, text=t("tools.up"), command=lambda: move(-1)),
                ttk.Button(bf, text=t("tools.down"), command=lambda: move(1)),
                ttk.Button(bf, text=t("tools.apply"), command=apply),
                ttk.Button(bf, text=t("common.cancel"), command=dlg.destroy)]
        ui_util.flow(bf, btns)
        dlg.bind("<Escape>", lambda *_: dlg.destroy())
        dlg.wait_window()
        return result["val"]

    def _map_edit_value(self, value, title):
        """Editor for a Map (list of key→value NdfValue pairs).  Add / Edit-key / Edit-value / Delete.
        Returns a new Map NdfValue or None."""
        import copy
        ndf = self._v_ndf
        pairs = [(copy.deepcopy(k), copy.deepcopy(v)) for (k, v) in (value.raw or [])]
        dk = _type_label(pairs[0][0].type_id) if pairs else None
        dv = _type_label(pairs[0][1].type_id) if pairs else None
        result = {"val": None}
        dlg = ui_util.themed_toplevel(self, title, min_size=(480, 380), resizable=True)
        pad = {"padx": 8, "pady": 4}
        tk.Label(dlg, text=title, background=_R_BG_PANEL, foreground=_R_GOLD_BRT,
                 font=_F_HEAD).pack(anchor="w", **pad)
        lh = tk.Frame(dlg, background=_R_BG_PANEL)
        lh.pack(fill="both", expand=True, **pad)
        tree = ttk.Treeview(lh, columns=("idx", "key", "value"), show="headings", selectmode="browse")
        tree.heading("idx", text="#"); tree.heading("key", text=t("text.col_key")); tree.heading("value", text=t("common.value"))
        tree.column("idx", width=42, anchor="e", stretch=False)
        tree.column("key", width=200, stretch=True); tree.column("value", width=200, stretch=True)
        ui_util.with_scrollbars(lh, tree)

        def repaint(sel=None):
            for it in tree.get_children():
                tree.delete(it)
            for i, (k, v) in enumerate(pairs):
                tree.insert("", tk.END, iid=str(i),
                            values=(i, _fmt_val(k, ndf)[:80], _fmt_val(v, ndf)[:80]))
            ui_util.stripe_treeview(tree, _R_BG_WIDGET); ui_util.retag_treeview(tree)
            if sel is not None and 0 <= sel < len(pairs):
                tree.selection_set(str(sel)); tree.see(str(sel))
        repaint()

        def cur():
            s = tree.selection()
            return int(s[0]) if s else None

        def add():
            k = self._author_value(t("tools.new_entry_key"), default_type=dk)
            if k is None:
                return
            v = self._author_value(t("tools.new_entry_value"), default_type=dv)
            if v is None:
                return
            pairs.append((k, v)); repaint(len(pairs) - 1)

        def edit_key():
            i = cur()
            if i is None:
                return
            nk = self._edit_value_standalone(pairs[i][0], t("tools.edit_key_i", i=i))
            if nk is not None:
                pairs[i] = (nk, pairs[i][1]); repaint(i)

        def edit_val():
            i = cur()
            if i is None:
                return
            nv = self._edit_value_standalone(pairs[i][1], t("tools.edit_value_i", i=i))
            if nv is not None:
                pairs[i] = (pairs[i][0], nv); repaint(i)

        def dele():
            i = cur()
            if i is None:
                return
            del pairs[i]; repaint(min(i, len(pairs) - 1))

        tree.bind("<Double-Button-1>", lambda *_: edit_val())

        def apply():
            result["val"] = ndfbin_mod.NdfValue(_T.Map, [(k, v) for (k, v) in pairs])
            dlg.destroy()
        bf = tk.Frame(dlg, background=_R_BG_PANEL)
        bf.pack(fill="x", **pad)
        btns = [ttk.Button(bf, text=t("tools.add"), command=add),
                ttk.Button(bf, text=t("tools.edit_key"), command=edit_key),
                ttk.Button(bf, text=t("tools.edit_value_2"), command=edit_val),
                ttk.Button(bf, text=t("tools.delete"), command=dele),
                ttk.Button(bf, text=t("tools.apply"), command=apply),
                ttk.Button(bf, text=t("common.cancel"), command=dlg.destroy)]
        ui_util.flow(bf, btns)
        dlg.bind("<Escape>", lambda *_: dlg.destroy())
        dlg.wait_window()
        return result["val"]

    def _pair_edit_value(self, value, title):
        """Editor for a Pair (two typed halves).  Returns a new Pair NdfValue or None."""
        import copy
        k, v = value.raw
        k = copy.deepcopy(k); v = copy.deepcopy(v)
        state = {"k": k, "v": v}
        result = {"val": None}
        dlg = ui_util.themed_toplevel(self, title, resizable=True)
        pad = {"padx": 8, "pady": 4}
        tk.Label(dlg, text=title, background=_R_BG_PANEL, foreground=_R_GOLD_BRT,
                 font=_F_HEAD).grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        kl = tk.Label(dlg, text="", background=_R_BG_PANEL, foreground=_R_TEXT, font=_F_MAIN)
        kl.grid(row=1, column=1, sticky="w", **pad)
        vl = tk.Label(dlg, text="", background=_R_BG_PANEL, foreground=_R_TEXT, font=_F_MAIN)
        vl.grid(row=2, column=1, sticky="w", **pad)

        def refresh():
            kl.config(text=f"{_type_label(state['k'].type_id)}: {_fmt_val(state['k'], self._v_ndf)[:60]}")
            vl.config(text=f"{_type_label(state['v'].type_id)}: {_fmt_val(state['v'], self._v_ndf)[:60]}")
        refresh()

        def edit_k():
            nk = self._edit_value_standalone(state["k"], t("tools.edit_first"))
            if nk is not None:
                state["k"] = nk; refresh()

        def edit_v():
            nv = self._edit_value_standalone(state["v"], t("tools.edit_second"))
            if nv is not None:
                state["v"] = nv; refresh()
        ttk.Button(dlg, text=t("tools.edit_first"), command=edit_k).grid(row=1, column=0, sticky="e", **pad)
        ttk.Button(dlg, text=t("tools.edit_second"), command=edit_v).grid(row=2, column=0, sticky="e", **pad)

        def apply():
            result["val"] = ndfbin_mod.NdfValue(_T.Pair, (state["k"], state["v"]))
            dlg.destroy()
        bf = tk.Frame(dlg, background=_R_BG_PANEL)
        bf.grid(row=3, column=0, columnspan=2, pady=8)
        ttk.Button(bf, text=t("tools.apply"), command=apply).pack(side="left", padx=8)
        ttk.Button(bf, text=t("common.cancel"), command=dlg.destroy).pack(side="left", padx=8)
        dlg.bind("<Escape>", lambda *_: dlg.destroy())
        dlg.wait_window()
        return result["val"]

    def _zipblob_edit_value(self, value, title):
        """Editor for a ZipBlob — edit the flag + payload (as UTF-8 text or hex).  Returns a new
        ZipBlob NdfValue or None."""
        import base64 as _b64
        cur = value.raw or {}
        data = cur.get("data", b"")
        result = {"val": None}
        dlg = ui_util.themed_toplevel(self, title, min_size=(460, 240), resizable=True)
        pad = {"padx": 8, "pady": 4}
        tk.Label(dlg, text=title, background=_R_BG_PANEL, foreground=_R_GOLD_BRT,
                 font=_F_HEAD).grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        tk.Label(dlg, text=t("tools.compressed_flag_1"), background=_R_BG_PANEL, foreground=_R_TEXT,
                 font=_F_MAIN).grid(row=1, column=0, sticky="e", **pad)
        comp = tk.IntVar(value=1 if cur.get("flag") == 1 else 0)
        ttk.Checkbutton(dlg, variable=comp).grid(row=1, column=1, sticky="w", **pad)
        tk.Label(dlg, text=t("tools.payload_base64"), background=_R_BG_PANEL, foreground=_R_TEXT,
                 font=_F_MAIN).grid(row=2, column=0, sticky="ne", **pad)
        txt = tk.Text(dlg, width=48, height=6, background=_R_BG_WIDGET, foreground=_R_TEXT,
                      insertbackground=_R_TEXT)
        txt.grid(row=2, column=1, sticky="ew", **pad)
        txt.insert("1.0", _b64.b64encode(bytes(data)).decode())

        def ok():
            try:
                raw = _b64.b64decode(txt.get("1.0", "end").strip())
            except Exception:
                ui_util.error(dlg, title, t("tools.payload_must_valid_base64"))
                return
            flag = 1 if comp.get() else 0
            if flag == 1:
                result["val"] = ndfbin_mod.NdfValue(_T.ZipBlob,
                                                    {"flag": 1, "uncomp_size": len(raw), "data": raw})
            else:
                result["val"] = ndfbin_mod.NdfValue(_T.ZipBlob, {"flag": flag, "data": raw})
            dlg.destroy()
        bf = tk.Frame(dlg, background=_R_BG_PANEL)
        bf.grid(row=3, column=0, columnspan=2, pady=8)
        ttk.Button(bf, text=t("tools.apply"), command=ok).pack(side="left", padx=8)
        ttk.Button(bf, text=t("common.cancel"), command=dlg.destroy).pack(side="left", padx=8)
        dlg.bind("<Escape>", lambda *_: dlg.destroy())
        dlg.wait_window()
        return result["val"]

    # ── LocHash localization manager ────────────────────────────────────────────────────────────────
    # A LocHash is a KEY into the game's per-language .dic text tables, not free bytes.  This manager
    # resolves the key to its real string(s), edits the TEXT (the common case, keeps the key), mints a
    # new key for a new string, re-points to an existing entry, or (advanced) edits the raw hex.

    # ── LocHash plumbing — the LOGIC lives in ruse_mod_engine.edits (layer 2) so the scenario editors and
    # any future editor perform the identical operation; only the caching + Tk refresh hooks stay here.
    def _loc_build_index(self, force=False):
        """Build (and cache) a LocIndex over ALL .dic tables in the 'loc' dat.  Cached until a .dic is
        written (see _loc_write_blobs).  None if no loc dat is reachable (nested archive / no project),
        which callers treat as 'fall back to raw hex'."""
        if not force and getattr(self, "_loc_index", None) is not None:
            return self._loc_index
        self._loc_index = edits_mod.loc_index(self._store)
        return self._loc_index

    def _loc_family_paths(self, idx, key):
        """All (dat_key, path) .dic files sharing the BASENAME of the file(s) `key` lives in, across
        every language — so a minted entry lands in all languages."""
        return edits_mod.loc_family_paths(idx, key)

    def _loc_write_blobs(self, blobs):
        """Write {(dat_key, path): blob} back through the store, mark dirty, and drop the cached index
        so the next resolve re-reads the updated .dic text."""
        if edits_mod.loc_write(self._store, blobs):
            self._loc_index = None
            self._notify()
            self._update_status()

    def _prompt_line(self, title, label):
        """One-line text prompt.  Returns the entered text or None."""
        result = {"s": None}
        dlg = ui_util.themed_toplevel(self, title, resizable=True)
        pad = {"padx": 8, "pady": 4}
        tk.Label(dlg, text=label, background=_R_BG_PANEL, foreground=_R_TEXT, font=_F_MAIN).grid(
            row=0, column=0, sticky="e", **pad)
        sv = tk.StringVar()
        ent = ttk.Entry(dlg, textvariable=sv, width=48)
        ent.grid(row=0, column=1, sticky="ew", **pad)

        def ok():
            if sv.get().strip():
                result["s"] = sv.get()
                dlg.destroy()
        bf = tk.Frame(dlg, background=_R_BG_PANEL)
        bf.grid(row=1, column=0, columnspan=2, pady=8)
        ttk.Button(bf, text=t("common.ok"), command=ok).pack(side="left", padx=8)
        ttk.Button(bf, text=t("common.cancel"), command=dlg.destroy).pack(side="left", padx=8)
        ent.focus_set()
        dlg.bind("<Return>", lambda *_: ok())
        dlg.bind("<Escape>", lambda *_: dlg.destroy())
        dlg.wait_window()
        return result["s"]

    def _loc_default_lang(self, idx):
        """The language to show strings in: the mod manager's configured language if the .dic has it,
        else dev, else us, else whatever's present."""
        avail = idx.available_langs()
        try:
            cur = i18n.current()
        except Exception:
            cur = "us"
        for c in (cur, "dev", "us"):
            if c in avail:
                return c
        return avail[0] if avail else "dev"

    def _loc_pick_entry(self, idx):
        """Browse existing localization entries and point the property at one.  A flat searchable list
        (search matches any language's text or the key hex); each row's string is shown in the mod
        manager's configured language (Settings), NOT dev.  Returns the picked 8-byte key or None."""
        result = {"key": None}
        cur = self._loc_default_lang(idx)          # the Settings language (dev/us fallback)
        dlg = ui_util.themed_toplevel(self, t("tools.re_point_pick_localization_entry"),
                                      min_size=(660, 480), resizable=True)
        pad = {"padx": 8, "pady": 4}
        srow = tk.Frame(dlg, background=_R_BG_PANEL)
        srow.pack(fill="x", **pad)
        tk.Label(srow, text=t("text.search"), background=_R_BG_PANEL, foreground=_R_TEXT,
                 font=_F_MAIN).pack(side="left")
        sv = tk.StringVar()
        ttk.Entry(srow, textvariable=sv).pack(side="left", fill="x", expand=True, padx=6)
        lh = tk.Frame(dlg, background=_R_BG_PANEL)
        lh.pack(fill="both", expand=True, **pad)
        tree = ttk.Treeview(lh, columns=("key", "lang", "string"), show="headings", selectmode="browse")
        tree.heading("key", text=t("tools.key")); tree.heading("lang", text=t("units.language"))
        tree.heading("string", text=t("tools.string"))
        tree.column("key", width=140, stretch=False); tree.column("lang", width=54, stretch=False)
        tree.column("string", width=420, stretch=True)
        ui_util.with_scrollbars(lh, tree)
        rows = []

        def repop(*_):
            rows.clear()
            for it in tree.get_children():
                tree.delete(it)
            seen = set()
            for e in idx.search(sv.get(), limit=2000):
                if e.key in seen:                  # one row per key, string in the Settings language
                    continue
                seen.add(e.key)
                s = idx.string_in(e.key, cur) or e.string
                rows.append(e.key)
                tree.insert("", tk.END, iid=str(len(rows) - 1),
                            values=(e.key.hex(), cur, s.replace("\n", " ")[:150] or "(empty)"))
            ui_util.stripe_treeview(tree, _R_BG_WIDGET); ui_util.retag_treeview(tree)
        sv.trace_add("write", lambda *_: repop())
        repop()

        def choose():
            sel = tree.selection()
            if sel:
                result["key"] = rows[int(sel[0])]
                dlg.destroy()
        tree.bind("<Double-Button-1>", lambda *_: choose())
        bf = tk.Frame(dlg, background=_R_BG_PANEL)
        bf.pack(fill="x", **pad)
        ttk.Button(bf, text=t("op.select"), command=choose).pack(side="left", padx=8)
        ttk.Button(bf, text=t("common.cancel"), command=dlg.destroy).pack(side="left", padx=8)
        dlg.bind("<Escape>", lambda *_: dlg.destroy())
        dlg.wait_window()
        return result["key"]

    def _lochash_edit_value(self, value, title, prop_name=""):
        """The LocHash manager.  Returns a new LocHash NdfValue if the property should be REPOINTED
        (mint / re-point / raw hex changed the key), else None.  Text edits are written to the .dic
        files as a side effect either way.  Falls back to the raw-hex box if no loc dat is reachable."""
        idx0 = self._loc_build_index()
        if idx0 is None:
            return self._scalar_edit_value(value, title)   # nested / no project — raw hex only
        key0 = bytes(value.raw)
        result = {"key": None}
        st = {"idx": idx0}
        edits = {}                      # lang -> new text (staged, not yet written)
        current = {"lang": None}
        langmap = {"m": {}}             # lang -> LocEntry for key0

        dlg = ui_util.themed_toplevel(self, title, min_size=(660, 520), resizable=True)
        pad = {"padx": 8, "pady": 4}
        head = tk.Label(dlg, text="", background=_R_BG_PANEL, foreground=_R_GOLD_BRT, font=_F_HEAD)
        head.pack(anchor="w", **pad)
        sub = tk.Label(dlg, text="", background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN,
                       justify="left", wraplength=620)
        sub.pack(anchor="w", **pad)

        lh = tk.Frame(dlg, background=_R_BG_PANEL)
        lh.pack(fill="both", expand=True, **pad)
        tree = ttk.Treeview(lh, columns=("lang", "string"), show="headings", selectmode="browse",
                            height=8)
        tree.heading("lang", text=t("units.language")); tree.heading("string", text=t("common.value"))
        tree.column("lang", width=110, stretch=False); tree.column("string", width=480, stretch=True)
        ui_util.with_scrollbars(lh, tree)

        tk.Label(dlg, text=t("tools.selected_string_edit_then_save"), background=_R_BG_PANEL,
                 foreground=_R_TEXT, font=_F_MAIN).pack(anchor="w", **pad)
        eframe, etxt = self._mk_text(dlg)
        etxt.configure(state="normal", height=4)
        eframe.pack(fill="x", **pad)

        def commit_current():
            lg = current["lang"]
            if lg is None:
                return
            newtext = etxt.get("1.0", "end-1c")
            orig = langmap["m"].get(lg)
            if orig is not None and newtext != orig.string:
                edits[lg] = newtext
            else:
                edits.pop(lg, None)

        def refresh():
            langs = st["idx"].langs_for(key0)
            langmap["m"] = langs
            cur = st["idx"].string_for(key0)
            head.config(text=((prop_name + "  —  ") if prop_name else "") + f"LocHash  {key0.hex()}")
            if cur is None:
                sub.config(text=t("tools.key_has_no_text_localization"))
            else:
                sub.config(text=t("tools.resolves_s", s=cur[:120]))
            for it in tree.get_children():
                tree.delete(it)
            order = [c for c, _ in dic_mod.LANGUAGES]
            for lg in sorted(langs, key=lambda l: order.index(l) if l in order else 99):
                s = edits.get(lg, langs[lg].string)
                mark = "✎ " if lg in edits else ""
                tree.insert("", tk.END, iid=lg, values=(lg, mark + s.replace("\n", " ")[:150]))
            ui_util.stripe_treeview(tree, _R_BG_WIDGET); ui_util.retag_treeview(tree)

        def on_sel(_=None):
            sel = tree.selection()
            if not sel:
                return
            commit_current()
            lg = sel[0]
            current["lang"] = lg
            etxt.delete("1.0", "end")
            base = langmap["m"].get(lg)
            etxt.insert("1.0", edits.get(lg, base.string if base else ""))
        tree.bind("<<TreeviewSelect>>", on_sel)
        refresh()

        def save_text():
            commit_current()
            if not edits:
                ui_util.info(dlg, title, t("tools.no_text_changes_save"))
                return
            # add_missing fills in any language whose .dic lacks the key entirely — LocIndex.set_text
            # alone skips those, which is how a mod ends up permanently blank in one language.
            blobs = edits_mod.loc_set_text(st["idx"], key0, dict(edits))
            self._loc_write_blobs(blobs)
            edits.clear()
            st["idx"] = self._loc_build_index(force=True)   # re-read so the view reflects the write
            refresh()
            ui_util.info(dlg, title,
                         t("tools.saved_text_n_language_file",
                           n=len(blobs)))

        def mint_new():
            commit_current()
            name = self._prompt_line(
                t("tools.new_localized_string"),
                t("tools.creates_new_dic_entry_every"))
            if not name:
                return
            try:
                newk, written, existed = edits_mod.loc_mint(st["idx"], name, family_of=key0)
            except edits_mod.EditError:
                # The only reachable cause here is an empty family — the engine message is developer
                # English, so keep showing the translated one the user already knows.
                ui_util.error(dlg, title, t("tools.no_dic_files_found_add"))
                return
            if existed:
                # md5(text) already exists — an identical string is already in the tables; just point at it.
                if not ui_util.confirm(dlg, title, t(
                        "tools.entry_exact_text_already_exists", k=newk.hex())):
                    return
            else:
                self._loc_write_blobs(written)
                st["idx"] = self._loc_build_index(force=True)
                ui_util.info(dlg, title, t(
                    "tools.created_localization_entry_k_s",
                    k=newk.hex(), s=name[:60], n=len(written)))
            result["key"] = newk
            dlg.destroy()

        def repoint():
            commit_current()
            k = self._loc_pick_entry(st["idx"])
            if k is not None:
                result["key"] = bytes(k)
                dlg.destroy()

        def raw_hex():
            commit_current()
            nv = self._scalar_edit_value(ndfbin_mod.NdfValue(_T.LocHash, key0),
                                         t("tools.edit_raw_lochash_bytes_advanced"))
            if nv is not None:
                result["key"] = bytes(nv.raw)
                dlg.destroy()

        bf = tk.Frame(dlg, background=_R_BG_PANEL)
        bf.pack(fill="x", **pad)
        btns = [ttk.Button(bf, text=t("text.save_text"), command=save_text),
                ttk.Button(bf, text=t("tools.mint_new"), command=mint_new),
                ttk.Button(bf, text=t("tools.re_point"), command=repoint),
                ttk.Button(bf, text=t("tools.raw_hex"), command=raw_hex),
                ttk.Button(bf, text=t("common.close"), command=dlg.destroy)]
        ui_util.flow(bf, btns)
        dlg.bind("<Escape>", lambda *_: dlg.destroy())
        dlg.wait_window()
        return (ndfbin_mod.NdfValue(_T.LocHash, result["key"]) if result["key"] is not None else None)

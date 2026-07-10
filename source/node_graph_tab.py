"""'Node Graph' tab (Scenario Designer doc 03 View 1) — the cause→effect graph of how the selected operation
plays out. Nodes = the EffetMap control-flow spine (sequence / parallel / wait / trigger) and the leaf actions
(objectives, spawns, AI, economy, victory/defeat …); edges = control-flow containment, left→right by depth.
Read-only legibility: pan (scrollbars / drag), zoom (Ctrl+wheel), click a node for its details. Editing stays in
the Objectives & Logic tab.

Shares script_logic's decompile cache, so it's instant after the logic/timeline tabs have loaded the scenario.
"""
import tkinter as tk

from i18n import t
from ruse_mod_engine import script_logic as SL

import theme                # single source of truth for the palette; local names kept unchanged
_BG = theme.BG; _PANEL = theme.PANEL; _WIDGET = theme.WIDGET; _TEXT = theme.TEXT; _DIM = theme.DIM
_GOLD = theme.GOLD; _GOLD_BRT = theme.GOLD_BRT; _EDGE = "#2a4055"
_F = theme.F; _FB = theme.FB; _FS = theme.FS

_CAT_COLOR = {
    "objective": _GOLD, "win": "#2f6e3f", "lose": "#7e3636", "economy": "#244c63",
    "units": "#7a5a28", "tech": "#4a3a68", "ai": "#6e4a3a", "score": "#5a4e1a",
    "av": "#2a3340", "ui": "#2a3340", "time": "#1e5048", "state": "#33384a",
    "setup": "#22303f", "flow": "#1a2c3e", "wait": "#13343a", "trigger": "#3a2f4a", "other": "#222f3e",
}
_BW, _BH, _XSP, _YSP, _MARGIN = 168, 22, 196, 27, 24


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


class NodeGraphFrame(tk.Frame):
    def __init__(self, parent, project, **_kw):
        super().__init__(parent, background=_BG)
        self.project = project
        self.binding = None
        self._ep = None
        self._irs = {}
        self._nodes = {}     # id -> {label, cat, kind, type, ir, depth, y}
        self._edges = []     # (parent_id, child_id, style)
        self._nid = 0
        self._leaf_y = 0
        self._scale = 1.0
        self._sel = None
        self._build()

    # ---- project data ----
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
        for lbl, cmd in ((t("graph.fit"), self._fit), (t("graph.x"), lambda: self._zoom(1.2)), (t("graph.x_2"), lambda: self._zoom(1 / 1.2))):
            tk.Button(head, text=lbl, command=cmd, background="#122030", foreground=_GOLD_BRT,
                      font=_FB, relief="flat", width=3).pack(side="right", padx=2, pady=3)

        body = tk.Frame(self, background=_BG); body.pack(side="top", fill="both", expand=True)
        cwrap = tk.Frame(body, background=_BG); cwrap.pack(side="left", fill="both", expand=True)
        self._canvas = tk.Canvas(cwrap, background=_BG, highlightthickness=0)
        hb = tk.Scrollbar(cwrap, orient="horizontal", command=self._canvas.xview)
        vb = tk.Scrollbar(cwrap, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(xscrollcommand=hb.set, yscrollcommand=vb.set)
        vb.pack(side="right", fill="y"); hb.pack(side="bottom", fill="x")
        self._canvas.pack(side="left", fill="both", expand=True)
        self._canvas.bind("<Button-1>", self._on_click)
        self._canvas.bind("<ButtonPress-2>", lambda e: self._canvas.scan_mark(e.x, e.y))
        self._canvas.bind("<B2-Motion>", lambda e: self._canvas.scan_dragto(e.x, e.y, gain=1))
        self._canvas.bind("<Control-MouseWheel>", lambda e: self._zoom(1.2 if e.delta > 0 else 1 / 1.2))
        self._canvas.bind("<MouseWheel>", lambda e: self._canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))

        side = tk.Frame(body, background=_PANEL, width=240); side.pack(side="right", fill="y"); side.pack_propagate(False)
        tk.Label(side, text=t("graph.node_details"), background=_PANEL, foreground=_GOLD_BRT,
                 font=_FB, anchor="w").pack(fill="x", padx=8, pady=(6, 2))
        self._detail = tk.Text(side, background=_WIDGET, foreground=_TEXT, font=_FS, wrap="word",
                               relief="flat", height=10)
        self._detail.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._detail.configure(state="disabled")

        self._status = tk.Label(self, text=t("common.select_map_scenario_above"), background=_BG,
                                foreground=_DIM, font=_F, anchor="w")
        self._status.pack(side="bottom", fill="x", padx=10, pady=4)

    def _toast(self, msg, err=False):
        try:
            self._status.config(text=msg, foreground=("#e06a6a" if err else _DIM)); self.update_idletasks()
        except Exception:
            pass

    # ---- graph model (walk parse_flow, group repeats, assign tree positions) ----
    def _add(self, label, cat, kind, typ, ir, depth):
        self._nid += 1
        self._nodes[self._nid] = {"label": label, "cat": cat, "kind": kind, "type": typ,
                                  "ir": ir, "depth": depth, "y": None}
        return self._nid

    def _take_row(self):
        y = self._leaf_y; self._leaf_y += 1; return y

    def _grouped_label(self, node, count):
        lab = node.get("label") or node["type"]
        return lab + ("  ×%d" % count if count > 1 else "")

    def _layout(self, node, depth, parent_id):
        kind = node["kind"]
        if kind == "wait":
            lab = (t("common.wait_d_s").format(d=node["duree"]) if node.get("duree") is not None
                   else t("graph.until_c").format(c=node.get("condition_type") or node.get("condition") or "?"))
            cat = "wait" if node.get("duree") is not None else "trigger"
            nid = self._add(lab, cat, "wait", node["type"], node.get("condition"), depth)
        elif kind == "action":
            nid = self._add(node.get("label") or node["type"], node.get("cat"), "action", node["type"],
                            node["ir"], depth)
        else:  # flow
            tag = (t("graph.parallel") if "Simultane" in node["type"] else t("graph.sequence"))
            nid = self._add("%s  (%d)" % (tag, len(node["children"])), "flow", "flow", node["type"],
                            node["ir"], depth)
        if parent_id is not None:
            self._edges.append((parent_id, nid))
        children = node["children"]
        if not children:
            self._nodes[nid]["y"] = self._take_row()
            return nid
        kids = []
        i = 0
        while i < len(children):
            c = children[i]
            if c["kind"] == "action":   # group consecutive identical actions into one node
                j = i
                while j + 1 < len(children) and children[j + 1]["kind"] == "action" \
                        and children[j + 1].get("label") == c.get("label"):
                    j += 1
                cnt = j - i + 1
                cid = self._add(self._grouped_label(c, cnt), c.get("cat"), "action", c["type"], c["ir"], depth + 1)
                self._edges.append((nid, cid))
                self._nodes[cid]["y"] = self._take_row()
                kids.append(cid)
                i = j + 1
            else:
                kids.append(self._layout(c, depth + 1, nid))
                i += 1
        self._nodes[nid]["y"] = (self._nodes[kids[0]]["y"] + self._nodes[kids[-1]]["y"]) / 2.0
        return nid

    # ---- render ----
    def _render(self):
        c = self._canvas; c.delete("all")
        s = self._scale
        bw, bh, xsp, ysp = _BW * s, _BH * s, _XSP * s, _YSP * s
        fsz = max(6, int(8 * s))
        for (a, b) in self._edges:
            na, nb = self._nodes[a], self._nodes[b]
            x1 = _MARGIN + na["depth"] * xsp + bw; y1 = _MARGIN + na["y"] * ysp + bh / 2
            x2 = _MARGIN + nb["depth"] * xsp; y2 = _MARGIN + nb["y"] * ysp + bh / 2
            mx = (x1 + x2) / 2
            c.create_line(x1, y1, mx, y1, mx, y2, x2, y2, fill=_EDGE, width=max(1, s))
        for nid, n in self._nodes.items():
            x = _MARGIN + n["depth"] * xsp; y = _MARGIN + n["y"] * ysp
            fill = _CAT_COLOR.get(n["cat"], _CAT_COLOR["other"])
            outline = _GOLD_BRT if nid == self._sel else "#33485e"
            c.create_rectangle(x, y, x + bw, y + bh, fill=fill, outline=outline,
                               width=(2 if nid == self._sel else 1), tags=("node", "n%d" % nid))
            txt = n["label"]
            c.create_text(x + 6, y + bh / 2, text=txt, anchor="w", fill=_TEXT, font=("Courier New", fsz),
                          width=bw - 10, tags=("node", "n%d" % nid))
        bbox = c.bbox("all")
        if bbox:
            c.configure(scrollregion=(bbox[0] - 20, bbox[1] - 20, bbox[2] + 20, bbox[3] + 20))

    def _zoom(self, factor):
        self._scale = max(0.25, min(2.5, self._scale * factor))
        self._render()

    def _fit(self):
        self._scale = 1.0
        self._render()
        self._canvas.xview_moveto(0); self._canvas.yview_moveto(0)

    def _on_click(self, e):
        item = self._canvas.find_closest(self._canvas.canvasx(e.x), self._canvas.canvasy(e.y))
        nid = None
        for tg in (self._canvas.gettags(item) if item else ()):
            if tg.startswith("n") and tg[1:].isdigit():
                nid = int(tg[1:]); break
        if nid is None or nid not in self._nodes:
            return
        self._sel = nid
        self._render()
        self._show_detail(self._nodes[nid])

    def _show_detail(self, n):
        lines = ["%s" % n["label"], "kind: %s" % n["kind"], "type: %s" % n["type"]]
        if n.get("cat"):
            lines.append("category: %s" % n["cat"])
        ir = n.get("ir")
        if ir and ir in self._irs:
            kw = SL.scalar_kwargs(self._irs[ir]["body"])
            if kw:
                lines.append("")
                lines += ["%s = %s" % (k, v) for k, v in kw.items()]
        self._detail.configure(state="normal"); self._detail.delete("1.0", "end")
        self._detail.insert("1.0", "\n".join(lines)); self._detail.configure(state="disabled")

    # ---- binding ----
    def set_binding(self, binding):
        self.binding = binding
        self._canvas.delete("all"); self._nodes = {}; self._edges = []
        self._nid = 0; self._leaf_y = 0; self._sel = None; self._irs = {}
        if binding is None:
            self._hdr.config(text=t("common.no_scenario_selected")); self._ep = None
            self._toast(t("common.select_map_scenario_above")); return
        kind = getattr(binding, "kind", "")
        self._hdr.config(text=t("common.map_scn_kind").format(
            map=binding.map_dir, scn=binding.scenario_name, kind=kind))
        if kind not in ("operation", "campaign"):
            self._ep = None
            self._toast(t("graph.multiplayer_map_scenario_no_scripted")); return
        ep = self._effetmap_path()
        if not ep:
            self._ep = None
            self._toast(t("common.no_mission_script_found_scenario")); return
        self._toast(t("common.reading_mission_script"))
        try:
            src = SL.decompile_xyz(self.project.get_raw("scripts", ep))   # cached
            self._irs = SL.ir_assignments(src)
            self._ep = ep
        except Exception as e:
            self._ep = None
            self._toast(t("common.could_not_read_mission_script").format(e=e), err=True); return
        root = SL.parse_flow(src)
        if not root:
            self._toast(t("graph.no_flow_root_effetmap_found")); return
        self._layout(root, 0, None)
        self._render()
        self._toast(t("graph.n_nodes_click_node_details").format(n=len(self._nodes)))

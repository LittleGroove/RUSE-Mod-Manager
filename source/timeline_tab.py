"""'Timeline' tab (Scenario Designer doc 03 View 2) — a read-only STORYBOARD of how the selected operation
unfolds: the EffetMap control-flow spine (Sequential = order, Simultaneous = parallel, WaitDuree = time,
WaitCondition = trigger) rendered as indented beats, color-coded by what each step does (objective / win / lose /
economy / units / AI / setup / audio …). Legibility first; editing stays in the Objectives & Logic tab.

Binding-driven; shares script_logic's decompile cache with the logic tab so the ~30s decompile is paid once.
"""
import tkinter as tk

import ui_util
from i18n import t
from ruse_mod_engine import script_logic as SL

import theme                # single source of truth for the palette; local names kept unchanged
_BG = theme.BG; _PANEL = theme.PANEL; _TEXT = theme.TEXT; _DIM = theme.DIM
_GOLD = theme.GOLD; _GOLD_BRT = theme.GOLD_BRT
_F = theme.F; _FB = theme.FB; _FS = theme.FS

# category -> colour (matches the Objectives & Logic palette where they overlap)
_CAT_COLOR = {
    "objective": _GOLD_BRT, "win": "#7fd17f", "lose": "#e06a6a", "economy": "#6fb0d8",
    "units": "#e0a44a", "tech": "#b08fd8", "ai": "#d88f6f", "score": _GOLD,
    "av": "#7a8aa0", "ui": "#7a8aa0", "time": "#6fd8c0", "state": "#9aa0b0",
    "setup": "#5a6a80", "flow": _GOLD, "other": "#8a98ac",
}


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


class TimelineFrame(tk.Frame):
    def __init__(self, parent, project, **_kw):
        super().__init__(parent, background=_BG)
        self.project = project
        self.binding = None
        self._ep = None
        self._lines = 0
        self._build()

    def _effetmap_path(self):
        b = self.binding
        want = ("\\map\\%s\\scripting%s\\effetmap.xyz" % (b.map_dir, _script_suffix(b.scenario_name))).lower()
        return next((v for v in _ProjDat(self.project, "scripts").list()
                     if v.lower().replace("/", "\\").endswith(want)), None)

    def _build(self):
        head = tk.Frame(self, background=_PANEL); head.pack(side="top", fill="x")
        self._hdr = tk.Label(head, text=t("common.follows_map_scenario_selection_above"),
                             background=_PANEL, foreground=_GOLD_BRT, font=_FB, anchor="w")
        self._hdr.pack(side="left", padx=8, pady=6)
        tk.Label(head, text=t("timeline.how_operation_unfolds_read_only"), background=_PANEL,
                 foreground=_DIM, font=_FS).pack(side="right", padx=8)
        holder = tk.Frame(self, background=_BG); holder.pack(fill="both", expand=True, padx=10, pady=8)
        self._flow = ui_util.make_scrollable(holder, bg=_BG)
        self._status = tk.Label(self, text=t("common.select_map_scenario_above"),
                                background=_BG, foreground=_DIM, font=_F, anchor="w")
        self._status.pack(side="bottom", fill="x", padx=10, pady=4)

    def _toast(self, msg, err=False):
        try:
            self._status.config(text=msg, foreground=("#e06a6a" if err else _DIM)); self.update_idletasks()
        except Exception:
            pass

    # ---- line emit ----
    def _line(self, depth, glyph, text, color, bold=False):
        if self._lines > 2500:
            return
        self._lines += 1
        tk.Label(self._flow, text=("    " * depth) + glyph + " " + text, background=_BG, foreground=color,
                 font=(_FB if bold else _F), anchor="w", justify="left").pack(fill="x", padx=2)

    @staticmethod
    def _flatten(node):
        if node["kind"] == "flow" and "Sequential" in node["type"]:
            return node["children"]
        return [node]

    def _emit_seq(self, children, depth):
        i = 0
        while i < len(children):
            c = children[i]
            if c["kind"] == "action":
                j = i  # group consecutive identical actions ("lock bluff card x13")
                while j + 1 < len(children) and children[j + 1]["kind"] == "action" \
                        and children[j + 1].get("label") == c.get("label"):
                    j += 1
                n = j - i + 1
                label = c.get("label") or c["type"]
                if n > 1:
                    label += "  x%d" % n
                self._line(depth, "•", label, _CAT_COLOR.get(c.get("cat"), _DIM))
                i = j + 1
            elif c["kind"] == "wait":
                if c.get("duree") is not None:
                    self._line(depth, "⏱", t("common.wait_d_s").format(d=c["duree"]), _CAT_COLOR["time"])
                else:
                    self._line(depth, "⏸", t("timeline.wait_until_c").format(
                        c=c.get("condition_type") or c.get("condition") or "?"), _CAT_COLOR["time"])
                i += 1
            elif c["kind"] == "flow":
                if "Simultane" in c["type"]:
                    self._line(depth, "⟂", t("timeline.same_time"), _DIM)
                    for b in c["children"]:
                        self._emit_seq(self._flatten(b), depth + 1)
                elif "Sequential" in c["type"]:
                    self._emit_seq(c["children"], depth)  # flatten nested sequence
                else:
                    self._line(depth, "▸", c["type"], _GOLD)
                    self._emit_seq(c["children"], depth + 1)
                i += 1
            else:
                i += 1

    # ---- binding ----
    def set_binding(self, binding):
        self.binding = binding
        for w in self._flow.winfo_children():
            w.destroy()
        self._lines = 0
        if binding is None:
            self._hdr.config(text=t("common.no_scenario_selected")); self._ep = None
            self._toast(t("common.select_map_scenario_above")); return
        kind = getattr(binding, "kind", "")
        self._hdr.config(text=t("common.map_scn_kind").format(
            map=binding.map_dir, scn=binding.scenario_name, kind=kind))
        if kind not in ("operation", "campaign"):
            self._ep = None
            self._toast(t("timeline.multiplayer_map_scenario_no_scripted")); return
        ep = self._effetmap_path()
        if not ep:
            self._ep = None
            self._toast(t("common.no_mission_script_found_scenario")); return
        self._toast(t("common.reading_mission_script"))
        try:
            src = SL.decompile_xyz(self.project.get_raw("scripts", ep))   # cached (shared with logic tab)
            self._ep = ep
        except Exception as e:
            self._ep = None
            self._toast(t("common.could_not_read_mission_script").format(e=e), err=True); return
        root = SL.parse_flow(src)
        if not root:
            self._toast(t("timeline.no_flow_root_effetmap_found")); return
        if root["kind"] == "flow" and "Simultane" in root["type"]:
            for idx, branch in enumerate(root["children"], 1):
                self._line(0, "▌", t("timeline.track_n_runs_parallel").format(n=idx), _GOLD, bold=True)
                self._emit_seq(self._flatten(branch), 1)
        else:
            self._emit_seq(self._flatten(root), 0)
        self._toast(t("timeline.storyboard_n_steps_edit_values").format(n=self._lines))

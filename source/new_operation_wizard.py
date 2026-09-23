"""'New Operation' — make a new operation or campaign mission, from the top strip.

This CREATES rather than edits, so it is not a step in the walk: the five steps are for a scenario that
already exists. It sits beside "New scenario" on the top strip and hands you into the walk when it is done.

It is built on the clone path (`scenario_gen.generate_scenario`) rather than assembling files from scratch,
because that path already re-makes every link in the chain correctly — new GUID on both sides, the cluster
re-pointed at its own scenario/script/dico, the per-language text copied so the new operation does not
write through to the one it came from, and the record placed in the right menu group. Building a fresh set
of files would mean re-deriving all of that, which is where the original clone quietly went wrong.

The starter script is the one real choice: keep the source mission's logic, or replace it with a generated
skeleton (`operation_script.starter_operation`) that has camps, an objective and a victory/defeat ending
already wired. Both go through the bundled Python 2.5.1 recompile, and neither is staged unless it
compiles — a script the game cannot load takes the whole mission down.
"""
import tkinter as tk
from tkinter import ttk

import theme
import ui_util
from i18n import t
from ruse_mod_engine import operation_script as ops
from ruse_mod_engine import scenario_chain as chain
from ruse_mod_engine import scenario_gen as SG
from ruse_mod_engine import script_logic as SL

_BG, _PANEL, _WIDGET = theme.BG, theme.PANEL, theme.WIDGET
_TEXT, _DIM, _GOLD, _GOLD_BRT = theme.TEXT, theme.DIM, theme.GOLD, theme.GOLD_BRT
_F, _FB, _FS = theme.F, theme.FB, theme.FS


def _btn(parent, label, command, primary=False):
    return tk.Button(parent, text=label, command=command, background=theme.BTN,
                     foreground=(_GOLD_BRT if primary else _TEXT),
                     activebackground=theme.BTN_ACT, font=_FB, relief="flat", padx=12)


def build_plan(store, source_binding, *, new_stem, name, tracking, kind,
               menu_group="new", fresh_script=False, subtitle="", briefing=""):
    """The whole creation, with no UI: returns the scenario_gen plan (adds/mods keyed by dat).

    Kept UI-free so the hard part is testable without a window — the wizard is a form over this.
    """
    plan = SG.generate_scenario(
        store.dm, store.gd, source_binding.map_dir, source_binding.scenario_name, new_stem,
        name, tracking, kind=kind, src_folder=source_binding.glad_folder,
        ia=store.ia, zz=store.zz, menu_group=menu_group,
        op_name=name, op_subtitle=subtitle, op_briefing=briefing)

    if fresh_script:
        # Replace the cloned mission logic with a generated skeleton. The path comes from the plan, which
        # took it from the DISCOVERED build id - the old generator hardcoded genpython\10000, which does
        # not exist on the shipped public build, so its script silently went nowhere.
        ia_add = plan.get("ia_add") or {}
        if not ia_add:
            raise ValueError("the clone produced no script to replace")
        path = next(iter(ia_add))
        src = ops.generate_source(ops.starter_operation())
        ia_add[path] = SL.recompile_source_to_xyz(src, xyz_for_meta=ia_add[path])
        plan["ia_add"] = ia_add
        plan["fresh_script"] = True
    return plan


class _Store:
    """The four dats the creation reads, opened once."""

    def __init__(self, project):
        self.project = project
        self.gd = _ProjDat(project, "gameplay")
        self.dm = _ProjDat(project, "maps")
        self.ia = _ProjDat(project, "scripts")
        self.zz = _ProjDat(project, "loc")

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


class _ProjDat:
    """One dat of the project, with the list()/get() shape scenario_gen expects."""

    def __init__(self, project, key):
        self.project, self.key = project, key

    def list(self):
        try:
            return self.project.entry_paths(self.key)
        except Exception:
            return []

    def get(self, vp):
        try:
            return self.project.get_raw(self.key, vp)
        except Exception:
            return None

    def read_many(self, paths):
        """Bulk read from ONE archive open. Without this the text pass does ~1000 separate opens of a
        2.4 GB .dat, which does not finish in any useful time."""
        try:
            return self.project.read_many(self.key, list(paths))
        except Exception:
            return {p: self.get(p) for p in paths if self.get(p) is not None}


def open_wizard(parent, project, bindings, *, on_created=None):
    """Show the wizard. `bindings` is {map_dir: [ScenarioBinding]}; `on_created(map_dir, stem)` fires
    after the plan is staged so the host can select the new scenario and drop the user into the walk."""
    sources = [b for v in bindings.values() for b in v
               if b.kind in ("operation", "campaign") and b.has_file and b.has_script]
    sources.sort(key=lambda b: (b.map_dir.lower(), b.scenario_name.lower()))
    if not sources:
        ui_util.error(parent, t("neww.title"), t("neww.no_sources"))
        return

    win = ui_util.themed_toplevel(parent, t("neww.title"), size=(700, 470), resizable=True)
    body = tk.Frame(win, background=_PANEL)
    body.pack(fill="both", expand=True, padx=12, pady=10)

    tk.Label(body, text=t("neww.intro"), background=_PANEL, foreground=_TEXT, font=_F,
             wraplength=660, justify="left", anchor="w").pack(fill="x", pady=(0, 8))

    def row(label):
        r = tk.Frame(body, background=_PANEL)
        r.pack(fill="x", pady=3)
        tk.Label(r, text=label, background=_PANEL, foreground=_GOLD, font=_FS,
                 width=22, anchor="w").pack(side="left")
        return r

    r = row(t("neww.based_on"))
    src_v = tk.StringVar()
    src_cb = ttk.Combobox(r, state="readonly", textvariable=src_v, font=_F,
                          values=["%s / %s  (%s)" % (b.map_dir, b.scenario_name, b.kind)
                                  for b in sources])
    src_cb.pack(side="left", fill="x", expand=True)
    src_cb.current(0)

    r = row(t("neww.name"))
    name_v = tk.StringVar(value="My Operation")
    ttk.Entry(r, textvariable=name_v).pack(side="left", fill="x", expand=True)

    r = row(t("neww.stem"))
    stem_v = tk.StringVar(value="leveldesign_myop")
    ttk.Entry(r, textvariable=stem_v).pack(side="left", fill="x", expand=True)

    r = row(t("neww.tracking"))
    trk_v = tk.StringVar(value="CH99")
    ttk.Entry(r, textvariable=trk_v, width=12).pack(side="left")

    r = row(t("neww.group"))
    grp_v = tk.StringVar(value="new")
    ttk.Combobox(r, state="readonly", textvariable=grp_v, width=28, font=_F,
                 values=["new", "same"]).pack(side="left")
    tk.Label(r, text=t("neww.group_hint"), background=_PANEL, foreground=_DIM, font=_FS,
             anchor="w").pack(side="left", padx=(8, 0))

    r = row(t("neww.script"))
    fresh_v = tk.BooleanVar(value=False)
    tk.Radiobutton(r, text=t("neww.script_copy"), variable=fresh_v, value=False, background=_PANEL,
                   foreground=_TEXT, selectcolor=_WIDGET, activebackground=_PANEL,
                   font=_FS).pack(side="left")
    tk.Radiobutton(r, text=t("neww.script_fresh"), variable=fresh_v, value=True, background=_PANEL,
                   foreground=_TEXT, selectcolor=_WIDGET, activebackground=_PANEL,
                   font=_FS).pack(side="left", padx=(10, 0))

    status = tk.Label(body, text="", background=_PANEL, foreground=_DIM, font=_FS,
                      wraplength=660, justify="left", anchor="w")
    status.pack(fill="x", pady=(10, 0))

    def create():
        src = sources[src_cb.current()]
        stem = stem_v.get().strip()
        if not stem.lower().startswith("leveldesign"):
            status.config(text=t("neww.bad_stem"), foreground=theme.RED)
            return
        store = _Store(project)
        try:
            plan = build_plan(store, src, new_stem=stem, name=name_v.get().strip() or stem,
                              tracking=trk_v.get().strip() or "CH99", kind=src.kind,
                              menu_group=grp_v.get(), fresh_script=fresh_v.get())
        except Exception as e:
            status.config(text=t("neww.failed", e=e), foreground=theme.RED)
            return
        n = _stage(project, plan)
        status.config(text=t("neww.created", n=n, map=src.map_dir, stem=stem), foreground=_TEXT)
        if on_created:
            try:
                on_created(src.map_dir, stem)
            except Exception:
                pass
        win.after(600, win.destroy)

    bar = tk.Frame(body, background=_PANEL)
    bar.pack(fill="x", pady=(10, 0))
    ui_util.flow(bar, [_btn(bar, t("neww.create"), create, primary=True),
                       _btn(bar, t("common.cancel"), win.destroy)])
    win.wait_window()


def _stage(project, plan):
    """Write the plan into the project. Same section->dat mapping the map editor uses; `zz_add` carries
    the per-language in-mission .dic files, and missing it leaves every objective line blank."""
    sections = [("datamap_add", "maps"), ("glad_add", "gameplay"), ("glad_mod", "gameplay"),
                ("ia_add", "scripts"), ("zz_add", "loc"), ("zz_mod", "loc")]
    n = 0
    for sect, dk in sections:
        for vp, blob in (plan.get(sect) or {}).items():
            project.set_raw(dk, vp, blob)
            n += 1
    return n

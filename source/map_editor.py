"""
RUSE Map Editor — Phase 3 test GUI (separate window).

Loads a map's minimap (terrain.png) and overlays the scenario's capture-zone
(sector) meshes, with smooth zoom/pan (Pillow), sector selection + details, and
boundary-vertex editing (drag handles) that saves the edited .scenario.

NOTE on capture: the .scenario zones are the AUTHORING source; in-game capture is
driven by the separate .kdt spatial index, which is NOT derived 1:1 from these
vertices and must be rebuilt from scratch for an edit to take effect in-game (the
KDT builder is the remaining piece). So editing here is "visual/authoring now,
fully functional once the KDT rebuild lands".

Run standalone:  python map_editor.py
or launched as a Toplevel from mod_manager.py via MapEditorWindow(master, ...).
"""
import copy
import glob
import io
import json
import logging
import math
import os
import re
import struct
import sys
import time
import tkinter as tk
from tkinter import ttk, filedialog

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
from ruse_mod_engine import edata, scenario as scenario_mod, kdt as kdt_mod, ndfbin, sdb as sdb_mod  # noqa: E402
from ruse_mod_engine import scenario_registry as screg  # noqa: E402  — map↔scenario kind classification
from ruse_mod_engine import placement_schema as pschema  # noqa: E402  — typed placement field schema
from ruse_mod_engine import scenario_gen  # noqa: E402  — clone-and-register new scenarios (any kind)
from ruse_mod_engine import xyz_compile as xyzc  # noqa: E402  — .xyz mission-script codec (in-project)
from ruse_mod_engine import placement_catalog as pcat  # noqa: E402  — full placeable-class universe
from ruse_mod_engine import placement_roles as proles  # noqa: E402  — per-class icon roles (map symbols)
from ruse_mod_engine import camp_resolver as cresolve  # noqa: E402  — Camp number -> controlling camp/nation
from ruse_mod_engine import dic as dicmod  # noqa: E402  — flash_txt.dic (localized menu names)
import pil_log                                       # noqa: E402  (tags PIL DEBUG as "Map editor")
from i18n import t                                    # noqa: E402
import ui_util                                        # noqa: E402  — language-aware widget sizing
import features  # noqa: E402  — release hold-backs (CLONING_ENABLED)

try:
    from PIL import Image, ImageTk, ImageDraw, ImageFont
    _HAVE_PIL = True
except Exception:
    _HAVE_PIL = False

# High-def terrain decode (issue #8) — optional: needs numpy + Pillow.  Guarded so the editor still
# runs (falling back to the baked minimap) if the codec/numpy isn't available.
try:
    from ruse_mod_engine import terrain_codec as _terrain_codec
except Exception:
    _terrain_codec = None
try:
    from ruse_mod_engine import terrain_tiles as _terrain_tiles
except Exception:
    _terrain_tiles = None
# Shaded relief from the decoded terrain MESH — shows the landform the painted colour can't.
try:
    from ruse_mod_engine import terrain_relief as _terrain_relief
except Exception:
    _terrain_relief = None

import threading
import tempfile

# ── Theme (mirrors mod_manager.py "Field Operations" palette) ──────────────────
import theme                # single source of truth for the palette; local _R_* names kept unchanged
_R_BG        = theme.BG
_R_BG_PANEL  = theme.PANEL
_R_BG_WIDGET = theme.WIDGET
_R_BORDER    = theme.BORDER
_R_GOLD      = theme.GOLD
_R_GOLD_BRT  = theme.GOLD_BRT
_R_TEXT      = theme.TEXT
_R_TEXT_DIM  = theme.DIM
_R_SEL_BG    = theme.SEL_BG
_F_MAIN = theme.F
_F_BOLD = theme.FB


def _settings_roots():
    """(data_root, maps_root) from settings.json's `game_root` (the configured Game directory) — locate
    its Data/PC/<version>/ holding DataMap_Win.dat. Returns (None, None) if not found (caller falls back)."""
    try:
        with open(os.path.join(REPO, "settings.json"), encoding="utf-8") as f:
            gr = (json.load(f) or {}).get("game_root")
        if gr and os.path.isdir(gr):
            pc = os.path.join(gr, "Data", "PC")
            if os.path.isdir(pc):
                for v in sorted(os.listdir(pc)):
                    if os.path.isfile(os.path.join(pc, v, "DataMap_Win.dat")):
                        return os.path.join(pc, v), gr
            for cand in (os.path.join(gr, "Data"), gr):
                if os.path.isfile(os.path.join(cand, "DataMap_Win.dat")):
                    return cand, gr
    except Exception:
        pass
    return None, None


# ── Data layer ─────────────────────────────────────────────────────────────────

def read_bbox(mapinfo_win: bytes):
    """Full-map world bounds from datasmap\\{map}\\mapinfo.win (public format):
    INFO header then minX,minY,maxX,maxY float32 at offset 32. Returns (minx,miny,maxx,maxy)."""
    if not mapinfo_win or mapinfo_win[:4] != b"INFO" or len(mapinfo_win) < 48:
        return None
    minx, miny, maxx, maxy = struct.unpack_from("<ffff", mapinfo_win, 32)
    if maxx <= minx or maxy <= miny:
        return None
    return (minx, miny, maxx, maxy)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def find_terrain_dat(maps_root: str, map_dir: str):
    """Locate the per-terrain DataMap<CamelCaseName>_v09.dat for a map dir."""
    want = _norm(map_dir)
    for p in glob.glob(os.path.join(maps_root, "**", "DataMap*_v09.dat"), recursive=True):
        stem = os.path.basename(p)[len("DataMap"):-len("_v09.dat")]
        if _norm(stem) == want:
            return p
    return None


def load_minimap(terrain_dat_path: str):
    if not terrain_dat_path or not _HAVE_PIL:
        return None
    dat = edata.open_dat(terrain_dat_path)
    for vp in dat.list():
        if vp.lower().endswith("terrain.png"):
            with pil_log.source("Map editor"):
                return Image.open(io.BytesIO(dat.get(vp))).convert("RGBA")
    return None


def map_dirs(dm):
    """Map dirs that have datasmap\\{map}\\mapinfo.win (real terrains) in an open dat."""
    maps = set()
    for vp in dm.list():
        m = re.match(r"datasmap[\\/]([^\\/]+)[\\/]mapinfo\.win$", vp, re.IGNORECASE)
        if m:
            maps.add(m.group(1))
    return sorted(maps)


def discover_maps(data_root: str):
    dm = edata.open_dat(os.path.join(data_root, "DataMap_Win.dat"))
    return map_dirs(dm), dm


def list_scenarios(dm, map_dir: str):
    out = []
    pre = f"test\\map\\{map_dir}\\".lower()
    for vp in dm.list():
        v = vp.replace("/", "\\").lower()
        if v.startswith(pre) and v.endswith(".scenario") and "\\" not in vp.replace("/", "\\")[len(pre):]:
            out.append(os.path.basename(vp)[:-len(".scenario")])
    return sorted(out)


def _boundary_edges(faces):
    """Edges that belong to exactly one triangle = the sector's outline."""
    from collections import Counter
    cnt = Counter()
    for f in faces:
        if len(f) < 3:
            continue
        a, b, c = f[0], f[1], f[2]
        for e in ((a, b), (b, c), (c, a)):
            cnt[tuple(sorted(e))] += 1
    return [e for e, n in cnt.items() if n == 1]


def render_zones_from_scn(scn: dict):
    """Build lightweight render dicts (parallel to scn['zones']) for drawing/editing:
    {scn_i, idx, name, pos:(x,y), verts:[(x,y)], faces, boundary:[(i,j)], bverts:[i...]}."""
    zones = []
    for si, z in enumerate(scn.get("zones", [])):
        verts = [(v[0], v[1]) for v in z.get("vertices", [])]
        faces = [tuple(f) for f in z.get("faces", [])]
        if not verts:
            continue
        boundary = _boundary_edges(faces)
        bverts = sorted({i for e in boundary for i in e})
        pos = z.get("pos")
        pos = (pos[0], pos[1]) if pos else (sum(x for x, _ in verts) / len(verts),
                                            sum(y for _, y in verts) / len(verts))
        zones.append({"scn_i": si, "idx": z.get("a0_zone_idx", si), "name": z.get("name", f"zone{si}"),
                      "pos": pos, "verts": verts, "faces": faces, "boundary": boundary, "bverts": bverts})
    return zones


# ── scenario NDF placements (depots / HQ spawns / labels / zones) ─────────────────
# Each placement is a TGameDesignItem (Position + optional Rotation + AddOn ref); the
# referenced AddOn's class decides the kind.  See memory: project-map-placements-format.
# AddOn class → editor "kind" (display category). NOTE: TGameDesignAddOn_Spawn is the
# UNIVERSAL camp-owned-spawn class — supply depots, pre-placed units, and pre-placed
# buildings all use it, distinguished by PythonClassName. _spawn_kind() sub-classifies
# them. Operations scenarios use Spawn heavily for unit pre-placement (m03_italie ch1
# has 18 real depots + 166 unit/building spawns, all sharing this AddOn class).
_PLACE_KINDS = {
    "TGameDesignAddOn_StartingPoint": "hq",         # player HQ + resting camera, per AllianceNum
    "TGameDesignAddOn_LabelVille":    "ville",      # city name label
    "TGameDesignAddOn_LabelMontagne": "montagne",   # mountain name label
    "TGameDesignAddOn_Name":          "name",       # generic named point
    "TGameDesignAddOn_CircularZone":  "circle",
    "TGameDesignAddOn_RectangleZone": "rect",
}
# Sub-classes for TGameDesignAddOn_Spawn (the camp-owned-spawn AddOn). All share the same
# Camp Int32 field; only depots carry ChampInteger (supply amount).
_SPAWN_DEPOT_PY = "front.batiment_depot.DalleBatimentDepot"
def _spawn_kind(pyclass):
    """Sub-classify a TGameDesignAddOn_Spawn by its PythonClassName.
    'depot' = real supply depot; 'unit'/'building' = pre-placed entities in Operations;
    'spawn' = any other Camp-owned scenario entity."""
    if pyclass == _SPAWN_DEPOT_PY:
        return "depot"
    short = pyclass.rsplit(".", 1)[-1] if pyclass else ""
    if short.startswith("Unit_"):
        return "unit"
    if short.startswith("Building_"):
        return "building"
    return "spawn"
# Inverse mapping — from editor kind back to the AddOn class to instantiate when adding a new
# placement. The four Spawn-derived kinds (depot/unit/building/spawn) all share TGameDesignAddOn_Spawn
# — the PythonClassName written into the new AddOn distinguishes them.
_KIND_TO_ADDON = {
    "depot":    "TGameDesignAddOn_Spawn",
    "unit":     "TGameDesignAddOn_Spawn",
    "building": "TGameDesignAddOn_Spawn",
    "spawn":    "TGameDesignAddOn_Spawn",
    "hq":       "TGameDesignAddOn_StartingPoint",
    "ville":    "TGameDesignAddOn_LabelVille",
    "montagne": "TGameDesignAddOn_LabelMontagne",
    "name":     "TGameDesignAddOn_Name",
    "circle":   "TGameDesignAddOn_CircularZone",
    "rect":     "TGameDesignAddOn_RectangleZone",
}
# marker colours (RGB) per kind
_PLACE_COL = {
    "depot": (90, 170, 235), "hq": (235, 90, 90), "ville": (120, 210, 170),
    "montagne": (200, 180, 120), "name": (180, 180, 200),
    "circle": (200, 150, 230), "rect": (200, 150, 230),
    "unit": (170, 150, 90), "building": (180, 90, 110), "spawn": (140, 100, 170),
    "unknown": (150, 150, 150),
}
# Same colours as hex, built once at import. The "#%02x%02x%02x" % rgb formatting was running per
# marker per frame; there are only a dozen kinds and they never change.
_PLACE_COL_HEX = {k: "#%02x%02x%02x" % v for k, v in _PLACE_COL.items()}

# Per-kind facing offset (radians) — fallback for non-road-locked kinds (item Rotation is radians).
_MODEL_FACING_OFFSET = {"depot": -math.pi / 2.0}

# Offerable multiplayer game modes (see memory: hq-mode-system). For a mode with `teams` × `per_team`
# players, the required HQ spawn set = {(AllianceNum 1..teams, AlliancePriority 1..per_team)}; total
# NbPlayers = teams·per_team. `gametype`/`gmm` + `dispo` are the TMultiMapInfo fields that make the
# lobby OFFER the mode (gmm = number of teams; GameType: 2-team uses per_team [1v1=1,2v2=2,3v3=3,4v4=4],
# 2v2v2=5, 2v2v2v2=6, FFA=None).  (key, label, teams, per_team, gametype, gmm, dispo_flag)
_GAME_MODES = [
    ("1v1",     "1v1",      2, 1, 1,    2, "DispoMulti2Teams"),
    ("2v2",     "2v2",      2, 2, 2,    2, "DispoMulti2Teams"),
    ("3v3",     "3v3",      2, 3, 3,    2, "DispoMulti2Teams"),
    ("4v4",     "4v4",      2, 4, 4,    2, "DispoMulti2Teams"),
    ("2v2v2",   "2v2v2",    3, 2, 5,    3, "DispoMulti3Teams"),
    ("2v2v2v2", "2v2v2v2",  4, 2, 6,    4, "DispoMulti4Teams"),
    ("ffa3",    "FFA (3)",  3, 1, None, 1, "DispoMultiFFA"),
    ("ffa4",    "FFA (4)",  4, 1, None, 1, "DispoMultiFFA"),
    ("ffa6",    "FFA (6)",  6, 1, None, 1, "DispoMultiFFA"),
    ("ffa8",    "FFA (8)",  8, 1, None, 1, "DispoMultiFFA"),
]

# ── XYZ0 container codec (cracked in tools/_ai_xyz_repack.py — round-trips byte-identically) ──
# Layout: b"XYZ0\n" (5) + version b"\x0d\xf2\xb3\x00" (4) + uncompressed-size 3-byte BE (3)
# + hash16 (16, NOT validated at load — embedded CPython) + zlib(Python-2.5 marshal blob).
# Reading: unpack → zlib.decompress → xdis.unmarshal.load_code(magic=62131) → uncompyle6.decompile
# → readable Python source. Writing (source-text → XYZ0) is SOLVED: recompile with the game's own
# Python 2.5.1 via ruse_mod_engine/script_logic.recompile_source_to_xyz (bundled python251/) — the
# golden path (in-game proven). See docs/operation_editor/05-editing-a-script.md.
_XYZ_MAGIC = b"XYZ0\n"
_XYZ_VER = b"\x0d\xf2\xb3\x00"
_XYZ_PY_MAGIC_INT = 62131          # CPython 2.5 marshal magic (0xf2b3) — the version the game embeds
def _xyz_unpack(b):
    """Return (marshal_bytes, hash16, declared_size) or raise ValueError."""
    import zlib as _zlib
    if b[:5] != _XYZ_MAGIC:
        raise ValueError("not XYZ0 (magic=%r)" % b[:5])
    if b[5:9] != _XYZ_VER:
        raise ValueError("unexpected XYZ0 version marker %s" % b[5:9].hex())
    size = int.from_bytes(b[9:12], "big")
    h16 = b[12:28]
    marshal_bytes = _zlib.decompress(b[28:])
    if len(marshal_bytes) != size:
        raise ValueError("container size %d != marshal size %d" % (size, len(marshal_bytes)))
    return marshal_bytes, h16, size
# These thin wrappers delegate to ruse_mod_engine (the single in-project codec) so the editor and
# engine never drift. The game embeds CPython 2.5.1: decompile works via xdis+uncompyle6 with NO
# interpreter; source-compile routes through script_logic -> the bundled Python 2.5.1 (python251/) —
# the only bytecode the 2.5.1 game VM runs correctly.
def _xyz_decompile_to_source(marshal_bytes):
    return xyzc.decompile(marshal_bytes)
def _xyz_pack(marshal_bytes, hash16=b"\x00" * 16):
    return xyzc.pack(marshal_bytes, hash16)
def _xyz_compiler_path():
    from ruse_mod_engine import script_logic as _sl
    return _sl._py251_interpreter()
def _xyz_compile_source(source_text, timeout=20):
    return xyzc.compile_source(source_text, timeout=timeout)

# Starter Python-2 .xyz template for the Script Editor's "create a new script" path. Skeletal —
# imports + a single VariableCamp for the player + a stub win condition the user is expected to
# customise. Two-space indent matches the game's own decompiled .xyz scripts.
# The script editor's hand-written reference panel lived here (a ~270-line list of docs, recipes and
# paste snippets, plus a _SCRIPT_SNIPPETS alias). It went with the popup it fed: the Mission Rules &
# Script step reads the DSL catalog instead — 1,133 classes with real parameters, types, defaults and
# enum domains, of which 99.4% of the beats in a shipped script resolve — so the panel is generated from
# the game's own data rather than maintained by hand.

# Auto-snap-to-roads: world-unit perpendicular distance a road-bound building sits from the nearest
# road LINE, in the EDITOR's road-graph frame (read_road_graph + the point→segment distance used by
# _nearest_road_point). These are MEASURED empirically across ALL shipped scenarios, not guessed and
# NOT the game's own TBatimentDescriptor.DistanceToRoad (which is measured against the game's true road
# centerline — depot=5000, hq=11700 — and matches neither our node nor our line metric because the
# cracked mapinfo.win graph isn't that centerline). See docs/map_editor/placements_and_roads.md §9 and
# tools/test_scripts/measure_road_offsets.py (writes tools/output/road_offset_report.md).
#   depot: line-median 11696 over 1554 placements / 31 maps; per-map spread only ~1900u → map-stable.
#          (node-median 12018 — the node-vs-line choice moves it <350u, so ~11700 is robust.)
#   hq:    line-median 13580 over 353 placements. Corrects the old 17000, which came from just 2 blitz
#          HQs; the 353-sample number is ~13600.
# Only depot + HQ are road-placed consistently enough across scenarios to trust. Other buildings
# (factories/airfield/barracks/admin/prototype) are pre-placed freely by Operations designers, so they
# have no reliable scenario-derived offset — when they become editor placements they'll need in-game
# calibration, not this table. Bunkers/defenses and units/zones are NOT road-bound (absent here on purpose).
_ROAD_SNAP_OFFSET = {"depot": 11696.0, "hq": 13580.0}

# On-canvas placement icons. icons/map_icons/roles/*.png is one picture PER ROLE — see
# ruse_mod_engine/placement_roles.py for what a role is and where the classification comes from (the
# game's own TypeUnitHintToken / NameInMenuToken / Factory fields), and
# tools/test_scripts/gen_map_icons.py for the drawing.
#
# Before this, everything but a depot or an HQ was a coloured dot: a Tiger, a paratrooper, an MG nest
# and a script waypoint were four dots in three colours. Now every placeable class draws as itself.
# icons/map_icons/*.png (the flat legacy palette) is kept for the older popup UI.
#
# Resolved through _MEIPASS when frozen, exactly like i18n does for lang/: __file__ points inside the
# unpacked bundle for a onefile exe, so a REPO-relative path would miss. (Before this, icons/ wasn't
# bundled at all — the two icons the editor used just silently fell back to coloured squares in every
# released build. build.py now ships the folder; see _SRC_DIRS and the spec's datas.)
def _icon_root():
    base = getattr(sys, "_MEIPASS", None) if getattr(sys, "frozen", False) else None
    return os.path.join(base or REPO, "icons", "map_icons")


ICON_DIR = _icon_root()
ROLE_ICON_DIR = os.path.join(ICON_DIR, "roles")
# Nation roundels (icons/nation_icons/*.png) composited onto the corners of a placement
# icon — see MapEditor._build_overlay_icon. Shipped 64 px; scaled down per use.
NATION_ICON_DIR = os.path.join(os.path.dirname(ICON_DIR), "nation_icons")

# Icon pixel size per "Icon size" setting. Quantised on purpose: the PhotoImage cache is keyed by
# size, so a continuously zoom-derived size would rebuild every icon on every wheel notch.
_ICON_SIZES = {"off": 0, "small": 21, "medium": 28, "large": 37}
_ICON_SIZE_LABELS = [("off", "map.icons_off"), ("small", "map.icons_small"),
                     ("medium", "map.icons_medium"), ("large", "map.icons_large")]


def _camp_display(camp):
    """The camp NUMBER to show for a placement, treating an absent Camp as camp 0.

    An absent `Camp` property is not "no owner": the engine reads the NDF integer default, so it
    resolves through camp_dico exactly like any other value and the placement spawns under camp 0
    (see ruse_mod_engine/camp_resolver). Showing nothing there made a perfectly normal placement
    look unowned.

    Drawing it as 0 is unambiguous because an EXPLICIT 0 cannot occur: it appears in none of the 102
    shipped scenarios, and the editor's own Camp choices are [none, -1, 1..9] — there is no 0 to
    pick. So `0` on a marker always means "no Camp property, i.e. the first camp"."""
    return 0 if camp is None else camp


def _camp_str(camp):
    """Decode a TGameDesignAddOn_Spawn `Camp` Int32 value into a human label.

    Encoding: null = camp 0 (Team 1); Camp = N (≥ 1) = camp N (Team N + 1); special values −1
    (visible neutral) and −2 (Operations despawn). On a depot in an MP scenario, null specifically
    means despawned at match start; on any other Spawn null is camp 0 — the engine resolves by
    scenario type, the editor labels both readings so the user can see both.

    RESOLVED (was "known unknown"): the value is a direct key into the engine's `camp_dico`, so an
    ABSENT Camp reads the NDF integer default and resolves as camp 0 — it is not a separate
    "no owner" state, and the placement spawns normally. The map therefore draws it as `0`
    (see _camp_display). An EXPLICIT 0 cannot be confused with it: 0 occurs in none of the 102
    shipped scenarios and the editor's Camp choices are [none, −1, 1..9]. See
    ruse_mod_engine/camp_resolver.py for the engine code this comes from."""
    if camp is None:
        return t("map.null_team_1_despawn_mp")
    if camp == -1:
        return t("map.1_neutral_visible_mp")
    if camp == -2:
        return t("map.2_despawn_operations")
    return t("map.camp_team_team", camp=camp, team=camp + 1)


def _pad4(b: bytes) -> bytes:
    """Pad to a 4-byte boundary — RUSE pads the embedded scenario NDF this way, so this makes
    a re-emitted (edited) NDF byte-identical to the original when nothing changed."""
    return b + b"\x00" * ((4 - len(b) % 4) % 4)


def _ndf_prop(ndf, inst, name):
    """NdfValue of property `name` on `inst` (resolved within the instance's class), or None."""
    p = ndf.prop_by_name_and_class(name, inst.class_index)
    return inst.get(p.index) if p is not None else None


def script_vpath_in(paths, map_dir, scn_name):
    """The paired effetmap.xyz virtual path within `paths`, or None.

    Same convention as MapEditor._script_path_for_scn (leveldesign<suffix>.scenario pairs with
    scripting<suffix>/effetmap.xyz) but matched against a path list instead of built from the live
    buildid, so callers that already hold the IA_Common listing (and tests) can use it.
    A None result means the scenario has no script — i.e. it is multiplayer, where camps are lobby
    slots carrying allegiance only and no nationality exists to resolve."""
    suffix = scn_name[len("leveldesign"):] if scn_name.startswith("leveldesign") else ""
    tail = ("test\\map\\%s\\scripting%s\\effetmap.xyz" % (map_dir, suffix)).lower()
    for p in paths:
        if p.lower().endswith(tail):
            return p
    return None


def parse_placements(ndf):
    """Extract editable placement items from a scenario NDF as a list of render/edit dicts:
    {item_idx, addon_idx, kind, pos:(x,y,z), rot, label, extra{...}}."""
    out = []
    item_cls = ndf.class_by_name("TGameDesignItem")
    if item_cls is None:
        return out
    for ii, inst in enumerate(ndf.instances):
        if inst.class_index != item_cls.index:
            continue
        posv = _ndf_prop(ndf, inst, "Position")
        if posv is None:
            continue
        rotv = _ndf_prop(ndf, inst, "Rotation")
        addonv = _ndf_prop(ndf, inst, "AddOn")
        kind, addon_idx, label, extra = "unknown", None, "", {}
        raw = addonv.raw if addonv else None
        if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], tuple):
            addon_idx = raw[1][0]
            if 0 <= addon_idx < len(ndf.instances):
                ainst = ndf.instances[addon_idx]
                cls_name = ndf.classes[ainst.class_index].name
                if cls_name == "TGameDesignAddOn_Spawn":
                    # Universal camp-owned-spawn AddOn (depots + units + buildings). Sub-classify
                    # by PythonClassName so callers can tell a real supply depot from a pre-placed
                    # unit/building — they used to all be labelled "depot" (incorrectly).
                    pcv = _ndf_prop(ndf, ainst, "PythonClassName")
                    py = ndf.get_string(pcv.raw) if pcv and isinstance(pcv.raw, int) else ""
                    kind = _spawn_kind(py)
                    extra["pyclass"] = py
                    cv = _ndf_prop(ndf, ainst, "Camp"); extra["camp"] = cv.raw if cv else None
                    if kind == "depot":
                        ci = _ndf_prop(ndf, ainst, "ChampInteger"); extra["champ"] = ci.raw if ci else None
                        label = "depot"
                    else:
                        short = py.rsplit(".", 1)[-1] if py else "?"
                        label = "%s:%s" % (kind, short)
                else:
                    kind = _PLACE_KINDS.get(cls_name, "unknown")
                    if kind in ("ville", "montagne"):
                        cv = _ndf_prop(ndf, ainst, "ChampTexte")
                        label = cv.raw if cv else ""
                    elif kind == "hq":
                        av = _ndf_prop(ndf, ainst, "AllianceNum")
                        extra["alliance"] = av.raw if av else None
                        pr = _ndf_prop(ndf, ainst, "AlliancePriority")
                        extra["priority"] = pr.raw if pr else None      # slot within the team
                        cam = _ndf_prop(ndf, ainst, "PositionCamera"); extra["cam"] = cam.raw if cam else None
                        azi = _ndf_prop(ndf, ainst, "Azimut");        extra["azimut"] = azi.raw if azi else None
                        sit = _ndf_prop(ndf, ainst, "Site");          extra["site"] = sit.raw if sit else None
                        wcp = _ndf_prop(ndf, ainst, "WarmupCamPath")  # -> warmup TCameraPath name (the real start camera)
                        extra["warmup"] = (ndf.get_string(wcp.raw) if wcp and wcp.type_id == ndfbin.T.StringRef else None)
                        extra["campath_keys"] = []
                        # priority=None is the vanilla FFA-seat encoding — show as '*' so it's
                        # visually distinct from the team-mode slot-1 case (also formerly "HQ A1.1").
                        _pri = extra.get("priority")
                        label = "HQ A%s.%s" % (extra.get("alliance"), "*" if _pri is None else _pri)
                    elif kind == "circle":
                        rv = _ndf_prop(ndf, ainst, "Radius"); extra["radius"] = (rv.raw if rv else 0.0)
                        label = "zone"
                    elif kind == "rect":
                        wv = _ndf_prop(ndf, ainst, "Width"); hv = _ndf_prop(ndf, ainst, "Height")
                        extra["w"] = (wv.raw if wv else 0.0); extra["h"] = (hv.raw if hv else 0.0)
                        label = "zone"
        # Resolve the icon role ONCE, here — _draw_placements runs for every marker on every pan and
        # zoom frame, so it must not be doing dictionary lookups per placement per frame.
        out.append({"item_idx": ii, "addon_idx": addon_idx, "kind": kind,
                    "pos": tuple(posv.raw), "rot": (rotv.raw if rotv else None),
                    "label": label, "extra": extra,
                    "role": proles.role_for(kind, extra.get("pyclass")),
                    # The unit's OWN nationality (fixed by its class). The CONTROLLING camp's
                    # nation is filled in later as `ctrl_nation` — it needs the paired script,
                    # which is loaded off the UI thread (see MapEditor._load_camp_map_async).
                    "nation": proles.nation_for(extra.get("pyclass")),
                    "ctrl_nation": None})
    return out


def _mapinfo_buffers(win_bytes, count=4):
    """First `count` length-prefixed buffers of mapinfo.win after the 48-byte header
    ('INFOIA\\r\\n'(8) md5(16) u32(=20) u32(=6) bbox(16)). buf[0]=road graph (TMapInfo+0x18),
    buf[3]=forest SDB (TMapInfo+0x30). Reverse-engineered from the mapinfo.win loader format."""
    if not win_bytes or win_bytes[:8] != b"INFOIA\r\n" or len(win_bytes) < 56:
        return []
    out, p = [], 48
    for _ in range(count):
        if p + 4 > len(win_bytes):
            break
        ln = struct.unpack_from("<I", win_bytes, p)[0]; p += 4
        if ln < 0 or p + ln > len(win_bytes):
            break
        out.append(win_bytes[p:p + ln]); p += ln
    return out


def _mapinfo_buffer1(win_bytes):
    bufs = _mapinfo_buffers(win_bytes, 1)
    return bufs[0] if bufs and len(bufs[0]) >= 12 else None


def read_forest_zones(win_bytes, layer_bit=0x08):
    """Forest CONCEALMENT zones from mapinfo.win buffer4 = an 'SDB' SparseSpatialStateDatabaseStatic
    quadtree (reverse-engineered from the SDB parse/query format and the forest-cover evaluator).
    These are the PRE-GENERATED zones where the game grants forest cover (GetIsEnForet),
    NOT the visual trees. Header: 'SDB\\r\\n'(5) md5(16) u32 ver u32 payloadlen .. f32 bboxX f32 bboxY
    f32 cell; payload (u32 quadtree) at +57. Node V: (V&1)->leaf (4 quadrant occupancy bytes); else
    internal, 4-child block at index (V-0x1c)>>2. layer_bit 0x08 = en_foret (the query's mask).
    Returns {"cells": [(x0,y0,x1,y1)], "bbox": (0,0,bboxX,bboxY)} in world space, or None."""
    bufs = _mapinfo_buffers(win_bytes, 4)
    if len(bufs) < 4:
        return None
    g = bufs[3]
    if len(g) < 49 or g[:5] != b"SDB\r\n":
        return None
    try:
        payloadlen = struct.unpack_from("<I", g, 25)[0]
        bboxX = struct.unpack_from("<f", g, 37)[0]
        bboxY = struct.unpack_from("<f", g, 41)[0]
    except struct.error:
        return None
    PB = 29 + 28                                   # 29-byte prefix + 28-byte header
    n = (payloadlen - 0x1C) >> 2
    if n <= 0 or PB + 4 * n > len(g) or bboxX <= 0 or bboxY <= 0:
        return None
    ents = [struct.unpack_from("<I", g, PB + 4 * i)[0] for i in range(n)]
    cells, seen = [], set()
    stack = [(0, 0.0, 0.0, bboxX, bboxY, 0)]       # iterative quadtree walk
    while stack:
        i, x0, y0, x1, y1, d = stack.pop()
        if not (0 <= i < n) or d > 20:
            continue
        V = ents[i]
        mx, my = (x0 + x1) * 0.5, (y0 + y1) * 0.5
        quads = ((x0, y0, mx, my), (mx, y0, x1, my), (x0, my, mx, y1), (mx, my, x1, y1))
        if V & 1:                                  # leaf: 4 quadrant occupancy bytes
            for q in range(4):
                if (V >> (q * 8)) & layer_bit:
                    cells.append(quads[q])
        else:                                      # internal: 4-child block
            ci = (V - 0x1C) >> 2
            if (i, ci) in seen:
                continue
            seen.add((i, ci))
            for q in range(4):
                qx0, qy0, qx1, qy1 = quads[q]
                stack.append((ci + q, qx0, qy0, qx1, qy1, d + 1))
    return {"cells": cells, "bbox": (0.0, 0.0, bboxX, bboxY)}


def read_road_graph(win_bytes):
    """Parse the EXACT road network from datasmap\\<map>\\mapinfo.win — authoritative, reverse-engineered
    from the game's own road-graph format.

    The graph is buffer1 (TMapInfo+0x18). Layout:
        u16 @0 point-count   u16 @2 edge-count   u32 @4 off_points   u32 @8 off_edges
        POINTS @off_points : 12-byte [u32 csr][f32 x][f32 y]   (coords at +4,+8)
        EDGES  @off_edges  : 6-byte  [u16 ptA][u16 ptB][u16 lenQ]   (lenQ>>1 = round(dist/20))
    The 6-byte edge records are the real adjacency (verified 100%: every edge joins two valid points,
    dist/lenQ ~= const 20). `degree` is recomputed from edge incidence so junctions/endpoints render
    correctly. (lenQ bit0 is a half-edge/direction marker, not a bridge flag; MP maps have no separate
    bridge entity — a bridge is just a road edge crossing a river.)"""
    g = _mapinfo_buffer1(win_bytes)
    if g is None:
        return None
    try:
        npts, nedg = struct.unpack_from("<HH", g, 0)
        off_pts, off_edg = struct.unpack_from("<II", g, 4)
    except struct.error:
        return None
    if off_pts + 12 * npts > len(g) or off_edg + 6 * nedg > len(g):
        return None
    pts = []
    for i in range(npts):
        x, y = struct.unpack_from("<ff", g, off_pts + 12 * i + 4)
        pts.append([x, y, 0])           # degree filled below from edges
    edges = []
    for i in range(nedg):
        a, b, _lenq = struct.unpack_from("<HHH", g, off_edg + 6 * i)
        if a < npts and b < npts and a != b:
            edges.append((a, b))
            pts[a][2] += 1; pts[b][2] += 1
    if not pts:
        return None
    nodes = [(x, y, deg) for x, y, deg in pts]
    return {"nodes": nodes, "edges": edges}


# distinct sector fill colours (cycled)
_SECTOR_COLORS = [
    (224, 96, 64), (96, 176, 224), (120, 200, 110), (224, 192, 48),
    (190, 120, 220), (240, 150, 70), (90, 200, 200), (220, 110, 160),
    (150, 200, 80), (130, 150, 230), (210, 200, 120), (200, 90, 90),
]
_HANDLE_R = 5   # px hit radius for boundary-vertex handles



# KDT triangle->sector ranges, cracked via runtime analysis (contiguous index blocks per
# sector). Keyed by (map_dir, scenario). Each entry: (start_tri, end_tri_inclusive, a0_zone_idx).
# For maps without an entry, the overlay falls back to geometric (centroid-in-scenario-zone).
KDT_SECTOR_RANGES = {
    ("supercrossroads4", "leveldesign_normal"): [
        (0, 8, 0), (9, 13, 1), (14, 25, 4), (26, 41, 3), (42, 56, 8),
        (57, 71, 2), (72, 82, 5), (83, 88, 6), (89, 93, 7),
    ],
}

# Runtime-captured KDT meshes — the REAL VtxBufIdx verts + IndexBuffer the
# game's SIMD codec produces (which we don't decode offline). The on-disk verts (kf.verts) are a
# DIFFERENT array, so the indices only reconstruct correct triangles against these dumped verts.
_KDT_VERTDUMP_PATH = os.path.join(REPO, "test_output", "kdt_re", "kdt_verts_capture.jsonl")
_kdt_vertdumps_cache = None


def load_kdt_vertdumps():
    """Parse the runtime vert-dump jsonl → list of {bmin,bmax, world:[(x,y)], tris:[(a,b,c)]}.
    Each mesh: 10-byte VtxBufIdx verts (coords u16 @+4,+6,+8, world = bmin+q/32767*(bmax-bmin)) +
    IndexBuffer (3 indices/triangle). Matched to a loaded KDT by bbox. Empty if no capture yet."""
    global _kdt_vertdumps_cache
    if _kdt_vertdumps_cache is not None:
        return _kdt_vertdumps_cache
    out = []
    try:
        import json
        with open(_KDT_VERTDUMP_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                if "verts" not in r or "idx" not in r:
                    continue
                vb = bytes.fromhex(r["verts"]); ib = bytes.fromhex(r["idx"])
                bmin, bmax = r["bmin"], r["bmax"]
                bx = bmax[0] - bmin[0]; by = bmax[1] - bmin[1]
                world = []
                for i in range(r["vcount"]):
                    o = i * 10
                    qx = struct.unpack_from("<H", vb, o + 4)[0]
                    qy = struct.unpack_from("<H", vb, o + 6)[0]
                    world.append((bmin[0] + qx / 32767.0 * bx, bmin[1] + qy / 32767.0 * by))
                tri = r["triCount"]
                if r.get("wide"):
                    idx = [struct.unpack_from("<I", ib, 4 * k)[0] for k in range(tri * 3)]
                else:
                    idx = [struct.unpack_from("<H", ib, 2 * k)[0] for k in range(tri * 3)]
                tris = [(idx[3 * t], idx[3 * t + 1], idx[3 * t + 2]) for t in range(tri)
                        if max(idx[3 * t], idx[3 * t + 1], idx[3 * t + 2]) < len(world)]
                out.append({"bmin": bmin, "bmax": bmax, "world": world, "tris": tris})
    except (OSError, ValueError, KeyError, struct.error):
        pass
    _kdt_vertdumps_cache = out
    return out


def match_kdt_vertdump(bbox_min, bbox_max, tol=2.0):
    """Find a dumped KDT mesh whose bbox matches the loaded KDT (XY within tol)."""
    for d in load_kdt_vertdumps():
        if (abs(d["bmin"][0] - bbox_min[0]) < tol and abs(d["bmin"][1] - bbox_min[1]) < tol and
                abs(d["bmax"][0] - bbox_max[0]) < tol and abs(d["bmax"][1] - bbox_max[1]) < tol):
            return d
    return None


def kdt_sector_borders(tris, tri_sector):
    """Phase-1 border model. A boundary edge of sector S = an edge in exactly ONE of S's triangles
    (the sector outline; shared borders are boundary edges of both neighbours). Returns
    (set of border vert indices, {sector: [ordered boundary vert-index loops]}). Border verts are the
    draggable handles; moving one deforms every triangle that uses it (auto-welds shared borders)."""
    from collections import defaultdict, Counter
    border = set()
    loops_by_sector = {}
    for S in sorted(s for s in set(tri_sector) if s is not None):
        ec = Counter()
        for ti, tri in enumerate(tris):
            if tri_sector[ti] != S:
                continue
            a, b, c = tri
            for e in ((a, b), (b, c), (c, a)):
                ec[(min(e), max(e))] += 1
        bedges = [e for e, n in ec.items() if n == 1]
        adj = defaultdict(list)
        for (u, v) in bedges:
            adj[u].append(v); adj[v].append(u); border.add(u); border.add(v)
        loops, used = [], set()
        for (u0, v0) in bedges:
            if (u0, v0) in used:
                continue
            loop = [u0]; used.add((u0, v0)); prev, cur = u0, v0
            while cur != u0:
                loop.append(cur)
                nxt = next((w for w in adj[cur] if w != prev and (min(cur, w), max(cur, w)) not in used), None)
                if nxt is None:
                    break
                used.add((min(cur, w := nxt), max(cur, nxt))); prev, cur = cur, nxt
            loops.append(loop)
        loops_by_sector[S] = loops
    return border, loops_by_sector


def kdt_vertex_adjacency(tris):
    """vert index -> sorted list of edge-neighbour vert indices (from the triangle mesh).
    Drives interior Laplacian relaxation: an interior node's optimal position is the average of
    its neighbours, so with the boundary pinned the interior settles to the harmonic solution."""
    from collections import defaultdict
    adj = defaultdict(set)
    for (a, b, c) in tris:
        adj[a].update((b, c)); adj[b].update((a, c)); adj[c].update((a, b))
    return {v: sorted(ns) for v, ns in adj.items()}


# ── GUI ─────────────────────────────────────────────────────────────────────────

class MapEditorWindow(tk.Frame):
    """Embedded as a nested in-tab view (formerly a Toplevel); the Mod Editor hosts it + the Back bar."""
    # Archive entry paths the editor reads/writes inside the gameplay dat (lobby / game modes).
    _GLOBALS_PATH = r"genglad\patchable\misc\globals.cpp.gladndfbin"
    _MAPINFO_PATH = r"genglad\patchable\mapinfo.cpp.gladndfbin"

    def __init__(self, master=None, project=None, on_change=None):
        super().__init__(master)
        # The editor saves into a mod PROJECT (output/editor_mods/<mod>/...), exactly like the Units/AI/
        # Economy windows — it NEVER writes the live game files. It must therefore be opened from the Mod
        # Editor hub with a project loaded (standalone main() builds a throwaway dev project).
        if project is None:
            self.destroy()
            raise RuntimeError("MapEditorWindow requires a ModProject — open it from the Mod Editor hub.")
        self.project = project
        self._on_change = on_change          # hub callback to refresh the unsaved-changes indicator
        self.configure(background=_R_BG)

        # Minimaps (the per-terrain DataMap*_v09.dat files) are loaded through the project just like
        # every other dat — the mod's Maps/PC copy if present, else the clean backup, else the game
        # (read-only display here).  No separate game-install path is needed; see _load_minimap().

        # view state
        self._pil = None
        self._bbox = None
        self._terrain_token = 0      # bumps per map change; stale background decodes are discarded (#8)
        self._terrain_result = None  # worker -> main-thread hand-off slot (polled, never after())
        self._tiles_dirty = False    # a decode worker landed chunks; the poll repaints for it
        self._redraw_scheduled = False  # coalesce pan/zoom/drag redraws onto one idle callback (perf)
        # status bar = one shared line: a MAIN part (map/scenario · cursor · zoom · [view]) plus a live
        # terrain-decode SUFFIX, so the decode % and the cursor/zoom readout no longer clobber each other.
        self._status_main = ""
        self._terrain_status = ""
        self._scn = None            # full scenario dict (editable source of truth)
        # The virtual path self._scn / self._pndf were actually READ FROM, recorded at load time.
        # Saving MUST use this, never the live comboboxes: the comboboxes are what the user is
        # *pointing at*, which is not always what is *loaded* (a cancelled "discard changes?" leaves
        # the box on the new map while the old scenario is still in memory). Writing loaded-scenario
        # bytes to a pointed-at path is how a scenario ends up inside the wrong map.
        self._scn_vpath = None      # e.g. "test\map\supercrossroads4\leveldesign.scenario"
        self._scn_src = None        # (map_dir, scn) the loaded scenario came from
        self._zones = []            # render dicts (parallel-ish to scn['zones'])
        self._sel = None            # index into self._zones
        self._scale = 1.0
        self._ox = 0.0
        self._oy = 0.0
        self._flip_y = tk.BooleanVar(value=False)   # OFF lines up with the terrain textures
        self._edit = tk.BooleanVar(value=False)
        self._lbl_var = tk.BooleanVar(value=True)
        self._show_sectors = tk.BooleanVar(value=True)   # scenario AREA-zone polygons (visual sectors)
        self._dirty = False
        self._dm = None
        # press/drag tracking
        self._press = None
        self._moved = False
        self._drag_vi = None        # boundary vertex index being dragged
        self._clusters = []         # welded corners: [{'members':[(zi,vi)..]}] across all zones
        self._drag_cluster = None   # cluster index being dragged (welds all member corners)
        self._sector_rings = {}     # scn-zone-index -> ring model (outer/inner rings, width, fill) for hooked editing
        self._drag_orig = None      # {zi: [(x,y)..]} snapshot at press_start for soft falloff
        self._drag_p0 = None        # grabbed corner world pos at press_start
        self._border_w = 0.0        # f3 border-width param (default soft radius basis)
        self._soft_radius = tk.StringVar(value="")  # falloff radius (world units); 0 = rigid corner
        # composite cache (excludes the selected sector so editing it stays cheap)
        self._comp_tk = None
        self._comp_key = None
        self._panning = False       # a pan drag is live -> slide the cached composite, rebuild on release
        self._pan_slid = (0.0, 0.0)  # screen offset the drawn frame has been slid since the last
        #   full redraw; bounded by _pan_slide_budget so the slid-in edge is still covered
        self._zooming = False       # a wheel burst is live -> rescale the composite, sharpen on settle
        self._zoom_after = None     # pending after() id for the post-wheel sharp render
        self._comp_img = None       # the composite as a PIL image (source for the zoom preview)
        self._comp_scale = 1.0      # the scale _comp_img was rendered at
        self._comp_src_rect = None  # base-px region _comp_img covers (fixed; _comp_rect can shrink)
        self._ov_crop_cache = {}    # (layer, box, w, h, id) -> cropped+resized overlay
        self._last_comp_build = 0.0  # monotonic stamp of the last composite rebuild (pan throttle)
        self._comp_rect = None      # base-pixel region the cached composite covers (viewport render)
        self._comp_is_preview = False  # current frame is a zoom preview: covers ONLY the viewport,
        #   with no margin, so a pan may not slide on it (it would expose blank at the leading edge)
        # ── view/data render split (perf): the static overlays are rasterised ONCE into whole-map
        # bitmaps in "overlay space" (base pixels × supersample) and only crop+resized on pan/zoom
        # (a C-level PIL op), instead of redrawing every vector primitive each frame. Two independent
        # caches so a paint stroke rebuilds ONLY the cheap numpy SDB raster, never the KDT/road vectors.
        self._ov_scale = 1          # overlay supersample factor (base px -> overlay px)
        self._ov_sdb = None         # cached RGBA of the SDB paint layers (whole map, overlay space)
        self._ov_sdb_key = None
        self._ov_vec = None         # cached RGBA of sectors + KDT mesh + roads (whole map, overlay space)
        self._ov_vec_key = None
        self._ov_sdb_dirty = True   # SDB raster needs (re)building (paint / layer toggle / load)
        self._ov_vec_dirty = True   # vector raster needs (re)building (sector/KDT/road change / load)
        # Shaded relief (whole map, its OWN aspect-correct raster — not overlay space, so it never
        # has to be rebuilt when the base image swaps from minimap to tiles). Built once per map on
        # a worker and cached to disk; None until that lands, and the toggle just hides it.
        self._ov_relief = None
        self._relief_token = 0      # invalidates an in-flight relief build on map change/teardown
        self._relief_busy = False
        self._relief_result = None  # worker -> main-thread hand-off slot (polled, never after())
        self._dragging = False      # a sector/KDT vertex drag is live -> freeze the vec overlay (stale
        #   fill is fine; the moving handles/outline are drawn as live canvas items), rebuild on release
        # KDT (mechanics mesh) overlay + global transform preview
        self._kf = None             # kdt.read_full of the map's KDT
        self._kdt_bytes = None      # original KDT bytes (for transform + save)
        self._kdt_vpath = None      # the KDT's path inside the dat
        self._kdt_world = []        # [(x,y)] world XY per KDT vertex (untransformed)
        self._kdt_tris = []         # [(i,j,k)] triangle vertex indices
        self._kdt_tri_sector = []   # per-triangle a0_zone_idx (or None) — colours the overlay
        self._kdt_color = tk.BooleanVar(value=False)  # colour the KDT overlay wireframe by sector
        #   (off = flat cyan). The scenario polygons are the coloured zones; the KDT just follows them.
        self._kdt_show = tk.BooleanVar(value=False)  # KDT overlay HIDDEN (KDT editing is shelved for now)
        self._kdt_dx = tk.StringVar(value="0")
        self._kdt_dy = tk.StringVar(value="0")
        self._kdt_scale = tk.StringVar(value="1.0")
        # Phase-1 sector-border editing: drag a KDT boundary vertex; every triangle that uses it
        # reshapes (shared verts auto-weld neighbouring sectors). Save rewrites the mesh via encode_mesh.
        self._kdt_edit = tk.BooleanVar(value=False)  # border-drag mode (vs. the rigid transform)
        self._kdt_dirty = False                      # KDT vertices moved -> save via encode_mesh
        self._kdt_bverts = set()                     # border vertex indices (the draggable handles)
        self._kdt_borders = {}                       # {sector: [ordered boundary vert-index loops]}
        self._kdt_adj = {}                           # vert -> [edge-neighbour verts] (connection graph)
        self._kdt_orig = []                          # node positions at load (baseline for geometry-saving fit)
        self._kdt_relax = tk.BooleanVar(value=True)   # geometry-saving interior optimize (Laplacian
        #   surface EDITING, δ-preserving) — gentle/proportional, keeps triangles clean. NOTE: the OLD
        #   neighbour-average SMOOTHING (which this replaced) is what broke zones; this is different.
        # KDT FOLLOWS the scenario sectors (the sectors are the edit target): only the KDT BORDER verts
        # are bound to the scenario sector BORDERS (nearest outer-ring edge) and track them; the KDT
        # INTERIOR verts then relax/optimize to the moved borders. encode_mesh writes the followed KDT.
        # DEFAULT OFF: moving KDT verts WITHOUT rebuilding the KD-tree partition makes the capture tree
        # inconsistent and CRASHES the game on a sector/depot query. Safe to enable only once the
        # KD-tree rebuilder lands. Until then, edit + save the SCENARIO only (KDT left untouched/valid).
        self._kdt_follow = tk.BooleanVar(value=False)
        self._kdt_border_bind = None                 # {kdt_border_vi: (render_zi, edge_k, t)} binding
        self._drag_kdt_vi = None                     # KDT border vert being dragged
        self._drag_kdt_orig = None                   # snapshot of _kdt_world at press (soft falloff base)
        self._drag_kdt_p0 = None                     # grabbed vert world pos at press
        # placements (depots / HQ spawns / labels / zones) parsed from the scenario NDF
        self._pndf = None           # parsed scenario NDF (NdfBinary) — edit source for placements
        self._places = []           # list of placement dicts (see parse_placements)
        self._place_sel = None      # index into self._places (selected placement)
        self._drag_place = None     # index of placement being dragged
        self._show_places = tk.BooleanVar(value=True)
        self._place_edit = tk.BooleanVar(value=False)
        # Marker size for the role icons ("off" falls back to the old cheap dots — see
        # _draw_placements; the value is one of _ICON_SIZES).
        self._icon_size = tk.StringVar(value="medium")
        # Corner marks on the placement icons: nationality (the unit's own, top-left; its
        # controlling camp's, bottom-left) and the camp number (bottom-right).
        self._show_nations = tk.BooleanVar(value=True)
        self._show_camps = tk.BooleanVar(value=True)
        self._camp_map = {}          # camp key -> camp info, from the paired .xyz (see camp_resolver)
        self._camp_map_key = None    # (map, scenario) the camp map was built for
        self._camp_map_busy = False
        self._camp_map_error = None  # why the controlling-nation marks are absent, if they are
        self._overlay_cache = {}     # (role, unit_nat, ctrl_nat, camp, size) -> PhotoImage
        self._nation_src = {}        # nation icon basename -> master RGBA
        self._camp_fonts = {}        # px -> ImageFont for the camp-number chip
        self._snap_roads = tk.BooleanVar(value=True)   # depots/HQ snap to the road-offset while dragging
        self._mode_vars = {m[0]: tk.BooleanVar(value=False) for m in _GAME_MODES}  # game-mode checkboxes
        # HQ alliance/priority/FFA-seat editing happens inline in the DETAILS panel via _det_entry
        # / _det_check (see _update_detail's hq branch). No persistent tk.Vars needed — the
        # widgets bind their own StringVar/BooleanVar per render and commit straight to the NDF.
        self._icon_cache = {}       # (filename, size) -> ImageTk.PhotoImage (placement icons)
        self._scn_size = 0          # loaded .scenario byte size (spawn count is file-size-bounded)
        # warmup campath ndfbin (the REAL start camera; PositionCamera is inert) — loaded per scenario
        self._campath = None
        self._campath_vpath = None
        self._campath_dirty = False
        # road network (from mapinfo.win buffer1) — exact points + edges, RE'd from the game's graph code
        self._roads = None          # {"nodes": [(x,y,deg)], "edges": [(i,j)]}
        # Memo for _nearest_road_point. Buildings face the nearest road, so EVERY depot/HQ marker
        # asked for that every frame — a full scan of the road graph each time. On Cotentin that was
        # 75% of a pan frame (~196 ms of ~260 ms). The answer depends only on the point and the road
        # graph, and neither changes while panning or zooming, so it is cached and dropped whenever
        # the graph is replaced.
        self._road_pt_memo = {}
        self._show_roads = tk.BooleanVar(value=True)
        # shaded relief from the terrain mesh (elevation the painted colour can't show)
        self._show_relief = tk.BooleanVar(value=True)
        # forest CONCEALMENT zones (mapinfo.win buffer4 SDB) — where the game grants forest cover
        self._forest = None         # {"cells": [(x0,y0,x1,y1)], "bbox": (...)}
        self._show_forest = tk.BooleanVar(value=True)
        # Editable SDB AI-terrain quadtree (paint/erase -> edit-in-place -> repack mapinfo.win), via the
        # unified codec ruse_mod_engine.sdb (issue #9), byte-identical safe. We edit buffer4 in
        # mapinfo.win — the game's runtime concealment/movement SDB (TSparseSpatialStateDatabaseStatic).
        # A runtime audit proved the game queries ONLY two layer bits:
        # 0x08 = forest / concealment (GetIsEnForet) and 0x04 = blocked / clear-path. (The terrain dat's
        # output.sdb is NEVER loaded by the game — a build artifact — so it isn't an edit target.)
        self._sdb = None            # {"parsed","grid","R","bboxX","bboxY","win","win_vpath","dirty"}
        self._conceal_edit = tk.BooleanVar(value=False)   # paint mode active
        self._conceal_erase = tk.BooleanVar(value=False)  # erase instead of paint
        self._conceal_brush = tk.IntVar(value=40000)      # brush radius in world units
        self._conceal_layer = tk.IntVar(value=0x08)       # which layer BIT to paint/erase
        self._sdb_layers = self._sdb_layers_list()        # the two runtime layers (label,bit,rgb)
        # per-layer visibility (forest on by default) + cached overlay cells per bit
        self._sdb_show = {bit: tk.BooleanVar(value=(bit == 0x08)) for (_, bit, _) in self._sdb_layers}
        self._sdb_cells = {}        # bit -> [(x0,y0,x1,y1)] overlay cells (lazy, invalidated on paint)
        self._sdb_rev = 0           # bumps on paint/load so the cached composite rebuilds
        self._sdb_lbl = None        # right-panel status label (built in _build_ui)
        self._painting = False
        self._drag_rot = None       # (unused: buildings auto-orient to road)
        self._drag_cam = None       # index of HQ whose camera marker is being dragged

        self._build_ui()
        if not _HAVE_PIL:
            ui_util.error(self, t("map.pillow_required"),
                                 t("map.map_editor_needs_pillow_pil"))
            return
        self._populate_maps()

    # ── layout ──────────────────────────────────────────────────────────────────
    def _build_ui(self):
        # ── TOP STRIP: map + scenario selection and project-wide actions. Lives ABOVE the tabs and is
        # unbound to them — loading a map/scenario is how you load everything, so it's always present.
        top = tk.Frame(self, background=_R_BG_PANEL)
        top.pack(side="top", fill="x")
        tk.Label(top, text=t("map.map"), background=_R_BG_PANEL, foreground=_R_GOLD,
                 font=_F_BOLD).pack(side="left", padx=(8, 4), pady=6)
        self._map_cb = ttk.Combobox(top, state="readonly", width=18, font=_F_MAIN)
        self._map_cb.pack(side="left", padx=4)
        self._map_cb.bind("<<ComboboxSelected>>", self._on_map_change)
        tk.Label(top, text=t("map.scenario"), background=_R_BG_PANEL, foreground=_R_GOLD,
                 font=_F_BOLD).pack(side="left", padx=(10, 4))
        self._scn_cb = ttk.Combobox(top, state="readonly", width=24, font=_F_MAIN)
        self._scn_cb.pack(side="left", padx=4)
        self._scn_cb.bind("<<ComboboxSelected>>", self._on_scn_change)
        # binding banner — what THIS scenario is (MP / Campaign / Operation / unbound).
        self._scn_kind_lbl = tk.Label(top, text="", background=_R_BG_PANEL, foreground=_R_GOLD_BRT,
                                      font=_F_BOLD)
        self._scn_kind_lbl.pack(side="left", padx=(8, 4))
        if features.CLONING_ENABLED:    # both copy a scenario; held back until proven in-game
            tk.Button(top, text=t("neww.title"), command=self._open_new_operation,
                      background="#122030", foreground=_R_GOLD_BRT, font=_F_BOLD,
                      relief="flat").pack(side="left", padx=4)
            tk.Button(top, text=t("map.new_scenario"), command=self._open_create_scenario_popup,
                      background="#122030", foreground=_R_GOLD_BRT, font=_F_BOLD,
                      relief="flat").pack(side="left", padx=4)
        tk.Button(top, text=t("map.save_mod"), command=self._save, background="#122030",
                  foreground=_R_GOLD_BRT, font=_F_BOLD, relief="flat").pack(side="right", padx=(4, 8))
        tk.Button(top, text=t("map.revert"), command=self._revert, background="#122030",
                  foreground=_R_TEXT, font=_F_BOLD, relief="flat").pack(side="right", padx=4)
        self._edit.set(False)   # sector boundary-drag stays inert (capture shape is .kdt-driven)

        # ── TABS: "Map Editor" (place things on the map) vs "Mission Logic" (how it plays). ──
        try:
            _style = ttk.Style(self)
            _style.configure("Map.TNotebook", background=_R_BG_PANEL, borderwidth=0)
            _style.configure("Map.TNotebook.Tab", background=_R_BG_PANEL, foreground=_R_TEXT,
                             padding=(16, 6), font=_F_BOLD)
            _style.map("Map.TNotebook.Tab", background=[("selected", _R_BG)],
                       foreground=[("selected", _R_GOLD_BRT)])
        except Exception:
            pass
        self._notebook = ttk.Notebook(self, style="Map.TNotebook")
        self._notebook.pack(side="top", fill="both", expand=True)
        # Tabs go in WALK ORDER (_WALK_STEPS), which is the game's own load order: a menu entry binds to a
        # map slot, the slot names the files, the files carry the placements, the script decides play, and
        # the text is what the player reads. Map Editor therefore sits third rather than first — each step
        # banner says "step N of 5", and numbered steps that ran left-to-right in a different order would
        # be telling the user two different things about where they are.
        self._menu_tab = tk.Frame(self._notebook, background=_R_BG)
        self._notebook.add(self._menu_tab, text=t("menu.step_title"))
        self._chain_tab = tk.Frame(self._notebook, background=_R_BG)
        self._notebook.add(self._chain_tab, text=t("chain.step_title"))
        map_tab = tk.Frame(self._notebook, background=_R_BG)
        self._notebook.add(map_tab, text=t("common.map_editor"))
        self._script_tab = tk.Frame(self._notebook, background=_R_BG)
        self._notebook.add(self._script_tab, text=t("script.step_title"))
        self._names_tab = tk.Frame(self._notebook, background=_R_BG)
        self._notebook.add(self._names_tab, text=t("text.step_title"))
        # Open ON the map, even though it is now the third step. Ordering the tabs is about making the
        # chain legible; which one you land on is about what you came for, and this window is opened from
        # a button called Map Editor. Without this it would open on Menu Entry purely because a notebook
        # selects whatever was added first.
        self._notebook.select(map_tab)

        # map-editor view toolbar (tab-specific view toggles)
        vbar = tk.Frame(map_tab, background=_R_BG_PANEL); vbar.pack(side="top", fill="x")
        tk.Checkbutton(vbar, text=t("map.flip_y"), variable=self._flip_y, command=self._invalidate_redraw,
                       background=_R_BG_PANEL, foreground=_R_TEXT, selectcolor=_R_BG_WIDGET,
                       font=_F_MAIN, activebackground=_R_BG_PANEL, activeforeground=_R_GOLD).pack(side="left", padx=10, pady=3)
        tk.Checkbutton(vbar, text=t("map.labels"), variable=self._lbl_var, command=self._redraw,
                       background=_R_BG_PANEL, foreground=_R_TEXT, selectcolor=_R_BG_WIDGET,
                       font=_F_MAIN, activebackground=_R_BG_PANEL, activeforeground=_R_GOLD).pack(side="left")
        tk.Checkbutton(vbar, text=t("map.sectors"), variable=self._show_sectors, command=self._invalidate_redraw,
                       background=_R_BG_PANEL, foreground=_R_TEXT, selectcolor=_R_BG_WIDGET,
                       font=_F_MAIN, activebackground=_R_BG_PANEL, activeforeground=_R_GOLD).pack(side="left", padx=(6, 0))
        # roads live in the VECTOR overlay, so the toggle has to mark that raster dirty — plain
        # _redraw rebuilt the composite from the CACHED overlay and the roads never changed at all
        tk.Checkbutton(vbar, text=t("map.roads"), variable=self._show_roads, command=self._invalidate_vec,
                       background=_R_BG_PANEL, foreground=_R_TEXT, selectcolor=_R_BG_WIDGET,
                       font=_F_MAIN, activebackground=_R_BG_PANEL, activeforeground=_R_GOLD).pack(side="left", padx=(10, 0))
        # relief is its own cached raster, so toggling it only needs a recompose (no re-rasterising)
        tk.Checkbutton(vbar, text=t("map.relief"), variable=self._show_relief,
                       command=self._on_relief_toggle,
                       background=_R_BG_PANEL, foreground=_R_TEXT, selectcolor=_R_BG_WIDGET,
                       font=_F_MAIN, activebackground=_R_BG_PANEL, activeforeground=_R_GOLD).pack(side="left", padx=(6, 0))
        tk.Button(vbar, text=t("map.reset_view"), command=self._fit_view, background="#122030",
                  foreground=_R_TEXT, font=_F_BOLD, relief="flat").pack(side="left", padx=8)

        # embed the mission-logic editor into its tab (kind-gated via set_binding on scenario change)

        # "Names & Descriptions" tab — author operation text via the operation_authoring engine
        # "Menu Entry" tab — whether the game lists this at all, where, and the metadata (P4).
        self._menu_frame = None
        try:
            import menu_entry_tab
            self._menu_frame = menu_entry_tab.MenuEntryFrame(self._menu_tab, self.project,
                                                             on_go=self._show_walk_step,
                                                             on_pick=self._select_by_info_idx)
            self._menu_frame.pack(fill="both", expand=True)
        except Exception:
            self._menu_frame = None

        # "Load & Files" tab — the menu-entry -> map-slot -> cluster -> files chain, walked and checked.
        # Every join here fails SILENTLY in game, so this is where you find out why (P3).
        self._chain_frame = None
        try:
            import load_files_tab
            self._chain_frame = load_files_tab.LoadFilesFrame(self._chain_tab, self.project,
                                                              on_go=self._show_walk_step)
            self._chain_frame.pack(fill="both", expand=True)
        except Exception:
            self._chain_frame = None

        # "Mission Rules & Script" tab — the outline, the guided pane and the source, over ONE decompiled
        # script. Folds in Objectives & Logic, Timeline, Node Graph and Author (P5).
        self._script_frame = None
        try:
            import mission_script_tab
            self._script_frame = mission_script_tab.MissionScriptFrame(
                self._script_tab, self.project, on_go=self._show_walk_step)
            self._script_frame.pack(fill="both", expand=True)
        except Exception:
            self._script_frame = None

        # "Text" tab — every LocHash this scenario owns, edited across all languages at once (P2).
        # Replaces the old Names & Descriptions tab, which edited ONE language per save and hid the
        # empty FoldedText slots (see our internal scenario notes).
        self._names_frame = None
        try:
            import text_tab
            self._names_frame = text_tab.TextFrame(self._names_tab, self.project,
                                                   on_go=self._show_walk_step)
            self._names_frame.pack(fill="both", expand=True)
        except Exception:
            self._names_frame = None

        # Objectives & Logic, Timeline, Node Graph and Author used to be four tabs here. They were four
        # views of ONE decompiled script (and Author was a WHEN/DO builder with 2 triggers and 4 actions
        # against a 1,133-class DSL), so they are now one step: mission_script_tab, above.

        body = tk.Frame(map_tab, background=_R_BG)
        body.pack(side="top", fill="both", expand=True)

        left = tk.Frame(body, background=_R_BG_PANEL, width=288)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        # ── KDT (capture zones) — HIDDEN for now ──────────────────────────────────
        # Editing the KDT crashes the game until the KD-tree rebuilder lands, so the whole panel is
        # shelved. The widgets are still BUILT (other code configures self._kdt_lbl etc.) but the
        # frame is intentionally NEVER packed, so nothing KDT-related is visible. The overlay also
        # defaults off (self._kdt_show=False). Re-pack kdtf to bring it back later.
        kdtf = tk.Frame(left, background=_R_BG_PANEL)   # intentionally NOT packed → hidden
        tk.Label(kdtf, text=t("map.kdt_capture_zones_edit_these"), background=_R_BG_PANEL,
                 foreground=_R_GOLD, font=_F_BOLD).pack(anchor="w", pady=(0, 2))
        self._kdt_lbl = tk.Label(kdtf, text=t("map.no_kdt"), background=_R_BG_PANEL,
                                 foreground=_R_TEXT_DIM, font=_F_MAIN, anchor="w", justify="left",
                                 wraplength=264)
        self._kdt_lbl.pack(anchor="w")
        tk.Checkbutton(kdtf, text=t("map.show_kdt_mesh"), variable=self._kdt_show,
                       command=self._redraw, background=_R_BG_PANEL, foreground=_R_TEXT,
                       selectcolor=_R_BG_WIDGET, font=_F_MAIN, activebackground=_R_BG_PANEL,
                       activeforeground=_R_GOLD).pack(anchor="w", pady=(2, 0))
        tk.Checkbutton(kdtf, text=t("map.colour_zone"), variable=self._kdt_color,
                       command=self._redraw, background=_R_BG_PANEL, foreground=_R_TEXT,
                       selectcolor=_R_BG_WIDGET, font=_F_MAIN, activebackground=_R_BG_PANEL,
                       activeforeground=_R_GOLD).pack(anchor="w", pady=(0, 2))
        tk.Checkbutton(kdtf, text=t("map.edit_kdt_nodes_drag"), variable=self._kdt_edit,
                       command=self._kdt_edit_toggle, background=_R_BG_PANEL, foreground=_R_GOLD_BRT,
                       selectcolor=_R_BG_WIDGET, font=_F_BOLD, activebackground=_R_BG_PANEL,
                       activeforeground=_R_GOLD).pack(anchor="w", pady=(2, 2))
        rr = tk.Frame(kdtf, background=_R_BG_PANEL); rr.pack(fill="x")
        tk.Checkbutton(rr, text=t("map.optimize_triangles_keep_geometry"), variable=self._kdt_relax,
                       background=_R_BG_PANEL, foreground=_R_TEXT, selectcolor=_R_BG_WIDGET,
                       font=_F_MAIN, activebackground=_R_BG_PANEL,
                       activeforeground=_R_GOLD).pack(side="left")
        tk.Button(rr, text=t("map.optimize_now"), command=self._kdt_relax_now, background="#122030",
                  foreground=_R_TEXT, font=_F_MAIN, relief="flat").pack(side="left", padx=6)
        tk.Label(kdtf, text=t("map.drag_node_interior_triangles_re"),
                 background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN,
                 anchor="w", justify="left").pack(anchor="w", pady=(2, 0))
        tk.Label(kdtf, text=t("map.rigid_transform_whole_mesh_proven"), background=_R_BG_PANEL,
                 foreground=_R_TEXT_DIM, font=_F_MAIN, anchor="w").pack(anchor="w", pady=(6, 0))
        gr = tk.Frame(kdtf, background=_R_BG_PANEL); gr.pack(fill="x")
        for i, (lab, var) in enumerate(((t("map.x_2"), self._kdt_dx), (t("map.y_2"), self._kdt_dy),
                                        (t("map.scale"), self._kdt_scale))):
            tk.Label(gr, text=lab, background=_R_BG_PANEL, foreground=_R_TEXT, font=_F_MAIN,
                     width=5, anchor="e").grid(row=i, column=0, sticky="e", pady=1)
            tk.Entry(gr, textvariable=var, font=_F_MAIN, width=12, background=_R_BG_WIDGET,
                     foreground=_R_TEXT, insertbackground=_R_TEXT, highlightthickness=0,
                     relief="flat").grid(row=i, column=1, sticky="w", padx=4, pady=1)
        br = tk.Frame(kdtf, background=_R_BG_PANEL); br.pack(fill="x", pady=(4, 0))
        tk.Button(br, text=t("common.preview"), command=self._kdt_preview, background="#122030",
                  foreground=_R_TEXT, font=_F_BOLD, relief="flat").pack(side="left")
        tk.Button(br, text=t("map.reset"), command=self._kdt_reset, background="#122030",
                  foreground=_R_TEXT, font=_F_BOLD, relief="flat").pack(side="left", padx=4)
        tk.Button(br, text=t("map.save_kdt"), command=self._save_kdt, background="#1a2c44",
                  foreground=_R_GOLD_BRT, font=_F_BOLD, relief="flat").pack(side="right")
        # (kdtf above is never packed — KDT UI is hidden)

        # PLACEMENTS + GAME MODES live in a block RESERVED at the bottom of the panel, so their
        # controls (incl. 'Recompute ticks' / 'Apply lobby modes') stay reachable no matter how long
        # the selection DETAILS get.  DETAILS takes the space above and SHRINKS (it's scrollable)
        # rather than pushing the bottom controls off-screen.
        bottom_block = tk.Frame(left, background=_R_BG_PANEL)
        bottom_block.pack(side="bottom", fill="x")

        # ── DETAILS panel ─────────────────────────────────────────────────────────
        # Sectors are visual-only (capture is driven by the KDT, not these polygons), so the
        # ex-"SECTORS" listbox was removed — that vertical real-estate now belongs to the
        # selection-detail panel below. The scrollbar lets very long PythonClassName strings or
        # multi-camera HQs spill past the visible height without truncation.
        tk.Label(left, text=t("map.details"), background=_R_BG_PANEL,
                 foreground=_R_GOLD, font=_F_BOLD).pack(anchor="w", padx=8, pady=(8, 2))
        df = tk.Frame(left, background=_R_BG_PANEL)
        df.pack(fill="both", expand=True, padx=8, pady=(2, 6))
        dsb = tk.Scrollbar(df, orient="vertical", background=_R_BG_WIDGET,
                           troughcolor=_R_BG_PANEL, activebackground=_R_SEL_BG,
                           highlightthickness=0, borderwidth=0)
        dsb.pack(side="right", fill="y")
        self._detail = tk.Text(df, background=_R_BG_WIDGET, foreground=_R_TEXT, font=_F_MAIN,
                               width=30, height=6, highlightthickness=0, borderwidth=0,
                               wrap="word", state="disabled",
                               yscrollcommand=dsb.set)
        self._detail.pack(side="left", fill="both", expand=True)
        dsb.config(command=self._detail.yview)
        # Section-header tag — bolded + gold, with a leading blank line for breathing room.
        self._detail.tag_configure("h", foreground=_R_GOLD, font=_F_BOLD, spacing1=4)
        self._detail.tag_configure("hint", foreground="#7a8aa0", font=_F_MAIN, spacing1=4)
        self._detail.tag_configure("path", foreground="#a0c0e0", font=_F_MAIN)

        # The old "EDIT HQ" frame that lived here (Alliance/Priority Spinboxes + FFA checkbox +
        # Apply button) was removed once Step 2 wired those same fields inline into the DETAILS
        # panel above — HQ editing now happens via the same _det_entry / _det_check widgets the
        # other kinds use. _commit_int_prop / _commit_ffa_toggle write directly to the NDF.

        # ── placements (depots / HQ spawns / labels) overlay + edit ──────────────
        plf = tk.Frame(bottom_block, background=_R_BG_PANEL)
        plf.pack(fill="x", padx=8, pady=(0, 4))
        tk.Label(plf, text=t("map.placements"), background=_R_BG_PANEL, foreground=_R_GOLD,
                 font=_F_BOLD).pack(anchor="w", pady=(2, 2))
        tk.Checkbutton(plf, text=t("map.show_placements"), variable=self._show_places,
                       command=self._redraw, background=_R_BG_PANEL, foreground=_R_TEXT,
                       selectcolor=_R_BG_WIDGET, font=_F_MAIN, activebackground=_R_BG_PANEL,
                       activeforeground=_R_GOLD).pack(anchor="w")
        tk.Checkbutton(plf, text=t("map.edit_placements_drag"), variable=self._place_edit,
                       command=self._on_place_edit_toggle, background=_R_BG_PANEL, foreground=_R_TEXT,
                       selectcolor=_R_BG_WIDGET, font=_F_MAIN, activebackground=_R_BG_PANEL,
                       activeforeground=_R_GOLD).pack(anchor="w")
        tk.Checkbutton(plf, text=t("map.auto_snap_roads"), variable=self._snap_roads,
                       background=_R_BG_PANEL, foreground=_R_TEXT,
                       selectcolor=_R_BG_WIDGET, font=_F_MAIN, activebackground=_R_BG_PANEL,
                       activeforeground=_R_GOLD).pack(anchor="w")
        # Icon size + legend. Every placement draws as a picture of what it is; this row is how you
        # trade that detail against clutter on a busy map, and how you learn what the pictures mean.
        icr = tk.Frame(plf, background=_R_BG_PANEL); icr.pack(fill="x", pady=(3, 0))
        tk.Label(icr, text=t("map.icon_size"), background=_R_BG_PANEL, foreground=_R_TEXT,
                 font=_F_MAIN).pack(side="left")
        self._icon_size_labels = {t(k): key for key, k in _ICON_SIZE_LABELS}
        cb = ttk.Combobox(icr, state="readonly", width=8,
                          values=[t(k) for _key, k in _ICON_SIZE_LABELS])
        cb.set(t(dict(_ICON_SIZE_LABELS)["medium"]))
        cb.bind("<<ComboboxSelected>>", self._on_icon_size)
        cb.pack(side="left", padx=4)
        self._icon_size_cb = cb
        tk.Button(icr, text=t("map.icon_legend"), command=self._open_icon_legend,
                  background="#122030", foreground=_R_TEXT, font=_F_MAIN,
                  relief="flat").pack(side="left")
        tk.Checkbutton(plf, text=t("map.show_nation_flags"), variable=self._show_nations,
                       command=self._on_overlay_toggle, background=_R_BG_PANEL, foreground=_R_TEXT,
                       selectcolor=_R_BG_WIDGET, font=_F_MAIN, activebackground=_R_BG_PANEL,
                       activeforeground=_R_GOLD).pack(anchor="w")
        tk.Checkbutton(plf, text=t("map.show_camp_numbers"), variable=self._show_camps,
                       command=self._on_overlay_toggle, background=_R_BG_PANEL, foreground=_R_TEXT,
                       selectcolor=_R_BG_WIDGET, font=_F_MAIN, activebackground=_R_BG_PANEL,
                       activeforeground=_R_GOLD).pack(anchor="w")
        self._place_lbl = tk.Label(plf, text="", background=_R_BG_PANEL, foreground=_R_TEXT_DIM,
                                   font=_F_MAIN, anchor="w", justify="left", wraplength=264)
        self._place_lbl.pack(anchor="w", pady=(2, 0))
        pbr = tk.Frame(plf, background=_R_BG_PANEL); pbr.pack(fill="x", pady=(3, 0))
        tk.Button(pbr, text=t("map.placement"), command=self._open_add_placement_popup,
                  background="#122030", foreground=_R_TEXT, font=_F_BOLD, relief="flat").pack(side="left")
        tk.Button(pbr, text=t("map.delete_sel"), command=self._delete_selected_place, background="#122030",
                  foreground=_R_TEXT, font=_F_BOLD, relief="flat").pack(side="left", padx=4)
        # (script editing moved to the Mission Logic tab — that's where mission behaviour lives)

        # ── GAME MODES: per-mode lobby ticks, read from the existing HQ layout ─────────────────
        # Step 3 reshape (2026-05-28): HQ creation is now ALWAYS user-driven via Add Placement.
        # The two buttons below split the old "Apply game modes (add HQs)" into the two real
        # operations: 'Recompute ticks' re-reads the HQ layout into the ticks; 'Apply lobby
        # modes' stages TMultiMapInfo so the lobby OFFERS exactly the ticked modes.
        gmf = tk.Frame(bottom_block, background=_R_BG_PANEL)
        gmf.pack(fill="x", padx=8, pady=(2, 4))
        self._gm_frame = gmf      # shown only for multiplayer/unbound scenarios (see _update_scenario_panel)
        tk.Label(gmf, text=t("map.game_modes"), background=_R_BG_PANEL, foreground=_R_GOLD,
                 font=_F_BOLD).pack(anchor="w", pady=(2, 0))
        tk.Label(gmf,
                 text=t("map.tick_modes_map_should_offer"),
                 background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN,
                 justify="left", wraplength=264).pack(anchor="w")
        grid = tk.Frame(gmf, background=_R_BG_PANEL); grid.pack(anchor="w", pady=(2, 2))
        for n, m in enumerate(_GAME_MODES):
            tk.Checkbutton(grid, text=t(m[1]), variable=self._mode_vars[m[0]],
                           background=_R_BG_PANEL, foreground=_R_TEXT, selectcolor=_R_BG_WIDGET,
                           font=_F_MAIN, activebackground=_R_BG_PANEL, activeforeground=_R_GOLD
                           ).grid(row=n // 2, column=n % 2, sticky="w", padx=(0, 8))
        gmbr = tk.Frame(gmf, background=_R_BG_PANEL); gmbr.pack(fill="x", pady=(2, 0))
        tk.Button(gmbr, text=t("map.recompute_ticks"), command=self._load_game_modes_state,
                  background="#122030", foreground=_R_TEXT, font=_F_BOLD,
                  relief="flat").pack(side="left")
        tk.Button(gmbr, text=t("map.apply_lobby_modes"), command=self._stage_lobby_modes,
                  background="#163048", foreground=_R_GOLD_BRT, font=_F_BOLD,
                  relief="flat").pack(side="left", padx=4)

        # ── per-kind metadata panel: shown INSTEAD of GAME MODES for campaign/operation scenarios,
        # whose lobby/objective/camp data is NOT lobby-mode driven. Mission logic (objectives, camps,
        # victory) lives in the paired .xyz script, edited on the Mission Rules & Script step — the
        # button below goes there. This panel is read-only registration metadata from the binding.
        self._scn_meta_frame = tk.Frame(bottom_block, background=_R_BG_PANEL)  # packed on demand
        self._scn_meta_lbl = tk.Label(self._scn_meta_frame, text="", background=_R_BG_PANEL,
                                      foreground=_R_TEXT, font=_F_MAIN, anchor="w", justify="left",
                                      wraplength=264)
        self._scn_meta_lbl.pack(anchor="w", fill="x")
        tk.Button(self._scn_meta_frame, text=t("map.edit_mission_logic"),
                  command=self._go_to_mission_script, background="#163048", foreground=_R_GOLD_BRT,
                  font=_F_BOLD, relief="flat").pack(anchor="w", pady=(4, 0))
        self._all_bindings = None     # {map_dir: [ScenarioBinding]} — built lazily, cached per session

        # ── right panel: AI-terrain SDB layers (toggle visibility + pick paint target) ───────────
        right = tk.Frame(body, background=_R_BG_PANEL, width=258)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        tk.Label(right, text=t("map.ai_terrain_layers_sdb"), background=_R_BG_PANEL, foreground=_R_GOLD,
                 font=_F_BOLD).pack(anchor="w", padx=8, pady=(8, 0))
        tk.Label(right, text=t("map.show_paint_target_mapinfo_win"),
                 background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN).pack(anchor="w", padx=8,
                                                                                   pady=(0, 4))
        for label, bit, rgb in self._sdb_layers:
            row = tk.Frame(right, background=_R_BG_PANEL); row.pack(fill="x", padx=8, pady=1)
            tk.Label(row, text="  ", background="#%02x%02x%02x" % rgb,
                     relief="solid", borderwidth=1).pack(side="left", padx=(0, 5))
            tk.Checkbutton(row, variable=self._sdb_show[bit], command=self._invalidate_redraw,
                           background=_R_BG_PANEL, selectcolor=_R_BG_WIDGET,
                           activebackground=_R_BG_PANEL).pack(side="left")
            tk.Radiobutton(row, text=t(label), value=bit, variable=self._conceal_layer,
                           command=self._on_paint_layer, background=_R_BG_PANEL, foreground=_R_TEXT,
                           selectcolor=_R_BG_WIDGET, font=_F_MAIN, activebackground=_R_BG_PANEL,
                           activeforeground=_R_GOLD).pack(side="left")
        tk.Frame(right, background=_R_BORDER, height=1).pack(fill="x", padx=8, pady=7)
        tk.Checkbutton(right, text=t("map.paint_mode_drag_map"), variable=self._conceal_edit,
                       command=self._invalidate_redraw, background=_R_BG_PANEL, foreground=_R_GOLD_BRT,
                       selectcolor=_R_BG_WIDGET, font=_F_BOLD, activebackground=_R_BG_PANEL,
                       activeforeground=_R_GOLD).pack(anchor="w", padx=8)
        tk.Checkbutton(right, text=t("map.erase_remove_from_layer"), variable=self._conceal_erase,
                       background=_R_BG_PANEL, foreground=_R_TEXT, selectcolor=_R_BG_WIDGET,
                       font=_F_MAIN, activebackground=_R_BG_PANEL,
                       activeforeground=_R_GOLD).pack(anchor="w", padx=8)
        brow = tk.Frame(right, background=_R_BG_PANEL); brow.pack(fill="x", padx=8, pady=(2, 0))
        tk.Label(brow, text=t("map.brush_radius"), background=_R_BG_PANEL, foreground=_R_TEXT,
                 font=_F_MAIN).pack(side="left")
        tk.Entry(brow, textvariable=self._conceal_brush, width=8, font=_F_MAIN,
                 background=_R_BG_WIDGET, foreground=_R_TEXT, insertbackground=_R_TEXT).pack(side="left", padx=4)
        tk.Label(right, text=t("map.painted_layers_are_written_top"),
                 background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN,
                 anchor="w", justify="left", wraplength=238).pack(anchor="w", padx=8, pady=(6, 0))
        self._sdb_lbl = tk.Label(right, text=t("map.no_sdb"), background=_R_BG_PANEL, foreground=_R_TEXT_DIM,
                                 font=_F_MAIN, anchor="w", justify="left", wraplength=238)
        self._sdb_lbl.pack(anchor="w", padx=8, pady=(4, 0))

        self._canvas = tk.Canvas(body, background=_R_BG_WIDGET, highlightthickness=0)
        self._canvas.pack(side="left", fill="both", expand=True)
        self._canvas.bind("<ButtonPress-1>", self._press_start)
        self._canvas.bind("<B1-Motion>", self._press_move)
        self._canvas.bind("<ButtonRelease-1>", self._press_release)
        self._canvas.bind("<MouseWheel>", self._wheel)
        self._canvas.bind("<Motion>", self._hover)
        self._canvas.bind("<Configure>", lambda e: self._redraw())

        self._status = tk.Label(self, text="", background=_R_BG_PANEL, foreground=_R_TEXT_DIM,
                                 font=_F_MAIN, anchor="w")
        self._status.pack(side="bottom", fill="x")

    # ── data load ─────────────────────────────────────────────────────────────
    # Walk step -> notebook tab. The scenario tabs are being rebuilt as a WALK along the chain that takes
    # a map from the main menu to a playable mission, so a tab can hand the user to the adjacent step (or
    # to whichever step owns something it references) without knowing the notebook's layout.
    # Steps not yet rebuilt map to their current tab; the mapping moves as each one lands.
    _WALK_STEPS = {
        1: "menu.step_title",         # menu entry (P4 - built)
        2: "chain.step_title",        # load & files (P3 - built)
        3: "common.map_editor",       # placements
        4: "script.step_title",       # mission rules & script (P5 - built)
        5: "text.step_title",         # text (P2 - built)
    }

    def _show_walk_step(self, index):
        """Switch to the tab that owns walk step `index`. Silently ignores an unknown step rather than
        raising out of a button callback."""
        want = self._WALK_STEPS.get(index)
        if not want or self._notebook is None:
            return
        label = t(want)
        for tab_id in self._notebook.tabs():
            if self._notebook.tab(tab_id, "text") == label:
                self._notebook.select(tab_id)
                return

    def _maps_src(self) -> str:
        """Path of the DataMap_Win.dat the editor READS from: the mod folder's own copy if this mod has
        already saved map edits, otherwise the clean backup (falling back to the live game file). Reading
        copies nothing into the mod folder — the project .dat is written only on "Save to mod"."""
        return str(self.project.read_source("maps"))

    def _populate_maps(self):
        src = self._maps_src()
        try:
            self._dm = edata.open_dat(src)
            maps = map_dirs(self._dm)
        except Exception as e:
            ui_util.error(self, t("common.load_failed"),
                                 t("map.could_not_open_map_dat", src=src, e=e))
            self._map_cb["values"] = []
            return
        self._ensure_bindings()      # classify first so the dropdown can show friendly localized names
        self._map_cb["values"] = self._build_map_display(maps)
        ui_util.fit_combobox(self._map_cb, minimum=18, maximum=46)
        if maps:
            # default to blitz (supercrossroads4) — the user's in-game test map
            self._set_map("supercrossroads4" if "supercrossroads4" in maps else maps[0])
            self._on_map_change(force=True)
        else:
            ui_util.warning(self, t("map.no_maps"),
                                   t("map.dat_has_no_datasmap_map"))

    def _load_minimap(self, map_dir):
        """Minimap terrain.png for a map, sourced like every other dat: the mod project's Maps/PC copy
        if present, else the clean backup, else the game (all via project.read_source).  Returns a PIL
        image or None (no Pillow, no matching terrain dat, or it isn't available)."""
        if not _HAVE_PIL:
            return None
        want = _norm(map_dir)
        key = None
        for k in self.project.terrain_dat_keys():            # "terrain/DataMap<Name>_v09.dat"
            stem = k.split("/", 1)[1]
            if stem.lower().startswith("datamap") and stem.lower().endswith("_v09.dat"):
                if _norm(stem[len("DataMap"):-len("_v09.dat")]) == want:
                    key = k
                    break
        if key is None:
            return None
        try:
            src = self.project.read_source(key)
            return load_minimap(str(src)) if os.path.isfile(str(src)) else None
        except Exception:
            return None

    def _terrain_dat_src(self, map_dir):
        """Resolve the on-disk terrain dat (mod/backup/game) for `map_dir`, or None."""
        want = _norm(map_dir)
        for k in self.project.terrain_dat_keys():            # "terrain/DataMap<Name>_v09.dat"
            stem = k.split("/", 1)[1]
            if stem.lower().startswith("datamap") and stem.lower().endswith("_v09.dat") \
                    and _norm(stem[len("DataMap"):-len("_v09.dat")]) == want:
                try:
                    src = self.project.read_source(k)
                    return str(src) if os.path.isfile(str(src)) else None
                except Exception:
                    return None
        return None

    def _terrain_cache_path(self, map_dir):
        """Where this map's decoded terrain chunks are cached — a DIRECTORY of lossless PNG tiles now,
        one per chunk, not a single baked image (issue #19).  Kept WITH the mod project, so it persists
        with it (the project folder is the home for everything created for the mod — like notes.json
        and the edited dats).  Falls back to the system temp dir only if that isn't writable."""
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", map_dir)
        try:
            base = os.path.join(str(self.project.folder), "cache", "terrain")
            os.makedirs(base, exist_ok=True)
        except Exception:
            base = os.path.join(tempfile.gettempdir(), "ruse_mm_terrain_cache")
            try:
                os.makedirs(base, exist_ok=True)
            except Exception:
                return None
        return os.path.join(base, safe)

    def _load_terrain(self, map_dir):
        """The map's display surface, REPLACING the baked minimap with the decoded high-def terrain
        (issue #8, #19).  Returns something to show NOW — the baked minimap — and opens the map's dat
        on a background thread; a ``TerrainTiles`` swaps in when ready and then sharpens in place as
        chunks decode.  Falls back to the minimap if Pillow/numpy/the codec aren't available."""
        self._terrain_token += 1
        self._terrain_status = ""        # reset the decode suffix; the loader below re-sets it
        self._close_terrain_tiles()
        if not _HAVE_PIL:
            return None
        if self._show_relief.get():      # independent of the tile source; lands when it lands
            self._start_relief(map_dir)
        else:
            self._relief_token += 1      # drop any relief still building for the previous map
            self._ov_relief = None
        if _terrain_tiles is not None:
            src = self._terrain_dat_src(map_dir)
            if src:
                token = self._terrain_token
                self._terrain_status = t("map.decoding_high_def_terrain")
                self._refresh_status()
                # read the dropdown HERE: Tk widgets may only be touched from the main thread, and a
                # worker doing it raises "main thread is not in main loop"
                self._terrain_result = None
                self._tiles_dirty = False
                threading.Thread(target=self._open_terrain_bg,
                                 args=(src, self._terrain_cache_path(map_dir), token),
                                 daemon=True).start()
                self.after(150, lambda: self._poll_terrain(token))   # scheduled ON the main thread
        return self._load_minimap(map_dir)

    def _on_relief_toggle(self):
        """Relief has its own cached raster, so showing/hiding it is just a recompose. If the raster
        isn't built yet (first time on this map), kick the build off now."""
        self._ov_crop_cache.clear()
        if self._show_relief.get() and self._ov_relief is None:
            self._start_relief(self._sel_map())
        self._comp_key = None
        self._redraw()

    def _relief_cache_file(self, map_dir):
        base = self._terrain_cache_path(map_dir)
        if not base:
            return None
        try:
            os.makedirs(base, exist_ok=True)
        except Exception:
            return None
        return os.path.join(base, "relief_%s_e%.1f_s%.2f.png"
                            % (_terrain_relief.LONG_SIDE, _terrain_relief.DEFAULT_EXAGGERATION,
                               _terrain_relief.DEFAULT_STRENGTH))

    # ── controlling camp / nationality (from the paired mission script) ──────────
    def _start_camp_map_load(self, map_dir, scn):
        """Resolve who CONTROLS each placement, off the UI thread.

        A placement stores only a `Camp` integer; the camp's nationality lives in the paired .xyz,
        and reading it means decompiling that script (0.03-1.7 s measured across the shipped
        corpus) — far too slow to do while the user is switching scenarios. So the map draws
        immediately without the controlling-nation marks and they appear a moment later.

        Multiplayer scenarios have no script at all: their camps are lobby slots that carry
        allegiance and no nationality, so there is nothing to resolve and nothing is shown."""
        self._camp_map = {}
        self._camp_map_key = (map_dir, scn)
        self._overlay_cache.clear()
        for pl in self._places or []:
            pl["ctrl_nation"] = None
        if not (map_dir and scn and self.project is not None):
            return
        self._camp_map_busy = True
        self._camp_map_result = None
        self._camp_map_error = None
        threading.Thread(target=self._load_camp_map_bg, args=((map_dir, scn),),
                         daemon=True).start()
        self.after(150, lambda: self._poll_camp_map((map_dir, scn)))

    def _poll_camp_map(self, key):
        """Main thread: pick up the resolved camp map and repaint with the nation marks."""
        if key != self._camp_map_key:
            return                                   # user moved to another scenario
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        got = getattr(self, "_camp_map_result", None)
        if got is not None:
            self._camp_map_result = None
            self._apply_camp_map(got, key)
            return
        if self._camp_map_busy:
            self.after(150, lambda: self._poll_camp_map(key))

    def _load_camp_map_bg(self, key):
        """Worker: read + decompile the paired script and build the camp map. Never touches Tk.

        Any failure is REPORTED, not swallowed. An earlier version logged it at debug and drew no
        marks, which is how a packaging bug (uncompyle6's runtime-built scanner module missing from
        the exe — see build.py's collect_all list) reached a release looking like "the feature just
        doesn't work" instead of naming its own cause."""
        log = logging.getLogger("map_editor")
        camp_map, err = {}, None
        try:
            map_dir, scn = key
            raw = None
            try:
                raw = self.project.get_raw("scripts", self._script_path_for_scn(map_dir, scn))
            except Exception as e:
                log.debug("camp map: no script entry for %s/%s (%r)", map_dir, scn, e)
                raw = None
            if raw:
                marshal_bytes, _h, _s = _xyz_unpack(raw)
                camp_map = cresolve.parse_camp_map(_xyz_decompile_to_source(marshal_bytes))
                log.debug("camp map for %s/%s: %d camps", map_dir, scn, len(camp_map))
                if not camp_map:
                    err = t("map.camp_map_no_camps")
        except ImportError as e:
            # The decompiler stack isn't importable — the signature of a packaging problem.
            err = t("map.camp_map_decompiler_missing", err=str(e))
            log.warning("camp map for %s: decompiler unavailable: %r", key, e)
        except Exception as e:
            err = t("map.camp_map_failed", err=str(e))
            log.warning("camp map for %s failed: %r", key, e)
        self._camp_map_error = err
        self._camp_map_result = camp_map
        self._camp_map_busy = False

    def _apply_camp_map(self, camp_map, key):
        """Main thread: stamp each placement with its controlling nation and repaint."""
        if key != self._camp_map_key:
            return
        self._camp_map = camp_map or {}
        for pl in self._places or []:
            if pl["kind"] in ("depot", "unit", "building", "spawn"):
                pl["ctrl_nation"] = cresolve.camp_nation_icon(self._camp_map,
                                                              pl["extra"].get("camp"))
            elif pl["kind"] == "hq":
                # HQs key off AllianceNum; only unanimous alliances yield a nation (see resolver).
                pl["ctrl_nation"] = cresolve.alliance_nation_icon(self._camp_map,
                                                                  pl["extra"].get("alliance"))
        self._overlay_cache.clear()
        self._update_place_info()
        if self._camp_map:
            self._redraw()

    def _camp_map_status(self):
        """One line for the Placements panel saying where the controlling-nation marks stand —
        resolved, still loading, not applicable (multiplayer), or broken and why."""
        if self._camp_map_busy:
            return t("map.camp_map_loading")
        err = getattr(self, "_camp_map_error", None)
        if err:
            return err
        if self._camp_map:
            return t("map.camp_map_ok", n=len(self._camp_map))
        return t("map.camp_map_none")

    def _camp_info_for(self, pl):
        """The controlling camp's info dict for a placement, or None."""
        if pl["kind"] not in ("depot", "unit", "building", "spawn"):
            return None
        camp = pl["extra"].get("camp")
        if camp == cresolve.NEUTRAL_CAMP:
            return None
        return self._camp_map.get(0 if camp is None else camp)

    def _start_relief(self, map_dir):
        """Build this map's relief overlay on a worker (decode + rasterise is ~1-3 s), or load the
        cached PNG if we already made it. Never blocks the UI: the map draws without it and it
        appears when ready."""
        self._relief_token += 1
        self._ov_relief = None
        if _terrain_relief is None or not _HAVE_PIL or not map_dir:
            return
        src = self._terrain_dat_src(map_dir)
        if not src:
            return
        token = self._relief_token
        self._relief_busy = True
        self._relief_result = None
        threading.Thread(target=self._build_relief_bg,
                         args=(src, self._relief_cache_file(map_dir), token),
                         daemon=True).start()
        # Collect the result by POLLING from the main thread rather than having the worker call
        # after() itself. Tk's after() has to createcommand(), which needs the interpreter's main
        # loop, so calling it off-thread can raise "main thread is not in main loop" and the result
        # is lost — silently, since the worker can only swallow it. Scheduling the poll here (on the
        # main thread) keeps every Tk touch on the main thread.
        self.after(200, lambda: self._poll_relief(token))

    def _poll_relief(self, token):
        """Main thread: pick up a finished relief raster, or keep waiting for it."""
        if token != self._relief_token:
            return                                  # superseded by a newer map/build
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        got = self._relief_result
        if got is not None:
            img, got_token = got
            self._relief_result = None
            self._apply_relief(img, got_token)
            return
        if self._relief_busy:
            self.after(200, lambda: self._poll_relief(token))

    def _build_relief_bg(self, src, cache_file, token):
        """Worker: decode the terrain mesh and rasterise the relief. Must not touch Tk."""
        log = logging.getLogger("map_editor")
        log.debug("relief: worker start token=%s src=%s cache=%s", token, src, cache_file)
        try:
            img = None
            if cache_file and os.path.isfile(cache_file):
                try:
                    img = Image.open(cache_file)
                    img.load()
                    img = img.convert("RGBA")
                except Exception:
                    img = None                      # corrupt/partial cache — just rebuild it
            if img is None:
                dat = edata.open_dat(src)
                blob = None
                for vp in dat.list():
                    low = vp.lower()
                    if low.endswith("highdef.tms"):
                        blob = dat.get(vp)
                        break
                    if low.endswith("lowdef.tms") and blob is None:
                        blob = dat.get(vp)          # fallback: coarser mesh, same geometry
                if blob is None:
                    # Say so. A silent return here is as opaque as a swallowed exception: the
                    # relief would just never appear, with nothing to explain why.
                    logging.getLogger("map_editor").warning(
                        "terrain relief: no highdef/lowdef .tms in %s", src)
                    self._relief_busy = False
                    return
                if token != self._relief_token:     # map changed while the dat was read
                    self._relief_busy = False
                    return
                img = _terrain_relief.build(blob)
                if cache_file:
                    try:
                        img.save(cache_file)
                    except Exception:
                        pass                        # cache is an optimisation, not a requirement
        except Exception:
            # Same rule as the terrain loader: never swallow this. A worker failing silently would
            # leave the relief permanently absent with nothing to explain why.
            logging.getLogger("map_editor").exception("terrain relief build failed for %s", src)
            self._relief_busy = False
            return
        log.debug("relief: worker done token=%s size=%s", token, getattr(img, "size", None))
        # Publish the result BEFORE clearing the busy flag. The poll stops when it sees "not busy
        # and nothing there", so clearing first would let a poll landing in between conclude the
        # build had produced nothing and give up.
        self._relief_result = (img, token)
        self._relief_busy = False

    def _apply_relief(self, img, token):
        if token != self._relief_token:
            logging.getLogger("map_editor").debug(
                "relief: dropping stale result token=%s current=%s", token, self._relief_token)
            return
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        self._ov_relief = img
        self._ov_crop_cache.clear()
        self._comp_key = None
        self._redraw()

    def _close_terrain_tiles(self):
        """Shut down the previous map's tile source so its worker/pool doesn't outlive the view."""
        old = getattr(self, "_pil", None)
        if hasattr(old, "close"):
            try:
                old.close()
            except Exception:
                pass

    def destroy(self):
        """Tk teardown — make sure the terrain worker/pool goes with the window."""
        self._terrain_token += 1                 # invalidate any in-flight load
        self._relief_token += 1                  # ...and any in-flight relief build
        self._close_terrain_tiles()
        return super().destroy()

    def _open_terrain_bg(self, src, cache_dir, token):
        """Background: pull the tmst pair + baked minimap out of the dat and build the tile source.

        Only the container is read here — no chunk is decoded until a view asks for one, so this
        finishes in about the time it takes to read the dat, whatever the map's size.

        Runs OFF the main thread, so it must not touch Tk — everything it needs is passed in."""
        try:
            dat = edata.open_dat(src)
            tmst = chunk = png = None
            for vp in dat.list():
                low = vp.lower()
                if low.endswith("highdef.tmst_pc"):
                    tmst = dat.get(vp)
                elif low.endswith("highdef.tmst_chunk_pc"):
                    chunk = dat.get(vp)
                elif low.endswith("terrain.png"):
                    png = dat.get(vp)
            if not (tmst and chunk):
                return
            if token != self._terrain_token:            # user switched maps while the dat was read
                return
            base = Image.open(io.BytesIO(png)).convert("RGB") if png else None
            tiles = _terrain_tiles.TerrainTiles(
                tmst, chunk, base_img=base, cache_dir=cache_dir,
                finest_lod=0,          # the tier follows the zoom; nothing caps it
                on_ready=lambda: self._on_tiles_ready(token))
        except Exception:
            # Do NOT fail silently: this ran on a worker, so an exception here leaves the editor
            # showing the baked minimap forever with nothing to explain why. A swallowed
            # "main thread is not in main loop" from reading a Tk widget off-thread did exactly that.
            logging.getLogger("map_editor").exception(
                "terrain tile source failed to open for %s", src)
            return
        # Hand off through a slot the MAIN thread polls, not after(). after() has to createcommand(),
        # which needs the interpreter's main loop, so off-thread it can raise "main thread is not in
        # main loop" — and a worker can only swallow that, leaving the baked minimap up forever with
        # nothing to explain why. Exactly the failure the comment above warns about, one line down.
        self._terrain_result = (tiles, token)

    def _poll_terrain(self, token):
        """Main thread: pick up the finished tile source, and keep repainting while chunks land."""
        if token != self._terrain_token:
            return
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        got = self._terrain_result
        if got is not None:
            tiles, got_token = got
            self._terrain_result = None
            self._apply_terrain(tiles, got_token)
        if self._tiles_dirty:                           # chunks landed since the last poll
            self._tiles_dirty = False
            self._tiles_repaint(token)
        if token == self._terrain_token:
            self.after(200, lambda: self._poll_terrain(token))

    def _on_tiles_ready(self, token):
        """A worker finished some chunks — flag it; _poll_terrain repaints on the main thread.

        Called FROM a decode worker, so it must not touch Tk at all (see _poll_terrain)."""
        if token != self._terrain_token:
            return
        self._tiles_dirty = True

    def _tiles_repaint(self, token):
        if token != self._terrain_token:
            return
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        tiles = self._pil
        st = tiles.stats() if hasattr(tiles, "stats") else {}
        pending = st.get("queued", 0) + st.get("inflight", 0)
        self._terrain_status = t("map.decoding_high_def_terrain") if pending else ""
        # only the base image changed — _recompose re-crops without re-rasterising the (heavy) SDB
        # and vector overlays, which matters because chunks land continuously while panning
        self._recompose()

    def _apply_terrain(self, tiles, token):
        if token != self._terrain_token:      # user switched maps meanwhile — discard
            if hasattr(tiles, "close"):
                tiles.close()
            return
        try:
            if not self.winfo_exists():
                if hasattr(tiles, "close"):
                    tiles.close()
                return
        except Exception:
            return
        self._pil = tiles
        self._terrain_status = ""          # container is up; chunks fill in behind the redraw
        self._invalidate_redraw()
        self._fit_view()

    def _on_map_change(self, _=None, force=False):
        if not force and not self._confirm_discard():
            # Keep-editing: undo the combobox's own move (see _on_scn_change for why).
            self._restore_selection()
            return
        map_dir = self._sel_map()
        scns = list_scenarios(self._dm, map_dir)
        self._scn_cb["values"] = scns
        ui_util.fit_combobox(self._scn_cb, minimum=24, maximum=44)
        self._pil = self._load_terrain(map_dir)
        win = self._dm.get(f"datasmap\\{map_dir}\\mapinfo.win")
        self._bbox = read_bbox(win) if win else None
        self._roads = read_road_graph(win) if win else None
        self._road_pt_memo = {}                 # different map -> different graph; drop the memo
        self._forest = read_forest_zones(win) if win else None
        self._sdb_map_dir = map_dir
        self._sdb_win = win
        self._sdb_win_vpath = f"datasmap\\{map_dir}\\mapinfo.win"
        self._load_sdb()
        if scns:
            # prefer the standard/MP scenario (challenge/testia variants have different placements)
            self._scn_cb.set(next((s for s in ("leveldesign_normal", "leveldesign") if s in scns),
                                  scns[0]))
        self._ensure_bindings()               # classify this terrain's scenarios for the binding view
        self._on_scn_change(force=True)        # drives the per-kind panel + banner
        self._fit_view()

    # ── AI-terrain SDB painting (unified codec; mapinfo buffer4 = the game's runtime SDB) ─────
    _SDB_LAYER_RGB = {0x08: (60, 220, 90), 0x04: (80, 160, 255)}

    def _sdb_layers_list(self):
        """The (label, bit, rgb) rows for the two runtime-meaningful SDB layers (confirmed:
        only 0x08 forest/conceal and 0x04 blocked/clear-path are ever queried by the game)."""
        return [(lbl, bit, self._SDB_LAYER_RGB.get(bit, (200, 200, 200)))
                for (lbl, bit) in sdb_mod.LAYERS_BUFFER4]

    def _load_sdb(self):
        """Decode the editable buffer4 SDB grid (mapinfo.win) with the unified codec. Stores the parsed
        tree for edit-in-place writeback."""
        self._sdb = None
        self._sdb_cells = {}; self._sdb_rev += 1; self._ov_sdb_dirty = True
        try:
            win = getattr(self, "_sdb_win", None)
            parts = sdb_mod.split_mapinfo(win) if win else None
            if not parts or len(parts[1]) < 4 or not sdb_mod.is_sdb(parts[1][3]):
                self._sdb_status(t("map.no_sdb")); return
            buf = parts[1][3]
            parsed = sdb_mod.parse(buf)
            grid, R = sdb_mod.to_grid(parsed)
            bboxX = struct.unpack_from("<f", buf, 37)[0]
            bboxY = struct.unpack_from("<f", buf, 41)[0]
            self._sdb = {"parsed": parsed, "grid": bytearray(grid), "R": R, "bboxX": bboxX,
                         "bboxY": bboxY, "win": win,
                         "win_vpath": getattr(self, "_sdb_win_vpath", None), "dirty": False}
        except Exception as e:
            self._sdb_status(t("map.sdb_load_failed_e", e=e)); return
        self._sdb_status(t("map.sdb_r_x_r_mapinfo", r=self._sdb["R"]))

    def _sdb_status(self, msg):
        if self._sdb_lbl is not None:
            self._sdb_lbl.config(text=msg)

    def _sdb_world_bounds(self):
        """The SDB grid's world bounds (minX,minY,maxX,maxY) — the EXACT mapping the engine uses, confirmed
        at runtime (m05_hollande -> [-655360,0,1966080,2621440]). The region is
        forced SQUARE: side=max(bboxX,bboxY), maxX=bboxX, maxY=bboxY, and the SHORTER axis extends NEGATIVE
        (minX=bboxX-side, minY=bboxY-side) so the terrain ends up centred. Square maps -> minX=minY=0 (identity)."""
        bx, by = self._sdb["bboxX"], self._sdb["bboxY"]
        side = max(bx, by)
        return (bx - side, by - side, bx, by)

    def _sdb_cells_for(self, bit):
        """Overlay cells for a layer bit (lazy; invalidated on paint of that bit)."""
        if not self._sdb:
            return []
        if bit not in self._sdb_cells:
            # SQUARE region, terrain centred (see _sdb_world_bounds) — the exact engine mapping.
            minX, minY, maxX, maxY = self._sdb_world_bounds()
            self._sdb_cells[bit] = [(minX + c[0], minY + c[1], minX + c[2], minY + c[3])
                                    for c in sdb_mod.grid_to_cells(self._sdb["grid"], self._sdb["R"],
                                                                   maxX - minX, maxY - minY, bit)]
        return self._sdb_cells[bit]

    def _on_paint_layer(self):
        """Selecting a paint target auto-shows that layer so you see what you edit."""
        self._sdb_show.setdefault(self._conceal_layer.get(), tk.BooleanVar(value=True)).set(True)
        self._invalidate_redraw()

    def _conceal_refresh_overlay(self):
        self._invalidate_sdb()      # only the SDB raster changed — don't re-rasterise the vectors

    def _conceal_paint_at(self, sx, sy):
        if not (self._sdb and self._bbox):
            return
        try:
            import numpy as np
        except Exception:
            return
        bit = self._conceal_layer.get()
        wx, wy = self._screen_to_world(sx, sy)
        R = self._sdb["R"]
        radius = float(self._conceal_brush.get())
        # world->cell on the SQUARE, terrain-centred bounds (see _sdb_world_bounds) — the exact engine
        # mapping, so the brush hits the same cell the game reads at this world point.
        minX, minY, maxX, maxY = self._sdb_world_bounds()
        cw = (maxX - minX) / R; ch = (maxY - minY) / R     # square cells = side/R
        ys, xs = np.ogrid[0:R, 0:R]
        mask = ((minX + (xs + 0.5) * cw - wx) ** 2 + (minY + (ys + 0.5) * ch - wy) ** 2) <= radius * radius
        if not mask.any():
            return
        erase = self._conceal_erase.get()
        sdb_mod.paint_tree(self._sdb["parsed"], mask, R, bit, not erase)
        # keep the display grid in sync with the edited tree (same mask)
        g = np.frombuffer(self._sdb["grid"], np.uint8).reshape(R, R).copy()
        if erase:
            g[mask] = (g[mask] & np.uint8(~bit & 0xFF)) | 1
        else:
            g[mask] = (g[mask] | np.uint8(bit)) | 1
        self._sdb["grid"] = bytearray(g.tobytes())
        self._sdb["dirty"] = True
        self._sdb_cells.pop(bit, None)      # invalidate this layer's overlay
        self._sdb_rev += 1

    def _on_scn_change(self, _=None, force=False):
        if not force and not self._confirm_discard():
            # The user chose to KEEP editing. The combobox has ALREADY moved itself to the new
            # value by the time this handler runs, so put it back on what is actually loaded --
            # otherwise the box points at one scenario while memory holds another, and the next
            # save writes the loaded scenario's bytes over whatever the box points at.
            self._restore_selection()
            return
        map_dir = self._sel_map()
        scn = self._scn_cb.get()
        vpath = f"test\\map\\{map_dir}\\{scn}.scenario" if scn else None
        raw = self._dm.get(vpath) if vpath else None
        # Remember what we actually LOADED. _save writes back to this path, never to the live
        # comboboxes -- see the _scn_vpath comment in __init__.
        self._scn_vpath = vpath if raw else None
        self._scn_src = (map_dir, scn) if raw else None
        self._scn_size = len(raw) if raw else 0
        self._scn = scenario_mod.read(raw) if raw else None
        self._zones = render_zones_from_scn(self._scn) if self._scn else []
        # parse placements (depots/HQ/labels/zones) from the embedded scenario NDF
        self._pndf = self._places = None
        self._place_sel = self._drag_place = None
        if self._scn:
            try:
                self._pndf = ndfbin.read(self._scn["ndf_data"])
                self._places = parse_placements(self._pndf)
            except Exception:
                self._pndf, self._places = None, []
        self._places = self._places or []
        # load the warmup campath (the actual start camera) + link each HQ to its keyframes
        self._campath = None; self._campath_vpath = None; self._campath_dirty = False
        if scn:
            cam_vp = f"test\\map\\{map_dir}\\campath\\campaths_{scn}.ndfbin"
            cb = self._dm.get(cam_vp)
            if cb:
                try:
                    self._campath = ndfbin.read(cb); self._campath_vpath = cam_vp
                except Exception:
                    self._campath = None
        self._link_campaths()
        self._start_camp_map_load(map_dir, scn)
        self._update_place_info()
        self._sel = None
        self._dirty = False
        self._build_sector_rings()
        self._build_clusters()
        # default soft-falloff radius from the per-zone border-width param (vertex float[3])
        self._border_w = next((z["vertices"][0][3] for z in (self._scn["zones"] if self._scn else [])
                               if z.get("vertices")), 0.0)
        self._soft_radius.set(str(int(self._border_w * 2.5)) if self._border_w else "40000")
        self._load_kdt(map_dir, scn)
        self._update_scenario_panel()      # banner + per-kind panel (GAME MODES only for mp/unbound)
        self._update_detail()
        self._invalidate_redraw()

    def _load_kdt(self, map_dir, scn):
        """Load the KDT that drives this scenario's capture mechanics (best-effort)."""
        self._kf = self._kdt_bytes = self._kdt_vpath = None
        self._kdt_world, self._kdt_tris = [], []
        self._kdt_bverts, self._kdt_borders, self._kdt_dirty = set(), {}, False
        self._kdt_adj = {}
        self._kdt_border_bind = None
        self._kdt_reset(redraw=False)
        if not scn:
            self._kdt_lbl.config(text=t("map.no_kdt")); return
        vpath = f"test\\map\\{map_dir}\\zonebluff\\{scn}.kdt"
        data = self._dm.get(vpath)
        if not data:
            self._kdt_lbl.config(text=t("map.no_kdt_scenario")); return
        try:
            kf = kdt_mod.read_full(data)
            self._kf, self._kdt_bytes, self._kdt_vpath = kf, data, vpath
            # Decode the REAL triangle mesh OFFLINE via the cracked VtxBufIdx predictor (kdt.decode_mesh).
            # Works for all maps, no runtime capture needed — d1 verts are residuals + ref-instructions, not absolute.
            mesh = kdt_mod.decode_mesh(data)
            if mesh and mesh["tris"]:
                self._kdt_world = mesh["verts"]
                self._kdt_tris = mesh["tris"]
                src = "real"
            else:
                self._kdt_world = [kf.world_xy(i) for i in range(len(kf.verts))]
                ib = kf.indexbuf or []
                self._kdt_tris = [(ib[i], ib[i + 1], ib[i + 2]) for i in range(0, len(ib) - 2, 3)]
                src = "approx"          # decode failed (empty/area-KDT) — fallback unreliable
            self._kdt_tri_sector = self._compute_kdt_sectors(map_dir, scn)
            self._kdt_bverts, self._kdt_borders = kdt_sector_borders(self._kdt_tris, self._kdt_tri_sector)
            self._kdt_adj = kdt_vertex_adjacency(self._kdt_tris)
            self._kdt_orig = list(self._kdt_world)        # baseline for the geometry-saving interior fit
            known = (map_dir, scn) in KDT_SECTOR_RANGES
            self._kdt_lbl.config(text=t("map.verts_nv_tris_nt_mesh", nv=len(self._kdt_world), nt=len(self._kdt_tris),
                                        src=src, sec=('exact' if known else 'geom'),
                                        nb=len(self._kdt_bverts)))
        except Exception as e:
            self._kdt_lbl.config(text=t("map.kdt_load_failed_e", e=e))

    def _compute_kdt_sectors(self, map_dir, scn):
        """Per-KDT-triangle a0_zone_idx. Exact from the cracked ranges if known for this map;
        else geometric (which scenario zone contains the triangle centroid)."""
        n = len(self._kdt_tris)
        rng = KDT_SECTOR_RANGES.get((map_dir, scn))
        if rng:
            out = [None] * n
            for a, b, sec in rng:
                for t in range(a, min(b, n - 1) + 1):
                    out[t] = sec
            return out
        # geometric fallback: triangle centroid -> scenario zone polygon
        out = []
        W = self._kdt_world
        for (ia, ib_, ic) in self._kdt_tris:
            if max(ia, ib_, ic) >= len(W):
                out.append(None); continue
            cx = (W[ia][0] + W[ib_][0] + W[ic][0]) / 3.0
            cy = (W[ia][1] + W[ib_][1] + W[ic][1]) / 3.0
            sec = None
            for z in self._zones:
                if self._pt_in_poly(cx, cy, z["verts"]):
                    sec = z["idx"]; break
            out.append(sec)
        return out

    @staticmethod
    def _pt_in_poly(x, y, poly):
        inside = False; j = len(poly) - 1
        for i in range(len(poly)):
            xi, yi = poly[i]; xj, yj = poly[j]
            if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
                inside = not inside
            j = i
        return inside

    def _kdt_xform(self):
        """Current preview transform params (dx, dy, scale) parsed from entries."""
        try:
            return (float(self._kdt_dx.get() or 0), float(self._kdt_dy.get() or 0),
                    float(self._kdt_scale.get() or 1))
        except ValueError:
            return 0.0, 0.0, 1.0

    def _kdt_xform_world(self, x, y):
        """Apply the preview transform to a world point (matches kdt.transform_kdt: scale about
        the KDT bbox centre, then translate)."""
        if not self._kf:
            return x, y
        dx, dy, s = self._kdt_xform()
        cx = (self._kf.bbox_min[0] + self._kf.bbox_max[0]) / 2.0
        cy = (self._kf.bbox_min[1] + self._kf.bbox_max[1]) / 2.0
        return cx + s * (x - cx) + dx, cy + s * (y - cy) + dy

    def _kdt_preview(self):
        if self._kf:
            self._kdt_show.set(True)
        self._invalidate_vec()      # KDT mesh shown/transform applied — rebuild the vector overlay

    def _kdt_reset(self, redraw=True):
        self._kdt_dx.set("0"); self._kdt_dy.set("0"); self._kdt_scale.set("1.0")
        if redraw:
            self._invalidate_vec()  # transform reset moves the mesh — rebuild the vector overlay

    def _kdt_follow_toggle(self):
        """KDT follow writes verts only (encode_mesh) on save — it KEEPS the per-triangle zone link so
        capture still works, but the tree isn't rebuilt, so only MODEST moves are safe. Note that."""
        if self._kdt_follow.get():
            ui_util.info(
                self,
                t("map.kdt_follows_sectors_modest_moves"),
                t("map.when_edit_sectors_kdt_mesh"))
        self._invalidate_redraw()

    def _kdt_edit_toggle(self):
        """Enter/leave KDT border-drag mode. Editing verts and the rigid bbox-transform are mutually
        exclusive save paths, so entering border-edit forces the transform back to identity."""
        if self._kdt_edit.get():
            self._kdt_show.set(True)
            self._kdt_dx.set("0"); self._kdt_dy.set("0"); self._kdt_scale.set("1.0")
        self._invalidate_redraw()

    def _kdt_handle_at(self, sx, sy):
        """Border-edit mode: index of the KDT border vertex whose handle is near (sx,sy)."""
        if not (self._kdt_edit.get() and self._kdt_show.get() and self._bbox and self._kdt_bverts):
            return None
        best, bestd = None, (_HANDLE_R + 3) ** 2
        for vi in self._kdt_bverts:
            if vi >= len(self._kdt_world):
                continue
            hx, hy = self._world_to_screen(*self._kdt_xform_world(*self._kdt_world[vi]))
            d = (hx - sx) ** 2 + (hy - sy) ** 2
            if d <= bestd:
                best, bestd = vi, d
        return best

    def _kdt_move(self, wx, wy):
        """Move the grabbed KDT node to (wx,wy) — just that one node (KDT meshes have NO welded/coincident
        twins). Then, if 'fit interior' is on, run the geometry-saving optimisation that repositions the
        other interior nodes via the connection graph to match the moved border."""
        if self._drag_kdt_vi is None or self._drag_kdt_orig is None or self._drag_kdt_p0 is None:
            return
        vi = self._drag_kdt_vi
        if vi >= len(self._drag_kdt_orig):
            return
        ox, oy = self._drag_kdt_orig[vi]
        self._kdt_world[vi] = (ox + (wx - self._drag_kdt_p0[0]), oy + (wy - self._drag_kdt_p0[1]))
        if self._kdt_relax.get():
            self._kdt_fit_interior()
        self._kdt_border_bind = None         # direct KDT edit invalidates the scenario-follow binding
        self._kdt_dirty = True

    def _kdt_fit_interior(self, iters=40):
        """GEOMETRY-SAVING optimisation of the interior KDT nodes — Laplacian surface EDITING (not the
        broken neighbour-average SMOOTHING). KDT nodes only have CONNECTIONS (triangle-edge neighbours),
        no coincident twins. We preserve each interior node's ORIGINAL Laplacian coordinate
        δ_v = orig_v − mean(orig_neighbours) (its local shape) while pinning the moved border nodes, and
        solve new_v = mean(new_neighbours) + δ_v (Gauss-Seidel). So the interior FOLLOWS the moved border
        through the connection graph while KEEPING its geometry — and reproduces the original exactly when
        the border hasn't moved (δ makes it identity)."""
        adj = self._kdt_adj; orig = self._kdt_orig
        if not (adj and orig and len(orig) == len(self._kdt_world) and self._kdt_bverts):
            return
        delta = {}
        for v, nb in adj.items():
            if nb:
                mx = sum(orig[n][0] for n in nb) / len(nb)
                my = sum(orig[n][1] for n in nb) / len(nb)
                delta[v] = (orig[v][0] - mx, orig[v][1] - my)
        free = [v for v in range(len(self._kdt_world)) if v not in self._kdt_bverts and adj.get(v)]
        if not free:
            return
        W = [list(p) for p in self._kdt_world]          # border already pinned at its moved positions
        for _ in range(iters):
            for v in free:
                nb = adj[v]
                sx = sum(W[n][0] for n in nb) / len(nb)
                sy = sum(W[n][1] for n in nb) / len(nb)
                W[v][0] = sx + delta[v][0]; W[v][1] = sy + delta[v][1]
        for v in free:
            self._kdt_world[v] = (W[v][0], W[v][1])

    def _kdt_relax_now(self):
        """Manual geometry-saving interior fit to the current border, then mark dirty + redraw."""
        if not (self._kf and self._kdt_world and self._kdt_adj):
            ui_util.info(self, t("map.no_kdt_2"), t("map.load_scenario_kdt_first"))
            return
        self._kdt_fit_interior()
        self._kdt_dirty = True
        self._invalidate_redraw()

    # ── KDT follows the scenario sectors (border-bind + interior relax) ───────────
    @staticmethod
    def _closest_on_seg(p, a, b):
        """(t, dist) of the closest point on segment a-b to p; point = a + t*(b-a)."""
        dx, dy = b[0] - a[0], b[1] - a[1]
        L2 = dx * dx + dy * dy
        if L2 < 1e-12:
            return 0.0, math.hypot(p[0] - a[0], p[1] - a[1])
        t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
        return t, math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))

    def _kdt_embed_in_scenario(self):
        """Bind only the KDT BORDER verts to the scenario sector BORDERS: each is tied to the nearest
        scenario outer-ring EDGE (render zone, edge index k, param t). Built against current positions
        (no jump). The KDT INTERIOR verts are NOT bound — they relax to the moved borders in follow."""
        self._kdt_border_bind = None
        if not (self._zones and self._kdt_world and self._kdt_bverts and self._sector_rings):
            return
        edges = []                                          # (render_zi, k, A, B) over all outer rings
        for ring in self._sector_rings.values():
            zi = ring["zi"]; V = self._zones[zi]["verts"]; outer = ring["outer"]; cnt = ring["cnt"]
            for k in range(cnt):
                edges.append((zi, k, V[outer[k]], V[outer[(k + 1) % cnt]]))
        bind = {}
        for kvi in self._kdt_bverts:
            if kvi >= len(self._kdt_world):
                continue
            p = self._kdt_world[kvi]
            best = None
            for (zi, k, A, B) in edges:
                t, d = self._closest_on_seg(p, A, B)
                if best is None or d < best[0]:
                    best = (d, zi, k, t)
            if best:
                bind[kvi] = (best[1], best[2], best[3])
        self._kdt_border_bind = bind

    def _kdt_follow_scenario(self):
        """KDT follows the scenario: move each KDT BORDER vert onto its bound scenario outer-ring edge
        (so KDT borders track the sector borders), then RELAX the KDT interior verts (pinned to those
        borders) so the interior optimizes its layout. Binding built lazily on first use."""
        if not (self._kdt_follow.get() and self._kdt_world and self._kdt_bverts):
            return
        if self._kdt_border_bind is None:
            self._kdt_embed_in_scenario()
        if not self._kdt_border_bind:
            return
        new = list(self._kdt_world)
        for kvi, (zi, k, t) in self._kdt_border_bind.items():
            if zi >= len(self._zones):
                continue
            ring = self._sector_rings.get(self._zones[zi]["scn_i"])
            if not ring:
                continue
            V = self._zones[zi]["verts"]; outer = ring["outer"]; cnt = ring["cnt"]
            A = V[outer[k]]; B = V[outer[(k + 1) % cnt]]
            new[kvi] = ((1 - t) * A[0] + t * B[0], (1 - t) * A[1] + t * B[1])
        self._kdt_world = new
        self._kdt_relax_interior()                          # interior KDT nodes optimize to the borders
        self._kdt_dirty = True

    # ── ring-hooked sector-boundary editing ───────────────────────────────────────
    @staticmethod
    def _seg_dist(p, a, b):
        """Perpendicular distance from p to segment a-b."""
        dx, dy = b[0] - a[0], b[1] - a[1]
        L2 = dx * dx + dy * dy
        if L2 < 1e-12:
            return math.hypot(p[0] - a[0], p[1] - a[1])
        t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
        return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))

    def _ring_dist(self, p, ring_pts):
        return min((self._seg_dist(p, ring_pts[k], ring_pts[(k + 1) % len(ring_pts)])
                    for k in range(len(ring_pts))), default=0.0)

    def _build_sector_rings(self):
        """Per scenario zone, decode the ring structure for hooked editing:
          outer ring = vertices[off : off+cnt] (the editable boundary, in polygon order),
          inner ring = vertices[off+cnt : off+2cnt] (hooked: inner[k] tracks outer[k] at constant width),
          fill verts = vertices[0 : off] (these triangulate the WHOLE sector — keep full coverage).
        Each fill vert is bound to the outer ring by MEAN-VALUE COORDINATES (`mvc`), so when boundary
        nodes move the fill is reconstructed to exactly tile the deformed polygon (no holes — unlike a
        relax). Also one global ribbon WIDTH = mean perpendicular inner->outer distance (equal-width path)."""
        self._sector_rings = {}
        if not self._scn:
            return
        render_zi = {z["scn_i"]: zi for zi, z in enumerate(self._zones)}
        all_w = []
        for si, sz in enumerate(self._scn["zones"]):
            V = sz.get("vertices") or []
            off, cnt = sz.get("border_vtx", (0, 0))
            if cnt < 3 or off + 2 * cnt > len(V) or si not in render_zi:
                continue
            outer = list(range(off, off + cnt))
            inner = list(range(off + cnt, off + 2 * cnt))
            outerP = [(V[i][0], V[i][1]) for i in outer]
            for ii in inner:
                all_w.append(self._ring_dist((V[ii][0], V[ii][1]), outerP))
            mvc = {fv: self._mvc_weights((V[fv][0], V[fv][1]), outerP) for fv in range(off)}
            self._sector_rings[si] = {
                "zi": render_zi[si], "off": off, "cnt": cnt, "outer": outer, "inner": inner,
                "fill_n": off, "mvc": mvc}
        W = sum(all_w) / len(all_w) if all_w else 0.0
        for ring in self._sector_rings.values():
            ring["width"] = W

    @staticmethod
    def _mvc_weights(v, poly):
        """Mean-value coordinates of v w.r.t. polygon `poly` (ordered (x,y) list): normalized weights
        w with v == sum(w_i * poly_i), so binding a fill vert by these weights makes it follow the
        ring deformation while the fill keeps tiling the polygon. Robust to on-vertex / on-edge cases."""
        n = len(poly); eps = 1e-9
        s = [(poly[i][0] - v[0], poly[i][1] - v[1]) for i in range(n)]
        r = [math.hypot(sx, sy) for sx, sy in s]
        for i in range(n):
            if r[i] < eps:                                   # v == a vertex -> follow it exactly
                w = [0.0] * n; w[i] = 1.0; return w
        t = [0.0] * n
        for i in range(n):
            j = (i + 1) % n
            dot = s[i][0] * s[j][0] + s[i][1] * s[j][1]
            cross = s[i][0] * s[j][1] - s[i][1] * s[j][0]
            denom = r[i] * r[j] + dot
            if abs(denom) < eps:                             # v on edge i-j -> linear interp
                w = [0.0] * n; d = r[i] + r[j]
                w[i] = r[j] / d; w[j] = r[i] / d; return w
            t[i] = cross / denom
        w = [0.0] * n; tot = 0.0
        for i in range(n):
            wi = (t[(i - 1) % n] + t[i]) / r[i]
            w[i] = wi; tot += wi
        if abs(tot) < eps:
            w = [1.0 / r[i] for i in range(n)]; tot = sum(w)
        return [wi / tot for wi in w]

    @staticmethod
    def _offset_ring_inward(P, W):
        """Inward miter offset of an ordered polygon P by perpendicular width W (so the ribbon between
        P and the result is an equal-width band). Inward = toward the polygon centroid; miter clamped."""
        n = len(P)
        if n < 3:
            return list(P)
        cx = sum(x for x, _ in P) / n; cy = sum(y for _, y in P) / n

        def innorm(p0, p1):
            ex, ey = p1[0] - p0[0], p1[1] - p0[1]
            nx, ny = -ey, ex
            L = math.hypot(nx, ny) or 1.0
            nx, ny = nx / L, ny / L
            mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
            if (cx - mx) * nx + (cy - my) * ny < 0:
                nx, ny = -nx, -ny
            return nx, ny

        out = []
        for k in range(n):
            a, p, b = P[(k - 1) % n], P[k], P[(k + 1) % n]
            n1 = innorm(a, p); n2 = innorm(p, b)
            mx, my = n1[0] + n2[0], n1[1] + n2[1]
            ml = math.hypot(mx, my)
            if ml < 1e-6:
                mx, my, denom = n1[0], n1[1], 1.0
            else:
                mx, my = mx / ml, my / ml
                denom = max(mx * n1[0] + my * n1[1], 0.25)   # clamp miter at sharp corners
            off = W / denom
            out.append((p[0] + mx * off, p[1] + my * off))
        return out

    def _set_scn_vert(self, zi, si, vi, x, y):
        """Move a vertex in BOTH the render zone (2D) and the scenario zone (preserving vt2/vt3/vt4)."""
        self._zones[zi]["verts"][vi] = (x, y)
        sv = self._scn["zones"][si]["vertices"]
        old = sv[vi]
        sv[vi] = (x, y, old[2], old[3], old[4])

    def _sector_node_move(self, wx, wy):
        """Drag a boundary (outer-ring) node: move it + its welded twins (shared across zones) + any
        coincident fill verts by the same delta, then for each touched zone re-derive the hooked inner
        ring as a constant-width inward offset and relax the interior fill. Finally the KDT follows."""
        if self._drag_cluster is None or self._drag_orig is None or self._drag_p0 is None:
            return
        p0x, p0y = self._drag_p0
        dx, dy = wx - p0x, wy - p0y
        cl = self._clusters[self._drag_cluster]
        touched = set()
        for (zi, vi) in cl["members"]:                         # move the node + all its welded twins
            si = self._zones[zi]["scn_i"]
            orig = self._drag_orig.get(zi)
            if orig is None or vi >= len(orig):
                continue
            self._set_scn_vert(zi, si, vi, orig[vi][0] + dx, orig[vi][1] + dy)
            touched.add((zi, si))
        if cl.get("kind") == "outer":                          # boundary node -> ring-hooked reshape
            for (zi, si) in touched:
                self._recompute_inner_ring(zi, si)             # inner ring stays equal-width
                self._warp_fill(zi, si)                        # fill keeps tiling the WHOLE sector (MVC)
        self._kdt_follow_scenario()                            # KDT mechanics mesh tracks the edit
        self._dirty = True

    def _recompute_inner_ring(self, zi, si):
        ring = self._sector_rings.get(si)
        if not ring:
            return
        V = self._zones[zi]["verts"]
        outerP = [V[i] for i in ring["outer"]]
        newinner = self._offset_ring_inward(outerP, ring.get("width", 0.0))
        for k, ii in enumerate(ring["inner"]):
            self._set_scn_vert(zi, si, ii, newinner[k][0], newinner[k][1])

    def _warp_fill(self, zi, si):
        """Reconstruct the interior FILL verts from the (moved) outer ring via their mean-value
        coordinates. Because every fill vert is an MVC combination of the ring, the fill exactly tiles
        the deformed polygon — the WHOLE sector stays covered (no holes), unlike a Laplacian relax."""
        ring = self._sector_rings.get(si)
        if not ring or "mvc" not in ring:
            return
        V = self._zones[zi]["verts"]
        outerP = [V[i] for i in ring["outer"]]
        nO = len(outerP)
        for fvi, w in ring["mvc"].items():
            x = sum(w[k] * outerP[k][0] for k in range(nO))
            y = sum(w[k] * outerP[k][1] for k in range(nO))
            self._set_scn_vert(zi, si, fvi, x, y)

    def _save_kdt(self):
        # KDT editing is shelved (its panel is never packed). Moved-vertex saves now flow through the
        # single "Save to mod" button — _save() stages the edited capture mesh (kdt.encode_mesh) when
        # self._kdt_dirty. The old rigid-transform (ΔX/ΔY/scale) write path is parked until the KD-tree
        # rebuilder lands; re-add transform staging in _save() when the KDT panel is un-hidden.
        return self._save()

    # Sector selection used to be driven by a listbox under the canvas; that listbox was retired
    # because capture is governed by the .kdt (not these visual polygons) so users had no reason
    # to pick one. self._sel still tracks the (non-existent) selected sector index for the
    # benefit of _update_detail's defensive sector branch — nothing actually sets it.

    # ── detail-panel rendering helpers ───────────────────────────────────────────
    # Each block of `_update_detail` writes a header line with the "h" tag (gold/bold)
    # followed by either read-only `_det_kv` rows or editable widgets (`_det_entry`,
    # `_det_check`) embedded inline via Text.window_create. The widgets fire their
    # on_commit on <Return>/<FocusOut> — that calls a property-writer which mutates the
    # NDF and queues a deferred rebuild via after_idle so we don't destroy the Entry
    # widget mid-FocusOut.
    def _det_head(self, text):
        self._detail.insert("end", text + "\n", "h")
    def _det_kv(self, k, v):
        self._detail.insert("end", "  %s : %s\n" % (k.ljust(13), v))
    def _det_path(self, k, v):
        # Long PythonClassName-style strings: highlight in path-tag colour for scannability.
        self._detail.insert("end", "  %s : " % k.ljust(13))
        self._detail.insert("end", str(v) + "\n", "path")
    def _det_hint(self, text):
        self._detail.insert("end", text + "\n", "hint")
    def _det_entry(self, key, value, on_commit, width=14, suffix=None):
        """Render `  key : [Entry(value)]  <suffix>` inline. `value=None` → empty entry
        (matches the null/clear-property semantics for fields where blank means 'no prop').
        `on_commit(s)` fires on <Return> and <FocusOut> with the raw string. `suffix` is an
        optional hint rendered after the entry in the dim-grey 'hint' tag — used for
        decoded-meaning glosses like '1 → Team 2'."""
        self._detail.insert("end", "  %s : " % key.ljust(13))
        var = tk.StringVar(value="" if value is None else str(value))
        e = tk.Entry(self._detail, textvariable=var, width=width,
                     background=_R_BG_WIDGET, foreground=_R_TEXT, font=_F_MAIN,
                     insertbackground=_R_TEXT, relief="flat", highlightthickness=1,
                     highlightbackground="#243748", highlightcolor="#88bbff")
        def _commit(_=None):
            on_commit(var.get())
        e.bind("<Return>", _commit)
        e.bind("<FocusOut>", _commit)
        self._detail.window_create("end", window=e)
        if suffix:
            self._detail.insert("end", "  " + suffix, "hint")
        self._detail.insert("end", "\n")
        return e
    def _det_check(self, key, value, on_commit):
        """Render `  key : [Checkbutton(value)]` inline. on_commit(bool) fires on click."""
        self._detail.insert("end", "  %s : " % key.ljust(13))
        var = tk.BooleanVar(value=bool(value))
        cb = tk.Checkbutton(self._detail, variable=var,
                            background=_R_BG_WIDGET, selectcolor=_R_BG_WIDGET,
                            activebackground=_R_BG_WIDGET, foreground=_R_TEXT,
                            font=_F_MAIN, highlightthickness=0, borderwidth=0,
                            command=lambda: on_commit(var.get()))
        self._detail.window_create("end", window=cb)
        self._detail.insert("end", "\n")
        return cb

    # ── inline-edit property writers ─────────────────────────────────────────────
    # All commits run NDF mutations synchronously (cheap), mark the scenario dirty, then
    # queue _refresh_after_edit via after_idle. Deferring the rebuild dodges the "destroy
    # widget while handling its own FocusOut" hazard tkinter throws otherwise.
    def _refresh_after_edit(self):
        """Re-parse placements and re-select the edited item by item_idx."""
        if not self.winfo_exists():                    # deferred after_idle fired after the editor closed
            return
        if self._pndf is None or self._place_sel is None or self._place_sel >= len(self._places):
            return
        item_idx = self._places[self._place_sel]["item_idx"]
        self._rebuild_places(select_item_idx=item_idx)

    def _commit_int_prop(self, addon, prop_name, value_str, blank_clears=False):
        """Write an Int32 property. blank_clears=True removes the property entirely when the
        entry is empty — that's the encoding for Camp=null / FFA-seat AlliancePriority."""
        if self._pndf is None or addon is None:
            return
        s = value_str.strip()
        if blank_clears and s == "":
            p = self._pndf.prop_by_name_and_class(prop_name, addon.class_index)
            if p is not None:
                addon.remove(p.index)
            self._dirty = True
            self.after_idle(self._refresh_after_edit)
            return
        try:
            v = int(s)
        except ValueError:
            return       # silently reject — entry keeps the bad string; next commit retries
        self._pndf.set_property(addon, prop_name,
                                ndfbin.NdfValue(ndfbin.T.Int32, v), create=True)
        self._dirty = True
        self.after_idle(self._refresh_after_edit)

    def _commit_float_prop(self, addon, prop_name, value_str):
        if self._pndf is None or addon is None: return
        try:
            v = float(value_str.strip())
        except ValueError:
            return
        self._pndf.set_property(addon, prop_name,
                                ndfbin.NdfValue(ndfbin.T.Float32, v), create=True)
        self._dirty = True
        self.after_idle(self._refresh_after_edit)

    def _commit_stringref_prop(self, addon, prop_name, value_str):
        if self._pndf is None or addon is None: return
        s = value_str.strip()
        if not s:
            return
        self._pndf.set_property(addon, prop_name,
                                ndfbin.NdfValue(ndfbin.T.StringRef, self._pndf.ensure_string(s)),
                                create=True)
        self._dirty = True
        self.after_idle(self._refresh_after_edit)

    def _commit_widestring_prop(self, addon, prop_name, value_str):
        if self._pndf is None or addon is None: return
        self._pndf.set_property(addon, prop_name,
                                ndfbin.NdfValue(ndfbin.T.WideStr, value_str),
                                create=True)
        self._dirty = True
        self.after_idle(self._refresh_after_edit)

    # ── schema-driven field rendering (placement_schema) ─────────────────────────
    # The per-kind hardcoded field blocks were replaced by a single loop over the placement's
    # PlacementClassSchema: every field (including ones currently nil/absent) gets a typed widget,
    # a decoded-meaning gloss, and help. Typing a value into a blank field ADDS the property
    # (set_property create=True); clearing an optional field REMOVES it. So behaviour-driving
    # properties are reachable without the raw editor.
    def _field_value(self, addon, item, fspec):
        """Live raw value of a schema field on its target instance, or None if absent."""
        target = item if fspec.on_item else addon
        if target is None:
            return None
        v = _ndf_prop(self._pndf, target, fspec.prop)
        if v is None:
            return None
        if v.type_id in (ndfbin.T.StringRef, ndfbin.T.PathRef):
            return self._pndf.get_string(v.raw)
        return v.raw

    def _det_combo(self, key, cur, choices, decode, on_pick, width=20):
        """Inline read-only combobox for an enum field. `choices` may include None ('(none)')."""
        choices = list(choices)
        if cur is not None and cur not in choices:
            choices = choices + [cur]      # surface an out-of-domain existing value rather than hide it
        # Let a field's own decode() name the None entry too — the Camp field calls it "camp 0
        # (none)" because that is what an absent Camp resolves to, and a hardcoded "(none)" hid that.
        labels = [(decode(c) if decode else ("(none)" if c is None else str(c))) for c in choices]
        self._detail.insert("end", "  %s : " % key.ljust(13))
        cb = ttk.Combobox(self._detail, values=labels, state="readonly", width=width, font=_F_MAIN)
        try:
            cb.current(choices.index(cur))
        except ValueError:
            if None in choices:
                cb.current(choices.index(None))
        cb.bind("<<ComboboxSelected>>",
                lambda _=None: on_pick(choices[cb.current()] if cb.current() >= 0 else None))
        self._detail.window_create("end", window=cb)
        self._detail.insert("end", "\n")
        return cb

    def _commit_field_value(self, addon, item, fspec, raw):
        """Write (or, when blank/None on an optional field, REMOVE) a schema field. Uses the field's
        declared type_id so the right NDF value is written; create=True adds a previously-absent prop."""
        if self._pndf is None:
            return
        target = item if fspec.on_item else addon
        if target is None:
            return
        blank = raw is None or (isinstance(raw, str) and raw.strip() == "")
        if blank:
            if not fspec.required:
                p = self._pndf.prop_by_name_and_class(fspec.prop, target.class_index)
                if p is not None:
                    target.remove(p.index)
                self._dirty = True
                self.after_idle(self._refresh_after_edit)
            return
        tid = fspec.type_id
        try:
            if tid in (ndfbin.T.Int32, ndfbin.T.UInt32, ndfbin.T.Int16, ndfbin.T.UInt16):
                val = ndfbin.NdfValue(tid, int(str(raw).strip()))
            elif tid in (ndfbin.T.Float32, ndfbin.T.Float64):
                val = ndfbin.NdfValue(tid, float(str(raw).strip()))
            elif tid in (ndfbin.T.StringRef, ndfbin.T.PathRef):
                val = ndfbin.NdfValue(tid, self._pndf.ensure_string(str(raw)))
            elif tid == ndfbin.T.WideStr:
                val = ndfbin.NdfValue(tid, str(raw))
            else:
                return   # vector3 etc. — not text-editable here
        except (ValueError, TypeError):
            return       # bad input — keep the widget, retry on next commit
        self._pndf.set_property(target, fspec.prop, val, create=True)
        self._dirty = True
        self.after_idle(self._refresh_after_edit)

    def _render_schema_fields(self, pl, addon, item, editing):
        """Render every editable field of this placement's class from placement_schema."""
        sch = pschema.schema_for(pl["kind"])
        if sch is None:
            return
        if sch.note:
            self._det_head(sch.note)
        for f in sch.fields:
            if f.on_item:
                continue                       # Rotation is rendered in the GEOMETRY section
            cur = self._field_value(addon, item, f)
            decoded = f.decode(cur) if f.decode else None
            if not editing:
                if cur is None and not f.required:
                    continue                   # keep the read-only view uncluttered
                shown = t("map.unset") if cur is None else cur
                self._det_kv(t(f.label), "%s   %s" % (shown, decoded) if decoded else shown)
                continue
            if f.widget in ("camp", "priority", "warmup"):
                self._det_combo(t(f.label), cur, f.choices, f.decode,
                                lambda v, ff=f: self._commit_field_value(addon, item, ff, v))
            elif f.widget == "vector3":
                self._det_kv(t(f.label), ("%.0f, %.0f" % (cur[0], cur[1])) if cur else t("map.unset"))
            else:
                w = 40 if f.widget == "pyclass" else (28 if f.widget == "widestr" else 12)
                self._det_entry(t(f.label), "" if cur is None else cur,
                                lambda s, ff=f: self._commit_field_value(addon, item, ff, s),
                                width=w, suffix=decoded)
            if f.help:
                self._det_hint("  " + t(f.help))

    def _commit_rotation(self, item, value_str):
        if self._pndf is None or item is None: return
        try:
            v = float(value_str.strip())
        except ValueError:
            return
        self._pndf.set_property(item, "Rotation",
                                ndfbin.NdfValue(ndfbin.T.Float32, v), create=True)
        self._dirty = True
        self.after_idle(self._refresh_after_edit)

    def _commit_ffa_toggle(self, addon, on):
        """Toggle the FFA-seat encoding by stripping/setting AlliancePriority (priority absent =
        FFA seat; priority=1 = the de-facto default when leaving FFA mode)."""
        if self._pndf is None or addon is None: return
        if on:
            ap = self._pndf.prop_by_name_and_class("AlliancePriority", addon.class_index)
            if ap is not None:
                addon.remove(ap.index)
        else:
            self._pndf.set_property(addon, "AlliancePriority",
                                    ndfbin.NdfValue(ndfbin.T.Int32, 1), create=True)
        self._dirty = True
        self.after_idle(self._refresh_after_edit)

    def _update_detail(self):
        td = self._detail
        td.config(state="normal")
        td.delete("1.0", "end")
        if self._place_sel is not None and self._place_sel < len(self._places):
            pl = self._places[self._place_sel]
            x, y, z_ = pl["pos"]
            ex = pl["extra"]
            kind = pl["kind"]
            # The DETAILS panel is ALWAYS editable — it edits the placement itself, so its fields
            # never lock. The "Edit placements (drag)" checkbox now ONLY enables dragging the marker
            # on the map (see _on_press); it no longer gates field editing.
            editing = True
            drag_on = self._place_edit.get()
            # Resolve the live NdfInstance handles so the inline-edit closures don't have to
            # re-look them up by index every keystroke (the instances themselves are stable
            # across _rebuild_places; only their indices in self._places shift).
            addon = self._pndf.instances[pl["addon_idx"]] if (
                self._pndf is not None and pl["addon_idx"] is not None) else None
            item = self._pndf.instances[pl["item_idx"]] if (
                self._pndf is not None and pl["item_idx"] is not None) else None

            # PLACEMENT header — kind + label, so you can see at a glance what's selected.
            self._det_head(t("map.placement_kind_label",
                             kind=kind, label=("  ·  " + pl["label"]) if pl["label"] else ""))
            # ...then say in words what the icon on the map is saying in a picture. A class name
            # like Unit_Semovente_M_43_105_25 doesn't tell you it's heavy self-propelled artillery.
            role = proles.role(pl.get("role"))
            if role is not None:
                self._det_kv(t("map.role"), role.label)
                if role.desc:
                    self._det_hint("    " + role.desc)
            # Who it IS vs who OWNS it — the two can differ (a French camp can field German tanks),
            # which is exactly why the icon carries both flags.
            if pl.get("nation"):
                self._det_kv(t("map.unit_nation"),
                             cresolve.NATION_LABEL.get(pl["nation"], pl["nation"]))
            ci = self._camp_info_for(pl)
            if ci is not None:
                self._det_kv(t("map.controlled_by"),
                             t("map.camp_nation_ia", nation=cresolve.NATION_LABEL.get(
                                 cresolve.NATION_ICON.get(ci.get("nation")), ci.get("nation") or "?"),
                               ia=ci.get("ia") or "?"))
            elif kind in ("depot", "unit", "building", "spawn"):
                camp = pl["extra"].get("camp")
                if camp == cresolve.NEUTRAL_CAMP:
                    self._det_hint("    " + t("map.camp_is_neutral"))
                elif self._camp_map and camp not in self._camp_map and camp is not None:
                    # The engine drops a placement whose Camp has no camp behind it (see
                    # camp_resolver) — it never spawns. Worth saying out loud.
                    self._det_hint("    " + t("map.camp_missing_never_spawns", camp=camp))

            # All per-kind fields are now rendered from placement_schema (typed widgets, add/remove of
            # nil fields, decoded meanings + help). Replaces the old hardcoded SPAWN/HQ/ville/zone blocks.
            self._render_schema_fields(pl, addon, item, editing)
            # HQ keeps a couple of read-only extras the schema doesn't own (live campath link).
            if kind == "hq" and ex.get("campath_keys"):
                self._det_kv(t("map.campath_keys"), "%d" % len(ex["campath_keys"]))

            # GEOMETRY (last because most users care about identity/ownership first). Position
            # stays drag-only; rotation is editable for kinds that AREN'T road-locked
            # (depot/HQ have their facing snapped to the nearest road in-game).
            self._det_head(t("map.geometry"))
            self._det_kv(t("map.position_2"), "%.0f, %.0f" % (x, y))
            self._det_kv(t("map.height_z"), "%.0f" % z_)
            road_locked = kind in ("depot", "hq")
            if editing and not road_locked:
                rv = pl["rot"] if pl["rot"] is not None else 0.0
                self._det_entry(t("map.rotation"), "%.4f" % rv,
                                lambda s, it=item: self._commit_rotation(it, s),
                                width=12, suffix=t("map.rad_deg_suffix", deg=math.degrees(rv)))
            elif pl["rot"] is not None:
                self._det_kv(t("map.rotation"),
                             "%.3f rad  (%.0f°)" % (pl["rot"], math.degrees(pl["rot"])))

            # EDIT hints (only when they say something useful — keep the panel quiet otherwise).
            self._det_head(t("map.edit"))
            if road_locked:
                self._det_hint(t("map.facing_auto_snaps_nearest_road"))
            if not drag_on:
                self._det_hint(t("map.toggle_edit_placements_above_edit"))
            elif kind == "hq":
                self._det_hint(t("map.drag_hq_move_camera_follows"))
            else:
                self._det_hint(t("map.drag_move_then_save"))

        elif self._sel is not None and self._sel < len(self._zones):
            # Defensive sector branch — the listbox that used to drive this was removed, so
            # in practice _sel stays None. Kept so any future canvas-pick can light it up.
            z = self._zones[self._sel]
            xs = [v[0] for v in z["verts"]]; ys = [v[1] for v in z["verts"]]
            self._det_head(t("map.sector_idx", idx=z["idx"]))
            self._det_kv(t("map.name"), z["name"])
            self._det_kv(t("map.marker"), "%.0f, %.0f" % (z["pos"][0], z["pos"][1]))
            self._det_kv(t("map.vertices"), len(z["verts"]))
            self._det_kv(t("map.triangles"), len(z["faces"]))
            self._det_kv(t("map.boundary_pts"), len(z["bverts"]))
            self._det_kv(t("map.x_range"), "%.0f … %.0f" % (min(xs), max(xs)))
            self._det_kv(t("map.y_range"), "%.0f … %.0f" % (min(ys), max(ys)))
            self._det_hint(t("map.sectors_are_visual_only_capture"))
        else:
            self._det_hint(t("map.click_placement_map_see_its"))
        td.config(state="disabled")

    def _on_place_edit_toggle(self):
        """Edit-mode checkbox callback: redraws the overlays AND re-renders the DETAILS panel
        (so its inline edit widgets appear / disappear). The dedicated HQ editor that used to
        live under the canvas is gone — HQ field editing is now inline alongside everything
        else, gated by _place_edit inside _update_detail."""
        self._redraw()
        self._update_detail()

    # ── view transforms ─────────────────────────────────────────────────────────
    def _world_to_base(self, x, y):
        minx, miny, maxx, maxy = self._bbox
        w, h = self._pil.size
        bx = (x - minx) / (maxx - minx) * w
        by = (maxy - y) / (maxy - miny) * h if self._flip_y.get() else (y - miny) / (maxy - miny) * h
        return bx, by

    def _world_to_screen(self, x, y):
        bx, by = self._world_to_base(x, y)
        return bx * self._scale + self._ox, by * self._scale + self._oy

    def _screen_xform(self):
        """The world->screen transform as four floats: sx = x*kx + cx, sy = y*ky + cy.

        World->screen is affine, so a draw loop can collapse it to two multiply-adds per point
        instead of calling _world_to_screen. That matters because _world_to_base reads
        `self._flip_y` — a Tk variable — on EVERY call: at 1088 markers that was ~1350 method calls
        and ~1350 Tk global-variable reads per frame, purely to recompute the same constants.
        Callers must recompute this whenever scale/offset/bbox/flip change (i.e. once per frame)."""
        minx, miny, maxx, maxy = self._bbox
        w, h = self._pil.size
        sc, ox, oy = self._scale, self._ox, self._oy
        kx = w / (maxx - minx) * sc
        cx = -minx * kx + ox
        if self._flip_y.get():
            ky = -h / (maxy - miny) * sc
            cy = maxy * (h / (maxy - miny)) * sc + oy
        else:
            ky = h / (maxy - miny) * sc
            cy = -miny * ky + oy
        return kx, cx, ky, cy

    def _screen_to_world(self, sx, sy):
        minx, miny, maxx, maxy = self._bbox
        w, h = self._pil.size
        bx = (sx - self._ox) / self._scale
        by = (sy - self._oy) / self._scale
        x = minx + bx / w * (maxx - minx)
        y = maxy - by / h * (maxy - miny) if self._flip_y.get() else miny + by / h * (maxy - miny)
        return x, y

    def _fit_view(self):
        if not self.winfo_exists():                    # deferred retry fired after the editor closed
            return
        if not self._pil:
            self._redraw(); return
        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        if cw < 50 or ch < 50:
            # canvas not laid out yet (first load) — retry once it has a real size, otherwise the
            # fit scale comes out tiny and the map looks zoomed way out
            self.after(60, self._fit_view)
            return
        w, h = self._pil.size
        self._scale = min(cw / w, ch / h) * 0.95   # whole map visible with a small margin
        self._ox = (cw - w * self._scale) / 2
        self._oy = (ch - h * self._scale) / 2
        self._invalidate_redraw()

    # ── welded corners (shared across zones move together) ───────────────────────
    def _build_clusters(self):
        """Cluster EVERY zone vertex (outer ring, inner ring, interior fill) by world position so each
        is a draggable handle and shared verts weld across touching zones. `kind` (outer/inner/interior)
        sets the drag behaviour: outer = ring-hooked (inner ring + fill + KDT follow); inner/interior =
        free single-node move (manual fixes) with the KDT still following. Built on scenario load."""
        self._clusters = []
        if not self._zones or not self._bbox:
            return
        tol = (self._bbox[2] - self._bbox[0]) * 0.0015      # ~0.15% of map width
        outer_set, inner_set = {}, {}
        for zi, z in enumerate(self._zones):
            ring = self._sector_rings.get(z["scn_i"])
            outer_set[zi] = set(ring["outer"]) if ring else set(z.get("bverts", []))
            inner_set[zi] = set(ring["inner"]) if ring else set()
        pts = [(zi, vi, self._zones[zi]["verts"][vi][0], self._zones[zi]["verts"][vi][1])
               for zi, z in enumerate(self._zones) for vi in range(len(z["verts"]))]
        used = [False] * len(pts)
        for i in range(len(pts)):
            if used[i]:
                continue
            zi, vi, x, y = pts[i]
            members = [(zi, vi)]
            used[i] = True
            for j in range(i + 1, len(pts)):
                if not used[j] and abs(pts[j][2] - x) <= tol and abs(pts[j][3] - y) <= tol:
                    members.append((pts[j][0], pts[j][1])); used[j] = True
            if any(v in outer_set.get(z2, ()) for z2, v in members):
                kind = "outer"
            elif any(v in inner_set.get(z2, ()) for z2, v in members):
                kind = "inner"
            else:
                kind = "interior"
            self._clusters.append({"members": members, "kind": kind})

    def _cluster_pos(self, cl):
        zi, vi = cl["members"][0]
        return self._zones[zi]["verts"][vi]

    # ── interaction ─────────────────────────────────────────────────────────────
    def _handle_at(self, sx, sy):
        """Edit mode: index of the welded-corner cluster whose handle is near (sx,sy)."""
        if not (self._edit.get() and self._pil and self._bbox):
            return None
        for ci, cl in enumerate(self._clusters):
            hx, hy = self._world_to_screen(*self._cluster_pos(cl))
            if abs(hx - sx) <= _HANDLE_R + 2 and abs(hy - sy) <= _HANDLE_R + 2:
                return ci
        return None

    # ── placements (depots / HQ / labels) ───────────────────────────────────────
    _PLACE_HIT_R = 8   # px hit radius for a placement marker

    def _place_at(self, sx, sy):
        """Index of the placement whose marker is near (sx,sy), or None."""
        if not (self._show_places.get() and self._places and self._pil and self._bbox):
            return None
        best, bestd = None, (self._PLACE_HIT_R + 2) ** 2
        for pi, pl in enumerate(self._places):
            hx, hy = self._world_to_screen(pl["pos"][0], pl["pos"][1])
            d = (hx - sx) ** 2 + (hy - sy) ** 2
            if d <= bestd:
                best, bestd = pi, d
        return best

    def _campath_prop(self, inst, name):
        p = (self._campath.prop_by_name_and_class(name, inst.class_index)
             or self._campath.prop_by_name(name))
        return inst.get(p.index) if p else None

    def _link_campaths(self):
        """Attach each HQ's warmup-campath position keyframes (the REAL start camera) to its
        placement, and set extra['cam'] to the resting (last) keyframe for display/drag."""
        for pl in self._places or []:
            if pl["kind"] != "hq":
                continue
            pl["extra"]["campath_keys"] = []
            name = pl["extra"].get("warmup")
            if not (self._campath and name):
                continue
            for ci, cinst in self._campath.find_instances("TCameraPath"):
                nv = self._campath_prop(cinst, "Name")
                cn = self._campath.get_string(nv.raw) if nv and nv.type_id == ndfbin.T.StringRef else None
                if cn != name:
                    continue
                pk = self._campath_prop(cinst, "PositionKeyVector")
                keys = [v.raw[1][0] for v in pk.raw] if pk and pk.type_id == ndfbin.T.List else []
                dk = self._campath_prop(cinst, "DirectionKeyVector")
                dkeys = [v.raw[1][0] for v in dk.raw] if dk and dk.type_id == ndfbin.T.List else []
                pl["extra"]["campath_keys"] = keys
                pl["extra"]["campath_dirkeys"] = dkeys   # per-key look direction (normalized vectors)
                if keys:
                    last = self._campath.instances[keys[-1]]
                    cp = self._campath.prop_by_name_and_class("Coord", last.class_index)
                    if cp is not None:
                        pl["extra"]["cam"] = tuple(last.get(cp.index).raw)   # resting view = last key
                break

    def _shift_campath(self, pl, dx, dy):
        """Translate all of an HQ's warmup-campath position keyframes by (dx,dy) so the start camera
        follows; also moves the displayed resting marker. Marks the campath dirty for save."""
        keys = pl["extra"].get("campath_keys") or []
        if not (self._campath and keys) or (dx == 0 and dy == 0):
            return
        for ki in keys:
            inst = self._campath.instances[ki]
            cp = self._campath.prop_by_name_and_class("Coord", inst.class_index)
            if cp is None:
                continue
            x, y, z = inst.get(cp.index).raw
            inst.set(cp.index, ndfbin.NdfValue(ndfbin.T.Vector3, (x + dx, y + dy, z)))
        cam = pl["extra"].get("cam")
        if cam:
            pl["extra"]["cam"] = (cam[0] + dx, cam[1] + dy, cam[2])
        self._aim_camera_at_hq(pl)          # re-point each keyframe at the (possibly new) HQ
        self._campath_dirty = True
        self._dirty = True

    def _aim_camera_at_hq(self, pl):
        """Recompute each warmup-keyframe's look direction (DirectionKeyVector) to point from the
        camera keyframe at the HQ base, so the camera always faces the HQ after a move."""
        pkeys = pl["extra"].get("campath_keys") or []
        dkeys = pl["extra"].get("campath_dirkeys") or []
        if not (self._campath and pkeys and dkeys):
            return
        tx, ty, tz = pl["pos"]              # HQ base = look-at target
        for pk, dk in zip(pkeys, dkeys):
            pi = self._campath.instances[pk]
            cpp = self._campath.prop_by_name_and_class("Coord", pi.class_index)
            cx, cy, cz = pi.get(cpp.index).raw
            vx, vy, vz = tx - cx, ty - cy, tz - cz
            m = math.sqrt(vx * vx + vy * vy + vz * vz) or 1.0
            di = self._campath.instances[dk]
            cpd = self._campath.prop_by_name_and_class("Coord", di.class_index)
            if cpd is not None:
                di.set(cpd.index, ndfbin.NdfValue(ndfbin.T.Vector3, (vx / m, vy / m, vz / m)))

    def _place_set_pos(self, pl, wx, wy):
        """Move a placement to world (wx,wy), writing Position back to the NDF. For an HQ, its warmup
        campath (the actual start camera) is carried by the SAME delta so the opening view follows."""
        ox, oy, z = pl["pos"]
        pl["pos"] = (wx, wy, z)
        inst = self._pndf.instances[pl["item_idx"]]
        p = self._pndf.prop_by_name_and_class("Position", inst.class_index)
        if p is not None:
            inst.set(p.index, ndfbin.NdfValue(ndfbin.T.Vector3, (float(wx), float(wy), float(z))))
        if pl["kind"] == "hq":
            self._shift_campath(pl, wx - ox, wy - oy)
        self._dirty = True

    def _set_camera(self, pl, cx, cy):
        """Drag the camera AROUND a ring centred on its HQ: rotate the whole warmup path about the HQ
        so the resting camera orbits to the cursor angle (radius = the HQ's camera distance, fixed).
        Only the viewing angle changes — the camera keeps looking at the HQ."""
        cam = pl["extra"].get("cam")
        if not cam:
            return
        hx, hy = pl["pos"][0], pl["pos"][1]
        old = math.atan2(cam[1] - hy, cam[0] - hx)
        new = math.atan2(cy - hy, cx - hx)
        self._rotate_campath(pl, new - old)

    def _rotate_campath(self, pl, dphi):
        """Rotate all of an HQ's warmup-campath position keyframes about the HQ base by dphi (radius
        preserved = orbit on the ring), then re-aim the look directions at the HQ."""
        keys = pl["extra"].get("campath_keys") or []
        if not (self._campath and keys) or dphi == 0.0:
            return
        hx, hy = pl["pos"][0], pl["pos"][1]
        ca, sa = math.cos(dphi), math.sin(dphi)
        def rot(x, y):
            rx, ry = x - hx, y - hy
            return hx + rx * ca - ry * sa, hy + rx * sa + ry * ca
        for ki in keys:
            inst = self._campath.instances[ki]
            cp = self._campath.prop_by_name_and_class("Coord", inst.class_index)
            if cp is None:
                continue
            x, y, z = inst.get(cp.index).raw
            nx, ny = rot(x, y)
            inst.set(cp.index, ndfbin.NdfValue(ndfbin.T.Vector3, (nx, ny, z)))
        cam = pl["extra"].get("cam")
        if cam:
            nx, ny = rot(cam[0], cam[1])
            pl["extra"]["cam"] = (nx, ny, cam[2])
        self._aim_camera_at_hq(pl)
        self._campath_dirty = True
        self._dirty = True

    def _cam_ring_radius(self, pl):
        """World-space radius of an HQ's camera ring = current resting-camera horizontal distance."""
        cam = pl["extra"].get("cam")
        if not cam:
            return 0.0
        return math.hypot(cam[0] - pl["pos"][0], cam[1] - pl["pos"][1])

    def _rot_handle_at(self, sx, sy):
        """Buildings (depot + HQ) auto-orient to the nearest road in-game, so there is no hand-set
        rotation — moving a building re-aims it at whatever road is now nearest."""
        return None

    def _cam_at(self, sx, sy):
        """Index of the HQ whose camera marker is near (sx,sy) — lets the start/warmup camera be
        dragged independently of its base. Only in placement-edit mode."""
        if not (self._place_edit.get() and self._show_places.get() and self._places and self._bbox):
            return None
        for pi, pl in enumerate(self._places):
            if pl["kind"] != "hq" or not pl["extra"].get("cam"):
                continue
            cxs, cys = self._world_to_screen(*pl["extra"]["cam"][:2])
            if (cxs - sx) ** 2 + (cys - sy) ** 2 <= (self._PLACE_HIT_R + 2) ** 2:
                return pi
        return None

    def _place_set_rot(self, pl, sx, sy):
        """Set a placement's facing from the dragged handle (world angle, so Y-flip/zoom don't
        matter). Stores the raw item Rotation (radians) with the per-model front offset undone."""
        wx, wy = self._screen_to_world(sx, sy)
        dx, dy = wx - pl["pos"][0], wy - pl["pos"][1]
        if dx == 0 and dy == 0:
            return
        # store the RAW Rotation (radians): undo the per-model front offset
        theta = math.atan2(dy, dx) - _MODEL_FACING_OFFSET.get(pl["kind"], 0.0)
        pl["rot"] = theta
        inst = self._pndf.instances[pl["item_idx"]]
        p = self._pndf.prop_by_name_and_class("Rotation", inst.class_index)
        if p is not None:
            inst.set(p.index, ndfbin.NdfValue(ndfbin.T.Float32, float(theta)))
        self._dirty = True

    def _select_place(self, pi):
        self._place_sel = pi
        if pi is not None:
            self._sel = None
        self._update_detail()
        self._redraw()   # comp_state covers any sel change; placements are live items

    # ── create / delete placements (NDF instance authoring) ──────────────────────
    def _place_template(self, kind):
        """An existing (item_inst, addon_inst) of `kind` to clone, or None."""
        for pl in self._places:
            if pl["kind"] == kind and pl["addon_idx"] is not None:
                return self._pndf.instances[pl["item_idx"]], self._pndf.instances[pl["addon_idx"]]
        return None

    def _obtain_pair(self, kind):
        """A NEW (item, addon) instance pair in self._pndf for a placement of `kind`, with the item's
        AddOn already pointing at the new addon. Clones a local template if the scenario has one;
        otherwise IMPORTS a donor pair from the corpus (placement_schema.import_placement) so the kind
        is placeable even when this scenario contains none of it (e.g. an HQ in an HQ-less map).
        Returns (new_item, new_addon, item_idx, addon_idx) or None."""
        if self._pndf is None:
            return None
        local = self._place_template(kind)
        if local is not None:
            item_src, addon_src = local
            new_addon = ndfbin.NdfInstance(class_index=addon_src.class_index,
                                           props=copy.deepcopy(addon_src.props))
            self._pndf.instances.append(new_addon)
            addon_idx = len(self._pndf.instances) - 1
            new_item = ndfbin.NdfInstance(class_index=item_src.class_index,
                                          props=copy.deepcopy(item_src.props))
            self._pndf.set_property(new_item, "AddOn", ndfbin.NdfValue(
                ndfbin.T.Reference, (ndfbin.OBJ_REF_MARKER, (addon_idx, new_addon.class_index))),
                create=True)
            self._pndf.instances.append(new_item)
            return new_item, new_addon, len(self._pndf.instances) - 1, addon_idx
        return self._corpus_import_pair(kind)

    def _corpus_import_pair(self, kind):
        """Import a (item, addon) pair of `kind` from a corpus scenario into self._pndf. Returns
        (new_item, new_addon, item_idx, addon_idx) or None."""
        cands = []
        src = pschema.TEMPLATE_SOURCES.get(kind)
        if src:
            cands.append(src)
        cands += [("alpha", "leveldesign_bh"), ("alpha", "leveldesign"),
                  ("m03_italie", "leveldesign_chapter1")]
        seen = set()
        for mp, sc in cands:
            if (mp, sc) in seen:
                continue
            seen.add((mp, sc))
            try:
                raw = self._dm.get("test\\map\\%s\\%s.scenario" % (mp, sc))
                if not raw:
                    continue
                sndf = ndfbin.read(scenario_mod.read(raw)["ndf_data"])
                hit = next((p for p in parse_placements(sndf)
                            if p["kind"] == kind and p["addon_idx"] is not None), None)
                if hit is None:
                    continue
                it_idx, ad_idx = pschema.import_placement(
                    self._pndf, sndf, hit["item_idx"], hit["addon_idx"])
                return (self._pndf.instances[it_idx], self._pndf.instances[ad_idx], it_idx, ad_idx)
            except Exception:
                continue
        return None

    def _nearest_z(self, wx, wy):
        """Z (terrain height) of the closest existing placement — a sane default for a new one."""
        best, bz = None, 0.0
        for pl in self._places:
            d = (pl["pos"][0] - wx) ** 2 + (pl["pos"][1] - wy) ** 2
            if best is None or d < best:
                best, bz = d, pl["pos"][2]
        return bz

    def _item_list_value(self):
        """The List NdfValue holding the TGameDesignItem references, or None."""
        lst_cls = self._pndf.class_by_name("TGameDesignItemList")
        if lst_cls is None:
            return None
        for inst in self._pndf.instances:
            if inst.class_index != lst_cls.index:
                continue
            p = self._pndf.prop_by_name_and_class("GameDesignItemList", inst.class_index)
            v = inst.get(p.index) if p else None
            if v is not None and v.type_id == ndfbin.T.List:
                return v
        return None

    @staticmethod
    def _is_objref_to(val, obj_idx):
        r = val.raw
        return (val.type_id == ndfbin.T.Reference and isinstance(r, tuple)
                and r[0] == ndfbin.OBJ_REF_MARKER and isinstance(r[1], tuple) and r[1][0] == obj_idx)

    def _remap_refs(self, val, removed, newidx):
        """Recursively rewrite object-reference instance indices after deletions."""
        t = val.type_id
        if t == ndfbin.T.Reference:
            r = val.raw
            if isinstance(r, tuple) and r[0] == ndfbin.OBJ_REF_MARKER and isinstance(r[1], tuple):
                obj, cls = r[1]
                if obj not in removed:
                    val.raw = (ndfbin.OBJ_REF_MARKER, (newidx(obj), cls))
        elif t == ndfbin.T.List:
            for it in val.raw:
                self._remap_refs(it, removed, newidx)
        elif t == ndfbin.T.Map:
            for k, v in val.raw:
                self._remap_refs(k, removed, newidx); self._remap_refs(v, removed, newidx)
        elif t == ndfbin.T.Pair:
            k, v = val.raw
            self._remap_refs(k, removed, newidx); self._remap_refs(v, removed, newidx)

    def _rebuild_places(self, select_item_idx=None):
        self._places = parse_placements(self._pndf)
        self._update_place_info()
        self._place_sel = None
        if select_item_idx is not None:
            for i, pl in enumerate(self._places):
                if pl["item_idx"] == select_item_idx:
                    self._place_sel = i
                    break
        self._update_detail()
        self._invalidate_redraw()

    # ── Game-mode HQ materialization (see memory: hq-mode-system) ───────────────────────────────
    @staticmethod
    def _mode_spawns(teams, per_team, ffa=False):
        """Required HQ spawn set for a mode. Team modes: {(AllianceNum 1..teams, AlliancePriority
        1..per_team)}. FFA modes use the vanilla encoding {(AllianceNum 1..teams, None)} — one
        solo-alliance seat per slot, no AlliancePriority property."""
        if ffa:
            return {(t, None) for t in range(1, teams + 1)}
        return {(t, s) for t in range(1, teams + 1) for s in range(1, per_team + 1)}

    def _existing_hq_spawns(self):
        """{(AllianceNum, AlliancePriority)} present in the current scenario — None preserved
        verbatim (FFA seats are NOT collapsed onto the priority=1 key). Use _existing_covers()
        for mode-coverage checks: those want None-priority seats to ALSO cover the team-mode
        priority=1 slot (vanilla blitz is a 1v1 encoded with None priorities, and the engine
        accepts that)."""
        out = {}
        for pl in self._places or []:
            if pl["kind"] != "hq":
                continue
            a = pl["extra"].get("alliance")
            pr = pl["extra"].get("priority")
            out[(a, pr)] = pl
        return out

    def _existing_covers(self):
        """Set of (AllianceNum, AlliancePriority) slots a TEAM mode considers 'already covered'.
        Strict existence PLUS the soft rule that a (a, None) FFA-style seat also satisfies (a, 1) —
        because vanilla 1v1 maps (blitz, etc.) encode their slot-1 HQs with priority=None and the
        engine accepts that. FFA-mode requirements use the strict set (priority=None only) since
        team-style (a, 1) seats are NOT valid FFA seats."""
        strict = set(self._existing_hq_spawns().keys())
        return strict | {(a, 1) for (a, pr) in strict if pr is None}

    def _hq_pos_for_warmup(self, warmup_name):
        for pl in self._places or []:
            if pl["kind"] == "hq" and pl["extra"].get("warmup") == warmup_name:
                return pl["pos"]
        return None

    def _next_warmup_name(self):
        """A fresh 'Warmup_J<n>' not already used by a TCameraPath."""
        used = set()
        if self._campath is not None:
            for _ci, inst in self._campath.find_instances("TCameraPath"):
                nv = self._campath_prop(inst, "Name")
                if nv and nv.type_id == ndfbin.T.StringRef:
                    used.add(self._campath.get_string(nv.raw))
        n = 1
        while f"Warmup_J{n}" in used:
            n += 1
        return f"Warmup_J{n}"

    def _clone_campath_for_hq(self, new_name, new_hq_pos):
        """Create a TCameraPath `new_name` in the campath NDF, cloned from an existing Warmup path, its
        keyframes translated so the camera keeps the template's relative offset from the new HQ, then
        AIMED at the new HQ (DirectionKeyVector = normalized HQ-minus-camera). Registers it as a top
        object so the game finds it. Returns new_name, or None if there's no template path."""
        if self._campath is None:
            return None
        tcps = list(self._campath.find_instances("TCameraPath"))
        if not tcps:
            return None
        _t_ci, t_inst = tcps[0]
        tnv = self._campath_prop(t_inst, "Name")
        tname = self._campath.get_string(tnv.raw) if tnv and tnv.type_id == ndfbin.T.StringRef else None
        pk = self._campath_prop(t_inst, "PositionKeyVector")
        dk = self._campath_prop(t_inst, "DirectionKeyVector")
        if not (pk and dk and pk.type_id == ndfbin.T.List and dk.type_id == ndfbin.T.List and pk.raw):
            return None
        pos_idxs = [v.raw[1][0] for v in pk.raw]
        dir_idxs = [v.raw[1][0] for v in dk.raw]
        key_cls = self._campath.instances[pos_idxs[0]].class_index
        coord_p = self._campath.prop_by_name_and_class("Coord", key_cls)
        # translate anchor = the template's HQ (preserve camera offset); else its first position key
        t_hq = self._hq_pos_for_warmup(tname)
        if t_hq is None:
            t_hq = self._campath.instances[pos_idxs[0]].get(coord_p.index).raw
        dx = new_hq_pos[0] - t_hq[0]; dy = new_hq_pos[1] - t_hq[1]; dz = new_hq_pos[2] - t_hq[2]

        def clone_key(src_idx, translate):
            src = self._campath.instances[src_idx]
            nk = ndfbin.NdfInstance(class_index=src.class_index, props=copy.deepcopy(src.props))
            if translate:
                x, y, z = nk.get(coord_p.index).raw
                nk.set(coord_p.index, ndfbin.NdfValue(ndfbin.T.Vector3, (x + dx, y + dy, z + dz)))
            self._campath.instances.append(nk)
            return len(self._campath.instances) - 1

        new_pos = [clone_key(i, True) for i in pos_idxs]
        new_dir = [clone_key(i, False) for i in dir_idxs]

        def objref(idx):
            return ndfbin.NdfValue(ndfbin.T.Reference, (ndfbin.OBJ_REF_MARKER, (idx, key_cls)))
        npath = ndfbin.NdfInstance(class_index=t_inst.class_index, props=copy.deepcopy(t_inst.props))
        npk = self._campath.prop_by_name_and_class("PositionKeyVector", npath.class_index)
        ndk = self._campath.prop_by_name_and_class("DirectionKeyVector", npath.class_index)
        nnm = self._campath.prop_by_name_and_class("Name", npath.class_index)
        npath.set(npk.index, ndfbin.NdfValue(ndfbin.T.List, [objref(i) for i in new_pos]))
        npath.set(ndk.index, ndfbin.NdfValue(ndfbin.T.List, [objref(i) for i in new_dir]))
        npath.set(nnm.index, ndfbin.NdfValue(ndfbin.T.StringRef, self._campath.ensure_string(new_name)))
        self._campath.instances.append(npath)
        self._campath.top_objects.append(len(self._campath.instances) - 1)
        # aim every direction key at the HQ
        tx, ty, tz = new_hq_pos
        for pi, di in zip(new_pos, new_dir):
            px, py, pz = self._campath.instances[pi].get(coord_p.index).raw
            vx, vy, vz = tx - px, ty - py, tz - pz
            m = math.sqrt(vx * vx + vy * vy + vz * vz) or 1.0
            self._campath.instances[di].set(
                coord_p.index, ndfbin.NdfValue(ndfbin.T.Vector3, (vx / m, vy / m, vz / m)))
        self._campath_dirty = True
        return new_name

    def _add_hq(self, alliance, slot, wx, wy):
        """Clone the scenario's template HQ (item + StartingPoint addon), set AllianceNum/
        AlliancePriority/Position/WarmupCamPath, append to the item list, and create the aimed
        campath. `slot=None` materializes the vanilla FFA-seat encoding (no AlliancePriority
        property at all) — and we also strip an inherited AlliancePriority from the cloned
        template so it doesn't masquerade as a team-mode slot-1 seat. Returns the new item
        index, or None if a template HQ couldn't be obtained (local or corpus)."""
        ilist = self._item_list_value()
        pair = self._obtain_pair("hq")     # local clone OR corpus import (HQ placeable in HQ-less maps)
        if pair is None or ilist is None or self._pndf is None:
            return None
        new_item, new_addon, item_idx, addon_idx = pair
        z = self._nearest_z(wx, wy)
        campath_name = self._next_warmup_name()

        def setp(inst, name, val):
            # create=True: a 1v1 scenario has no AlliancePriority property yet — add it (like the applier).
            self._pndf.set_property(inst, name, val, create=True)

        setp(new_addon, "AllianceNum", ndfbin.NdfValue(ndfbin.T.Int32, int(alliance)))
        if slot is None:
            # FFA seat: drop any AlliancePriority the template carried (so the cloned 1v1 HQ
            # doesn't smuggle priority=1 into our priority=None FFA materialization).
            ap = self._pndf.prop_by_name_and_class("AlliancePriority", new_addon.class_index)
            if ap is not None:
                new_addon.remove(ap.index)
        else:
            setp(new_addon, "AlliancePriority", ndfbin.NdfValue(ndfbin.T.Int32, int(slot)))
        setp(new_addon, "WarmupCamPath",
             ndfbin.NdfValue(ndfbin.T.StringRef, self._pndf.ensure_string(campath_name)))
        setp(new_item, "Position", ndfbin.NdfValue(ndfbin.T.Vector3, (float(wx), float(wy), float(z))))
        ilist.raw.append(ndfbin.NdfValue(
            ndfbin.T.Reference, (ndfbin.OBJ_REF_MARKER, (item_idx, new_item.class_index))))

        self._clone_campath_for_hq(campath_name, (float(wx), float(wy), float(z)))
        self._dirty = True
        return item_idx

    def _set_hq_slot(self, pl, alliance, slot):
        """Re-slot an EXISTING HQ: set its AllianceNum + AlliancePriority (slot=None drops the property =
        FFA seat). Reuses the placed position — so changing modes edits HQs in place instead of leaving
        orphans and adding duplicates."""
        if self._pndf is None:
            return
        addon = self._pndf.instances[pl["addon_idx"]]
        self._pndf.set_property(addon, "AllianceNum", ndfbin.NdfValue(ndfbin.T.Int32, int(alliance)), create=True)
        ap = self._pndf.prop_by_name_and_class("AlliancePriority", addon.class_index)
        if slot is None:
            if ap is not None and addon.get(ap.index) is not None:
                addon.remove(ap.index)
        else:
            self._pndf.set_property(addon, "AlliancePriority",
                                    ndfbin.NdfValue(ndfbin.T.Int32, int(slot)), create=True)
        pl["extra"]["alliance"] = alliance
        pl["extra"]["priority"] = slot
        self._dirty = True

    def _needed_spawns(self, checked):
        """The minimal StartingPoint set {(alliance, slot)} the ticked modes need. Union of each mode's
        spawns, then the soft-cover reduction: an (a,None) FFA seat ALSO serves team slot 1, so drop (a,1)
        when (a,None) is present. (For all-modes this yields exactly the vanilla 16-HQ compassrose layout.)"""
        needed = set()
        for (_k, _l, teams, per, _gt, _gmm, dispo) in checked:
            needed |= self._mode_spawns(teams, per, ffa=(dispo == "DispoMultiFFA"))
        return {(a, s) for (a, s) in needed if not (s == 1 and (a, None) in needed)}

    def _materialize_hqs(self, needed):
        """Cover `needed` by EDITING existing HQs first — keep ones that already satisfy a slot, re-slot
        spares into missing slots — and ADD new HQs only when there aren't enough. The modder shouldn't
        have to hand-place every base, and we never duplicate an HQ we could just re-slot. Returns
        (added, updated)."""
        if self._pndf is None:
            return 0, 0
        hqs = [pl for pl in (self._places or []) if pl["kind"] == "hq"]
        remaining = set(needed)
        used = set()

        def _matches(a, pr, na, ns):
            return a == na and (ns == pr or (ns is None and pr is None) or (ns == 1 and pr is None))
        # 1) keep existing HQs already satisfying a needed slot (one HQ discharges every slot it covers)
        for pl in hqs:
            a, pr = pl["extra"].get("alliance"), pl["extra"].get("priority")
            sat = {sl for sl in remaining if _matches(a, pr, *sl)}
            if sat:
                remaining -= sat
                used.add(id(pl))
        # 2) re-slot SPARE existing HQs into the still-missing slots (edit in place, no new HQ)
        spares = [pl for pl in hqs if id(pl) not in used]
        rem_ord = sorted(remaining, key=lambda p: (p[0], -1 if p[1] is None else p[1]))
        updated = 0
        while rem_ord and spares:
            a, s = rem_ord.pop(0)
            self._set_hq_slot(spares.pop(0), a, s)
            remaining.discard((a, s))
            updated += 1
        # 3) ADD whatever's still missing, spread on a ring around the map centre
        added = 0
        if remaining and self._bbox:
            order = sorted(needed, key=lambda p: (p[0], -1 if p[1] is None else p[1]))
            minx, miny, maxx, maxy = self._bbox
            cx, cy, R = (minx + maxx) / 2.0, (miny + maxy) / 2.0, 0.35 * min(maxx - minx, maxy - miny)
            for i, sl in enumerate(order):
                if sl not in remaining:
                    continue
                ang = -math.pi / 2 + 2 * math.pi * i / max(1, len(order))
                if self._add_hq(sl[0], sl[1], cx + R * math.cos(ang), cy + R * math.sin(ang)) is not None:
                    added += 1
        return added, updated

    # ── scenario binding view (kind-aware panel; replaces the MP-only GAME-MODES assumption) ─────
    def _ensure_bindings(self, rebuild=False):
        """Build {map_dir: [ScenarioBinding]} from the project's registry NDFs (cached per session).
        Best-effort: on any failure leaves an empty dict so the editor degrades to legacy MP behaviour
        (a brand-new custom map with no gameplay dat yet, etc.)."""
        if self._all_bindings is not None and not rebuild:
            return self._all_bindings
        try:
            m_ndf = self.project.get_ndf("gameplay", self._MAPINFO_PATH)
            g_ndf = self.project.get_ndf("gameplay", self._GLOBALS_PATH)
            self._all_bindings = screg.build_bindings(m_ndf, g_ndf, self._dm,
                                                      gd=self._GladProjAdapter(self.project))
        except Exception:
            # Degrade to legacy MP behaviour, but DON'T fail silently — a swallowed error here
            # once hid a total binding loss (all maps 'unbound', friendly names gone) caused by a
            # per-build property re-casing. Log it so the next regression is diagnosable.
            import logging, traceback
            logging.getLogger("map_editor").warning(
                "scenario binding build failed; map/scenario pairings unavailable:\n%s",
                traceback.format_exc())
            self._all_bindings = {}
        return self._all_bindings

    class _GladProjAdapter:
        """A minimal edata-like (.list()/.get()) view over one of the mod project's dats, so engine
        helpers (scenario_registry, scenario_gen) can read clustermaps / registry NDFs / scripts
        through the project. Defaults to the gameplay (glad) dat."""
        def __init__(self, project, dat_key="gameplay"):
            self.project = project
            self.dat_key = dat_key
        def list(self):
            try:
                return self.project.entry_paths(self.dat_key)
            except Exception:
                return []
        def get(self, vpath):
            try:
                return self.project.get_raw(self.dat_key, vpath)
            except Exception:
                return None

    def _current_binding(self):
        """ScenarioBinding for the loaded map/scenario, or None."""
        if not self._all_bindings:
            return None
        scn = self._scn_cb.get()
        for b in self._all_bindings.get(self._sel_map(), []):
            if b.scenario_name == scn and b.has_file:
                return b
        return None

    # ── friendly map names in the Map dropdown (so you pick "Blitz", not "supercrossroads4") ──
    def _loc_blob(self):
        """The localized flash_txt.dic the game shows (English/us), for resolving menu names."""
        try:
            zz = self._GladProjAdapter(self.project, "loc")
            vp = scenario_gen.preferred_flash_dic(zz)
            return self.project.get_raw("loc", vp) if vp else None
        except Exception:
            return None

    def _resolve_desc(self, g, info_idx, blob):
        if info_idx is None or blob is None:
            return None
        v = screg._get_prop(g, g.instances[info_idx], "Description")
        if v is not None and v.type_id == ndfbin.T.LocHash:
            return dicmod.get_entry(blob, bytes(v.raw))
        return None

    def _friendly_map_name(self, map_dir, g, blob):
        """A terrain's friendly name = the localized name of its representative scenario (prefer the MP
        'leveldesign', else any registered one). Falls back to the dir."""
        binds = (self._all_bindings or {}).get(map_dir, [])
        rep = next((b for b in binds if b.scenario_name == "leveldesign" and b.kind == "mp"), None) \
            or next((b for b in binds if b.kind == "mp"), None) \
            or next((b for b in binds if b.kind != "unbound"), None)
        if rep is not None:
            nm = self._resolve_desc(g, rep.info_idx, blob)
            if nm:
                return nm
        return map_dir

    def _build_map_display(self, maps):
        """Build display strings 'Friendly  ·  mapdir' and the display<->dir maps for the dropdown."""
        self._map_dir_by_disp = {}; self._map_disp_by_dir = {}
        try:
            g = self.project.get_ndf("gameplay", self._GLOBALS_PATH); blob = self._loc_blob()
        except Exception:
            g = blob = None
        out = []
        for d in maps:
            friendly = self._friendly_map_name(d, g, blob) if g is not None else d
            disp = ("%s   ·  %s" % (friendly, d)) if friendly != d else d
            self._map_dir_by_disp[disp] = d; self._map_disp_by_dir[d] = disp
            out.append(disp)
        return out

    def _sel_map(self):
        """The selected map DIR (maps the friendly dropdown text back to the raw dir)."""
        cur = self._map_cb.get()
        return getattr(self, "_map_dir_by_disp", {}).get(cur, cur)

    def _set_map(self, map_dir):
        self._map_cb.set(getattr(self, "_map_disp_by_dir", {}).get(map_dir, map_dir))

    def _goto(self, map_dir, scn):
        """Jump the whole editor to (map_dir, scn) -- the ONLY way other tabs should move the
        selection. Setting the two comboboxes and calling _on_scn_change is not enough: everything
        loaded per-MAP (terrain, bbox, road graph, forest zones, the SDB layer and its writeback
        path, the scenario list) would stay on the previous map. That mix is silent, and it saves
        one map's SDB into another map's mapinfo.win. So when the map changes, go through the map
        change; only then pick the scenario.

        Honours the unsaved-changes prompt. Returns True if the editor moved.
        """
        if not self._confirm_discard():
            return False
        # confirmed discard -- clear every flag so the nested handlers don't re-prompt
        self._dirty = self._campath_dirty = self._kdt_dirty = False
        if getattr(self, "_sdb", None):
            self._sdb["dirty"] = False
        if self._sel_map() != map_dir:
            self._set_map(map_dir)
            self._on_map_change(force=True)     # reloads terrain / bbox / roads / SDB / scn list
        if scn and self._scn_cb.get() != scn:
            self._scn_cb.set(scn)
            self._on_scn_change(force=True)
        elif self._sel_map() == map_dir and scn and self._scn_src != (map_dir, scn):
            self._on_scn_change(force=True)
        return True

    def _restore_selection(self):
        """Put both comboboxes back on the map/scenario that is actually LOADED.

        A ttk Combobox commits the new value before <<ComboboxSelected>> fires, so a handler that
        bails out (the user answered "keep editing" to the discard prompt) leaves the widget
        showing a map/scenario that was never loaded. Everything downstream that asks the widget
        "which map am I on?" then gets the wrong answer -- most damagingly the save, which used to
        write the in-memory scenario to the pointed-at path and so dropped one map's scenario into
        another map's folder.
        """
        src = getattr(self, "_scn_src", None)
        if not src:
            return
        map_dir, scn = src
        try:
            if self._sel_map() != map_dir:
                self._set_map(map_dir)
                scns = list_scenarios(self._dm, map_dir)
                self._scn_cb["values"] = scns
                ui_util.fit_combobox(self._scn_cb, minimum=24, maximum=44)
            if self._scn_cb.get() != scn:
                self._scn_cb.set(scn)
        except Exception:
            pass

    _KIND_LABEL = {"mp": "Multiplayer", "campaign": "Campaign",
                   "operation": "Operation", "unbound": "Unbound"}

    def _binding_banner_text(self, b):
        if b is None:
            return ""
        label = t(self._KIND_LABEL.get(b.kind, b.kind))
        tr = ("  ·  " + b.tracking_id) if b.tracking_id else ""
        return "▸ %s%s" % (label, tr)

    def _binding_meta_text(self, b):
        """Read-only registration summary for campaign/operation scenarios."""
        d = b.detail or {}
        lines = []
        if b.kind == "campaign":
            lines.append(t("map.campaign_chapter_tr", tr=b.tracking_id or "?"))
            if d.get("ChapterId") is not None:
                lines.append(t("map.chapterid_c_n_secondary_objective",
                               c=d.get("ChapterId"), n=d.get("NbSecondaryObjectives", 0)))
        elif b.kind == "operation":
            lines.append(t("map.operation_tr", tr=b.tracking_id or "?"))
            lines.append(t("map.difficulty_c_p_player_s",
                           c=d.get("CategoryId", "?"), p=d.get("NbPlayers", "?")))
        lines.append(t("map.lobby_game_modes_don_t"))
        lines.append(t("map.paired_script_s", s=t("map.present") if b.has_script else t("map.none_yet")))
        return "\n".join(str(x) for x in lines)

    def _select_by_info_idx(self, info_idx):
        """Select the scenario owning menu record `info_idx`. True if the selection actually moved.

        Clicking a row on the Menu Entry step comes through here, because the selection belongs to the
        window, not to one step — changing it here is what makes all five steps follow. A record is
        matched against every registration a scenario carries, not just its primary one: one scenario file
        can be listed as an operation AND a multiplayer map, so matching only the primary would fail to
        find rows that are really there.
        """
        bindings = self._ensure_bindings() or {}
        for map_dir, blist in bindings.items():
            for b in blist:
                idxs = {r.get("info_idx") for r in (b.registrations or [])}
                idxs.add(b.info_idx)
                if info_idx in idxs and b.scenario_name:
                    try:
                        return self._goto(map_dir, b.scenario_name)
                    except Exception:
                        return False
        return False

    def _go_to_mission_script(self):
        """Take the user to where a mission's logic is now edited: the Mission Rules & Script step.
        (The old Mission Logic tab is gone — its listing half became the Menu Entry step, its text half
        the Text step, and its generator the New Operation wizard on the top strip.)"""
        self._show_walk_step(4)

    def _open_new_operation(self):
        """The New Operation wizard. It CREATES rather than edits, so it belongs on the top strip beside
        New scenario rather than as a step in the walk."""
        try:
            import new_operation_wizard
        except Exception as e:
            ui_util.error(self, t("neww.title"), str(e))
            return

        def created(map_dir, stem):
            self._all_bindings = None
            try:
                # the wizard just wrote the new scenario into the project; nothing to keep here
                self._dirty = self._campath_dirty = self._kdt_dirty = False
                if getattr(self, "_sdb", None):
                    self._sdb["dirty"] = False
                self._goto(map_dir, stem)
            except Exception:
                pass
            self._show_walk_step(1)      # start the user at the first step of the walk

        new_operation_wizard.open_wizard(self, self.project, self._ensure_bindings() or {},
                                         on_created=created)

    def _update_scenario_panel(self):
        """Set the binding banner and show the right controls for this scenario's KIND:
        multiplayer/unbound → GAME MODES (lobby ticks); campaign/operation → read-only metadata."""
        if not hasattr(self, "_gm_frame"):
            return
        b = self._current_binding()
        self._scn_kind_lbl.config(text=self._binding_banner_text(b))
        if getattr(self, "_names_frame", None) is not None:
            try:
                self._names_frame.set_binding(b)   # the Text tab follows the selection too
            except Exception:
                pass
        if getattr(self, "_chain_frame", None) is not None:
            try:
                self._chain_frame.set_binding(b)   # Load & Files follows the selection too
            except Exception:
                pass
        if getattr(self, "_script_frame", None) is not None:
            try:
                self._script_frame.set_binding(b)  # Mission Rules & Script follows the selection too
            except Exception:
                pass
        if getattr(self, "_menu_frame", None) is not None:
            try:
                self._menu_frame.set_binding(b)    # Menu Entry follows the selection too
            except Exception:
                pass
        is_mp_like = (b is None) or b.kind in ("mp", "unbound")
        if is_mp_like:
            if self._scn_meta_frame.winfo_manager():
                self._scn_meta_frame.pack_forget()
            if not self._gm_frame.winfo_manager():
                self._gm_frame.pack(fill="x", padx=8, pady=(2, 4))
            self._load_game_modes_state()
        else:
            if self._gm_frame.winfo_manager():
                self._gm_frame.pack_forget()
            self._scn_meta_lbl.config(text=self._binding_meta_text(b))
            if not self._scn_meta_frame.winfo_manager():
                self._scn_meta_frame.pack(fill="x", padx=8, pady=(2, 4))

    def _stage_scenario_plan(self, plan):
        """Write a scenario_gen plan into the mod project's dats (never the live game). Plan sections
        map to project dat keys: datamap_add/glad_add/glad_mod/ia_add/zz_add/zz_mod ->
        maps/gameplay/scripts/loc.  `zz_add` carries the per-language in-mission .dic files copied to the
        clone's own scripting folder — miss it and every objective line renders blank."""
        sections = [("datamap_add", "maps"), ("glad_add", "gameplay"), ("glad_mod", "gameplay"),
                    ("ia_add", "scripts"), ("zz_add", "loc"), ("zz_mod", "loc")]
        n = 0
        for sect, dk in sections:
            for vp, b in (plan.get(sect) or {}).items():
                self.project.set_raw(dk, vp, b)
                n += 1
        return n

    def _open_create_scenario_popup(self):
        """Clone the loaded scenario into a NEW scenario of the SAME kind (mp/campaign/operation) and
        register it — the clone-and-tweak path for map / operation / campaign packs. Reuses the source's
        zone geometry + KDT + (campaign/op) mission script; the user then edits placements and saves."""
        b = self._current_binding()
        if self._pndf is None or not self._scn_cb.get() or b is None:
            ui_util.info(self, t("map.can_t_clone"),
                                t("map.load_scenario_s_registered_mp"))
            return
        if b.kind not in ("mp", "campaign", "operation"):
            ui_util.info(self, t("map.unbound_scenario"),
                                t("map.scenario_isn_t_registered_any"))
            return
        map_dir = self._sel_map()
        src_scn = self._scn_cb.get()
        win = ui_util.themed_toplevel(self, t("map.new_scenario_clone"), modal=False, resizable=True)
        kind_lbl = {"mp": t("map.multiplayer"), "campaign": t("map.campaign"), "operation": t("map.operation")}[b.kind]

        def _row(label, default=""):
            fr = tk.Frame(win, background=_R_BG_PANEL); fr.pack(fill="x", padx=10, pady=3)
            tk.Label(fr, text=label, background=_R_BG_PANEL, foreground=_R_TEXT, font=_F_MAIN,
                     width=16, anchor="e").pack(side="left")
            v = tk.StringVar(value=default)
            tk.Entry(fr, textvariable=v, font=_F_MAIN, width=28, background=_R_BG_WIDGET,
                     foreground=_R_TEXT, insertbackground=_R_TEXT, relief="flat").pack(side="left", padx=6)
            return v

        tk.Label(win, text=t("map.clone_map_scn_kind", map=map_dir, scn=src_scn, kind=kind_lbl),
                 background=_R_BG_PANEL, foreground=_R_GOLD_BRT, font=_F_BOLD).pack(anchor="w", padx=10, pady=(10, 4))
        stem_v = _row(t("map.new_scenario_name"), src_scn + "_copy")
        trk_v = _row(t("map.tracking_id"), (b.tracking_id or "") + "_C")
        name_v = _row(t("common.display_name"), (b.name or src_scn) + " (copy)")
        tk.Label(win, text=t("map.reuses_terrain_zones_kdt_campaign"),
                 background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN, wraplength=360,
                 justify="left").pack(anchor="w", padx=10, pady=(4, 6))

        def _create():
            new_scn = stem_v.get().strip()
            if not new_scn:
                ui_util.info(win, t("map.name_needed"), t("map.enter_new_scenario_name")); return
            try:
                gd = self._GladProjAdapter(self.project, "gameplay")
                ia = self._GladProjAdapter(self.project, "scripts")
                zz = self._GladProjAdapter(self.project, "loc")   # flash_txt.dic — sets the menu name
                plan = scenario_gen.generate_scenario(
                    self._dm, gd, map_dir, src_scn, new_scn, name_v.get().strip() or new_scn,
                    trk_v.get().strip(), kind=b.kind, src_folder=b.glad_folder,
                    new_folder=scenario_gen.glad_scenario_folder(new_scn), ia=ia, zz=zz,
                    op_name=(name_v.get().strip() if b.kind in ("operation", "campaign") else None))
                n = self._stage_scenario_plan(plan)
            except Exception as e:
                ui_util.error(win, t("common.create_failed"), str(e)); return
            win.destroy()
            self._all_bindings = None
            ui_util.info(self, t("map.scenario_created"),
                                t("map.staged_n_file_s_map",
                                  n=n, map=map_dir, scn=new_scn, kind=b.kind))
            self._on_map_change(force=True)

        bar = tk.Frame(win, background=_R_BG_PANEL); bar.pack(fill="x", padx=10, pady=(2, 10))
        tk.Button(bar, text=t("map.create"), command=_create, background="#163048",
                  foreground=_R_GOLD_BRT, font=_F_BOLD, relief="flat").pack(side="right")
        tk.Button(bar, text=t("common.cancel"), command=win.destroy, background="#122030",
                  foreground=_R_TEXT, font=_F_BOLD, relief="flat").pack(side="right", padx=6)

    def _load_modes_from_binding(self, b):
        """Tick GAME MODES straight from the scenario's TMultiMapInfo Dispo flags + NbPlayers (the
        registry record) — authoritative, vs. re-deriving from HQ spawns (the old buggy bandaid)."""
        d = b.detail or {}
        nb = d.get("NbPlayers") or 0
        for (key, _l, teams, per, _gt, _gmm, dispo) in _GAME_MODES:
            on = bool(d.get(dispo)) and (teams * per == nb)
            if key == "1v1" and d.get("DispoLadder1v1") and nb == 2:
                on = True
            if key == "2v2" and d.get("DispoLadder2v2") and nb == 4:
                on = True
            self._mode_vars[key].set(on)

    def _stage_lobby_modes(self):
        """Apply the ticked GAME MODES: (1) AUTO-PLACE any HQs the chosen modes need that aren't on the
        map yet (one base per player slot, spread on a ring — the modder can drag them afterwards), then
        (2) stage this map's TMultiMapInfo (the lobby record) so the lobby OFFERS exactly those modes.

        At least one mode must be ticked. After the auto-place offer, if any mode's required HQ spawn set
        is STILL not covered (user declined, no template, or no map bounds) we warn but let them proceed —
        the game starts anyway; uncovered modes just fail to launch when picked."""
        if self._pndf is None:
            ui_util.info(self, t("map.no_scenario"), t("map.load_scenario_first"))
            return
        checked = [m for m in _GAME_MODES if self._mode_vars[m[0]].get()]
        if not checked:
            ui_util.info(self, t("map.no_modes"),
                                t("map.tick_least_one_mode_first"))
            return
        # SET UP the HQs the chosen modes need — the whole point of the mode picker is that the modder
        # shouldn't have to hand-place/fix every base. We EDIT existing HQs (re-slot spares) and ADD only
        # the genuinely missing ones — never duplicating an HQ we could just update.
        needed = self._needed_spawns(checked)

        def _have(a, s):
            return (a, s) in set(self._existing_hq_spawns().keys()) if s is None else (a, s) in self._existing_covers()
        if any(not _have(*sl) for sl in needed) and self._bbox:
            if ui_util.confirm(
                    self,
                    t("map.set_up_hqs"),
                    t("map.selected_modes_need_base_each")):
                added, updated = self._materialize_hqs(needed)
                if added or updated:
                    self._place_edit.set(True)
                    self._rebuild_places()
                    self._link_campaths()
                    self._invalidate_redraw()
                    self._set_status(t("map.hqs_set_up_u_re", u=updated, a=added))
        # Warn only if HQs are STILL unmet (user declined, no template, or no map bounds).
        strict_existing = set(self._existing_hq_spawns().keys())
        soft_existing = self._existing_covers()
        unmet = []
        for (_k, label, teams, per, _gt, _gmm, dispo) in checked:
            req = self._mode_spawns(teams, per, ffa=(dispo == "DispoMultiFFA"))
            covered = strict_existing if dispo == "DispoMultiFFA" else soft_existing
            if req - covered:
                unmet.append(label)
        if unmet:
            ok = ui_util.confirm(
                self,
                t("map.lobby_scenario_mismatch"),
                t("map.these_ticked_modes_still_don", modes=", ".join(unmet)))
            if not ok:
                return
        # Lobby metadata. NbPlayers = the biggest enabled mode. For a SINGLE mode, set its specific
        # GameType/GameModeMulti; for MULTIPLE modes use the permissive multi-mode encoding GameType=None
        # / GameModeMulti=1 (RE'd from vanilla Bagration/Strategists) so the lobby OFFERS every Dispo'd
        # family — a fixed GameType/GMM is what locks a map to just '2 teams'.
        nb = max(m[2] * m[3] for m in checked)
        dispos = {m[6] for m in checked}
        if len(checked) == 1:
            gt, gmm = checked[0][4], checked[0][5]
        else:
            gt, gmm = None, 1
        info = self._sync_multimapinfo(nb, gt, gmm, dispos)
        ui_util.info(
            self,
            t("map.lobby_modes_staged"),
            t("map.lobby_will_offer_modes_info",
              modes=", ".join(t(m[1]) for m in checked), info=info))

    def _sync_multimapinfo(self, nb_players, gametype, gmm, dispo_set):
        """Find this map's TMultiMapInfo (globals.cpp) by GUID via TMapLoadInfo.Path==map dir, and offer
        to set NbPlayers / GameType / GameModeMulti + the Dispo* flags so the lobby OFFERS exactly the
        chosen modes.  The edit is STAGED into the mod project's gameplay dat (written on "Save to mod") —
        it never touches the live game.  Returns a human-readable status string."""
        ALL_DISPO = ["DispoLadder1v1", "DispoLadder2v2", "DispoMulti2Teams", "DispoMulti3Teams",
                     "DispoMulti4Teams", "DispoMultiFFA"]
        map_dir = (self._sel_map() or "").lower()
        target = "NbPlayers=%d  GameType=%s  GameModeMulti=%d  %s" % (
            nb_players, gametype, gmm, "+".join(sorted(d for d in ALL_DISPO if d in dispo_set)) or "(none)")
        try:
            ndfL = self.project.get_ndf("gameplay", self._MAPINFO_PATH)
            ndfG = self.project.get_ndf("gameplay", self._GLOBALS_PATH)
        except Exception as e:
            return t("map.lobby_metadata_not_changed_couldn",
                     e=e, target=target)

        def gprop(ndf, inst, n):
            p = ndf.prop_by_name_and_class(n, inst.class_index)
            return inst.get(p.index) if p else None
        def gguid(ndf, inst):
            v = gprop(ndf, inst, "GUID")
            return bytes(v.raw) if (v and isinstance(v.raw, (bytes, bytearray))) else None
        def gpath(ndf, inst):
            v = gprop(ndf, inst, "Path")
            try:
                return (ndf.resolve_value(v).lower()
                        if v and v.type_id in (ndfbin.T.StringRef, ndfbin.T.PathRef) else None)
            except Exception:
                return None
        clsL = ndfL.class_by_name("TMapLoadInfo")
        guids = {gguid(ndfL, i) for i in ndfL.instances
                 if i.class_index == clsL.index and gpath(ndfL, i) == map_dir}
        guids.discard(None)
        clsM = ndfG.class_by_name("TMultiMapInfo")
        matches = [i for i in ndfG.instances if i.class_index == clsM.index and gguid(ndfG, i) in guids]
        if not matches:
            return t("map.no_tmultimapinfo_found_map_dir",
                     map_dir=map_dir, target=target)
        if not ui_util.confirm(
                self,
                t("map.update_lobby_modes"),
                t("map.set_lobby_metadata_n_tmultimapinfo", n=len(matches), map_dir=map_dir, target=target)):
            return t("map.lobby_metadata_left_unchanged_target", target=target)
        for inst in matches:
            def setp(n, val):
                p = ndfG.prop_by_name_and_class(n, inst.class_index)
                if p is not None:
                    inst.set(p.index, val)
            setp("NbPlayers", ndfbin.NdfValue(ndfbin.T.Int32, int(nb_players)))
            if gametype is None:
                # multi-mode: clear GameType (matches vanilla Bagration/Strategists) so the lobby isn't
                # pinned to one family — the Dispo* flags decide what's offered.
                gp = ndfG.prop_by_name_and_class("GameType", inst.class_index)
                if gp is not None and inst.get(gp.index) is not None:
                    inst.remove(gp.index)
            else:
                setp("GameType", ndfbin.NdfValue(ndfbin.T.Int32, int(gametype)))
            setp("GameModeMulti", ndfbin.NdfValue(ndfbin.T.Int32, int(gmm)))
            # Dispo* availability flags are BOOLEAN (type 0) in the engine data, NOT Int32 — writing them
            # as Int32 makes the game ignore them (that's why only '2 teams' showed). And vanilla marks an
            # unavailable mode by OMITTING the flag, not by 0 — so set True for ticked, remove the rest.
            for f in ALL_DISPO:
                fp = ndfG.prop_by_name_and_class(f, inst.class_index)
                if f in dispo_set:
                    if fp is not None:
                        inst.set(fp.index, ndfbin.NdfValue(ndfbin.T.Bool, True))
                    else:
                        ndfG.set_property(inst, f, ndfbin.NdfValue(ndfbin.T.Bool, True), create=True)
                elif fp is not None and inst.get(fp.index) is not None:
                    inst.remove(fp.index)
        # Offer to auto-move the map to the matching SELECTION-LIST group (the game groups MP by CategoryId).
        # List grouping is purely visual, so this is OPT-IN — and the modder can still place it freely in the
        # Mission Logic tab. When accepted, append it at the END of the target group (DLC/pre-order packs
        # likewise append into the same group, so it slots in after the existing maps).
        tgt_cat = screg.mp_category_for_nbplayers(int(nb_players))

        def _cur_cat(inst):
            p = ndfG.prop_by_name_and_class("CategoryId", inst.class_index)
            v = inst.get(p.index) if p is not None else None
            return v.raw if v is not None else None
        movable = [inst for inst in matches if _cur_cat(inst) != tgt_cat]
        if movable and ui_util.confirm(
                self,
                t("map.move_its_group"),
                t("map.map_now_set_up_nb",
                  nb=nb_players, grp=screg.group_label("mp", tgt_cat))):
            for inst in movable:
                try:
                    ii = ndfG.instances.index(inst)
                    pidx = screg.find_pack_of(ndfG, "mp", ii)
                    if pidx is not None:
                        screg.assign_group(ndfG, "mp", pidx, ii, tgt_cat)
                except Exception:
                    pass
            target += "  →  list group: %s" % screg.group_label("mp", tgt_cat)
        # The shared globals.cpp NDF is mutated in place; flag it dirty so "Save to mod" writes it into
        # the mod project's gameplay dat (the live game is never touched).
        self.project.mark_dirty("gameplay", self._GLOBALS_PATH)
        self._all_bindings = None      # registry changed → rebuild bindings (ticks/banner) on next use
        if self._on_change:
            self._on_change()
        return t("map.lobby_modes_staged_n_tmultimapinfo", n=len(matches), target=target)

    def _read_multimapinfo(self):
        """Read the current map's TMultiMapInfo fields from the mod's gameplay dat (mod folder → backup →
        game) — the first entry whose GUID matches a TMapLoadInfo with Path==this map dir.  Returns a
        dict of the mode fields, or None (no gameplay dat / no entry, e.g. a brand-new custom map).
        Reflects any lobby edits already staged this session (the shared globals.cpp NDF is reused)."""
        try:
            ndfL = self.project.get_ndf("gameplay", self._MAPINFO_PATH)
            ndfG = self.project.get_ndf("gameplay", self._GLOBALS_PATH)
        except Exception:
            return None
        map_dir = (self._sel_map() or "").lower()

        def gprop(ndf, inst, n):
            p = ndf.prop_by_name_and_class(n, inst.class_index)
            return inst.get(p.index) if p else None
        def gguid(ndf, inst):
            v = gprop(ndf, inst, "GUID")
            return bytes(v.raw) if (v and isinstance(v.raw, (bytes, bytearray))) else None
        def gpath(ndf, inst):
            v = gprop(ndf, inst, "Path")
            try:
                return (ndf.resolve_value(v).lower()
                        if v and v.type_id in (ndfbin.T.StringRef, ndfbin.T.PathRef) else None)
            except Exception:
                return None

        clsL = ndfL.class_by_name("TMapLoadInfo")
        guids = {gguid(ndfL, i) for i in ndfL.instances
                 if clsL and i.class_index == clsL.index and gpath(ndfL, i) == map_dir}
        guids.discard(None)
        clsM = ndfG.class_by_name("TMultiMapInfo")
        for inst in (ndfG.instances if clsM else []):
            if inst.class_index == clsM.index and gguid(ndfG, inst) in guids:
                def gi(n):
                    v = gprop(ndfG, inst, n)
                    return v.raw if (v and isinstance(v.raw, int)) else None
                return {n: gi(n) for n in
                        ("NbPlayers", "GameType", "GameModeMulti", "DispoLadder1v1", "DispoLadder2v2",
                         "DispoMulti2Teams", "DispoMulti3Teams", "DispoMulti4Teams", "DispoMultiFFA")}
        return None

    def _load_game_modes_state(self):
        """Pre-tick the GAME MODES checkboxes to reflect what this map's TMultiMapInfo ALREADY offers
        AND what the loaded scenario actually carries — the union, so divergence between lobby
        metadata and scenario reality is visible (the next 'Apply Game Modes' click reconciles).
        A globals-derived mode is 'on' if its Dispo* flag is set and its player count
        (teams·per_team) equals NbPlayers; ranked Ladder1v1/2v2 also tick 1v1/2v2. A
        scenario-derived mode is 'on' if its required spawn pool is a subset of the scenario's
        existing StartingPoints."""
        if not hasattr(self, "_mode_vars"):
            return
        # Prefer the registry binding (authoritative TMultiMapInfo Dispo flags) over re-deriving
        # ticks from HQ spawns — the latter was a buggy bandaid. Fall back to the heuristic only
        # for maps with no registered TMultiMapInfo (brand-new / unbound).
        b = self._current_binding()
        if b is not None and b.kind == "mp" and (b.detail or {}).get("NbPlayers") is not None:
            self._load_modes_from_binding(b)
            return
        info = self._read_multimapinfo()
        strict_existing = set(self._existing_hq_spawns().keys())
        soft_existing = self._existing_covers()       # team-mode (a,1) also covered by (a,None)
        if info is None and not strict_existing:
            for k in self._mode_vars:
                self._mode_vars[k].set(False)
            return
        nb = (info or {}).get("NbPlayers") or 0
        for (key, _l, teams, per, _gt, _gmm, dispo) in _GAME_MODES:
            on = info is not None and bool(info.get(dispo)) and (teams * per == nb)
            if info is not None and key == "1v1" and info.get("DispoLadder1v1") and nb == 2:
                on = True
            if info is not None and key == "2v2" and info.get("DispoLadder2v2") and nb == 4:
                on = True
            # OR-in the scenario-derived tick: the .scenario already carries the spawn pool.
            # FFA requirements use strict existence (priority must really be None); team
            # requirements use soft (an existing (a, None) covers the (a, 1) slot).
            if strict_existing:
                ffa = (dispo == "DispoMultiFFA")
                required = self._mode_spawns(teams, per, ffa=ffa)
                covered = strict_existing if ffa else soft_existing
                if required and required <= covered:
                    on = True
            self._mode_vars[key].set(on)

    def _suggested_pyclasses(self, kind):
        """Distinct PythonClassName strings observed in the current scenario, sorted, optionally
        narrowed to a sub-kind (depot/unit/building/spawn). Used by the Add Placement popup to
        seed its PyClass picker — users can also type a custom string."""
        seen = set()
        for pl in self._places:
            py = pl["extra"].get("pyclass")
            if not py:
                continue
            if kind in ("depot", "unit", "building", "spawn") and _spawn_kind(py) != kind:
                continue
            seen.add(py)
        return sorted(seen)

    def _create_placement(self, kind, pos, py_class=None, fields=None):
        """Author a brand-new TGameDesignItem of `kind` into the current scenario.

        Cloning-first strategy: if the scenario already has at least one placement of this kind,
        we deep-copy its (item, AddOn) instances and overwrite Position + the AddOn ref + any
        kind-specific fields supplied via `fields`. That preserves all the verbatim defaults
        (default props, ObjRef edge cases) which is far safer than building NDF instances from
        scratch when we don't yet have a tested constructor for every AddOn class.

        Returns the new item_idx, or None on failure (with a messagebox already shown).
        For kind='hq' we delegate to `_add_hq` so the warmup-campath clone + FFA-seat encoding
        stay in one place. `fields` for HQ is `{"alliance": int, "slot": int|None}`."""
        if self._pndf is None:
            ui_util.info(self, t("map.no_scenario"), t("map.load_scenario_first"))
            return None

        fields = fields or {}
        wx, wy, wz = pos

        if kind == "hq":
            alliance = int(fields.get("alliance", 1))
            slot = fields.get("slot")
            item_idx = self._add_hq(alliance, slot, wx, wy)
            if item_idx is None:
                ui_util.info(self, t("map.couldn_t_add_hq"),
                                    t("map.couldn_t_obtain_hq_template"))
                return None
            self._place_edit.set(True)
            self._rebuild_places(select_item_idx=item_idx)
            # Link the cloned warmup campath so the new HQ's 'cam' marker + campath_keys
            # show in the detail panel and on the canvas — matches what the old
            # _apply_game_modes button did once per add-pass.
            self._link_campaths()
            self._invalidate_redraw()
            self._set_status(t("map.added_hq_s_drag_then",
                               a=alliance, s="*" if slot is None else slot))
            return item_idx

        # All other kinds: get a fresh (item, addon) pair — clone a local template, or import one from
        # the corpus when this scenario has none of `kind` (so e.g. depots/buildings/zones are placeable
        # in a bare scenario without "load another map and re-save to seed it" first).
        ilist = self._item_list_value()
        if ilist is None:
            ui_util.error(self, t("map.add_failed"), t("map.couldn_t_find_tgamedesignitemlist"))
            return None
        pair = self._obtain_pair(kind)
        if pair is None:
            addon_cls = _KIND_TO_ADDON.get(kind, "?")
            ui_util.info(self, t("map.couldn_t_add"),
                                t("map.no_template_kind_found_locally", kind=kind, cls=addon_cls))
            return None
        new_item, new_addon, item_idx, addon_idx = pair

        def setp(inst, name, val):
            self._pndf.set_property(inst, name, val, create=True)

        # Kind-specific field overrides (everything the popup lets the user set up-front).
        if kind in ("depot", "unit", "building", "spawn"):
            if py_class:
                setp(new_addon, "PythonClassName",
                     ndfbin.NdfValue(ndfbin.T.StringRef, self._pndf.ensure_string(py_class)))
            if "camp" in fields:
                setp(new_addon, "Camp", ndfbin.NdfValue(ndfbin.T.Int32, int(fields["camp"])))
            if kind == "depot" and "champ" in fields:
                setp(new_addon, "ChampInteger", ndfbin.NdfValue(ndfbin.T.Int32, int(fields["champ"])))
        elif kind in ("ville", "montagne") and "text" in fields:
            setp(new_addon, "ChampTexte", ndfbin.NdfValue(ndfbin.T.WideStr, str(fields["text"])))
        elif kind == "circle" and "radius" in fields:
            setp(new_addon, "Radius", ndfbin.NdfValue(ndfbin.T.Float32, float(fields["radius"])))
        elif kind == "rect":
            if "w" in fields:
                setp(new_addon, "Width", ndfbin.NdfValue(ndfbin.T.Float32, float(fields["w"])))
            if "h" in fields:
                setp(new_addon, "Height", ndfbin.NdfValue(ndfbin.T.Float32, float(fields["h"])))

        setp(new_item, "Position", ndfbin.NdfValue(ndfbin.T.Vector3, (float(wx), float(wy), float(wz))))
        ilist.raw.append(ndfbin.NdfValue(ndfbin.T.Reference,
                         (ndfbin.OBJ_REF_MARKER, (item_idx, new_item.class_index))))
        self._dirty = True
        self._place_edit.set(True)
        self._rebuild_places(select_item_idx=item_idx)
        self._set_status(t("map.added_kind_view_centre_drag",
                           kind=kind))
        return item_idx

    def _open_add_placement_popup(self):
        """Modal popup that lets the user create any kind of placement. Picks the kind, picks
        a PythonClassName for Spawn-derived kinds (with a filterable listbox seeded from the
        current scenario, plus free-text input), fills kind-specific fields, then Create →
        _create_placement(...). Position defaults to the canvas centre."""
        if self._pndf is None:
            ui_util.info(self, t("map.no_scenario"), t("map.load_scenario_first"))
            return
        cw = max(self._canvas.winfo_width(), 50); ch = max(self._canvas.winfo_height(), 50)
        wx, wy = self._screen_to_world(cw / 2, ch / 2)
        wz = self._nearest_z(wx, wy)

        win = ui_util.themed_toplevel(self, t("map.add_placement"), size=(560, 660), resizable=True)

        kind_var = tk.StringVar(value="depot")
        py_var = tk.StringVar(value=_SPAWN_DEPOT_PY)
        # Per-kind field vars (only the ones relevant to the selected kind are shown). Camp
        # defaults to -1 (visible-neutral) — the typical MP-depot author intent. Leaving the
        # entry blank means "no Camp property" (null = Team 1, or despawn for MP depots).
        camp_var = tk.StringVar(value="-1")
        champ_var = tk.StringVar(value="25")
        alli_var = tk.StringVar(value="1"); pri_var = tk.StringVar(value="1")
        ffa_var = tk.BooleanVar(value=False)
        text_var = tk.StringVar(value="")
        radius_var = tk.StringVar(value="50000")
        w_var = tk.StringVar(value="50000"); h_var = tk.StringVar(value="50000")
        x_var = tk.StringVar(value=f"{wx:.0f}"); y_var = tk.StringVar(value=f"{wy:.0f}")

        # ── kind selector ────────────────────────────────────────────────────────
        tk.Label(win, text=t("map.kind"), background=_R_BG_PANEL, foreground=_R_GOLD,
                 font=_F_BOLD).pack(anchor="w", padx=8, pady=(8, 2))
        kg = tk.Frame(win, background=_R_BG_PANEL); kg.pack(anchor="w", padx=8)
        KINDS = [("depot", t("map.depot")), ("unit", t("map.unit")), ("building", t("map.building")),
                 ("spawn", t("map.spawn_other")), ("hq", t("map.player_start")),
                 ("ville", t("map.city_label")), ("montagne", t("map.mountain_label")),
                 ("name", t("map.named_point")), ("circle", t("map.circular_zone")),
                 ("rect", t("map.rect_zone"))]
        for i, (k, label) in enumerate(KINDS):
            tk.Radiobutton(kg, text=label, variable=kind_var, value=k,
                           background=_R_BG_PANEL, foreground=_R_TEXT, selectcolor=_R_BG_WIDGET,
                           font=_F_MAIN, activebackground=_R_BG_PANEL, activeforeground=_R_GOLD,
                           command=lambda: _refresh_fields()
                           ).grid(row=i // 5, column=i % 5, sticky="w", padx=(0, 8))
        kind_note = tk.Label(win, text="", background=_R_BG_PANEL, foreground=_R_TEXT_DIM,
                             font=_F_MAIN, justify="left", wraplength=520, anchor="w")
        kind_note.pack(anchor="w", padx=8, pady=(4, 0))

        # ── ENTITY-CLASS picker — the COMPREHENSIVE roster (placement_catalog), shown for Spawn-derived
        #    kinds. Lists EVERY unit/building/plane the game defines — not just what's in this scenario —
        #    including the ~277 classes no shipped scenario ever placed but which are valid to add.
        cat_var = tk.StringVar(value="(all)")
        show_var = tk.StringVar(value="all")
        py_frame = tk.Frame(win, background=_R_BG_PANEL)
        tk.Label(py_frame, text=t("map.entity_class_full_game_roster"), background=_R_BG_PANEL,
                 foreground=_R_GOLD, font=_F_BOLD).pack(anchor="w", pady=(8, 2))
        filt_row = tk.Frame(py_frame, background=_R_BG_PANEL); filt_row.pack(fill="x")
        cat_cb = ttk.Combobox(filt_row, textvariable=cat_var, state="readonly", width=10, font=_F_MAIN,
                              values=["(all)"] + sorted(pcat.categories().keys()))
        cat_cb.pack(side="left")
        for lbl, val in ((t("map.all"), "all"), (t("map.placed"), "placed"), (t("map.unused"), "unused")):
            tk.Radiobutton(filt_row, text=lbl, variable=show_var, value=val, background=_R_BG_PANEL,
                           foreground=_R_TEXT, selectcolor=_R_BG_WIDGET, font=_F_MAIN,
                           activebackground=_R_BG_PANEL, activeforeground=_R_GOLD,
                           command=lambda: _populate_pylist()).pack(side="left", padx=(6, 0))
        py_search = tk.Entry(py_frame, background=_R_BG_WIDGET, foreground=_R_TEXT, font=_F_MAIN,
                             insertbackground=_R_TEXT, relief="flat", highlightthickness=0)
        py_search.pack(fill="x", pady=(2, 2))
        pylb_frame = tk.Frame(py_frame, background=_R_BG_PANEL); pylb_frame.pack(fill="both", expand=True)
        pysb = tk.Scrollbar(pylb_frame, orient="vertical")
        pysb.pack(side="right", fill="y")
        py_list = tk.Listbox(pylb_frame, background=_R_BG_WIDGET, foreground=_R_TEXT,
                             selectbackground=_R_SEL_BG, selectforeground=_R_GOLD_BRT,
                             font=_F_MAIN, highlightthickness=0, borderwidth=0,
                             activestyle="none", exportselection=False, height=8,
                             yscrollcommand=pysb.set)
        py_list.pack(side="left", fill="both", expand=True)
        pysb.config(command=py_list.yview)
        count_lbl = tk.Label(py_frame, text="", background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN)
        count_lbl.pack(anchor="w")
        usage_lbl = tk.Label(py_frame, text="", background=_R_BG_PANEL, foreground="#a0c0e0", font=_F_MAIN,
                             anchor="w", justify="left", wraplength=460)
        usage_lbl.pack(anchor="w")
        tk.Label(py_frame, text=t("map.pythonclassname_write"), background=_R_BG_PANEL,
                 foreground=_R_TEXT_DIM, font=_F_MAIN).pack(anchor="w", pady=(4, 0))
        tk.Entry(py_frame, textvariable=py_var, background=_R_BG_WIDGET, foreground=_R_TEXT, font=_F_MAIN,
                 insertbackground=_R_TEXT, relief="flat", highlightthickness=0).pack(fill="x")
        self._pcat_names = []

        def _populate_pylist():
            py_list.delete(0, "end"); self._pcat_names = []
            if kind_var.get() == "depot":
                py_list.insert("end", _SPAWN_DEPOT_PY); self._pcat_names.append(_SPAWN_DEPOT_PY)
                py_var.set(_SPAWN_DEPOT_PY); count_lbl.config(text=t("map.1_depot_class")); usage_lbl.config(text="")
                return
            category = None if cat_var.get() == "(all)" else cat_var.get()
            placed = {"all": None, "placed": True, "unused": False}[show_var.get()]
            rows = pcat.search(category=category, placed=placed, query=py_search.get().strip(), limit=3000)
            for name, info in rows:
                n = info.get("used_count") or 0
                tag = (t("map.used_n_x", n=n) if info.get("ever_placed") else t("map.never_placed_valid"))
                py_list.insert("end", "%-42s %s" % (name, tag)); self._pcat_names.append(name)
            count_lbl.config(text=t("map.n_classes_tot_game",
                                    n=len(rows), tot=pcat.load().get("catalog_size", "?")))
            if self._pcat_names and not py_var.get():
                py_var.set(pcat.placement_string(self._pcat_names[0]))

        def _on_py_pick(_=None):
            cur = py_list.curselection()
            if not cur or cur[0] >= len(self._pcat_names):
                return
            name = self._pcat_names[cur[0]]
            py_var.set(pcat.placement_string(name))
            info = pcat.info_for(name) or {}
            if info.get("ever_placed"):
                ex = ", ".join((info.get("example_scenarios") or [])[:4])
                usage_lbl.config(text=t("map.placed_n_x_e_g", n=info.get("used_count", 0), ex=ex))
            else:
                usage_lbl.config(text=t("map.never_placed_shipped_scenario_but"))

        py_search.bind("<KeyRelease>", lambda _: _populate_pylist())
        cat_cb.bind("<<ComboboxSelected>>", lambda _: _populate_pylist())
        py_list.bind("<<ListboxSelect>>", _on_py_pick)

        # ── kind-specific field frame (re-packed when kind changes) ──────────────
        kf = tk.Frame(win, background=_R_BG_PANEL)

        def _row(parent, label, var, width=8):
            r = tk.Frame(parent, background=_R_BG_PANEL); r.pack(anchor="w", pady=(2, 0))
            tk.Label(r, text=label, background=_R_BG_PANEL, foreground=_R_TEXT,
                     font=_F_MAIN, width=14, anchor="w").pack(side="left")
            tk.Entry(r, textvariable=var, width=width, background=_R_BG_WIDGET, foreground=_R_TEXT,
                     font=_F_MAIN, insertbackground=_R_TEXT, relief="flat",
                     highlightthickness=0).pack(side="left")
            return r

        def _refresh_fields():
            for w in kf.winfo_children():
                w.destroy()
            kind = kind_var.get()
            # What this kind IS, from the schema — the same note the Details panel shows. Surfaced
            # here because the confusion happens at CREATION: "HQ" reads as "the HQ building", when
            # it is the player START and the real HQ buildings live under Building.
            _sch = pschema.schema_for(kind)
            kind_note.config(text=(_sch.note if _sch and _sch.note else ""))
            # Show/hide the PyClass picker.
            if kind in ("depot", "unit", "building", "spawn"):
                if not py_frame.winfo_ismapped():
                    py_frame.pack(fill="both", expand=False, padx=8, pady=(2, 0))
                # Pre-filter the catalog to the kind's category and reset the pick so the default re-picks.
                if kind == "depot":
                    py_var.set(_SPAWN_DEPOT_PY)
                elif kind == "building":
                    cat_var.set("building"); py_var.set("")
                elif kind == "unit":
                    cat_var.set("unit"); py_var.set("")
                else:  # spawn (other) — show everything
                    cat_var.set("(all)")
                _populate_pylist()
            else:
                if py_frame.winfo_ismapped():
                    py_frame.pack_forget()
            # Kind-specific scalar fields.
            tk.Label(kf, text=t("map.fields"), background=_R_BG_PANEL, foreground=_R_GOLD,
                     font=_F_BOLD).pack(anchor="w", pady=(6, 2))
            if kind in ("depot", "unit", "building", "spawn"):
                _row(kf, t("map.camp_int"), camp_var)
                tk.Label(kf,
                         text=t("map.blank_no_camp_prop_team"),
                         background=_R_BG_PANEL, foreground=_R_TEXT_DIM,
                         font=_F_MAIN, justify="left", wraplength=440).pack(anchor="w", padx=14)
                if kind == "depot":
                    _row(kf, t("map.champinteger"), champ_var)
                    tk.Label(kf, text=t("map.supply_9_champinteger"),
                             background=_R_BG_PANEL, foreground=_R_TEXT_DIM,
                             font=_F_MAIN).pack(anchor="w", padx=14)
            elif kind == "hq":
                _row(kf, t("map.alliancenum"), alli_var, width=6)
                _row(kf, t("map.alliancepriority"), pri_var, width=6)
                fr = tk.Frame(kf, background=_R_BG_PANEL); fr.pack(anchor="w", pady=(2, 0))
                tk.Checkbutton(fr, text=t("map.ffa_seat_no_alliancepriority_property"),
                               variable=ffa_var,
                               background=_R_BG_PANEL, foreground=_R_TEXT, selectcolor=_R_BG_WIDGET,
                               font=_F_MAIN, activebackground=_R_BG_PANEL,
                               activeforeground=_R_GOLD).pack(side="left")
            elif kind in ("ville", "montagne"):
                _row(kf, t("map.champtexte"), text_var, width=30)
            elif kind == "circle":
                _row(kf, t("map.radius"), radius_var)
            elif kind == "rect":
                _row(kf, t("map.width"), w_var); _row(kf, t("map.height"), h_var)
            # Position fields (always shown).
            tk.Label(kf, text=t("map.position"), background=_R_BG_PANEL, foreground=_R_GOLD,
                     font=_F_BOLD).pack(anchor="w", pady=(6, 2))
            _row(kf, t("map.x"), x_var); _row(kf, t("map.y"), y_var)
            tk.Label(kf, text=t("map.z_defaults_terrain_near_x"),
                     background=_R_BG_PANEL, foreground=_R_TEXT_DIM,
                     font=_F_MAIN).pack(anchor="w", padx=14)

        kf.pack(fill="both", expand=True, padx=8)
        _refresh_fields()

        # ── action buttons ───────────────────────────────────────────────────────
        bar = tk.Frame(win, background=_R_BG_PANEL); bar.pack(fill="x", padx=8, pady=8)

        def _do_create():
            kind = kind_var.get()
            try:
                wx_ = float(x_var.get()); wy_ = float(y_var.get())
            except ValueError:
                ui_util.error(win, t("map.bad_position"), t("map.x_y_must_numbers"))
                return
            wz_ = self._nearest_z(wx_, wy_)
            fields = {}
            py = None
            if kind in ("depot", "unit", "building", "spawn"):
                py = py_var.get().strip() or None
                if not py:
                    ui_util.error(win, t("map.need_pyclass"),
                                         t("map.pick_type_pythonclassname"))
                    return
                camp_s = camp_var.get().strip()
                if camp_s:
                    try:
                        fields["camp"] = int(camp_s)
                    except ValueError:
                        ui_util.error(win, t("map.bad_camp"),
                                             t("map.camp_must_integer_blank_team"))
                        return
                # else: leave fields["camp"] absent → _create_placement skips the Camp property,
                # matching the null encoding the engine reads as Team 1 / MP-depot despawn.
                if kind == "depot":
                    try:
                        fields["champ"] = int(champ_var.get())
                    except ValueError:
                        ui_util.error(win, t("map.bad_champinteger"),
                                             t("map.champinteger_must_integer"))
                        return
            elif kind == "hq":
                try:
                    fields["alliance"] = int(alli_var.get())
                    fields["slot"] = None if ffa_var.get() else int(pri_var.get())
                except ValueError:
                    ui_util.error(win, t("map.bad_hq_fields"),
                                         t("map.alliancenum_alliancepriority_must_intege"))
                    return
            elif kind in ("ville", "montagne"):
                fields["text"] = text_var.get()
            elif kind == "circle":
                try: fields["radius"] = float(radius_var.get())
                except ValueError:
                    ui_util.error(win, t("map.bad_radius"), t("map.radius_must_number")); return
            elif kind == "rect":
                try:
                    fields["w"] = float(w_var.get()); fields["h"] = float(h_var.get())
                except ValueError:
                    ui_util.error(win, t("map.bad_size"), t("map.width_height_must_numbers")); return
            item_idx = self._create_placement(kind, (wx_, wy_, wz_), py_class=py, fields=fields)
            if item_idx is not None:
                win.destroy()

        tk.Button(bar, text=t("map.create"), command=_do_create, background="#163048",
                  foreground=_R_GOLD_BRT, font=_F_BOLD, relief="flat").pack(side="left")
        tk.Button(bar, text=t("common.cancel"), command=win.destroy, background="#122030",
                  foreground=_R_TEXT, font=_F_BOLD, relief="flat").pack(side="left", padx=4)

    # ── Script editor (Step 4) ───────────────────────────────────────────────────
    def _script_buildid_prefix(self):
        """Discover the `genpython\\<buildid>\\` prefix this install's IA_Common.dat uses.
        Public 190852 uses `10000`, compat 1360 (e.g. Endless Defence mod) uses `100000` —
        we peek at one existing entry instead of hardcoding either. Returns `10000` as a
        last-resort default (matches the public build the editor targets per memory's
        target-version policy)."""
        try:
            paths = self.project.entry_paths("scripts", suffix=".xyz")
        except Exception:
            paths = []
        for p in paths:
            parts = p.split("\\")
            if len(parts) >= 2 and parts[0] == "genpython" and parts[1].isdigit():
                return parts[1]
        return "10000"

    def _script_path_for_scn(self, map_dir, scn_name):
        """Virtual path of the paired .xyz script inside IA_Common.dat.

        Convention (observed in the live build + the Endless Defence mod):
          leveldesign.scenario              → scripting/effetmap.xyz
          leveldesign_<suffix>.scenario     → scripting_<suffix>/effetmap.xyz
        So the suffix is whatever follows the `leveldesign` prefix in the .scenario name,
        appended to `scripting` verbatim (the leading underscore is kept). The buildid
        between `genpython\\` and the map subtree differs by install — discovered live."""
        suffix = scn_name[len("leveldesign"):] if scn_name.startswith("leveldesign") else ""
        return "genpython\\%s\\test\\map\\%s\\scripting%s\\effetmap.xyz" % (
            self._script_buildid_prefix(), map_dir, suffix)


    def _delete_selected_place(self):
        pi = self._place_sel
        if pi is None or self._pndf is None or pi >= len(self._places):
            ui_util.info(self, t("map.nothing_selected"), t("map.select_placement_delete_first"))
            return
        pl = self._places[pi]
        if not ui_util.confirm(self, t("map.delete_placement"),
                                   t("map.delete_kind_label", kind=pl['kind'], label=pl['label'])):
            return
        removed = {pl["item_idx"]}
        if pl["addon_idx"] is not None:
            removed.add(pl["addon_idx"])
        # 1. drop the item's reference from the item list
        ilist = self._item_list_value()
        if ilist is not None:
            ilist.raw[:] = [r for r in ilist.raw if not self._is_objref_to(r, pl["item_idx"])]
        # 2. old->new index map, 3. remap every obj ref + top_objects
        import bisect
        removed_asc = sorted(removed)
        newidx = lambda i: i - bisect.bisect_left(removed_asc, i)   # noqa: E731
        for inst in self._pndf.instances:
            for pv in inst.props:
                self._remap_refs(pv.value, removed, newidx)
        self._pndf.top_objects = [newidx(i) for i in self._pndf.top_objects if i not in removed]
        # 4. delete the instances (descending so indices stay valid)
        for i in sorted(removed, reverse=True):
            del self._pndf.instances[i]
        self._dirty = True
        self._rebuild_places(select_item_idx=None)

    def _press_start(self, e):
        self._moved = False
        if self._conceal_edit.get() and self._sdb:          # concealment paint tool takes the drag
            self._painting = True
            self._press = (e.x, e.y, self._ox, self._oy)
            self._conceal_paint_at(e.x, e.y)
            self._conceal_refresh_overlay()                 # SDB-only refresh (cheap numpy raster)
            return
        self._painting = False
        self._drag_kdt_vi = None
        self._drag_cam = self._cam_at(e.x, e.y) if self._place_edit.get() else None
        self._drag_place = (self._place_at(e.x, e.y)
                            if (self._drag_cam is None and self._place_edit.get()) else None)
        # KDT border-vertex drag (its own edit mode) takes priority over zone-cluster handles
        if self._drag_cam is None and self._drag_place is None and self._kdt_edit.get():
            self._drag_kdt_vi = self._kdt_handle_at(e.x, e.y)
        self._drag_cluster = (None if (self._drag_cam is not None or self._drag_place is not None
                                       or self._drag_kdt_vi is not None)
                              else self._handle_at(e.x, e.y))
        self._press = (e.x, e.y, self._ox, self._oy)
        # a sector-cluster / KDT-vertex drag freezes the heavy vector overlay for the whole stroke
        self._dragging = (self._drag_kdt_vi is not None or self._drag_cluster is not None)
        if self._drag_cam is not None or self._drag_place is not None:
            return
        if self._drag_kdt_vi is not None:
            self._drag_kdt_p0 = self._kdt_world[self._drag_kdt_vi]
            self._drag_kdt_orig = [tuple(v) for v in self._kdt_world]
            return
        if self._drag_cluster is not None:
            self._drag_p0 = self._cluster_pos(self._clusters[self._drag_cluster])
            self._drag_orig = {zi: [tuple(v) for v in z["verts"]]
                               for zi, z in enumerate(self._zones)}
            if self._kdt_follow.get() and self._kdt_border_bind is None:
                self._kdt_embed_in_scenario()       # bind KDT borders to the pre-edit sector borders

    def _press_move(self, e):
        if self._press is None:
            return
        if self._painting:                                  # concealment paint drag
            import time
            self._conceal_paint_at(e.x, e.y)
            now = time.monotonic()
            if now - getattr(self, "_last_conceal_ref", 0) > 0.12:  # throttle overlay regen
                self._last_conceal_ref = now
                self._conceal_refresh_overlay()             # SDB-only (cheap); no vector rebuild
            return
        x0, y0, ox0, oy0 = self._press
        if abs(e.x - x0) + abs(e.y - y0) > 3:
            self._moved = True
        if self._drag_cam is not None:                      # drag an HQ's camera marker (independent)
            self._set_camera(self._places[self._drag_cam], *self._screen_to_world(e.x, e.y))
            self._redraw()                                  # placements are live items — no rebuild
        elif self._drag_place is not None:                  # drag a placement marker
            pl = self._places[self._drag_place]
            wx, wy = self._screen_to_world(e.x, e.y)
            wx, wy = self._snap_to_road(pl, wx, wy)          # depots/HQ snap to the road-offset (if on)
            self._place_set_pos(pl, wx, wy)
            self._redraw()
        elif self._drag_kdt_vi is not None:                 # drag a KDT sector border vertex
            self._kdt_move(*self._screen_to_world(e.x, e.y))
            self._recompose()                               # vec overlay frozen; handles drawn live
        elif self._drag_cluster is not None:                # drag a boundary node (ring-hooked + KDT follows)
            self._sector_node_move(*self._screen_to_world(e.x, e.y))
            self._recompose()                               # vec overlay frozen; outline drawn live
        else:                                               # pan
            nox = ox0 + (e.x - x0)
            noy = oy0 + (e.y - y0)
            dx, dy = nox - self._ox, noy - self._oy
            self._ox, self._oy = nox, noy
            self._panning = True
            if not dx and not dy:
                return
            # A pan translates EVERY drawn item by the same screen offset — the terrain image, the
            # markers, the handles, all of them. So the correct new frame is the current one moved
            # by (dx, dy), and Tk can do that natively in one call. Redrawing instead (delete all +
            # re-create every item) cost ~43 ms a frame here, which is why the map lagged the cursor
            # and then jumped to catch up.
            #
            # It stays valid only while the exposed edge is still covered: the composite carries a
            # margin and the markers are culled with a matching pad, so once the accumulated slide
            # runs past that budget we fall back to a real redraw (which resets the accumulator).
            budget = self._pan_slide_budget()
            if (abs(self._pan_slid[0] + dx) <= budget and abs(self._pan_slid[1] + dy) <= budget
                    and self._comp_tk is not None and not self._comp_is_preview):
                self._pan_slid = (self._pan_slid[0] + dx, self._pan_slid[1] + dy)
                self._canvas.move("all", dx, dy)
            else:
                self._schedule_redraw()                      # coalesce the motion flood (perf)

    def _press_release(self, e):
        if self._painting:                                  # finish a concealment paint stroke
            self._painting = False
            self._conceal_refresh_overlay()                 # final SDB-only refresh
            self._press = None
            return
        if self._drag_cam is not None:
            self._select_place(self._drag_cam)
            self._drag_cam = None
        elif self._drag_place is not None:
            self._select_place(self._drag_place)
            self._drag_place = None
        elif self._drag_kdt_vi is not None:
            self._drag_kdt_vi = None
            self._drag_kdt_orig = self._drag_kdt_p0 = None
            self._dragging = False
            self._invalidate_vec()              # stroke done: rebuild the vector overlay once
        elif self._drag_cluster is not None:
            self._drag_cluster = None
            self._drag_orig = self._drag_p0 = None
            self._dragging = False
            self._invalidate_vec()              # stroke done: rebuild the vector overlay once
        elif self._press and not self._moved and self._pil and self._bbox:
            # click selects a placement under the cursor; sector selection is via the left list ONLY
            pi = self._place_at(e.x, e.y)
            if pi is not None:
                self._select_place(pi)
        self._press = None
        if self._panning:                       # pan finished — refresh the region we slid into
            self._panning = False
            self._recompose()

    def _soft_move(self, wx, wy):
        """Move the grabbed corner to (wx,wy); nearby vertices follow with a smooth falloff over
        `soft_radius`, so the secondary border + fill track the boundary.  Coincident verts (same
        snapshot pos) get identical weight -> welds across zones stay intact automatically."""
        if self._drag_orig is None or self._drag_p0 is None:
            return
        p0x, p0y = self._drag_p0
        dx, dy = wx - p0x, wy - p0y
        try:
            R = float(self._soft_radius.get() or 0)
        except ValueError:
            R = 0.0
        R = max(R, 1.0)
        rin = R * 0.5
        for zi, orig in self._drag_orig.items():
            z = self._zones[zi]
            sz = self._scn["zones"][z["scn_i"]]
            for vi, (ox, oy) in enumerate(orig):
                d = math.hypot(ox - p0x, oy - p0y)
                if d >= R:
                    continue                                # outside influence: leave at snapshot
                if d <= rin:
                    w = 1.0
                else:
                    t = (d - rin) / (R - rin)
                    w = 1.0 - t * t * (3.0 - 2.0 * t)       # smoothstep falloff
                nx, ny = ox + dx * w, oy + dy * w
                z["verts"][vi] = (nx, ny)
                old = sz["vertices"][vi]
                sz["vertices"][vi] = (nx, ny, old[2], old[3], old[4])
        self._dirty = True

    _ZOOM_STEP = 1.25          # per notch; 1.1 needed a lot of spinning to get anywhere
    _ZOOM_SETTLE_MS = 110      # quiet time after the last notch before the sharp render
    # How far the preview may magnify what it already has. There is no cap worth having: the only
    # alternative to a blocky preview is a frame drawn at the wrong SCALE (which slides the map) or
    # a mid-burst rebuild (which stalls it), and both are worse than a coarse picture that lives
    # ~100 ms before the sharp render replaces it. Kept as a named constant so the intent is
    # explicit rather than implied by its absence.
    _PREVIEW_MAG = float("inf")

    def _wheel(self, e):
        """Zoom about the cursor.

        The step is exponential in WHEEL NOTCHES (delta/120) rather than a fixed multiply per event,
        so a high-resolution wheel or a trackpad that sends many small deltas zooms by the same amount
        per physical movement as a notched wheel — that proportionality is what makes it feel steady
        instead of lurching.

        The exact re-render costs ~70 ms, so doing it per notch made a zoom burst crawl. Instead the
        view transform is applied immediately and drawn by rescaling the composite we already have
        (_zoom_preview, a few ms), with the sharp rebuild once the wheel goes quiet."""
        if not self._pil:
            return
        notches = (e.delta / 120.0) if e.delta else (1.0 if getattr(e, "num", 5) == 4 else -1.0)
        factor = self._ZOOM_STEP ** max(-4.0, min(4.0, notches))
        bx = (e.x - self._ox) / self._scale
        by = (e.y - self._oy) / self._scale
        self._scale *= factor
        self._ox = e.x - bx * self._scale
        self._oy = e.y - by * self._scale
        self._zooming = True
        if self._zoom_after is not None:
            try:
                self.after_cancel(self._zoom_after)
            except Exception:
                pass
        self._zoom_after = self.after(self._ZOOM_SETTLE_MS, self._zoom_settled)
        self._recompose()           # zoom is a view change only — reuse both cached overlays

    def _zoom_settled(self):
        """Wheel has gone quiet — replace the rescaled preview with the exact render."""
        self._zoom_after = None
        self._zooming = False
        self._recompose()

    def _zoom_preview(self):
        """Draw the composite we already have, rescaled to the new zoom. True if a preview was made.

        Only the VISIBLE rect is produced, at canvas size — not the whole margined composite scaled
        up. That matters more than it sounds: ImageTk.PhotoImage dominates a repaint, so its cost
        tracks output pixels, and a first attempt that rescaled the entire margined composite came out
        no cheaper than just rebuilding properly. Cropping to the viewport is ~3-4x fewer pixels.

        Refuses to magnify the source more than 3x, where the blur would be worse than a short wait.

        The crop is measured against _comp_src_rect — the region _comp_img ACTUALLY covers — never
        against _comp_rect, which this function overwrites with the smaller previewed region so
        _redraw positions it correctly. Reading the origin back out of _comp_rect meant the second and
        every later notch of a burst cropped against the previous preview's rect while the pixels
        still had the original origin, so the view darted somewhere else and the error compounded
        until the sharp render landed."""
        try:
            cx0, cy0, cx1, cy1 = self._comp_src_rect
            vx0, vy0, vx1, vy1 = self._visible_base_rect()
            if vx1 <= vx0 or vy1 <= vy0:
                return False
            vx0 = max(vx0, cx0); vy0 = max(vy0, cy0)
            vx1 = min(vx1, cx1); vy1 = min(vy1, cy1)
            if vx1 - vx0 < 1 or vy1 - vy0 < 1:
                return False
            cs = self._comp_scale
            src = ((vx0 - cx0) * cs, (vy0 - cy0) * cs, (vx1 - cx0) * cs, (vy1 - cy0) * cs)
            dw = max(1, int(round((vx1 - vx0) * self._scale)))
            dh = max(1, int(round((vy1 - vy0) * self._scale)))
            if dw > (src[2] - src[0]) * self._PREVIEW_MAG or \
               dh > (src[3] - src[1]) * self._PREVIEW_MAG:
                return False
            # NEAREST, not BILINEAR: this frame exists only until the wheel goes quiet, and the
            # filter was the single biggest cost in a zoom burst (57% of it, ~14 ms a notch). The
            # sharp render lands _ZOOM_SETTLE_MS later and is the one the user actually reads, so
            # paying for smooth interpolation on a frame that lives ~100 ms buys nothing and is
            # exactly what made zooming feel spiky. Magnifying is the common case in a burst and
            # NEAREST magnification is blocky rather than blurry — no worse a stand-in.
            img = self._comp_img.resize((dw, dh), Image.NEAREST, box=src)
            self._set_comp_image(img)
            self._comp_rect = (vx0, vy0, vx1, vy1)   # preview covers only what is on screen
            self._comp_is_preview = True             # no margin -> a pan must not slide on this
            return True
        except Exception:
            return False

    def _set_status(self, main):
        """Set the MAIN status text (cursor/zoom/map/scenario/[view] or a transient action note) and
        render it together with the live terrain-decode suffix. Keeping them in one render means a
        mouse-move that refreshes the cursor readout no longer wipes the decode % (issue #15)."""
        self._status_main = main
        try:
            self._status.config(text=main + self._terrain_status)
        except Exception:
            pass

    def _refresh_status(self):
        """Re-render the status line after only the terrain-decode suffix changed — keeps whatever
        MAIN text (cursor/zoom/…) is currently showing instead of blanking it."""
        try:
            self._status.config(text=self._status_main + self._terrain_status)
        except Exception:
            pass

    def _hover(self, e):
        if self._pil and self._bbox:
            wx, wy = self._screen_to_world(e.x, e.y)
            cur = t("map.drag_handle") if self._handle_at(e.x, e.y) is not None else \
                  (t("map.edit_2") if self._edit.get() else t("map.view"))
            self._set_status(
                f"  {self._map_cb.get()} / {self._scn_cb.get()}{t('map.modified') if self._dirty else ''}   "
                + t("map.sectors_ns_cursor_wx_0f", ns=len(self._zones), wx=wx, wy=wy,
                    zoom=self._scale, cur=cur))

    # ── hit-test (point in any sector triangle) ──────────────────────────────────
    @staticmethod
    def _pt_in_tri(px, py, a, b, c):
        d1 = (px - b[0]) * (a[1] - b[1]) - (a[0] - b[0]) * (py - b[1])
        d2 = (px - c[0]) * (b[1] - c[1]) - (b[0] - c[0]) * (py - c[1])
        d3 = (px - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (py - a[1])
        neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
        pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
        return not (neg and pos)

    def _hit_test(self, wx, wy):
        for zi, z in enumerate(self._zones):
            vs = z["verts"]
            for f in z["faces"]:
                if len(f) >= 3 and max(f[:3]) < len(vs):
                    if self._pt_in_tri(wx, wy, vs[f[0]], vs[f[1]], vs[f[2]]):
                        return zi
        return None

    # ── render ──────────────────────────────────────────────────────────────────
    def _invalidate_redraw(self):
        """Data may have changed: rebuild BOTH overlay rasters on the next compose. Used by the
        infrequent callers (toggles, property commits, map/scenario load). The per-motion hot paths
        use the granular _invalidate_sdb / _invalidate_vec / _recompose instead so a paint stroke
        never re-rasterises the (heavy) KDT/road vectors and a pan/zoom rebuilds neither overlay."""
        self._ov_sdb_dirty = True
        self._ov_vec_dirty = True
        self._comp_key = None
        self._redraw()

    def _invalidate_sdb(self):
        """Only the painted SDB layer changed — leave the vector overlay cache intact (keeps painting
        snappy: no KDT/road redraw)."""
        self._ov_sdb_dirty = True
        self._comp_key = None
        self._redraw()

    def _invalidate_vec(self):
        """Only sectors / KDT mesh / roads changed — leave the SDB raster cache intact."""
        self._ov_vec_dirty = True
        self._comp_key = None
        self._redraw()

    def _recompose(self):
        """View transform only (pan/zoom): re-crop+resize the cached overlays, rebuild NEITHER."""
        self._comp_key = None
        self._schedule_redraw()

    def _schedule_redraw(self):
        """Coalesce the high-frequency interactive redraws (pan / zoom / vertex drag). Tk floods
        <B1-Motion>/<MouseWheel> far faster than we can repaint a big map; collapsing the burst onto a
        single idle callback drops the intermediate frames and keeps panning responsive (perf, #15)."""
        if self._redraw_scheduled:
            return
        self._redraw_scheduled = True
        self.after_idle(self._do_scheduled_redraw)

    def _do_scheduled_redraw(self):
        self._redraw_scheduled = False
        try:
            if self.winfo_exists():
                self._redraw()
        except Exception:
            pass

    def _comp_state(self):
        # everything baked into the cached composite image; pan/placement-drag don't change these,
        # so they reuse the cached bitmap (only the few live edit items get redrawn each frame).
        return (id(self._zones), round(self._scale, 5), self._sel, self._flip_y.get(),
                self._pil.size if self._pil else None, self._dirty,
                self._show_roads.get(), self._show_forest.get(), self._show_sectors.get(),
                self._show_relief.get(), id(self._ov_relief),
                self._kdt_show.get(), self._kdt_color.get(), self._kdt_edit.get(), self._kdt_dirty,
                self._kdt_dx.get(), self._kdt_dy.get(), self._kdt_scale.get(),
                tuple(v.get() for v in self._sdb_show.values()), self._sdb_rev)

    def _set_comp_image(self, img):
        """Hand a finished frame to Tk, reusing the PhotoImage when the size is unchanged.

        Building a PhotoImage allocates a whole new Tk image and was ~29% of a zoom frame. During a
        wheel burst the output is the viewport almost every time, so the size usually repeats and
        `paste` can refill the existing one instead."""
        tk_img = self._comp_tk
        if tk_img is not None and tk_img.width() == img.width and tk_img.height() == img.height:
            try:
                tk_img.paste(img)
                return
            except Exception:
                pass                    # mode/state mismatch — fall back to a fresh image
        self._comp_tk = ImageTk.PhotoImage(img)

    def _pan_slide_budget(self):
        """How far the drawn frame may be slid before it has to be redrawn for real.

        Both the composite and the markers are rendered with slack around the viewport
        (``_build_composite``'s margin and ``_place_cull_pad``), so sliding is only honest while the
        newly exposed edge is inside that slack. Kept at a quarter of the SHORTER side so it is
        within the composite's margin (a max of the two) on any canvas shape."""
        cw = max(self._canvas.winfo_width(), 1)
        ch = max(self._canvas.winfo_height(), 1)
        return max(96, min(cw, ch) // 4)

    def _place_cull_pad(self):
        """Screen-space slack around the viewport for marker culling. Has to cover the pan slide
        budget, or a slid frame would show blank where markers should have come into view."""
        return self._pan_slide_budget() + 80

    def _visible_base_rect(self):
        """The minimap-pixel rectangle currently visible in the canvas (clamped to the minimap)."""
        cw = max(self._canvas.winfo_width(), 1); ch = max(self._canvas.winfo_height(), 1)
        sc = self._scale; W, H = self._pil.size
        return (max(0.0, -self._ox / sc), max(0.0, -self._oy / sc),
                min(float(W), (cw - self._ox) / sc), min(float(H), (ch - self._oy) / sc))

    @staticmethod
    def _rect_covers(r, v):
        return r is not None and r[0] <= v[0] and r[1] <= v[1] and r[2] >= v[2] and r[3] >= v[3]

    # ── overlay rasters (view-independent; built once per data change, reused on pan/zoom) ──────
    # The static overlays are rendered ONCE into whole-map bitmaps in "overlay space" (= base/minimap
    # pixels × a supersample factor, capped so the longest side stays ~2048px → thin lines stay crisp
    # at normal zoom). Pan/zoom then only crop+resize these cached bitmaps (a C-level PIL op) instead
    # of re-running thousands of Python ImageDraw/numpy calls every frame. Two independent caches:
    #   _ov_sdb  — the painted SDB layers (numpy raster; rebuilt by a paint stroke, which is cheap)
    #   _ov_vec  — sectors + KDT mesh + roads (vector raster; the heavy one — frozen during drags)
    _OV_LONG_SIDE = 2048        # overlay rasters are held at ~this long side, whatever the map size

    def _ov_scale_for(self, W, H):
        """Base px -> overlay px. Supersamples a SMALL base image so 1px overlay lines still render
        ~1px on screen near zoom 1, and DOWNSAMPLES a large one so the raster stays bounded: the
        terrain is now full-detail (issue #19), and at 6144x4096 a 1:1 overlay would be 100 MB per
        layer. Returns a float — the callers scale by it and round."""
        return max(0.02, min(8.0, self._OV_LONG_SIDE / float(max(W, H, 1))))

    def _ov_world_to_overlay(self, x, y, ovs):
        bx, by = self._world_to_base(x, y)
        return bx * ovs, by * ovs

    def _build_sdb_overlay(self):
        """Rasterise the visible SDB paint layers into _ov_sdb (overlay space). Direct numpy fill from
        the R×R grid → one Image.fromarray + a NEAREST resize per layer (no per-cell Python loop, no
        grid_to_cells). This is what makes a paint stroke refresh in ms instead of ~10s."""
        W, H = self._pil.size; ovs = self._ov_scale
        OW, OH = max(1, int(round(W * ovs))), max(1, int(round(H * ovs)))
        ov = Image.new("RGBA", (OW, OH), (0, 0, 0, 0))
        if self._bbox and self._sdb:
            try:
                import numpy as np
            except Exception:
                np = None
            if np is not None:
                R = self._sdb["R"]
                g = np.frombuffer(bytes(self._sdb["grid"]), np.uint8).reshape(R, R)
                # The engine makes the SDB region SQUARE (side=max(bboxX,bboxY)); the shorter axis extends in
                # the NEGATIVE direction so the terrain ends up CENTRED. Confirmed at runtime
                # (m05_hollande bounds=[-655360,0,1966080,2621440]). Map the grid onto those exact world
                # bounds so the overlay AND painting match what the engine reads. Square maps -> identity.
                minX, minY, maxX, maxY = self._sdb_world_bounds()
                bx0, by0 = self._ov_world_to_overlay(minX, minY, ovs)
                bx1, by1 = self._ov_world_to_overlay(maxX, maxY, ovs)
                dx0 = int(math.floor(min(bx0, bx1))); dy0 = int(math.floor(min(by0, by1)))
                dw = max(1, int(math.ceil(max(bx0, bx1))) - dx0)
                dh = max(1, int(math.ceil(max(by0, by1))) - dy0)
                flip = self._flip_y.get()                  # grid row 0 = world y 0; y-flip puts it bottom
                for _label, _bit, _rgb in self._sdb_layers:
                    if not self._sdb_show[_bit].get():
                        continue
                    mask = (g & _bit) > 0
                    if not mask.any():
                        continue
                    rgba = np.zeros((R, R, 4), np.uint8)
                    rgba[..., 0] = _rgb[0]; rgba[..., 1] = _rgb[1]; rgba[..., 2] = _rgb[2]
                    rgba[..., 3] = np.where(mask, 104, 0).astype(np.uint8)
                    block = Image.fromarray(rgba, "RGBA")
                    if flip:
                        block = block.transpose(Image.FLIP_TOP_BOTTOM)
                    block = block.resize((dw, dh), Image.NEAREST)
                    # paste (no mask) into a full-size transparent tile preserves the block's STRAIGHT
                    # alpha (0/104) and clips out-of-bounds; alpha_composite then blends it correctly
                    # (paste-with-self-as-mask would premultiply against the transparent base = darken).
                    tile = Image.new("RGBA", (OW, OH), (0, 0, 0, 0))
                    tile.paste(block, (dx0, dy0))
                    ov = Image.alpha_composite(ov, tile)
        self._ov_sdb = ov
        self._ov_sdb_dirty = False
        self._ov_crop_cache.clear()

    def _build_vec_overlay(self):
        """Rasterise sectors + KDT mesh + roads into _ov_vec (overlay space). Heavy (vector primitives),
        so it is rebuilt ONLY when that geometry/toggles change — never on pan/zoom, and frozen while a
        sector/KDT vertex is being dragged (the moving handles/outline are live canvas items instead)."""
        W, H = self._pil.size; ovs = self._ov_scale
        OW, OH = max(1, int(round(W * ovs))), max(1, int(round(H * ovs)))
        ov = Image.new("RGBA", (OW, OH), (0, 0, 0, 0))
        d = ImageDraw.Draw(ov, "RGBA")

        def b(x, y):
            return self._ov_world_to_overlay(x, y, ovs)

        def onscreen(px, py, pad=8):
            return -pad <= px <= OW + pad and -pad <= py <= OH + pad

        # PIL's draw width= must be an int, and _ov_scale is a FLOAT now that the overlay raster is
        # capped independently of the terrain size (issue #19) — round, don't just clamp.
        lw = max(1, int(round(ovs)))                       # 1px-at-zoom-1 line width
        if self._bbox:
            # scenario AREA-zone polygons = the VISUAL sectors (coloured fill — the EDIT TARGET)
            for zi, z in enumerate(self._zones if self._show_sectors.get() else []):
                if zi == self._sel:
                    continue
                col = _SECTOR_COLORS[z["idx"] % len(_SECTOR_COLORS)]
                bp = [b(x, y) for (x, y) in z["verts"]]
                if not bp:
                    continue
                xs = [p[0] for p in bp]; ys = [p[1] for p in bp]
                if max(xs) < -8 or min(xs) > OW + 8 or max(ys) < -8 or min(ys) > OH + 8:
                    continue
                for f in z["faces"]:
                    if len(f) >= 3 and max(f[:3]) < len(bp):
                        d.polygon([bp[f[0]], bp[f[1]], bp[f[2]]], fill=col + (55,))
                for (a, bk) in z["boundary"]:
                    if a < len(bp) and bk < len(bp):
                        d.line([bp[a][0], bp[a][1], bp[bk][0], bp[bk][1]], fill=col + (230,), width=lw)
            # KDT mechanics mesh = a wireframe OVERLAY that FOLLOWS the sectors
            if self._kdt_show.get() and self._kdt_tris:
                by_sector = self._kdt_color.get()
                bp = [b(*self._kdt_xform_world(x, y)) for (x, y) in self._kdt_world]
                n = len(bp)
                for ti, (a, bb, k) in enumerate(self._kdt_tris):
                    if a >= n or bb >= n or k >= n:
                        continue
                    if by_sector:
                        S = self._kdt_tri_sector[ti] if ti < len(self._kdt_tri_sector) else None
                        col = _SECTOR_COLORS[S % len(_SECTOR_COLORS)] if S is not None else (51, 72, 94)
                    else:
                        col = (33, 212, 212)
                    d.line([bp[a], bp[bb], bp[k], bp[a]], fill=col + (235,), width=lw)
            # roads ON TOP (brightest): edges + node dots, junctions highlighted
            if self._show_roads.get() and self._roads:
                nodes = self._roads["nodes"]
                bp = [b(x, y) for (x, y, _) in nodes]
                rw = max(2, int(round(2 * ovs))); rr = max(2, int(round(ovs + 1)))
                for i, j in self._roads["edges"]:
                    if onscreen(*bp[i]) or onscreen(*bp[j]):
                        d.line([bp[i], bp[j]], fill=(214, 142, 64, 255), width=rw)
                for (px, py), (_, _, deg) in zip(bp, nodes):
                    if not onscreen(px, py):
                        continue
                    if deg >= 3:
                        d.ellipse([px - rr - 1, py - rr - 1, px + rr + 1, py + rr + 1],
                                  fill=(255, 214, 110, 255), outline=(70, 44, 12, 255))
                    else:
                        d.ellipse([px - rr, py - rr, px + rr, py + rr], fill=(232, 158, 86, 255))
        self._ov_vec = ov
        self._ov_crop_cache.clear()
        if not self._dragging:                            # while dragging we keep the stale cache
            self._ov_vec_dirty = False

    def _build_composite(self):
        """Compose the visible viewport: crop+resize the base image, then crop+resize the cached SDB
        and vector overlays over it. The overlays are (re)rasterised only when their data is dirty —
        pan/zoom just re-crop the existing bitmaps, so this is a handful of C-level PIL ops per frame."""
        W, H = self._pil.size
        sc = self._scale
        cw = max(self._canvas.winfo_width(), 1); ch = max(self._canvas.winfo_height(), 1)
        # screen-px slack baked around the viewport so a pan reuses the cached composite (just
        # repositions it) instead of rebuilding. A quarter-viewport of slack covers most drag motions
        # for a ~2.25x-area composite — a small per-rebuild cost for far fewer rebuilds (perf, #15).
        # Kept at a quarter deliberately: a half-viewport doubled the area and took a rebuild from
        # ~65 ms to ~154 ms, which is worse than rebuilding slightly more often.
        margin = max(128, cw // 4, ch // 4)
        cx0 = max(0, int(math.floor((-self._ox - margin) / sc)))
        cy0 = max(0, int(math.floor((-self._oy - margin) / sc)))
        cx1 = min(W, int(math.ceil((cw - self._ox + margin) / sc)))
        cy1 = min(H, int(math.ceil((ch - self._oy + margin) / sc)))
        if cx1 <= cx0 or cy1 <= cy0:                   # panned off-map — fall back to whole map
            cx0, cy0, cx1, cy1 = 0, 0, W, H
        dw = max(1, int(round((cx1 - cx0) * sc))); dh = max(1, int(round((cy1 - cy0) * sc)))
        resample = Image.NEAREST if sc >= 1 else Image.BILINEAR
        if hasattr(self._pil, "crop_resized"):
            # tiled terrain (issue #19): it picks the LOD tier for this zoom and decodes only the
            # chunks this view touches, so zooming in keeps resolving real 512 px detail
            img = self._pil.crop_resized((cx0, cy0, cx1, cy1), (dw, dh), resample)
        else:
            img = self._pil.crop((cx0, cy0, cx1, cy1)).resize((dw, dh), resample)
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        # keep the overlay caches in sync (rebuild only when dirty; recompute supersample on map resize)
        ovs = self._ov_scale_for(W, H)
        if ovs != self._ov_scale:
            self._ov_scale = ovs
            self._ov_sdb_dirty = self._ov_vec_dirty = True
        if self._ov_sdb is None or self._ov_sdb_dirty:
            self._build_sdb_overlay()
        if self._ov_vec is None or (self._ov_vec_dirty and not self._dragging):
            self._build_vec_overlay()
        ob = (int(cx0 * ovs), int(cy0 * ovs), int(cx1 * ovs), int(cy1 * ovs))
        # Cropping+resizing the two whole-map overlay rasters is a large slice of a rebuild, and it
        # repeats verbatim whenever the same view is rebuilt (settling a zoom, finishing a pan, a
        # terrain chunk landing). Cache the finished crops; _invalidate_sdb/_invalidate_vec drop them.
        # Shaded relief sits UNDER the data overlays: it is terrain, not annotation, so sectors,
        # roads and painted SDB have to stay legible on top of it. It lives in its own aspect-correct
        # raster rather than overlay space, so the crop box is computed as a fraction of the map.
        if self._show_relief.get() and self._ov_relief is not None:
            rw, rh = self._ov_relief.size
            rb = (int(cx0 / W * rw), int(cy0 / H * rh),
                  max(int(cx1 / W * rw), int(cx0 / W * rw) + 1),
                  max(int(cy1 / H * rh), int(cy0 / H * rh) + 1))
            ck = ("relief", rb, dw, dh, id(self._ov_relief))
            crop = self._ov_crop_cache.get(ck)
            if crop is None:
                crop = self._ov_relief.crop(rb)
                if crop.size != (dw, dh):
                    crop = crop.resize((dw, dh), Image.BILINEAR)
                if len(self._ov_crop_cache) > 8:
                    self._ov_crop_cache.clear()
                self._ov_crop_cache[ck] = crop
            img.alpha_composite(crop)
        for name, cache in (("sdb", self._ov_sdb), ("vec", self._ov_vec)):   # SDB under vectors
            if cache is None:
                continue
            ck = (name, ob, dw, dh, id(cache))
            crop = self._ov_crop_cache.get(ck)
            if crop is None:
                crop = cache.crop(ob)
                if crop.size != (dw, dh):
                    crop = crop.resize((dw, dh), Image.BILINEAR)
                if len(self._ov_crop_cache) > 8:
                    self._ov_crop_cache.clear()
                self._ov_crop_cache[ck] = crop
            img.alpha_composite(crop)
        self._comp_img = img
        self._comp_scale = sc           # px-per-base-px the composite was rendered at (zoom preview)
        self._comp_src_rect = (cx0, cy0, cx1, cy1)   # what _comp_img covers; _comp_rect may shrink
        self._set_comp_image(img)
        self._comp_rect = (cx0, cy0, cx1, cy1)
        self._comp_is_preview = False   # full render, margin included -> safe to slide a pan on
        self._comp_key = self._comp_state()

    def _redraw(self):
        c = self._canvas
        c.delete("all")
        self._pan_slid = (0.0, 0.0)     # everything is drawn at the true offset again
        if not self._pil:
            c.create_text(20, 20, anchor="nw", fill=_R_TEXT_DIM, font=_F_MAIN,
                          text=t("map.no_minimap_terrain_dat_not"))
            return
        vis = self._visible_base_rect()
        stale = (self._comp_key != self._comp_state()
                 or not self._rect_covers(self._comp_rect, vis))
        # A rebuild costs ~65-100 ms, far longer than motion events arrive, so rebuilding per frame
        # is what made dragging lag the cursor and snap on release. While a pan is live we slide the
        # cached bitmap instead and refresh at most a few times a second, so the drag stays smooth and
        # the edges still fill in on a long throw. _press_release does the final, exact rebuild.
        if stale and self._panning and self._comp_tk is not None:
            if time.monotonic() - self._last_comp_build < 0.2:
                stale = False
        # Mid-zoom the exact rebuild is deferred (see _wheel): rescale the composite we already have
        # so the view tracks the wheel with no lag, and let _zoom_settled put the sharp one in.
        if stale and self._zooming and self._comp_img is not None:
            if self._zoom_preview():
                stale = False
            # No "hold the last frame" fallback here, deliberately. _redraw positions the composite
            # with the CURRENT scale while a reused frame's pixels are still at the scale it was
            # made at, so reusing one slides the map under the cursor — measured up to 263 px before
            # the sharp render snapped it back, which is exactly the drift that makes zooming feel
            # disorienting. A rebuild costs time; a wrong position costs trust. So if the preview
            # cannot produce a correct frame we rebuild, and _zoom_preview is instead made able to
            # cover essentially every notch (see _PREVIEW_MAG) so that stays rare.
        if stale:
            self._build_composite()
            self._last_comp_build = time.monotonic()
        cx0, cy0 = self._comp_rect[0], self._comp_rect[1]
        c.create_image(cx0 * self._scale + self._ox, cy0 * self._scale + self._oy,
                       anchor="nw", image=self._comp_tk)

        # selected sector boundary outline (white) for context
        if self._sel is not None and self._bbox and self._sel < len(self._zones):
            z = self._zones[self._sel]
            for (a, b) in z["boundary"]:
                x1, y1 = self._world_to_screen(*z["verts"][a])
                x2, y2 = self._world_to_screen(*z["verts"][b])
                c.create_line(x1, y1, x2, y2, fill="#ffffff", width=2)

        # edit mode: draw EVERY welded-corner handle (shared corners = gold, single = grey).
        # Dragging one moves all zones that share it, so adjacent sectors never tear apart.
        if self._edit.get() and self._bbox:
            # show EVERY node so any single one can be grabbed & fixed. outer ring = gold square
            # (bright if shared/welded), inner ring = amber square, interior fill = small grey dot.
            for cl in self._clusters:
                hx, hy = self._world_to_screen(*self._cluster_pos(cl))
                kind = cl.get("kind", "outer")
                if kind == "outer":
                    shared = len({zi for zi, _ in cl["members"]}) > 1
                    col = _R_GOLD_BRT if shared else _R_GOLD
                    c.create_rectangle(hx - _HANDLE_R, hy - _HANDLE_R, hx + _HANDLE_R, hy + _HANDLE_R,
                                       fill=col, outline="#000000")
                elif kind == "inner":
                    c.create_rectangle(hx - 3, hy - 3, hx + 3, hy + 3, fill="#d9b24a", outline="#000000")
                else:
                    c.create_oval(hx - 2, hy - 2, hx + 2, hy + 2, fill="#8aa0b4", outline="#1a2636")

        # KDT border-edit mode: interior nodes as small dim dots (auto-relax flow is visible),
        # border vertices as draggable cyan square handles (gold while grabbed).
        if self._kdt_edit.get() and self._kdt_show.get() and self._bbox:
            for vi in range(len(self._kdt_world)):
                if vi in self._kdt_bverts:
                    continue
                hx, hy = self._world_to_screen(*self._kdt_xform_world(*self._kdt_world[vi]))
                c.create_oval(hx - 2, hy - 2, hx + 2, hy + 2, fill="#6f8aa6", outline="")
            for vi in self._kdt_bverts:
                if vi >= len(self._kdt_world):
                    continue
                hx, hy = self._world_to_screen(*self._kdt_xform_world(*self._kdt_world[vi]))
                col = "#ffe14d" if vi == self._drag_kdt_vi else "#33d4d4"
                c.create_rectangle(hx - _HANDLE_R, hy - _HANDLE_R, hx + _HANDLE_R, hy + _HANDLE_R,
                                   fill=col, outline="#000000")

        if self._lbl_var.get():
            for zi, z in enumerate(self._zones):
                sx, sy = self._world_to_screen(*z["pos"])
                r = 9 if zi == self._sel else 7
                c.create_oval(sx - r, sy - r, sx + r, sy + r, fill="#0e1a2a",
                              outline=_R_GOLD_BRT if zi == self._sel else _R_GOLD)
                c.create_text(sx, sy, text=str(z["idx"]), fill=_R_GOLD_BRT, font=_F_BOLD)

        self._draw_placements(c)

        self._set_status(
            f"  {self._map_cb.get()} / {self._scn_cb.get()}{t('map.modified') if self._dirty else ''}   "
            + t("map.sectors_ns_selected_sel_zoom",
                ns=len(self._zones),
                sel=('#' + str(self._zones[self._sel]['idx']) if self._sel is not None else '-'),
                zoom=self._scale))

    # ── oriented building icons + rotation ───────────────────────────────────────
    @staticmethod
    def _rot_poly(cx, cy, pts, ang):
        """Local px points -> flat canvas coord list, rotated by `ang` and centred at (cx,cy)."""
        ca, sa = math.cos(ang), math.sin(ang)
        flat = []
        for px, py in pts:
            flat += [cx + px * ca - py * sa, cy + px * sa + py * ca]
        return flat

    def _facing_screen_angle(self, x, y, theta):
        """Screen-space angle of world facing `theta` (radians) at world (x,y) — handles Y-flip/zoom
        by transforming the facing vector through world->screen."""
        L = (self._bbox[2] - self._bbox[0]) * 0.01
        sx, sy = self._world_to_screen(x, y)
        tx, ty = self._world_to_screen(x + L * math.cos(theta), y + L * math.sin(theta))
        return math.atan2(ty - sy, tx - sx) if (tx != sx or ty != sy) else 0.0

    def _nearest_road_point(self, x, y):
        """Nearest point ON a road LINE to world (x,y): the perpendicular foot on the closest road
        SEGMENT (clamped to the segment), not just the closest node.  Returns (px, py, dist) or None.
        This is the road geometry both the facing and the road-snap use."""
        if not (self._roads and self._roads.get("edges") and self._roads.get("nodes")):
            return None
        # Cached: this is called once per building marker per repaint, and the markers mostly sit
        # still, so a pan/zoom is almost all cache hits. Dropped when the road graph is replaced.
        memo = self._road_pt_memo
        key = (x, y)
        hit = memo.get(key)
        if hit is not None:
            return hit[0]                       # boxed, so a cached None is still a hit
        nodes, edges = self._roads["nodes"], self._roads["edges"]
        best = None
        for a, b in edges:
            ax, ay = nodes[a][0], nodes[a][1]
            bx, by = nodes[b][0], nodes[b][1]
            ex, ey = bx - ax, by - ay
            l2 = ex * ex + ey * ey
            if l2 <= 1e-9:
                continue
            t = max(0.0, min(1.0, ((x - ax) * ex + (y - ay) * ey) / l2))
            fx, fy = ax + t * ex, ay + t * ey
            d2 = (x - fx) ** 2 + (y - fy) ** 2
            if best is None or d2 < best[0]:
                best = (d2, fx, fy)
        out = None if best is None else (best[1], best[2], math.sqrt(best[0]))
        if len(memo) > 8192:                    # dragging a marker feeds a new point every event
            memo.clear()
        memo[key] = (out,)
        return out

    def _snap_to_road(self, pl, wx, wy):
        """If Auto-snap-to-roads is on and this is a depot/HQ, return the closest valid position that
        sits at the kind's fixed offset (_ROAD_SNAP_OFFSET) from a road LINE, on the mouse's side —
        i.e. snap the dragged point to the road-parallel offset curve nearest the cursor.  Otherwise
        return (wx,wy) unchanged."""
        if not (self._snap_roads.get() and pl["kind"] in _ROAD_SNAP_OFFSET
                and self._roads and self._roads.get("edges") and self._roads.get("nodes")):
            return wx, wy
        d = _ROAD_SNAP_OFFSET[pl["kind"]]
        nodes, edges = self._roads["nodes"], self._roads["edges"]
        best = None  # (dist-to-mouse², cand_x, cand_y)
        for a, b in edges:
            ax, ay = nodes[a][0], nodes[a][1]
            bx, by = nodes[b][0], nodes[b][1]
            ex, ey = bx - ax, by - ay
            l2 = ex * ex + ey * ey
            if l2 <= 1e-9:
                continue
            t = max(0.0, min(1.0, ((wx - ax) * ex + (wy - ay) * ey) / l2))
            px, py = ax + t * ex, ay + t * ey            # nearest point on the segment
            dx, dy = wx - px, wy - py
            dl = math.hypot(dx, dy)
            if dl < 1e-6:                                # cursor sits on the road — push out perpendicular
                nlen = math.sqrt(l2)
                ux, uy = -ey / nlen, ex / nlen
            else:
                ux, uy = dx / dl, dy / dl                # unit direction from the road toward the cursor
            cx, cy = px + d * ux, py + d * uy            # candidate at offset d from the road, cursor side
            dd = (cx - wx) ** 2 + (cy - wy) ** 2
            if best is None or dd < best[0]:
                best = (dd, cx, cy)
        return (best[1], best[2]) if best else (wx, wy)

    def _place_facing(self, pl):
        """World-space facing (radians). BUILDINGS (depot + HQ) LOCK TO THE NEAREST ROAD in-game,
        regardless of their stored Rotation (user-confirmed behavior; data backs it — on alpha the
        depots' facing matches nearest-road direction at R=0.92, while blitz's near-zero stored
        rotations don't, R=0.05). We orient them toward the nearest point on the nearest road LINE
        (segment, not just a node — more accurate when the foot lands mid-edge).  Other kinds fall
        back to item Rotation + per-model offset."""
        if pl["kind"] in ("depot", "hq") and self._roads and self._roads.get("nodes"):
            x, y = pl["pos"][0], pl["pos"][1]
            rp = self._nearest_road_point(x, y)
            if rp is None:                       # graph has nodes but no usable edges — face nearest node
                nx, ny, _ = min(self._roads["nodes"], key=lambda n: (n[0] - x) ** 2 + (n[1] - y) ** 2)
                rp = (nx, ny, 0.0)
            px, py, _ = rp
            if (px, py) != (x, y):
                return math.atan2(py - y, px - x)
        return (pl["rot"] or 0.0) + _MODEL_FACING_OFFSET.get(pl["kind"], 0.0)

    def _place_rot_handle_screen(self, pl):
        sx, sy = self._world_to_screen(pl["pos"][0], pl["pos"][1])
        ang = self._facing_screen_angle(pl["pos"][0], pl["pos"][1], self._place_facing(pl))
        R = 30
        return sx + R * math.cos(ang), sy + R * math.sin(ang)

    def _update_place_info(self):
        """Show placement count + scenario byte size — spawn count is bounded by the .scenario FILE
        SIZE (~600-entity practical envelope), so the byte budget matters more than a count cap."""
        n = len(self._places or [])
        self._place_lbl.config(text=t("map.n_placements_kb_1f_kb",
                                      n=n, kb=(self._scn_size or 0) / 1024.0)
                               + "\n" + self._camp_map_status())

    def _get_icon(self, name, size, subdir=None):
        """Load+cache a map-editor placement icon (icons/map_icons/[subdir/]<name>) fit to `size` px,
        aspect-preserved, as an ImageTk.PhotoImage. None if missing/unavailable.

        Cached per (name, size, subdir): the canvas rebuilds every marker on every frame, so the
        resize+PhotoImage conversion has to happen once per distinct icon, not once per draw."""
        if not name or not _HAVE_PIL:
            return None
        key = (name, size, subdir)
        if key not in self._icon_cache:
            try:
                path = os.path.join(ICON_DIR, subdir, name) if subdir \
                    else os.path.join(ICON_DIR, name)
                with pil_log.source("Map editor"):
                    im = Image.open(path).convert("RGBA")
                w, h = im.size
                s = size / max(w, h)
                im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
                self._icon_cache[key] = ImageTk.PhotoImage(im)
            except Exception:
                self._icon_cache[key] = None
        return self._icon_cache[key]

    # Corner overlays, composited ONTO the role icon at draw time rather than baked into icon
    # variants. They have to be live: the controlling camp is something the author changes at will,
    # and 105 roles x 7 unit nations x 7 controlling nations x N camps is not a set of files anyone
    # wants on disk. Composited at the icon's MASTER resolution and downscaled once, so the small
    # marks stay antialiased instead of turning to mush.
    #   top-left     the unit's OWN nationality (fixed by its class)
    #   bottom-left  the nationality of the camp CONTROLLING it (from the paired script)
    #   bottom-right the camp number
    #   top-right    already baked into the role icon (advanced / nuclear / decoy / airborne)
    # The marks are big enough to read at map size; what keeps them from burying the silhouette is
    # WHERE they sit (centred on the corners, half outside the chip), not shrinking them.
    _OVERLAY_MASTER = 64            # role PNGs are 64 px; composite there, then resize
    _NATION_FRAC = 0.42             # nation roundel diameter (INCLUDING its dark ring)
    _CAMP_W_FRAC = 0.44             # camp-number chip
    _CAMP_H_FRAC = 0.36

    def _role_icon(self, pl, size, show_nations=None, show_camps=None):
        """The picture for one placement at `size` px — the role icon plus its live corner marks.

        `show_nations` / `show_camps` are passed in by the draw loop, which reads the Tk variables
        ONCE per frame: reading them here cost two Tk global-variable lookups per marker per frame
        (2280 of them a frame at 1088 markers). They fall back to reading the vars for any other
        caller. None when icons are off or PIL is unavailable."""
        if not size:
            return None
        if show_nations is None:
            show_nations = self._show_nations.get()
        if show_camps is None:
            show_camps = self._show_camps.get()
        role = pl.get("role") or "unknown"
        # Only camp-owned kinds have a camp/nation story to tell.
        owned = pl["kind"] in ("depot", "unit", "building", "spawn")
        is_hq = pl["kind"] == "hq"
        show_nat = (owned or is_hq) and show_nations
        unit_nat = pl.get("nation") if (show_nat and owned) else None
        ctrl_nat = pl.get("ctrl_nation") if show_nat else None
        camp = None                       # None here means ONLY "draw no camp chip"
        if show_camps:
            if owned:
                camp = _camp_display(pl["extra"].get("camp"))
            elif is_hq:
                # An HQ is keyed by ALLIANCE, not camp — a different axis, so it is badged "A1", not
                # "1", or a 1 here and a 1 on a tank would look like the same thing.
                a = pl["extra"].get("alliance")
                camp = ("A%s" % a) if a is not None else None
        if not (unit_nat or ctrl_nat or camp is not None):
            return self._get_icon(proles.icon_name(role), size, "roles")
        key = (role, unit_nat, ctrl_nat, camp, size)
        if key not in self._overlay_cache:
            try:
                self._overlay_cache[key] = self._build_overlay_icon(
                    role, unit_nat, ctrl_nat, camp, size)
            except Exception:
                self._overlay_cache[key] = self._get_icon(proles.icon_name(role), size, "roles")
        return self._overlay_cache[key]

    def _nation_png(self, name):
        """Cached RGBA of a nation roundel at master size (icons/nation_icons/<name>)."""
        if name not in self._nation_src:
            try:
                with pil_log.source("Map editor"):
                    im = Image.open(os.path.join(NATION_ICON_DIR, name)).convert("RGBA")
                self._nation_src[name] = im
            except Exception:
                self._nation_src[name] = None
        return self._nation_src[name]

    def _build_overlay_icon(self, role, unit_nat, ctrl_nat, camp, size):
        """Compose role icon + corner marks, returned as an ImageTk.PhotoImage.

        The marks are CENTRED ON the chip's corners, so each one straddles the edge and hangs half
        outside. That keeps them clear of the silhouette they annotate (sitting them fully inside
        buried it) and reads the way a notification badge does.

        Because they overhang, the composite is drawn on a canvas larger than the chip and the
        result is scaled so the CHIP still measures `size` px. Two placements therefore always show
        the same size chip whether or not they carry marks — only the marks stick out."""
        M = self._OVERLAY_MASTER
        with pil_log.source("Map editor"):
            base = Image.open(os.path.join(ROLE_ICON_DIR, proles.icon_name(role))).convert("RGBA")
        if base.size != (M, M):
            base = base.resize((M, M), Image.LANCZOS)

        d = int(round(M * self._NATION_FRAC))
        cw = int(round(M * self._CAMP_W_FRAC))
        ch = int(round(M * self._CAMP_H_FRAC))
        over = int(round(max(d, cw, ch) / 2.0)) + 1        # how far a mark reaches past the chip
        C = M + 2 * over
        img = Image.new("RGBA", (C, C), (0, 0, 0, 0))
        img.alpha_composite(base, (over, over))

        def roundel(name, cx, cy):
            src = self._nation_png(name)
            if src is None:
                return
            ring = max(1, int(M * 0.028))
            inner = d - 2 * ring
            plate = Image.new("RGBA", (d, d), (0, 0, 0, 0))
            # dark disc behind the flag so a pale roundel still separates from a pale chip
            ImageDraw.Draw(plate).ellipse([0, 0, d - 1, d - 1], fill=(10, 16, 26, 240))
            flag = src.resize((inner, inner), Image.LANCZOS)
            plate.alpha_composite(flag, (ring, ring))
            img.alpha_composite(plate, (int(cx - d / 2.0), int(cy - d / 2.0)))

        if unit_nat:
            roundel(unit_nat, over, over)                        # top-left corner
        if ctrl_nat:
            roundel(ctrl_nat, over, over + M)                    # bottom-left corner
        if camp is not None:
            self._draw_camp_chip(img, camp, M, over + M, over + M, cw, ch)   # bottom-right corner

        out_px = max(1, int(round(size * C / float(M))))
        return ImageTk.PhotoImage(img.resize((out_px, out_px), Image.LANCZOS))

    def _draw_camp_chip(self, img, camp, M, cx, cy, w, h):
        """Camp badge centred on (cx, cy). -1 draws as 'N' (neutral) and -2 as 'X' (despawn):
        a bare minus sign is one unreadable stroke at map size."""
        text = "N" if camp == -1 else ("X" if camp == -2 else str(camp))
        # "A1"-style alliance badges are wider than a digit; shrink to fit rather than overflow.
        if len(text) > 1:
            w = int(w * (1.0 + 0.34 * (len(text) - 1)))
        x0, y0 = int(cx - w / 2.0), int(cy - h / 2.0)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([x0, y0, x0 + w, y0 + h], radius=int(h * 0.34),
                            fill=(12, 18, 28, 242), outline=(238, 244, 250, 255),
                            width=max(1, int(M * 0.028)))
        f = self._camp_font(max(6, int(h * 0.80)))
        try:
            bb = d.textbbox((0, 0), text, font=f)
            tw, th = bb[2] - bb[0], bb[3] - bb[1]
            d.text((x0 + (w - tw) / 2.0 - bb[0], y0 + (h - th) / 2.0 - bb[1]), text,
                   font=f, fill=(255, 255, 255, 255))
        except Exception:
            d.text((x0 + w * 0.28, y0 + h * 0.08), text, font=f, fill=(255, 255, 255, 255))

    def _camp_font(self, px):
        if px not in self._camp_fonts:
            fnt = None
            for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "arial.ttf"):
                try:
                    fnt = ImageFont.truetype(name, px)
                    break
                except Exception:
                    continue
            if fnt is None:
                fnt = ImageFont.load_default()
            self._camp_fonts[px] = fnt
        return self._camp_fonts[px]

    def _icon_px(self):
        """Current icon size in pixels — 0 when the user has turned icons off (plain dots).

        Zero DURING a wheel burst as well: a Cotentin-sized scenario is 633 markers, and drawing
        each as an image instead of a dot took the zoom frame from 18.7 ms mean / 29.1 ms worst to
        25.9 / 56.4 — over the 33 ms budget, which reads as a stutter. The map already swaps to a
        cheap rescaled preview mid-zoom for the same reason (_zoom_preview); the markers do the same
        and come back sharp on _zoom_settled's redraw, a sixth of a second later."""
        if self._zooming:
            return 0
        return _ICON_SIZES.get(self._icon_size.get(), _ICON_SIZES["medium"])

    def _on_icon_size(self, _e=None):
        self._icon_size.set(self._icon_size_labels.get(self._icon_size_cb.get(), "medium"))
        self._redraw()

    def _on_overlay_toggle(self):
        """Nation-flag / camp-number corner marks toggled — the composites are keyed on them."""
        self._overlay_cache.clear()
        self._redraw()

    def _open_icon_legend(self):
        """The key to the map: every icon, what it is, and what it means.

        Grouped by family (the chip colour) and listing the weight pips and badges, because those
        are the parts of the grammar you can't guess from one icon on its own."""
        win = ui_util.themed_toplevel(self, t("map.icon_legend_title"), size=(760, 640),
                                      modal=False, resizable=True, on_escape=True)
        hdr = tk.Label(win, text=t("map.icon_legend_intro"), background=_R_BG_PANEL,
                       foreground=_R_TEXT_DIM, font=_F_MAIN, justify="left", wraplength=724)
        hdr.pack(anchor="w", padx=10, pady=(10, 2))
        tk.Label(win, text=t("map.icon_corner_key"), background=_R_BG_PANEL,
                 foreground=_R_TEXT, font=_F_MAIN, justify="left",
                 wraplength=724).pack(anchor="w", padx=10, pady=(0, 6))

        holder = ui_util.make_scrollable(win, bg=_R_BG_PANEL)
        self._legend_icons = []                 # keep references — Tk drops unreferenced PhotoImages

        by_family = {}
        for r in proles.all_roles():
            by_family.setdefault(r.family, []).append(r)
        for fam, roles in by_family.items():
            rgb, fam_desc = proles.FAMILIES[fam]
            head = tk.Frame(holder, background=_R_BG_PANEL)
            head.pack(fill="x", padx=8, pady=(10, 2))
            tk.Frame(head, background="#%02x%02x%02x" % rgb, width=14, height=14).pack(side="left")
            tk.Label(head, text="  %s — %s" % (fam, fam_desc), background=_R_BG_PANEL,
                     foreground=_R_GOLD, font=_F_BOLD).pack(side="left")
            for r in roles:
                row = tk.Frame(holder, background=_R_BG_PANEL)
                row.pack(fill="x", padx=18, pady=1)
                ico = self._get_icon(r.icon, 30, "roles")
                if ico is not None:
                    self._legend_icons.append(ico)
                    tk.Label(row, image=ico, background=_R_BG_PANEL).pack(side="left")
                txt = tk.Frame(row, background=_R_BG_PANEL)
                txt.pack(side="left", padx=8, fill="x", expand=True)
                bits = [r.label]
                if r.weight:
                    bits.append(t("map.icon_weight_" + proles.WEIGHT_NAMES[r.weight]))
                if r.badge:
                    bits.append(t("map.icon_badge_" + r.badge))
                tk.Label(txt, text="   ".join(bits), background=_R_BG_PANEL, foreground=_R_TEXT,
                         font=_F_BOLD, anchor="w").pack(anchor="w")
                tk.Label(txt, text=r.desc, background=_R_BG_PANEL, foreground=_R_TEXT_DIM,
                         font=_F_MAIN, anchor="w", justify="left", wraplength=620).pack(anchor="w")

    def _draw_placements(self, c):
        """Draw every placement as live canvas items (not baked into the composite), so they track
        drag edits.

        Each placement draws as its ROLE ICON — a Tiger, a paratrooper, an MG nest and a script
        waypoint are four different pictures now, not three coloured dots (see ROLE_ICON_DIR and
        ruse_mod_engine/placement_roles.py). Zones keep their real footprint (the circle's radius,
        the rectangle's width x height) with the icon at the centre. HQ + depot are also oriented —
        the gold tick is their facing, draggable when selected in edit mode.

        Icons can be sized down or switched off entirely from the Placements panel: the icons cost
        more to repaint than a 4 px dot, and a scenario with 600 markers is exactly where someone
        might want the cheap rendering back."""
        if not (self._show_places.get() and self._places and self._bbox):
            return
        cw = max(c.winfo_width(), 1); ch = max(c.winfo_height(), 1)
        isz = self._icon_px()
        # HOIST EVERY FRAME-CONSTANT OUT OF THE LOOP. These are read once per placement otherwise,
        # and each one is a round trip into Tk: profiling 1088 markers showed _pan_slide_budget
        # running 1088x per frame (two winfo_* calls each) and 2280 globalgetvar calls per frame from
        # BooleanVar.get(). None of it changes while a frame is being drawn. Same bug class as the
        # per-marker road scan fixed earlier — cheap call, murderous frequency.
        base_pad = self._place_cull_pad()
        show_nat = self._show_nations.get()
        show_camp = self._show_camps.get()
        want_labels = self._lbl_var.get()
        edit_on = self._place_edit.get()
        sel_i = self._place_sel
        w2s = self._world_to_screen
        kx, cx, ky, cy = self._screen_xform()      # affine: sx = x*kx + cx, sy = y*ky + cy
        for pi, pl in enumerate(self._places):
            _p = pl["pos"]
            sx = _p[0] * kx + cx
            sy = _p[1] * ky + cy
            kind = pl["kind"]
            sel = (pi == sel_i)
            # Cull markers wholly outside the viewport — a big win on large maps zoomed in, where most
            # placements are off-screen (each one is several canvas items rebuilt every frame). HQ is
            # never culled (its camera link/marker may reach back on-screen); circle/rect use their own
            # screen radius as the pad so a large zone straddling the edge still draws.
            if not sel and kind != "hq":
                pad = base_pad
                if kind == "circle":
                    rad = pl["extra"].get("radius", 0.0) or 0.0
                    pad = max(pad, abs(rad * kx) + 8)
                elif kind == "rect":
                    pad = max(pad, abs((pl["extra"].get("w", 0.0) or 0.0) / 2.0 * kx) + 8,
                              abs((pl["extra"].get("h", 0.0) or 0.0) / 2.0 * ky) + 8)
                if sx < -pad or sy < -pad or sx > cw + pad or sy > ch + pad:
                    continue
            outline = "#ffffff" if sel else "#001018"
            ow = 2 if sel else 1
            col = _PLACE_COL_HEX.get(kind) or _PLACE_COL_HEX["unknown"]

            # ── the footprint / linkage layer (drawn UNDER the icon) ────────────────────────
            if kind in ("hq", "depot"):
                ang = self._facing_screen_angle(pl["pos"][0], pl["pos"][1], self._place_facing(pl))
                if kind == "hq" and sel and edit_on and pl["extra"].get("cam"):
                    R = self._cam_ring_radius(pl)                       # camera-orbit ring (selected + edit only)
                    rxp = abs(self._world_to_screen(pl["pos"][0] + R, pl["pos"][1])[0] - sx)
                    ryp = abs(self._world_to_screen(pl["pos"][0], pl["pos"][1] + R)[1] - sy)
                    c.create_oval(sx - rxp, sy - ryp, sx + rxp, sy + ryp, outline="#6a86ab", dash=(4, 3))
                if kind == "hq" and pl["extra"].get("cam"):             # dashed link to resting camera
                    cxs, cys = self._world_to_screen(*pl["extra"]["cam"][:2])
                    c.create_line(sx, sy, cxs, cys, fill="#6688aa", width=1, dash=(3, 2))
                    c.create_rectangle(cxs - 3, cys - 3, cxs + 3, cys + 3, outline="#88bbff", fill="#22364a")
                    c.create_text(cxs, cys - 8, text=t("map.cam"), fill="#88bbff", font=_F_MAIN)
                # facing tick, poking out past the icon edge
                fr = max(isz, 16)
                c.create_line(sx + fr * 0.45 * math.cos(ang), sy + fr * 0.45 * math.sin(ang),
                              sx + fr * 0.80 * math.cos(ang), sy + fr * 0.80 * math.sin(ang),
                              fill="#ffd24a", width=2)
            elif kind in ("circle", "rect"):
                # A zone's SIZE is the whole point of it, so the real footprint is always drawn even
                # when icons are off — the icon only marks the centre.
                if kind == "circle":
                    rad = pl["extra"].get("radius", 0.0) or 0.0
                    px = abs(self._world_to_screen(pl["pos"][0] + rad, pl["pos"][1])[0] - sx)
                    c.create_oval(sx - px, sy - px, sx + px, sy + px, outline=col, width=ow)
                else:
                    w = (pl["extra"].get("w", 0.0) or 0.0) / 2.0
                    h = (pl["extra"].get("h", 0.0) or 0.0) / 2.0
                    pxw = abs(self._world_to_screen(pl["pos"][0] + w, pl["pos"][1])[0] - sx)
                    pxh = abs(self._world_to_screen(pl["pos"][0], pl["pos"][1] + h)[1] - sy)
                    c.create_rectangle(sx - pxw, sy - pxh, sx + pxw, sy + pxh, outline=col, width=ow)

            # ── the marker itself ──────────────────────────────────────────────────────────
            ico = self._role_icon(pl, isz, show_nat, show_camp)
            if ico:
                if sel:
                    h = isz // 2 + 3
                    c.create_rectangle(sx - h, sy - h, sx + h, sy + h, outline="#ffffff", width=2)
                c.create_image(sx, sy, image=ico)
            else:
                # Icons off (or unavailable): the original dot rendering. An outline on a 4 px dot is
                # near-invisible but almost DOUBLES what the canvas costs to repaint (measured: 600
                # dots + terrain, 45.9 ms outlined vs 24.1 ms without), so only the selected one —
                # where the outline is what makes it stand out — pays for it.
                r = 6 if sel else 4
                if sel:
                    c.create_oval(sx - r, sy - r, sx + r, sy + r, fill=col, outline=outline, width=ow)
                else:
                    c.create_oval(sx - r, sy - r, sx + r, sy + r, fill=col, outline="")

            # ── the text layer ─────────────────────────────────────────────────────────────
            if kind == "hq":
                c.create_text(sx, sy + max(isz, 14) // 2 + 7, text=pl["label"],
                              fill="#ffd0d0", font=_F_MAIN)
            elif kind in ("ville", "montagne") and want_labels and pl["label"]:
                c.create_text(sx + max(isz, 8) // 2 + 3, sy, text=pl["label"], fill="#a8e0c0",
                              font=_F_MAIN, anchor="w")

    # ── save / revert ─────────────────────────────────────────────────────────
    def _has_pending_edits(self):
        """Any unsaved editor state. Switching map or scenario throws ALL of it away (the SDB is
        re-read per map, the KDT and start camera per scenario), so the prompt has to ask about all
        of it -- _dirty alone only covers the scenario, which used to let painted terrain, a moved
        start camera and an edited capture mesh vanish without a word."""
        return bool(self._dirty
                    or getattr(self, "_campath_dirty", False)
                    or getattr(self, "_kdt_dirty", False)
                    or (getattr(self, "_sdb", None) or {}).get("dirty"))

    def _confirm_discard(self):
        if not self._has_pending_edits():
            return True
        return ui_util.confirm(self, t("map.discard_changes"),
                                   t("map.scenario_has_unsaved_edits_discard"))

    def _revert(self):
        if self._scn is not None:
            self._restore_selection()   # reload what is LOADED, not what the boxes point at
            self._dirty = self._campath_dirty = self._kdt_dirty = False
            if getattr(self, "_sdb", None):
                self._sdb["dirty"] = False
            self._on_scn_change(force=True)

    def _save(self):
        """Stage every pending map change into the mod project, then flush it to the project's dat(s).
        Like the other editor windows, this writes ONLY into output/editor_mods/<mod>/... — never the
        live game. Collected here: the scenario (placements / depots / HQ), the warmup start camera, the
        painted AI-terrain SDB layer, the edited capture mesh (KDT), plus — staged earlier by "Apply
        game modes" — the lobby TMultiMapInfo in the gameplay dat. Deploy to the game from the hub."""
        staged = []
        # scenario + embedded placement NDF (only when there are scenario/placement edits)
        if self._scn is not None and self._dirty:
            # The path the scenario was READ from -- never the comboboxes. The widgets show where
            # the user is pointing, which is not always what is loaded (a cancelled discard prompt,
            # or a jump driven from another tab); writing loaded bytes to a pointed-at path is how a
            # scenario ends up saved into the wrong map.
            vp = self._scn_vpath
            scn = (self._scn_src or ("", ""))[1]
            if not vp:                       # nothing was ever loaded -- nothing to write back
                ui_util.error(self, t("map.no_scenario"), t("map.load_scenario_first"))
                return
            try:
                # re-embed the (possibly edited) placement NDF; byte-identical when unchanged
                if self._pndf is not None:
                    self._scn["ndf_data"] = _pad4(ndfbin.write(self._pndf,
                                                               compress=self._pndf.is_compressed))
                data = scenario_mod.write(self._scn)
                scenario_mod.read(data)        # sanity: must reparse
            except Exception as e:
                ui_util.error(self, t("common.save_failed"), t("map.could_not_serialize_scenario_e", e=e))
                return
            self.project.set_raw("maps", vp, data)
            staged.append(t("map.scn_scenario_placements", scn=scn))
        # warmup campath (the real start camera; PositionCamera is inert)
        if self._campath_dirty and self._campath is not None and self._campath_vpath:
            try:
                self.project.set_raw("maps", self._campath_vpath,
                                     ndfbin.write(self._campath, compress=self._campath.is_compressed))
            except Exception as e:
                ui_util.error(self, t("common.save_failed"), t("map.could_not_serialize_start_camera", e=e))
                return
            staged.append(t("map.start_camera"))
        # painted AI-terrain SDB layer (unified codec, edit-in-place -> mapinfo.win buffer4)
        if self._sdb and self._sdb.get("dirty"):
            try:
                new_sdb = sdb_mod.serialize(self._sdb["parsed"])
                new_win = sdb_mod.replace_buffer4(self._sdb["win"], new_sdb)
            except Exception as e:
                ui_util.error(self, t("common.save_failed"), t("map.could_not_rebuild_sdb_layer", e=e))
                return
            self.project.set_raw("maps", self._sdb["win_vpath"], new_win)
            self._sdb["win"] = new_win
            staged.append(t("map.ai_terrain_sdb"))
        # edited capture mesh (KDT verts; Eugen's tree preserved) — shelved, only when dirty
        if self._kdt_dirty and self._kdt_vpath and self._kdt_bytes is not None:
            try:
                new_kdt = kdt_mod.encode_mesh(self._kdt_bytes, self._kdt_world)
            except Exception as e:
                ui_util.error(self, t("common.save_failed"), t("map.could_not_encode_capture_mesh", e=e))
                return
            self.project.set_raw("maps", self._kdt_vpath, new_kdt)
            self._kdt_bytes = new_kdt
            staged.append(t("map.capture_mesh_kdt"))
        # the lobby/game-modes change (globals.cpp) was already mark_dirty'd by "Apply game modes".
        if not self.project.is_dirty():
            ui_util.info(self, t("map.save_mod"), t("common.no_pending_changes_save"))
            return
        try:
            written = self.project.save_all()
        except Exception as e:
            ui_util.error(self, t("common.save_failed"),
                                 t("map.could_not_write_mod_s", e=e))
            return
        # clear local dirty flags; re-open the read source so further edits build on the saved state
        self._dirty = self._campath_dirty = self._kdt_dirty = False
        if self._sdb:
            self._sdb["dirty"] = False
        try:
            self._dm = edata.open_dat(self._maps_src())
        except Exception:
            pass
        if self._on_change:
            self._on_change()
        self._redraw()
        body = (t("map.saved_into_mod") + "\n  ".join(staged)) if staged \
            else t("map.saved_staged_lobby_game_mode")
        ui_util.info(self, t("common.saved"),
                            t("map.body_written_dat_file_s",
                              body=body, written="\n  ".join(written)))


def main():
    """Standalone dev entry point. The editor now requires a ModProject, so build/load a throwaway dev
    project (output/editor_mods/_mapeditor_dev) from settings.json — that way `python map_editor.py`
    still runs for testing, but it always saves into a mod folder, never the live game."""
    from ruse_mod_engine import mod_project as _mp
    gr, working = "", REPO
    try:
        with open(os.path.join(REPO, "settings.json"), encoding="utf-8") as f:
            _s = json.load(f) or {}
        gr = _s.get("game_root", "") or ""
        working = _s.get("working_dir", REPO) or REPO
    except Exception:
        pass
    mods_dir = os.path.join(working, "output", "editor_mods")
    backup = os.path.join(working, "output", "backups", "public")
    os.makedirs(mods_dir, exist_ok=True)
    folder = os.path.join(mods_dir, "_mapeditor_dev")
    if os.path.isdir(folder):
        proj = _mp.ModProject.load(folder, gr, backup)
    else:
        proj = _mp.ModProject.create(mods_dir, "_mapeditor_dev", "public", gr, backup)
    # The editor is now a Frame (embedded in the Mod Manager); host it in a plain window for dev use.
    root = tk.Tk()
    root.title(t("map.r_u_s_e_map", name=proj.name))
    root.geometry("1180x800")
    root.minsize(980, 660)
    root.configure(background=_R_BG)
    MapEditorWindow(root, proj).pack(fill="both", expand=True)
    root.mainloop()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()   # terrain decode spawns a worker pool — see mod_manager.py
    main()

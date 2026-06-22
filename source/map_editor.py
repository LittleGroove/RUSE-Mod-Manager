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
import math
import os
import re
import struct
import sys
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
from ruse_mod_engine import edata, scenario as scenario_mod, kdt as kdt_mod, ndfbin, sdb as sdb_mod  # noqa: E402
import pil_log                                       # noqa: E402  (tags PIL DEBUG as "Map editor")
from i18n import t                                    # noqa: E402
import ui_util                                        # noqa: E402  — language-aware widget sizing

try:
    from PIL import Image, ImageTk, ImageDraw
    _HAVE_PIL = True
except Exception:
    _HAVE_PIL = False

# High-def terrain decode (issue #8) — optional: needs numpy + Pillow.  Guarded so the editor still
# runs (falling back to the baked minimap) if the codec/numpy isn't available.
try:
    from ruse_mod_engine import terrain_codec as _terrain_codec
except Exception:
    _terrain_codec = None

import threading
import tempfile

# ── Theme (mirrors mod_manager.py "Field Operations" palette) ──────────────────
_R_BG        = "#08101c"
_R_BG_PANEL  = "#0e1a2a"
_R_BG_WIDGET = "#060d18"
_R_BORDER    = "#243a5c"
_R_GOLD      = "#c8a020"
_R_GOLD_BRT  = "#e0c030"
_R_TEXT      = "#ccd8e8"
_R_TEXT_DIM  = "#3e5878"
_R_SEL_BG    = "#1a3060"
_F_MAIN = ("Courier New", 9)
_F_BOLD = ("Courier New", 9, "bold")

# Minimaps fall back to the bundled public sources only for read-only display when no game root is set;
# the editable map dat itself always comes from the mod project (read_source: mod folder → backup → game).
_DEFAULT_MAPS_ROOT = os.path.join(REPO, "Claude", "sources", "RUSE-game-public")


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
# + hash16 (16, NOT validated at load — embedded CPython) + zlib(Python-2.6 marshal blob).
# Reading: unpack → zlib.decompress → xdis.unmarshal.load_code(magic=62211) → uncompyle6.decompile
# → readable Python source. Writing source-text back to XYZ0 needs Python-2 source compilation,
# which doesn't have a clean Python-3 shim (compile()/marshal in CPython 3 emit Python-3 bytecode
# that the engine's embedded Python 2 can't load). So the editor saves drafts as plain .py text
# into test_output/script_drafts/ for external compilation; full round-trip is a follow-up phase.
_XYZ_MAGIC = b"XYZ0\n"
_XYZ_VER = b"\x0d\xf2\xb3\x00"
_XYZ_PY_MAGIC_INT = 62211          # CPython 2.6 marshal magic; matches what the engine ships
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
def _xyz_decompile_to_source(marshal_bytes):
    """Return a Python-source string from a Python-2.6 marshal blob, or raise.
    Uses xdis + uncompyle6 (both in .venv per [[feedback-venv-python]])."""
    import io as _io
    from xdis import unmarshal as _um
    from uncompyle6.main import decompile as _decompile
    code = _um.load_code(marshal_bytes, _XYZ_PY_MAGIC_INT)
    buf = _io.StringIO()
    _decompile(code, bytecode_version=(2, 6), out=buf, magic_int=_XYZ_PY_MAGIC_INT)
    return buf.getvalue()
def _xyz_pack(marshal_bytes, hash16=b"\x00" * 16):
    """Wrap a Python-2 marshal blob in the XYZ0 container. The 16-byte hash isn't
    validated at load (proven in tools/_ai_xyz_repack.py), so a zero hash is accepted.
    Returns the bytes ready to write into IA_Common.dat via project.set_raw('scripts',...)."""
    import zlib as _zlib
    if len(hash16) != 16:
        raise ValueError("hash16 must be exactly 16 bytes")
    comp = _zlib.compress(marshal_bytes, 9)
    return _XYZ_MAGIC + _XYZ_VER + len(marshal_bytes).to_bytes(3, "big") + hash16 + comp
def _py27_worker_path():
    """Absolute path to tools/xyz_compile.py — the Python-2.7 compile worker."""
    import os as _os
    return _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                         "tools", "xyz_compile.py")
def _py27_interpreter_path():
    """Absolute path of the bundled Python 2.7 interpreter, or None if not present.
    Looks in tools/python27/python.exe per docs/map_editor/setup_py27.md."""
    import os as _os
    p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                      "tools", "python27", "python.exe")
    return p if _os.path.isfile(p) else None
def _xyz_compile_source(source_text, timeout=20):
    """Compile a .xyz Python-2.7 source string to marshal bytes using the bundled Py2
    interpreter. Raises FileNotFoundError when tools/python27/python.exe is missing
    (the editor's caller catches this and steers the user to setup_py27.md), or
    RuntimeError with stderr on compile failure."""
    import subprocess as _sp
    py27 = _py27_interpreter_path()
    if py27 is None:
        raise FileNotFoundError("tools/python27/python.exe not found — see docs/map_editor/setup_py27.md")
    proc = _sp.run([py27, _py27_worker_path()],
                   input=source_text.encode("utf-8"),
                   capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(err or "Python 2 worker failed with no diagnostic")
    return proc.stdout

# Starter Python-2 .xyz template for the Script Editor's "create a new script" path. Skeletal —
# imports + a single VariableCamp for the player + a stub win condition the user is expected to
# customise. Two-space indent matches the decompiled examples in Claude/sources/scripts/.
_SCRIPT_TEMPLATE = '''# Minimal .xyz starter — paired with this scenario.
# See [the help/reference pane] for objective, trigger, and endless-wave patterns.
import leveldesign.camps
import leveldesign.descriptor
import leveldesignsolo.descriptor
import leveldesignsolo.contrainte_unit
import _enum_for_game_play

Camp_Player = leveldesign.camps.VariableCamp(
    Alliance = 1, AllianceName = u'', CampNumber = -1,
    Couleur = defines.colors.Color_01_Blue,
    Difficulte = _enum_for_game_play.TDifficultes.Normal,
    LocalizedName = None, Message = u'',
    Nationalite = _enum_for_game_play.Nationalite.EU,
    NiveauIA = _enum_for_game_play.NiveauIA.Player,
    PartageCamion = True, PlayerNameChallenge = True,
    Profil = _enum_for_game_play.TProfils.ProfilStandard)

Camp_Enemy = leveldesign.camps.VariableCamp(
    Alliance = 2, AllianceName = u'', CampNumber = -1,
    Couleur = defines.colors.Color_02_Red,
    Difficulte = _enum_for_game_play.TDifficultes.Normal,
    LocalizedName = None, Message = u'Enemy',
    Nationalite = _enum_for_game_play.Nationalite.Allemagne,
    NiveauIA = _enum_for_game_play.NiveauIA.Scripted,
    PartageCamion = False, PlayerNameChallenge = False,
    Profil = _enum_for_game_play.TProfils.ProfilStandard)

# TODO: define objectives + win/lose conditions.
'''

# Reference content for the script editor's right pane. Entries are tuples whose first
# element selects a renderer:
#   ('section', title)
#       → orange/gold section divider header.
#   ('doc', title, body)
#       → explanatory paragraph; no paste-able snippet.
#   ('recipe', title, body, paste_code)
#       → "what to change in existing code" with an insertable example.
#   ('snippet', title, body, paste_code)
#       → paste-able skeleton for adding NEW content.
#   ('table', title, body, rows)
#       → enum/value lookup table; rows are (key, description).
#
# Content is grounded in a corpus scan of every decompiled .xyz/.py in
# Claude/sources/scripts/ — value counts per enum are noted below where revealing.
# (User-asked: the script editor's primary job is editing the PARAMETERS of these
# factory calls, not writing arbitrary control flow; the reference reflects that.)
_SCRIPT_REFERENCE = [
    # ── Overview ─────────────────────────────────────────────────────────────────
    ('section', "HOW THESE SCRIPTS WORK"),
    ('doc', "Mission tree", (
        "A RUSE script is a tree of `Descriptor*` instances. The root descriptor runs at "
        "match start; each parent runs its `SubActions` either in order "
        "(`DescriptorSequential`), in parallel (`DescriptorSimultaneous`), or as a race "
        "(`DescriptorCompetition`). Branches happen via `DescriptorIfThenElse` and "
        "`DescriptorOnContrainte`. Variables persist state across the tree.")),
    ('doc', "Tags = scenario references", (
        "`TagPosition('name')`, `TagUnit('name')` and `TagZoneDetection('name')` pull "
        "named entities directly from the paired .scenario file (the same items the "
        "map editor shows). Tags are how scripts reference where to spawn, where to "
        "attack, which units to watch. To add a new tag target: place the entity in "
        "the .scenario with `+ Placement`, give it a `Name`, then `TagPosition('that name')` "
        "in the script.")),
    ('doc', "Camps drive everything", (
        "`VariableCamp(...)` defines a faction slot. The slot indexes correspond to the "
        "`Camp` integer on placements: 1st VariableCamp = the `Camp = null` placements "
        "(Team 1); 2nd VariableCamp = `Camp = 1` (Team 2); 3rd = `Camp = 2` (Team 3); "
        "and so on. Exception: an explicit `CampNumber = N` argument pins the slot "
        "regardless of declaration order (see m08_allemagne for shipped examples).")),

    # ── Common edits ─────────────────────────────────────────────────────────────
    ('section', "COMMON EDITS"),
    ('recipe', "Change enemy difficulty", (
        "Find the enemy's `VariableCamp(...)` and change `Difficulte`. Observed across "
        "shipped scripts: Facile (5), Normal (344), Difficile (162). Difficile gives the "
        "AI faster production, larger force sizes, more aggressive behavior."),
        "Difficulte = _enum_for_game_play.TDifficultes.Difficile"),
    ('recipe', "Change enemy faction (nationality)", (
        "In a `VariableCamp(...)`, change `Nationalite`. This sets the unit roster, "
        "Rusopedia and ruse cards the camp uses."),
        "Nationalite = _enum_for_game_play.Nationalite.Allemagne"),
    ('recipe', "Change AI personality", (
        "`Profil` swaps the AI's strategic preset. ProfilStandard (445) is balanced; "
        "ProfilRush (32) goes aggressive early; ProfilAvion (23) prioritises planes; "
        "ProfilTortue (11) turtles defensively."),
        "Profil = _enum_for_game_play.TProfils.ProfilRush"),
    ('recipe', "Change camp colour on the minimap", (
        "Pick a `defines.colors.Color_*` (see Quick Reference below). Color_Neutre is "
        "for non-combat factions; pick one of the numbered colours for visible teams."),
        "Couleur = defines.colors.Color_05_Orange"),
    ('recipe', "Disable a camp's truck-sharing with allies", (
        "`PartageCamion = False` means this camp's depots ONLY feed its own units, even "
        "when same-Alliance. Set True for normal MP alliance economy."),
        "PartageCamion = False"),
    ('recipe', "Tweak a mission timer", (
        "Wherever a `DescriptorWaitDuree(Duree = N.0, ...)` appears, change the `Duree` "
        "(seconds). Most missions use 30-600 second timers."),
        "Duree = 120.0"),

    # ── Camp setup snippets ──────────────────────────────────────────────────────
    ('section', "CAMP SETUP — VariableCamp templates"),
    ('snippet', "Player camp (human)", (
        "Marks a VariableCamp as the human player. NiveauIA = Player is the toggle; "
        "PlayerNameChallenge = True replaces Message with the player's profile name. "
        "Convention: the LAST VariableCamp in a mission script is usually the player."),
        ("leveldesign.camps.VariableCamp(\n"
         "    Alliance = 1, AllianceName = u'', CampNumber = -1,\n"
         "    Couleur = defines.colors.Color_01_Blue,\n"
         "    Difficulte = _enum_for_game_play.TDifficultes.Normal,\n"
         "    LocalizedName = None, Message = u'',\n"
         "    Nationalite = _enum_for_game_play.Nationalite.EU,\n"
         "    NiveauIA = _enum_for_game_play.NiveauIA.Player,\n"
         "    PartageCamion = True, PlayerNameChallenge = True,\n"
         "    Profil = _enum_for_game_play.TProfils.ProfilStandard)\n")),
    ('snippet', "Scripted AI enemy", (
        "NiveauIA = Scripted runs the descriptors in this file rather than the strategic "
        "AI. Alliance = 2 puts the camp on the enemy side; Message is the on-screen team "
        "name during the pre-game splash."),
        ("leveldesign.camps.VariableCamp(\n"
         "    Alliance = 2, AllianceName = u'', CampNumber = -1,\n"
         "    Couleur = defines.colors.Color_02_Red,\n"
         "    Difficulte = _enum_for_game_play.TDifficultes.Normal,\n"
         "    LocalizedName = None, Message = u'Afrika Korps',\n"
         "    Nationalite = _enum_for_game_play.Nationalite.Allemagne,\n"
         "    NiveauIA = _enum_for_game_play.NiveauIA.Scripted,\n"
         "    PartageCamion = False, PlayerNameChallenge = False,\n"
         "    Profil = _enum_for_game_play.TProfils.ProfilStandard)\n")),
    ('snippet', "Neutral / allied AI", (
        "Color_Neutre + Alliance = 1 (same as player) marks a non-combat ally that "
        "shares the map. PartageCamion = True lets supply trucks cross alliance lines."),
        ("leveldesign.camps.VariableCamp(\n"
         "    Alliance = 1, AllianceName = u'', CampNumber = -1,\n"
         "    Couleur = defines.colors.Color_Neutre,\n"
         "    Difficulte = _enum_for_game_play.TDifficultes.Normal,\n"
         "    LocalizedName = None, Message = u'',\n"
         "    Nationalite = _enum_for_game_play.Nationalite.EU,\n"
         "    NiveauIA = _enum_for_game_play.NiveauIA.Scripted,\n"
         "    PartageCamion = True, PlayerNameChallenge = False,\n"
         "    Profil = _enum_for_game_play.TProfils.ProfilStandard)\n")),

    # ── Objectives & win/lose ────────────────────────────────────────────────────
    ('section', "OBJECTIVES — WIN / LOSE / ENDLESS"),
    ('snippet', "Win — destroy all enemy units", (
        "ContrainteOnUnitTeam returns 0 when Camp_Enemy has no units left. "
        "DescriptorDeclencheVictoireChallenge fires the victory screen when its "
        "Contrainte is met."),
        ("victory_cond = leveldesignsolo.contrainte_unit.ContrainteOnUnitTeam(\n"
         "    Camp = Camp_Enemy, OperatorType = 0)\n"
         "win = leveldesign.descriptor.DescriptorDeclencheVictoireChallenge(\n"
         "    Contrainte = victory_cond)\n")),
    ('snippet', "Win — survive a timer", (
        "Wait N seconds, then win. Use DescriptorCompetition with a parallel lose-check "
        "to make the timer race the player's survival."),
        ("survive = leveldesign.descriptor.DescriptorSequential(\n"
         "    SubActions = [\n"
         "        leveldesign.descriptor.DescriptorWaitDuree(Duree = 300.0),\n"
         "        leveldesign.descriptor.DescriptorDeclencheVictoireChallenge()])\n")),
    ('snippet', "Lose — player loses all units", (
        "Mirror of the destroy-enemy condition. ContrainteOnUnitTeam on the player's "
        "camp triggers when their unit count drops to 0."),
        ("lose_cond = leveldesignsolo.contrainte_unit.ContrainteOnUnitTeam(\n"
         "    Camp = Camp_Player, OperatorType = 0)\n"
         "lose = leveldesign.descriptor.DescriptorDeclencheDefaiteChallenge(\n"
         "    Contrainte = lose_cond)\n")),
    ('snippet', "Endless mode (no win condition)", (
        "Omit DescriptorDeclencheVictoireChallenge entirely. Pair an infinite "
        "DescriptorWaitDuree → DescriptorCreateUnit loop via DescriptorSequential "
        "(or recursive SubActions) for escalating waves. Player wins only by quitting "
        "to menu, loses by dying."),
        ("endless_root = leveldesign.descriptor.DescriptorSequential(\n"
         "    SubActions = [wave_descriptor, wave_descriptor, wave_descriptor])\n")),

    # ── Triggers & timing ────────────────────────────────────────────────────────
    ('section', "TRIGGERS & TIMING"),
    ('snippet', "Wait N seconds", (
        "Sleep for a fixed duration before running the next descriptor. Most common "
        "timer in shipped scripts (6008 calls)."),
        "leveldesign.descriptor.DescriptorWaitDuree(Duree = 60.0)"),
    ('snippet', "Wait until a condition is true", (
        "DescriptorWaitCondition blocks until its Condition argument returns true. "
        "Pair with ConditionVariableInteger, ConditionUnitGroup, or one of the "
        "leveldesignsolo.condition.* classes."),
        ("leveldesign.descriptor.DescriptorWaitCondition(\n"
         "    Condition = my_condition)")),
    ('snippet', "Trigger when units enter a zone", (
        "ConditionDetectUnitDansZoneDetection fires when a unit of Camp Camp_Player "
        "enters the zone bound to TagZoneDetection('zone_name')."),
        ("zone_check = leveldesignsolo.condition.ConditionDetectUnitDansZoneDetection(\n"
         "    Zone = leveldesignsolo.helper.TagZoneDetection('zone_attack'),\n"
         "    Camp = Camp_Player, OperatorType = 0)\n")),
    ('snippet', "Run actions sequentially", (
        "Most common flow construct (9724 calls). Each SubAction completes before the "
        "next starts."),
        ("leveldesign.descriptor.DescriptorSequential(\n"
         "    SubActions = [action_a, action_b, action_c])")),
    ('snippet', "Run actions in parallel", (
        "All SubActions start at once. Parent completes when ALL children complete."),
        ("leveldesign.descriptor.DescriptorSimultaneous(\n"
         "    SubActions = [action_a, action_b])")),
    ('snippet', "Race actions (first to finish)", (
        "All SubActions start; parent completes as soon as ANY child finishes. Useful "
        "for 'survive OR destroy enemy = win' mission setups."),
        ("leveldesign.descriptor.DescriptorCompetition(\n"
         "    SubActions = [survive_timer, destroy_enemy])")),
    ('snippet', "Branch on condition", (
        "Run different SubActions depending on a Condition's truth value."),
        ("leveldesign.descriptor.DescriptorIfThenElse(\n"
         "    Condition = my_cond,\n"
         "    SubActionsIfTrue = [yes_action], SubActionsIfFalse = [no_action])")),

    # ── Spawn & move ─────────────────────────────────────────────────────────────
    ('section', "SPAWN & UNIT CONTROL"),
    ('snippet', "Spawn units at a position", (
        "Creates N copies of each UnitDescriptor at TagPosition. Camp owns them; "
        "AddToUnitGroup attaches them to a VariableUnitGroup for later orders."),
        ("leveldesignsolo.production.DescriptorCreateUnit(\n"
         "    UnitDescriptors = [Unit_Carro_Armato_M11_39],\n"
         "    Position = leveldesignsolo.helper.TagPosition('spawn_01'),\n"
         "    Camp = Camp_Enemy,\n"
         "    AddToUnitGroup = wave_group)\n")),
    ('snippet', "Move a unit group toward a position", (
        "Orders Group's units to a destination. OperatorType controls formation."),
        ("leveldesignsolo.missions.DescriptorMove(\n"
         "    Group = wave_group,\n"
         "    Position = leveldesignsolo.helper.TagPosition('attack_target'))")),
    ('snippet', "Attack a target", (
        "Group attacks the nearest enemy unit/structure to Position."),
        ("leveldesignsolo.missions.DescriptorAttack(\n"
         "    Group = wave_group, Camp = Camp_Player,\n"
         "    Position = leveldesignsolo.helper.TagPosition('depot_01'))")),
    ('snippet', "Defend a zone", (
        "Group holds a zone, engaging anything that enters."),
        ("leveldesignsolo.missions.DescriptorDefendZone(\n"
         "    Group = defender_group,\n"
         "    Zone = leveldesignsolo.helper.TagZoneDetection('zone_defend'))")),
    ('snippet', "Change camp ownership", (
        "Reassigns every unit in Group to NewCamp. Useful for capture events."),
        ("leveldesignsolo.units.DescriptorChangeCamp(\n"
         "    Group = captured_group, NewCamp = Camp_Player)")),
    ('snippet', "Kill all units in a group", (
        "Removes them — cause-of-death goes on the player's stat ledger if you set it."),
        ("leveldesignsolo.units.DescriptorKillAllUnit(\n"
         "    Group = wave_group,\n"
         "    CauseMort = _enum_for_game_play.CauseMort.NonSpecifiee)")),

    # ── Variables & state ────────────────────────────────────────────────────────
    ('section', "VARIABLES & STATE"),
    ('snippet', "Declare an integer variable", (
        "Most scripts track wave counts, kill counts, mission stage via integer vars."),
        "wave_count = leveldesign.variable.VariableInteger(InitialValue = 0)"),
    ('snippet', "Set / increment an integer", (
        "DescriptorSetVariableInteger writes a new value; "
        "DescriptorIncrementeVariableInteger adds Delta."),
        ("leveldesign.descriptor.DescriptorIncrementeVariableInteger(\n"
         "    Variable = wave_count, Delta = 1)")),
    ('snippet', "Check an integer", (
        "Use with DescriptorWaitCondition / DescriptorIfThenElse. OperatorIntegerEqual / "
        "MoreOrEqual / StrictlyMore / LessOrEqual are the common comparators."),
        ("leveldesign.condition.ConditionVariableInteger(\n"
         "    Variable = wave_count,\n"
         "    Operator = leveldesign.operator.OperatorIntegerMoreOrEqual(Value = 5))")),

    # ── Quick reference tables ───────────────────────────────────────────────────
    ('section', "QUICK REFERENCE — enum values"),
    ('table', "Nationalite", "Faction roster (unit pool + Rusopedia + cards):", [
        ("Allemagne", "German / Wehrmacht"),
        ("EU",        "Western Allies (US, generic Allied)"),
        ("RU",        "British / Royaume-Uni"),
        ("URSS",      "Soviet Union"),
        ("France",    "Free French"),
        ("Italie",    "Italian (Regio Esercito)"),
        ("Japon",     "Imperial Japan"),
    ]),
    ('table', "NiveauIA", "Camp control mode:", [
        ("Player",   "Human player slot — the camp picks ruse cards, manages economy"),
        ("Scripted", "Story AI — executes descriptors from THIS .xyz file (Strategic / Tactic are NOT real values)"),
    ]),
    ('table', "TDifficultes", "Difficulty applied to AI camps:", [
        ("Facile",    "Easy (rare in shipped — 5 occurrences)"),
        ("Normal",    "Default (344 occurrences)"),
        ("Difficile", "Hard (162 — used for campaign veterans / Operations)"),
    ]),
    ('table', "TProfils", "AI personality preset:", [
        ("ProfilStandard", "Balanced (445 — default)"),
        ("ProfilRush",     "Aggressive early (32)"),
        ("ProfilAvion",    "Plane-focused (23)"),
        ("ProfilTortue",   "Defensive turtle (11)"),
    ]),
    ('table', "defines.colors.Color_*", "Camp colour on minimap + UI:", [
        ("Color_01_Blue",   "(player default)"),
        ("Color_02_Red",    "(enemy default)"),
        ("Color_03_Teal",   ""),
        ("Color_04_Purple", ""),
        ("Color_05_Orange", ""),
        ("Color_06_Pink",   ""),
        ("Color_07_Green",  ""),
        ("Color_08_Brown",  ""),
        ("Color_09_Black",  ""),
        ("Color_Neutre",    "(neutral / non-combatant grey)"),
    ]),
    ('table', "CauseMort", "Cause of death tag (for kill descriptors):", [
        ("NonSpecifiee",       "Generic — most common"),
        ("Capture",            "Captured (buildings)"),
        ("CaptureFake",        "Fake-capture (fake buildings)"),
        ("EliminationCamp",    "Camp surrendered"),
        ("Stress_Moyen",       "Medium stress kill"),
        ("Stress_Lourd",       "Heavy stress kill"),
        ("Stress_ExtraLourd",  "Extra-heavy stress kill"),
        ("Stress_UltraLourd",  "Ultra-heavy stress kill"),
    ]),
    ('table', "OperatorType", "Constraint matching mode (most ContrainteOn* calls):", [
        ("0", "Match — equality / contains (965 occurrences)"),
        ("1", "Negation — not-match (17 occurrences)"),
    ]),
]
# Backwards-compat name retained briefly so any older code paths that imported the
# legacy list don't break — points at the reference list so 'insert' still gets a body.
_SCRIPT_SNIPPETS = [(e[1], e[3] if len(e) > 3 and isinstance(e[3], str) else "")
                    for e in _SCRIPT_REFERENCE if e[0] in ("snippet", "recipe")]

# Auto-snap-to-roads: world-unit perpendicular distance a depot / HQ sits from the nearest road LINE.
# Measured from blitz (supercrossroads4) leveldesign_normal — the clean MP reference (map is 1,310,720u
# across): depots cluster tightly at ~11,478u (median, sd 797 over 12); the 2 HQs average ~17,003u.
# So the absolute numbers are large only because the maps are huge (~0.9% of the map width).
_ROAD_SNAP_OFFSET = {"depot": 11500.0, "hq": 17000.0}

# On-canvas placement icons (the map-editor "front-end vocabulary"). icons/map_icons/* are the
# palette of droppable things; map each placement KIND to its icon. depot=supply depot,
# hq=factory/admin building. Unit icons (tank/inf/plane/aa/at/arty/reco/truck) + gameplay
# (cards/nuke/range) are reserved for future per-unit/spawn placement.
ICON_DIR = os.path.join(REPO, "icons", "map_icons")
_PLACE_ICON = {"depot": "depot.png", "hq": "base2.png"}


def _camp_str(camp):
    """Decode a TGameDesignAddOn_Spawn `Camp` Int32 value into a human label.

    Encoding: null = Team 1; Camp = N (≥ 1) = Team N + 1; special values −1 (visible neutral)
    and −2 (Operations despawn). On a depot in an MP scenario, null specifically means
    despawned at match start; on any other Spawn null means Team 1 — the engine resolves
    by scenario type, the editor labels both readings so the user can see both.

    Known unknown: `Camp = 0` is never observed in shipped data. We label it "Team 1" by
    applying the N + 1 offset uniformly, but it could equally be "Camp = 0 ≡ null" or a
    distinct sentinel. See docs/map_editor/placements_and_roads.md §2 (Camp field semantics)
    for the candidate interpretations and the in-game test that would settle it."""
    if camp is None:
        return t("null -> Team 1  (or despawn for MP depots)")
    if camp == -1:
        return t("-1 -> neutral / visible in MP")
    if camp == -2:
        return t("-2 -> despawn (Operations)")
    return t("{camp} -> Team {team}", camp=camp, team=camp + 1)


def _pad4(b: bytes) -> bytes:
    """Pad to a 4-byte boundary — RUSE pads the embedded scenario NDF this way, so this makes
    a re-emitted (edited) NDF byte-identical to the original when nothing changed."""
    return b + b"\x00" * ((4 - len(b) % 4) % 4)


def _ndf_prop(ndf, inst, name):
    """NdfValue of property `name` on `inst` (resolved within the instance's class), or None."""
    p = ndf.prop_by_name_and_class(name, inst.class_index)
    return inst.get(p.index) if p is not None else None


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
        out.append({"item_idx": ii, "addon_idx": addon_idx, "kind": kind,
                    "pos": tuple(posv.raw), "rot": (rotv.raw if rotv else None),
                    "label": label, "extra": extra})
    return out


def _mapinfo_buffers(win_bytes, count=4):
    """First `count` length-prefixed buffers of mapinfo.win after the 48-byte header
    ('INFOIA\\r\\n'(8) md5(16) u32(=20) u32(=6) bbox(16)). buf[0]=road graph (TMapInfo+0x18),
    buf[3]=forest SDB (TMapInfo+0x30). RE'd from the loader FUN_1406d12b0."""
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
    quadtree (RE'd from parser FUN_1405613c0 + query FUN_140a6b7c0 + the en_foret evaluator
    FUN_1405ee560). These are the PRE-GENERATED zones where the game grants forest cover (GetIsEnForet),
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
    """Parse the EXACT road network from datasmap\\<map>\\mapinfo.win — authoritative, RE'd from the
    game's own graph code (loader FUN_1406d12b0, query FUN_14068ae90, length-finalizer FUN_14068bc70).

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

# KDT triangle->sector ranges, cracked via Frida dynamic analysis (contiguous index blocks per
# sector). Keyed by (map_dir, scenario). Each entry: (start_tri, end_tri_inclusive, a0_zone_idx).
# For maps without an entry, the overlay falls back to geometric (centroid-in-scenario-zone).
KDT_SECTOR_RANGES = {
    ("supercrossroads4", "leveldesign_normal"): [
        (0, 8, 0), (9, 13, 1), (14, 25, 4), (26, 41, 3), (42, 56, 8),
        (57, 71, 2), (72, 82, 5), (83, 88, 6), (89, 93, 7),
    ],
}

# Frida-dumped KDT meshes (tools/frida_kdt_verts.py) — the REAL VtxBufIdx verts + IndexBuffer the
# game's SIMD codec produces (which we don't decode offline). The on-disk verts (kf.verts) are a
# DIFFERENT array, so the indices only reconstruct correct triangles against these dumped verts.
_KDT_VERTDUMP_PATH = os.path.join(REPO, "test_output", "kdt_re", "kdt_verts_capture.jsonl")
_kdt_vertdumps_cache = None


def load_kdt_vertdumps():
    """Parse the Frida vert-dump jsonl → list of {bmin,bmax, world:[(x,y)], tris:[(a,b,c)]}.
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
        self._scn = None            # full scenario dict (editable source of truth)
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
        self._comp_rect = None      # base-pixel region the cached composite covers (viewport render)
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
        self._show_roads = tk.BooleanVar(value=True)
        # forest CONCEALMENT zones (mapinfo.win buffer4 SDB) — where the game grants forest cover
        self._forest = None         # {"cells": [(x0,y0,x1,y1)], "bbox": (...)}
        self._show_forest = tk.BooleanVar(value=True)
        # Editable SDB AI-terrain quadtree (paint/erase -> edit-in-place -> repack mapinfo.win), via the
        # unified codec ruse_mod_engine.sdb (issue #9), byte-identical safe. We edit buffer4 in
        # mapinfo.win — the game's runtime concealment/movement SDB (TSparseSpatialStateDatabaseStatic).
        # A Ghidra runtime audit (jpype_sdb_runtime_audit) proved the game queries ONLY two layer bits:
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
            messagebox.showerror(t("Pillow required"),
                                 t("The map editor needs Pillow (PIL).\n\npip install Pillow"),
                                 parent=self)
            return
        self._populate_maps()

    # ── layout ──────────────────────────────────────────────────────────────────
    def _build_ui(self):
        bar = tk.Frame(self, background=_R_BG_PANEL)
        bar.pack(side="top", fill="x")
        tk.Label(bar, text=t("Map:"), background=_R_BG_PANEL, foreground=_R_GOLD,
                 font=_F_BOLD).pack(side="left", padx=(8, 4), pady=6)
        self._map_cb = ttk.Combobox(bar, state="readonly", width=18, font=_F_MAIN)
        self._map_cb.pack(side="left", padx=4)
        self._map_cb.bind("<<ComboboxSelected>>", self._on_map_change)
        tk.Label(bar, text=t("Scenario:"), background=_R_BG_PANEL, foreground=_R_GOLD,
                 font=_F_BOLD).pack(side="left", padx=(10, 4))
        self._scn_cb = ttk.Combobox(bar, state="readonly", width=24, font=_F_MAIN)
        self._scn_cb.pack(side="left", padx=4)
        self._scn_cb.bind("<<ComboboxSelected>>", self._on_scn_change)
        tk.Checkbutton(bar, text=t("Flip Y"), variable=self._flip_y, command=self._invalidate_redraw,
                       background=_R_BG_PANEL, foreground=_R_TEXT, selectcolor=_R_BG_WIDGET,
                       font=_F_MAIN, activebackground=_R_BG_PANEL, activeforeground=_R_GOLD).pack(side="left", padx=10)
        tk.Checkbutton(bar, text=t("Labels"), variable=self._lbl_var, command=self._redraw,
                       background=_R_BG_PANEL, foreground=_R_TEXT, selectcolor=_R_BG_WIDGET,
                       font=_F_MAIN, activebackground=_R_BG_PANEL, activeforeground=_R_GOLD).pack(side="left")
        tk.Checkbutton(bar, text=t("Sectors"), variable=self._show_sectors, command=self._invalidate_redraw,
                       background=_R_BG_PANEL, foreground=_R_TEXT, selectcolor=_R_BG_WIDGET,
                       font=_F_MAIN, activebackground=_R_BG_PANEL, activeforeground=_R_GOLD).pack(side="left", padx=(6, 0))
        tk.Checkbutton(bar, text=t("Roads"), variable=self._show_roads, command=self._redraw,
                       background=_R_BG_PANEL, foreground=_R_TEXT, selectcolor=_R_BG_WIDGET,
                       font=_F_MAIN, activebackground=_R_BG_PANEL, activeforeground=_R_GOLD).pack(side="left", padx=(10, 0))
        tk.Button(bar, text=t("Reset view"), command=self._fit_view, background="#122030",
                  foreground=_R_TEXT, font=_F_BOLD, relief="flat").pack(side="left", padx=8)

        # edit toolbar (right side) — ONE save button writes every pending map change (placements,
        # SDB, KDT, lobby/game modes) into the mod project's dats, like the other editor windows.
        tk.Button(bar, text=t("Save to mod"), command=self._save, background="#122030",
                  foreground=_R_GOLD_BRT, font=_F_BOLD, relief="flat").pack(side="right", padx=(4, 8))
        tk.Button(bar, text=t("Revert"), command=self._revert, background="#122030",
                  foreground=_R_TEXT, font=_F_BOLD, relief="flat").pack(side="right", padx=4)
        # Sector MOVING is intentionally not exposed: sector capture shape is driven by the .kdt
        # (not rebuilt yet), so moving sector boundaries wouldn't change anything in-game. Sectors
        # stay view-only. `self._edit` is kept permanently False so the boundary-drag path is inert.
        self._edit.set(False)

        body = tk.Frame(self, background=_R_BG)
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
        tk.Label(kdtf, text=t("KDT — CAPTURE ZONES  (edit these)"), background=_R_BG_PANEL,
                 foreground=_R_GOLD, font=_F_BOLD).pack(anchor="w", pady=(0, 2))
        self._kdt_lbl = tk.Label(kdtf, text=t("(no kdt)"), background=_R_BG_PANEL,
                                 foreground=_R_TEXT_DIM, font=_F_MAIN, anchor="w", justify="left",
                                 wraplength=264)
        self._kdt_lbl.pack(anchor="w")
        tk.Checkbutton(kdtf, text=t("Show KDT mesh"), variable=self._kdt_show,
                       command=self._redraw, background=_R_BG_PANEL, foreground=_R_TEXT,
                       selectcolor=_R_BG_WIDGET, font=_F_MAIN, activebackground=_R_BG_PANEL,
                       activeforeground=_R_GOLD).pack(anchor="w", pady=(2, 0))
        tk.Checkbutton(kdtf, text=t("Colour by zone"), variable=self._kdt_color,
                       command=self._redraw, background=_R_BG_PANEL, foreground=_R_TEXT,
                       selectcolor=_R_BG_WIDGET, font=_F_MAIN, activebackground=_R_BG_PANEL,
                       activeforeground=_R_GOLD).pack(anchor="w", pady=(0, 2))
        tk.Checkbutton(kdtf, text=t("Edit KDT nodes (drag)"), variable=self._kdt_edit,
                       command=self._kdt_edit_toggle, background=_R_BG_PANEL, foreground=_R_GOLD_BRT,
                       selectcolor=_R_BG_WIDGET, font=_F_BOLD, activebackground=_R_BG_PANEL,
                       activeforeground=_R_GOLD).pack(anchor="w", pady=(2, 2))
        rr = tk.Frame(kdtf, background=_R_BG_PANEL); rr.pack(fill="x")
        tk.Checkbutton(rr, text=t("Optimize triangles (keep geometry)"), variable=self._kdt_relax,
                       background=_R_BG_PANEL, foreground=_R_TEXT, selectcolor=_R_BG_WIDGET,
                       font=_F_MAIN, activebackground=_R_BG_PANEL,
                       activeforeground=_R_GOLD).pack(side="left")
        tk.Button(rr, text=t("Optimize now"), command=self._kdt_relax_now, background="#122030",
                  foreground=_R_TEXT, font=_F_MAIN, relief="flat").pack(side="left", padx=6)
        tk.Label(kdtf, text=t("Drag a node; interior triangles re-optimize to stay clean & even,\n"
                          "keeping their shape (no welded twins, no rings — KDT is just nodes+tris)."),
                 background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN,
                 anchor="w", justify="left").pack(anchor="w", pady=(2, 0))
        tk.Label(kdtf, text=t("Rigid transform (whole mesh — proven safe):"), background=_R_BG_PANEL,
                 foreground=_R_TEXT_DIM, font=_F_MAIN, anchor="w").pack(anchor="w", pady=(6, 0))
        gr = tk.Frame(kdtf, background=_R_BG_PANEL); gr.pack(fill="x")
        for i, (lab, var) in enumerate(((t("ΔX"), self._kdt_dx), (t("ΔY"), self._kdt_dy),
                                        (t("scale"), self._kdt_scale))):
            tk.Label(gr, text=lab, background=_R_BG_PANEL, foreground=_R_TEXT, font=_F_MAIN,
                     width=5, anchor="e").grid(row=i, column=0, sticky="e", pady=1)
            tk.Entry(gr, textvariable=var, font=_F_MAIN, width=12, background=_R_BG_WIDGET,
                     foreground=_R_TEXT, insertbackground=_R_TEXT, highlightthickness=0,
                     relief="flat").grid(row=i, column=1, sticky="w", padx=4, pady=1)
        br = tk.Frame(kdtf, background=_R_BG_PANEL); br.pack(fill="x", pady=(4, 0))
        tk.Button(br, text=t("Preview"), command=self._kdt_preview, background="#122030",
                  foreground=_R_TEXT, font=_F_BOLD, relief="flat").pack(side="left")
        tk.Button(br, text=t("Reset"), command=self._kdt_reset, background="#122030",
                  foreground=_R_TEXT, font=_F_BOLD, relief="flat").pack(side="left", padx=4)
        tk.Button(br, text=t("Save KDT"), command=self._save_kdt, background="#1a2c44",
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
        tk.Label(left, text=t("DETAILS"), background=_R_BG_PANEL,
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
        tk.Label(plf, text=t("PLACEMENTS"), background=_R_BG_PANEL, foreground=_R_GOLD,
                 font=_F_BOLD).pack(anchor="w", pady=(2, 2))
        tk.Checkbutton(plf, text=t("Show placements"), variable=self._show_places,
                       command=self._redraw, background=_R_BG_PANEL, foreground=_R_TEXT,
                       selectcolor=_R_BG_WIDGET, font=_F_MAIN, activebackground=_R_BG_PANEL,
                       activeforeground=_R_GOLD).pack(anchor="w")
        tk.Checkbutton(plf, text=t("Edit placements (drag)"), variable=self._place_edit,
                       command=self._on_place_edit_toggle, background=_R_BG_PANEL, foreground=_R_TEXT,
                       selectcolor=_R_BG_WIDGET, font=_F_MAIN, activebackground=_R_BG_PANEL,
                       activeforeground=_R_GOLD).pack(anchor="w")
        tk.Checkbutton(plf, text=t("Auto snap to roads"), variable=self._snap_roads,
                       background=_R_BG_PANEL, foreground=_R_TEXT,
                       selectcolor=_R_BG_WIDGET, font=_F_MAIN, activebackground=_R_BG_PANEL,
                       activeforeground=_R_GOLD).pack(anchor="w")
        self._place_lbl = tk.Label(plf, text="", background=_R_BG_PANEL, foreground=_R_TEXT_DIM,
                                   font=_F_MAIN, anchor="w", justify="left", wraplength=264)
        self._place_lbl.pack(anchor="w", pady=(2, 0))
        pbr = tk.Frame(plf, background=_R_BG_PANEL); pbr.pack(fill="x", pady=(3, 0))
        tk.Button(pbr, text=t("+ Placement"), command=self._open_add_placement_popup,
                  background="#122030", foreground=_R_TEXT, font=_F_BOLD, relief="flat").pack(side="left")
        tk.Button(pbr, text=t("Delete sel"), command=self._delete_selected_place, background="#122030",
                  foreground=_R_TEXT, font=_F_BOLD, relief="flat").pack(side="left", padx=4)
        tk.Button(pbr, text=t("Edit script"), command=self._open_script_editor, background="#122030",
                  foreground=_R_TEXT, font=_F_BOLD, relief="flat").pack(side="left", padx=4)

        # ── GAME MODES: per-mode lobby ticks, read from the existing HQ layout ─────────────────
        # Step 3 reshape (2026-05-28): HQ creation is now ALWAYS user-driven via Add Placement.
        # The two buttons below split the old "Apply game modes (add HQs)" into the two real
        # operations: 'Recompute ticks' re-reads the HQ layout into the ticks; 'Apply lobby
        # modes' stages TMultiMapInfo so the lobby OFFERS exactly the ticked modes.
        gmf = tk.Frame(bottom_block, background=_R_BG_PANEL)
        gmf.pack(fill="x", padx=8, pady=(2, 4))
        tk.Label(gmf, text=t("GAME MODES"), background=_R_BG_PANEL, foreground=_R_GOLD,
                 font=_F_BOLD).pack(anchor="w", pady=(2, 0))
        tk.Label(gmf,
                 text=t("tick the modes this map should offer in the lobby. "
                        "'Recompute ticks' fills these in from the HQ spawns already on the map; "
                        "'Apply lobby modes' writes TMultiMapInfo so the lobby OFFERS them."),
                 background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN,
                 justify="left", wraplength=264).pack(anchor="w")
        grid = tk.Frame(gmf, background=_R_BG_PANEL); grid.pack(anchor="w", pady=(2, 2))
        for n, m in enumerate(_GAME_MODES):
            tk.Checkbutton(grid, text=t(m[1]), variable=self._mode_vars[m[0]],
                           background=_R_BG_PANEL, foreground=_R_TEXT, selectcolor=_R_BG_WIDGET,
                           font=_F_MAIN, activebackground=_R_BG_PANEL, activeforeground=_R_GOLD
                           ).grid(row=n // 2, column=n % 2, sticky="w", padx=(0, 8))
        gmbr = tk.Frame(gmf, background=_R_BG_PANEL); gmbr.pack(fill="x", pady=(2, 0))
        tk.Button(gmbr, text=t("Recompute ticks"), command=self._load_game_modes_state,
                  background="#122030", foreground=_R_TEXT, font=_F_BOLD,
                  relief="flat").pack(side="left")
        tk.Button(gmbr, text=t("Apply lobby modes"), command=self._stage_lobby_modes,
                  background="#163048", foreground=_R_GOLD_BRT, font=_F_BOLD,
                  relief="flat").pack(side="left", padx=4)

        # ── right panel: AI-terrain SDB layers (toggle visibility + pick paint target) ───────────
        right = tk.Frame(body, background=_R_BG_PANEL, width=258)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        tk.Label(right, text=t("AI-TERRAIN LAYERS (SDB)"), background=_R_BG_PANEL, foreground=_R_GOLD,
                 font=_F_BOLD).pack(anchor="w", padx=8, pady=(8, 0))
        tk.Label(right, text=t("show ✓   paint target ◉   (mapinfo.win — runtime SDB)"),
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
        tk.Checkbutton(right, text=t("PAINT MODE  (drag on map)"), variable=self._conceal_edit,
                       command=self._invalidate_redraw, background=_R_BG_PANEL, foreground=_R_GOLD_BRT,
                       selectcolor=_R_BG_WIDGET, font=_F_BOLD, activebackground=_R_BG_PANEL,
                       activeforeground=_R_GOLD).pack(anchor="w", padx=8)
        tk.Checkbutton(right, text=t("Erase (remove from layer)"), variable=self._conceal_erase,
                       background=_R_BG_PANEL, foreground=_R_TEXT, selectcolor=_R_BG_WIDGET,
                       font=_F_MAIN, activebackground=_R_BG_PANEL,
                       activeforeground=_R_GOLD).pack(anchor="w", padx=8)
        brow = tk.Frame(right, background=_R_BG_PANEL); brow.pack(fill="x", padx=8, pady=(2, 0))
        tk.Label(brow, text=t("Brush radius"), background=_R_BG_PANEL, foreground=_R_TEXT,
                 font=_F_MAIN).pack(side="left")
        tk.Entry(brow, textvariable=self._conceal_brush, width=8, font=_F_MAIN,
                 background=_R_BG_WIDGET, foreground=_R_TEXT, insertbackground=_R_TEXT).pack(side="left", padx=4)
        tk.Label(right, text=t("Painted layers are written with the top-right  \"Save to mod\"  button."),
                 background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN,
                 anchor="w", justify="left", wraplength=238).pack(anchor="w", padx=8, pady=(6, 0))
        self._sdb_lbl = tk.Label(right, text=t("(no SDB)"), background=_R_BG_PANEL, foreground=_R_TEXT_DIM,
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
            messagebox.showerror(t("Load failed"),
                                 t("Could not open the map dat for this mod:\n{src}\n\n{e}\n\n"
                                   "Make sure a clean DataMap_Win.dat backup exists (Mod Manager tab) "
                                   "or set the Game Root in Settings.", src=src, e=e), parent=self)
            self._map_cb["values"] = []
            return
        self._map_cb["values"] = maps
        ui_util.fit_combobox(self._map_cb, minimum=18, maximum=40)
        if maps:
            # default to blitz (supercrossroads4) — the user's in-game test map
            self._map_cb.set("supercrossroads4" if "supercrossroads4" in maps else maps[0])
            self._on_map_change(force=True)
        else:
            messagebox.showwarning(t("No maps"),
                                   t("That dat has no datasmap\\{map}\\mapinfo.win entries "
                                     "(is it DataMap_Win.dat?)."), parent=self)

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
        """Generated high-def terrain is cached WITH the mod project, so it persists with it (the
        project folder is the home for everything created for the mod — like notes.json and the
        edited dats).  Falls back to the system temp dir only if the project folder isn't writable."""
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", map_dir)
        try:
            base = os.path.join(str(self.project.folder), "cache", "terrain")
            os.makedirs(base, exist_ok=True)
        except Exception:
            base = os.path.join(tempfile.gettempdir(), "ruse_mm_terrain_cache")
            try:
                os.makedirs(base, exist_ok=True)
            except Exception:
                pass
        return os.path.join(base, f"{safe}_hd.png")

    def _load_terrain(self, map_dir):
        """The map's display image, REPLACING the baked minimap with the decoded high-def terrain
        (issue #8).  Returns an image to show NOW — a cached high-def PNG if we have one, else the
        minimap as an instant placeholder while the tmst high-def decodes on a background thread and
        swaps in.  Falls back to the minimap if Pillow/numpy/the codec aren't available."""
        self._terrain_token += 1
        if not _HAVE_PIL:
            return None
        cache = self._terrain_cache_path(map_dir)
        if os.path.isfile(cache):
            try:
                return Image.open(cache).convert("RGBA")
            except Exception:
                pass
        if _terrain_codec is not None:
            src = self._terrain_dat_src(map_dir)
            if src:
                token = self._terrain_token
                self._status.config(text=t("  decoding high-def terrain…"))
                threading.Thread(target=self._decode_terrain_bg,
                                 args=(src, cache, token), daemon=True).start()
        return self._load_minimap(map_dir)

    def _decode_terrain_bg(self, src, cache, token):
        """Background: extract the tmst pair, pick the best LOD, decode + enhance, cache, then swap."""
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
            gw, gh, _recs = _terrain_codec.parse_tile_index(tmst)
            lod = _terrain_codec.best_lod(gw, gh)         # LOD0 detail where the map is small enough
            img = _terrain_codec.decode_terrain(tmst, chunk, lod=lod, use_index=True)
            if img is None:
                return
            # Lay the tmst's fine DETAIL over the clean minimap colour base — kills the per-tile
            # banding and restores true colour (see terrain_codec.compose).
            base = Image.open(io.BytesIO(png)).convert("RGB") if png else None
            img = _terrain_codec.compose(img, base)
            try:
                img.save(cache)
            except Exception:
                pass
            rgba = img.convert("RGBA")
        except Exception:
            return
        self.after(0, lambda: self._apply_terrain(rgba, token))

    def _apply_terrain(self, rgba, token):
        if token != self._terrain_token:      # user switched maps meanwhile — discard
            return
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        self._pil = rgba
        self._status.config(text="")
        self._invalidate_redraw()
        self._fit_view()

    def _on_map_change(self, _=None, force=False):
        if not force and not self._confirm_discard():
            return
        map_dir = self._map_cb.get()
        scns = list_scenarios(self._dm, map_dir)
        self._scn_cb["values"] = scns
        ui_util.fit_combobox(self._scn_cb, minimum=24, maximum=44)
        self._pil = self._load_terrain(map_dir)
        win = self._dm.get(f"datasmap\\{map_dir}\\mapinfo.win")
        self._bbox = read_bbox(win) if win else None
        self._roads = read_road_graph(win) if win else None
        self._forest = read_forest_zones(win) if win else None
        self._sdb_map_dir = map_dir
        self._sdb_win = win
        self._sdb_win_vpath = f"datasmap\\{map_dir}\\mapinfo.win"
        self._load_sdb()
        if scns:
            # prefer the standard/MP scenario (challenge/testia variants have different placements)
            self._scn_cb.set(next((s for s in ("leveldesign_normal", "leveldesign") if s in scns),
                                  scns[0]))
        self._on_scn_change(force=True)
        self._load_game_modes_state()      # reflect the map's currently-enabled game modes in the panel
        self._fit_view()

    # ── AI-terrain SDB painting (unified codec; mapinfo buffer4 = the game's runtime SDB) ─────
    _SDB_LAYER_RGB = {0x08: (60, 220, 90), 0x04: (80, 160, 255)}

    def _sdb_layers_list(self):
        """The (label, bit, rgb) rows for the two runtime-meaningful SDB layers (Ghidra-confirmed:
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
                self._sdb_status(t("(no SDB)")); return
            buf = parts[1][3]
            parsed = sdb_mod.parse(buf)
            grid, R = sdb_mod.to_grid(parsed)
            bboxX = struct.unpack_from("<f", buf, 37)[0]
            bboxY = struct.unpack_from("<f", buf, 41)[0]
            self._sdb = {"parsed": parsed, "grid": bytearray(grid), "R": R, "bboxX": bboxX,
                         "bboxY": bboxY, "win": win,
                         "win_vpath": getattr(self, "_sdb_win_vpath", None), "dirty": False}
        except Exception as e:
            self._sdb_status(t("SDB load failed: {e}", e=e)); return
        self._sdb_status(t("SDB {r}x{r}  (mapinfo buffer4)", r=self._sdb["R"]))

    def _sdb_status(self, msg):
        if self._sdb_lbl is not None:
            self._sdb_lbl.config(text=msg)

    def _sdb_cells_for(self, bit):
        """Overlay cells for a layer bit (lazy; invalidated on paint of that bit)."""
        if not self._sdb:
            return []
        if bit not in self._sdb_cells:
            self._sdb_cells[bit] = sdb_mod.grid_to_cells(self._sdb["grid"], self._sdb["R"],
                                                         self._sdb["bboxX"], self._sdb["bboxY"], bit)
        return self._sdb_cells[bit]

    def _on_paint_layer(self):
        """Selecting a paint target auto-shows that layer so you see what you edit."""
        self._sdb_show.setdefault(self._conceal_layer.get(), tk.BooleanVar(value=True)).set(True)
        self._invalidate_redraw()

    def _conceal_refresh_overlay(self):
        self._invalidate_sdb()      # only the SDB raster changed — don't re-rasterise the vectors

    def _conceal_paint_at(self, sx, sy):
        if not self._sdb:
            return
        try:
            import numpy as np
        except Exception:
            return
        bit = self._conceal_layer.get()
        wx, wy = self._screen_to_world(sx, sy)
        R = self._sdb["R"]; bboxX = self._sdb["bboxX"]; bboxY = self._sdb["bboxY"]
        radius = float(self._conceal_brush.get())
        # circular brush mask at grid resolution
        cw = bboxX / R; ch = bboxY / R
        cx = int(wx / cw); cy = int(wy / ch)
        rx = max(1, int(radius / cw)); ry = max(1, int(radius / ch))
        ys, xs = np.ogrid[0:R, 0:R]
        mask = (((xs + 0.5) * cw - wx) ** 2 + ((ys + 0.5) * ch - wy) ** 2) <= radius * radius
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
            return
        map_dir = self._map_cb.get()
        scn = self._scn_cb.get()
        raw = self._dm.get(f"test\\map\\{map_dir}\\{scn}.scenario") if scn else None
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
            self._kdt_lbl.config(text=t("(no kdt)")); return
        vpath = f"test\\map\\{map_dir}\\zonebluff\\{scn}.kdt"
        data = self._dm.get(vpath)
        if not data:
            self._kdt_lbl.config(text=t("(no kdt for this scenario)")); return
        try:
            kf = kdt_mod.read_full(data)
            self._kf, self._kdt_bytes, self._kdt_vpath = kf, data, vpath
            # Decode the REAL triangle mesh OFFLINE via the cracked VtxBufIdx predictor (kdt.decode_mesh).
            # Works for all maps, no Frida — d1 verts are residuals + ref-instructions, not absolute.
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
            self._kdt_lbl.config(text=t("verts={nv}  tris={nt}  "
                                        "mesh={src}  sectors={sec}  "
                                        "border={nb}", nv=len(self._kdt_world), nt=len(self._kdt_tris),
                                        src=src, sec=('exact' if known else 'geom'),
                                        nb=len(self._kdt_bverts)))
        except Exception as e:
            self._kdt_lbl.config(text=t("(kdt load failed: {e})", e=e))

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
            messagebox.showinfo(
                t("KDT follows sectors (modest moves)"),
                t("When you edit sectors, the KDT mesh follows and 'Save KDT' rewrites it (verts only — the "
                  "capture zones are preserved).\n\nKeep moves MODEST: the KD-tree partition isn't rebuilt, "
                  "so large reshapes can break capture. (A full rebuild for big moves is pending one more "
                  "bit of reverse-engineering.) Test on blitz with a small edit first."), parent=self)
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
            messagebox.showinfo(t("No KDT"), t("Load a scenario with a KDT first."), parent=self)
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
                                ndfbin.NdfValue(ndfbin.T.WideString, value_str),
                                create=True)
        self._dirty = True
        self.after_idle(self._refresh_after_edit)

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
            editing = self._place_edit.get()
            # Resolve the live NdfInstance handles so the inline-edit closures don't have to
            # re-look them up by index every keystroke (the instances themselves are stable
            # across _rebuild_places; only their indices in self._places shift).
            addon = self._pndf.instances[pl["addon_idx"]] if (
                self._pndf is not None and pl["addon_idx"] is not None) else None
            item = self._pndf.instances[pl["item_idx"]] if (
                self._pndf is not None and pl["item_idx"] is not None) else None

            # PLACEMENT header — kind + label, so you can see at a glance what's selected.
            self._det_head(t("PLACEMENT — {kind}{label}",
                             kind=kind, label=("  ·  " + pl["label"]) if pl["label"] else ""))

            # SPAWN sub-block — depots, units, buildings, generic spawns. All share Camp; only
            # depots carry ChampInteger. Edit-mode swaps each row's value for an inline Entry.
            if kind in ("depot", "unit", "building", "spawn"):
                role = {"depot": t("supply depot"),
                        "unit":  t("pre-placed unit"),
                        "building": t("pre-placed building"),
                        "spawn": t("other camp-owned entity")}[kind]
                self._det_head(t("SPAWN — {role}", role=role))
                if editing:
                    self._det_entry(t("PythonClass"), ex.get("pyclass") or "",
                                    lambda s, a=addon: self._commit_stringref_prop(a, "PythonClassName", s),
                                    width=40)
                elif ex.get("pyclass"):
                    self._det_path(t("PythonClass"), ex["pyclass"])
                cv = ex.get("camp")
                cv_decoded = _camp_str(cv)
                if editing:
                    self._det_entry(t("Camp"), cv,
                                    lambda s, a=addon: self._commit_int_prop(a, "Camp", s, blank_clears=True),
                                    width=8, suffix=cv_decoded)
                    self._det_hint(t("  blank = clear (Team 1; MP depot = despawn).  "
                                     "-1 = visible-neutral.  N ≥ 0 = Team N+1."))
                else:
                    cv_raw = t("(unset)") if cv is None else cv
                    self._det_kv(t("Camp"), "%s   %s" % (cv_raw, cv_decoded))
                ci = ex.get("champ")
                if kind == "depot":
                    self._det_head(t("SUPPLY"))
                    if editing:
                        self._det_entry(t("ChampInteger"), ci if ci is not None else "",
                                        lambda s, a=addon: self._commit_int_prop(a, "ChampInteger", s),
                                        width=8)
                    else:
                        self._det_kv(t("ChampInteger"), ci if ci is not None else t("(unset)"))
                    if ci is not None:
                        self._det_kv(t("RUSE total"), "%d  (9 × %d)" % (9 * ci, ci))
                        self._det_kv(t("atomic units"), "%d  (27 × %d)" % (27 * ci, ci))

            # HQ sub-block — team identity + camera (resting + warmup). Edit-mode exposes all
            # the fields the old dedicated EDIT-HQ frame used to own, plus the previously
            # read-only Azimut/Site.
            elif kind == "hq":
                self._det_head(t("TEAM"))
                _pri = ex.get("priority")
                if editing:
                    self._det_entry(t("alliance"), ex.get("alliance") if ex.get("alliance") is not None else 1,
                                    lambda s, a=addon: self._commit_int_prop(a, "AllianceNum", s),
                                    width=6, suffix=t("(direct team id — Team 1 = 1)"))
                    self._det_entry(t("priority"), _pri if _pri is not None else "",
                                    lambda s, a=addon: self._commit_int_prop(a, "AlliancePriority", s, blank_clears=True),
                                    width=6, suffix=t("blank = FFA seat (no property)"))
                    self._det_check(t("FFA seat"), _pri is None,
                                    lambda on, a=addon: self._commit_ffa_toggle(a, on))
                else:
                    _pri_s = "* (FFA seat)" if _pri is None else _pri
                    self._det_kv(t("alliance"), ex.get("alliance"))
                    self._det_kv(t("priority"), _pri_s)
                self._det_head(t("CAMERA"))
                if editing:
                    self._det_entry(t("azimut"), "%.1f" % (ex.get("azimut") or 0.0),
                                    lambda s, a=addon: self._commit_float_prop(a, "Azimut", s),
                                    width=10, suffix="°")
                    self._det_entry(t("site"), "%.1f" % (ex.get("site") or 0.0),
                                    lambda s, a=addon: self._commit_float_prop(a, "Site", s),
                                    width=10, suffix="°")
                else:
                    if ex.get("azimut") is not None:
                        self._det_kv(t("azimut"), "%.1f°" % ex["azimut"])
                    if ex.get("site") is not None:
                        self._det_kv(t("site"), "%.1f°" % ex["site"])
                if ex.get("cam"):
                    cx, cy = ex["cam"][0], ex["cam"][1]
                    self._det_kv(t("rest cam pos"), "%.0f, %.0f" % (cx, cy))
                if ex.get("warmup"):
                    self._det_kv(t("warmup path"), ex["warmup"])
                if ex.get("campath_keys"):
                    self._det_kv(t("campath keys"), "%d" % len(ex["campath_keys"]))

            # Zone-shape blocks. ChampTexte for city/mountain labels is the user-visible string —
            # editable in edit mode.
            elif kind in ("ville", "montagne"):
                self._det_head(t("LABEL"))
                if editing:
                    self._det_entry(t("ChampTexte"), pl.get("label", ""),
                                    lambda s, a=addon: self._commit_widestring_prop(a, "ChampTexte", s),
                                    width=30)
                else:
                    self._det_kv(t("ChampTexte"), pl.get("label", "") or t("(empty)"))
            elif kind == "circle":
                self._det_head(t("ZONE"))
                if editing:
                    self._det_entry(t("radius"), "%.0f" % (ex.get("radius") or 0.0),
                                    lambda s, a=addon: self._commit_float_prop(a, "Radius", s),
                                    width=12)
                else:
                    self._det_kv(t("radius"), "%.0f" % (ex.get("radius") or 0.0))
            elif kind == "rect":
                self._det_head(t("ZONE"))
                if editing:
                    self._det_entry(t("width"), "%.0f" % (ex.get("w") or 0.0),
                                    lambda s, a=addon: self._commit_float_prop(a, "Width", s),
                                    width=12)
                    self._det_entry(t("height"), "%.0f" % (ex.get("h") or 0.0),
                                    lambda s, a=addon: self._commit_float_prop(a, "Height", s),
                                    width=12)
                else:
                    self._det_kv(t("size (w×h)"), "%.0f × %.0f"
                                 % ((ex.get("w") or 0.0), (ex.get("h") or 0.0)))

            # GEOMETRY (last because most users care about identity/ownership first). Position
            # stays drag-only; rotation is editable for kinds that AREN'T road-locked
            # (depot/HQ have their facing snapped to the nearest road in-game).
            self._det_head(t("GEOMETRY"))
            self._det_kv(t("position"), "%.0f, %.0f" % (x, y))
            self._det_kv(t("height (z)"), "%.0f" % z_)
            road_locked = kind in ("depot", "hq")
            if editing and not road_locked:
                rv = pl["rot"] if pl["rot"] is not None else 0.0
                self._det_entry(t("rotation"), "%.4f" % rv,
                                lambda s, it=item: self._commit_rotation(it, s),
                                width=12, suffix=t("rad  (%.0f°)" % math.degrees(rv)))
            elif pl["rot"] is not None:
                self._det_kv(t("rotation"),
                             "%.3f rad  (%.0f°)" % (pl["rot"], math.degrees(pl["rot"])))

            # EDIT hints (only when they say something useful — keep the panel quiet otherwise).
            self._det_head(t("EDIT"))
            if road_locked:
                self._det_hint(t("Facing auto-snaps to the nearest road in-game (Rotation here is ignored)."))
            if not editing:
                self._det_hint(t("Toggle 'Edit placements' above to edit fields and drag this."))
            elif kind == "hq":
                self._det_hint(t("Drag HQ to move (camera follows). Drag the 'cam' marker around the ring to re-aim."))
            else:
                self._det_hint(t("Drag to move, then Save."))

        elif self._sel is not None and self._sel < len(self._zones):
            # Defensive sector branch — the listbox that used to drive this was removed, so
            # in practice _sel stays None. Kept so any future canvas-pick can light it up.
            z = self._zones[self._sel]
            xs = [v[0] for v in z["verts"]]; ys = [v[1] for v in z["verts"]]
            self._det_head(t("SECTOR  #{idx}", idx=z["idx"]))
            self._det_kv(t("name"), z["name"])
            self._det_kv(t("marker"), "%.0f, %.0f" % (z["pos"][0], z["pos"][1]))
            self._det_kv(t("vertices"), len(z["verts"]))
            self._det_kv(t("triangles"), len(z["faces"]))
            self._det_kv(t("boundary pts"), len(z["bverts"]))
            self._det_kv(t("X range"), "%.0f … %.0f" % (min(xs), max(xs)))
            self._det_kv(t("Y range"), "%.0f … %.0f" % (min(ys), max(ys)))
            self._det_hint(t("Sectors are visual only — capture is driven by the .kdt."))
        else:
            self._det_hint(t("Click a placement on the map to see its details."))
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

    def _screen_to_world(self, sx, sy):
        minx, miny, maxx, maxy = self._bbox
        w, h = self._pil.size
        bx = (sx - self._ox) / self._scale
        by = (sy - self._oy) / self._scale
        x = minx + bx / w * (maxx - minx)
        y = maxy - by / h * (maxy - miny) if self._flip_y.get() else miny + by / h * (maxy - miny)
        return x, y

    def _fit_view(self):
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
        index, or None if there's no HQ to clone."""
        tmpl = self._place_template("hq")
        ilist = self._item_list_value()
        if tmpl is None or ilist is None or self._pndf is None:
            return None
        item_src, addon_src = tmpl
        z = self._nearest_z(wx, wy)
        campath_name = self._next_warmup_name()

        def setp(inst, name, val):
            # create=True: a 1v1 scenario has no AlliancePriority property yet — add it (like the applier).
            self._pndf.set_property(inst, name, val, create=True)

        new_addon = ndfbin.NdfInstance(class_index=addon_src.class_index,
                                       props=copy.deepcopy(addon_src.props))
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
        self._pndf.instances.append(new_addon)
        addon_idx = len(self._pndf.instances) - 1

        new_item = ndfbin.NdfInstance(class_index=item_src.class_index,
                                      props=copy.deepcopy(item_src.props))
        setp(new_item, "Position", ndfbin.NdfValue(ndfbin.T.Vector3, (float(wx), float(wy), float(z))))
        setp(new_item, "AddOn", ndfbin.NdfValue(
            ndfbin.T.Reference, (ndfbin.OBJ_REF_MARKER, (addon_idx, new_addon.class_index))))
        self._pndf.instances.append(new_item)
        item_idx = len(self._pndf.instances) - 1
        ilist.raw.append(ndfbin.NdfValue(
            ndfbin.T.Reference, (ndfbin.OBJ_REF_MARKER, (item_idx, new_item.class_index))))

        self._clone_campath_for_hq(campath_name, (float(wx), float(wy), float(z)))
        self._dirty = True
        return item_idx

    def _stage_lobby_modes(self):
        """Stage this map's TMultiMapInfo (the lobby record) so the lobby OFFERS exactly the
        currently-ticked GAME MODES. This is the lobby-metadata half of the old
        '_apply_game_modes' button — the HQ-spawning half is gone, since every HQ is now
        user-placed through Add Placement (Step 1).

        Validation: at least one mode ticked; warn (but proceed) if any ticked mode's required
        HQ spawn set isn't fully covered by the current StartingPoints. The user can either
        add the missing HQs via Add Placement or accept the lobby/scenario divergence (the
        game will start anyway; mismatched modes just fail to launch when picked)."""
        if self._pndf is None:
            messagebox.showinfo(t("No scenario"), t("Load a scenario first."), parent=self)
            return
        checked = [m for m in _GAME_MODES if self._mode_vars[m[0]].get()]
        if not checked:
            messagebox.showinfo(t("No modes"),
                                t("Tick at least one mode first (or click Recompute ticks to "
                                  "fill them in from the HQs already on the map)."),
                                parent=self)
            return
        # Warn about ticked modes that the HQ layout can't satisfy. Same coverage rules as
        # _load_game_modes_state: team-modes accept (a, None) FFA seats as cover for (a, 1).
        strict_existing = set(self._existing_hq_spawns().keys())
        soft_existing = self._existing_covers()
        unmet = []
        for (_k, label, teams, per, _gt, _gmm, dispo) in checked:
            req = self._mode_spawns(teams, per, ffa=(dispo == "DispoMultiFFA"))
            covered = strict_existing if dispo == "DispoMultiFFA" else soft_existing
            if req - covered:
                unmet.append(label)
        if unmet:
            ok = messagebox.askyesno(
                t("Lobby ↔ scenario mismatch"),
                t("These ticked modes don't have all the HQs they need on the map: {modes}.\n\n"
                  "Lobby will offer them but launches will fail until you place the missing "
                  "HQs via Add Placement.\n\nStage TMultiMapInfo anyway?",
                  modes=", ".join(unmet)),
                parent=self)
            if not ok:
                return
        # lobby metadata: NbPlayers = the biggest enabled mode; primary GameType/GMM = that mode
        primary = max(checked, key=lambda m: m[2] * m[3])
        nb = max(m[2] * m[3] for m in checked)
        dispos = {m[6] for m in checked}
        info = self._sync_multimapinfo(nb, primary[4], primary[5], dispos)
        messagebox.showinfo(
            t("Lobby modes staged"),
            t("Lobby will offer: {modes}\n\n{info}",
              modes=", ".join(t(m[1]) for m in checked), info=info),
            parent=self)

    def _sync_multimapinfo(self, nb_players, gametype, gmm, dispo_set):
        """Find this map's TMultiMapInfo (globals.cpp) by GUID via TMapLoadInfo.Path==map dir, and offer
        to set NbPlayers / GameType / GameModeMulti + the Dispo* flags so the lobby OFFERS exactly the
        chosen modes.  The edit is STAGED into the mod project's gameplay dat (written on "Save to mod") —
        it never touches the live game.  Returns a human-readable status string."""
        ALL_DISPO = ["DispoLadder1v1", "DispoLadder2v2", "DispoMulti2Teams", "DispoMulti3Teams",
                     "DispoMulti4Teams", "DispoMultiFFA"]
        map_dir = (self._map_cb.get() or "").lower()
        target = "NbPlayers=%d  GameType=%s  GameModeMulti=%d  %s" % (
            nb_players, gametype, gmm, "+".join(sorted(d for d in ALL_DISPO if d in dispo_set)) or "(none)")
        try:
            ndfL = self.project.get_ndf("gameplay", self._MAPINFO_PATH)
            ndfG = self.project.get_ndf("gameplay", self._GLOBALS_PATH)
        except Exception as e:
            return t("Lobby metadata NOT changed — couldn't read the gameplay dat for this mod "
                     "({e}).\nSet this on the map's TMultiMapInfo yourself:\n  {target}",
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
            return t("No TMultiMapInfo found for '{map_dir}' in the gameplay dat (a brand-new custom map "
                     "won't have one yet).\nThe HQs/cameras are set; set the lobby entry yourself:\n  {target}",
                     map_dir=map_dir, target=target)
        if not messagebox.askyesno(
                t("Update lobby modes?"),
                t("Set the lobby metadata on {n} TMultiMapInfo entry(ies) for '{map_dir}' to:\n\n"
                  "  {target}\n\nThis is staged into the mod project's gameplay dat and written when you "
                  "click \"Save to mod\". Proceed?", n=len(matches), map_dir=map_dir, target=target),
                parent=self):
            return t("Lobby metadata left unchanged. Target was:\n  {target}", target=target)
        for inst in matches:
            def setp(n, val):
                p = ndfG.prop_by_name_and_class(n, inst.class_index)
                if p is not None:
                    inst.set(p.index, val)
            setp("NbPlayers", ndfbin.NdfValue(ndfbin.T.Int32, int(nb_players)))
            if gametype is not None:
                setp("GameType", ndfbin.NdfValue(ndfbin.T.Int32, int(gametype)))
            setp("GameModeMulti", ndfbin.NdfValue(ndfbin.T.Int32, int(gmm)))
            for f in ALL_DISPO:
                setp(f, ndfbin.NdfValue(ndfbin.T.Int32, 1 if f in dispo_set else 0))
        # The shared globals.cpp NDF is mutated in place; flag it dirty so "Save to mod" writes it into
        # the mod project's gameplay dat (the live game is never touched).
        self.project.mark_dirty("gameplay", self._GLOBALS_PATH)
        if self._on_change:
            self._on_change()
        return t("Lobby modes staged on {n} TMultiMapInfo entry(ies) (gameplay dat — click "
                 "\"Save to mod\" to write):\n  {target}", n=len(matches), target=target)

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
        map_dir = (self._map_cb.get() or "").lower()

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
            messagebox.showinfo(t("No scenario"), t("Load a scenario first."), parent=self)
            return None

        fields = fields or {}
        wx, wy, wz = pos

        if kind == "hq":
            alliance = int(fields.get("alliance", 1))
            slot = fields.get("slot")
            item_idx = self._add_hq(alliance, slot, wx, wy)
            if item_idx is None:
                messagebox.showinfo(t("No template HQ"),
                                    t("This scenario has no existing HQ to clone from.\n"
                                      "Load any MP scenario and re-save the project to seed one."),
                                    parent=self)
                return None
            self._place_edit.set(True)
            self._rebuild_places(select_item_idx=item_idx)
            # Link the cloned warmup campath so the new HQ's 'cam' marker + campath_keys
            # show in the detail panel and on the canvas — matches what the old
            # _apply_game_modes button did once per add-pass.
            self._link_campaths()
            self._invalidate_redraw()
            self._status.config(text=t("  added HQ A{a}.{s} — drag it, then Save.",
                                       a=alliance, s="*" if slot is None else slot))
            return item_idx

        # All other kinds: clone-from-template.
        tmpl = self._place_template(kind)
        if tmpl is None:
            addon_cls = _KIND_TO_ADDON.get(kind, "?")
            messagebox.showinfo(t("No template available"),
                                t("This scenario has no existing {kind} to clone "
                                  "(would need a {cls} instance).\n"
                                  "Load a scenario that has one of these and re-save to seed it.",
                                  kind=kind, cls=addon_cls),
                                parent=self)
            return None
        item_src, addon_src = tmpl
        ilist = self._item_list_value()
        if ilist is None:
            messagebox.showerror(t("Add failed"), t("Couldn't find the TGameDesignItemList."), parent=self)
            return None

        new_addon = ndfbin.NdfInstance(class_index=addon_src.class_index,
                                       props=copy.deepcopy(addon_src.props))

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
            setp(new_addon, "ChampTexte", ndfbin.NdfValue(ndfbin.T.WideString, str(fields["text"])))
        elif kind == "circle" and "radius" in fields:
            setp(new_addon, "Radius", ndfbin.NdfValue(ndfbin.T.Float32, float(fields["radius"])))
        elif kind == "rect":
            if "w" in fields:
                setp(new_addon, "Width", ndfbin.NdfValue(ndfbin.T.Float32, float(fields["w"])))
            if "h" in fields:
                setp(new_addon, "Height", ndfbin.NdfValue(ndfbin.T.Float32, float(fields["h"])))

        self._pndf.instances.append(new_addon)
        addon_idx = len(self._pndf.instances) - 1

        new_item = ndfbin.NdfInstance(class_index=item_src.class_index,
                                      props=copy.deepcopy(item_src.props))
        setp(new_item, "Position", ndfbin.NdfValue(ndfbin.T.Vector3, (float(wx), float(wy), float(wz))))
        setp(new_item, "AddOn", ndfbin.NdfValue(ndfbin.T.Reference,
             (ndfbin.OBJ_REF_MARKER, (addon_idx, new_addon.class_index))))
        self._pndf.instances.append(new_item)
        item_idx = len(self._pndf.instances) - 1
        ilist.raw.append(ndfbin.NdfValue(ndfbin.T.Reference,
                         (ndfbin.OBJ_REF_MARKER, (item_idx, new_item.class_index))))
        self._dirty = True
        self._place_edit.set(True)
        self._rebuild_places(select_item_idx=item_idx)
        self._status.config(text=t("  added a {kind} at view centre — drag it into place, then Save.",
                                   kind=kind))
        return item_idx

    def _open_add_placement_popup(self):
        """Modal popup that lets the user create any kind of placement. Picks the kind, picks
        a PythonClassName for Spawn-derived kinds (with a filterable listbox seeded from the
        current scenario, plus free-text input), fills kind-specific fields, then Create →
        _create_placement(...). Position defaults to the canvas centre."""
        if self._pndf is None:
            messagebox.showinfo(t("No scenario"), t("Load a scenario first."), parent=self)
            return
        cw = max(self._canvas.winfo_width(), 50); ch = max(self._canvas.winfo_height(), 50)
        wx, wy = self._screen_to_world(cw / 2, ch / 2)
        wz = self._nearest_z(wx, wy)

        win = tk.Toplevel(self)
        win.title(t("Add placement"))
        win.configure(background=_R_BG_PANEL)
        win.transient(self); win.grab_set()
        win.geometry("520x520")

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
        tk.Label(win, text=t("KIND"), background=_R_BG_PANEL, foreground=_R_GOLD,
                 font=_F_BOLD).pack(anchor="w", padx=8, pady=(8, 2))
        kg = tk.Frame(win, background=_R_BG_PANEL); kg.pack(anchor="w", padx=8)
        KINDS = [("depot", t("Depot")), ("unit", t("Unit")), ("building", t("Building")),
                 ("spawn", t("Spawn (other)")), ("hq", t("HQ")),
                 ("ville", t("City label")), ("montagne", t("Mountain label")),
                 ("name", t("Named point")), ("circle", t("Circular zone")),
                 ("rect", t("Rect zone"))]
        for i, (k, label) in enumerate(KINDS):
            tk.Radiobutton(kg, text=label, variable=kind_var, value=k,
                           background=_R_BG_PANEL, foreground=_R_TEXT, selectcolor=_R_BG_WIDGET,
                           font=_F_MAIN, activebackground=_R_BG_PANEL, activeforeground=_R_GOLD,
                           command=lambda: _refresh_fields()
                           ).grid(row=i // 5, column=i % 5, sticky="w", padx=(0, 8))

        # ── PyClass picker — only relevant for Spawn-derived kinds ───────────────
        py_frame = tk.Frame(win, background=_R_BG_PANEL)
        tk.Label(py_frame, text=t("PYTHONCLASSNAME"), background=_R_BG_PANEL, foreground=_R_GOLD,
                 font=_F_BOLD).pack(anchor="w", pady=(8, 2))
        py_search = tk.Entry(py_frame, background=_R_BG_WIDGET, foreground=_R_TEXT,
                             font=_F_MAIN, insertbackground=_R_TEXT, relief="flat",
                             highlightthickness=0)
        py_search.pack(fill="x", pady=(0, 2))
        pylb_frame = tk.Frame(py_frame, background=_R_BG_PANEL); pylb_frame.pack(fill="both", expand=True)
        pysb = tk.Scrollbar(pylb_frame, orient="vertical")
        pysb.pack(side="right", fill="y")
        py_list = tk.Listbox(pylb_frame, background=_R_BG_WIDGET, foreground=_R_TEXT,
                             selectbackground=_R_SEL_BG, selectforeground=_R_GOLD_BRT,
                             font=_F_MAIN, highlightthickness=0, borderwidth=0,
                             activestyle="none", exportselection=False, height=6,
                             yscrollcommand=pysb.set)
        py_list.pack(side="left", fill="both", expand=True)
        pysb.config(command=py_list.yview)
        tk.Label(py_frame, text=t("Active PyClass:"), background=_R_BG_PANEL, foreground=_R_TEXT_DIM,
                 font=_F_MAIN).pack(anchor="w", pady=(4, 0))
        tk.Entry(py_frame, textvariable=py_var, background=_R_BG_WIDGET, foreground=_R_TEXT,
                 font=_F_MAIN, insertbackground=_R_TEXT, relief="flat",
                 highlightthickness=0).pack(fill="x")

        def _populate_pylist():
            py_list.delete(0, "end")
            kind = kind_var.get()
            base = self._suggested_pyclasses(kind)
            # Always offer the canonical depot pyclass so a barebones scenario can author depots.
            if kind == "depot" and _SPAWN_DEPOT_PY not in base:
                base = [_SPAWN_DEPOT_PY] + base
            filt = py_search.get().strip().lower()
            for s in base:
                if not filt or filt in s.lower():
                    py_list.insert("end", s)

        def _on_py_pick(_=None):
            cur = py_list.curselection()
            if cur:
                py_var.set(py_list.get(cur[0]))

        py_search.bind("<KeyRelease>", lambda _: _populate_pylist())
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
            # Show/hide the PyClass picker.
            if kind in ("depot", "unit", "building", "spawn"):
                if not py_frame.winfo_ismapped():
                    py_frame.pack(fill="both", expand=False, padx=8, pady=(2, 0))
                # Snap the active pyclass to the kind's canonical default when switching INTO depot.
                if kind == "depot" and py_var.get() and _spawn_kind(py_var.get()) != "depot":
                    py_var.set(_SPAWN_DEPOT_PY)
                _populate_pylist()
            else:
                if py_frame.winfo_ismapped():
                    py_frame.pack_forget()
            # Kind-specific scalar fields.
            tk.Label(kf, text=t("FIELDS"), background=_R_BG_PANEL, foreground=_R_GOLD,
                     font=_F_BOLD).pack(anchor="w", pady=(6, 2))
            if kind in ("depot", "unit", "building", "spawn"):
                _row(kf, t("Camp (int)"), camp_var)
                tk.Label(kf,
                         text=t("blank = no Camp prop (Team 1; MP depot = despawn).  "
                                "-1 = visible-neutral.  N ≥ 0 = Team N+1."),
                         background=_R_BG_PANEL, foreground=_R_TEXT_DIM,
                         font=_F_MAIN, justify="left", wraplength=440).pack(anchor="w", padx=14)
                if kind == "depot":
                    _row(kf, t("ChampInteger"), champ_var)
                    tk.Label(kf, text=t("(supply = 9 × ChampInteger)"),
                             background=_R_BG_PANEL, foreground=_R_TEXT_DIM,
                             font=_F_MAIN).pack(anchor="w", padx=14)
            elif kind == "hq":
                _row(kf, t("AllianceNum"), alli_var, width=6)
                _row(kf, t("AlliancePriority"), pri_var, width=6)
                fr = tk.Frame(kf, background=_R_BG_PANEL); fr.pack(anchor="w", pady=(2, 0))
                tk.Checkbutton(fr, text=t("FFA seat (no AlliancePriority property)"),
                               variable=ffa_var,
                               background=_R_BG_PANEL, foreground=_R_TEXT, selectcolor=_R_BG_WIDGET,
                               font=_F_MAIN, activebackground=_R_BG_PANEL,
                               activeforeground=_R_GOLD).pack(side="left")
            elif kind in ("ville", "montagne"):
                _row(kf, t("ChampTexte"), text_var, width=30)
            elif kind == "circle":
                _row(kf, t("Radius"), radius_var)
            elif kind == "rect":
                _row(kf, t("Width"), w_var); _row(kf, t("Height"), h_var)
            # Position fields (always shown).
            tk.Label(kf, text=t("POSITION"), background=_R_BG_PANEL, foreground=_R_GOLD,
                     font=_F_BOLD).pack(anchor="w", pady=(6, 2))
            _row(kf, t("X"), x_var); _row(kf, t("Y"), y_var)
            tk.Label(kf, text=t("(Z defaults to terrain near (X,Y))"),
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
                messagebox.showerror(t("Bad position"), t("X and Y must be numbers."), parent=win)
                return
            wz_ = self._nearest_z(wx_, wy_)
            fields = {}
            py = None
            if kind in ("depot", "unit", "building", "spawn"):
                py = py_var.get().strip() or None
                if not py:
                    messagebox.showerror(t("Need PyClass"),
                                         t("Pick or type a PythonClassName."), parent=win)
                    return
                camp_s = camp_var.get().strip()
                if camp_s:
                    try:
                        fields["camp"] = int(camp_s)
                    except ValueError:
                        messagebox.showerror(t("Bad Camp"),
                                             t("Camp must be an integer (or blank for Team 1 / "
                                               "MP-depot despawn)."), parent=win)
                        return
                # else: leave fields["camp"] absent → _create_placement skips the Camp property,
                # matching the null encoding the engine reads as Team 1 / MP-depot despawn.
                if kind == "depot":
                    try:
                        fields["champ"] = int(champ_var.get())
                    except ValueError:
                        messagebox.showerror(t("Bad ChampInteger"),
                                             t("ChampInteger must be an integer."), parent=win)
                        return
            elif kind == "hq":
                try:
                    fields["alliance"] = int(alli_var.get())
                    fields["slot"] = None if ffa_var.get() else int(pri_var.get())
                except ValueError:
                    messagebox.showerror(t("Bad HQ fields"),
                                         t("AllianceNum and AlliancePriority must be integers."),
                                         parent=win)
                    return
            elif kind in ("ville", "montagne"):
                fields["text"] = text_var.get()
            elif kind == "circle":
                try: fields["radius"] = float(radius_var.get())
                except ValueError:
                    messagebox.showerror(t("Bad Radius"), t("Radius must be a number."), parent=win); return
            elif kind == "rect":
                try:
                    fields["w"] = float(w_var.get()); fields["h"] = float(h_var.get())
                except ValueError:
                    messagebox.showerror(t("Bad size"), t("Width and Height must be numbers."), parent=win); return
            item_idx = self._create_placement(kind, (wx_, wy_, wz_), py_class=py, fields=fields)
            if item_idx is not None:
                win.destroy()

        tk.Button(bar, text=t("Create"), command=_do_create, background="#163048",
                  foreground=_R_GOLD_BRT, font=_F_BOLD, relief="flat").pack(side="left")
        tk.Button(bar, text=t("Cancel"), command=win.destroy, background="#122030",
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

    def _open_script_editor(self):
        """Open a Toplevel that edits the paired Python-2 .xyz script for the current scenario.

        Read flow: bytes from IA_Common.dat → _xyz_unpack (strip XYZ0 container) →
        _xyz_decompile_to_source (xdis.unmarshal + uncompyle6) → display.

        Save flow: text → test_output/script_drafts/<map>_<scn>.py. Compiling back to a
        loadable XYZ0 needs Python-2 source compilation that has no clean Python-3 shim,
        so direct save into IA_Common is **not yet implemented** — the user runs an
        external Python-2 toolchain on the exported draft and drops the resulting .xyz
        into their mod project manually. The right pane carries explicit instructions."""
        if self._pndf is None or not self._scn_cb.get():
            messagebox.showinfo(t("No scenario"), t("Load a scenario first."), parent=self)
            return
        map_dir = self._map_cb.get()
        scn_name = self._scn_cb.get()
        script_path = self._script_path_for_scn(map_dir, scn_name)

        def _read_source():
            """Return (source_text, status_str). status_str is empty on success, otherwise
            a human-readable explanation rendered above the editor."""
            try:
                raw = self.project.get_raw("scripts", script_path)
            except Exception:
                raw = None
            if raw is None:
                return None, t("(new — not yet in the dat; use 'Save draft as .py' below)")
            # XYZ0 binary → decompiled Python source. Both stages can fail (corrupted .xyz,
            # uncompyle6 stumbling on an unusual instruction sequence) — bubble those up as
            # readable banners in the editor instead of crashing the popup.
            try:
                marshal_bytes, _h, _sz = _xyz_unpack(raw)
            except Exception as e:
                return ("# Could not unpack the XYZ0 container.\n"
                        "# %s\n"
                        "# Raw payload: %d bytes." % (e, len(raw)),
                        t("XYZ0 unpack failed — editor showing diagnostic only"))
            try:
                return _xyz_decompile_to_source(marshal_bytes), t("decompiled from XYZ0  ·  read-only round-trip")
            except Exception as e:
                return ("# Decompile failed — the .xyz contains valid Python-2.6 bytecode\n"
                        "# but uncompyle6 couldn't render it back to source:\n"
                        "#   %s\n"
                        "# (Marshal payload: %d bytes.)\n" % (e, len(marshal_bytes)),
                        t("decompile failed — see banner"))

        source, init_status = _read_source()
        is_new = source is None
        if is_new:
            source = _SCRIPT_TEMPLATE

        win = tk.Toplevel(self)
        win.title(t("Script — {map}/{scn}", map=map_dir, scn=scn_name))
        win.configure(background=_R_BG_PANEL)
        win.geometry("1200x720")

        # Top bar — virtual path + dirty/new indicator + status message.
        top = tk.Frame(win, background=_R_BG_PANEL); top.pack(fill="x", padx=8, pady=(6, 2))
        path_lbl = tk.Label(top, text=script_path, background=_R_BG_PANEL,
                            foreground=_R_TEXT_DIM, font=_F_MAIN, anchor="w")
        path_lbl.pack(side="left", fill="x", expand=True)
        status_lbl = tk.Label(top, text=init_status,
                              background=_R_BG_PANEL, foreground=_R_GOLD, font=_F_MAIN)
        status_lbl.pack(side="right")

        # Status banner — switches its message + colour based on whether the bundled Py27
        # interpreter is present. With Py27 in place the Save button writes a real .xyz
        # into IA_Common.dat; without it, we fall back to draft-export.
        py27 = _py27_interpreter_path()
        if py27 is not None:
            warn_text = t("Python 2.7 detected at {p}. Save to mod project compiles source → "
                          "Py2.7 marshal → XYZ0 → writes into IA_Common.dat (mod project copy, "
                          "never the live game). Decompile-edit-recompile round-trip is live.",
                          p=py27)
            warn_bg, warn_fg = "#18302a", "#a0e0c0"
        else:
            warn_text = t(".xyz files are XYZ0-magic Python-2.7 bytecode (NOT plain source). The "
                          "editor decompiles them for viewing, but saving needs a Py2.7 interpreter — "
                          "set one up via docs/map_editor/setup_py27.md, or use 'Save draft as .py' "
                          "to export source for external compilation.")
            warn_bg, warn_fg = "#3a2a18", "#ffd7a0"
        warn = tk.Label(win, anchor="w", justify="left", wraplength=1180,
                        background=warn_bg, foreground=warn_fg, font=_F_MAIN,
                        text=warn_text)
        warn.pack(fill="x", padx=8, pady=(2, 4))

        # Body — left = code editor with scrollbar; right = reference panel (notebook of snippets).
        body = tk.Frame(win, background=_R_BG_PANEL); body.pack(fill="both", expand=True, padx=8, pady=4)
        # Left: scrollable Text editor.
        left = tk.Frame(body, background=_R_BG_PANEL); left.pack(side="left", fill="both", expand=True)
        sb = tk.Scrollbar(left, orient="vertical")
        sb.pack(side="right", fill="y")
        editor = tk.Text(left, background=_R_BG_WIDGET, foreground=_R_TEXT,
                         font=("Consolas", 11), insertbackground=_R_GOLD_BRT,
                         relief="flat", highlightthickness=0, borderwidth=0,
                         wrap="none", undo=True, tabs=("2c",),
                         yscrollcommand=sb.set)
        editor.pack(side="left", fill="both", expand=True)
        sb.config(command=editor.yview)
        # Very lightweight syntax tinting — keywords + strings + comments via tags.
        editor.tag_configure("kw", foreground="#88bbff")
        editor.tag_configure("str", foreground="#cca070")
        editor.tag_configure("cmt", foreground="#7a8aa0", font=("Consolas", 11, "italic"))
        _PY_KW = ("import", "from", "def", "class", "return", "if", "else", "elif",
                  "for", "while", "in", "is", "not", "and", "or", "True", "False", "None")
        def _retint(*_):
            editor.tag_remove("kw", "1.0", "end")
            editor.tag_remove("str", "1.0", "end")
            editor.tag_remove("cmt", "1.0", "end")
            text = editor.get("1.0", "end")
            for kw in _PY_KW:
                idx = "1.0"
                while True:
                    idx = editor.search(r"\m%s\M" % kw, idx, stopindex="end", regexp=True)
                    if not idx: break
                    end = "%s+%dc" % (idx, len(kw))
                    editor.tag_add("kw", idx, end)
                    idx = end
            # Strings (single/double-quoted, single-line) + line comments.
            for pat, tag in ((r"'[^'\n]*'", "str"), (r'"[^"\n]*"', "str"),
                             (r"#[^\n]*", "cmt")):
                idx = "1.0"
                while True:
                    idx = editor.search(pat, idx, stopindex="end", regexp=True)
                    if not idx: break
                    m_end = editor.index("%s lineend" % idx)
                    # search the substring length manually since editor.search returns just start
                    sub = editor.get(idx, m_end)
                    import re as _re
                    rm = _re.match(pat, sub)
                    end = "%s+%dc" % (idx, len(rm.group(0))) if rm else m_end
                    editor.tag_add(tag, idx, end)
                    idx = end

        editor.insert("1.0", source)
        _retint()
        editor.bind("<KeyRelease>", _retint)

        # Right: reference panel — categorized docs + edit recipes + paste-able snippets +
        # enum lookup tables. The user said the primary use is tweaking parameters of
        # existing calls (not writing arbitrary code), so the panel leads with
        # overview docs + 'COMMON EDITS' recipes before any paste-snippet section.
        right = tk.Frame(body, background=_R_BG_PANEL, width=460)
        right.pack(side="right", fill="y", padx=(8, 0))
        right.pack_propagate(False)
        tk.Label(right, text=t("REFERENCE"),
                 background=_R_BG_PANEL, foreground=_R_GOLD, font=_F_BOLD,
                 wraplength=440, justify="left").pack(anchor="w")
        tk.Label(right,
                 text=t("Top-down: how scripts work → common edits → paste-able templates → enum value tables. "
                        "'insert' drops the code block at the editor's cursor."),
                 background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN,
                 wraplength=440, justify="left").pack(anchor="w", pady=(0, 4))
        # Scrollable container.
        rsc_outer = tk.Frame(right, background=_R_BG_PANEL); rsc_outer.pack(fill="both", expand=True)
        rsb = tk.Scrollbar(rsc_outer, orient="vertical")
        rsb.pack(side="right", fill="y")
        rsc = tk.Canvas(rsc_outer, background=_R_BG_PANEL, highlightthickness=0,
                        borderwidth=0, yscrollcommand=rsb.set)
        rsc.pack(side="left", fill="both", expand=True)
        rsb.config(command=rsc.yview)
        rsf = tk.Frame(rsc, background=_R_BG_PANEL)
        rsc_win = rsc.create_window(0, 0, anchor="nw", window=rsf)
        def _resize_inner(_=None):
            rsc.itemconfig(rsc_win, width=rsc.winfo_width())
            rsc.config(scrollregion=rsc.bbox("all"))
        rsf.bind("<Configure>", _resize_inner)
        rsc.bind("<Configure>", _resize_inner)

        def _make_inserter(body_text):
            def _go():
                editor.insert("insert", body_text)
                _retint()
            return _go

        # Entry renderers — one per kind. Wraplength tuned to 420px so long enum
        # descriptions don't overflow the 460-wide right pane.
        for entry in _SCRIPT_REFERENCE:
            kind = entry[0]
            if kind == "section":
                _, title = entry
                tk.Label(rsf, text=title, background=_R_BG_PANEL, foreground="#ffc774",
                         font=_F_BOLD, anchor="w").pack(fill="x", pady=(10, 2))
                tk.Frame(rsf, background="#3a2a18", height=1).pack(fill="x", pady=(0, 4))
            elif kind == "doc":
                _, title, body = entry
                tk.Label(rsf, text=title, background=_R_BG_PANEL, foreground=_R_GOLD,
                         font=_F_BOLD, anchor="w").pack(fill="x", pady=(4, 1))
                tk.Label(rsf, text=body, background=_R_BG_PANEL, foreground=_R_TEXT,
                         font=_F_MAIN, anchor="w", justify="left",
                         wraplength=420).pack(fill="x", padx=4)
            elif kind in ("recipe", "snippet"):
                _, title, body, code = entry
                head_row = tk.Frame(rsf, background=_R_BG_PANEL); head_row.pack(fill="x", pady=(6, 1))
                tk.Label(head_row, text=title, background=_R_BG_PANEL, foreground=_R_GOLD,
                         font=_F_BOLD, anchor="w").pack(side="left", fill="x", expand=True)
                tk.Button(head_row, text=t("insert"), command=_make_inserter(code),
                          background="#122030", foreground=_R_TEXT, font=_F_MAIN,
                          relief="flat").pack(side="right")
                tk.Label(rsf, text=body, background=_R_BG_PANEL, foreground=_R_TEXT,
                         font=_F_MAIN, anchor="w", justify="left",
                         wraplength=420).pack(fill="x", padx=4)
                tk.Label(rsf, text=code, background=_R_BG_WIDGET, foreground=_R_TEXT_DIM,
                         font=("Consolas", 9), anchor="w", justify="left",
                         wraplength=440).pack(fill="x", padx=2, pady=(2, 0))
            elif kind == "table":
                _, title, body, rows = entry
                tk.Label(rsf, text=title, background=_R_BG_PANEL, foreground=_R_GOLD,
                         font=_F_BOLD, anchor="w").pack(fill="x", pady=(4, 1))
                tk.Label(rsf, text=body, background=_R_BG_PANEL, foreground=_R_TEXT_DIM,
                         font=_F_MAIN, anchor="w", justify="left",
                         wraplength=420).pack(fill="x", padx=4)
                table_frame = tk.Frame(rsf, background=_R_BG_WIDGET)
                table_frame.pack(fill="x", padx=4, pady=(2, 0))
                for k, v in rows:
                    row = tk.Frame(table_frame, background=_R_BG_WIDGET); row.pack(fill="x")
                    tk.Label(row, text=k, background=_R_BG_WIDGET, foreground="#a0c0e0",
                             font=("Consolas", 9), anchor="w", width=18).pack(side="left")
                    tk.Label(row, text=v, background=_R_BG_WIDGET, foreground=_R_TEXT,
                             font=_F_MAIN, anchor="w", justify="left",
                             wraplength=290).pack(side="left", fill="x", expand=True)

        # Bottom buttons — Reload (re-read+decompile) / Save draft as .py (export to
        # test_output/script_drafts/) / Close. The path under the buttons shows where the
        # next 'Save draft' will land so the user can find the file for external compilation.
        bar = tk.Frame(win, background=_R_BG_PANEL); bar.pack(fill="x", padx=8, pady=(4, 8))
        REPO_ = os.path.dirname(os.path.abspath(__file__))
        draft_path = os.path.join(REPO_, "test_output", "script_drafts",
                                  "%s_%s.py" % (map_dir, scn_name))

        def _reload():
            fresh, st = _read_source()
            if fresh is None:
                if not messagebox.askyesno(t("Reload"),
                                           t("No script in the dat yet. Reset editor to the starter "
                                             "template? (Unsaved edits will be lost.)"),
                                           parent=win):
                    return
                fresh = _SCRIPT_TEMPLATE
            editor.delete("1.0", "end")
            editor.insert("1.0", fresh)
            _retint()
            status_lbl.config(text=st or t("reloaded"))

        def _save_draft():
            os.makedirs(os.path.dirname(draft_path), exist_ok=True)
            text = editor.get("1.0", "end-1c")
            try:
                with open(draft_path, "w", encoding="utf-8") as f:
                    f.write(text)
            except OSError as e:
                messagebox.showerror(t("Save draft failed"),
                                     t("Could not write the draft:\n{e}", e=e), parent=win)
                return
            status_lbl.config(text=t("draft saved → {path}", path=draft_path))

        def _save_to_mod():
            """Full source → Py2.7 marshal → XYZ0 → mod project's IA_Common.dat."""
            text = editor.get("1.0", "end-1c")
            try:
                marshal_bytes = _xyz_compile_source(text)
            except FileNotFoundError:
                messagebox.showerror(t("Python 2.7 missing"),
                                     t("tools/python27/python.exe wasn't found. See "
                                       "docs/map_editor/setup_py27.md to install."),
                                     parent=win)
                return
            except RuntimeError as e:
                messagebox.showerror(t("Compile failed"),
                                     t("Python 2 reported:\n\n{e}", e=str(e)), parent=win)
                return
            try:
                xyz_bytes = _xyz_pack(marshal_bytes)
                self.project.set_raw("scripts", script_path, xyz_bytes)
            except Exception as e:
                messagebox.showerror(t("Save failed"),
                                     t("Could not stage the XYZ0 into the mod project:\n{e}", e=e),
                                     parent=win)
                return
            status_lbl.config(text=t("saved → mod project's IA_Common.dat  ·  deploy to test in-game"))

        # Primary button switches behaviour based on Py27 availability — when present,
        # "Save to mod project" runs the full compile pipeline; otherwise we expose
        # only the draft export so users don't think a missing-Py27 save did anything.
        if _py27_interpreter_path() is not None:
            tk.Button(bar, text=t("Save to mod project"), command=_save_to_mod,
                      background="#163048", foreground=_R_GOLD_BRT,
                      font=_F_BOLD, relief="flat").pack(side="left")
            tk.Button(bar, text=t("Save draft as .py"), command=_save_draft,
                      background="#122030", foreground=_R_TEXT,
                      font=_F_BOLD, relief="flat").pack(side="left", padx=4)
        else:
            tk.Button(bar, text=t("Save draft as .py"), command=_save_draft,
                      background="#163048", foreground=_R_GOLD_BRT,
                      font=_F_BOLD, relief="flat").pack(side="left")
        tk.Button(bar, text=t("Reload from dat"), command=_reload, background="#122030",
                  foreground=_R_TEXT, font=_F_BOLD, relief="flat").pack(side="left", padx=4)
        tk.Button(bar, text=t("Close"), command=win.destroy, background="#122030",
                  foreground=_R_TEXT, font=_F_BOLD, relief="flat").pack(side="right")
        tk.Label(win, text=t("draft target: {path}", path=draft_path),
                 background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN,
                 anchor="w").pack(fill="x", padx=8, pady=(0, 6))

    def _delete_selected_place(self):
        pi = self._place_sel
        if pi is None or self._pndf is None or pi >= len(self._places):
            messagebox.showinfo(t("Nothing selected"), t("Select a placement to delete first."), parent=self)
            return
        pl = self._places[pi]
        if not messagebox.askyesno(t("Delete placement?"),
                                   t("Delete this {kind} ({label})?", kind=pl['kind'], label=pl['label']),
                                   parent=self):
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
            self._ox = ox0 + (e.x - x0)
            self._oy = oy0 + (e.y - y0)
            self._redraw()

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

    def _wheel(self, e):
        if not self._pil:
            return
        factor = 1.1 if e.delta > 0 else (1 / 1.1)
        bx = (e.x - self._ox) / self._scale
        by = (e.y - self._oy) / self._scale
        self._scale *= factor
        self._ox = e.x - bx * self._scale
        self._oy = e.y - by * self._scale
        self._recompose()           # zoom is a view change only — reuse both cached overlays

    def _hover(self, e):
        if self._pil and self._bbox:
            wx, wy = self._screen_to_world(e.x, e.y)
            cur = t("drag handle") if self._handle_at(e.x, e.y) is not None else \
                  (t("edit") if self._edit.get() else t("view"))
            self._status.config(text=(
                f"  {self._map_cb.get()} / {self._scn_cb.get()}{t('  *MODIFIED*') if self._dirty else ''}   "
                + t("sectors={ns}   cursor=({wx:.0f}, {wy:.0f})   "
                    "zoom={zoom:.2f}x   [{cur}]", ns=len(self._zones), wx=wx, wy=wy,
                    zoom=self._scale, cur=cur)))

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
        self._redraw()

    def _comp_state(self):
        # everything baked into the cached composite image; pan/placement-drag don't change these,
        # so they reuse the cached bitmap (only the few live edit items get redrawn each frame).
        return (id(self._zones), round(self._scale, 5), self._sel, self._flip_y.get(),
                self._pil.size if self._pil else None, self._dirty,
                self._show_roads.get(), self._show_forest.get(), self._show_sectors.get(),
                self._kdt_show.get(), self._kdt_color.get(), self._kdt_edit.get(), self._kdt_dirty,
                self._kdt_dx.get(), self._kdt_dy.get(), self._kdt_scale.get(),
                tuple(v.get() for v in self._sdb_show.values()), self._sdb_rev)

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
    def _ov_scale_for(self, W, H):
        # supersample base px so 1px overlay lines render ~1px on screen near zoom 1, capped ~2048px
        return max(1, min(8, 2048 // max(W, H, 1)))

    def _ov_world_to_overlay(self, x, y, ovs):
        bx, by = self._world_to_base(x, y)
        return bx * ovs, by * ovs

    def _build_sdb_overlay(self):
        """Rasterise the visible SDB paint layers into _ov_sdb (overlay space). Direct numpy fill from
        the R×R grid → one Image.fromarray + a NEAREST resize per layer (no per-cell Python loop, no
        grid_to_cells). This is what makes a paint stroke refresh in ms instead of ~10s."""
        W, H = self._pil.size; ovs = self._ov_scale
        OW, OH = max(1, W * ovs), max(1, H * ovs)
        ov = Image.new("RGBA", (OW, OH), (0, 0, 0, 0))
        if self._bbox and self._sdb:
            try:
                import numpy as np
            except Exception:
                np = None
            if np is not None:
                R = self._sdb["R"]; bboxX = self._sdb["bboxX"]; bboxY = self._sdb["bboxY"]
                g = np.frombuffer(bytes(self._sdb["grid"]), np.uint8).reshape(R, R)
                # destination box (overlay px) for the SDB world extent [0,bboxX]×[0,bboxY]
                bx0, by0 = self._ov_world_to_overlay(0.0, 0.0, ovs)
                bx1, by1 = self._ov_world_to_overlay(bboxX, bboxY, ovs)
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

    def _build_vec_overlay(self):
        """Rasterise sectors + KDT mesh + roads into _ov_vec (overlay space). Heavy (vector primitives),
        so it is rebuilt ONLY when that geometry/toggles change — never on pan/zoom, and frozen while a
        sector/KDT vertex is being dragged (the moving handles/outline are live canvas items instead)."""
        W, H = self._pil.size; ovs = self._ov_scale
        OW, OH = max(1, W * ovs), max(1, H * ovs)
        ov = Image.new("RGBA", (OW, OH), (0, 0, 0, 0))
        d = ImageDraw.Draw(ov, "RGBA")

        def b(x, y):
            return self._ov_world_to_overlay(x, y, ovs)

        def onscreen(px, py, pad=8):
            return -pad <= px <= OW + pad and -pad <= py <= OH + pad

        lw = max(1, ovs)                                   # 1px-at-zoom-1 line width
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
                rw = max(2, 2 * ovs); rr = max(2, ovs + 1)
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
        if not self._dragging:                            # while dragging we keep the stale cache
            self._ov_vec_dirty = False

    def _build_composite(self):
        """Compose the visible viewport: crop+resize the base image, then crop+resize the cached SDB
        and vector overlays over it. The overlays are (re)rasterised only when their data is dirty —
        pan/zoom just re-crop the existing bitmaps, so this is a handful of C-level PIL ops per frame."""
        W, H = self._pil.size
        sc = self._scale
        cw = max(self._canvas.winfo_width(), 1); ch = max(self._canvas.winfo_height(), 1)
        margin = 96                                    # screen-px slack so small pans don't rebuild
        cx0 = max(0, int(math.floor((-self._ox - margin) / sc)))
        cy0 = max(0, int(math.floor((-self._oy - margin) / sc)))
        cx1 = min(W, int(math.ceil((cw - self._ox + margin) / sc)))
        cy1 = min(H, int(math.ceil((ch - self._oy + margin) / sc)))
        if cx1 <= cx0 or cy1 <= cy0:                   # panned off-map — fall back to whole map
            cx0, cy0, cx1, cy1 = 0, 0, W, H
        dw = max(1, int(round((cx1 - cx0) * sc))); dh = max(1, int(round((cy1 - cy0) * sc)))
        resample = Image.NEAREST if sc >= 1 else Image.BILINEAR
        img = self._pil.crop((cx0, cy0, cx1, cy1)).resize((dw, dh), resample)  # _pil is already RGBA
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
        for cache in (self._ov_sdb, self._ov_vec):     # SDB under vectors
            if cache is None:
                continue
            crop = cache.crop(ob)
            if crop.size != (dw, dh):
                crop = crop.resize((dw, dh), Image.BILINEAR)
            img.alpha_composite(crop)
        self._comp_tk = ImageTk.PhotoImage(img)
        self._comp_rect = (cx0, cy0, cx1, cy1)
        self._comp_key = self._comp_state()

    def _redraw(self):
        c = self._canvas
        c.delete("all")
        if not self._pil:
            c.create_text(20, 20, anchor="nw", fill=_R_TEXT_DIM, font=_F_MAIN,
                          text=t("(no minimap — terrain dat not found for this map)"))
            return
        vis = self._visible_base_rect()
        if self._comp_key != self._comp_state() or not self._rect_covers(self._comp_rect, vis):
            self._build_composite()
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

        self._status.config(text=(
            f"  {self._map_cb.get()} / {self._scn_cb.get()}{t('  *MODIFIED*') if self._dirty else ''}   "
            + t("sectors={ns}   "
                "selected={sel}   "
                "zoom={zoom:.2f}x   "
                "drag=pan · wheel=zoom · sectors: select in the left list",
                ns=len(self._zones),
                sel=('#' + str(self._zones[self._sel]['idx']) if self._sel is not None else '-'),
                zoom=self._scale)))

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
        return None if best is None else (best[1], best[2], math.sqrt(best[0]))

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
        self._place_lbl.config(text=t("{n} placements  ·  {kb:.1f} KB on disk\n(spawn cap = file size; ~600 budget)",
                                      n=n, kb=(self._scn_size or 0) / 1024.0))

    def _get_icon(self, name, size):
        """Load+cache a map-editor placement icon (icons/map_icons/<name>) fit to `size` px,
        aspect-preserved, as an ImageTk.PhotoImage. None if missing/unavailable."""
        if not name or not _HAVE_PIL:
            return None
        key = (name, size)
        if key not in self._icon_cache:
            try:
                with pil_log.source("Map editor"):
                    im = Image.open(os.path.join(ICON_DIR, name)).convert("RGBA")
                w, h = im.size
                s = size / max(w, h)
                im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
                self._icon_cache[key] = ImageTk.PhotoImage(im)
            except Exception:
                self._icon_cache[key] = None
        return self._icon_cache[key]

    def _draw_placements(self, c):
        """Draw depot/HQ/label/zone markers as live canvas items (not baked into the composite),
        so they track drag edits. HQ + depot are oriented footprints (rotate with their Rotation);
        when selected in edit mode a gold rotation handle lets you set the facing."""
        if not (self._show_places.get() and self._places and self._bbox):
            return
        for pi, pl in enumerate(self._places):
            sx, sy = self._world_to_screen(pl["pos"][0], pl["pos"][1])
            kind = pl["kind"]
            col = "#%02x%02x%02x" % _PLACE_COL.get(kind, _PLACE_COL["unknown"])
            sel = (pi == self._place_sel)
            outline = "#ffffff" if sel else "#001018"
            ow = 2 if sel else 1
            if kind in ("hq", "depot"):
                ang = self._facing_screen_angle(pl["pos"][0], pl["pos"][1], self._place_facing(pl))
                if kind == "hq" and sel and self._place_edit.get() and pl["extra"].get("cam"):
                    R = self._cam_ring_radius(pl)                       # camera-orbit ring (selected + edit only)
                    rxp = abs(self._world_to_screen(pl["pos"][0] + R, pl["pos"][1])[0] - sx)
                    ryp = abs(self._world_to_screen(pl["pos"][0], pl["pos"][1] + R)[1] - sy)
                    c.create_oval(sx - rxp, sy - ryp, sx + rxp, sy + ryp, outline="#6a86ab", dash=(4, 3))
                if kind == "hq" and pl["extra"].get("cam"):             # dashed link to resting camera
                    cxs, cys = self._world_to_screen(*pl["extra"]["cam"][:2])
                    c.create_line(sx, sy, cxs, cys, fill="#6688aa", width=1, dash=(3, 2))
                    c.create_rectangle(cxs - 3, cys - 3, cxs + 3, cys + 3, outline="#88bbff", fill="#22364a")
                    c.create_text(cxs, cys - 8, text=t("cam"), fill="#88bbff", font=_F_MAIN)
                isz = 30 if kind == "hq" else 26
                ico = self._get_icon(_PLACE_ICON.get(kind), isz)
                # road-facing tick poking out past the icon edge
                c.create_line(sx + isz * 0.45 * math.cos(ang), sy + isz * 0.45 * math.sin(ang),
                              sx + isz * 0.80 * math.cos(ang), sy + isz * 0.80 * math.sin(ang),
                              fill="#ffd24a", width=2)
                if sel:
                    h = isz // 2 + 2
                    c.create_rectangle(sx - h, sy - h, sx + h, sy + h, outline="#ffffff", width=2)
                if ico:
                    c.create_image(sx, sy, image=ico)
                else:                                                   # fallback if icon missing
                    c.create_rectangle(sx - 7, sy - 7, sx + 7, sy + 7, fill=col, outline=outline, width=ow)
                if kind == "hq":
                    c.create_text(sx, sy + isz // 2 + 7, text=pl["label"], fill="#ffd0d0", font=_F_MAIN)
            elif kind in ("ville", "montagne"):
                rr = 5 if sel else 3
                c.create_oval(sx - rr, sy - rr, sx + rr, sy + rr, fill=col, outline=outline, width=ow)
                if self._lbl_var.get() and pl["label"]:
                    c.create_text(sx + 6, sy, text=pl["label"], fill="#a8e0c0",
                                  font=_F_MAIN, anchor="w")
            elif kind in ("circle", "rect"):
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
                c.create_oval(sx - 3, sy - 3, sx + 3, sy + 3, fill=col, outline=outline)
            else:
                r = 6 if sel else 4
                c.create_oval(sx - r, sy - r, sx + r, sy + r, fill=col, outline=outline, width=ow)

    # ── save / revert ─────────────────────────────────────────────────────────
    def _confirm_discard(self):
        if not self._dirty:
            return True
        return messagebox.askyesno(t("Discard changes?"),
                                   t("This scenario has unsaved edits. Discard them?"), parent=self)

    def _revert(self):
        if self._scn is not None:
            self._dirty = False
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
            map_dir = self._map_cb.get(); scn = self._scn_cb.get()
            vp = f"test\\map\\{map_dir}\\{scn}.scenario"
            try:
                # re-embed the (possibly edited) placement NDF; byte-identical when unchanged
                if self._pndf is not None:
                    self._scn["ndf_data"] = _pad4(ndfbin.write(self._pndf,
                                                               compress=self._pndf.is_compressed))
                data = scenario_mod.write(self._scn)
                scenario_mod.read(data)        # sanity: must reparse
            except Exception as e:
                messagebox.showerror(t("Save failed"), t("Could not serialize scenario:\n{e}", e=e), parent=self)
                return
            self.project.set_raw("maps", vp, data)
            staged.append(t("{scn}.scenario (placements)", scn=scn))
        # warmup campath (the real start camera; PositionCamera is inert)
        if self._campath_dirty and self._campath is not None and self._campath_vpath:
            try:
                self.project.set_raw("maps", self._campath_vpath,
                                     ndfbin.write(self._campath, compress=self._campath.is_compressed))
            except Exception as e:
                messagebox.showerror(t("Save failed"), t("Could not serialize the start camera:\n{e}", e=e),
                                     parent=self)
                return
            staged.append(t("start camera"))
        # painted AI-terrain SDB layer (unified codec, edit-in-place -> mapinfo.win buffer4)
        if self._sdb and self._sdb.get("dirty"):
            try:
                new_sdb = sdb_mod.serialize(self._sdb["parsed"])
                new_win = sdb_mod.replace_buffer4(self._sdb["win"], new_sdb)
            except Exception as e:
                messagebox.showerror(t("Save failed"), t("Could not rebuild the SDB layer:\n{e}", e=e), parent=self)
                return
            self.project.set_raw("maps", self._sdb["win_vpath"], new_win)
            self._sdb["win"] = new_win
            staged.append(t("AI-terrain / SDB"))
        # edited capture mesh (KDT verts; Eugen's tree preserved) — shelved, only when dirty
        if self._kdt_dirty and self._kdt_vpath and self._kdt_bytes is not None:
            try:
                new_kdt = kdt_mod.encode_mesh(self._kdt_bytes, self._kdt_world)
            except Exception as e:
                messagebox.showerror(t("Save failed"), t("Could not encode the capture mesh:\n{e}", e=e),
                                     parent=self)
                return
            self.project.set_raw("maps", self._kdt_vpath, new_kdt)
            self._kdt_bytes = new_kdt
            staged.append(t("capture mesh (KDT)"))
        # the lobby/game-modes change (globals.cpp) was already mark_dirty'd by "Apply game modes".
        if not self.project.is_dirty():
            messagebox.showinfo(t("Save to mod"), t("No pending changes to save."), parent=self)
            return
        try:
            written = self.project.save_all()
        except Exception as e:
            messagebox.showerror(t("Save failed"),
                                 t("Could not write the mod's dat file(s):\n{e}", e=e), parent=self)
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
        body = (t("Saved into the mod:\n  ") + "\n  ".join(staged)) if staged \
            else t("Saved staged lobby / game-mode changes into the mod.")
        messagebox.showinfo(t("Saved"),
                            t("{body}\n\nWritten dat file(s):\n  {written}"
                              "\n\nUse \"Deploy to Game\" in the Mod Editor hub to apply this mod.",
                              body=body, written="\n  ".join(written)),
                            parent=self)


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
    root.title(t("R.U.S.E. Map Editor (standalone dev) — {name}", name=proj.name))
    root.geometry("1180x800")
    root.minsize(980, 660)
    root.configure(background=_R_BG)
    MapEditorWindow(root, proj).pack(fill="both", expand=True)
    root.mainloop()


if __name__ == "__main__":
    main()

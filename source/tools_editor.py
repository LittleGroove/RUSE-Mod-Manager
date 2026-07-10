"""
RUSE Raw / Asset Editor — the project-aware "everything" tool.

Opened from the Mod Editor hub with a live ModProject. It edits the SAME project space as the
Units/Economy/AI windows: it reads each game file from the mod's own .dat when one exists, otherwise
from the clean backup (or the live game), and its single Save flushes every accumulated change into
the mod's .dat via ``project.save_all()`` — exactly like the other editor windows.

Three tabs over the picked project dat (Gameplay / AI Scripts / Localization):
  • Browse / Files — every archive entry, with a live preview of what's supported (images incl. .tgv
    textures, .dic / .xyz / plain text, an NDF summary, hex fallback) plus Export and Import (replace).
  • NDF Vars       — drill NDF file → instance → property and edit values (mutates the shared NDF object,
    so edits compose with the other editors).
  • Search         — find NDF instances/properties by class / property / value; double-click jumps to Vars.

Import (replace) stages bytes into the project (set_raw); nothing touches disk until Save.

Nested archives: a .ipk/.apk/.mpk/.ppk (or any embedded .dat) inside one of the six dats is itself an
edata container.  "Open as nested .dat" on such an entry opens it in its own editor window (backed by a
NestedDatStore over a temp working copy); editing it and clicking "Apply into parent .dat" stages the
rebuilt archive back into THIS window.  Saving here then writes it into the .dat on disk.  The window
drives a small store abstraction (ProjectDatStore | NestedDatStore), so nesting works to any depth.
"""
import io
import os
import shutil
import struct
import sys
import tempfile
import threading
import zlib
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog

REPO = os.path.dirname(os.path.abspath(__file__))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
from ruse_mod_engine import mod_project as mp_mod   # noqa: E402
from ruse_mod_engine import ndfbin as ndfbin_mod    # noqa: E402
from ruse_mod_engine import dic as dic_mod          # noqa: E402
from ruse_mod_engine import edata as edata_mod      # noqa: E402
from ruse_mod_engine import xyz_compile as xyz_mod  # noqa: E402  (.xyz decompile/recompile)
from ruse_mod_engine import scenario as scenario_mod  # noqa: E402  (.scenario read for summary)
from ruse_mod_engine import kdt as kdt_mod          # noqa: E402  (.kdt read for summary)
from ruse_mod_engine import sdb as sdb_mod          # noqa: E402  (.sdb read for summary)
try:
    from ruse_mod_engine import terrain_codec as terrain_mod   # noqa: E402  (.tmst decode; needs numpy+PIL)
except Exception:
    terrain_mod = None
import pil_log                                       # noqa: E402  (tags PIL DEBUG as "Raw editor")
from i18n import t                                    # noqa: E402
import ui_util                                        # noqa: E402  — language-aware widget sizing

try:
    from PIL import Image, ImageTk
    _HAVE_PIL = True
except Exception:
    _HAVE_PIL = False

# ── theme (matches the other editor windows) ─────────────────────────────────────
import theme                # single source of truth for the palette; local _R_* names kept unchanged
_R_BG, _R_BG_PANEL, _R_BG_WIDGET = theme.BG, theme.PANEL, theme.WIDGET
_R_GOLD, _R_GOLD_BRT = theme.GOLD, theme.GOLD_BRT
_R_TEXT, _R_TEXT_DIM = theme.TEXT, theme.DIM
_R_SEL_BG, _R_SEL_FG = theme.SEL_BG, theme.SEL_FG
_R_GREEN, _R_RED, _R_BORDER = theme.GREEN, theme.RED, theme.BORDER
_F_MAIN = theme.F
_F_BOLD = theme.FB
_F_HEAD = theme.FHEAD

# dat_key -> friendly label for the picker.  ALL six public dats (every one that gets backed up) — edit
# and Save writes the working copy into the project folder just like the gameplay dat.  Order matches the
# game's own layout loosely (gameplay first, then maps, localization, common).
_DAT_CHOICES = [
    ("gameplay",    t("tools.gameplay_zz_gladpatchablewin_dat")),
    ("gameplay_np", t("tools.gameplay_non_patchable_zz_gladnotpatchab")),
    ("scripts",     t("tools.ai_scripts_ia_common_dat")),
    ("maps",        t("tools.maps_datamap_win_dat")),
    ("loc",         t("tools.localization_textures_zz_win_dat")),
    ("common",      t("tools.common_video_fonts_data_common")),
]

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

_NDF_EXTS = (".gladndfbin", ".ndfbin", ".truendfbin")
_TGV_EXTS = (".tgv", ".tgv_pc")
_IMG_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tga", ".dds")
# NOTE: .scenario is NOT text — it's the binary SCEN container (handled as a read-only summary).
_TEXT_EXTS = (".xml", ".txt", ".ndf", ".lua", ".cfg", ".ini", ".csv", ".json")
# Structured binaries the raw editor previews READ-ONLY (full editing lives in the map/scenario editors).
_SCENARIO_EXTS = (".scenario",)
_KDT_EXTS = (".kdt",)
_SDB_EXTS = (".sdb",)
_TMST_EXTS = (".tmst_pc", ".tmst_chunk_pc")            # high-def terrain index + chunk (decode as a pair)
# Renamed .dat archives that live INSIDE the six main dats: .ipk (Python), .apk (animation),
# .mpk (sound), .ppk (textures), .gpk (packed textures) — plus a literal embedded .dat.  These are
# edata containers openable recursively (gated on the real 'edat' magic, so the extension list is
# only a hint for the Type column and for which entries offer "Open as nested .dat").
_NESTED_DAT_EXTS = (".ipk", ".apk", ".mpk", ".ppk", ".gpk", ".dat")


# ── small format helpers ──────────────────────────────────────────────────────────

def _arc_get(arc, path):
    """Fetch an entry tolerating slash direction differences in the archive key."""
    d = arc.get(path)
    if d is None:
        d = arc.get(path.replace("/", "\\")) if "/" in path else arc.get(path.replace("\\", "/"))
    return d


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
        items = [_fmt_val(x, ndf) for x in r[:4]]
        suf = f" …+{len(r) - 4}" if len(r) > 4 else ""
        return "[" + ", ".join(items) + suf + "]"
    if t == _T.Map:
        return f"Map{{{len(r)} entries}}"
    if t in (_T.StringRef, _T.PathRef):
        return repr(ndf.resolve_value(val))
    return repr(r)[:150]


def _parse_val(raw: str, type_name: str):
    try:
        if type_name == "Bool":
            return 1 if raw.strip().lower() in ("1", "true", "yes", "on") else 0
        if type_name in ("Int8", "Int16", "UInt16", "Int32", "UInt32", "Long"):
            return int(raw.strip())
        if type_name in ("Float32", "Float64"):
            return float(raw.strip())
        if type_name in ("StringRef", "PathRef", "WideStr"):
            return raw
        if type_name in ("Vector3", "Color128", "Color32", "TripleInt", "Int2", "Float2"):
            return [float(x.strip()) for x in raw.strip("[]()").split(",")]
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


def _entry_kind(path: str) -> str:
    p = path.lower()
    if p.endswith(_NDF_EXTS):
        return "NDF"
    if p.endswith(_TGV_EXTS):
        return "Texture (TGV)"
    if p.endswith(_IMG_EXTS):
        return "Image"
    if p.endswith(".dic"):
        return "Localization"
    if p.endswith(".xyz"):
        return "AI Script"
    if p.endswith(_SCENARIO_EXTS):
        return "Scenario"
    if p.endswith(_KDT_EXTS):
        return "KDT"
    if p.endswith(_SDB_EXTS):
        return "SDB"
    if p.endswith(_TMST_EXTS):
        return "Terrain (TMST)"
    if p.endswith(_TEXT_EXTS):
        return "Text"
    if p.endswith(_NESTED_DAT_EXTS):
        return "Nested .dat"
    return "Binary"


def _sniff_kind(path: str, raw: bytes) -> str:
    """Effective kind, upgrading an otherwise-'Binary' entry by MAGIC so odd/renamed extensions still
    get a real preview (e.g. a .shc that is actually EUG0+CNDF NDF, or a renamed .dic/.xyz/.scenario)."""
    k = _entry_kind(path)
    if k != "Binary" or not raw:
        return k
    if raw[:4] == b"EUG0":                 # NDF binary (EUG0+CNDF), any extension
        return "NDF"
    if raw[:8] == b"SCENARIO":
        return "Scenario"
    if raw[:5] == b"SDB\r\n":
        return "SDB"
    if raw[:3] == b"TRA":
        return "Localization"
    if raw[:4] == b"XYZ0":
        return "AI Script"
    return "Binary"


def _scenario_summary(raw: bytes) -> str:
    sc = scenario_mod.read(raw)
    zones = sc.get("zones", [])
    lines = []
    try:
        ndf = ndfbin_mod.read(sc["ndf_data"])
        lines.append(t("tools.scenario_z_zone_s_embedded",
                       z=len(zones), i=len(ndf.instances), c=len(ndf.classes)))
    except Exception:
        lines.append(t("tools.scenario_z_zone_s", z=len(zones)))
    lines.append("─" * 60)
    for zn in zones[:60]:
        pos = zn.get("pos", (0, 0, 0))
        lines.append("  zone '{n}'   pos=({x:.0f},{y:.0f},{z:.0f})   {v} verts / {f} faces".format(
            n=zn.get("name", "?"), x=pos[0], y=pos[1], z=pos[2],
            v=len(zn.get("vertices", [])), f=zn.get("face_count", 0)))
    if len(zones) > 60:
        lines.append(t("tools.n_more_zones", n=len(zones) - 60))
    return "\n".join(lines)


def _kdt_summary(raw: bytes) -> str:
    k = kdt_mod.read(raw)
    return t("tools.kd_tree_tri_triangles_vtx",
             tri=k.tri_count, vtx=k.vtx_count, sub=k.subtree_count,
             x0=k.bbox_min[0], y0=k.bbox_min[1], z0=k.bbox_min[2],
             x1=k.bbox_max[0], y1=k.bbox_max[1], z1=k.bbox_max[2])


def _sdb_summary(raw: bytes) -> str:
    g = sdb_mod.decode_grid(raw)
    grid, R = g["grid"], g["R"]
    nonzero = sum(1 for c in grid if c)
    return t("tools.sdb_terrain_grid_r_r",
             R=R, tot=R * R, nz=nonzero, bx=g.get("bboxX"), by=g.get("bboxY"), cell=g.get("cell"))


def _tmst_pair_paths(path: str):
    """(index_path, chunk_path) for a selected .tmst_pc/.tmst_chunk_pc — they share a stem."""
    p = path
    for suf in (".tmst_chunk_pc", ".tmst_pc"):
        if p.lower().endswith(suf):
            stem = p[:-len(suf)]
            return stem + ".tmst_pc", stem + ".tmst_chunk_pc"
    return None, None


# ── structured read-only "view as raw" builders (Phase 3) ─────────────────────────
# Each returns plain nested dict/list/scalar which _view_structure_dialog renders in a tree.

_STRUCT_LIST_CAP = 400          # max list children shown per node
_STRUCT_INST_CAP = 300          # max embedded-NDF instances detailed


def _hexbytes(b) -> str:
    return b.hex() if isinstance(b, (bytes, bytearray)) else str(b)


def _struct_scenario(raw: bytes) -> dict:
    sc = scenario_mod.read(raw)
    hdr = sc.get("header", {})
    out = {
        "header": {k: (_hexbytes(v) if isinstance(v, (bytes, bytearray)) else v) for k, v in hdr.items()},
        "zone_count": len(sc.get("zones", [])),
        "zones": [
            {"name": zn.get("name", "?"), "pos": zn.get("pos"),
             "subparts": len(zn.get("subparts", [])), "border_faces": zn.get("border_faces"),
             "border_vtx": zn.get("border_vtx"), "vertices": len(zn.get("vertices", [])),
             "faces": zn.get("face_count", 0), "area7_len": len(zn.get("area7", []))}
            for zn in sc.get("zones", [])
        ],
    }
    try:
        ndf = ndfbin_mod.read(sc["ndf_data"])
        prop_by_idx = {p.index: p for p in ndf.properties}
        cls_by_idx = {c.index: c.name for c in ndf.classes}
        insts = []
        for i, inst in enumerate(ndf.instances[:_STRUCT_INST_CAP]):
            props = {}
            for pv in inst.props:
                pr = prop_by_idx.get(pv.prop_index)
                if pr:
                    props[pr.name] = _fmt_val(pv.value, ndf)
            insts.append({"class": cls_by_idx.get(inst.class_index, "?"), "properties": props})
        out["embedded_ndf"] = {"instances": len(ndf.instances), "classes": len(ndf.classes),
                               "properties": len(ndf.properties), "instance_detail": insts}
    except Exception as e:
        out["embedded_ndf"] = {"error": str(e)}
    return out


def _struct_kdt(raw: bytes) -> dict:
    k = kdt_mod.read(raw)
    return {
        "bbox_min": k.bbox_min, "bbox_max": k.bbox_max,
        "tri_count": k.tri_count, "subtree_count": k.subtree_count, "vtx_count": k.vtx_count,
        "offsets": dict(k.offsets or {}),
        "vertices": [list(v) for v in k.vertices],
    }


def _struct_sdb(raw: bytes) -> dict:
    from collections import Counter
    g = sdb_mod.decode_grid(raw)
    grid, R = g["grid"], g["R"]
    hist = Counter(grid)
    try:
        ents = sdb_mod.build_ents(grid, R)
        ent_count = len(ents)
    except Exception:
        ent_count = None
    return {
        "R": R, "cells": R * R, "bboxX": g.get("bboxX"), "bboxY": g.get("bboxY"),
        "cell": g.get("cell"), "non_empty_cells": sum(1 for c in grid if c),
        "layer_byte_histogram": {("0x%02X" % b): n for b, n in sorted(hist.items())},
        "quadtree_entries": ent_count,
    }


def _sdb_grid_image(raw: bytes):
    """Render the SDB per-cell layer grid as an RxR image (distinct colour per layer byte, black=empty)."""
    if not _HAVE_PIL:
        return None
    g = sdb_mod.decode_grid(raw)
    grid, R = g["grid"], g["R"]
    px = [((0, 0, 0) if b == 0 else ((b * 37) % 256, (b * 91 + 60) % 256, (b * 53 + 120) % 256))
          for b in grid]
    img = Image.new("RGB", (R, R))
    img.putdata(px)
    scale = max(1, 640 // R)
    return img.resize((R * scale, R * scale), Image.NEAREST)


def _kdt_vertex_image(raw: bytes, size: int = 640):
    """Top-down scatter of the KD-tree vertices, mapped from its world bbox (a quick look at the mesh)."""
    if not _HAVE_PIL:
        return None
    k = kdt_mod.read(raw)
    verts = k.vertices
    if not verts:
        return None
    x0, y0 = k.bbox_min[0], k.bbox_min[1]
    x1, y1 = k.bbox_max[0], k.bbox_max[1]
    dx = (x1 - x0) or 1.0
    dy = (y1 - y0) or 1.0
    img = Image.new("RGB", (size, size), (15, 15, 25))
    px = img.load()
    for vx, vy in verts:
        ix = int((vx - x0) / dx * (size - 1))
        iy = int((1.0 - (vy - y0) / dy) * (size - 1))     # flip Y so north is up
        if 0 <= ix < size and 0 <= iy < size:
            px[ix, iy] = (90, 220, 120)
    return img


def _is_edata(b) -> bool:
    """True if the bytes are an edata (.dat) container — i.e. openable as a nested archive."""
    return bool(b) and b[:4] == edata_mod.EDATA_MAGIC


def _looks_text(b: bytes) -> bool:
    if not b:
        return False
    sample = b[:4096]
    if b"\x00" in sample and sample[:2] not in (b"\xff\xfe", b"\xfe\xff"):
        return False
    printable = sum(1 for c in sample if 9 <= c <= 13 or 32 <= c <= 126 or c >= 160)
    return printable / len(sample) > 0.85


def _decode_text(b: bytes) -> str:
    for enc in ("utf-8", "utf-16-le", "latin-1"):
        try:
            return b.decode(enc)
        except Exception:
            continue
    return b.decode("latin-1", "replace")


def _text_codec(raw: bytes):
    """Decode text bytes AND return an encoder that reproduces the SAME on-disk encoding (BOM + charset)
    so an in-place text edit round-trips exactly. Returns (text, encode_fn)."""
    if raw[:2] == b"\xff\xfe":
        return raw[2:].decode("utf-16-le", "replace"), (lambda s: b"\xff\xfe" + s.encode("utf-16-le"))
    if raw[:2] == b"\xfe\xff":
        return raw[2:].decode("utf-16-be", "replace"), (lambda s: b"\xfe\xff" + s.encode("utf-16-be"))
    if raw[:3] == b"\xef\xbb\xbf":
        return raw.decode("utf-8-sig", "replace"), (lambda s: b"\xef\xbb\xbf" + s.encode("utf-8"))
    try:
        raw.decode("utf-8")
        return raw.decode("utf-8"), (lambda s: s.encode("utf-8"))
    except Exception:
        return raw.decode("latin-1"), (lambda s: s.encode("latin-1"))


def _hexdump(b: bytes, limit: int = 8192) -> str:
    out = []
    chunk = b[:limit]
    for i in range(0, len(chunk), 16):
        row = chunk[i:i + 16]
        h = " ".join(f"{x:02x}" for x in row)
        a = "".join(chr(x) if 32 <= x < 127 else "." for x in row)
        out.append(f"{i:08x}  {h:<48}  {a}")
    if len(b) > limit:
        out.append(t("tools.n_more_bytes_export_see", n=len(b) - limit))
    return "\n".join(out)


def _xyz_inflate(b: bytes):
    """Best-effort decompress a .xyz AI script (XYZ0 + md5 + zlib stream). Returns text or None."""
    for magic in (b"\x78\x9c", b"\x78\xda", b"\x78\x01"):
        i = b.find(magic)
        if 0 <= i < 64:
            try:
                dec = zlib.decompressobj().decompress(b[i:])
                return _decode_text(dec)
            except Exception:
                return None
    return None


def _dic_text(b: bytes, limit: int = 4000) -> str:
    entries = dic_mod.read(b)
    head = t("tools.tra_localization_n_entries_key", n=len(entries)) + "─" * 60 + "\n"
    lines = [f"{k.hex()}  {v}" for k, v in entries[:limit]]
    if len(entries) > limit:
        lines.append(t("tools.n_more_entries_export_see", n=len(entries) - limit))
    return head + "\n".join(lines)


# ── TGV texture decode → PIL image ────────────────────────────────────────────────

def _parse_tgv(data: bytes):
    if len(data) < 32:
        return None
    try:
        v1, v2, w, h, w2, h2 = struct.unpack_from("<6I", data, 0)
        mip_count, fmt_len = struct.unpack_from("<2H", data, 24)
        if mip_count == 0 or mip_count > 16 or fmt_len == 0 or fmt_len > 16:
            return None
        fmt = data[28:28 + fmt_len].decode("ascii", "replace")
        table = 28 + fmt_len
        offs = struct.unpack_from("<%dI" % mip_count, data, table)
        sizes = struct.unpack_from("<%dI" % mip_count, data, table + mip_count * 4)
    except Exception:
        return None
    mips = []
    for i in range(mip_count):
        off = offs[i]
        if off + 8 > len(data) or data[off:off + 4] != b"ZIPO":
            continue
        decomp_size = struct.unpack_from("<I", data, off + 4)[0]
        blob = data[off + 8: off + sizes[i]]
        try:
            px = zlib.decompressobj().decompress(blob)
            if len(px) == decomp_size:
                mips.append((decomp_size, px))
        except Exception:
            continue
    if not mips:
        return None
    return {"w": w, "h": h, "fmt": fmt.strip("\x00").upper(), "mips": mips}


def _dds_bytes(fourcc: str, w: int, h: int, data: bytes) -> bytes:
    """Wrap raw DXT block data in a minimal DDS container so PIL can decode it."""
    DDSD = 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000          # CAPS|HEIGHT|WIDTH|PIXELFORMAT|LINEARSIZE
    blocks = max(1, (w + 3) // 4) * max(1, (h + 3) // 4)
    linsize = blocks * (8 if fourcc == "DXT1" else 16)
    hdr = bytearray(128)
    hdr[0:4] = b"DDS "
    struct.pack_into("<I", hdr, 4, 124)                # dwSize
    struct.pack_into("<I", hdr, 8, DDSD)               # dwFlags
    struct.pack_into("<I", hdr, 12, h)                 # dwHeight
    struct.pack_into("<I", hdr, 16, w)                 # dwWidth
    struct.pack_into("<I", hdr, 20, linsize)           # dwPitchOrLinearSize
    struct.pack_into("<I", hdr, 28, 1)                 # dwMipMapCount
    struct.pack_into("<I", hdr, 76, 32)                # pixelformat dwSize
    struct.pack_into("<I", hdr, 80, 0x4)               # DDPF_FOURCC
    hdr[84:88] = fourcc.encode("ascii")[:4].ljust(4, b"\x00")
    struct.pack_into("<I", hdr, 108, 0x1000)           # dwCaps TEXTURE
    return bytes(hdr) + data


def _tgv_image(raw: bytes):
    """Decode a .tgv texture to a PIL RGBA image (largest mip).  Handles the ZIPO-compressed
    DXT1/DXT3/DXT5 and uncompressed A8R8G8B8 textures; returns None for formats we can't decode
    (e.g. the TGU1 codec), so the caller falls back to a hex preview."""
    if not _HAVE_PIL:
        return None
    info = _parse_tgv(raw)
    if not info:
        return None
    w, h, fmt = info["w"], info["h"], info["fmt"]
    _, px = max(info["mips"], key=lambda m: m[0])      # biggest mip = full resolution
    try:
        with pil_log.source("Raw editor"):
            if fmt in ("DXT1", "DXT3", "DXT5"):
                img = Image.open(io.BytesIO(_dds_bytes(fmt, w, h, px)))
                return img.convert("RGBA"), fmt
            if len(px) >= w * h * 4:                   # uncompressed 32-bit (A8R8G8B8 = BGRA bytes)
                rawmode = "BGRA" if ("A8R8G8B8" in fmt or "BGRA" in fmt) else "RGBA"
                return Image.frombytes("RGBA", (w, h), px[:w * h * 4], "raw", rawmode), (fmt or "RGBA8")
            if len(px) >= w * h * 3:
                return Image.frombytes("RGB", (w, h), px[:w * h * 3]).convert("RGBA"), (fmt or "RGB")
    except Exception:
        return None
    return None


def _pil_open(raw: bytes):
    if not _HAVE_PIL:
        return None
    try:
        with pil_log.source("Raw editor"):
            return Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════════
# Dat stores — the window talks to one of these instead of a ModProject directly, so the SAME
# editor can drive either the six top-level game dats (ProjectDatStore) or a .dat embedded inside
# one of them (NestedDatStore).  Each exposes the handful of methods the window calls.

class ProjectDatStore:
    """Top-level store: the six game dats of a ModProject, saved to the mod folder on disk."""
    picker_label = t("tools.project_file")
    save_button_label = t("common.save_mod_dat")
    save_title = t("common.save_mod")
    save_error_hint = t("tools.tip_set_game_root_settings")
    save_help = t(
        "tools.reads_mod_s_own_dat")

    def __init__(self, project: "mp_mod.ModProject"):
        self._p = project

    @property
    def name(self):
        return self._p.name

    def dat_choices(self):
        # The six core dats (Data/PC/<sub>/) first, then every per-terrain dat discovered under Maps/PC/.
        # All share the same dat_key machinery, so the window edits/saves them identically.
        out = list(_DAT_CHOICES)
        try:
            for key in self._p.terrain_dat_keys():
                out.append((key, t("tools.terrain_map_name", name=key.split('/', 1)[1])))
        except Exception:
            pass
        return out

    def read_source(self, dat_key):
        return self._p.read_source(dat_key)

    def is_mod_path(self, dat_key, src):
        return Path(src) == self._p.project_dat_path(dat_key)

    def source_kind(self, dat_key, src):
        return t("tools.mod_copy") if self.is_mod_path(dat_key, src) else t("tools.clean_backup_game")

    def get_raw(self, dat_key, path):
        return self._p.get_raw(dat_key, path)

    def get_ndf(self, dat_key, path):
        return self._p.get_ndf(dat_key, path)

    def set_raw(self, dat_key, path, data):
        self._p.set_raw(dat_key, path, data)

    def mark_dirty(self, dat_key, path):
        self._p.mark_dirty(dat_key, path)

    def dirty_count(self):
        return self._p.dirty_count()

    def is_dirty(self):
        return self._p.is_dirty()

    def save(self):
        written = self._p.save_all()
        return t("tools.saved_mod_changes") + "\n".join(written)

    def cleanup(self):
        pass


class NestedDatStore:
    """A dat that lives *inside* another dat — a renamed .ipk/.apk/.mpk/.ppk or any embedded edata.

    Backed by a temp working file so it reuses the on-disk edata rebuild machinery unchanged.
    "Saving" does not touch the real game files: it serializes the (edited) nested archive and
    stages it back into the PARENT store as a replace of the entry it came from.  The user then
    saves the parent window to write the parent .dat to disk.  Because it mirrors the same
    interface, a NestedDatStore can itself be a parent — nesting works to any depth.
    """
    picker_label = t("tools.nested_archive")
    save_button_label = t("tools.apply_into_parent_dat")
    save_title = t("tools.apply_parent")
    save_error_hint = t("tools.parent_archive_may_unsupported_edata")

    _KEY = "nested"

    def __init__(self, parent_store, parent_dat_key, entry_path, data):
        self._parent = parent_store
        self._parent_dat_key = parent_dat_key
        self._entry_path = entry_path
        self.name = entry_path.replace("\\", "/").split("/")[-1]
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="ruse_nested_"))
        self._tmp = self._tmp_dir / "nested.dat"
        self._tmp.write_bytes(data)
        edata_mod.open_dat(str(self._tmp))   # validate it parses (raises -> caller reports it)
        self._ndf_cache = {}
        self._raw_cache = {}
        self._raw_origpath = {}
        self._dirty = set()

    @staticmethod
    def _norm(p):
        return p.replace("\\", "/").lower()

    @property
    def save_help(self):
        return (t("tools.editing_archive_embedded") + self._entry_path + t("tools.apply_into_parent_dat_stages"))

    def dat_choices(self):
        label = self._entry_path if len(self._entry_path) < 70 else "…" + self._entry_path[-68:]
        return [(self._KEY, t("tools.nested") + label)]

    def read_source(self, dat_key):
        return self._tmp

    def is_mod_path(self, dat_key, src):
        return True

    def source_kind(self, dat_key, src):
        return t("tools.nested_archive_2")

    def get_raw(self, dat_key, path):
        key = (dat_key, self._norm(path))
        if key in self._raw_cache:
            return self._raw_cache[key]
        raw = edata_mod.open_dat(str(self._tmp)).get(path)
        if raw is None:
            raise KeyError(f"{path} not found in nested archive")
        self._raw_cache[key] = raw
        return raw

    def get_ndf(self, dat_key, path):
        key = (dat_key, self._norm(path))
        if key in self._ndf_cache:
            return self._ndf_cache[key]
        raw = edata_mod.open_dat(str(self._tmp)).get(path)
        if raw is None:
            raise KeyError(f"{path} not found in nested archive")
        ndf = ndfbin_mod.read(raw)
        self._ndf_cache[key] = ndf
        return ndf

    def set_raw(self, dat_key, path, data):
        key = (dat_key, self._norm(path))
        self._raw_cache[key] = data
        self._raw_origpath[key] = path
        self._ndf_cache.pop(key, None)
        self._dirty.add(key)

    def mark_dirty(self, dat_key, path):
        self._dirty.add((dat_key, self._norm(path)))

    def dirty_count(self):
        return len(self._dirty)

    def is_dirty(self):
        return bool(self._dirty)

    def _flush_to_tmp(self):
        """Write every staged edit into the temp .dat (one structure-preserving rebuild)."""
        if not self._dirty:
            return
        arc = edata_mod.open_dat(str(self._tmp))
        to_replace, to_add = {}, {}
        for (dk, npath) in self._dirty:
            ndf = self._ndf_cache.get((dk, npath))
            if ndf is not None:
                data = ndfbin_mod.write(ndf, compress=ndf.is_compressed)
            elif (dk, npath) in self._raw_cache:
                data = self._raw_cache[(dk, npath)]
            else:
                continue
            if arc.get(npath) is not None:
                to_replace[npath] = data
            else:
                orig = self._raw_origpath.get((dk, npath), npath)
                to_add[orig.replace("/", "\\")] = data
        if to_replace or to_add:
            try:
                arc.batch_update(to_replace, to_add)
            except NotImplementedError:                 # v2 archive: replace one at a time (no add)
                for npath, data in to_replace.items():
                    arc.replace(npath, data)
        self._dirty.clear()

    def save(self):
        self._flush_to_tmp()
        new_bytes = self._tmp.read_bytes()
        self._parent.set_raw(self._parent_dat_key, self._entry_path, new_bytes)
        self._parent.mark_dirty(self._parent_dat_key, self._entry_path)
        return (t("tools.applied_modified_archive_back_into")
                + self._entry_path + t("tools.switch_parent_window_click")
                + self._parent.save_button_label + t("tools.write_dat_disk"))

    def cleanup(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)


class ToolsEditorWindow(tk.Frame):
    """Embedded as a nested in-tab view (formerly a Toplevel); the Mod Editor hosts it + the Back bar.

    `open_nested(store, on_applied)` is the host callback used to open an embedded .dat as ANOTHER
    nested view (so Back walks back out through each nested archive); without it (standalone), a child
    Toplevel is used.  `cleanup()` releases a nested store's temp working copy when the view is popped."""
    def __init__(self, master, project: "mp_mod.ModProject" = None, on_change=None, *,
                 store=None, open_nested=None):
        super().__init__(master)
        self._store = store if store is not None else ProjectDatStore(project)
        self._on_change = on_change
        self._open_nested_cb = open_nested
        self.configure(background=_R_BG)
        self._dat_choices = self._store.dat_choices()
        self._dat_label = dict(self._dat_choices)

        self._dat_key = None
        self._arc = None                 # read-only archive for listing / preview / export
        self._sizes = {}                 # display-path -> size
        self._all_paths = []             # display paths in the current dat
        self._img_ref = None             # keep a ref so Tk doesn't GC the preview image
        self._search_meta = {}           # tree iid -> (ndf_path, inst_idx)
        self._ns_cache = {}              # ndf_path -> parsed NdfBinary (search only)

        self._build_ui()
        self._select_dat(self._dat_choices[0][0])

    # ── chrome ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        top = tk.Frame(self, background=_R_BG)
        top.pack(fill="x", padx=8, pady=(8, 2))
        tk.Label(top, text=self._store.picker_label, background=_R_BG, foreground=_R_TEXT,
                 font=_F_BOLD).pack(side="left")
        self._dat_var = tk.StringVar(value=self._dat_choices[0][1])
        cb = ttk.Combobox(top, textvariable=self._dat_var, state="readonly", width=52,
                          values=[lbl for _k, lbl in self._dat_choices])
        cb.pack(side="left", padx=8)
        ui_util.fit_combobox(cb, maximum=70)
        cb.bind("<<ComboboxSelected>>", self._on_dat_pick)
        self._src_lbl = tk.Label(top, text="", background=_R_BG, foreground=_R_TEXT_DIM, font=_F_MAIN)
        self._src_lbl.pack(side="left", padx=10)

        self._nb = nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=4)
        self._t_browse, self._t_vars, self._t_search = ttk.Frame(nb), ttk.Frame(nb), ttk.Frame(nb)
        nb.add(self._t_browse, text=t("tools.browse_files"))
        nb.add(self._t_vars,   text=t("tools.ndf_vars"))
        nb.add(self._t_search, text=t("tools.search"))
        self._build_browse(self._t_browse)
        self._build_vars(self._t_vars)
        self._build_search(self._t_search)

        bot = tk.Frame(self, background=_R_BG)
        bot.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Button(bot, text=self._store.save_button_label, command=self._save).pack(side="left")
        self._status = tk.Label(bot, text="", background=_R_BG, foreground=_R_TEXT_DIM, font=_F_MAIN)
        self._status.pack(side="right")
        tk.Label(self, text=self._store.save_help,
                 background=_R_BG, foreground=_R_TEXT_DIM, font=_F_MAIN, justify="left",
                 wraplength=1140).pack(fill="x", padx=8, pady=(0, 6))

    def _mk_listbox(self, parent, **kw):
        lb = tk.Listbox(parent, activestyle="none", exportselection=False,
                        background=_R_BG_WIDGET, foreground=_R_TEXT, selectbackground=_R_SEL_BG,
                        selectforeground=_R_SEL_FG, font=_F_MAIN, relief="flat",
                        highlightthickness=1, highlightcolor=_R_BORDER, highlightbackground=_R_BORDER,
                        **kw)
        return lb

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

    # ── dat selection / loading ─────────────────────────────────────────────────────

    def _on_dat_pick(self, _=None):
        for k, lbl in self._dat_choices:
            if lbl == self._dat_var.get():
                self._select_dat(k)
                return

    def _select_dat(self, dat_key):
        self._dat_key = dat_key
        self._ns_cache = {}
        try:
            src = self._store.read_source(dat_key)
            self._arc = edata_mod.open_dat(str(src))
        except Exception as e:
            self._arc = None
            self._all_paths = []
            self._src_lbl.configure(text=t("tools.e", e=e), foreground=_R_RED)
            self._browse_refresh()
            self._vars_reload()
            return
        is_mod = self._store.is_mod_path(dat_key, src)
        self._sizes = {e.path: e.size for e in self._arc._entries}
        self._all_paths = sorted((e.path.replace("\\", "/") for e in self._arc._entries),
                                 key=str.lower)
        where = self._store.source_kind(dat_key, src)
        self._src_lbl.configure(
            text=t("tools.name_n_entries_from_where",
                   name=Path(src).name, n=len(self._all_paths), where=where),
            foreground=(_R_GOLD if is_mod else _R_TEXT_DIM))
        self._browse_refresh()
        self._vars_reload()
        self._ns_clear()
        self._update_status()

    # ── Browse / Files tab ──────────────────────────────────────────────────────────

    _BROWSE_CAP = 6000

    def _build_browse(self, parent):
        pw = ttk.PanedWindow(parent, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=4, pady=4)

        left = ttk.Frame(pw)
        pw.add(left, weight=2)
        fr = tk.Frame(left, background=_R_BG)
        fr.pack(fill="x", pady=(2, 2))
        tk.Label(fr, text=t("tools.filter"), background=_R_BG, foreground=_R_TEXT, font=_F_MAIN).pack(side="left")
        self._bf_var = tk.StringVar()
        self._bf_var.trace_add("write", lambda *_: self._browse_refresh())
        ttk.Entry(fr, textvariable=self._bf_var).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(fr, text="✕", width=2, command=lambda: self._bf_var.set("")).pack(side="left")

        cols = ("path", "kind", "size")
        tvh = tk.Frame(left, background=_R_BG)
        tvh.pack(fill="both", expand=True)
        self._b_tv = ttk.Treeview(tvh, columns=cols, show="headings", selectmode="browse")
        for c, hd, w in [("path", t("tools.virtual_path"), 360), ("kind", t("tools.type"), 110), ("size", t("tools.size"), 90)]:
            self._b_tv.heading(c, text=hd)
            # path doesn't stretch — it's widened to the longest path on refresh so the horizontal
            # scrollbar can reveal full virtual paths (issue #5.4).
            self._b_tv.column(c, width=w, minwidth=50, stretch=(c != "path"),
                              anchor=("e" if c == "size" else "w"))
        ui_util.with_scrollbars(tvh, self._b_tv)
        # Debounced preview + instant 'Loading …' feedback (see ui_util.debounce_load): NDF parsing /
        # texture decoding is heavy, so scrolling the tree no longer previews every row passed over —
        # only the settled selection, and the preview always matches the highlighted entry.
        ui_util.debounce_load(self._b_tv, self._on_browse_select, on_peek=self._peek_browse)

        self._b_count = tk.Label(left, text="", background=_R_BG, foreground=_R_TEXT_DIM, font=_F_MAIN)
        self._b_count.pack(anchor="w", pady=(2, 0))

        right = ttk.LabelFrame(pw, text=t("common.preview"))
        pw.add(right, weight=3)
        self._pv_head = tk.Label(right, text=t("tools.select_file_preview"), anchor="w", justify="left",
                                 background=_R_BG_PANEL, foreground=_R_GOLD_BRT, font=_F_BOLD)
        self._pv_head.pack(fill="x", padx=4, pady=(4, 2))
        body = tk.Frame(right, background=_R_BG_PANEL)
        body.pack(fill="both", expand=True, padx=4, pady=2)
        self._pv_body = body
        self._pv_img = tk.Label(body, background=_R_BG_WIDGET, anchor="center")
        self._pv_txt_frame, self._pv_txt = self._mk_text(body)
        self._pv_goto = ttk.Button(right, text=t("tools.edit_these_vars_ndf_vars"),
                                   command=self._browse_goto_vars)
        # Shown only for high-def terrain (.tmst): decodes the tmst index+chunk pair to an image on a
        # background thread (heavy numpy) so selecting the entry stays instant.
        self._pv_tmst_btn = ttk.Button(right, text=t("tools.decode_terrain_image"),
                                       command=self._decode_terrain_preview)
        # Phase 3: read-only "view as raw" for structured binaries (.scenario/.kdt/.sdb).
        self._pv_struct_btn = ttk.Button(right, text=t("tools.view_full_structure"),
                                         command=self._view_structure)
        self._pv_render_btn = ttk.Button(right, text=t("tools.render_image"),
                                         command=self._render_raw_image)

        ab = tk.Frame(right, background=_R_BG_PANEL)
        ab.pack(fill="x", padx=4, pady=(2, 4))
        ttk.Button(ab, text=t("common.export_2"), command=self._export).pack(side="left", padx=2)
        self._pv_edit_btn = ttk.Button(ab, text=t("common.edit_2"), command=self._edit_entry, state="disabled")
        self._pv_edit_btn.pack(side="left", padx=2)
        ttk.Button(ab, text=t("tools.import_replace_2"), command=self._import).pack(side="left", padx=2)
        ttk.Button(ab, text=t("tools.add_file"), command=self._add_file).pack(side="left", padx=2)
        self._pv_nest_btn = ttk.Button(ab, text=t("tools.open_as_nested_dat"), command=self._open_nested,
                                       state="disabled")
        self._pv_nest_btn.pack(side="left", padx=2)
        self._pv_status = tk.Label(ab, text="", background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN)
        self._pv_status.pack(side="right")

    def _browse_refresh(self):
        for it in self._b_tv.get_children():
            self._b_tv.delete(it)
        flt = self._bf_var.get().lower().strip()
        shown = 0
        shown_paths = []
        for p in self._all_paths:
            if flt and flt not in p.lower():
                continue
            if shown >= self._BROWSE_CAP:
                break
            orig = p.replace("/", "\\") if p.replace("/", "\\") in self._sizes else p
            size = self._sizes.get(orig, self._sizes.get(p, 0))
            self._b_tv.insert("", tk.END, iid=p, values=(p, _entry_kind(p), f"{size:,}"))
            shown_paths.append(p)
            shown += 1
        ui_util.fit_tree_column(self._b_tv, "path", shown_paths, header=t("tools.virtual_path"))  # #5.4
        ui_util.stripe_treeview(self._b_tv, _R_BG_WIDGET); ui_util.retag_treeview(self._b_tv)  # #5.2
        total = sum(1 for p in self._all_paths if (not flt or flt in p.lower()))
        cap = t("tools.showing_first_n_narrow_filter", n=self._BROWSE_CAP) if total > self._BROWSE_CAP else ""
        self._b_count.configure(text=t("tools.n_match_es_cap", n=total, cap=cap))

    def _selected_browse_path(self):
        sel = self._b_tv.selection()
        return sel[0] if sel else None

    def _entry_bytes(self, path):
        """Bytes for an entry: the on-disk archive normally, falling back to a staged (not-yet-saved)
        replace/add held in the project — so a just-Added file previews/exports before Save."""
        raw = _arc_get(self._arc, path) if self._arc is not None else None
        if raw is None:
            try:
                raw = self._store.get_raw(self._dat_key, path)
            except Exception:
                raw = None
        return raw

    def _show_preview_widget(self, which):
        self._pv_img.pack_forget()
        self._pv_txt_frame.pack_forget()
        self._pv_goto.pack_forget()
        self._pv_tmst_btn.pack_forget()
        self._pv_struct_btn.pack_forget()
        self._pv_render_btn.pack_forget()
        if which == "image":
            self._pv_img.pack(fill="both", expand=True)
        elif which == "text":
            self._pv_txt_frame.pack(fill="both", expand=True)

    def _set_text(self, s):
        self._pv_txt.configure(state="normal")
        self._pv_txt.delete("1.0", tk.END)
        self._pv_txt.insert("1.0", s)
        self._pv_txt.configure(state="disabled")

    def _peek_browse(self):
        """Instant 'Loading <entry>…' feedback before the (heavy) preview of the picked entry."""
        path = self._selected_browse_path()
        if path:
            self._pv_head.configure(text=t("common.loading_name", name=path.split("/")[-1]))

    def _on_browse_select(self, _=None):
        path = self._selected_browse_path()
        self._pv_nest_btn.configure(state="disabled")
        self._pv_edit_btn.configure(state="disabled")
        if not path or self._arc is None:
            return
        raw = self._entry_bytes(path)
        if raw is None:
            self._pv_head.configure(text=t("tools.path_could_not_read_entry", path=path))
            return
        # Enable "Open as nested .dat" only for entries that really are edata containers — keyed on
        # the 'edat' magic, not the extension, so a renamed .ipk/.apk/.mpk/.ppk is detected correctly.
        if _is_edata(raw):
            self._pv_nest_btn.configure(state="normal")
        # Enable in-place "Edit…" for string-based entries we can round-trip (text / .dic / .xyz).
        if self._editable_kind(path, raw):
            self._pv_edit_btn.configure(state="normal")
        kind = _sniff_kind(path, raw)          # magic-aware: e.g. a .shc that's really EUG0 NDF
        name = path.split("/")[-1]
        self._img_ref = None

        # NDF → summary + jump button
        if kind == "NDF":
            try:
                ndf = ndfbin_mod.read(raw)
                from collections import Counter
                c = Counter(next((cl.name for cl in ndf.classes if cl.index == i.class_index), "?")
                            for i in ndf.instances)
                top = "\n".join(f"   {n:<40} {k}" for n, k in c.most_common(12))
                self._set_text(t("tools.ndf_binary_inst_instances_cls",
                                 inst=len(ndf.instances), cls=len(ndf.classes),
                                 props=len(ndf.properties), compressed=ndf.is_compressed, top=top))
            except Exception as e:
                self._set_text(t("tools.ndf_could_not_parse_summary", e=e) + _hexdump(raw))
            self._pv_head.configure(text=t("tools.name_kind_n_bytes",
                                           name=name, kind=kind, n=len(raw)))
            self._show_preview_widget("text")
            # The Vars tab lists NDF by extension, so only offer the jump for a real NDF extension
            # (a magic-detected NDF under some other extension previews here read-only).
            if path.lower().endswith(_NDF_EXTS):
                self._pv_goto.pack(fill="x", padx=4, pady=(0, 2))
            self._pv_status.configure(text="")
            return

        # structured binaries → read-only summary + "view as raw" structure/image buttons
        if kind in ("Scenario", "KDT", "SDB"):
            try:
                summary = {"Scenario": _scenario_summary, "KDT": _kdt_summary,
                           "SDB": _sdb_summary}[kind](raw)
            except Exception as e:
                summary = t("tools.kind_could_not_parse_e", kind=kind, e=e) + _hexdump(raw)
            self._set_text(summary)
            self._pv_head.configure(text=t("tools.name_kind_n_bytes",
                                           name=name, kind=kind, n=len(raw)))
            self._show_preview_widget("text")
            self._pv_struct = (kind, raw, name)                 # for the "view structure/render" buttons
            self._pv_struct_btn.pack(fill="x", padx=4, pady=(0, 2))
            if kind in ("SDB", "KDT") and _HAVE_PIL:
                self._pv_render_btn.pack(fill="x", padx=4, pady=(0, 2))
            self._pv_status.configure(text="")
            return

        # high-def terrain (.tmst index/chunk pair) → dims summary + on-demand image decode
        if kind == "Terrain (TMST)":
            self._preview_tmst(path, raw, name)
            return

        # images (.tgv textures and ordinary image files)
        img, info = None, ""
        if kind == "Texture (TGV)":
            res = _tgv_image(raw)
            if res:
                img, fmt = res
                info = t("tools.w_h_fmt", w=img.width, h=img.height, fmt=fmt)
        elif kind == "Image":
            img = _pil_open(raw)
            if img:
                info = t("tools.w_h", w=img.width, h=img.height)
        if img is not None:
            disp = img.copy()
            disp.thumbnail((720, 600))
            self._img_ref = ImageTk.PhotoImage(disp)
            self._pv_img.configure(image=self._img_ref, text="")
            self._pv_head.configure(text=t("tools.name_kind_info_n_bytes",
                                           name=name, kind=kind, info=info, n=len(raw)))
            self._show_preview_widget("image")
            self._pv_status.configure(text="")
            return

        # text-ish
        text = None
        if kind == "Localization":
            try:
                text = _dic_text(raw)
            except Exception as e:
                text = t("tools.dic_parse_failed_e", e=e) + _hexdump(raw)
        elif kind == "AI Script":
            text = _xyz_inflate(raw)
            if text is None:
                text = t("tools.compiled_xyz_could_not_inflate") + _hexdump(raw)
        elif kind == "Text" or _looks_text(raw):
            text = _decode_text(raw)

        if text is not None:
            self._set_text(text)
            self._show_preview_widget("text")
        else:
            extra = "" if _HAVE_PIL or kind not in ("Texture (TGV)", "Image") else t("tools.pillow_not_available")
            self._set_text(t("tools.no_inline_preview_type_extra", extra=extra) + _hexdump(raw))
            self._show_preview_widget("text")
        self._pv_head.configure(text=t("tools.name_kind_n_bytes",
                                       name=name, kind=kind, n=len(raw)))
        self._pv_status.configure(text="")

    def _preview_tmst(self, path, raw, name):
        """Instant read-only summary for a high-def terrain entry (.tmst_pc/.tmst_chunk_pc). The heavy
        image decode is deferred to the "Decode terrain image" button so selection stays snappy."""
        idx_path, chunk_path = _tmst_pair_paths(path)
        idx_raw = self._entry_bytes(idx_path) if idx_path else None
        lines = []
        if terrain_mod is None:
            lines.append(t("tools.terrain_decode_needs_numpy_pillow"))
        try:
            gw, gh, _recs = terrain_mod.parse_tile_index(idx_raw if idx_raw else raw)
            lines.append(t("tools.high_def_terrain_tile_grid", gw=gw, gh=gh, tiles=gw * gh))
        except Exception:
            lines.append(t("tools.high_def_terrain_chunk"))
        if idx_raw is None and path.lower().endswith(".tmst_chunk_pc"):
            lines.append(t("tools.index_p_not_found_alongside", p=idx_path.split("/")[-1] if idx_path else "?"))
        lines.append("")
        lines.append(t("tools.read_only_use_decode_terrain"))
        self._set_text("\n".join(lines))
        self._pv_head.configure(text=t("tools.name_kind_n_bytes",
                                       name=name, kind="Terrain (TMST)", n=len(raw)))
        self._show_preview_widget("text")
        # offer the decode only when we have both halves of the pair and the codec is available
        chunk_raw = self._entry_bytes(chunk_path) if chunk_path else None
        if terrain_mod is not None and _HAVE_PIL and idx_raw and chunk_raw:
            self._tmst_pair = (idx_raw, chunk_raw)
            self._pv_tmst_btn.pack(fill="x", padx=4, pady=(0, 2))
        else:
            self._tmst_pair = None
        self._pv_status.configure(text="")

    def _decode_terrain_preview(self):
        """Decode the current tmst index+chunk pair to an image on a background thread (heavy numpy),
        at a low LOD budget for a quick preview, then show it on the Tk thread."""
        pair = getattr(self, "_tmst_pair", None)
        if not pair or terrain_mod is None:
            return
        idx_raw, chunk_raw = pair
        self._pv_status.configure(text=t("tools.decoding_terrain"))
        self._pv_tmst_btn.configure(state="disabled")

        def work():
            try:
                gw, gh, _ = terrain_mod.parse_tile_index(idx_raw)
                lod = terrain_mod.best_lod(gw, gh, budget=48)      # small budget → fast coarse preview
                img = terrain_mod.decode_terrain(idx_raw, chunk_raw, lod=lod, use_index=True)
                self.after(0, lambda: self._show_terrain_image(img))
            except Exception as e:
                msg = t("tools.terrain_decode_failed_e", e=e)
                def _fail(msg=msg):                    # guard: the editor frame may be gone by now
                    if self.winfo_exists():
                        self._pv_status.configure(text=msg)
                        self._pv_tmst_btn.configure(state="normal")
                self.after(0, _fail)

        threading.Thread(target=work, daemon=True).start()

    def _show_image(self, img, status=""):
        """Display a PIL image in the preview pane (shared by texture/terrain/raw-render paths)."""
        disp = img.copy()
        disp.thumbnail((720, 600))
        self._img_ref = ImageTk.PhotoImage(disp)
        self._pv_img.configure(image=self._img_ref, text="")
        self._show_preview_widget("image")
        if status:
            self._pv_status.configure(text=status)

    def _show_terrain_image(self, img):
        if not self.winfo_exists():                    # background decode finished after the frame closed
            return
        try:
            self._show_image(img, t("tools.terrain_preview_w_h", w=img.width, h=img.height))
        except Exception as e:
            self._pv_status.configure(text=t("tools.terrain_decode_failed_e", e=e))
        finally:
            self._pv_tmst_btn.configure(state="normal")

    def _render_raw_image(self):
        """Render the current .sdb/.kdt entry to a visual (grid map / vertex scatter) in the preview."""
        st = getattr(self, "_pv_struct", None)
        if not st:
            return
        kind, raw, name = st
        try:
            img = _sdb_grid_image(raw) if kind == "SDB" else _kdt_vertex_image(raw) if kind == "KDT" else None
        except Exception as e:
            self._pv_status.configure(text=t("tools.render_failed_e", e=e))
            return
        if img is None:
            self._pv_status.configure(text=t("tools.nothing_render"))
            return
        self._show_image(img, t("tools.kind_render_w_h", kind=kind, w=img.width, h=img.height))

    def _view_structure(self):
        """Open a read-only tree of the current structured binary's full parsed content."""
        st = getattr(self, "_pv_struct", None)
        if not st:
            return
        kind, raw, name = st
        try:
            data = {"Scenario": _struct_scenario, "KDT": _struct_kdt, "SDB": _struct_sdb}[kind](raw)
        except Exception as e:
            ui_util.error(self, t("tools.view_structure"), t("tools.could_not_parse_kind_e", kind=kind, e=e))
            return
        self._view_structure_dialog(t("tools.kind_structure_name", kind=kind, name=name), data)

    def _view_structure_dialog(self, title, data):
        """Render nested dict/list/scalar `data` in a read-only, expandable two-column tree."""
        dlg = ui_util.themed_toplevel(self, title, size=(880, 640), resizable=True, modal=False)
        holder = tk.Frame(dlg, background=_R_BG_PANEL)
        holder.pack(fill="both", expand=True, padx=8, pady=8)
        tree = ttk.Treeview(holder, columns=("val",), show="tree headings")
        tree.heading("#0", text=t("common.field"))
        tree.heading("val", text=t("common.value"))
        tree.column("#0", width=360, stretch=False)
        tree.column("val", width=460, stretch=True)
        ui_util.with_scrollbars(holder, tree)

        def add(parent, key, val):
            if isinstance(val, dict):
                nid = tree.insert(parent, "end", text=key, values=("{…}",), open=(parent == ""))
                for k, v in val.items():
                    add(nid, str(k), v)
            elif isinstance(val, (list, tuple)):
                nid = tree.insert(parent, "end", text="%s  [%d]" % (key, len(val)), values=("",))
                for i, v in enumerate(val[:_STRUCT_LIST_CAP]):
                    add(nid, str(i), v)
                if len(val) > _STRUCT_LIST_CAP:
                    tree.insert(nid, "end", text=t("tools.n_more", n=len(val) - _STRUCT_LIST_CAP), values=("",))
            else:
                s = val if isinstance(val, str) else repr(val)
                tree.insert(parent, "end", text=key, values=(s[:400],))

        for k, v in data.items():
            add("", str(k), v)
        ttk.Button(dlg, text=t("common.close"), command=dlg.destroy).pack(pady=(0, 8))
        dlg.bind("<Escape>", lambda *_: dlg.destroy())

    def _browse_goto_vars(self):
        path = self._selected_browse_path()
        if path:
            self._goto_vars(path)

    def _export(self):
        path = self._selected_browse_path()
        if not path or self._arc is None:
            ui_util.info(self, t("common.export"), t("tools.select_file_list_first"))
            return
        raw = self._entry_bytes(path)
        if raw is None:
            ui_util.error(self, t("common.export"), t("tools.could_not_read_entry"))
            return
        dest = filedialog.asksaveasfilename(title=t("tools.export_file_as"), initialfile=path.split("/")[-1],
                                            parent=self, filetypes=[(t("common.all_files"), "*.*")])
        if not dest:
            return
        try:
            Path(dest).write_bytes(raw)
            self._pv_status.configure(text=t("tools.exported_name", name=Path(dest).name))
        except Exception as e:
            ui_util.error(self, t("tools.export_failed"), str(e))

    def _import(self):
        path = self._selected_browse_path()
        if not path:
            ui_util.info(self, t("tools.import_replace"), t("tools.select_entry_want_replace_first"))
            return
        src = filedialog.askopenfilename(title=t("tools.replacement_name", name=path.split('/')[-1]),
                                         parent=self, filetypes=[(t("common.all_files"), "*.*")])
        if not src:
            return
        if not ui_util.confirm(self, t("tools.replace_entry"),
                                   t("tools.replace_inside_mod_path_src", path=path, src=src)):
            return
        try:
            data = Path(src).read_bytes()
        except Exception as e:
            ui_util.error(self, t("tools.import_failed"), str(e))
            return
        self._stage_replace(path, data)

    def _stage_replace(self, path: str, data: bytes, status: str = None):
        """Stage replacement bytes for an existing entry (shared by Import/Replace and the in-place
        editors): set_raw + size bookkeeping + status + dirty notification. Written on Save."""
        self._store.set_raw(self._dat_key, path, data)
        self._sizes[path] = len(data)
        self._sizes[path.replace("/", "\\")] = len(data)
        if self._b_tv.exists(path):
            self._b_tv.set(path, "size", f"{len(data):,}")
        self._pv_status.configure(
            text=status or t("tools.edited_staged_n_bytes_save", n=len(data)))
        self._notify()
        self._update_status()

    # ── in-place editing of string-based entries (text / .dic / .xyz) ─────────────────

    def _editable_kind(self, path, raw):
        """Which in-place editor applies: 'dic' | 'xyz' | 'text' | None. Only string-based entries we
        can round-trip. Text is gated on the bytes actually LOOKING like text so a binary type that
        merely has a text-ish extension (e.g. .scenario) is not offered a corrupting text edit."""
        if not raw:
            return None
        kind = _entry_kind(path)
        if kind == "Localization":
            try:
                dic_mod.read(raw)          # TRA only; DICS/other variants raise → not editable here
                return "dic"
            except Exception:
                return None
        if kind == "AI Script":
            return "xyz"
        if _looks_text(raw):
            return "text"
        return None

    def _edit_entry(self):
        path = self._selected_browse_path()
        if not path:
            return
        raw = self._entry_bytes(path)
        kind = self._editable_kind(path, raw)
        if kind == "dic":
            self._edit_dic_dialog(path, raw)
        elif kind == "xyz":
            self._edit_xyz_dialog(path, raw)
        elif kind == "text":
            self._edit_text_dialog(path, raw)
        else:
            ui_util.info(self, t("common.edit"),
                                t("tools.no_place_editor_type_yet"))

    def _big_text_edit(self, title, initial, save_cb=None, note="", readonly=False):
        """Editable multiline-text dialog. When editable, `save_cb(text)` returns (ok, err) and the
        dialog closes on ok. When readonly (or no save_cb) it's a scrollable viewer with a Close button."""
        ro = readonly or save_cb is None
        dlg = ui_util.themed_toplevel(self, title, size=(860, 620), resizable=True)
        if note:
            tk.Label(dlg, text=note, background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN,
                     anchor="w", justify="left", wraplength=820).pack(fill="x", padx=8, pady=(8, 2))
        frame, txt = self._mk_text(dlg)
        txt.configure(state="normal")
        txt.delete("1.0", tk.END)
        txt.insert("1.0", initial)
        if ro:
            txt.configure(state="disabled")
        frame.pack(fill="both", expand=True, padx=8, pady=6)
        bf = tk.Frame(dlg, background=_R_BG_PANEL)
        bf.pack(fill="x", padx=8, pady=(0, 8))

        def do_save():
            ok, msg = save_cb(txt.get("1.0", "end-1c"))
            if not ok:
                ui_util.error(dlg, title, msg or t("tools.could_not_save"))
                return
            dlg.destroy()

        if ro:
            ttk.Button(bf, text=t("common.close"), command=dlg.destroy).pack(side="left", padx=4)
        else:
            ttk.Button(bf, text=t("tools.save_stage"), command=do_save).pack(side="left", padx=4)
            ttk.Button(bf, text=t("common.cancel"), command=dlg.destroy).pack(side="left", padx=4)
        txt.focus_set()
        dlg.bind("<Escape>", lambda *_: dlg.destroy())
        dlg.wait_window()

    def _edit_text_dialog(self, path, raw):
        text, encode = _text_codec(raw)

        def save(s):
            try:
                data = encode(s)
            except Exception as e:
                return False, str(e)
            self._stage_replace(path, data)
            return True, None

        self._big_text_edit(t("tools.edit_text_name", name=path.split("/")[-1]), text, save,
                            note=t("tools.plain_text_entry_saved_back"))

    def _edit_xyz_dialog(self, path, raw):
        try:
            source = xyz_mod.decompile_xyz(raw)
        except Exception as e:
            ui_util.error(self, t("tools.edit_script"),
                                 t("tools.could_not_decompile_xyz_e", e=e))
            return
        name = path.split("/")[-1]
        if not xyz_mod.have_native_compiler():
            self._big_text_edit(t("tools.view_script_name", name=name), source, readonly=True,
                                note=t("tools.decompiled_python_2_source_bundled"))
            return

        def save(s):
            try:
                data = xyz_mod.compile_to_xyz(s)
            except Exception as e:
                return False, t("tools.recompile_failed_e", e=e)
            self._stage_replace(path, data)
            return True, None

        self._big_text_edit(t("tools.edit_script_name", name=name), source, save,
                            note=t("tools.decompiled_python_2_source_saving"))

    def _edit_dic_dialog(self, path, raw):
        try:
            entries = dic_mod.read(raw)                 # [(key_bytes, string)] in file order
        except Exception as e:
            ui_util.error(self, t("tools.edit_localization"),
                                 t("tools.not_editable_tra_dic_e", e=e))
            return
        name = path.split("/")[-1]
        edits = {}                                       # key_bytes -> new string (only changed ones)
        by_key = {k: v for k, v in entries}

        dlg = ui_util.themed_toplevel(self, t("tools.edit_localization_name", name=name),
                                      size=(900, 600), resizable=True)
        tk.Label(dlg, text=t("tools.n_entries_pick_one_edit", n=len(entries)),
                 background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN).pack(fill="x", padx=8, pady=(8, 2))

        pw = ttk.PanedWindow(dlg, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=8, pady=4)
        lh = tk.Frame(pw, background=_R_BG); pw.add(lh, weight=3)
        frow = tk.Frame(lh, background=_R_BG); frow.pack(fill="x")
        tk.Label(frow, text=t("tools.filter"), background=_R_BG, foreground=_R_TEXT, font=_F_MAIN).pack(side="left")
        filt = tk.StringVar()
        ttk.Entry(frow, textvariable=filt).pack(side="left", fill="x", expand=True, padx=4)
        tvh = tk.Frame(lh, background=_R_BG); tvh.pack(fill="both", expand=True)
        tree = ttk.Treeview(tvh, columns=("key", "val"), show="headings", selectmode="browse")
        tree.heading("key", text=t("tools.key")); tree.heading("val", text=t("tools.string"))
        tree.column("key", width=150, stretch=False); tree.column("val", width=420, stretch=True)
        ui_util.with_scrollbars(tvh, tree)

        rh = tk.Frame(pw, background=_R_BG_PANEL); pw.add(rh, weight=2)
        tk.Label(rh, text=t("tools.selected_string_utf_16"), background=_R_BG_PANEL, foreground=_R_TEXT,
                 font=_F_MAIN).pack(anchor="w", padx=4, pady=(2, 0))
        eframe, etxt = self._mk_text(rh)
        etxt.configure(state="normal")
        eframe.pack(fill="both", expand=True, padx=4, pady=4)
        cur = {"key": None}

        def cur_val(k):
            return edits.get(k, by_key.get(k, ""))

        def repaint():
            f = filt.get().lower().strip()
            for it in tree.get_children():
                tree.delete(it)
            for i, (k, _v) in enumerate(entries):
                kh = k.hex()
                val = cur_val(k)
                if f and f not in kh and f not in val.lower():
                    continue
                mark = "✎ " if k in edits else ""
                tree.insert("", tk.END, iid=str(i), values=(kh, mark + val.replace("\n", " ")[:120]))
            ui_util.stripe_treeview(tree, _R_BG_WIDGET); ui_util.retag_treeview(tree)

        def on_sel(_=None):
            sel = tree.selection()
            if not sel:
                return
            k = entries[int(sel[0])][0]
            cur["key"] = k
            etxt.configure(state="normal")
            etxt.delete("1.0", tk.END)
            etxt.insert("1.0", cur_val(k))

        def update_entry():
            if cur["key"] is None:
                return
            k = cur["key"]
            new = etxt.get("1.0", "end-1c")
            if new == by_key.get(k, ""):
                edits.pop(k, None)                       # reverted to original
            else:
                edits[k] = new
            repaint()

        def save_all():
            if not edits:
                dlg.destroy(); return
            try:
                b = raw
                for k, s in edits.items():
                    b = dic_mod.set_entry(b, k, s)
            except Exception as e:
                ui_util.error(dlg, t("tools.edit_localization"), str(e))
                return
            self._stage_replace(path, b, status=t("tools.n_string_s_edited_staged", n=len(edits)))
            dlg.destroy()

        tree.bind("<<TreeviewSelect>>", on_sel)
        filt.trace_add("write", lambda *_: repaint())
        bf = tk.Frame(dlg, background=_R_BG_PANEL); bf.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(bf, text=t("tools.update_entry"), command=update_entry).pack(side="left", padx=4)
        ttk.Button(bf, text=t("tools.save_stage"), command=save_all).pack(side="left", padx=4)
        ttk.Button(bf, text=t("common.cancel"), command=dlg.destroy).pack(side="left", padx=4)
        repaint()
        dlg.bind("<Escape>", lambda *_: dlg.destroy())
        dlg.wait_window()

    def _do_add_file(self, vpath: str, data: bytes):
        """Stage a NEW entry at vpath (testable; no dialogs).  Returns (ok, error).  Refuses an empty
        path or one that already exists (that's a replace — use Import instead)."""
        vpath = (vpath or "").strip().strip("/\\")
        if not vpath:
            return False, t("tools.enter_virtual_path_new_file")
        disp = vpath.replace("\\", "/")
        if disp in self._all_paths or disp.replace("/", "\\") in self._sizes or disp in self._sizes:
            return False, t("tools.entry_already_exists_disp_use", disp=disp)
        self._store.set_raw(self._dat_key, vpath, data)
        self._all_paths.append(disp)
        self._all_paths.sort(key=str.lower)
        self._sizes[disp] = len(data)
        self._sizes[disp.replace("/", "\\")] = len(data)
        return True, None

    def _ask_vpath(self, default: str, title_name: str):
        """Modal prompt for the new entry's virtual path inside the dat.  Returns the path or None."""
        dlg = ui_util.themed_toplevel(self, t("tools.add_name", name=title_name), resizable=True)
        pad = {"padx": 8, "pady": 4}
        tk.Label(dlg, text=t("tools.virtual_path_inside_dat"), background=_R_BG_PANEL,
                 foreground=_R_GOLD_BRT, font=_F_HEAD).grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        tk.Label(dlg, text=t("tools.e_g_pc_ndf_patchable"),
                 background=_R_BG_PANEL, foreground=_R_TEXT_DIM, font=_F_MAIN).grid(
            row=1, column=0, columnspan=2, sticky="w", padx=8)
        var = tk.StringVar(value=default)
        ent = ttk.Entry(dlg, textvariable=var, width=60)
        ent.grid(row=2, column=0, columnspan=2, sticky="ew", **pad)
        result = {"path": None}

        def ok():
            result["path"] = var.get()
            dlg.destroy()

        bf = tk.Frame(dlg, background=_R_BG_PANEL)
        bf.grid(row=3, column=0, columnspan=2, pady=8)
        ttk.Button(bf, text=t("tools.add"), command=ok).pack(side="left", padx=8)
        ttk.Button(bf, text=t("common.cancel"), command=dlg.destroy).pack(side="left", padx=8)
        ent.focus_set()
        ent.icursor("end")
        dlg.bind("<Return>", lambda *_: ok())
        dlg.bind("<Escape>", lambda *_: dlg.destroy())
        dlg.wait_window()
        return result["path"]

    def _add_file(self):
        if self._arc is None:
            return
        src = filedialog.askopenfilename(title=t("tools.add_file_dat"),
                                         parent=self, filetypes=[(t("common.all_files"), "*.*")])
        if not src:
            return
        # default the virtual path to the selected entry's folder + the source filename
        sel = self._selected_browse_path()
        folder = sel.rsplit("/", 1)[0] if (sel and "/" in sel) else ""
        default_vp = ((folder + "/") if folder else "") + os.path.basename(src)
        vpath = self._ask_vpath(default_vp.replace("/", "\\"), os.path.basename(src))
        if not vpath:
            return
        try:
            data = Path(src).read_bytes()
        except Exception as e:
            ui_util.error(self, t("tools.add_file_2"), str(e))
            return
        ok, err = self._do_add_file(vpath, data)
        if not ok:
            ui_util.error(self, t("tools.add_file_2"), err)
            return
        disp = vpath.strip().strip("/\\").replace("\\", "/")
        self._browse_refresh()
        if self._b_tv.exists(disp):
            self._b_tv.selection_set(disp)
            self._b_tv.see(disp)
            self._on_browse_select()
        self._pv_status.configure(text=t("tools.added_staged_n_bytes_save", n=len(data)))
        self._notify()
        self._update_status()

    # ── nested .dat (open an embedded archive in its own editor) ──────────────────────

    def _open_nested(self):
        """Open the selected entry — a renamed .ipk/.apk/.mpk/.ppk or any embedded .dat — as ANOTHER
        nested Raw / Asset view (pushed onto the Mod Editor's view stack; Back steps back out).  Editing
        it and clicking “Apply into parent .dat” stages the modified archive back into THIS view; saving
        here then writes it into the .dat on disk."""
        path = self._selected_browse_path()
        if not path or self._arc is None:
            return
        raw = self._entry_bytes(path)
        if not _is_edata(raw):
            ui_util.info(self, t("tools.open_nested_dat"),
                                t("tools.entry_isn_t_embedded_dat"))
            return
        try:
            store = NestedDatStore(self._store, self._dat_key, path, raw)
        except Exception as e:
            ui_util.error(self, t("tools.open_nested_dat"),
                                 t("tools.could_not_open_embedded_archive", e=e))
            return
        on_applied = lambda p=path: self._on_nested_applied(p)
        if self._open_nested_cb is not None:
            self._open_nested_cb(store, on_applied)          # host pushes it as a nested in-tab view
        else:
            ToolsEditorWindow(self, store=store, on_change=on_applied)   # standalone fallback

    def _on_nested_applied(self, path):
        """A child (nested) editor staged its archive back into this window — refresh the entry's
        shown size from the now-staged bytes and update the unsaved-changes indicator."""
        try:
            raw = self._store.get_raw(self._dat_key, path)
        except Exception:
            raw = None
        if raw is not None:
            self._sizes[path] = len(raw)
            self._sizes[path.replace("/", "\\")] = len(raw)
            if self._b_tv.exists(path):
                self._b_tv.set(path, "size", f"{len(raw):,}")
        self._update_status()
        self._notify()

    # ── NDF Vars tab ────────────────────────────────────────────────────────────────

    def _build_vars(self, parent):
        pw = ttk.PanedWindow(parent, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=4, pady=4)

        lf = ttk.LabelFrame(pw, text=t("tools.ndf_files"))
        pw.add(lf, weight=1)   # equal weights → the three lists default to 1/3 each (still draggable)
        self._v_files = self._mk_listbox(lf, selectmode="browse")
        ui_util.with_scrollbars(lf, self._v_files)   # horizontal scroll for long NDF paths (issue #5.4)
        # Debounced: parsing an NDF file is heavy — scrolling the file list loads only the settled
        # selection, not every file passed over (see ui_util.debounce_load).
        ui_util.debounce_load(self._v_files, self._vars_on_file)

        cf = ttk.LabelFrame(pw, text=t("tools.instances"))
        pw.add(cf, weight=1)
        row = tk.Frame(cf, background=_R_BG)
        row.pack(fill="x", padx=2, pady=(2, 0))
        tk.Label(row, text=t("tools.filter"), background=_R_BG, foreground=_R_TEXT, font=_F_MAIN).pack(side="left")
        self._v_filter = tk.StringVar()
        self._v_filter.trace_add("write", lambda *_: self._vars_apply_filter())
        ttk.Entry(row, textvariable=self._v_filter).pack(side="left", fill="x", expand=True, padx=4)
        ih = tk.Frame(cf, background=_R_BG)
        ih.pack(fill="both", expand=True, padx=2, pady=2)
        self._v_inst = self._mk_listbox(ih, selectmode="browse")
        ui_util.with_scrollbars(ih, self._v_inst)   # horizontal scroll for long instance names (#5.4)
        ui_util.debounce_load(self._v_inst, self._vars_on_inst)

        rf = ttk.LabelFrame(pw, text=t("tools.properties"))
        pw.add(rf, weight=1)
        ui_util.equalize_panes(pw)   # start the three lists at equal (1/3) widths (issue #5.4)
        eb = tk.Frame(rf, background=_R_BG)
        eb.pack(fill="x", padx=2, pady=(2, 0))
        ttk.Button(eb, text=t("tools.edit_value"), command=self._vars_edit).pack(side="left", padx=2)
        tk.Label(eb, text=t("tools.double_click_row"), background=_R_BG, foreground=_R_TEXT_DIM,
                 font=_F_MAIN).pack(side="left", padx=6)
        cols = ("property", "type", "value", "edited")
        ph = tk.Frame(rf, background=_R_BG)
        ph.pack(fill="both", expand=True, padx=2, pady=2)
        self._v_props = ttk.Treeview(ph, columns=cols, show="headings", selectmode="browse")
        for c, hd, w in [("property", t("tools.property"), 160), ("type", t("tools.type"), 80),
                         ("value", t("common.value"), 240), ("edited", "", 40)]:
            self._v_props.heading(c, text=hd)
            # property/value don't stretch — widened to their content on render so the horizontal
            # scrollbar can reveal long property names and value strings (issue #5.4).
            self._v_props.column(c, width=w, minwidth=40, stretch=(c in ("type", "edited")))
        self._v_props.tag_configure("mod", foreground=_R_GOLD)
        ui_util.with_scrollbars(ph, self._v_props)
        self._v_props.bind("<Double-Button-1>", self._vars_edit)

        self._v_ndf = None
        self._v_ndf_path = ""
        self._v_all = []         # [(inst_idx, cls, dbg, inst, label)]
        self._v_shown = []
        self._v_modified = set()  # (ndf_path, inst_idx, prop_idx)

    def _vars_reload(self):
        self._v_files.delete(0, tk.END)
        self._v_ndf = None
        self._v_ndf_path = ""
        self._v_all = []
        self._v_shown = []
        for it in self._v_props.get_children():
            self._v_props.delete(it)
        self._v_inst.delete(0, tk.END)
        ndf_paths = sorted((p for p in self._all_paths if p.lower().endswith(_NDF_EXTS)), key=str.lower)
        self._v_ndf_files = ndf_paths
        for p in ndf_paths:
            self._v_files.insert(tk.END, p)
        if not ndf_paths:
            self._v_files.insert(tk.END, t("tools.no_ndf_files_dat"))

    def _vars_on_file(self, _=None):
        sel = self._v_files.curselection()
        if not sel or not getattr(self, "_v_ndf_files", None):
            return
        if sel[0] >= len(self._v_ndf_files):
            return
        path = self._v_ndf_files[sel[0]]
        try:
            ndf = self._store.get_ndf(self._dat_key, path)
        except Exception as e:
            ui_util.error(self, t("tools.ndf"), t("tools.could_not_load_path_e", path=path, e=e))
            return
        self._v_ndf = ndf
        self._v_ndf_path = path
        cls_by_idx = {c.index: c.name for c in ndf.classes}
        prop_by_idx = {pr.index: pr for pr in ndf.properties}
        self._v_all = []
        for i, inst in enumerate(ndf.instances):
            cls = cls_by_idx.get(inst.class_index, "?")
            dbg = ""
            for pv in inst.props:
                pr = prop_by_idx.get(pv.prop_index)
                if pr and pr.name == "ClassNameForDebug":
                    dbg = str(ndf.resolve_value(pv.value))
                    break
            label = f"[{i}] {cls}" + (f"  ({dbg})" if dbg else "")
            self._v_all.append((i, cls, dbg, inst, label))
        self._v_filter.set("")
        self._vars_apply_filter()
        for it in self._v_props.get_children():
            self._v_props.delete(it)

    def _vars_apply_filter(self, *_):
        flt = self._v_filter.get().lower().strip()
        self._v_inst.delete(0, tk.END)
        self._v_shown = []
        for e in self._v_all:
            if not flt or flt in e[4].lower():
                self._v_shown.append(e)
                self._v_inst.insert(tk.END, e[4])

    def _vars_on_inst(self, _=None):
        sel = self._v_inst.curselection()
        if not sel:
            return
        self._vars_refresh_props(sel[0])

    def _vars_refresh_props(self, shown_idx):
        if shown_idx >= len(self._v_shown):
            return
        ndf = self._v_ndf
        inst_idx, _c, _d, inst, _l = self._v_shown[shown_idx]
        prop_by_idx = {p.index: p for p in ndf.properties}
        for it in self._v_props.get_children():
            self._v_props.delete(it)
        names, vals = [], []
        for pv in inst.props:
            pr = prop_by_idx.get(pv.prop_index)
            if pr is None:
                continue
            tname = _type_label(pv.value.type_id)
            key = (self._v_ndf_path, inst_idx, pv.prop_index)
            tag = ("mod",) if key in self._v_modified else ()
            mark = "✎" if key in self._v_modified else ""
            vstr = _fmt_val(pv.value, ndf)
            names.append(pr.name); vals.append(vstr)
            self._v_props.insert("", tk.END, iid=f"{inst_idx}:{pv.prop_index}",
                                 values=(pr.name, tname, vstr, mark), tags=tag)
        ui_util.fit_tree_column(self._v_props, "property", names, header=t("tools.property"))   # #5.4
        ui_util.fit_tree_column(self._v_props, "value", vals, header=t("common.value"))          # #5.4
        ui_util.stripe_treeview(self._v_props, _R_BG_WIDGET); ui_util.retag_treeview(self._v_props)  # #5.2

    def _commit_var(self, inst_idx, inst, prop_idx, type_name, raw_in):
        """Parse raw_in as type_name and write it onto the instance's property, staging the edit.
        Returns (ok, error). Separated from the dialog so it's unit-testable."""
        raw_in = (raw_in or "").strip()
        if not raw_in:
            return False, t("tools.enter_value")
        parsed = _parse_val(raw_in, type_name)
        if parsed is None:
            return False, t("tools.could_not_parse_raw_as", raw=raw_in, type_name=type_name)
        pv = next((pv for pv in inst.props if pv.prop_index == prop_idx), None)
        if pv is None:
            return False, t("tools.property_not_present_instance")
        try:
            nv = ndfbin_mod.make_value(type_name, parsed)
            if nv.type_id in (_T.StringRef, _T.PathRef):
                nv.raw = self._v_ndf.ensure_string(parsed)
        except Exception as e:
            return False, str(e)
        pv.value = nv
        self._v_modified.add((self._v_ndf_path, inst_idx, prop_idx))
        self._store.mark_dirty(self._dat_key, self._v_ndf_path)
        self._notify()
        self._update_status()
        return True, None

    def _commit_list_var(self, inst_idx, inst, prop_idx, elem_type_name, elements):
        """Build a List<elem_type_name> from the already-parsed Python `elements` and stage
        it onto the instance's property.  Returns (ok, error).  UI-free so it's unit-testable;
        per-element parsing happens in the collection-editor dialog, not here."""
        pv = next((pv for pv in inst.props if pv.prop_index == prop_idx), None)
        if pv is None:
            return False, t("tools.property_not_present_instance")
        if not elem_type_name:
            return False, t("tools.choose_element_type_first")
        try:
            nv = ndfbin_mod.make_value(f"List<{elem_type_name}>", list(elements))
            # StringRef/PathRef elements come back holding the raw string; resolve each to a
            # STRG index the same way the scalar path does (make_value can't reach the ndf).  Guard
            # on str: an element already given as an int index is left as-is (never re-index it).
            for elem in nv.raw:
                if elem.type_id in (_T.StringRef, _T.PathRef) and isinstance(elem.raw, str):
                    elem.raw = self._v_ndf.ensure_string(elem.raw)
        except Exception as e:
            return False, str(e)
        pv.value = nv
        self._v_modified.add((self._v_ndf_path, inst_idx, prop_idx))
        self._store.mark_dirty(self._dat_key, self._v_ndf_path)
        self._notify()
        self._update_status()
        return True, None

    def _vars_edit(self, _=None):
        psel = self._v_props.selection()
        isel = self._v_inst.curselection()
        if not psel or not isel or self._v_ndf is None:
            ui_util.info(self, t("common.edit"), t("tools.select_instance_property_first"))
            return
        inst_idx, _c, _d, inst, _l = self._v_shown[isel[0]]
        prop_idx = int(psel[0].split(":")[1])
        ndf = self._v_ndf
        prop_obj = next((p for p in ndf.properties if p.index == prop_idx), None)
        pv = next((pv for pv in inst.props if pv.prop_index == prop_idx), None)
        if prop_obj is None or pv is None:
            return
        tname = _type_label(pv.value.type_id)

        # List / collection properties get the dedicated grid editor.
        if pv.value.type_id == _T.List:
            self._vars_edit_list(isel[0], inst_idx, inst, prop_idx, prop_obj, pv)
            return
        # Non-scalar types we can't author (Map/ObjRef/LocHash/Blob/…) open read-only
        # rather than silently pretending to be an Int32 scalar (would corrupt on save).
        if tname not in _EDIT_TYPES:
            self._vars_show_readonly(prop_obj, pv, tname)
            return

        dlg = ui_util.themed_toplevel(self, t("tools.edit_name", name=prop_obj.name), resizable=True)
        pad = {"padx": 8, "pady": 4}
        tk.Label(dlg, text=prop_obj.name, background=_R_BG_PANEL, foreground=_R_GOLD_BRT,
                 font=_F_HEAD).grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        tk.Label(dlg, text=t("tools.type_2"), background=_R_BG_PANEL, foreground=_R_TEXT,
                 font=_F_MAIN).grid(row=1, column=0, sticky="e", **pad)
        type_var = tk.StringVar(value=tname if tname in _EDIT_TYPES else "Int32")
        type_cb = ttk.Combobox(dlg, textvariable=type_var, values=_EDIT_TYPES, width=14,
                               state="readonly")
        type_cb.grid(row=1, column=1, sticky="w", **pad)
        ui_util.fit_combobox(type_cb)
        tk.Label(dlg, text=t("tools.current"), background=_R_BG_PANEL, foreground=_R_TEXT,
                 font=_F_MAIN).grid(row=2, column=0, sticky="e", **pad)
        tk.Label(dlg, text=_fmt_val(pv.value, ndf)[:80], background=_R_BG_PANEL,
                 foreground=_R_TEXT_DIM, font=_F_MAIN).grid(row=2, column=1, sticky="w", **pad)
        tk.Label(dlg, text=t("tools.new_value"), background=_R_BG_PANEL, foreground=_R_TEXT,
                 font=_F_MAIN).grid(row=3, column=0, sticky="e", **pad)
        new_var = tk.StringVar()
        ent = ttk.Entry(dlg, textvariable=new_var, width=42)
        ent.grid(row=3, column=1, sticky="ew", **pad)

        def ok():
            okay, err = self._commit_var(inst_idx, inst, prop_idx, type_var.get(), new_var.get())
            if not okay:
                ui_util.error(dlg, t("common.edit"), err or t("tools.could_not_apply_value"))
                return
            self._vars_refresh_props(isel[0])
            iid = f"{inst_idx}:{prop_idx}"
            if self._v_props.exists(iid):
                self._v_props.selection_set(iid)
            dlg.destroy()

        bf = tk.Frame(dlg, background=_R_BG_PANEL)
        bf.grid(row=4, column=0, columnspan=2, pady=8)
        ttk.Button(bf, text=t("tools.apply"), command=ok).pack(side="left", padx=8)
        ttk.Button(bf, text=t("common.cancel"), command=dlg.destroy).pack(side="left", padx=8)
        ent.focus_set()
        dlg.bind("<Return>", lambda *_: ok())
        dlg.bind("<Escape>", lambda *_: dlg.destroy())
        dlg.wait_window()

    def _vars_show_readonly(self, prop_obj, pv, tname):
        """Info dialog for a property whose type the raw editor can't author yet.  Shows the
        value read-only instead of opening a scalar box that would corrupt it on save."""
        ndf = self._v_ndf
        dlg = ui_util.themed_toplevel(self, t("tools.edit_name", name=prop_obj.name), resizable=True)
        pad = {"padx": 8, "pady": 4}
        tk.Label(dlg, text=prop_obj.name, background=_R_BG_PANEL, foreground=_R_GOLD_BRT,
                 font=_F_HEAD).grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        tk.Label(dlg, text=t("tools.type_type_isn_t_editable", type=tname),
                 background=_R_BG_PANEL, foreground=_R_TEXT, font=_F_MAIN,
                 wraplength=360, justify="left").grid(row=1, column=0, columnspan=2, sticky="w", **pad)
        tk.Label(dlg, text=t("tools.current"), background=_R_BG_PANEL, foreground=_R_TEXT,
                 font=_F_MAIN).grid(row=2, column=0, sticky="ne", **pad)
        tk.Label(dlg, text=_fmt_val(pv.value, ndf)[:200], background=_R_BG_PANEL,
                 foreground=_R_TEXT_DIM, font=_F_MAIN, wraplength=300, justify="left").grid(
            row=2, column=1, sticky="w", **pad)
        bf = tk.Frame(dlg, background=_R_BG_PANEL)
        bf.grid(row=3, column=0, columnspan=2, pady=8)
        ttk.Button(bf, text=t("common.close"), command=dlg.destroy).pack(side="left", padx=8)
        dlg.bind("<Escape>", lambda *_: dlg.destroy())
        dlg.wait_window()

    def _vars_edit_list(self, shown_idx, inst_idx, inst, prop_idx, prop_obj, pv):
        """Collection editor for a List property — a grid of typed rows with Add / Duplicate /
        Delete and in-place value editing.  Lists of scalars are editable; lists of ObjRef /
        nested containers / mixed types are shown read-only (safety — deleting an ObjRef element
        could break the mod)."""
        ndf = self._v_ndf
        elem_type_name, editable, reason = _list_elem_info(pv)
        empty = not pv.value.raw

        def _decode(e):
            # Native python value the user edits; strings for StringRef/PathRef.
            if e.type_id in (_T.StringRef, _T.PathRef):
                return ndf.resolve_value(e)
            return e.raw

        def _default_for(tn):
            if tn in ("Float32", "Float64"):
                return 0.0
            if tn in ("StringRef", "PathRef", "WideStr"):
                return ""
            return 0

        rows = [_decode(e) for e in (pv.value.raw or [])]

        dlg = ui_util.themed_toplevel(self, t("tools.edit_list_name", name=prop_obj.name),
                                      min_size=(380, 340), resizable=True)
        pad = {"padx": 8, "pady": 4}

        tk.Label(dlg, text=t("tools.edit_list_name", name=prop_obj.name), background=_R_BG_PANEL,
                 foreground=_R_GOLD_BRT, font=_F_HEAD).pack(anchor="w", **pad)
        if not editable and reason:
            tk.Label(dlg, text=reason, background=_R_BG_PANEL, foreground=_R_GOLD_BRT,
                     font=_F_MAIN, wraplength=360, justify="left").pack(anchor="w", **pad)

        top = tk.Frame(dlg, background=_R_BG_PANEL)
        top.pack(fill="x", **pad)
        tk.Label(top, text=t("tools.element_type"), background=_R_BG_PANEL, foreground=_R_TEXT,
                 font=_F_MAIN).pack(side="left")
        et_var = tk.StringVar(value=elem_type_name or ("UInt32" if editable else ""))
        # Fixed to what's on disk for a populated list; only an empty editable list lets the user pick.
        et_state = "readonly" if (empty and editable) else "disabled"
        et_cb = ttk.Combobox(top, textvariable=et_var, values=_SCALAR_ELEM_NAMES, width=12,
                             state=et_state)
        et_cb.pack(side="left", padx=6)
        ui_util.fit_combobox(et_cb)

        holder = tk.Frame(dlg, background=_R_BG_PANEL)
        holder.pack(fill="both", expand=True, **pad)
        tree = ttk.Treeview(holder, columns=("idx", "type", "value"), show="headings",
                            selectmode="browse", height=12)
        tree.heading("idx", text=t("tools.x"))
        tree.heading("type", text=t("tools.type"))
        tree.heading("value", text=t("common.value"))
        tree.column("idx", width=48, anchor="e", stretch=False)
        tree.column("type", width=90, stretch=False)
        tree.column("value", width=240, stretch=True)
        ui_util.with_scrollbars(holder, tree)

        popup = {"ent": None}

        def _kill_popup():
            if popup["ent"] is not None:
                popup["ent"].destroy()
                popup["ent"] = None

        def _row_type(i):
            # For an editable list every element shares et_var's type; for read-only use each
            # element's real type (they may be ObjRef etc.).
            if editable:
                return et_var.get()
            return _type_label(pv.value.raw[i].type_id)

        def _cell_text(i):
            if editable:
                return str(rows[i])
            return _fmt_val(pv.value.raw[i], ndf)

        def _repaint():
            _kill_popup()
            for it in tree.get_children():
                tree.delete(it)
            for i in range(len(rows)):
                tree.insert("", tk.END, iid=str(i), values=(i, _row_type(i), _cell_text(i)))
            ui_util.stripe_treeview(tree, _R_BG_WIDGET)
            ui_util.retag_treeview(tree)

        _repaint()

        def _begin_edit(iid):
            _kill_popup()
            tree.see(iid)
            box = tree.bbox(iid, "value")
            if not box:                     # cell scrolled out of view / not realized yet
                return
            x, y, w, h = box
            i = int(iid)
            ev = ttk.Entry(tree)
            ev.insert(0, str(rows[i]))
            ev.select_range(0, tk.END)
            ev.place(x=x, y=y, width=w, height=h)
            ev.focus_set()
            popup["ent"] = ev

            def _commit(_=None):
                parsed = _parse_val(ev.get(), et_var.get())
                if parsed is None:
                    ui_util.error(dlg, t("common.edit"),
                        t("tools.could_not_parse_raw_as", raw=ev.get(),
                          type_name=et_var.get()))
                    ev.focus_set()
                    return "break"
                rows[i] = parsed
                _kill_popup()
                tree.set(iid, "value", str(rows[i]))
                return "break"

            def _cancel(_=None):
                _kill_popup()
                return "break"

            ev.bind("<Return>", _commit)
            ev.bind("<FocusOut>", _commit)
            ev.bind("<Escape>", _cancel)

        def _on_double(event):
            if not editable:
                return
            if tree.identify_column(event.x) != "#3":     # only the value column
                return
            iid = tree.identify_row(event.y)
            if iid:
                _begin_edit(iid)

        tree.bind("<Double-1>", _on_double)

        def _add():
            rows.append(_default_for(et_var.get()))
            _repaint()
            iid = str(len(rows) - 1)
            tree.selection_set(iid)
            _begin_edit(iid)

        def _dup():
            sel = tree.selection()
            src = int(sel[0]) if sel else (len(rows) - 1)
            if src < 0:
                return
            rows.append(rows[src])
            _repaint()
            tree.selection_set(str(len(rows) - 1))

        def _delete():
            sel = tree.selection()
            if not sel:
                return
            del rows[int(sel[0])]
            _repaint()

        def _apply():
            _kill_popup()
            okay, err = self._commit_list_var(inst_idx, inst, prop_idx, et_var.get(), rows)
            if not okay:
                ui_util.error(dlg, t("common.edit"), err or t("tools.could_not_apply_value"))
                return
            self._vars_refresh_props(shown_idx)
            iid = f"{inst_idx}:{prop_idx}"
            if self._v_props.exists(iid):
                self._v_props.selection_set(iid)
            dlg.destroy()

        bf = tk.Frame(dlg, background=_R_BG_PANEL)
        bf.pack(fill="x", **pad)
        btns = []
        if editable:
            b_add = ttk.Button(bf, text=t("tools.add"), command=_add)
            b_dup = ttk.Button(bf, text=t("common.duplicate"), command=_dup)
            b_del = ttk.Button(bf, text=t("tools.delete"), command=_delete)
            b_ok = ttk.Button(bf, text=t("tools.apply"), command=_apply)
            b_cancel = ttk.Button(bf, text=t("common.cancel"), command=dlg.destroy)
            btns = [b_add, b_dup, b_del, b_ok, b_cancel]
        else:
            btns = [ttk.Button(bf, text=t("common.close"), command=dlg.destroy)]
        ui_util.flow(bf, btns)          # positions the buttons (place-based, wraps on narrow windows)

        dlg.bind("<Escape>", lambda *_: dlg.destroy())
        dlg.wait_window()

    def _goto_vars(self, ndf_path):
        if not ndf_path.lower().endswith(_NDF_EXTS):
            return
        self._nb.select(self._t_vars)
        files = getattr(self, "_v_ndf_files", [])
        if ndf_path in files:
            i = files.index(ndf_path)
            self._v_files.selection_clear(0, tk.END)
            self._v_files.selection_set(i)
            self._v_files.see(i)
            self._vars_on_file()
        return i if ndf_path in files else None

    # ── Search tab ──────────────────────────────────────────────────────────────────

    def _build_search(self, parent):
        ff = ttk.LabelFrame(parent, text=t("tools.search_ndf_dat"))
        ff.pack(fill="x", padx=6, pady=4)
        for i in (1, 3, 5):
            ff.columnconfigure(i, weight=1)
        pad = {"padx": 6, "pady": 4}
        tk.Label(ff, text=t("tools.class_2"), background=_R_BG, foreground=_R_TEXT, font=_F_MAIN).grid(
            row=0, column=0, sticky="e", **pad)
        self._s_class = tk.StringVar()
        ttk.Entry(ff, textvariable=self._s_class).grid(row=0, column=1, sticky="ew", **pad)
        tk.Label(ff, text=t("tools.property_2"), background=_R_BG, foreground=_R_TEXT, font=_F_MAIN).grid(
            row=0, column=2, sticky="e", **pad)
        self._s_prop = tk.StringVar()
        ttk.Entry(ff, textvariable=self._s_prop).grid(row=0, column=3, sticky="ew", **pad)
        tk.Label(ff, text=t("tools.value_has"), background=_R_BG, foreground=_R_TEXT, font=_F_MAIN).grid(
            row=0, column=4, sticky="e", **pad)
        self._s_val = tk.StringVar()
        ttk.Entry(ff, textvariable=self._s_val).grid(row=0, column=5, sticky="ew", **pad)
        br = tk.Frame(ff, background=_R_BG)
        br.grid(row=1, column=0, columnspan=6, pady=(0, 4))
        self._s_btn = ttk.Button(br, text=t("tools.search"), command=self._ns_search)
        self._s_btn.pack(side="left", padx=4)
        ttk.Button(br, text=t("tools.clear"), command=self._ns_clear).pack(side="left", padx=4)
        tk.Label(br, text=t("tools.empty_field_match_all_double"),
                 background=_R_BG, foreground=_R_TEXT_DIM, font=_F_MAIN).pack(side="left", padx=12)

        cols = ("ndf", "idx", "class", "name", "property", "value")
        sh = tk.Frame(parent, background=_R_BG)
        sh.pack(fill="both", expand=True, padx=6, pady=2)
        self._s_tv = ttk.Treeview(sh, columns=cols, show="headings", selectmode="browse")
        for c, hd, w in [("ndf", t("tools.ndf_file"), 150), ("idx", "#", 50), ("class", t("tools.class"), 130),
                         ("name", t("tools.instance"), 150), ("property", t("tools.property"), 130), ("value", t("common.value"), 220)]:
            self._s_tv.heading(c, text=hd)
            # only the small '#' column stretches; the rest are sized to content on each search so
            # the horizontal scrollbar reveals wide names/values (issue #5.4).
            self._s_tv.column(c, width=w, minwidth=40, stretch=(c == "idx"))
        ui_util.with_scrollbars(sh, self._s_tv)
        self._s_tv.bind("<Double-Button-1>", self._ns_goto)
        self._s_status = tk.Label(parent, text=t("tools.load_dat_then_search"), background=_R_BG,
                                  foreground=_R_TEXT_DIM, font=_F_MAIN)
        self._s_status.pack(anchor="w", padx=8, pady=(0, 4))

    def _ns_clear(self):
        if hasattr(self, "_s_class"):
            self._s_class.set("")
            self._s_prop.set("")
            self._s_val.set("")
        if hasattr(self, "_s_tv"):
            for it in self._s_tv.get_children():
                self._s_tv.delete(it)
            self._search_meta = {}
            self._s_status.configure(text=t("tools.cleared"))

    def _ns_search(self):
        if self._arc is None:
            return
        cls_f = self._s_class.get().lower().strip()
        prop_f = self._s_prop.get().lower().strip()
        val_f = self._s_val.get().lower().strip()
        for it in self._s_tv.get_children():
            self._s_tv.delete(it)
        self._search_meta = {}
        self._s_status.configure(text=t("tools.searching"))
        self._s_btn.configure(state="disabled")
        ndf_paths = [p for p in self._all_paths if p.lower().endswith(_NDF_EXTS)]

        def work():
            results = []
            for path in ndf_paths:
                try:
                    ndf = self._ns_cache.get(path)
                    if ndf is None:
                        raw = _arc_get(self._arc, path)
                        ndf = ndfbin_mod.read(raw)
                        self._ns_cache[path] = ndf
                except Exception:
                    continue
                cbi = {c.index: c.name for c in ndf.classes}
                pbi = {pr.index: pr for pr in ndf.properties}
                for ii, inst in enumerate(ndf.instances):
                    cn = cbi.get(inst.class_index, "?")
                    if cls_f and cls_f not in cn.lower():
                        continue
                    nm = ""
                    for pv in inst.props:
                        pr = pbi.get(pv.prop_index)
                        if pr and pr.name == "ClassNameForDebug":
                            nm = str(ndf.resolve_value(pv.value))
                            break
                    for pv in inst.props:
                        pr = pbi.get(pv.prop_index)
                        if pr is None or (prop_f and prop_f not in pr.name.lower()):
                            continue
                        vs = _fmt_val(pv.value, ndf)
                        if val_f and val_f not in vs.lower():
                            continue
                        results.append((path, ii, cn, nm, pr.name, vs))
                        if len(results) >= 5000:
                            break
                    if len(results) >= 5000:
                        break
                if len(results) >= 5000:
                    break
            self.after(0, lambda: self._ns_done(results))

        threading.Thread(target=work, daemon=True).start()

    def _ns_done(self, results):
        if not self.winfo_exists():                    # name-search worker returned after the frame closed
            return
        cells = {"ndf": [], "class": [], "name": [], "property": [], "value": []}
        for path, ii, cn, nm, pn, vs in results:
            ndf = path.split("/")[-1]
            iid = self._s_tv.insert("", tk.END, values=(ndf, ii, cn, nm, pn, vs))
            self._search_meta[iid] = (path, ii)
            cells["ndf"].append(ndf); cells["class"].append(cn); cells["name"].append(nm)
            cells["property"].append(pn); cells["value"].append(vs)
        for col, hd in (("ndf", t("tools.ndf_file")), ("class", t("tools.class")), ("name", t("tools.instance")),
                        ("property", t("tools.property")), ("value", t("common.value"))):
            ui_util.fit_tree_column(self._s_tv, col, cells[col], header=hd)   # #5.4
        ui_util.stripe_treeview(self._s_tv, _R_BG_WIDGET); ui_util.retag_treeview(self._s_tv)  # #5.2
        self._s_btn.configure(state="normal")
        cap = t("tools.capped_5000") if len(results) >= 5000 else ""
        self._s_status.configure(text=t("tools.n_result_s_cap", n=len(results), cap=cap))

    def _ns_goto(self, _=None):
        sel = self._s_tv.selection()
        if not sel:
            return
        meta = self._search_meta.get(sel[0])
        if not meta:
            return
        path, inst_idx = meta
        self._goto_vars(path)
        # select the instance in the (now unfiltered) instance list
        for j, e in enumerate(self._v_shown):
            if e[0] == inst_idx:
                self._v_inst.selection_clear(0, tk.END)
                self._v_inst.selection_set(j)
                self._v_inst.see(j)
                self._vars_refresh_props(j)
                break

    # ── save / status ────────────────────────────────────────────────────────────────

    def _notify(self):
        if self._on_change:
            try:
                self._on_change()
            except Exception:
                pass

    def _update_status(self):
        n = self._store.dirty_count()
        self._status.configure(text=(t("common.n_unsaved_change_set_s", n=n) if n else t("common.all_changes_saved")),
                               foreground=(_R_GOLD if n else _R_GREEN))

    def _save(self):
        if not self._store.is_dirty():
            ui_util.info(self, self._store.save_title, t("common.no_pending_changes_save"))
            return
        try:
            msg = self._store.save()
        except Exception as e:
            ui_util.error(self, t("common.save_failed"), t("tools.e_hint", e=e, hint=self._store.save_error_hint))
            return
        # re-open the current dat so listings/sizes reflect what was just written
        self._select_dat(self._dat_key)
        self._notify()
        self._update_status()
        ui_util.info(self, self._store.save_title, msg)

    def cleanup(self):
        """Release a nested store's temp working file (no-op for the project store).  Called by the Mod
        Editor host when this view is popped off the stack (the host destroys the frame itself)."""
        try:
            self._store.cleanup()
        except Exception:
            pass


if __name__ == "__main__":
    print("ToolsEditorWindow is launched from the Mod Editor hub in mod_manager.py")

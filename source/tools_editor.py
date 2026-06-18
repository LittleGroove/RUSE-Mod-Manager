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
from tkinter import ttk, messagebox, filedialog

REPO = os.path.dirname(os.path.abspath(__file__))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
from ruse_mod_engine import mod_project as mp_mod   # noqa: E402
from ruse_mod_engine import ndfbin as ndfbin_mod    # noqa: E402
from ruse_mod_engine import dic as dic_mod          # noqa: E402
from ruse_mod_engine import edata as edata_mod      # noqa: E402
import pil_log                                       # noqa: E402  (tags PIL DEBUG as "Raw editor")
from i18n import t                                    # noqa: E402
import ui_util                                        # noqa: E402  — language-aware widget sizing

try:
    from PIL import Image, ImageTk
    _HAVE_PIL = True
except Exception:
    _HAVE_PIL = False

# ── theme (matches the other editor windows) ─────────────────────────────────────
_R_BG, _R_BG_PANEL, _R_BG_WIDGET = "#08101c", "#0e1a2a", "#060d18"
_R_GOLD, _R_GOLD_BRT = "#c8a020", "#e0c030"
_R_TEXT, _R_TEXT_DIM = "#ccd8e8", "#3e5878"
_R_SEL_BG, _R_SEL_FG = "#1a3060", "#e0c030"
_R_GREEN, _R_RED, _R_BORDER = "#3a8030", "#b03020", "#243a5c"
_F_MAIN = ("Courier New", 9)
_F_BOLD = ("Courier New", 9, "bold")
_F_HEAD = ("Courier New", 10, "bold")

# dat_key -> friendly label for the picker.  ALL six public dats (every one that gets backed up) — edit
# and Save writes the working copy into the project folder just like the gameplay dat.  Order matches the
# game's own layout loosely (gameplay first, then maps, localization, common).
_DAT_CHOICES = [
    ("gameplay",    t("Gameplay  ·  ZZ_GladPatchableWin.dat")),
    ("gameplay_np", t("Gameplay (non-patchable)  ·  ZZ_GladNotPatchableWin.dat")),
    ("scripts",     t("AI Scripts  ·  IA_Common.dat")),
    ("maps",        t("Maps  ·  DataMap_Win.dat")),
    ("loc",         t("Localization / Textures  ·  ZZ_Win.dat")),
    ("common",      t("Common (video / fonts)  ·  Data_Common.dat")),
]

_T = ndfbin_mod.T
_TYPE_NAMES = {
    _T.Bool: "Bool", _T.Int8: "Int8", _T.Int16: "Int16", _T.UInt16: "UInt16",
    _T.Int32: "Int32", _T.UInt32: "UInt32", _T.Long: "Long",
    _T.Float32: "Float32", _T.Float64: "Float64",
    _T.StringRef: "StringRef", _T.PathRef: "PathRef", _T.WideStr: "WideStr",
    _T.Vector3: "Vector3", _T.Color128: "Color128", _T.Color32: "Color32",
}
_EDIT_TYPES = ["Bool", "Int8", "Int16", "UInt16", "Int32", "UInt32", "Long",
               "Float32", "Float64", "StringRef", "PathRef", "WideStr",
               "Vector3", "Color128", "Color32", "TripleInt", "Int2", "Float2"]

_NDF_EXTS = (".gladndfbin", ".ndfbin", ".truendfbin")
_TGV_EXTS = (".tgv", ".tgv_pc")
_IMG_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tga", ".dds")
_TEXT_EXTS = (".xml", ".txt", ".ndf", ".lua", ".cfg", ".ini", ".csv", ".json", ".scenario")
# Renamed .dat archives that live INSIDE the six main dats: .ipk (Python), .apk (animation),
# .mpk (sound), .ppk (textures) — plus a literal embedded .dat.  These are edata containers and
# can be opened recursively in their own Raw / Asset Editor window (gated on the real 'edat' magic,
# so the extension list is only a hint for the Type column).
_NESTED_DAT_EXTS = (".ipk", ".apk", ".mpk", ".ppk", ".dat")


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
    if p.endswith(_TEXT_EXTS):
        return "Text"
    if p.endswith(_NESTED_DAT_EXTS):
        return "Nested .dat"
    return "Binary"


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


def _hexdump(b: bytes, limit: int = 8192) -> str:
    out = []
    chunk = b[:limit]
    for i in range(0, len(chunk), 16):
        row = chunk[i:i + 16]
        h = " ".join(f"{x:02x}" for x in row)
        a = "".join(chr(x) if 32 <= x < 127 else "." for x in row)
        out.append(f"{i:08x}  {h:<48}  {a}")
    if len(b) > limit:
        out.append(t("\n… {n:,} more bytes (export to see the whole file)", n=len(b) - limit))
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
    head = t("TRA localization — {n} entries (key = 8-byte LocHash)\n", n=len(entries)) + "─" * 60 + "\n"
    lines = [f"{k.hex()}  {v}" for k, v in entries[:limit]]
    if len(entries) > limit:
        lines.append(t("\n… {n:,} more entries (export to see all)", n=len(entries) - limit))
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
    picker_label = t("Project file:")
    save_button_label = t("Save mod (.dat)")
    save_title = t("Save mod")
    save_error_hint = t("Tip: set the Game Root in Settings.")
    save_help = t(
        "Reads the mod's own .dat if it exists, else the clean backup. Edits & imports stage into "
        "the project; “Save mod (.dat)” writes them into the mod, just like the other editor windows. "
        "Import/Export work for every file. Use “Open as nested .dat” on a .ipk/.apk/.mpk/.ppk (or any "
        "embedded .dat) to edit the archive inside it.")

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
                out.append((key, t("Terrain map  ·  {name}", name=key.split('/', 1)[1])))
        except Exception:
            pass
        return out

    def read_source(self, dat_key):
        return self._p.read_source(dat_key)

    def is_mod_path(self, dat_key, src):
        return Path(src) == self._p.project_dat_path(dat_key)

    def source_kind(self, dat_key, src):
        return t("mod copy") if self.is_mod_path(dat_key, src) else t("clean backup / game")

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
        return t("Saved mod changes to:\n\n") + "\n".join(written)

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
    picker_label = t("Nested archive:")
    save_button_label = t("Apply into parent .dat")
    save_title = t("Apply to parent")
    save_error_hint = t("The parent archive may be an unsupported edata version (v2 can't add files).")

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
        return (t("Editing the archive embedded at  ") + self._entry_path + t(".  “Apply into parent .dat” "
                "stages the modified archive back into the parent window (it does NOT write to disk); "
                "switch to the parent window and Save there to commit it to the .dat."))

    def dat_choices(self):
        label = self._entry_path if len(self._entry_path) < 70 else "…" + self._entry_path[-68:]
        return [(self._KEY, t("Nested · ") + label)]

    def read_source(self, dat_key):
        return self._tmp

    def is_mod_path(self, dat_key, src):
        return True

    def source_kind(self, dat_key, src):
        return t("nested archive")

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
        return (t("Applied the modified archive back into the parent, staged at:\n\n  ")
                + self._entry_path + t("\n\nSwitch to the parent window and click “")
                + self._parent.save_button_label + t("” to write it to the .dat on disk."))

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
        nb.add(self._t_browse, text=t("  Browse / Files  "))
        nb.add(self._t_vars,   text=t("  NDF Vars  "))
        nb.add(self._t_search, text=t("  Search  "))
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
            self._src_lbl.configure(text=t("⚠ {e}", e=e), foreground=_R_RED)
            self._browse_refresh()
            self._vars_reload()
            return
        is_mod = self._store.is_mod_path(dat_key, src)
        self._sizes = {e.path: e.size for e in self._arc._entries}
        self._all_paths = sorted((e.path.replace("\\", "/") for e in self._arc._entries),
                                 key=str.lower)
        where = self._store.source_kind(dat_key, src)
        self._src_lbl.configure(
            text=t("{name}  ·  {n:,} entries  ·  from {where}",
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
        tk.Label(fr, text=t("Filter:"), background=_R_BG, foreground=_R_TEXT, font=_F_MAIN).pack(side="left")
        self._bf_var = tk.StringVar()
        self._bf_var.trace_add("write", lambda *_: self._browse_refresh())
        ttk.Entry(fr, textvariable=self._bf_var).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(fr, text="✕", width=2, command=lambda: self._bf_var.set("")).pack(side="left")

        cols = ("path", "kind", "size")
        tvh = tk.Frame(left, background=_R_BG)
        tvh.pack(fill="both", expand=True)
        self._b_tv = ttk.Treeview(tvh, columns=cols, show="headings", selectmode="browse")
        for c, hd, w in [("path", t("Virtual Path"), 360), ("kind", t("Type"), 110), ("size", t("Size"), 90)]:
            self._b_tv.heading(c, text=hd)
            # path doesn't stretch — it's widened to the longest path on refresh so the horizontal
            # scrollbar can reveal full virtual paths (issue #5.4).
            self._b_tv.column(c, width=w, minwidth=50, stretch=(c != "path"),
                              anchor=("e" if c == "size" else "w"))
        ui_util.with_scrollbars(tvh, self._b_tv)
        self._b_tv.bind("<<TreeviewSelect>>", self._on_browse_select)

        self._b_count = tk.Label(left, text="", background=_R_BG, foreground=_R_TEXT_DIM, font=_F_MAIN)
        self._b_count.pack(anchor="w", pady=(2, 0))

        right = ttk.LabelFrame(pw, text=t("Preview"))
        pw.add(right, weight=3)
        self._pv_head = tk.Label(right, text=t("Select a file to preview."), anchor="w", justify="left",
                                 background=_R_BG_PANEL, foreground=_R_GOLD_BRT, font=_F_BOLD)
        self._pv_head.pack(fill="x", padx=4, pady=(4, 2))
        body = tk.Frame(right, background=_R_BG_PANEL)
        body.pack(fill="both", expand=True, padx=4, pady=2)
        self._pv_body = body
        self._pv_img = tk.Label(body, background=_R_BG_WIDGET, anchor="center")
        self._pv_txt_frame, self._pv_txt = self._mk_text(body)
        self._pv_goto = ttk.Button(right, text=t("Edit these vars in the NDF Vars tab  →"),
                                   command=self._browse_goto_vars)

        ab = tk.Frame(right, background=_R_BG_PANEL)
        ab.pack(fill="x", padx=4, pady=(2, 4))
        ttk.Button(ab, text=t("Export…"), command=self._export).pack(side="left", padx=2)
        ttk.Button(ab, text=t("Import / Replace…"), command=self._import).pack(side="left", padx=2)
        ttk.Button(ab, text=t("Add File…"), command=self._add_file).pack(side="left", padx=2)
        self._pv_nest_btn = ttk.Button(ab, text=t("Open as nested .dat  →"), command=self._open_nested,
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
        ui_util.fit_tree_column(self._b_tv, "path", shown_paths, header=t("Virtual Path"))  # #5.4
        ui_util.stripe_treeview(self._b_tv, _R_BG_WIDGET); ui_util.retag_treeview(self._b_tv)  # #5.2
        total = sum(1 for p in self._all_paths if (not flt or flt in p.lower()))
        cap = t("  (showing first {n:,} — narrow with Filter)", n=self._BROWSE_CAP) if total > self._BROWSE_CAP else ""
        self._b_count.configure(text=t("{n:,} match(es){cap}", n=total, cap=cap))

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
        if which == "image":
            self._pv_img.pack(fill="both", expand=True)
        elif which == "text":
            self._pv_txt_frame.pack(fill="both", expand=True)

    def _set_text(self, s):
        self._pv_txt.configure(state="normal")
        self._pv_txt.delete("1.0", tk.END)
        self._pv_txt.insert("1.0", s)
        self._pv_txt.configure(state="disabled")

    def _on_browse_select(self, _=None):
        path = self._selected_browse_path()
        self._pv_nest_btn.configure(state="disabled")
        if not path or self._arc is None:
            return
        raw = self._entry_bytes(path)
        if raw is None:
            self._pv_head.configure(text=t("{path}\n(could not read entry)", path=path))
            return
        # Enable "Open as nested .dat" only for entries that really are edata containers — keyed on
        # the 'edat' magic, not the extension, so a renamed .ipk/.apk/.mpk/.ppk is detected correctly.
        if _is_edata(raw):
            self._pv_nest_btn.configure(state="normal")
        kind = _entry_kind(path)
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
                self._set_text(t("NDF binary — {inst} instances, {cls} classes, {props} property names.\n"
                                 "compressed={compressed}\n\nTop classes:\n{top}",
                                 inst=len(ndf.instances), cls=len(ndf.classes),
                                 props=len(ndf.properties), compressed=ndf.is_compressed, top=top))
            except Exception as e:
                self._set_text(t("NDF (could not parse for summary: {e})\n\n", e=e) + _hexdump(raw))
            self._pv_head.configure(text=t("{name}   ·   {kind}   ·   {n:,} bytes",
                                           name=name, kind=kind, n=len(raw)))
            self._show_preview_widget("text")
            self._pv_goto.pack(fill="x", padx=4, pady=(0, 2))
            self._pv_status.configure(text="")
            return

        # images (.tgv textures and ordinary image files)
        img, info = None, ""
        if kind == "Texture (TGV)":
            res = _tgv_image(raw)
            if res:
                img, fmt = res
                info = t("   ·   {w}×{h} {fmt}", w=img.width, h=img.height, fmt=fmt)
        elif kind == "Image":
            img = _pil_open(raw)
            if img:
                info = t("   ·   {w}×{h}", w=img.width, h=img.height)
        if img is not None:
            disp = img.copy()
            disp.thumbnail((720, 600))
            self._img_ref = ImageTk.PhotoImage(disp)
            self._pv_img.configure(image=self._img_ref, text="")
            self._pv_head.configure(text=t("{name}   ·   {kind}{info}   ·   {n:,} bytes",
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
                text = t("(.dic parse failed: {e})\n\n", e=e) + _hexdump(raw)
        elif kind == "AI Script":
            text = _xyz_inflate(raw)
            if text is None:
                text = t("(compiled .xyz — could not inflate; export to inspect)\n\n") + _hexdump(raw)
        elif kind == "Text" or _looks_text(raw):
            text = _decode_text(raw)

        if text is not None:
            self._set_text(text)
            self._show_preview_widget("text")
        else:
            extra = "" if _HAVE_PIL or kind not in ("Texture (TGV)", "Image") else t("  (Pillow not available)")
            self._set_text(t("No inline preview for this type{extra} — use Export to open it externally, "
                             "or Import to replace it.\n\nHex preview:\n\n", extra=extra) + _hexdump(raw))
            self._show_preview_widget("text")
        self._pv_head.configure(text=t("{name}   ·   {kind}   ·   {n:,} bytes",
                                       name=name, kind=kind, n=len(raw)))
        self._pv_status.configure(text="")

    def _browse_goto_vars(self):
        path = self._selected_browse_path()
        if path:
            self._goto_vars(path)

    def _export(self):
        path = self._selected_browse_path()
        if not path or self._arc is None:
            messagebox.showinfo(t("Export"), t("Select a file in the list first."), parent=self)
            return
        raw = self._entry_bytes(path)
        if raw is None:
            messagebox.showerror(t("Export"), t("Could not read that entry."), parent=self)
            return
        dest = filedialog.asksaveasfilename(title=t("Export file as"), initialfile=path.split("/")[-1],
                                            parent=self, filetypes=[(t("All files"), "*.*")])
        if not dest:
            return
        try:
            Path(dest).write_bytes(raw)
            self._pv_status.configure(text=t("Exported → {name}", name=Path(dest).name))
        except Exception as e:
            messagebox.showerror(t("Export failed"), str(e), parent=self)

    def _import(self):
        path = self._selected_browse_path()
        if not path:
            messagebox.showinfo(t("Import / Replace"), t("Select the entry you want to replace first."), parent=self)
            return
        src = filedialog.askopenfilename(title=t("Replacement for  {name}", name=path.split('/')[-1]),
                                         parent=self, filetypes=[(t("All files"), "*.*")])
        if not src:
            return
        if not messagebox.askyesno(t("Replace entry?"),
                                   t("Replace inside the mod:\n  {path}\nwith:\n  {src}\n\n"
                                     "(Staged now; written when you Save.)", path=path, src=src), parent=self):
            return
        try:
            data = Path(src).read_bytes()
            self._store.set_raw(self._dat_key, path, data)
        except Exception as e:
            messagebox.showerror(t("Import failed"), str(e), parent=self)
            return
        self._sizes[path] = len(data)
        self._sizes[path.replace("/", "\\")] = len(data)
        if self._b_tv.exists(path):
            self._b_tv.set(path, "size", f"{len(data):,}")
        self._pv_status.configure(text=t("Replaced (staged) — {n:,} bytes. Save to write.", n=len(data)))
        self._notify()
        self._update_status()

    def _do_add_file(self, vpath: str, data: bytes):
        """Stage a NEW entry at vpath (testable; no dialogs).  Returns (ok, error).  Refuses an empty
        path or one that already exists (that's a replace — use Import instead)."""
        vpath = (vpath or "").strip().strip("/\\")
        if not vpath:
            return False, t("Enter a virtual path for the new file.")
        disp = vpath.replace("\\", "/")
        if disp in self._all_paths or disp.replace("/", "\\") in self._sizes or disp in self._sizes:
            return False, t("An entry already exists at:\n  {disp}\n\nUse Import / Replace to overwrite it.", disp=disp)
        self._store.set_raw(self._dat_key, vpath, data)
        self._all_paths.append(disp)
        self._all_paths.sort(key=str.lower)
        self._sizes[disp] = len(data)
        self._sizes[disp.replace("/", "\\")] = len(data)
        return True, None

    def _ask_vpath(self, default: str, title_name: str):
        """Modal prompt for the new entry's virtual path inside the dat.  Returns the path or None."""
        dlg = tk.Toplevel(self)
        dlg.title(t("Add  {name}", name=title_name))
        dlg.configure(background=_R_BG_PANEL)
        dlg.transient(self)
        dlg.grab_set()
        pad = {"padx": 8, "pady": 4}
        tk.Label(dlg, text=t("Virtual path inside the .dat"), background=_R_BG_PANEL,
                 foreground=_R_GOLD_BRT, font=_F_HEAD).grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        tk.Label(dlg, text=t("(e.g. pc\\ndf\\patchable\\gfx\\myfile.ndfbin — use the game's folder layout)"),
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
        ttk.Button(bf, text=t("Add"), command=ok).pack(side="left", padx=8)
        ttk.Button(bf, text=t("Cancel"), command=dlg.destroy).pack(side="left", padx=8)
        ent.focus_set()
        ent.icursor("end")
        dlg.bind("<Return>", lambda *_: ok())
        dlg.bind("<Escape>", lambda *_: dlg.destroy())
        dlg.wait_window()
        return result["path"]

    def _add_file(self):
        if self._arc is None:
            return
        src = filedialog.askopenfilename(title=t("Add a file to this .dat"),
                                         parent=self, filetypes=[(t("All files"), "*.*")])
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
            messagebox.showerror(t("Add file"), str(e), parent=self)
            return
        ok, err = self._do_add_file(vpath, data)
        if not ok:
            messagebox.showerror(t("Add file"), err, parent=self)
            return
        disp = vpath.strip().strip("/\\").replace("\\", "/")
        self._browse_refresh()
        if self._b_tv.exists(disp):
            self._b_tv.selection_set(disp)
            self._b_tv.see(disp)
            self._on_browse_select()
        self._pv_status.configure(text=t("Added (staged) — {n:,} bytes. Save to write.", n=len(data)))
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
            messagebox.showinfo(t("Open nested .dat"),
                                t("This entry isn't an embedded .dat archive (no 'edat' header)."),
                                parent=self)
            return
        try:
            store = NestedDatStore(self._store, self._dat_key, path, raw)
        except Exception as e:
            messagebox.showerror(t("Open nested .dat"),
                                 t("Could not open the embedded archive:\n{e}", e=e), parent=self)
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

        lf = ttk.LabelFrame(pw, text=t("NDF Files"))
        pw.add(lf, weight=1)   # equal weights → the three lists default to 1/3 each (still draggable)
        self._v_files = self._mk_listbox(lf, selectmode="browse")
        ui_util.with_scrollbars(lf, self._v_files)   # horizontal scroll for long NDF paths (issue #5.4)
        self._v_files.bind("<<ListboxSelect>>", self._vars_on_file)

        cf = ttk.LabelFrame(pw, text=t("Instances"))
        pw.add(cf, weight=1)
        row = tk.Frame(cf, background=_R_BG)
        row.pack(fill="x", padx=2, pady=(2, 0))
        tk.Label(row, text=t("Filter:"), background=_R_BG, foreground=_R_TEXT, font=_F_MAIN).pack(side="left")
        self._v_filter = tk.StringVar()
        self._v_filter.trace_add("write", lambda *_: self._vars_apply_filter())
        ttk.Entry(row, textvariable=self._v_filter).pack(side="left", fill="x", expand=True, padx=4)
        ih = tk.Frame(cf, background=_R_BG)
        ih.pack(fill="both", expand=True, padx=2, pady=2)
        self._v_inst = self._mk_listbox(ih, selectmode="browse")
        ui_util.with_scrollbars(ih, self._v_inst)   # horizontal scroll for long instance names (#5.4)
        self._v_inst.bind("<<ListboxSelect>>", self._vars_on_inst)

        rf = ttk.LabelFrame(pw, text=t("Properties"))
        pw.add(rf, weight=1)
        ui_util.equalize_panes(pw)   # start the three lists at equal (1/3) widths (issue #5.4)
        eb = tk.Frame(rf, background=_R_BG)
        eb.pack(fill="x", padx=2, pady=(2, 0))
        ttk.Button(eb, text=t("Edit Value"), command=self._vars_edit).pack(side="left", padx=2)
        tk.Label(eb, text=t("or double-click a row"), background=_R_BG, foreground=_R_TEXT_DIM,
                 font=_F_MAIN).pack(side="left", padx=6)
        cols = ("property", "type", "value", "edited")
        ph = tk.Frame(rf, background=_R_BG)
        ph.pack(fill="both", expand=True, padx=2, pady=2)
        self._v_props = ttk.Treeview(ph, columns=cols, show="headings", selectmode="browse")
        for c, hd, w in [("property", t("Property"), 160), ("type", t("Type"), 80),
                         ("value", t("Value"), 240), ("edited", "", 40)]:
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
            self._v_files.insert(tk.END, t("(no NDF files in this dat)"))

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
            messagebox.showerror(t("NDF"), t("Could not load {path}:\n{e}", path=path, e=e), parent=self)
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
            tname = _TYPE_NAMES.get(pv.value.type_id, f"0x{pv.value.type_id:02X}")
            key = (self._v_ndf_path, inst_idx, pv.prop_index)
            tag = ("mod",) if key in self._v_modified else ()
            mark = "✎" if key in self._v_modified else ""
            vstr = _fmt_val(pv.value, ndf)
            names.append(pr.name); vals.append(vstr)
            self._v_props.insert("", tk.END, iid=f"{inst_idx}:{pv.prop_index}",
                                 values=(pr.name, tname, vstr, mark), tags=tag)
        ui_util.fit_tree_column(self._v_props, "property", names, header=t("Property"))   # #5.4
        ui_util.fit_tree_column(self._v_props, "value", vals, header=t("Value"))          # #5.4
        ui_util.stripe_treeview(self._v_props, _R_BG_WIDGET); ui_util.retag_treeview(self._v_props)  # #5.2

    def _commit_var(self, inst_idx, inst, prop_idx, type_name, raw_in):
        """Parse raw_in as type_name and write it onto the instance's property, staging the edit.
        Returns (ok, error). Separated from the dialog so it's unit-testable."""
        raw_in = (raw_in or "").strip()
        if not raw_in:
            return False, t("Enter a value.")
        parsed = _parse_val(raw_in, type_name)
        if parsed is None:
            return False, t("Could not parse '{raw}' as {type_name}.", raw=raw_in, type_name=type_name)
        pv = next((pv for pv in inst.props if pv.prop_index == prop_idx), None)
        if pv is None:
            return False, t("Property is not present on this instance.")
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

    def _vars_edit(self, _=None):
        psel = self._v_props.selection()
        isel = self._v_inst.curselection()
        if not psel or not isel or self._v_ndf is None:
            messagebox.showinfo(t("Edit"), t("Select an instance and a property first."), parent=self)
            return
        inst_idx, _c, _d, inst, _l = self._v_shown[isel[0]]
        prop_idx = int(psel[0].split(":")[1])
        ndf = self._v_ndf
        prop_obj = next((p for p in ndf.properties if p.index == prop_idx), None)
        pv = next((pv for pv in inst.props if pv.prop_index == prop_idx), None)
        if prop_obj is None or pv is None:
            return
        tname = _TYPE_NAMES.get(pv.value.type_id, f"0x{pv.value.type_id:02X}")

        dlg = tk.Toplevel(self)
        dlg.title(t("Edit  {name}", name=prop_obj.name))
        dlg.configure(background=_R_BG_PANEL)
        dlg.transient(self)
        dlg.grab_set()
        pad = {"padx": 8, "pady": 4}
        tk.Label(dlg, text=prop_obj.name, background=_R_BG_PANEL, foreground=_R_GOLD_BRT,
                 font=_F_HEAD).grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        tk.Label(dlg, text=t("Type:"), background=_R_BG_PANEL, foreground=_R_TEXT,
                 font=_F_MAIN).grid(row=1, column=0, sticky="e", **pad)
        type_var = tk.StringVar(value=tname if tname in _EDIT_TYPES else "Int32")
        type_cb = ttk.Combobox(dlg, textvariable=type_var, values=_EDIT_TYPES, width=14,
                               state="readonly")
        type_cb.grid(row=1, column=1, sticky="w", **pad)
        ui_util.fit_combobox(type_cb)
        tk.Label(dlg, text=t("Current:"), background=_R_BG_PANEL, foreground=_R_TEXT,
                 font=_F_MAIN).grid(row=2, column=0, sticky="e", **pad)
        tk.Label(dlg, text=_fmt_val(pv.value, ndf)[:80], background=_R_BG_PANEL,
                 foreground=_R_TEXT_DIM, font=_F_MAIN).grid(row=2, column=1, sticky="w", **pad)
        tk.Label(dlg, text=t("New value:"), background=_R_BG_PANEL, foreground=_R_TEXT,
                 font=_F_MAIN).grid(row=3, column=0, sticky="e", **pad)
        new_var = tk.StringVar()
        ent = ttk.Entry(dlg, textvariable=new_var, width=42)
        ent.grid(row=3, column=1, sticky="ew", **pad)

        def ok():
            okay, err = self._commit_var(inst_idx, inst, prop_idx, type_var.get(), new_var.get())
            if not okay:
                messagebox.showerror(t("Edit"), err or t("Could not apply value."), parent=dlg)
                return
            self._vars_refresh_props(isel[0])
            iid = f"{inst_idx}:{prop_idx}"
            if self._v_props.exists(iid):
                self._v_props.selection_set(iid)
            dlg.destroy()

        bf = tk.Frame(dlg, background=_R_BG_PANEL)
        bf.grid(row=4, column=0, columnspan=2, pady=8)
        ttk.Button(bf, text=t("Apply"), command=ok).pack(side="left", padx=8)
        ttk.Button(bf, text=t("Cancel"), command=dlg.destroy).pack(side="left", padx=8)
        ent.focus_set()
        dlg.bind("<Return>", lambda *_: ok())
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
        ff = ttk.LabelFrame(parent, text=t("Search NDF (this dat)"))
        ff.pack(fill="x", padx=6, pady=4)
        for i in (1, 3, 5):
            ff.columnconfigure(i, weight=1)
        pad = {"padx": 6, "pady": 4}
        tk.Label(ff, text=t("Class:"), background=_R_BG, foreground=_R_TEXT, font=_F_MAIN).grid(
            row=0, column=0, sticky="e", **pad)
        self._s_class = tk.StringVar()
        ttk.Entry(ff, textvariable=self._s_class).grid(row=0, column=1, sticky="ew", **pad)
        tk.Label(ff, text=t("Property:"), background=_R_BG, foreground=_R_TEXT, font=_F_MAIN).grid(
            row=0, column=2, sticky="e", **pad)
        self._s_prop = tk.StringVar()
        ttk.Entry(ff, textvariable=self._s_prop).grid(row=0, column=3, sticky="ew", **pad)
        tk.Label(ff, text=t("Value has:"), background=_R_BG, foreground=_R_TEXT, font=_F_MAIN).grid(
            row=0, column=4, sticky="e", **pad)
        self._s_val = tk.StringVar()
        ttk.Entry(ff, textvariable=self._s_val).grid(row=0, column=5, sticky="ew", **pad)
        br = tk.Frame(ff, background=_R_BG)
        br.grid(row=1, column=0, columnspan=6, pady=(0, 4))
        self._s_btn = ttk.Button(br, text=t("  Search  "), command=self._ns_search)
        self._s_btn.pack(side="left", padx=4)
        ttk.Button(br, text=t("Clear"), command=self._ns_clear).pack(side="left", padx=4)
        tk.Label(br, text=t("(empty field = match all · double-click a result to edit it)"),
                 background=_R_BG, foreground=_R_TEXT_DIM, font=_F_MAIN).pack(side="left", padx=12)

        cols = ("ndf", "idx", "class", "name", "property", "value")
        sh = tk.Frame(parent, background=_R_BG)
        sh.pack(fill="both", expand=True, padx=6, pady=2)
        self._s_tv = ttk.Treeview(sh, columns=cols, show="headings", selectmode="browse")
        for c, hd, w in [("ndf", t("NDF File"), 150), ("idx", "#", 50), ("class", t("Class"), 130),
                         ("name", t("Instance"), 150), ("property", t("Property"), 130), ("value", t("Value"), 220)]:
            self._s_tv.heading(c, text=hd)
            # only the small '#' column stretches; the rest are sized to content on each search so
            # the horizontal scrollbar reveals wide names/values (issue #5.4).
            self._s_tv.column(c, width=w, minwidth=40, stretch=(c == "idx"))
        ui_util.with_scrollbars(sh, self._s_tv)
        self._s_tv.bind("<Double-Button-1>", self._ns_goto)
        self._s_status = tk.Label(parent, text=t("Load a dat then search."), background=_R_BG,
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
            self._s_status.configure(text=t("Cleared."))

    def _ns_search(self):
        if self._arc is None:
            return
        cls_f = self._s_class.get().lower().strip()
        prop_f = self._s_prop.get().lower().strip()
        val_f = self._s_val.get().lower().strip()
        for it in self._s_tv.get_children():
            self._s_tv.delete(it)
        self._search_meta = {}
        self._s_status.configure(text=t("Searching…"))
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
        cells = {"ndf": [], "class": [], "name": [], "property": [], "value": []}
        for path, ii, cn, nm, pn, vs in results:
            ndf = path.split("/")[-1]
            iid = self._s_tv.insert("", tk.END, values=(ndf, ii, cn, nm, pn, vs))
            self._search_meta[iid] = (path, ii)
            cells["ndf"].append(ndf); cells["class"].append(cn); cells["name"].append(nm)
            cells["property"].append(pn); cells["value"].append(vs)
        for col, hd in (("ndf", t("NDF File")), ("class", t("Class")), ("name", t("Instance")),
                        ("property", t("Property")), ("value", t("Value"))):
            ui_util.fit_tree_column(self._s_tv, col, cells[col], header=hd)   # #5.4
        ui_util.stripe_treeview(self._s_tv, _R_BG_WIDGET); ui_util.retag_treeview(self._s_tv)  # #5.2
        self._s_btn.configure(state="normal")
        cap = t(" (capped at 5000)") if len(results) >= 5000 else ""
        self._s_status.configure(text=t("{n} result(s){cap}", n=len(results), cap=cap))

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
        self._status.configure(text=(t("● {n} unsaved change-set(s)", n=n) if n else t("✓ all changes saved")),
                               foreground=(_R_GOLD if n else _R_GREEN))

    def _save(self):
        if not self._store.is_dirty():
            messagebox.showinfo(self._store.save_title, t("No pending changes to save."), parent=self)
            return
        try:
            msg = self._store.save()
        except Exception as e:
            messagebox.showerror(t("Save failed"), t("{e}\n\n{hint}", e=e, hint=self._store.save_error_hint), parent=self)
            return
        # re-open the current dat so listings/sizes reflect what was just written
        self._select_dat(self._dat_key)
        self._notify()
        self._update_status()
        messagebox.showinfo(self._store.save_title, msg, parent=self)

    def cleanup(self):
        """Release a nested store's temp working file (no-op for the project store).  Called by the Mod
        Editor host when this view is popped off the stack (the host destroys the frame itself)."""
        try:
            self._store.cleanup()
        except Exception:
            pass


if __name__ == "__main__":
    print("ToolsEditorWindow is launched from the Mod Editor hub in mod_manager.py")

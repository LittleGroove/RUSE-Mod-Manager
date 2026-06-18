"""
Mod project model for the RUSE Mod Editor.

A *mod project* is a folder under ``output/editor_mods/<name>/`` that owns the
working copies of the game .dat files being modified plus a ``project.json``
metadata file.  All editor windows (Units, Map, AI, Economy ...) edit a single
*shared* in-memory NDF object per game file, held by the project, so edits
accumulate across windows until the user saves.

Lifecycle
---------
  create(base_dir, name, ...)  -> make the folder + project.json
  load(folder, ...)            -> reopen an existing project
  get_ndf(dat_key, ndf_path)   -> shared, cached NdfBinary (edits mutate it)
  mark_dirty(dat_key, ndf_path)-> flag an edited entry
  save_all()                   -> flush every dirty entry into the project .dat
  deploy(backup_dir)           -> copy project .dat(s) over the live game files

The project reads from its own .dat when one exists (so a reopened project shows
prior edits) and otherwise from the live game .dat.  The project .dat is created
lazily the first time ``save_all`` needs to write a given file.
"""

import json
import re
import shutil
import time
from pathlib import Path

from . import edata as _edata
from . import ndfbin as _ndfbin

# Version → Data/PC subfolder.  New tools target the public build.
_VERSION_SUB = {"public": "190852", "compat": "99"}

# Backups and mod projects now mirror the GAME ROOT: the six core dats live at Data/PC/<sub>/<dat>, and
# the per-terrain dats live at Maps/PC/<dat> (alongside Data/, just like the install).
#
# dat_key → filename inside Data/PC/<sub>/.  The first three have dedicated editor windows; ALL six
# (every core .dat that gets backed up) are browsable/editable in the Raw / Asset Editor, and saving
# any of them writes a working copy into the project's Data/PC/<sub>/ just like the gameplay dat.
DAT_FILES = {
    "gameplay": "ZZ_GladPatchableWin.dat",        # units, buildings, economy, AI params, menus (NDF)
    "scripts": "IA_Common.dat",                   # AI / mission / challenge .xyz Python scripts
    "loc": "ZZ_Win.dat",                          # localization .dic, textures (.tgv), UI NDF (.truendfbin)
    "gameplay_np": "ZZ_GladNotPatchableWin.dat",  # non-patchable gameplay NDF (.gladndfbin)
    "maps": "DataMap_Win.dat",                    # map NDF (.ndfbin), .kdt, .scenario, mapinfo .win
    "common": "Data_Common.dat",                  # videos (.webm), fonts (.ttf/.ttc), .xml
}

# Per-terrain minimap/world dats live in the game's Maps/PC/ folder (shared across game versions; ~32
# of them, named per map, e.g. DataMapSuperCrossroads4_v09.dat).  They are NOT in DAT_FILES — they're
# discovered dynamically (from the mod folder, backup, or game) and addressed by a dat_key of the form
# "terrain/<filename>", so the same get_raw / get_ndf / set_raw / save_all machinery handles them.
_TERRAIN_PREFIX = "terrain/"

# Well-known NDF entries inside the gameplay dat.
EVERYTHING_PATH  = r"genglad\patchable\gfx\everything.cpp.gladndfbin"
GDCONSTANTE_PATH = r"genglad\patchable\gfx\gdconstanteoriginal.cpp.gladndfbin"

_META_NAME = "project.json"

# Free-text mod metadata lives beside project.json in the project ROOT (NOT inside the Data/PC
# sub-folder with the .dat files).  Layout: the FIRST line is the author, the LAST line is the
# version (e.g. "v1.0.0"), and every line in between is the description.  This is the same file the
# Convert tab reads so a mod folder carries its own author/version/description.
DESCRIPTION_NAME = "description.txt"


def parse_description(text):
    """Parse a description.txt body into ``{"author", "description", "version"}``.

    The first line is the author, the last line is the version, and everything between them is the
    description.  Returns ``None`` when the body can't be read as that format — empty, or fewer than
    two content lines, so author and version can't be told apart.  Callers treat ``None`` as "no
    usable description.txt" and fall back to their own default (e.g. the mod folder name)."""
    if text is None:
        return None
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    # Drop wholly-blank lines at the very start/end so a trailing newline isn't read as the version.
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) < 2:
        return None
    author  = lines[0].strip()
    version = lines[-1].strip()
    if not author or not version:
        return None
    return {"author": author,
            "description": "\n".join(lines[1:-1]).strip(),
            "version": version}


def format_description(author, description, version):
    """Render author / description / version into the description.txt layout: author on the first
    line, version on the last, the description in between."""
    author      = (author or "").strip()
    version     = (version or "").strip()
    description = (description or "").strip()
    body = author + "\n"
    if description:
        body += description + "\n"
    return body + version + "\n"


def read_folder_description(folder):
    """Read & parse a mod folder's root description.txt.  Returns ``{author, description, version}``
    when present and well-formed, else ``None`` (missing, unreadable, or malformed)."""
    p = Path(folder) / DESCRIPTION_NAME
    if not p.is_file():
        return None
    try:
        return parse_description(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# Names Windows reserves for devices — a folder named exactly one of these (optionally with an
# extension) can't be created, so we prefix it.  Checked case-insensitively against the part before
# the first dot.
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


def safe_folder_name(name: str) -> str:
    """Folder name from a free-text mod name — kept as LITERAL as possible (spaces preserved) but
    always SAFE.

    The mod's display name is taken verbatim (see ModProject.create, which stores the original `name`);
    only the on-disk folder is sanitized here.  We KEEP spaces and ordinary punctuation and remove only
    the characters Windows forbids in a path component — ``< > : " / \\ | ? *`` and control chars.  That
    removal is also what makes the result injection-safe: with every ``/`` ``\\`` and ``:`` gone there is
    no path separator or drive prefix left, so the name can never traverse out of the mods folder (``..``
    on its own collapses to the ``"mod"`` fallback).  The name is only ever used to build a Path and
    mkdir — it is never passed to a shell, eval, or format string — so there is no code-execution path.

    Also: internal whitespace collapses to a single space; leading/trailing whitespace and trailing dots
    or spaces are trimmed (Windows rejects a trailing dot/space); an empty / all-dots / reserved-device
    name is made safe; worst case falls back to ``"mod"``.
    """
    # Drop path-illegal chars + control chars, but NOT the whitespace controls (\x09-\x0d) — those are
    # left for the next step to fold into a single space, so "a\tb" becomes "a b" rather than "ab".
    s = re.sub(r'[<>:"/\\|?*\x00-\x08\x0e-\x1f\x7f]', "", str(name))
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ").strip()   # collapse ws; trim ends + trailing dot/space
    if not s or set(s) <= {"."}:                              # "", ".", ".." → not a usable folder name
        return "mod"
    if s.upper().split(".")[0] in _WIN_RESERVED:              # CON, NUL, COM1, "CON.txt", …
        s = "_" + s
    return s


class ModProject:
    def __init__(self, folder, name: str, version: str, game_root: str, backup_dir: str = ""):
        self.folder = Path(folder)
        self.name = name
        self.version = version or "public"
        self.game_root = game_root or ""
        # where clean (pristine) game files were backed up — the backup mirrors the game root:
        # <backup_dir>/Data/PC/<sub>/<file> for the core dats and <backup_dir>/Maps/PC/<file> for terrain
        self.backup_dir = backup_dir or ""
        # (dat_key, normalized ndf path) -> shared NdfBinary
        self._ndf_cache = {}
        # (dat_key, normalized entry path) -> raw bytes, for non-NDF entries (e.g. .dic dictionaries)
        self._raw_cache = {}
        # (dat_key, normalized entry path) -> the original (un-normalized) entry path, so a NEWLY
        # ADDED entry is stored in the archive with its real path/casing (not the lookup-normalized key)
        self._raw_origpath = {}
        # set of (dat_key, normalized ndf path) with unsaved edits (changes since loading)
        self._dirty = set()
        # dat_keys whose working copy has been grabbed into the mod folder
        self._files = set()

    # ── construction ──────────────────────────────────────────────────────────

    @staticmethod
    def _norm(p: str) -> str:
        return p.replace("\\", "/").lower()

    @classmethod
    def create(cls, base_dir, name: str, version: str, game_root: str,
               backup_dir: str = "") -> "ModProject":
        folder = Path(base_dir) / safe_folder_name(name)
        if folder.exists() and any(folder.iterdir()):
            raise FileExistsError(f"A mod folder named '{folder.name}' already exists and is not empty.")
        folder.mkdir(parents=True, exist_ok=True)
        proj = cls(folder, name, version, game_root, backup_dir)
        proj._write_meta()
        return proj

    @classmethod
    def load(cls, folder, game_root: str, backup_dir: str = "") -> "ModProject":
        folder = Path(folder)
        meta = {}
        mp = folder / _META_NAME
        if mp.is_file():
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        proj = cls(folder, meta.get("name", folder.name),
                   meta.get("version", "public"), game_root, backup_dir)
        # any working .dat already saved into the project counts as grabbed: the core dats under
        # Data/PC/<sub>/ and any terrain dats under Maps/PC/.
        for dk in DAT_FILES:
            if proj.project_dat_path(dk).is_file():
                proj._files.add(dk)
        mp = folder / "Maps" / "PC"
        if mp.is_dir():
            for p in mp.glob("*.dat"):
                proj._files.add(_TERRAIN_PREFIX + p.name)
        return proj

    @classmethod
    def is_project_folder(cls, folder) -> bool:
        return (Path(folder) / _META_NAME).is_file()

    def _write_meta(self):
        meta = {
            "name": self.name,
            "version": self.version,
            "tool": "RUSE ModManager — Mod Editor",
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            # the game files this mod has grabbed to edit (core + terrain dats)
            "files": sorted(self._fname(dk) for dk in self._files),
        }
        (self.folder / _META_NAME).write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # ── description.txt (author / version / description) ─────────────────────────

    def description_path(self) -> Path:
        """Path to the project's description.txt — in the project ROOT (beside project.json), NOT
        inside the Data/PC sub-folder with the .dat files."""
        return self.folder / DESCRIPTION_NAME

    def read_description(self) -> dict:
        """Return ``{author, description, version}`` from the project's description.txt, or empty
        strings when it's missing or unparseable."""
        return read_folder_description(self.folder) or {
            "author": "", "description": "", "version": ""}

    def write_description(self, author: str, description: str, version: str):
        """Write author / description / version to the project's description.txt."""
        self.description_path().write_text(
            format_description(author, description, version), encoding="utf-8")

    # ── paths ───────────────────────────────────────────────────────────────────

    def _sub(self) -> str:
        """Data/PC sub-folder for this project's version (public = 190852)."""
        return _VERSION_SUB.get(self.version, "190852")

    @staticmethod
    def is_terrain_key(dat_key: str) -> bool:
        return dat_key.startswith(_TERRAIN_PREFIX)

    def _rel_parts(self, dat_key: str) -> tuple:
        """Path components, relative to the GAME ROOT, for a dat_key.  The six core dats live at
        Data/PC/<sub>/<file>; a terrain dat ("terrain/<file>") lives at Maps/PC/<file> (shared across
        versions).  Both the backup and the mod project mirror this game-root layout."""
        if self.is_terrain_key(dat_key):
            return ("Maps", "PC", dat_key[len(_TERRAIN_PREFIX):])
        return ("Data", "PC", self._sub(), DAT_FILES[dat_key])

    def _fname(self, dat_key: str) -> str:
        """The on-disk filename for a dat_key (works for both core and terrain keys)."""
        return self._rel_parts(dat_key)[-1]

    def project_dat_path(self, dat_key: str) -> Path:
        # Mirror the game ROOT inside the project: Data/PC/<sub>/<dat> for the core dats, Maps/PC/<dat>
        # for terrain dats.  The converter strips the leading Data/ so rmod paths come out as PC/<sub>/…
        return self.folder.joinpath(*self._rel_parts(dat_key))

    def game_dat_path(self, dat_key: str) -> Path:
        p = Path(self.game_root).joinpath(*self._rel_parts(dat_key))
        if p.is_file():
            return p
        # fallback: search anywhere under the relevant top folder (the version sub-dir may differ)
        top = "Maps" if self.is_terrain_key(dat_key) else "Data"
        base = Path(self.game_root) / top / "PC"
        fname = self._fname(dat_key)
        if base.is_dir():
            for c in base.rglob(fname):
                return c
        return p

    def clean_source(self, dat_key: str) -> Path:
        """Pristine source for a file.  When a backup is configured it is AUTHORITATIVE: we read from
        the backup and NEVER fall back to the live game files — those may already be modded, which
        would silently poison every edit.  The backup mirrors the game root, so the core dats are at
        <backup>/Data/PC/<sub>/<file> and the terrain dats at <backup>/Maps/PC/<file>.  If the file
        isn't in the backup, the returned path won't exist and callers raise a "create a backup"
        error.  Only with no backup configured at all do we use the live game file as a last resort."""
        if self.backup_dir:
            return Path(self.backup_dir).joinpath(*self._rel_parts(dat_key))
        return self.game_dat_path(dat_key)

    def read_source(self, dat_key: str) -> Path:
        """Where to LOAD a file from: the mod folder's own .dat if it exists (it carries this
        mod's prior edits), otherwise the clean backup.  Reading copies nothing into the mod
        folder — the mod-folder .dat is only written when changes are saved."""
        pj = self.project_dat_path(dat_key)
        if pj.is_file():
            return pj
        return self.clean_source(dat_key)

    def terrain_dat_keys(self) -> list:
        """Sorted "terrain/<file>" dat_keys for every Maps/PC terrain dat available to this mod.  The
        full set is discovered from the first source that has a Maps/PC folder — the clean backup, then
        the live game, then the mod folder (which only holds the terrain dats this mod has saved)."""
        roots = []
        if self.backup_dir:
            roots.append(Path(self.backup_dir))
        if self.game_root:
            roots.append(Path(self.game_root))
        roots.append(self.folder)
        for root in roots:
            mp = root / "Maps" / "PC"
            if mp.is_dir():
                names = sorted((p.name for p in mp.glob("*.dat")), key=str.lower)
                if names:
                    return [_TERRAIN_PREFIX + n for n in names]
        return []

    # ── shared NDF access ─────────────────────────────────────────────────────

    def get_ndf(self, dat_key: str, ndf_path: str):
        """Return the shared, cached NdfBinary for one entry.  Edits to the
        returned object are persisted by save_all() once mark_dirty() is called."""
        key = (dat_key, self._norm(ndf_path))
        if key in self._ndf_cache:
            return self._ndf_cache[key]
        src = self.read_source(dat_key)
        if not Path(src).is_file():
            raise FileNotFoundError(
                f"Cannot load '{self._fname(dat_key)}' — it isn't in the mod folder and no clean "
                f"copy was found in the backup or game. Create a backup in the Mod Manager tab, "
                f"or set the Game Root in Settings.")
        arc = _edata.open_dat(str(src))
        raw = arc.get(ndf_path)
        if raw is None:
            raise KeyError(f"{ndf_path} not found in {Path(src).name}")
        ndf = _ndfbin.read(raw)
        self._ndf_cache[key] = ndf
        return ndf

    def everything(self):
        return self.get_ndf("gameplay", EVERYTHING_PATH)

    def gdconstante(self):
        return self.get_ndf("gameplay", GDCONSTANTE_PATH)

    # ── raw (non-NDF) entry access (e.g. .dic localization dictionaries) ──────────

    def get_raw(self, dat_key: str, entry_path: str) -> bytes:
        """Return the cached raw bytes of a non-NDF archive entry (read from the mod folder's
        own .dat if present, else the clean backup).  Edits go through set_raw()."""
        key = (dat_key, self._norm(entry_path))
        if key in self._raw_cache:
            return self._raw_cache[key]
        src = self.read_source(dat_key)
        if not Path(src).is_file():
            raise FileNotFoundError(
                f"Cannot load '{self._fname(dat_key)}' — not in the mod folder and no clean copy "
                f"found. Create a backup in the Mod Manager tab, or set the Game Root in Settings.")
        raw = _edata.open_dat(str(src)).get(entry_path)
        if raw is None:
            raise KeyError(f"{entry_path} not found in {Path(src).name}")
        self._raw_cache[key] = raw
        return raw

    def set_raw(self, dat_key: str, entry_path: str, data: bytes):
        """Stage new bytes for ANY entry — REPLACING an existing one or ADDING a brand-new file.
        save_all() decides replace-vs-add by whether the entry already exists in the .dat.  If the
        entry was previously loaded as a parsed NDF, drop that cached object so the raw bytes win."""
        key = (dat_key, self._norm(entry_path))
        self._raw_cache[key] = data
        self._raw_origpath[key] = entry_path
        self._ndf_cache.pop(key, None)
        self._dirty.add(key)

    def entry_paths(self, dat_key: str, suffix: str = "") -> list:
        """List archive entry paths in a dat (from the mod folder copy or clean source), optionally
        filtered to those ending with `suffix` (case-insensitive). [] if the dat isn't available."""
        src = self.read_source(dat_key)
        if not Path(src).is_file():
            return []
        paths = _edata.open_dat(str(src)).list()
        s = suffix.lower()
        return [p for p in paths if p.lower().endswith(s)] if s else list(paths)

    # ── dirty tracking ──────────────────────────────────────────────────────────

    def mark_dirty(self, dat_key: str, ndf_path: str):
        self._dirty.add((dat_key, self._norm(ndf_path)))

    def is_dirty(self) -> bool:
        return bool(self._dirty)

    def dirty_count(self) -> int:
        return len(self._dirty)

    # ── save / deploy ─────────────────────────────────────────────────────────

    def _ensure_project_dat(self, dat_key: str) -> Path:
        """Make sure the mod folder has its own .dat to write into.  The first time changes for
        a file are saved, the new mod-folder .dat is created by copying the clean source it was
        loaded from (backup, falling back to the live game file)."""
        pj = self.project_dat_path(dat_key)
        if not pj.is_file():
            src = self.clean_source(dat_key)
            if not Path(src).is_file():
                raise FileNotFoundError(
                    f"Cannot find a clean '{self._fname(dat_key)}' to base the mod on (checked "
                    f"backup and game). Create a backup in the Mod Manager tab, or set the Game "
                    f"Root in Settings.")
            pj.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, pj)
        self._files.add(dat_key)
        return pj

    def save_all(self) -> list:
        """Write every dirty NDF entry into the project's .dat(s).  Returns the
        list of project .dat paths that were written."""
        if not self._dirty:
            return []
        by_dat = {}
        for (dk, npath) in self._dirty:
            by_dat.setdefault(dk, []).append(npath)
        written = []
        for dk, paths in by_dat.items():
            pj = self._ensure_project_dat(dk)
            arc = _edata.open_dat(str(pj))
            to_replace, to_add = {}, {}
            for npath in paths:
                ndf = self._ndf_cache.get((dk, npath))
                if ndf is not None:
                    data = _ndfbin.write(ndf, compress=ndf.is_compressed)
                elif (dk, npath) in self._raw_cache:
                    data = self._raw_cache[(dk, npath)]
                else:
                    continue
                # Replace if the entry already exists in the .dat, otherwise ADD it as a new file.
                if arc.get(npath) is not None:
                    to_replace[npath] = data
                else:
                    # New entry: store it under its real path (backslashes, original casing) so the
                    # game finds it, not the lookup-normalized cache key.
                    orig = self._raw_origpath.get((dk, npath), npath)
                    to_add[orig.replace("/", "\\")] = data
            if not to_replace and not to_add:
                continue
            # One rebuild for every edited/added entry in this dat (structure-preserving, v1).
            # edata is case/slash-insensitive on the lookup key.
            try:
                arc.batch_update(to_replace, to_add)
            except NotImplementedError:                 # v2 archive: replace one at a time (no add)
                for npath, data in to_replace.items():
                    arc.replace(npath, data)
            written.append(str(pj))
        self._dirty.clear()
        self._write_meta()
        return written

    def saved_dats(self) -> list:
        """Project .dat files that currently exist on disk (deployable): the core dats under
        Data/PC/<sub>/ plus any terrain dats saved under Maps/PC/."""
        out = [self.project_dat_path(dk) for dk in DAT_FILES
               if self.project_dat_path(dk).is_file()]
        mp = self.folder / "Maps" / "PC"
        if mp.is_dir():
            out += sorted(mp.glob("*.dat"), key=lambda p: p.name.lower())
        return out

    def _saved_dat_keys(self) -> list:
        """dat_keys whose working copy exists on disk in the project (core + terrain)."""
        keys = [dk for dk in DAT_FILES if self.project_dat_path(dk).is_file()]
        mp = self.folder / "Maps" / "PC"
        if mp.is_dir():
            keys += [_TERRAIN_PREFIX + p.name for p in sorted(mp.glob("*.dat"))]
        return keys

    def deployed_dat_rels(self) -> list:
        """Game-root-relative paths (forward slashes) of the dats a deploy() overlays onto the game —
        e.g. 'Data/PC/190852/ZZ_GladPatchableWin.dat', 'Maps/PC/DataMapX_v09.dat'.  Lets the Mod Manager
        track exactly what a project deploy modified (shared deploy/restore state)."""
        return ["/".join(self._rel_parts(dk)) for dk in self._saved_dat_keys()]

    def deploy(self, backup_dir) -> tuple:
        """Copy each saved project .dat (core dats + any terrain dats) over the matching live game file,
        backing the original up first.  Returns (copied, backups)."""
        copied, backups = [], []
        backup_dir = Path(backup_dir)
        for dk in self._saved_dat_keys():
            pj = self.project_dat_path(dk)
            if not pj.is_file():
                continue
            dest = self.game_dat_path(dk)
            if dest.is_file():
                backup_dir.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                bak = backup_dir / f"{dest.name}.{stamp}.bak"
                shutil.copy2(dest, bak)
                backups.append(str(bak))
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pj, dest)
            copied.append(str(dest))
        return copied, backups

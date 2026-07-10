"""
.rmod file format definition and validation.

A .rmod file is a JSON document that describes one RUSE mod as a set of
surgical patches to NDF binary files inside .dat archives.  Instead of
distributing a full replacement .dat file, a .rmod only contains the changes —
this lets multiple incompatible mods coexist.

File extension: .rmod
Encoding: UTF-8 JSON

Schema (v1)
-----------

{
  "$schema": "ruse-mod/v1",
  "id":          "unique-kebab-case-id",       // required, no spaces
  "name":        "Display Name",               // required
  "version":     "1.0.0",                      // required
  "author":      "Author Name",                // optional
  "description": "What this mod does",         // optional
  "game_version": "1.4",                       // optional — target RUSE version
  "patches": [                                 // required, list of patch groups
    {
      "dat":  "Data/PC/99/ZZ_GladPatchableWin.dat", // required — path relative to the GAME ROOT
                                               // (Data/PC/<sub>/<dat> or Maps/PC/<dat>); legacy
                                               // Data-relative "PC/..." paths are still accepted
      "ndf":  "genglad/patchable/clustergfx/everything.cpp.gladndfbin",  // required — path inside .dat
      "changes": [                             // required, list of changes
        {
          "action": "patch",                   // "patch" (default) | "create" | "delete"
          "table":  "TUniteAuSolDescriptor",   //   | "delete_props"
          "match":  {                          // match conditions (property → value)
            "ClassNameForDebug": "Unit_Stug_III_B"
          },
          "set": {                             // properties to change (patch/create)
            "SeuilMort": {
              "type":  "Float32",              // NDF type name (see ndfbin.T)
              "value": 600.0
            },
            "ProductionTime": {
              "type":  "Int32",
              "value": 8
            }
          },
          "props": ["PropA", "PropB"]          // properties to remove (delete_props only)
        },
        {
          "action": "patch",
          "table": "TTunableConstante",
          "match": {},
          "set": {
            "QteDeviseInitiale": {"type": "Int32", "value": 500}
          }
        }
      ]
    }
  ]
}

Localization patches (loc_patches) — surgical .dic edits
--------------------------------------------------------
Unit/map display NAMES live in .dic localization dictionaries (TRA format) inside ZZ_Win.dat, NOT in
the NDF.  A descriptor's NameInMenuToken is an 8-byte LocHash that is the .dic key; the string lives in
baseunite.dic (one per language).  A loc patch changes just those keys, so a rename is a few bytes and
multiple name-mods compose (instead of each clobbering the whole dictionary):

  "loc_patches": [
    {
      "dat": "PC/190852/ZZ_Win.dat",
      "dic": "genlocalisation/ww2/localisation/translations/us/baseunite.dic",
      "entries": [
        { "key": "42209413f6951800", "value": "ATOMIC PERSHING" },   // change an existing key
        { "key": "0011223344556677", "value": "New Card", "add": true } // add a new key
      ]
    }
  ]
The applier set_entry's existing keys and add_entry's absent ones (the "add" flag is a hint; existence
is re-checked at apply time).  The converter emits these automatically when it diffs a changed .dic.

SDB layer patches (sdb_patches) — composable AI-terrain layers
--------------------------------------------------------------
The AI-terrain spatial DB (forest cover, sight-block, ...) lives in mapinfo.win buffer4 as a quadtree.
A mod that paints e.g. forest changes ONE layer (one bit of each cell byte).  An sdb patch ships each
changed layer as a full-grid bitmask (one bit/cell, zlib-compressed), so layer edits compose (two mods
editing different layers don't conflict) and stay small.  Only emitted when nothing but the SDB changed
(road graph / other buffers → raw file patch).

  "sdb_patches": [
    { "dat": "PC/190852/DataMap_Win.dat",
      "win": "datasmap/<map>/mapinfo.win",
      "grid_size": 1024,                 // R; the SDB grid is R×R finest cells
      "layers": [ { "bit": 8, "mask": "<base64(zlib(R*R-bit LSB-first bitmask))>" } ] }
  ]
The applier decodes buffer4 to a per-cell grid, sets/clears each layer's bit per the mask (other layers
untouched), re-encodes the quadtree, and recomputes both SDB and mapinfo.win md5s.

Whole-file content (KDT meshes, scenarios, anything not handled above) ships via "file_patches" (a raw,
lossless full-file replacement) — e.g. a copied .kdt for a new map is packaged whole, not edited surgically.

Supported actions
-----------------
  "patch"        — find matching instance(s), update their properties.  A property the target class
                   doesn't have yet is ADDED (the applier appends it to the NDF property table), so a
                   mod can introduce a brand-new property (e.g. a new gdconstante) for compatibility,
                   not only change existing ones.
  "delete"       — find matching instance(s), remove them from the table
  "delete_props" — find matching instance(s), remove specific named properties
                   (requires "props": ["PropName", ...] instead of "set")
  "create"       — add a new instance to the table (advanced; "set" defines initial props)

Patch group flags
-----------------
  "create_if_missing": true  — if the ndf file does not exist in the .dat, create it
                               from scratch before applying changes (useful for new datasets)

Supported type names (case-insensitive)
----------------------------------------
  Bool, Int8, Int16, UInt16, Int32, UInt32, Long, Float32, Float64,
  StringRef, PathRef, WideStr / WideString, Vector3, Color32, Color128,
  TripleInt, Int2, Float2

Match conditions
----------------
  Each key-value pair in "match" is a property name and its expected string
  representation.  All conditions must match (logical AND).
  Use "ClassNameForDebug" as the primary stable identifier.
  If "match" is omitted the change applies to ALL instances of the table.

Notes
-----
  - dat paths use forward or back slashes; normalised internally.
  - ndf paths inside the .dat also use forward or back slashes.
  - "value" for StringRef/PathRef should be the string content, NOT an index.
    The engine handles the string table insertion.
  - For Vector3, Color128, Int2, Float2 pass "value" as a JSON array, e.g. [1.0, 0.0, 0.0].
"""

import base64
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class ModValueDef:
    """A single property value in a 'set' block."""
    type_name: str
    value: Any


@dataclass
class ModChange:
    """One change entry inside a patch group."""
    action: str                          # "patch" | "create" | "delete" | "delete_props"
    table: str                           # NDF class name
    match: Dict[str, str]                # property→value match conditions
    set_props: Dict[str, ModValueDef]    # property→value for patch/create
    del_props: List[str] = field(default_factory=list)  # property names for delete_props
    local_id: Optional[str] = None       # create only: label for $ref resolution
    is_top_object: bool = False          # create only: add instance to NDF top_objects list


@dataclass
class ModPatchGroup:
    """Targets one ndfbin file inside one .dat archive."""
    dat: str                   # relative to game Data/  e.g. "PC/99/ZZ_GladPatchableWin.dat"
    ndf: str                   # path inside .dat         e.g. "genglad/.../everything.cpp.gladndfbin"
    changes: List[ModChange]
    create_if_missing: bool = False  # create a blank NDF if the file is not in the .dat
    # Maps game version → translation dict for _index match and ObjRef translation.
    # Keys: str(og_index)→int updated_index, "_instance_offset"→int, "_class_map"→{str→int}
    index_map: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Surgical IMPR/EXPR (import/export tree) edits, expressed as portable full paths
    # ("$/IA/Cluster/PackMesh_All") so they migrate across builds unchanged. The applier
    # merges these onto the live NDF's import/export tables (see ndfbin.add_import_path).
    import_add: List[str] = field(default_factory=list)
    import_remove: List[str] = field(default_factory=list)
    export_add: List[str] = field(default_factory=list)
    export_remove: List[str] = field(default_factory=list)


@dataclass
class ModFilePatch:
    """Replaces an entire file inside a .dat archive with new binary content."""
    dat: str    # relative to game Data/
    path: str   # path inside .dat (forward slashes)
    data: bytes # replacement file contents


@dataclass
class ModFileGroup:
    """Groups raw file replacements for one .dat archive."""
    dat: str                  # relative to game Data/
    files: List[ModFilePatch]


@dataclass
class ModLocEntry:
    """One localization-dictionary (.dic) entry change."""
    key: str             # 8-byte LocHash as 16-char hex (e.g. "42209413f6951800")
    value: str           # the new UTF-16 string
    add: bool = False    # True = add a brand-new key (vs change an existing one)


@dataclass
class ModSdbLayer:
    """One AI-terrain layer (a single bit) of an SDB, as a full-grid bitmask."""
    bit: int          # layer bit (0x08 conceal/forest, 0x04 sight-block, ...; not 0x01 leaf flag)
    mask: bytes       # ceil(R*R/8) bytes, LSB-first: cell i set ⇒ that cell has this layer


@dataclass
class ModSdbGroup:
    """Layer-granular edits to the SDB (mapinfo.win buffer4) — the pre-baked AI-terrain spatial DB
    the game point-queries (forest cover, sight-block, ...).  Ships whole CHANGED LAYERS (one bitmask
    each) rather than per-cell deltas or the whole mapinfo.win, so layer edits compose and stay small.
    Only emitted when nothing but the SDB buffer changed (roads/other buffers → raw file patch)."""
    dat: str                       # relative to game Data/
    win: str                       # path to mapinfo.win inside the .dat (forward slashes)
    grid_size: int                 # R (the SDB grid is R×R finest cells)
    layers: List[ModSdbLayer]


@dataclass
class ModLocGroup:
    """Surgical edits to one .dic localization dictionary inside a .dat archive.

    Unit/map display names live in .dic dictionaries (e.g. baseunite.dic) keyed by an 8-byte
    LocHash (the descriptor's NameInMenuToken).  A loc patch carries ONLY the changed entries, so
    renaming a unit is a few bytes instead of a full-file replacement, and multiple name-mods compose
    (each only touches its own keys).
    """
    dat: str                       # relative to game Data/, e.g. "PC/190852/ZZ_Win.dat"
    dic: str                       # path inside the .dat (forward slashes)
    entries: List[ModLocEntry]


@dataclass
class ModScenarioGroup:
    """Surgical edits to the placement NDF embedded inside a .scenario file.

    A .scenario = binary zone geometry + an embedded NDF (depots / HQ spawns / labels / zone objects).
    When ONLY that embedded NDF changed (the zone geometry stays byte-identical), we ship the NDF diff —
    exactly like a normal NDF patch group, but targeted at the scenario's inner NDF — so scenario edits
    stay small and multiple mods editing different placements in the same scenario compose.  Zone
    geometry edits aren't surgically diffable and fall back to a raw file patch."""
    dat: str                       # game-root-relative, e.g. Data/PC/190852/DataMap_Win.dat
    scenario: str                  # path to the .scenario inside the .dat (forward slashes)
    changes: List[ModChange]


@dataclass
class RuseMod:
    """Parsed representation of a .rmod file."""
    schema: str
    id: str
    name: str
    version: str
    author: str
    description: str
    game_version: str
    patches: List[ModPatchGroup]
    file_patches: List[ModFileGroup] = field(default_factory=list)
    loc_patches: List[ModLocGroup] = field(default_factory=list)
    sdb_patches: List[ModSdbGroup] = field(default_factory=list)
    scenario_patches: List[ModScenarioGroup] = field(default_factory=list)

    def summary(self) -> str:
        total_ndf = sum(len(p.changes) for p in self.patches)
        total_files = sum(len(g.files) for g in self.file_patches)
        total_loc = sum(len(g.entries) for g in self.loc_patches)
        total_sdb = sum(len(g.layers) for g in self.sdb_patches)
        total_scn = sum(len(g.changes) for g in self.scenario_patches)
        parts = []
        if total_ndf:
            parts.append(f"{total_ndf} NDF change(s) across {len(self.patches)} ndf file(s)")
        if total_scn:
            parts.append(f"{total_scn} scenario NDF change(s)")
        if total_loc:
            parts.append(f"{total_loc} localization entry change(s)")
        if total_sdb:
            parts.append(f"{total_sdb} SDB layer(s)")
        if total_files:
            parts.append(f"{total_files} raw file replacement(s)")
        return (f"{self.name} v{self.version} by {self.author or 'unknown'} — "
                + (", ".join(parts) if parts else "0 changes"))


# ── Parser ────────────────────────────────────────────────────────────────────

class ModFormatError(ValueError):
    pass


def _require(obj: dict, key: str, context: str) -> Any:
    if key not in obj:
        raise ModFormatError(f"Missing required field '{key}' in {context}")
    return obj[key]


def _parse_value_def(raw: Any, prop_name: str) -> ModValueDef:
    if isinstance(raw, dict):
        # "$ref" shorthand: {"$ref": "local_id"}
        if "$ref" in raw:
            return ModValueDef(type_name="$ref", value=str(raw["$ref"]))
        type_name = _require(raw, "type", f"property '{prop_name}'")
        # "$ref" as type field: {"type": "$ref", "value": "local_id"}
        if str(type_name) == "$ref":
            value = _require(raw, "value", f"property '{prop_name}'")
            return ModValueDef(type_name="$ref", value=str(value))
        value = _require(raw, "value", f"property '{prop_name}'")
    else:
        # Shorthand: just a value — caller must supply type or we infer
        raise ModFormatError(
            f"Property '{prop_name}' must be an object with 'type' and 'value' keys. "
            f"Got: {raw!r}"
        )
    return ModValueDef(type_name=str(type_name), value=value)


def _parse_change(raw: dict, idx: int) -> ModChange:
    ctx = f"changes[{idx}]"
    action = raw.get("action", "patch").lower()
    valid_actions = ("patch", "create", "delete", "delete_props")
    if action not in valid_actions:
        raise ModFormatError(f"{ctx}: unknown action {action!r}. Expected {'/'.join(valid_actions)}")
    table = _require(raw, "table", ctx)
    match = raw.get("match", {})
    if not isinstance(match, dict):
        raise ModFormatError(f"{ctx}: 'match' must be a dict of {{property: value}}")
    set_raw = raw.get("set", {})
    if not isinstance(set_raw, dict):
        raise ModFormatError(f"{ctx}: 'set' must be a dict of {{property: {{type,value}}}}")
    set_props = {}
    for prop_name, val_raw in set_raw.items():
        set_props[prop_name] = _parse_value_def(val_raw, prop_name)
    del_props_raw = raw.get("props", [])
    if not isinstance(del_props_raw, list):
        raise ModFormatError(f"{ctx}: 'props' must be a list of property name strings")
    del_props = [str(p) for p in del_props_raw]
    if action == "delete_props" and not del_props:
        raise ModFormatError(f"{ctx}: 'delete_props' action requires a non-empty 'props' list")
    local_id = raw.get("local_id") or None
    is_top_object = bool(raw.get("top_object", False))
    # Property matches are string values ({prop: "val"}, {"_index": "N"}); but a durable ANCHOR match
    # ({"anchor": {root, steps}}) is a nested STRUCTURE — preserve dict/list values verbatim, only
    # stringify scalars, so resolve_anchor gets a dict, not its repr().
    match_str = {k: (v if isinstance(v, (dict, list)) else str(v)) for k, v in match.items()}
    return ModChange(action=action, table=table, match=match_str,
                     set_props=set_props, del_props=del_props, local_id=local_id,
                     is_top_object=is_top_object)


def _parse_patch_group(raw: dict, idx: int) -> ModPatchGroup:
    ctx = f"patches[{idx}]"
    dat = _require(raw, "dat", ctx).replace("\\", "/")
    ndf = _require(raw, "ndf", ctx).replace("\\", "/")
    changes_raw = _require(raw, "changes", ctx)
    if not isinstance(changes_raw, list):
        raise ModFormatError(f"{ctx}: 'changes' must be a list")
    changes = [_parse_change(c, i) for i, c in enumerate(changes_raw)]
    create_if_missing = bool(raw.get("create_if_missing", False))
    index_map_raw = raw.get("index_map", {})
    index_map: Dict[str, Dict[str, Any]] = {}
    # A hostile/corrupt rmod may give a non-dict here, or non-integer index values — int() would raise
    # ValueError/TypeError and .items() AttributeError, neither of which is a ModFormatError.  Normalize to
    # ModFormatError so the loader reports a clean "bad rmod" instead of an opaque crash.
    try:
        for ver, mapping in index_map_raw.items():
            ver_map: Dict[str, Any] = {}
            for k, v in mapping.items():
                if isinstance(v, dict):
                    ver_map[str(k)] = {str(ck): int(cv) for ck, cv in v.items()}
                elif isinstance(v, list):
                    # e.g. _instance_segments: [{"from": 0, "to": 46454, "offset": 0}, ...]
                    ver_map[str(k)] = v
                else:
                    ver_map[str(k)] = int(v)
            index_map[ver] = ver_map
    except (AttributeError, TypeError, ValueError) as e:
        raise ModFormatError(f"malformed index_map in patch group: {e}")
    _strlist = lambda key: [str(s) for s in raw.get(key, [])]
    return ModPatchGroup(dat=dat, ndf=ndf, changes=changes,
                         create_if_missing=create_if_missing, index_map=index_map,
                         import_add=_strlist("import_add"), import_remove=_strlist("import_remove"),
                         export_add=_strlist("export_add"), export_remove=_strlist("export_remove"))


def _parse_file_group(raw: dict, idx: int) -> ModFileGroup:
    ctx = f"file_patches[{idx}]"
    dat = _require(raw, "dat", ctx).replace("\\", "/")
    files_raw = _require(raw, "files", ctx)
    if not isinstance(files_raw, list):
        raise ModFormatError(f"{ctx}: 'files' must be a list")
    files = []
    for j, f in enumerate(files_raw):
        fctx = f"{ctx}.files[{j}]"
        path = _require(f, "path", fctx).replace("\\", "/")
        data_b64 = _require(f, "data", fctx)
        try:
            data = base64.b64decode(data_b64)
        except Exception as e:
            raise ModFormatError(f"{fctx}: invalid base64 in 'data': {e}")
        files.append(ModFilePatch(dat=dat, path=path, data=data))
    return ModFileGroup(dat=dat, files=files)


def _parse_loc_group(raw: dict, idx: int) -> ModLocGroup:
    ctx = f"loc_patches[{idx}]"
    dat = _require(raw, "dat", ctx).replace("\\", "/")
    dic = _require(raw, "dic", ctx).replace("\\", "/")
    entries_raw = _require(raw, "entries", ctx)
    if not isinstance(entries_raw, list):
        raise ModFormatError(f"{ctx}: 'entries' must be a list")
    entries = []
    for j, e in enumerate(entries_raw):
        ectx = f"{ctx}.entries[{j}]"
        key = str(_require(e, "key", ectx)).strip().lower()
        if len(key) != 16 or any(c not in "0123456789abcdef" for c in key):
            raise ModFormatError(f"{ectx}: 'key' must be a 16-char hex LocHash, got {key!r}")
        value = _require(e, "value", ectx)
        entries.append(ModLocEntry(key=key, value=str(value), add=bool(e.get("add", False))))
    return ModLocGroup(dat=dat, dic=dic, entries=entries)


def _parse_sdb_group(raw: dict, idx: int) -> ModSdbGroup:
    ctx = f"sdb_patches[{idx}]"
    dat = _require(raw, "dat", ctx).replace("\\", "/")
    win = _require(raw, "win", ctx).replace("\\", "/")
    try:
        grid_size = int(_require(raw, "grid_size", ctx))
    except (TypeError, ValueError) as e:
        raise ModFormatError(f"{ctx}: 'grid_size' must be an integer: {e}")
    layers_raw = _require(raw, "layers", ctx)
    if not isinstance(layers_raw, list):
        raise ModFormatError(f"{ctx}: 'layers' must be a list")
    layers = []
    for j, ly in enumerate(layers_raw):
        lctx = f"{ctx}.layers[{j}]"
        if not isinstance(ly, dict):
            raise ModFormatError(f"{lctx}: layer must be an object")
        try:
            bit = int(_require(ly, "bit", lctx))
        except (TypeError, ValueError) as e:
            raise ModFormatError(f"{lctx}: 'bit' must be an integer: {e}")
        try:
            mask = base64.b64decode(_require(ly, "mask", lctx))
        except Exception as e:
            raise ModFormatError(f"{lctx}: invalid base64 in 'mask': {e}")
        layers.append(ModSdbLayer(bit=bit, mask=mask))
    return ModSdbGroup(dat=dat, win=win, grid_size=grid_size, layers=layers)


def _parse_scenario_group(raw: dict, idx: int) -> ModScenarioGroup:
    ctx = f"scenario_patches[{idx}]"
    dat = _require(raw, "dat", ctx).replace("\\", "/")
    scenario = _require(raw, "scenario", ctx).replace("\\", "/")
    changes_raw = _require(raw, "changes", ctx)
    if not isinstance(changes_raw, list):
        raise ModFormatError(f"{ctx}: 'changes' must be a list")
    changes = [_parse_change(c, i) for i, c in enumerate(changes_raw)]
    return ModScenarioGroup(dat=dat, scenario=scenario, changes=changes)


def parse(data: str) -> RuseMod:
    """Parse a JSON string into a RuseMod object."""
    try:
        obj = json.loads(data)
    except json.JSONDecodeError as e:
        raise ModFormatError(f"Invalid JSON: {e}")

    if not isinstance(obj, dict):
        raise ModFormatError("Root must be a JSON object")

    schema = obj.get("$schema", "ruse-mod/v1")
    mod_id = _require(obj, "id", "root")
    # Allow '.' (real shipped ids like '0.5x-airfield-capacity-v1') but keep the id
    # filesystem-safe: no path separators and no '..' traversal (see no-breakout rule).
    if not re.match(r"^[a-zA-Z0-9_.\-]+$", mod_id) or ".." in mod_id:
        raise ModFormatError(f"'id' must contain only letters, digits, hyphens, underscores, dots "
                             f"(no '..'). Got: {mod_id!r}")
    name = _require(obj, "name", "root")
    version = _require(obj, "version", "root")
    author = obj.get("author", "")
    description = obj.get("description", "")
    game_version = obj.get("game_version", "")
    patches_raw = obj.get("patches", [])
    if not isinstance(patches_raw, list):
        raise ModFormatError("'patches' must be a list")
    patches = [_parse_patch_group(p, i) for i, p in enumerate(patches_raw)]
    file_patches_raw = obj.get("file_patches", [])
    if not isinstance(file_patches_raw, list):
        raise ModFormatError("'file_patches' must be a list")
    file_patches = [_parse_file_group(g, i) for i, g in enumerate(file_patches_raw)]
    loc_patches_raw = obj.get("loc_patches", [])
    if not isinstance(loc_patches_raw, list):
        raise ModFormatError("'loc_patches' must be a list")
    loc_patches = [_parse_loc_group(g, i) for i, g in enumerate(loc_patches_raw)]
    sdb_patches_raw = obj.get("sdb_patches", [])
    if not isinstance(sdb_patches_raw, list):
        raise ModFormatError("'sdb_patches' must be a list")
    sdb_patches = [_parse_sdb_group(g, i) for i, g in enumerate(sdb_patches_raw)]
    scenario_patches_raw = obj.get("scenario_patches", [])
    if not isinstance(scenario_patches_raw, list):
        raise ModFormatError("'scenario_patches' must be a list")
    scenario_patches = [_parse_scenario_group(g, i) for i, g in enumerate(scenario_patches_raw)]

    return RuseMod(
        schema=schema,
        id=mod_id,
        name=name,
        version=version,
        author=author,
        description=description,
        game_version=game_version,
        patches=patches,
        file_patches=file_patches,
        loc_patches=loc_patches,
        sdb_patches=sdb_patches,
        scenario_patches=scenario_patches,
    )


def load(path: str) -> RuseMod:
    """Load and parse a .rmod file from disk."""
    with open(path, "r", encoding="utf-8") as f:
        return parse(f.read())


def _change_to_obj(ch: ModChange) -> Dict[str, Any]:
    """Serialise one ModChange back to its JSON object form (shared by patches + scenario_patches)."""
    ch_obj: Dict[str, Any] = {"action": ch.action, "table": ch.table}
    if ch.local_id:
        ch_obj["local_id"] = ch.local_id
    if ch.is_top_object:
        ch_obj["top_object"] = True
    if ch.match:
        ch_obj["match"] = ch.match
    if ch.set_props:
        ch_obj["set"] = {}
        for pname, v in ch.set_props.items():
            if v.type_name == "$ref":
                ch_obj["set"][pname] = {"$ref": v.value}
            else:
                ch_obj["set"][pname] = {"type": v.type_name, "value": v.value}
    if ch.del_props:
        ch_obj["props"] = ch.del_props
    return ch_obj


def dump(mod: RuseMod) -> str:
    """Serialise a RuseMod back to a JSON string."""
    obj: Dict[str, Any] = {
        "$schema": mod.schema,
        "id": mod.id,
        "name": mod.name,
        "version": mod.version,
    }
    if mod.author:
        obj["author"] = mod.author
    if mod.description:
        obj["description"] = mod.description
    if mod.game_version:
        obj["game_version"] = mod.game_version

    obj["patches"] = []
    for pg in mod.patches:
        pg_obj: Dict[str, Any] = {"dat": pg.dat, "ndf": pg.ndf}
        if pg.create_if_missing:
            pg_obj["create_if_missing"] = True
        if pg.index_map:
            pg_obj["index_map"] = pg.index_map
        if pg.import_add:
            pg_obj["import_add"] = pg.import_add
        if pg.import_remove:
            pg_obj["import_remove"] = pg.import_remove
        if pg.export_add:
            pg_obj["export_add"] = pg.export_add
        if pg.export_remove:
            pg_obj["export_remove"] = pg.export_remove
        pg_obj["changes"] = [_change_to_obj(ch) for ch in pg.changes]
        obj["patches"].append(pg_obj)

    if mod.loc_patches:
        obj["loc_patches"] = []
        for lg in mod.loc_patches:
            entries = []
            for e in lg.entries:
                eo: Dict[str, Any] = {"key": e.key, "value": e.value}
                if e.add:
                    eo["add"] = True
                entries.append(eo)
            obj["loc_patches"].append({"dat": lg.dat, "dic": lg.dic, "entries": entries})

    if mod.sdb_patches:
        obj["sdb_patches"] = []
        for sg in mod.sdb_patches:
            layers = [{"bit": ly.bit, "mask": base64.b64encode(ly.mask).decode("ascii")}
                      for ly in sg.layers]
            obj["sdb_patches"].append(
                {"dat": sg.dat, "win": sg.win, "grid_size": sg.grid_size, "layers": layers})

    if mod.scenario_patches:
        obj["scenario_patches"] = []
        for sg in mod.scenario_patches:
            obj["scenario_patches"].append(
                {"dat": sg.dat, "scenario": sg.scenario,
                 "changes": [_change_to_obj(ch) for ch in sg.changes]})

    if mod.file_patches:
        obj["file_patches"] = []
        for fg in mod.file_patches:
            fg_obj: Dict[str, Any] = {
                "dat": fg.dat,
                "files": [
                    {"path": fp.path, "data": base64.b64encode(fp.data).decode("ascii")}
                    for fp in fg.files
                ],
            }
            obj["file_patches"].append(fg_obj)

    return json.dumps(obj, indent=2, ensure_ascii=False)


def save(mod: RuseMod, path: str) -> None:
    """Write a RuseMod to disk as a .rmod file, ATOMICALLY.

    Serialise first, then write a sibling ``.tmp`` (flushed + fsync'd) and os.replace it into place, so an
    interrupted write — app closed / process killed / disk full mid-write — can never leave a truncated,
    unparseable .rmod where a good one used to be.  Matters because callers overwrite SHIPPED mods in place
    (Convert-tab migrate-to-all-versions) and the Mod Editor's scenario save.  Mirrors
    converter._atomic_write_text.  A serialisation failure raises before any file is touched.
    """
    text = dump(mod)                      # may raise — do it before opening any file (original left intact)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)                 # atomic on the same filesystem (also replaces an existing file)


# ── Template helpers ──────────────────────────────────────────────────────────

TEMPLATE = """\
{
  "$schema": "ruse-mod/v1",
  "id": "my-mod",
  "name": "My Mod Name",
  "version": "1.0.0",
  "author": "Your Name",
  "description": "Describe what this mod changes",
  "patches": [
    {
      "dat": "PC/99/ZZ_GladPatchableWin.dat",
      "ndf": "genglad/patchable/clustergfx/everything.cpp.gladndfbin",
      "changes": [
        {
          "action": "patch",
          "table": "TUniteAuSolDescriptor",
          "match": {
            "ClassNameForDebug": "Unit_Stug_III_B"
          },
          "set": {
            "SeuilMort":      { "type": "Float32", "value": 600.0 },
            "ProductionTime": { "type": "Int32",   "value": 8     }
          }
        }
      ]
    }
  ]
}
"""


# ── Known RUSE dat / ndf paths ────────────────────────────────────────────────

KNOWN_DAT_FILES = {
    "patchable": "PC/99/ZZ_GladPatchableWin.dat",
    "notpatchable": "PC/99/ZZ_GladNotPatchableWin.dat",
    "mapdata_1360": "PC/1360/DataMap_Win.dat",
    "common_1360": "PC/1360/Data_Common.dat",
    "ia_1360": "PC/1360/IA_Common.dat",
    "zz_1360": "PC/1360/ZZ_Win.dat",
}

KNOWN_NDF_FILES = {
    # Primary unit/weapon/building/AI database inside PC/99/ZZ_GladPatchableWin.dat
    "everything":            "genglad/patchable/gfx/everything.cpp.gladndfbin",
    # Game-wide scalar constants (TTunableConstante — economy, timings, AI, colours)
    "constants_original":    "genglad/patchable/gfx/gdconstanteoriginal.cpp.gladndfbin",
    "constants_atomic":      "genglad/patchable/gfx/gdconstanteatomic.cpp.gladndfbin",
    # Visibility range table (TVisibilityRange × 50)
    "constants_visibility":  "genglad/patchable/gfx/visibility.cpp.gladndfbin",
    # Globals: multiplayer map list, campaign chapters
    "globals":               "genglad/patchable/misc/globals.cpp.gladndfbin",
    # Map load info (TMapLoadInfo, TUIResourceTexture)
    "mapinfo":               "genglad/patchable/mapinfo.cpp.gladndfbin",
}

COMMON_TABLES = [
    "TUniteAuSolDescriptor",       # ground units          — everything.cpp.gladndfbin
    "TAvionDescriptor",            # aircraft              — everything.cpp.gladndfbin
    "TBatimentDescriptor",         # buildings             — everything.cpp.gladndfbin
    "TAmmunition",                 # ammunition/weapons    — everything.cpp.gladndfbin
    "TWeaponDescriptor",           # weapon configurations — everything.cpp.gladndfbin
    "TIAProfil",                   # AI profiles           — everything.cpp.gladndfbin
    "TTunableConstante",           # game-wide constants   — everythinggdconstanteoriginal.cpp.gladndfbin
    "TVisibilityRange",            # vision ranges         — everythinggdconstantevisibility.cpp.gladndfbin
    "TMultiMapInfo",               # multiplayer map list  — globals.cpp.gladndfbin (deep chain)
    "TChapterMapInfo",             # campaign chapters     — globals.cpp.gladndfbin (deep chain)
    "TMapLoadInfo",                # map load info         — mapinfo.cpp.gladndfbin (deep chain)
]

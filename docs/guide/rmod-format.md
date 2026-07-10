# The `.rmod` File Format

A `.rmod` file is one small text file (written in **JSON**, a common text format for
data) that describes a single RUSE mod as a set of *small, precise changes* to the
game's data. Think of it like an edit list, not a full copy.

Instead of shipping a whole replacement game archive (a `.dat` file), an `.rmod`
carries only the **changes**. Because it only has the changes, several mods that
would otherwise clash can be stacked on the same game files and still work together.

This page is the exact, field-by-field reference for the format. Every detail here
has been checked against the game engine:

| Topic | The file that defines it |
| --- | --- |
| The layout, fields, checks, and type names | `ruse_mod_engine/mod_format.py` |
| How changes are applied, and how paths/references are handled | `ruse_mod_engine/applier.py` |
| How changes are *made* (the precise diff) | `ruse_mod_engine/converter.py` |
| Converting a mod between game versions | `ruse_mod_engine/migrate.py` |

Related guides: [Converting a mod](convert.md) ·
[Versions & backups](versions-and-backups.md) ·
[Project README](../../README.md).

---

## 1. Top-level structure

Here is the overall shape of an `.rmod` file:

```json
{
  "$schema": "ruse-mod/v1",
  "id": "my-mod",
  "name": "My Mod Name",
  "version": "1.0.0",
  "author": "Your Name",
  "description": "Describe what this mod changes",
  "game_version": "190852",
  "patches": [ /* … NDF patch groups … */ ],
  "loc_patches": [ /* … localization (.dic) edits … */ ],
  "sdb_patches": [ /* … AI-terrain layer edits … */ ],
  "scenario_patches": [ /* … embedded-NDF placement edits … */ ],
  "file_patches": [ /* … raw whole-file replacements … */ ]
}
```

| Field | Required? | Type | What it means |
| --- | --- | --- | --- |
| `$schema` | no | text | A tag saying which format this is. If missing, it defaults to `"ruse-mod/v1"`. |
| `id` | **yes** | text | A short, stable name that is safe to use as a filename. Allowed characters: letters, digits, `-`, `_`, `.`. **No slashes and no `..`** — this is a safety rule, because `.rmod` files get shared between people. |
| `name` | **yes** | text | The friendly name shown to people. |
| `version` | **yes** | text | The mod's version (anything you like, e.g. `"1.0.0"`). |
| `author` | no | text | Who made it. Defaults to `""` (blank). |
| `description` | no | text | What the mod does. Defaults to `""` (blank). |
| `game_version` | no | text | Which RUSE **build** this mod is for (a build id like `"190852"`), or a branch word (`"public"`, `"compat"`). Used when converting the mod so it knows which version it currently fits. Defaults to `""`. |
| `patches` | no¹ | list | Groups of NDF changes (see §2). NDF is the game's own data format. |
| `loc_patches` | no | list | Edits to text/name dictionaries (see §6.1). |
| `sdb_patches` | no | list | Edits to the AI-terrain map layers (see §6.2). |
| `scenario_patches` | no | list | Edits to the data hidden inside `.scenario` map files (see §6.3). |
| `file_patches` | no | list | Whole-file replacements — the safe catch-all (see §6.4). |

¹ `patches` is optional and defaults to an empty list `[]`, but a real mod must have
at least one of the five patch lists filled in — otherwise it does nothing. If any
list that *is* present isn't a proper JSON list, the reader stops with a
`ModFormatError`.

> **Checking is strict.** Reading the file fails (with a `ModFormatError`) if a
> required field is missing, the `id` is malformed, a patch container isn't a list, a
> property `set` value isn't a `{type, value}` object, or a base64 blob is invalid.

---

## 2. An NDF patch group

Each entry in `patches` targets exactly **one NDF data file inside one `.dat`
archive**. (An NDF file is one of the game's internal data files; a `.dat` is the
archive that holds many of them.)

```json
{
  "dat": "Data/PC/190852/ZZ_GladPatchableWin.dat",
  "ndf": "genglad/patchable/gfx/everything.cpp.gladndfbin",
  "create_if_missing": false,
  "changes": [ /* … change entries … */ ]
}
```

| Field | Required? | What it means |
| --- | --- | --- |
| `dat` | **yes** | Path to the `.dat` archive, written **relative to the game's root folder**. Slashes get tidied up automatically. |
| `ndf` | **yes** | Path to the NDF file *inside* that `.dat`. Slashes tidied up too. |
| `changes` | **yes** | The list of changes to make (see §3). Must be a JSON list. |
| `create_if_missing` | no | If `true` and the NDF file isn't in the `.dat`, start from a blank NDF and add it instead of failing. Default `false`. |
| `index_map` | no | A translation table written by the version-converter — **you never type this by hand**. See §7. |

### 2.1 How `dat` paths work

`dat` paths are always written relative to the game's root folder. The applier tidies
them to fit whichever version you have installed:

- **Core dats:** `Data/PC/<sub>/<dat>` — e.g. `Data/PC/190852/ZZ_GladPatchableWin.dat`.
- **Terrain dats:** `Maps/PC/<dat>` — shared across versions, used as-is.
- **Old-style paths** that begin with `PC/...` still work: the applier adds `Data/` in
  front, so older community mods keep applying.
- In **public** mode (the Steam re-release), every `Data/PC/...` path is flattened to
  `Data/PC/190852/<dat-name>`, because the re-release puts all core dats in one
  folder. OG/Compat versions keep their own sub-folders (`Data/PC/99/...`,
  `Data/PC/1360/...`).

Every finished `dat` path is checked to make sure it stays **inside** the game/output
folder. If a path tries to escape (using `..` or a full drive letter), that whole
group is refused and skipped. This is a safety wall: a hostile shared mod can never
read or overwrite files outside the game folder.

### 2.2 Common targets

| `ndf` (inside `ZZ_GladPatchableWin.dat`) | Holds |
| --- | --- |
| `genglad/patchable/gfx/everything.cpp.gladndfbin` | Units, aircraft, buildings, ammo, weapons, AI profiles |
| `genglad/patchable/gfx/gdconstanteoriginal.cpp.gladndfbin` | Game-wide number settings (economy, timings) |
| `genglad/patchable/gfx/visibility.cpp.gladndfbin` | The vision/sight range table |
| `genglad/patchable/misc/globals.cpp.gladndfbin` | The multiplayer map list, campaign chapters |
| `genglad/patchable/mapinfo.cpp.gladndfbin` | Map load info and map UI textures |

Common data tables you'll target: `TUniteAuSolDescriptor` (ground units),
`TAvionDescriptor` (aircraft), `TBatimentDescriptor` (buildings), `TAmmunition`,
`TWeaponDescriptor`, `TIAProfil` (AI), `TTunableConstante` (tunable numbers),
`TVisibilityRange`, `TMultiMapInfo`, `TChapterMapInfo`, `TMapLoadInfo`.

---

## 3. Change entries (actions)

A change entry lives inside a group's `changes` list. Here's one:

```json
{
  "action": "patch",
  "table":  "TUniteAuSolDescriptor",
  "match":  { "ClassNameForDebug": "Unit_Stug_III_B" },
  "set":    { "SeuilMort": { "type": "Float32", "value": 600.0 } }
}
```

| Field | Used by | What it means |
| --- | --- | --- |
| `action` | all | What to do: `"patch"` (the default) \| `"create"` \| `"delete"` \| `"delete_props"`. Capitalization doesn't matter. |
| `table` | all | The NDF class (data type) to work on. **Required.** |
| `match` | patch / delete / delete_props | `{property: value}` conditions. All must match (they're ANDed together) and are compared as text. Leave it out to match **every** instance. |
| `set` | patch / create | `{property: {type, value}}` — the properties to write. |
| `props` | delete_props | A list of property names to remove. **Required** and can't be empty for `delete_props`. |
| `local_id` | create | A label so other changes can point at this new instance with `$ref` (see §5). Usually named `inst_<N>`. |
| `top_object` | create | If `true`, register the new instance in the NDF's `top_objects` list. |

### 3.1 What each action does

| Action | What happens |
| --- | --- |
| **`patch`** | Find every matching instance and write each property from `set`. If the class doesn't have that property yet, it is **added** — so a mod can introduce a brand-new property, not just change existing ones. If nothing matches, you get a warning and the change is skipped. If several match, all of them are changed. |
| **`create`** | Add a brand-new instance to the `table` class. `set` gives it its starting properties. If you gave it a `local_id`, it's registered so `$ref` can find it. It can also be added to `top_objects`. |
| **`delete`** | Remove every matching instance from the table. The `top_objects` list is **fixed up**: deleted entries are dropped and the remaining ones shift down. (Deletes run highest-index-first so the shifting doesn't cause mistakes.) |
| **`delete_props`** | Remove the named `props` from every matching instance. |

### 3.2 How matching works

- Each `match` key is a property **name**, and its value is compared against the
  property's text form.
- `ClassNameForDebug` (or `DescriptorId` if that's missing) is the **best key to
  match on**. It's a stable name that survives when the game reshuffles things, so use
  it whenever you can.
- An **`anchor`** is a durable key for a *keyless* sub-object — like a weapon's ammo, a
  turret, or a camera key — that has no stable name of its own. Instead of a position
  number it records the nearest **keyed ancestor plus the path to reach it**, e.g.
  `{ "anchor": { "root": ["ClassNameForDebug", "Unit_Soldat_US_Para"], "steps":
  [["WeaponDescriptor"], ["TurretDescriptorList", "[0]"], ["Ammunition"]] } }`. The
  applier walks that path on the *live* game data, so the target is found by **identity**
  and survives a game update that shifts positions — the same durability a name gives a
  top-level instance. The converter emits anchors automatically wherever it can.
- `_index` is a **special positional key**: it matches by raw position number.
  Position numbers are fragile — they can break between game versions (fixing that is
  exactly what migration does, §7). The converter only falls back to `_index` for
  instances that have no stable name **and** can't be anchored (the small leftover tail).
- A name-based `patch` (without `_index`) is automatically stopped from hitting an
  instance that this same mod **created earlier in the same run**. So if a mod creates
  a new unit that reuses an original unit's `ClassNameForDebug`, and also patches the
  original one, it won't accidentally clobber its own creation.

### 3.3 The order changes run in (within one group)

1. **Creates run first** (sorted by the number in their `local_id`, e.g.
   `inst_63840`). This way, later `$ref`/`local_id` references can find their target,
   and new instances land in the same order as in the original mod.
2. **Patches, deletes, and delete_props run next**, once every created instance is
   known.
3. **A final pass** fills in `stable_ref` references whose targets were only created
   in step 1.

---

## 4. Supported NDF value types

Every `set` value is written as `{ "type": <name>, "value": <json> }`. The type names
are **not case-sensitive**. The converter writes these out, and the applier turns them
back into real game values.

### 4.1 Scalars (single numbers and true/false)

| Type | How `value` is written | Notes |
| --- | --- | --- |
| `Bool` | `0` / `1` (as a number) | Stored as a whole number (0 or 1). |
| `Int8`, `Int16`, `UInt16`, `Int32`, `UInt32` | whole number | |
| `Long` | whole number | A big (64-bit) whole number. |
| `Time64` | whole number | A time value. |
| `Float32`, `Float64` | number | Decimal numbers. |

```json
{ "ProductionTime": { "type": "Int32", "value": 8 } }
```

### 4.2 Strings (text)

| Type | `value` | Notes |
| --- | --- | --- |
| `StringRef` | the **text itself** | The game handles storing it in its text table — *never pass a raw table number*. |
| `PathRef` | the path text | Handled the same way as `StringRef`. |
| `WideStr` / `WideString` | the text | A wide (UTF-16) string stored inline. |

### 4.3 Vectors & colours

All of these are written as **JSON arrays** (lists in square brackets):

| Type | Array shape |
| --- | --- |
| `Vector3` | `[x, y, z]` |
| `Color32` | `[r, g, b, a]` (values 0–255) |
| `Color128` | `[r, g, b, a]` (decimal values) |
| `TripleInt` | `[a, b, c]` |
| `Int2` | `[a, b]` |
| `Float2` | `[a, b]` |
| `Matrix` | a flat list of components |

```json
{ "Couleur": { "type": "Color32", "value": [255, 0, 0, 255] } }
```

### 4.4 References (pointers to other instances)

| Type | `value` | Notes |
| --- | --- | --- |
| `ObjRef` | an object — see §5 | A pointer to another instance. It can be written a few different ways. |
| `TransRef` | `{ "trans": "<path>" }` | A pointer to an **imported** object in another file, stored by its full path (e.g. `"$/IA/Cluster/PackMesh_All"`) so it stays valid across different NDF files and game versions. The applier maps the path to whatever number the target file uses. |

### 4.5 Collections (lists and maps)

| Type | `value` | Notes |
| --- | --- | --- |
| `List<T>` | a JSON array of `T`-values | The element type is inside the name, e.g. `List<Int32>`, `List<ObjRef>`. An empty list is written as `List<Int32>` / `[]`. |
| `Map<K,V>` | an array of `[key, value]` pairs | e.g. `Map<StringRef,ObjRef>`. An empty map is written as `Map<Int32,Int32>` / `[]`. |
| `Pair` | `[{type,value}, {type,value}]` | Two typed items together. |

If a `List<ObjRef>` or `List<TransRef>` mixes different kinds of elements, each element
gets its own `{type,value}` wrapper so there's no confusion about its type:

```json
{
  "MultiList": {
    "type": "List<ObjRef>",
    "value": [
      { "type": "ObjRef", "value": { "$ref": "inst_63840" } },
      { "type": "ObjRef", "value": { "inst": 12, "class": 7 } }
    ]
  }
}
```

`Pair` and `Map` elements can hold `ObjRef` `local_id`/`stable_ref` values themselves —
the applier resolves those too, instead of treating them as plain data.

### 4.6 Other types

| Type | `value` | Notes |
| --- | --- | --- |
| `Blob` | a base64 string | Raw bytes (encoded as text). |
| `ZipBlob` | `{ "flag": <int>, "data": <base64> }` | A compressed blob, keeping its flag byte. |
| `Hash` | a hex string | |
| `Guid` | a hex string | |
| `LocHash` | a hex string | An 8-byte name-lookup key (it's also the `.dic` key — see §6.1). |

---

## 5. ObjRef portability

An `ObjRef` (a pointer to another instance) can be written a few different ways. The
converter picks the most **portable** one — the one most likely to still point at the
right thing even if position numbers differ between the mod file and the live game
data:

| Encoding | When the converter uses it | How the applier finds the target |
| --- | --- | --- |
| `{ "local_id": "inst_<N>", "class": <ci> }` | The target is a **new instance created** in this same mod. | Looked up by that label — no position numbers involved. |
| `{ "$ref": "<local_id>" }` | A short form of the above, for pointing at a create in the same rmod (also written as `{ "type": "$ref", "value": "<local_id>" }`). | Same as `local_id`. |
| `{ "stable_ref": "<keyprop>", "key_val": "<value>", "class": <ci> }` | The target is an original instance whose stable name is **unchanged** between the original game and the mod. | Found by name in the *live* game data (including this run's creates). Uses the **live** class number, not the stored one. |
| `{ "anchor": { "root": [...], "steps": [...] }, "class": <ci> }` | The target is a **keyless** instance (ammo, turret, camera key, …) reachable from a keyed ancestor. Same durable form as an `anchor` *match* (§3.2). | The applier walks the ancestor + path on the live data, so the target is found by identity and survives position shifts across game updates. |
| `{ "inst": <N>, "class": <ci> }` | The last-resort fallback: a plain position number. Used when the instance was renamed by this same rmod, or has no stable name **and** can't be anchored. | Used as a raw position; this is the form migration fixes up (§7). |

The `class` number stored in an `ObjRef` is only a hint. When resolving, the applier
prefers the class number from the **live** game data instead. That matters when a mod
hasn't been fully pre-converted for the version.

Because `stable_ref` is only used when the name is *unchanged*, renaming an instance
and then pointing at it falls back cleanly to a plain position (`inst`) — you never get
a reference that can't be found when the mod is applied.

---

## 6. Non-NDF surgical patch kinds

The converter looks at every changed dat file and picks the **most precise** patch
kind it can safely use. If it can't make a precise diff, it falls back to a whole-file
replacement — so **nothing that changed is ever silently dropped.**

### 6.1 Localization patches — `loc_patches`

Unit and map display **names** live in `.dic` name dictionaries, each name keyed by an
8-byte **LocHash** number. A loc patch carries only the changed keys, so a rename is
just a few bytes, and several name-mods can stack.

```json
"loc_patches": [
  {
    "dat": "Data/PC/190852/ZZ_Win.dat",
    "dic": "genlocalisation/ww2/localisation/translations/us/baseunite.dic",
    "entries": [
      { "key": "42209413f6951800", "value": "ATOMIC PERSHING" },
      { "key": "0011223344556677", "value": "New Card", "add": true }
    ]
  }
]
```

- `key` must be **16 hex characters** (checked). `value` is the new text.
- `add: true` is a *hint* that the key is new. The applier still checks for itself
  whether the key already exists (it updates existing keys and adds missing ones), so
  the hint can never cause a wrong result.
- The converter only writes a loc patch when the dictionary is valid and **no key was
  removed**. If a key was removed, it falls back to a whole-file replacement instead.

### 6.2 SDB layer patches — `sdb_patches`

The AI-terrain database (things like forest cover and sight-blocking) lives inside a
map's `mapinfo.win` file. A mod that paints a layer flips one bit for each map cell. An
SDB patch ships each changed layer as a **full grid of bits** (one bit per cell,
compressed), so layer edits stack and stay small.

```json
"sdb_patches": [
  {
    "dat": "Data/PC/190852/DataMap_Win.dat",
    "win": "datasmap/<map>/mapinfo.win",
    "grid_size": 1024,
    "layers": [ { "bit": 8, "mask": "<base64(zlib(LSB-first R*R bitmask))>" } ]
  }
]
```

- `grid_size` is `R`; the grid is `R×R` cells across.
- `bit` says which layer (`0x08` = forest/conceal, `0x04` = blocked/clear-path — those
  are the only two the game actually reads; `0x01` is an internal flag, never a layer).
- The applier reads the map data, turns each layer on or off according to its mask
  (other layers are left alone), rebuilds the data, and recomputes the checksums it
  needs. There's a fast path and a slower fallback path if the fast one isn't available.
- This is used **only** when nothing but the SDB data changed. If the road graph or
  anything else in `mapinfo.win` changed too, the whole file is shipped as a plain
  replacement instead.

### 6.3 Scenario patches — `scenario_patches`

A `.scenario` file is two things: the map's zone geometry, plus a hidden data section
that places depots, HQ spawns, labels, and zone objects. When *only* that hidden data
changed (and the geometry is byte-for-byte the same), the diff ships as NDF `changes` —
exactly the same shape as a normal patch group, but aimed at the scenario's inner data.

```json
"scenario_patches": [
  {
    "dat": "Data/PC/190852/DataMap_Win.dat",
    "scenario": "datasmap/<map>/<file>.scenario",
    "changes": [ /* same change entries as §3 */ ]
  }
]
```

The applier reads the scenario, opens the inner data, applies the change list (in the
same create → patch → final-pass order, with **no** cross-version translation), tidies
up the data size, and writes the scenario back out. Changes to the zone geometry (or to
certain internal tables) can't be diffed this way and fall back to a whole-file
replacement.

### 6.3a Import/export edits — `import_add` / `import_remove`

An NDF file keeps a small table of the **other files it points at** (its imports) and
the things **it lets other files point at** (its exports). A custom operation or an
AI-count mod usually adds a couple of imports (for example a shared unit pack). Those
edits ride along inside the same patch group as extra lists, written by **full path** so
they stay valid on every game version:

```json
{
  "dat": "Data/PC/190852/ZZ_GladPatchableWin.dat",
  "ndf": "genglad/.../clustermap.cpp.gladndfbin",
  "import_add": ["$/IA/Cluster/PackMesh_All", "$/IA/Cluster/PackProxy_All"],
  "changes": [ /* the normal instance edits */ ]
}
```

The applier adds each new import to the live file (keeping the existing ones exactly
where they are, so nothing else shifts) and points the mod's `TransRef` values at them.
`import_remove` / `export_remove` drop paths the mod no longer needs. Because these are
plain paths, they need **no** version translation — this is what lets scenario
`clustermap` files (custom operations) ship as small surgical edits instead of a frozen
whole-file copy that breaks when the game updates.

### 6.4 Raw file patches — `file_patches`

This is the safe catch-all. It replaces (or adds) whole files inside a `.dat` — used for
map meshes (KDT), scenarios whose geometry changed, non-standard dictionaries, or any
NDF whose precise diff was empty or unsafe.

```json
"file_patches": [
  {
    "dat": "Maps/PC/SomeTerrain_Win.dat",
    "files": [
      { "path": "datasmap/<map>/leveldesign.kdt", "data": "<base64 file bytes>" }
    ]
  }
]
```

When applied, files that already exist are **replaced**, and missing ones are **added**.
(v1 dats can add files in batches; v2 dats can only replace — trying to add to a v2 dat
is skipped with a warning.)

---

## 7. Cross-version migration

Most references in a modern `.rmod` are **durable**: name-keyed matches, `anchor`
matches and values, and `stable_ref` ObjRefs all find their target by **identity**, so
they keep working when RUSE updates and instances inside the data get added, removed, or
reordered — **no translation needed**. Only the positional fallback (`_index` matches and
`{inst,class}` ObjRefs) is tied to the exact game build the mod was made for. Migration
fixes up that fallback using **translation maps** made by matching instances between two
clean game snapshots (using the same stable-name/anchor matching the converter uses).

For each file, a translation map records: `inst_remap` (old-number → new-number),
`_instance_segments` (runs of numbers that all shift by the same amount),
`_class_map` (old class number → new class number), and `removed` / `removed_keys`
(instances that no longer exist in the new build). The app ships **direct maps between
build pairs** — each pair of clean snapshots is matched up on its own — so a jump between
two modern builds is a **single step, not a chain** (chaining accumulated error). It also
flags any edit whose *value* the game changed between the two builds, so you know which
edits are worth revisiting. (Release order goes by the branch *number*, not the build id —
a later revert can actually be an older build.)

### 7.1 Non-destructive translation — `migrate_rmod`

`migrate_rmod` writes the combined translation map into each patch group's
`index_map[<target build>]` and **leaves your change data alone**. Then, when the mod is
applied and `game_version` matches that build, the applier reads `index_map` and
translates the `_index` matches and ObjRef `inst` numbers on the fly. References to
instances that were **deleted** in the new build, or reference shapes it doesn't
support, are **flagged for you to review — never silently mistranslated.**

```jsonc
// index_map is written by the engine, keyed by target build id — don't hand-edit it:
"index_map": {
  "190852": {
    "48888": 48871,
    "_instance_segments": [ { "from": 48888, "to": 99999, "offset": -17 } ],
    "_class_map": { "12": 11 }
  }
}
```

### 7.2 Rewrite mode — `convert_rmod` ("Convert to all versions")

`convert_rmod` rebuilds the mod **natively** for the target build — that is, in the
target build's own numbering. It rewrites each `dat` path, translates the NDF file paths
across the OG-to-remaster format change, and re-numbers every positional `_index` match
and ObjRef `inst` value using the exact computed map (no guessing). It works in **both
directions** (going forward chains the steps; going backward inverts them). If a change
targets something that was **deleted** in the destination version, that change is
**dropped and flagged**. The rebuilt groups carry `index_map = {}` because they're
already native — no translation is needed at apply time. Path-only groups (`file_patches`,
`loc_patches`, `sdb_patches`, `scenario_patches`) just get their `dat` path relocated.

The old **compat→public** jump also has its own dedicated converter that bakes in the dat
paths, NDF path swaps, the `everything.cpp` number shift, and the `IA_Common`/`DataMap`
path fixes. It produces a fully pre-translated `.rmod` that the applier runs as-is. If a
`.compat.rmod` is instead applied in public mode, it gets that same translation at the
moment it's applied.

> See [convert.md](convert.md) for how to do this in the UI, and
> [versions-and-backups.md](versions-and-backups.md) for how game versions and
> clean backups are handled.

---

## 8. Full annotated example

```jsonc
{
  "$schema": "ruse-mod/v1",
  "id": "stug-buff-v1",
  "name": "StuG III Buff",
  "version": "1.0.0",
  "author": "Example Author",
  "description": "Tougher StuG, cheaper economy, renamed Pershing, one new unit.",
  "game_version": "190852",

  "patches": [
    {
      "dat": "Data/PC/190852/ZZ_GladPatchableWin.dat",
      "ndf": "genglad/patchable/gfx/everything.cpp.gladndfbin",
      "changes": [
        {
          "action": "patch",
          "table": "TUniteAuSolDescriptor",
          "match": { "ClassNameForDebug": "Unit_Stug_III_B" },
          "set": {
            "SeuilMort":      { "type": "Float32", "value": 600.0 },
            "ProductionTime": { "type": "Int32",   "value": 8     }
          }
        },
        {
          "action": "delete_props",
          "table": "TUniteAuSolDescriptor",
          "match": { "ClassNameForDebug": "Unit_Stug_III_B" },
          "props": [ "ObsoleteFlag" ]
        },
        {
          "action": "create",
          "table": "TUniteAuSolDescriptor",
          "local_id": "inst_99001",
          "top_object": true,
          "set": {
            "ClassNameForDebug": { "type": "StringRef", "value": "Unit_Super_Stug" },
            "SeuilMort":         { "type": "Float32",   "value": 900.0 }
          }
        }
      ]
    },
    {
      "dat": "Data/PC/190852/ZZ_GladPatchableWin.dat",
      "ndf": "genglad/patchable/gfx/gdconstanteoriginal.cpp.gladndfbin",
      "changes": [
        {
          "action": "patch",
          "table": "TTunableConstante",
          "match": {},
          "set": { "QteDeviseInitiale": { "type": "Int32", "value": 500 } }
        }
      ]
    }
  ],

  "loc_patches": [
    {
      "dat": "Data/PC/190852/ZZ_Win.dat",
      "dic": "genlocalisation/ww2/localisation/translations/us/baseunite.dic",
      "entries": [
        { "key": "42209413f6951800", "value": "ATOMIC PERSHING" }
      ]
    }
  ]
}
```

What this mod does, step by step:

1. **patch** — finds the StuG by its stable `ClassNameForDebug` name, then raises its
   death threshold to `600` and its build time to `8`.
2. **delete_props** — removes the `ObsoleteFlag` property from that same unit.
3. **create** — adds a brand-new unit, labelled `inst_99001` (so other changes could
   point at it with `$ref`), and registers it as a top object.
4. A second patch group lowers the starting cash for *all* `TTunableConstante` instances
   (an empty `match` means "every instance").
5. A loc patch renames the Pershing card by editing one `.dic` key.

---

## 9. Round-trip & layering notes

- Reading a mod in and writing it back out doesn't lose anything — the format survives a
  full round-trip.
- **Layering (stacking mods):** when several mods are applied in order, a later mod
  overrides an earlier one for the same property. The applier copies a dat into the
  output folder the first time any mod touches it, then keeps patching that same copy —
  so changes from different mods **add up** instead of one erasing the next.
- **Conflict detection:** the tool can do a test run of a set of mods and report any
  spot where two mods both `patch` the same property, so you can spot clashes.
- **Slashes:** both `/` and `\` are accepted anywhere and are tidied to `/` when the
  file is read.
```

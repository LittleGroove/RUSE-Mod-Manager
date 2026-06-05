# RUSE Mod Manager

A standalone Windows application that fundamentally redefines modding for **R.U.S.E.** Instead of distributing full replacement `.dat` files that break each other, mods are **surgical patch files** that describe only what they change, so multiple mods coexist and stack cleanly.

It is also a full **mod creation suite**: convert old mods, or build new ones from scratch with editors for units, the AI, the economy, maps, and every raw asset in the game.

Supports both **R.U.S.E. Compat** (Ubisoft build) and the **public Steam release**.

**[Latest release →](https://github.com/LittleGroove/RUSE-Mod-Manager/releases/latest)**

---

## Contents

- [Just want to play mods? (Players)](#just-want-to-play-mods-players)
- [Download](#download)
- [The four tabs](#the-four-tabs)
- [1. Mod Manager tab](#1-mod-manager-tab) — enable, order and deploy mods
  - [Basic use](#basic-use)
  - [Advanced: caching, dry runs and bundled mods](#advanced-caching-dry-runs-and-bundled-mods)
- [2. Convert tab](#2-convert-tab) — turn old `.dat` mods into `.rmod`
- [3. Mod Editor tab](#3-mod-editor-tab) — build a mod from scratch
  - [Projects: create, load, save, deploy, export](#projects-create-load-save-deploy-export)
  - [Units & Buildings editor](#units--buildings-editor)
  - [AI editor](#ai-editor)
  - [Economy editor](#economy-editor)
  - [Map editor](#map-editor)
  - [Raw / Asset editor](#raw--asset-editor)
- [4. Settings tab](#4-settings-tab)
- [How updates work](#how-updates-work)
- [The `.rmod` format (reference)](#the-rmod-format-reference)
- [Running from source](#running-from-source)
- [Technical notes](#technical-notes)
- [License](#license)

---

## Just want to play mods? (Players)

If you only want to **download mods and play**, you don't need the rest of this page. Five steps:

### 1. Download it

Go to the **[Latest release →](https://github.com/LittleGroove/RUSE-Mod-Manager/releases/latest)** and download **`RUSE_ModManager_v<X.Y.Z>.exe`**.

That single `.exe` *is* the whole program. There is **no installer** and **nothing to set up** — Python is not required.

### 2. Put it in a folder Windows lets it write to (important)

The Mod Manager **creates files and folders next to itself** (a `mods/` folder, an `output/` folder for backups and patched files, a `settings.json`, etc.). If Windows blocks writing to that location, the app will fail or behave strangely.

**Do this:** make a normal folder you own and drop the exe inside it. Good places:

- `C:\Games\RUSE Mod Manager\`
- Your Desktop, e.g. `C:\Users\<You>\Desktop\RUSE Mod Manager\`
- `C:\Users\<You>\Documents\RUSE Mod Manager\`
- Any folder on another drive, e.g. `D:\RUSE Mod Manager\`

**Avoid these — Windows protects them and will block writes:**

- `C:\Program Files\` or `C:\Program Files (x86)\`
- `C:\Windows\` or anywhere inside it
- The root of your C: drive (`C:\` directly)
- Running it straight out of the `.zip` preview without extracting first

> **Tip:** Give the exe its **own dedicated folder**. You do **not** need administrator rights, and you should **not** "Run as administrator."

### 3. Run it

Double-click the exe. If Windows SmartScreen shows a blue "Windows protected your PC" box, click **More info → Run anyway** (this appears because the app isn't code-signed, not because anything is wrong).

### 4. Point it at your game (one time)

Open the **Settings** tab. Click **Detect Game Version** to find your install automatically via Steam, or set **Game Root Directory** by hand to your R.U.S.E. install folder (the one containing `Ruse.exe` and `Data/`). Then click **Create Backup** — this makes a safe copy of your original game files. Your originals are never lost.

### 5. Add mods and play

- Put any `.rmod` files you've downloaded into the **`mods/`** folder (or use **Add .rmod…** on the Mod Manager tab).
- Tick the mods you want, drag them into the order you like, and click **▶ Deploy Mods**.
- Changed your mind? Click **Restore Clean** to put the game back exactly as it was.

That's it — launch R.U.S.E. and your mods are active. Everything below is reference detail for power users and modders.

---

## Download

**[Latest release →](https://github.com/LittleGroove/RUSE-Mod-Manager/releases/latest)**

Grab `RUSE_ModManager_v<X.Y.Z>.exe` for a single self-contained executable, or the `.zip` bundle (exe + an empty `mods/` folder + preset profiles).

No installer, no Python required — just download and run. See the **[Players quick start](#just-want-to-play-mods-players)** for where to put the exe so Windows doesn't block it.

---

## The problem (and the solution)

Classic RUSE mods replace entire `.dat` archive files. If two mods touch the same `.dat`, only one can be active — they are fundamentally incompatible. Players had to pick one or the other.

The Mod Manager introduces the **`.rmod`** format: a small JSON file that records *only* the specific changes a mod makes. Rather than *"replace this entire archive"*, an `.rmod` says *"find this unit and change these two properties."* Any number of mods can patch the same file without conflict. **Load order** decides who wins when two mods edit the same property.

---

## The four tabs

The app has four tabs across the top:

| Tab | What it's for |
|---|---|
| **Mod Manager** | Enable/disable, order, and deploy `.rmod` mods to your game. This is where players live. |
| **Convert** | Turn an old full-`.dat`-replacement mod into a clean `.rmod` patch file. |
| **Mod Editor** | Build a mod from scratch — edit units, AI, economy, maps, and any raw asset. |
| **Settings** | Game paths, backups, profiles, language, and shortcuts. |

---

## 1. Mod Manager tab

This is the main tab: the list of your mods, in load order, with a Deploy button.

### Basic use

**First-time setup checklist.** The top of the tab shows two steps that must both be green before you can deploy:

1. **Set Game Root** — if it's red, click **Open Settings** and set the Game Root Directory (or use **Detect Game Version**).
2. **Create Backup** — click **Create Backup** to copy your original game files. Until this exists, deploying is blocked. **Restore Clean** (here or in Settings) reverts the game to those originals at any time.

**Adding mods.**

- **Scan Mods Folder** — picks up any `.rmod` files you've dropped into the `mods/` folder.
- **Add .rmod…** — file picker that copies one or more `.rmod` files into the folder for you.
- **Remove Selected** / **Clear All** — take mods out of the list. (Bundled "SAFE" mods re-appear automatically — see below.)

**The mod list.** Each row shows:

- a **☑ / ☐ checkbox** (click it to enable/disable that mod),
- an optional **[SAFE]** tag (a bundled multiplayer-safe mod) or **[COMPAT]** tag (a `.compat.rmod`),
- the mod **name** and **version**.

Click a row to see its full details (name, author, version, number of NDF changes, description) in the panel on the right.

**Load order.** Mods apply **top to bottom**, and **the bottom wins** on conflicts: *"TOP loads first — BOTTOM overrides."* Reorder with the arrow buttons on the right:

- **⇈** move to top, **▲** up one, **▼** down one, **⇊** move to bottom.

When two mods change the *same property* on the *same thing*, the lower one wins. Everything else from both mods is applied normally.

**Enable / disable in bulk.**

- **☑ Enable Selected** / **☐ Disable Selected** — for the highlighted rows.
- **All Off** — disable everything at once.

**Deploy.** Click **▶ Deploy Mods** to apply every enabled mod, in order, to your game. **🎮 Launch R.U.S.E.** starts the game from your configured Game Root. To undo everything, click **Restore Clean** (game returns to the backed-up originals).

**Share & import load orders.** Playing with friends? Use **Share Order** to copy your enabled mod list (names + versions) to the clipboard as a short text block, and **Import Order…** to paste a friend's list back in. The importer checks which mods you actually have, warns about anything missing or on the wrong version, and rearranges/enables your list to match.

### Advanced: caching, dry runs and bundled mods

A row of options sits to the left of the Deploy button.

**Dry run (no files written).** Runs the whole deploy and logs everything it *would* do, without touching the game. Handy for checking conflicts before committing.

**The deploy cache (on by default).** Patching every `.dat` from scratch on each deploy is slow. The cache stores the patched result of each *prefix* of your load order, so re-deploying after a small change only re-does the part that changed.

- **Cache enabled** — master switch for the whole caching system.
- **Per-mod cache points** — when on, each mod row gets a small **[✓ cache] / [ cache]** toggle on the far right. The cache reuses the **longest matching run of mods from the top** that hasn't changed, then applies the rest on top. Example: if mods 1–3 are unchanged and cached, deploying 1–5 reuses the 1–3 result and only applies 4 and 5. Turn off a mod's cache point if you're actively editing it.
- **Always regenerate dat files** — rebuilds every `.dat` from patches on every deploy (still writes to the cache), ignoring reuse. Use this if you suspect the cache is stale.

**Bundled "SAFE" mods.** Some multiplayer-balance mods are baked into the exe itself. They're marked **[SAFE]**, are always present, and **override (hide) any external mod with the same name and major version** — this keeps multiplayer games consistent for everyone running the manager. They can still be toggled, and their state persists across restarts by identity.

**Compat vs. Public.** The manager detects whether your game is the Compat build or the public Steam release and shows the matching mods. In Public mode a **COMPAT ○ / ●** toggle controls whether `.compat.rmod` files are also shown in the list. Each branch keeps its own enable/order state.

The **Log** panel at the bottom records every backup, deploy and restore with colour-coded, before/after detail; **Clear Log** empties it.

---

## 2. Convert tab

The Convert tab turns an **old-style mod** (one that ships full replacement `.dat` files) into a clean **`.rmod`** patch, by diffing the mod's `.dat` files against your clean originals and recording only what differs.

### How to convert

1. **Mod Folder:** click **Browse…** and pick the **root folder** of the old mod (the folder that contains the game's directory layout — see structure below). A coloured line tells you what it detected:
   - paths with `99/` or `1360/` → **R.U.S.E. COMPAT** mod → saved as `.compat.rmod` to `mods/compat/`
   - paths with `190852/` → **R.U.S.E. (public)** mod → saved as `.rmod` to `mods/public/`
   - no version folder → a warning (you can still proceed).
2. Fill in the mod info on the left: **Name**, **ID** (auto-generated from the name if left blank), **Version** (`x.x.x` — the first/major number becomes the `_V#` filename suffix), and **Author**. A live preview shows the exact output filename. Add an optional **Description** on the right.
3. Click **Scan for Changes** to list the `.dat` files it found and matched to your originals.
4. Click **▶ Convert** (the button names the target, e.g. *"Convert to .rmod"*). The log shows each file diffed and a final change summary; the finished `.rmod` lands in the matching `mods/` subfolder, ready to deploy.

The converter diffs intelligently per file type — NDF gameplay data, localization (`.dic`), map AI-grid (`mapinfo.win`), and scenarios are diffed *surgically*; anything it can't diff falls back to a lossless raw-file patch.

### Expected mod folder structure

The converter mirrors the game's own layout and is flexible about how deep the folder nesting goes. Core gameplay/data files live under a **version folder** (`99` or `1360` for Compat, `190852` for public); terrain/map files live under `Maps/PC/`.

**Public (Steam) mod:**

```
my_public_mod/
├── Data/
│   └── PC/
│       └── 190852/
│           ├── ZZ_GladPatchableWin.dat
│           └── ...other core .dats
└── Maps/
    └── PC/
        └── Map_0123.dat        (optional, terrain)
```

**Compat (Ubisoft) mod:**

```
my_compat_mod/
├── Data/
│   └── PC/
│       ├── 99/
│       │   └── ZZ_GladPatchableWin.dat
│       └── 1360/
│           └── ...other version's dats (if needed)
└── Maps/
    └── PC/
        └── Map_0123.dat        (optional, terrain)
```

Older flat layouts also work — the `Data/` wrapper may be omitted (`PC/190852/…`), and even a loose version folder at the root (`190852/…`) is accepted. The converter normalises all of these to game-root-relative paths automatically.

### Convert Compat → Public

A separate panel at the bottom translates an **already-made `.compat.rmod`** into a public `.rmod` (it remaps all the internal map/NDF paths and indices to the public game's layout). Use **Browse…** for a single file or **Browse Folder…** to batch a whole folder, then **▶ Convert to R.U.S.E. .rmod**. Output goes to `mods/public/`.

---

## 3. Mod Editor tab

The Mod Editor is a full creation suite. You make a **project**, edit it through several specialised windows, then either **deploy** it to your game directly or **export** it as an `.rmod` to share.

> **Editor projects target the public Steam release only.** The Compat build is maintenance-only (use Convert for it).

### Projects: create, load, save, deploy, export

**What a project is.** A folder under `output/editor_mods/<name>/` holding a `project.json`, a `description.txt` (author / description / version), and working copies of the game `.dat` files you've edited. A `.dat` is only copied into the project the first time you save a change that touches it — so a project stays small.

**Create / load.** The Mod Editor opens on a selection screen:

- **Create New Mod** — type a **Mod name** and click **Create Project**.
- **Load Existing Mod** — pick from the list and click **Load Selected** (or double-click), use **Refresh** to rescan, or **Browse Folder…** to open a project from anywhere.

**The project hub.** Once loaded you see the mod's name, a save-status indicator (*✓ all changes saved* / *● N unsaved change-set(s)*), a **Mod Windows** section with one button per editor, a **Project** section, and a **Mod Details (description.txt)** box (Author / Version / Description + **Save Details**).

**Saving.** Each editor window has its **own Save button** (e.g. *Save mod (.dat)*) that writes everything you changed in that window into the project's `.dat`. The hub's status counts how many windows still have unsaved changes; it warns you if you try to close or deploy with unsaved work.

**Deploy vs. export — the two outputs:**

- **Deploy to Game** — copies the project's `.dat` files straight into your live game (timestamped backups are made first, and any files a previous deploy left modified but this mod doesn't touch are reverted to clean). Good for testing your own mod.
- **Convert to rmod** — exports an **update mod containing only your changes** as a versioned `.rmod` into the `mods/` folder, ready to share. This reuses the same diffing engine as the Convert tab and pulls metadata from your Mod Details.

**Close Project** returns to the selection screen.

The editor windows open one at a time inside the tab; a **← Back** button returns you to the hub.

---

### Units & Buildings editor

Edit unit and building stats, weapons/ammo, upgrade chains, and in-game display names. Two tabs: **Units & Buildings** and **Ammo**.

**Finding a unit.** Filter the left-hand list by **Faction**, by **Building** (shows only the units that appear in that building's menu), and/or a **Search** box. Click a unit to load its properties on the right.

**Editing.** Each editable stat shows its **Field** name, the **Current** value, and a **New value** box. You can change health (`SeuilMort`), speeds and acceleration, vision/detection, attack ranges, **build time** and **price per game mode**, the **build menu (Factory)** and **menu slot**, per-mode visibility, upgrade price/time, and (for buildings) the building type. The **Factory** field's current-value display lists which buildings actually host that unit, so you can see exactly where it will appear.

**Display names.** If `ZZ_Win.dat` is part of the project, you can edit a unit's in-game name **per language** (pick a language, type the name).

**Upgrade chains.** Tick **upgradable** to make a unit a researched upgrade, or pick a parent from **Upgrades from** to slot it into an existing chain.

**Weapons & ammo.** Under a selected unit, each weapon lists its current ammo with a **Set ammo** dropdown; **Remove all weapons from this unit** strips them. Editing an ammo's stats affects **every** unit that shares that ammo. To give a unit a *unique* weapon, go to the **Ammo** tab, **Duplicate this ammo** to make a private copy, then **Set ammo** it on the weapon.

**Saving.** **Apply changes to this unit** (or ammo) commits your edits into the project; **Save mod (.dat)** writes everything to disk.

> ⚠ **Do not use these two features — they are partially implemented and do not work yet:**
> - **"Duplicate this unit"** (the button next to *Apply changes*). It does not produce a working in-game unit.
> - **Changing a unit's faction / nation** — both the **"Migrate to nation…"** button and editing the raw `Nationalite` property by hand. Moving a unit to a different faction does not work correctly in-game.
>
> Editing existing units (stats, weapons, names, upgrades, menu placement *within their own faction*) works fine. Just don't clone units or move them between factions for now.

---

### AI editor

Tunes the C++ skirmish AI. Four tabs:

- **AI Profiles** — the 10 `TIAProfil` configurations (a default plus the difficulty/personality profiles: Regular, Air Force, Howitzer, Prototype, Blitzkrieg, Turtle, Random). Dozens of grouped values: attack/defense behaviour, harassment, economy and money/income **cheat bonuses**, logistics & depots, idle production counts, unit-type weighting, deception-card usage, retaliation, and intel/stealth.
- **Difficulty Handicap** — `TAISpecificBonus` entries: the actual cheat amounts, scoped by difficulty (0=Easy, 1=Medium, 2=Hard) and profile.
- **Ruse Cards** — `TBluffCardDescriptor`: each deception card's effect duration, menu visibility, and slot.
- **AI Scripts** — a **read-only** viewer that decompiles the mission/skirmish scripts from `IA_Common.dat` (with an **Export…** for the `.py`). Script *editing/recompiling* isn't wired up yet.

Workflow: pick an item on the left, edit values on the right, **Apply changes**, then **Save mod (.dat)**.

---

### Economy editor

Controls money, supply, production limits, population cap, the deception-card pool, and economy buildings. Two tabs:

- **Global Economy** — the global constants (`TTunableConstante`), grouped: starting money & income, depot/convoy supply output (with read-only derived totals so you can see money-per-convoy and a depot's total supply as you tweak), building value, production limits, AI production-queue tuning, decoy/fake-building settings, **population cap**, and the **deception-card pool**. A catch-all section exposes every other global constant for advanced use.
- **Economy Buildings** — per-building properties for depots, admin buildings and truck factories: cost per phase, build time, HP, menu visibility, depot flag, road distance, vision, plus any other numeric fields.

Workflow mirrors the AI editor: edit, **Apply**, **Save mod (.dat)**.

---

### Map editor

Edits scenario placements, multiplayer game modes, start cameras, the AI-terrain grid, and the map's mission script. It shows the minimap with interactive markers and colour overlays, and **always writes to the mod project, never the live game**.

**Loading.** Pick a **Map** and a **Scenario** from the top dropdowns. Toggle overlays with **Labels**, **Sectors**, **Roads**, **Flip Y**; **Reset view** fits the map; **Revert** discards unsaved edits.

**Placements.** Turn on **Edit placements (drag)** to select and drag markers (with optional **Auto snap to roads**). **+ Placement** opens a creation popup; kinds include **Depot**, **Unit**, **Building**, **Spawn**, **HQ**, **City/Mountain label**, **Named point**, and **Circular/Rect zone**. The **Details** panel exposes each kind's fields — e.g. a depot's supply (`ChampInteger`), an HQ's **alliance/priority** and start-camera angles, a label's text, a zone's radius/size, plus position, height and rotation. **Delete sel** removes the selected one.

**Game modes.** Tick the modes you want to offer in the lobby (1v1, 2v2, 3v3, 4v4, 2v2v2, FFA variants, …). **Recompute ticks** auto-detects which modes the map's existing HQ spawns already support; **Apply lobby modes** stages the lobby metadata into the project (and warns if a ticked mode lacks the required HQs).

**Start cameras.** Selecting an HQ shows its warmup camera path; drag the HQ to move base + camera together, or drag the **cam** ring to orbit the start camera (it auto-aims at the base).

**AI-terrain layers (SDB).** The right panel paints the AI-terrain grid (forest concealment, sight-blocking and other layers). Pick a layer's paint target, enable **PAINT MODE (drag on map)** (or **Erase**), set a **Brush radius**, and paint on the minimap.

**Mission script.** **Edit script** opens a Python-2 script editor for the map's `.xyz` script, with an extensive in-app reference (camps, objectives, triggers, spawns, win/lose patterns). If `tools/python27/` is bundled it can compile and **Save to mod project**; otherwise you can **Save draft as .py** to compile externally.

**Saving.** **Save to mod** stages all of the above into the project's `.dat` files (then Deploy from the hub, or test in-game).

> **Capture-zone (KDT) and sector-shape editing are not exposed.** Changing capture geometry requires rebuilding the game's KD-tree, which isn't implemented yet — so the scenario zone polygons you see are **visual only**. You can place spawns, set modes, edit cameras, paint AI-terrain and edit scripts; you can't reshape capture zones.

---

### Raw / Asset editor

A universal browser/editor for **every** file inside the game's `.dat` archives — for anything the specialised editors don't cover. Pick a `.dat` from the **Project file** dropdown. Three tabs:

- **Browse / Files** — every entry with a live preview (images/textures as thumbnails, NDF summaries, localization tables, decompiled scripts, text, or a hex dump). **Export…**, **Import / Replace…** and **Add File…** stage changes; **Open as nested .dat →** drills into embedded archives (`.ipk`/`.apk`/etc.) to any depth, with **Apply into parent .dat** to fold edits back up.
- **NDF Vars** — drill into any NDF file → instance → property and edit values in a typed dialog. These edits compose with the unit/AI/economy editors (they share the same underlying objects).
- **Search** — find NDF instances by class / property / value across the whole `.dat`; double-click a result to jump straight to it in NDF Vars.

**Save mod (.dat)** writes all staged changes to disk.

---

## 4. Settings tab

**Paths.**

- **Game Root Directory** — your R.U.S.E. install (contains `Ruse.exe` and `Data/`). Set it with **Browse…** or auto-find it via **Detect Game Version**. Everything else keys off this.
- **Working Directory** (read-only) — where the app lives and writes its output/state. **Open…** reveals it in Explorer.
- **Mods Folder** (read-only) — always `<working dir>\mods`. **Open…** reveals it.

**Game File Backup.**

- **Create Backup** — copies your original `Data/` and `Maps/` files so mods can always be undone. Required before deploying.
- **Restore Clean** — reverts the game to that backup (removes all deployed mods).
- **Detect Game Version** — finds your R.U.S.E. / R.U.S.E. Compat install through Steam.

**Profile (Compat).** Convenience buttons for the Compat campaign profile: **Set lvl 1 Profile**, **Set lvl 100 Profile**, **Back Up Current Profile**, and **Set Backed-Up Profile**.

**Output folder structure.** A reminder of where things go:

```
output/backups/          ← original game files (from 'Create Backup')
output/mod_output_files/  ← patched .dat files (generated on Deploy)
mods/                     ← your .rmod files (and Convert/Editor output)
```

**Accessibility.**

- **Default language** — the UI/editing language. English plus French, German, Italian, Spanish, Polish, Czech, Russian, Japanese and Simplified Chinese (and a dev option). Changing it prompts a restart. All app and editor text is driven by `lang.json` and can be extended.
- **Add Start Menu Shortcut** / **Add Desktop Shortcut** — create Windows shortcuts to the app.

---

## How updates work

The packaged exe checks GitHub for new releases on startup. When a newer version exists you're asked **Yes / No**. **No** closes the app; **Yes** downloads the new exe, replaces the current one, and relaunches. Your `mods/`, `output/`, and `settings.json` are left untouched.

If you're offline or GitHub is unreachable, the check is skipped silently and the app starts normally. (Running from source never checks for updates.)

---

## The `.rmod` format (reference)

An `.rmod` is a plain JSON file you can read or edit in any text editor.

```json
{
  "$schema": "ruse-mod/v1",
  "id":      "my-mod",
  "name":    "My Mod",
  "version": "1.0.0",
  "author":  "You",
  "description": "What this mod does",
  "patches": [
    {
      "dat": "Data/PC/190852/ZZ_GladPatchableWin.dat",
      "ndf": "genglad/patchable/clustergfx/everything.cpp.gladndfbin",
      "changes": [
        {
          "action": "patch",
          "table":  "TUniteAuSolDescriptor",
          "match":  { "ClassNameForDebug": "Unit_Stug_III_B" },
          "set": {
            "SeuilMort":      { "type": "Float32", "value": 600.0 },
            "ProductionTime": { "type": "Int32",   "value": 8 }
          }
        }
      ]
    }
  ]
}
```

### Supported actions

| Action | Description |
|---|---|
| `patch` | Find matching instance(s) and update named properties |
| `create` | Add a new instance to a class table with given properties |
| `delete` | Remove matching instance(s) from the NDF entirely |
| `delete_props` | Remove specific properties from matching instance(s) |

### Supported NDF types

All NDF binary types are fully supported in both the converter and the applier:

- **Scalars:** `Bool`, `Int8`, `Int16`, `UInt16`, `Int32`, `UInt32`, `Long`, `Float32`, `Float64`
- **Strings:** `StringRef`, `PathRef`, `WideStr`
- **Vectors & colours:** `Vector3`, `Color32`, `Color128`, `TripleInt`, `Int2`, `Float2`, `Matrix`
- **References:** `ObjRef`, `TransRef`
- **Collections:** `List<T>`, `Map<K,V>`
- **Other:** `Blob` (base64), `ZipBlob`, `Hash`, `Guid`, `LocHash`

---

## Running from source

```bash
git clone https://github.com/LittleGroove/RUSE-Mod-Manager.git
cd RUSE-Mod-Manager
pip install -r requirements.txt
python mod_manager.py
```

The auto-update check is **disabled** when running from source — only the packaged exe checks for new releases.

---

## Technical notes

- Single-file Windows exe built with PyInstaller. No Python install required to run.
- Mods are applied to a working copy of the `.dat` files; your backups are never touched.
- When multiple mods target the same `.dat`, later mods layer on top of the already-patched output — originals are never re-read mid-chain.
- The deploy cache reuses the longest unchanged prefix of your load order so re-deploys are fast.
- Shipped multiplayer-safe ("SAFE") mods are bundled inside the exe and override external mods of the same name + major version, so multiplayer stays consistent.
- All changes are logged with before/after values for every property touched.

---

## License

All rights reserved. Code is public-visible for transparency and contributions, but not licensed for reuse or redistribution. Open an issue if you'd like to use the code in a derivative project.

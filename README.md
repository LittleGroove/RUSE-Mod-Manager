# RUSE Mod Manager

A standalone Windows application that fundamentally redefines modding for **R.U.S.E.** Instead of distributing full replacement `.dat` files that break each other, mods are **surgical patch files** that describe only what they change, so multiple mods coexist and stack cleanly.

Supports both **R.U.S.E. Compat** (community multiplayer build) and the **public Steam release**.

---

## Just want to use mods? Start here (Players)

If you only want to **download mods and play**, you don't need to read the rest of this page. Follow these five steps:

### 1. Download it

Go to the **[Latest release →](https://github.com/LittleGroove/RUSE-Mod-Manager/releases/latest)** and download **`RUSE_ModManager_v<X.Y.Z>.exe`**.

That single `.exe` *is* the whole program. There is **no installer** and **nothing to set up** — Python is not required.

### 2. Put it in a folder Windows lets it write to (important)

The Mod Manager needs to **create files and folders next to itself** (it makes a `mods/` folder, a `profile/` folder, a `settings.json`, backups of your game files, etc.). If Windows blocks writing to that location, the app will fail or behave strangely.

**Do this:** make a normal folder somewhere you own and drop the exe inside it. Good places:

- `C:\Games\RUSE Mod Manager\`
- Your Desktop, e.g. `C:\Users\<You>\Desktop\RUSE Mod Manager\`
- `C:\Users\<You>\Documents\RUSE Mod Manager\`
- Any folder on another drive, e.g. `D:\RUSE Mod Manager\`

**Avoid these — Windows protects them and will block writes:**

- `C:\Program Files\` or `C:\Program Files (x86)\`
- `C:\Windows\` or anywhere inside it
- The root of your C: drive (`C:\` directly)
- Running it straight out of the `.zip` / "Downloads" preview without extracting first

> **Tip:** Give the exe its **own dedicated folder**. The program creates several files and folders right beside itself, so keeping it isolated stays tidy. You do **not** need administrator rights, and you should **not** "Run as administrator."

### 3. Run it

Double-click the exe. If Windows SmartScreen shows a blue "Windows protected your PC" box, click **More info → Run anyway** (this appears because the app isn't code-signed, not because anything is wrong).

### 4. Point it at your game (one time)

Open the **Settings** tab and set **Game Root** to your R.U.S.E. `Data/` folder. The Mod Manager automatically makes a safe backup of your original game files the first time — your originals are never lost.

### 5. Add mods and play

- Put any `.rmod` mod files you've downloaded into the **`mods/`** folder (or use the **+** button on the Mod Manager tab).
- Tick the mods you want, drag them into the order you like, and click **Deploy**.
- Changed your mind? Click **Restore Original Files** to put the game back exactly as it was.

That's it — launch R.U.S.E. and your mods are active. Everything below is reference detail for power users, modders, and developers.

---

## Download

**[Latest release →](https://github.com/LittleGroove/RUSE-Mod-Manager/releases/latest)**

Grab `RUSE_ModManager_v<X.Y.Z>.exe` for a single self-contained executable, or `RUSE_Mod_Manager_v<X.Y.Z>.zip` for the full bundle (exe + empty `mods/` folder + preset `profile/` folders).

No installer, no Python required — just unzip and run. See the **[Players quick start](#just-want-to-use-mods-start-here-players)** above for where to put the exe so Windows doesn't block it.

---

## How updates work

The Mod Manager checks GitHub for new releases on startup. When a newer version exists, you're asked **Yes / No**. Choosing **No** closes the application; choosing **Yes** downloads the new exe, replaces the current one, and relaunches. Your `mods/`, `profile/`, and `settings.json` are left untouched.

If you're offline or GitHub is unreachable, the check is skipped silently and the app starts normally.

---

## The problem with traditional RUSE mods

Classic RUSE mods replace entire `.dat` archive files. If two mods touch the same `.dat`, only one can be active — they are fundamentally incompatible. Players had to pick one or the other.

## The solution — `.rmod` files

The Mod Manager introduces the **`.rmod`** format: a small JSON file that records only the specific changes a mod makes. Rather than *"replace this entire archive"*, an `.rmod` says *"find this unit and change these two properties"*. Any number of mods can patch the same file without conflict. Load order determines who wins when two mods edit the same property.

---

## Features

### Mod Manager
- **Load order** — drag mods up/down to set priority. Later wins on conflicts.
- **Enable / disable** individual mods without removing them.
- **One-click Deploy** — applies all enabled mods in order to your R.U.S.E. install.
- **Restore Original Files** — reverts to the backed-up originals at any time.
- **Setup checklist** — two-step guided setup (Set Game Root → Create Backup) with live status indicators.
- **Share / Import Load Order** — copy your current enabled list to the clipboard; paste a shared list to sync exactly with a friend.
- **Bundled multiplayer-safe mods** — shipped mods are baked into the exe, always applied, and override any external mod of the same name. Keeps MP games consistent.

### Convert
Turns old-style mods (full `.dat` replacement) into `.rmod` patch files automatically by diffing against your original game files.

### Mod Editor
Project-based editor with per-window Save-all, faction/menu/upgrade unit editing, AI editor, economy editor, tools editor, and a map editor (zones, roads, spawns, HQs, depots, cameras).

### Settings
- Game Root directory, mods folder, working directory.
- Automatic first-time backup of game `.dat` files.
- UI language selection (English + 9 other languages, expandable via `lang.json`).

---

## Running from source

```bash
git clone https://github.com/LittleGroove/RUSE-Mod-Manager.git
cd RUSE-Mod-Manager
pip install -r requirements.txt
python mod_manager.py
```

The auto-update check is **disabled** when running from source — only the packaged exe checks for new releases.

To build the exe yourself:

```bash
pip install pyinstaller
python build.py
```

The output lands in `FINAL_output/v<X.Y.Z>/`.

---

## The `.rmod` format

An `.rmod` is a plain JSON file. You can read and edit it in any text editor.

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
      "dat": "PC/99/ZZ_GladPatchableWin.dat",
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

## Load order & conflict resolution

When two mods patch the **same property** on the **same instance**, the mod **lower** in the list wins (later overrides earlier). Everything else from both mods is applied without conflict.

Share / Import Load Order lets groups of players synchronise their exact mod list with a short text block:

```
--- RUSE Load Order ---
1. Balance Overhaul Mod | v2.1.0
2. No Artillery | v1.0.0
3. Admin Buildings 1942 | v1.0.0
--- end ---
```

The importer verifies versions and warns if a mod is missing or on the wrong version.

---

## Getting started

1. Download the latest release (link at the top).
2. Place the exe (or the unzipped bundle) anywhere.
3. Open it — in **Settings**, set **Game Root** to your R.U.S.E. `Data/` folder. A backup is created automatically.
4. Drop `.rmod` files into the `mods/` folder, or use the **+** button in the Mod Manager tab.
5. Enable the mods you want, arrange the load order, click **Deploy**.
6. To revert, click **Restore Original Files**.

---

## Technical notes

- Single-file Windows exe built with PyInstaller. No Python install required to run.
- Shipped multiplayer-safe mods are bundled inside the exe and applied automatically.
- Mods are applied to a working copy of the `.dat` files; backups are never touched.
- When multiple mods target the same `.dat`, later mods layer on top of the already-patched output — originals are never re-read mid-chain.
- All changes are logged with before/after values for every property touched.

---

## License

All rights reserved. Code is public-visible for transparency and contributions, but not licensed for reuse or redistribution. Open an issue if you'd like to use the code in a derivative project.

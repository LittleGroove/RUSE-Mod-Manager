# Mod Editor Tab — The Project Workflow

This guide covers the **Mod Editor** tab of the RUSE Mod Manager. A *mod* is a
change you make to the game. The Mod Editor keeps all your changes together in a
**project** (a folder that holds one mod you're building). This tab ties every
separate editor together — Units & Buildings, AI, Economy, Map, and Raw / Asset.

Here you'll learn how to make a mod project, open it, edit it, save it, put it
into the game to try it out, and turn it into a `.rmod` file you can share. An
`.rmod` file is a small "update mod" that holds only your changes so other
people can add them to their own game.

This guide does **not** explain each individual editor. Each of those has its own
guide:

- [Units & Buildings Editor](units-editor.md)
- [AI Editor](ai-editor.md)
- [Economy Editor](economy-editor.md)
- [Map Editor](map-editor.md)
- [Raw / Asset Editor](raw-asset-editor.md)

More to read:

- [Versions & Backups](versions-and-backups.md) — why you need a clean backup, and what a "build" is.
- [Convert Tab](convert.md) — makes the same kind of `.rmod`, but from your own mod files.
- [The rmod Format](rmod-format.md) — what's inside an exported update mod.
- [Project README](../../README.md) — the big picture.

---

## First you need a clean backup

A **backup** is a saved copy of the game's original files, before any mod
touched them. The Mod Editor never looks at your live game files. Those files
might already have a mod in them, which would quietly mess up every change you
make. Instead, the editor always reads from a clean backup.

So before you can make *or* open a project, you must have at least one clean
backup. You make one from the **Mod Manager** tab (Step&nbsp;2, "Create Backup").
Each backup is saved for one game version, in a folder like this:

```
output/backups/v<buildid>/
```

If there's no backup yet, the editor stops and shows a "Backup required"
message, and sends you to the Mod Manager tab. See
[Versions & Backups](versions-and-backups.md) to learn about game versions and
where files are kept.

---

## What a project is

A **mod project** is just a folder on your computer, kept here:

```
output/editor_mods/<name>/
```

Inside it you'll find:

| File / folder | What it's for |
| --- | --- |
| `project.json` | Basic info about the project — its name, the **game version** it was made for, and the list of game files it has copied in. |
| `description.txt` | The author, version number, and description (the **Mod Details** box). The first line is the author, the last line is the version, and everything in between is the description. The **Convert** tab reads this same file. |
| `Data/PC/<sub>/<dat>` | Copies of the main game files (`.dat` files) you've edited. `<sub>` is a number folder: `190852` for newer (remaster) versions or `99` for the older (OG/compat) version. |
| `Maps/PC/<dat>` | Copies of the per-map land files you've edited. |
| `notes.json` | (Optional) your own notes about the mod. |

The folders inside a project are laid out the same way as the game itself
(`Data/PC/…` and `Maps/PC/…`). That makes putting the mod into the game very
simple — the files just drop right into place.

### Projects start small — files are copied only when needed

A brand-new project has only one file: `project.json`. A game file is copied
into the project **only the first time you save a change to it** (and the copy
comes from the clean backup, never from the live game). So a mod that only
changes units carries just that one game file, not all 25. This keeps projects
small and makes it easy to see which files a mod actually changes.

The six main game files, and where each one goes:

| File name | What it holds | Goes into |
| --- | --- | --- |
| `ZZ_GladPatchableWin.dat` | Units, buildings, economy, AI settings, menus (gameplay) | `Data/PC/<sub>/` |
| `IA_Common.dat` | AI, mission, and challenge scripts | `Data/PC/<sub>/` |
| `ZZ_Win.dat` | Text (translations), pictures, menus | `Data/PC/<sub>/` |
| `ZZ_GladNotPatchableWin.dat` | Other gameplay data | `Data/PC/<sub>/` |
| `DataMap_Win.dat` | Map info, scenarios, and map details | `Data/PC/<sub>/` |
| `Data_Common.dat` | Videos, fonts, and XML | `Data/PC/<sub>/` |

Any other file (like a single map's minimap/world file, for example
`DataMapSuperCrossroads4_v09.dat`) is treated as a map file and goes into
`Maps/PC/`.

---

## The project-choosing screen

When you open the Mod Editor tab, you start on the choosing screen. The left
side lets you pick a project. The right side is a **Log** that shows everything
the program does. It keeps everything and is never cleared while the program is
open.

### Create New Mod

1. Type a **Mod name** in the text box. Your name is shown exactly as you type
   it. Only the folder name on disk is cleaned up (Windows won't allow a few
   special characters, so those are removed — but spaces are kept).
2. Pick the game version from the **"Game version:"** dropdown.
   - It lists **every version you have a clean backup for** — one for each backup
     folder that has files in it.
   - It **starts on your installed version** by default.
   - Because you pick the version here, **you can make a mod for a different
     version than the one your game is running.** The editor reads that version's
     clean backup for all the original files.
3. Click **Create Project** (or press Enter in the name box).

If the name is already taken by a folder that isn't empty, you'll be asked to
pick a different name or open the one that already exists. When it works, the
project is made and you go straight to the **project hub**.

> If you don't have any backups at all, Create Project can't run. It shows the
> "Backup required" message instead. Make a backup first.

### Load Existing Mod

The list below shows every folder under `output/editor_mods/`. Folders that
aren't real projects are marked `(no project.json)`.

- **Load Selected** — opens the project you clicked. (You can also
  **double-click** a project to open it.)
- **Refresh** — checks the folder again for new projects (and also updates the
  version dropdown above).
- **Browse Folder…** — **opens the mod-projects folder in your computer's file
  explorer.** This doesn't pick a project to open — it just shows you your
  projects on disk so you can manage them yourself.

When you open a project, the editor reads clean files from the backup that
matches **that project's game version** (saved in its `project.json`), which may
not be the version your game is running.

#### Opening very old projects

Very old projects saved a branch *name* (like `compat` or `public`) instead of a
game version number. When you open one, the editor asks you to pick the game
version it was built for, then saves that version number. If the version you pick
uses the same data format, the project is simply relabeled. But if it would mean
jumping between the old (OG) and newer (remaster) format, the project is left
alone and you're told to *convert* the mod instead of relabeling it (otherwise
the project's files wouldn't be found).

---

## The project hub

Making or opening a project brings you to the **hub** — the project's home
screen.

### Header

- **Mod: \<name\>** — the project name, shown in gold.
- **Save status** (top-right):
  - `✓ all changes saved` (green) when nothing is waiting to be saved.
  - `● N unsaved change-set(s)` (gold) when N of your edits haven't been written
    into the mod's files yet.
- Under the header is the folder path where the project lives.

### Mod Windows

A box holds the row of buttons that open each editor:

- **Units & Buildings** → see [Units & Buildings Editor](units-editor.md)
- **Map Editor** → see [Map Editor](map-editor.md)
- **AI** → see [AI Editor](ai-editor.md)
- **Economy** → see [Economy Editor](economy-editor.md)
- **Raw / Asset Editor** → see [Raw / Asset Editor](raw-asset-editor.md)

If the window is narrow, these buttons wrap onto new lines so none of them get
cut off. A note under the row reminds you: *each window has its own Save button —
it saves every change you've made into the mod's files.*

To the **right** of the button row is the **Add .dat files…** button.

### Add .dat files…

Use this to bring **already-built** game files into the project — for example,
files made by another tool that you want to keep editing here.

1. Click **Add .dat files…**.
2. Pick one or more `.dat` files (the picker opens at your Game Root, or the
   projects folder if you haven't set a game root).
3. Each file you pick is sorted **by its file name**:
   - If the name is one of the six main game files (`ZZ_GladPatchableWin.dat`,
     `IA_Common.dat`, `ZZ_Win.dat`, `ZZ_GladNotPatchableWin.dat`,
     `DataMap_Win.dat`, `Data_Common.dat`) it goes into `Data/PC/<sub>/`.
   - Anything else is treated as a map file and goes into `Maps/PC/`.

   Because these names are one-of-a-kind, you don't have to browse to any folder.
4. A **confirmation box** shows where each file will go (`name → path`) and warns
   you about any that would **replace** a file already in the project. The `<sub>`
   folder is picked based on the project's game version.
5. When you confirm, the files are copied in and the project is **reloaded** so
   the editors use them right away.

---

## Saving your work

There is **no save button on the hub itself.** Each editor window has its own
**"Save mod (.dat)"** button. Pressing it saves the whole project — it writes
*every* change from *every* window into the mod's files. In other words, it's the
same save no matter which editor you press it from.

- The first time you save a change to a game file, the clean copy is brought into
  the project, and then your edits are written into that copy.
- The hub's save status changes to show whether work is waiting or already saved.
- **Deploy** and **Convert** both refuse to run while you have unsaved changes,
  and tell you to save in the editor window first.
- **Close Project** warns you if you're about to throw away unsaved changes.

### Mod Details (description.txt)

The bottom-left of the hub is the **Mod Details** box (author, version, and
description). It writes the `description.txt` file. If you leave the version field
blank or type it wrong, it snaps to the `x.x.x` shape (and defaults to `1.0.0`).
**Save Details** turns on whenever a field is different from what's saved on disk.
These are the details that go into an exported rmod.

---

## Deploy to Game

**Deploy to Game** copies the project's saved files into your **live game** so
you can play with the mod turned on.

Here's what happens, step by step:

1. The project must have a Game Root set (in Settings) and **no unsaved changes**.
2. You must have saved at least one change (so there's a project file to copy).
   If not, you're told to make a change first.
3. A **version check** runs (see the warning below).
4. You confirm a box listing the live game files that will be overwritten.
5. For each file:
   - The **original is backed up first**, with the date and time in the name
     (`<name>.<stamp>.bak`) under `output/backups`.
   - If an earlier deploy left some files changed that this mod *doesn't* use,
     those are put **back to clean** (read from this version's backup), so old
     leftover changes from another mod don't stick around.
   - The project's file is copied over the live game file.

Deploy reads from the backup that matches **this project's game version**, so the
clean reads and the leftover cleanup come from the right version's originals —
not necessarily your installed version's.

> [!WARNING]
> **Version mismatch warning.** If your installed game version is different from
> the project's version, Deploy warns you:
>
> *"This mod project targets **X**, but your game is currently set to **Y**.
> Deploying writes **X** files into a **Y** game, which may not load correctly.
> Switch your game to **X** in Steam to test it. Deploy anyway?"*
>
> Files made for one version may not load in another. To test a mod for a
> different version, switch your game to that version in Steam first (see
> [Versions & Backups](versions-and-backups.md)), then deploy.

---

## Convert to rmod

**Convert to rmod** makes a shareable **update mod** that holds **only your
changes** (found by comparing your files against the clean originals). It's saved
as an `.rmod` file with a version number in its name, here:

```
mods/v<buildid>/
```

Details:

- It uses the **same engine as the Convert tab** — version numbering in the name
  (`_V#` / `-v#`), spotting which version it's for, and comparing against the
  clean originals.
- The author, version, and description come from the **Mod Details** box. If you
  have unsaved Mod Details, you're offered the chance to save them to
  `description.txt` first so they go into the rmod.
- Just like Deploy, it needs **no unsaved changes** and at least one saved
  project file.

See [Convert Tab](convert.md) and [The rmod Format](rmod-format.md) to learn what
the output holds and how it's used.

---

## Editor windows: one at a time, with a Back bar

Opening any Mod Window fills the main area as a view inside the tab — you see one
window at a time. A **← Back** bar shows at the top with the view's title. Click
**← Back** to leave the current view and go back to the one under it, or to the
hub.

The hub itself is *not* one of these views — it keeps its own **Close Project**
button instead of a Back bar.

---

## Close Project

**Close Project** (on the hub) takes you back to the project-choosing screen.

If you have unsaved changes, you're asked to confirm — closing throws away any
edits you haven't saved yet through an editor window's Save button. Save first if
you want to keep them.

---

## Quick recap

1. Make a clean backup (Mod Manager tab).
2. **Create New Mod** — name it and pick the game version.
3. Open editor windows, make changes, and press **Save** (any window — it saves
   everything).
4. Fill in **Mod Details** (author, version, description).
5. **Deploy to Game** to test it (watch the version mismatch warning), or
   **Convert to rmod** to make a shareable update mod.
6. **Close Project** when you're done.

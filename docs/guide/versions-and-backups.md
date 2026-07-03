# Versions & Backups

This page explains how the RUSE Mod Manager figures out which game version you
have, where it keeps backups and mods, and why you need a clean backup before you
edit or deploy anything.

A **backup** is a saved copy of the game's original files, before any mod touched
them. "Deploy" means putting a mod into your live game so you can play it.

> **Short version** — The Mod Manager sorts *everything* by the game's Steam
> **build id** (a number Steam gives each set of game files). Backups live in
> `output/backups/v<buildid>/`, mods live in `mods/v<buildid>/`, and a clean
> backup of your `Data/` and `Maps/` folders is **required** before you edit or
> deploy. The editors and the mod-installer always read the clean original files
> from the backup, never from your live game (which might already be modded).

See also: [Mod Manager](mod-manager.md) · [Convert](convert.md) ·
[Profiles](profiles.md) · [back to project README](../../README.md)

---

## What a "build" is

R.U.S.E. comes from Steam. Every time the game's files change on Steam, Steam
stamps the install with a number called the **build id**. Think of it as a name
tag that means "exactly these game files."

A Steam **branch** (Steam sometimes calls it a "beta" in its menus) is just a
*pointer* to whatever build is on that branch right now. The same branch can
point to different builds over time. And here's the tricky part: **branches are
not in the same order as build ids.** For example, `compat-3` is build
`v23660935`, which is a *lower* number than `compat-2`'s build `v23661872`. That's
because `compat-3` was a fix the developers put out *after* `compat-2`. If the
tool went by branch names, this kind of shuffling would confuse it.

That's why the Mod Manager goes by the **build id**, not the branch name:

- The build id points to exactly one set of game files, and never changes.
- Backups, mod versions, and version-update maps all hang off the build id.
- The branch name is kept only to **show you** (so you can check you picked the
  branch you meant), like `compat-2 (v23661872)`.

---

## Known builds

The tool ships with a small list matching each known build id to its branch name
and data-version. Most people don't have the giant developer copies of the game,
so this built-in list is how the app shows branch names and finds the right data
folder for any build.

| Branch    | Build id     | Folder name  | Data-version | rmod extension  |
|-----------|--------------|--------------|--------------|-----------------|
| compat    | `v3591`      | `v3591`      | `99`         | `.compat.rmod`  |
| compat-2  | `v23661872`  | `v23661872`  | `190852`     | `.rmod`         |
| compat-3  | `v23660935`  | `v23660935`  | `190852`     | `.rmod`         |
| compat-4  | `v23738184`  | `v23738184`  | `190852`     | `.rmod`         |
| public    | `v23762668`  | `v23762668`  | `190852`     | `.rmod`         |

Notes:

- **compat (`v3591`)** is the original Ubisoft version (data-version `99`). It's
  its own separate family, and its mods use a special `.compat.rmod` name.
- **compat-2 and later** are the Steam remaster family. They all use data-version
  `190852` and the plain `.rmod` name.
- **public (`v23762668`)** is the current Steam release.
- Notice again that `compat-3` (`v23660935`) has a *lower* build id than
  `compat-2` (`v23661872`) — branches aren't in build-id order. Always go by the
  build id in the folder name, never the branch label, when matching files.

If you have a build that isn't in this list, the tool still works. It uses the
plain build id for folder names and figures out the data-version straight from
your install (explained below). It just can't show a friendly branch label for
that build.

---

## How the tool finds your version

When you set your **Game Root** in Settings (your
`…/steamapps/common/R.U.S.E` folder), the Mod Manager checks a Steam file to
find the build id. R.U.S.E.'s Steam app number is `21970`, so that file is named
`appmanifest_21970.acf`. It usually sits two folders up from the game root
(`…/steamapps/appmanifest_21970.acf`). The app pulls the build id out of it.

The tool also works out the **data-version** — which format of game files you
actually have on disk. This works even if your build isn't in the built-in list:

- If the folder `Data/PC/190852/` exists → data-version `190852` (the remaster).
- Otherwise → data-version `99` (the original Ubisoft version).

This split between `99` and `190852` also decides the OG-vs-remaster look of the
app and which mod name is used (`.compat.rmod` for `99`, `.rmod` for `190852`).

If no build id can be found (for example, a non-Steam copy with no Steam file),
the app falls back to the branch name (`compat` or `public`) so the basics still
work.

---

## Where backups and mods are kept

Everything is sorted **by build id** inside your working folder.

### Backups

```
output/
└── backups/
    ├── v3591/            ← OG compat (data-version 99)
    ├── v23661872/        ← compat-2
    ├── v23660935/        ← compat-3
    ├── v23738184/        ← compat-4
    └── v23762668/        ← public
```

Each build's backup folder is laid out just like your game root, so inside a
backup you'll find the original `Data/` and `Maps/` folders:

```
output/backups/v23762668/
├── Data/
│   └── PC/
│       └── 190852/
│           ├── <…>.dat
│           └── …
└── Maps/
    └── PC/
        ├── <…>.dat            ← each map's world / minimap files
        └── …
```

(For the OG build, the data files sit under `Data/PC/99/` instead of
`Data/PC/190852/`.)

### Mods

```
mods/
├── v3591/            ← .compat.rmod files for OG compat
├── v23661872/        ← .rmod files for compat-2
├── v23660935/        ← compat-3
├── v23738184/        ← compat-4
└── v23762668/        ← .rmod files for public
```

Mod folders are made when they're first needed for a build. Mod **projects** also
save the build id they were made for (in their `project.json`), so a project
always knows which game's files it was built against.

---

## Create Backup — what it copies and why you need it

The **Create Backup** action (Step 2 in the setup checklist) copies your whole
`Data/` and `Maps/` folders from your game root into
`output/backups/v<buildid>/`, keeping the same folder layout. Because the `Maps/`
folder has big map files in it, the backup can take a while and use a fair bit of
disk space.

A clean backup is **REQUIRED** before you can edit or deploy:

- The **editors** (units, menus, upgrades, AI, maps) read the clean original game
  files from the backup — never from your live install. If you edited the live
  files directly, the backup is your record of "what the game shipped with."
- The **mod-installer** also builds your mod onto the *clean* backup copies and
  then writes the result into the game, instead of patching files that might
  already have another mod's changes in them.

That's the whole point: mods must be layered onto known-clean files, or you get
mistakes stacking on top of mistakes. The Mod Manager makes sure of this — it
won't let you deploy without a backup that actually has game files in it, and the
editors need one too.

> **Make your backup on a *clean* game.** If you've already put mods into your
> install, restore or re-verify a clean install *before* making the backup. A
> backup made over a modded game copies the mod, not the originals — and then
> every editor and deploy would read from those wrong files.

---

## Restore Clean

**Restore Clean** copies the original files back out of the backup and over your
live install, removing any mods you deployed. It restores only the `Data/` and
`Maps/` folders from the backup (loose files at the top of the backup are
ignored), clears the tool's list of deployed files, and removes the temporary
mod-output folder, so the game is left completely clean.

Use this whenever you want to get back to a stock (unmodded) game — for example,
before checking a fresh backup, or when you're done testing mods.

---

## Several builds at once

Because everything is sorted by build id, you can keep clean backups for
**several builds at the same time** — they sit side by side under
`output/backups/`. This is handy if you switch between Steam branches (compat,
compat-2/3/4, public) or work on mods for more than one build.

When you make a mod project, the version dropdown lists *every* build you have a
backup for — not just the one installed right now. A backup only "counts" if its
folder actually has game files in it, so an empty or half-finished folder won't
show up. This lets you make a mod for any backed-up build, then switch Steam to
that build to test it.

---

## Old `compat` / `public` folders

Older versions of the tool kept backups and mods in branch-named folders
(`output/backups/compat/`, `output/backups/public/`, `mods/compat/`,
`mods/public/`) instead of build-id folders. The current build-id system does
**not** touch these:

- The app **never auto-deletes or moves your backups or mods** — old branch-named
  folders are left exactly where they are.
- To move them over, take your old `.rmod` / `.compat.rmod` files out of the old
  `mods/compat` or `mods/public` folder and drop them into the matching build-id
  folder (`mods/v<buildid>/`). The manager then stamps that build id onto them.
  Moving them is always something *you* choose to do, and nothing is destroyed.

This is a safety rule on purpose: if a backup gets destroyed, the editors and the
mod-installer have no clean files to read from. So the tool always leans toward
keeping your files safe.

---

## Data-version `99` vs `190852` (and the two rmod names)

The data-version is the `Data/PC/<dataver>/` folder your game files live in, and
it neatly splits the two game families apart:

| Data-version | Family                     | rmod extension  | Build(s)              |
|--------------|----------------------------|-----------------|------------------------|
| `99`         | OG / original Ubisoft build| `.compat.rmod`  | compat (`v3591`)       |
| `190852`     | Steam remaster             | `.rmod`         | compat-2/3/4, public   |

What this means for you:

- The OG compat family uses the **`.compat.rmod`** name. Everything from compat-2
  onward uses the plain **`.rmod`** name. The two are *not* swappable — they're
  built for game files with different insides.
- In OG (compat) mode, the file pickers and mod list show only `.compat.rmod`
  files. In remaster (public) mode they show `.rmod` files, with an optional
  switch to also show `.compat.rmod` files.

---

## See also

- [Mod Manager](mod-manager.md) — deploying, the active-mod list, and launching.
- [Convert](convert.md) — turning a mod project into a deployable `.rmod`.
- [Profiles](profiles.md) — managing more than one mod loadout.
- [Project README](../../README.md) — top-level overview.

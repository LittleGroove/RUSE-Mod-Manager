# Profiles

Back up, restore, and save your R.U.S.E. **career/campaign progress** — one copy
per game version — from the Mod Manager's **Settings** tab.

> **Quick version** — R.U.S.E. keeps all your single-player progress in one file
> called `PROFILE.ruse`. It lives inside Steam's `userdata` folders. The Mod
> Manager can copy that file out to keep it safe (a *backup*, saved separately for
> each game version), and copy a saved one back in (a *restore*). It can also drop
> in two ready-made **level presets** (lvl 1 and lvl 100). These come from the old
> "compat" version, but because an *older* profile safely upgrades to a newer game
> (the game rebuilds it when you launch), they work on **any** version. For the
> same reason — a newer profile can break an *older* game — the restore feature
> only ever offers profiles from your current version or older.

See also: [Versions & Backups](versions-and-backups.md) ·
[Settings](settings.md) · [back to project README](../../README.md)

---

## What `PROFILE.ruse` is

`PROFILE.ruse` is R.U.S.E.'s single-player save. It holds your campaign progress,
career rank and unlocks, career money and state, settings, and (on the original
game) the linked Ubisoft account. There is **one** profile file per Steam account,
and the game reads and writes it while you play.

The Mod Manager never changes what's inside this file — it only **copies** it: out
of the game (backup) and back in (restore or preset). Restoring, or putting in a
preset, **replaces** your current profile. So make a fresh backup first if you want
a safety net before you experiment.

---

## Where the game keeps profiles

R.U.S.E.'s Steam app number is **21970** (the original / "compat" listing).
Profiles live in Steam's per-account `userdata` folders:

```
<Steam>/userdata/<account-id>/21970/local/PROFILE.ruse
<Steam>/userdata/<account-id>/21970/remote/PROFILE.ruse
```

`<Steam>` is where Steam is installed (found for you — usually
`C:\Program Files (x86)\Steam`). `<account-id>` is the number for each Steam
account that has signed in on this PC. The Mod Manager checks **every** account's
`21970` folder. It uses both the `local` and `remote` sub-folders when they exist
(`remote` is the Steam Cloud copy). It handles all of them together. So a backup
grabs your real profile, and a restore writes to every place the game might read
from.

> If there is no `21970/{local,remote}` folder, the game has never made a profile
> on this PC under any signed-in account. The profile actions then say
> "No Steam R.U.S.E. profile directories found."

---

## Where the Mod Manager keeps backups and presets

Everything the Mod Manager ships or saves goes in the app's `profile/` folder,
sorted by the game's Steam **build id** (the same version id used for backups and
mods — see [Versions & Backups](versions-and-backups.md)):

```
profile/v<buildid>/PROFILE.ruse     ← your backed-up profile for that version
profile/v3591-lvl1/PROFILE.ruse     ← ready-made OG-compat level-1 preset
profile/v3591-lvl100/PROFILE.ruse   ← ready-made OG-compat level-100 preset
```

The OG Compat version is **`v3591`**. The newer versions (compat-2, compat-3,
compat-4, and the public re-release) each get their own `profile/v<buildid>/`
folder the first time you back up while that version is the one it finds.

---

## The four buttons (Settings → Profile)

![The profile buttons in the Settings tab](../../screenshots/settings/profile.png)

*The profile buttons in Settings.*

The profile buttons are in the **Profile** group on the right of the Settings tab.
Whether they can be clicked depends on what the Mod Manager has found:

| Control | You can use it when | What it does |
|---|---|---|
| **Set lvl 1 Profile** | a game root is set (**any** version) | Puts the ready-made `v3591-lvl1` preset into your Steam profile folder(s). |
| **Set lvl 100 Profile** | a game root is set (**any** version) | Puts the ready-made `v3591-lvl100` preset in. |
| **Back Up Current Profile** | a game root is set | Copies your live `PROFILE.ruse` into `profile/v<buildid>/`. |
| **Set Backed-Up Profile** + dropdown | a *usable* backup exists | Copies a saved profile back into your Steam profile folder(s). |

A *game root* is the game's install folder that the Mod Manager found, or that you
set on the Settings tab. If no game is found, all the profile buttons are turned
off.

---

## Level presets (work on any version)

**Set lvl 1 Profile** and **Set lvl 100 Profile** drop a ready-made profile into
your Steam folders. These are the OG-compat version's career presets that come with
the Mod Manager. But because an older profile upgrades forward, they work on
**any** installed version (on a newer version the game rebuilds the preset for you
the next time you launch):

- **lvl 1** — `profile/v3591-lvl1/PROFILE.ruse` — a fresh, low-level career start.
- **lvl 100** — `profile/v3591-lvl100/PROFILE.ruse` — a maxed-out career.

Both buttons only turn on when the version it finds is **OG Compat** — the presets
exist only for that version (`v3591`). If the preset file is missing, the Mod
Manager says "Profile Not Found" and shows where it looked. If it can't find the
Steam profile folders, it says "Steam Not Found." When it works, it lists every
folder the preset was copied to.

> Putting in a preset **replaces** your current profile in every Steam profile
> folder. Back up first if you want to keep your progress.

---

## Back Up Current Profile

**Back Up Current Profile** saves a copy of your live profile for the version the
Mod Manager currently finds:

1. It checks all your Steam `21970/{local,remote}` folders for `PROFILE.ruse` and
   picks the **most recently changed** one (your real, active profile).
2. It copies that file to `profile/v<buildid>/PROFILE.ruse`, making the
   `v<buildid>` folder if it isn't there yet.
3. It shows the friendly version name (like *compat-2 (v23661872)*) plus where the
   file came from and where it went.

Because backups are sorted by version, you keep a **separate saved profile for each
game version**. Backing up while on compat-2 never writes over your compat-3 saved
profile, and so on. Backing up the same version again writes over that version's
saved profile with your latest progress.

---

## Set Backed-Up Profile + the Auto / version dropdown

**Set Backed-Up Profile** puts a saved profile back into your Steam folder(s). The
dropdown next to it picks *which* saved profile to use. It only turns on when at
least one **usable** backup exists.

### What "usable" means — the older-goes-to-newer rule

The dropdown never offers a profile from a version **newer** than the one you have
installed. Here's why, based on how R.U.S.E. handles profiles across versions:

- An **older** profile **upgrades to a newer game just fine**. The first time you
  launch, the game rebuilds the profile for the new version and removes the old
  Ubisoft-account data the original game stored. This makes restoring an old
  profile a safe, easy way to carry your progress forward.
- A **newer** profile may **not** work on an **older** game. Its format can be
  ahead of what the older game understands.

So the Mod Manager goes through the versions in release order (oldest to newest),
takes the current version plus **everything older**, and lists only the versions
that actually have a `profile/v<buildid>/PROFILE.ruse` saved. The list shows the
**newest first**, so the current version (if it has a backup) is at the top.

### Auto vs. a specific version

The dropdown starts on **Auto**. You can also pick a specific saved version from
the list.

- **Auto** uses the **newest usable** backup:
  - If your **current version** has a backup, that one is used **right away** (no
    pop-up) — it's an exact match for your game.
  - If not, Auto uses the **most recent older** backup and asks you to
    **confirm**, because the game will upgrade that profile forward the next time
    you launch.
- **A specific version** uses that exact backup **right away, with no upgrade
  pop-up** — you picked it on purpose. If the version you picked matches your
  current game, it's an exact restore. If it's older, the success message reminds
  you the game will rebuild it the next time you launch.

In every case, the chosen `PROFILE.ruse` is copied into **every** Steam
`21970/{local,remote}` folder, and the result message lists each place it went (and
any that failed).

### Example

Say you have backed up profiles for **compat-2 (v23661872)** and
**compat-3 (v23762668)**, and your install order (oldest to newest) is
`OG (v3591) → compat-2 → compat-3 → compat-4`.

- **Installed version = compat-3, dropdown = Auto** → compat-3 has a backup, so it
  is used **right away** (exact match).
- **Installed version = compat-4, dropdown = Auto** → compat-4 has *no* backup, so
  Auto uses **compat-3** (the newest older one) and **asks you to confirm** the
  upgrade. compat-4's own (missing) backup and any newer ones are never offered.
- **Installed version = compat-4, dropdown = compat-2 (v23661872)** → compat-2 is
  used **right away** (no pop-up); the message notes the game will rebuild it for
  compat-4 the next time you launch.
- **Installed version = compat-2** → the dropdown lists Auto + compat-2 only.
  compat-3 is **newer** than your game, so it's left out — you can't put a newer
  profile on an older game.

---

## A bit of history: the Ubisoft login

The original R.U.S.E. (OG Compat, `v3591`) needed a **Ubisoft account login**, and
that account link is stored inside its `PROFILE.ruse`. When you move to a newer
version, the game **rebuilds the profile and removes that old Ubisoft data the
first time you launch**. That's exactly why moving forward is safe — and why this
feature is a handy way to **carry an old compat profile into a newer version**
without grinding your career all over again. Back up your OG profile once, switch
versions, and let **Set Backed-Up Profile** (Auto or a specific version) carry it
across.

---

## Quick how-tos

**Save each version's progress**
1. Settings tab → make sure your game is found.
2. Click **Back Up Current Profile** → saved to `profile/v<buildid>/`.
3. Do it again after switching versions — each version keeps its own saved profile.

**Carry an old profile to a new version**
1. Install or switch to the newer version (let the Mod Manager find it).
2. **Set Backed-Up Profile** with the dropdown on **Auto** (or pick the specific
   older version) → confirm the upgrade pop-up.
3. Launch R.U.S.E. once so the game rebuilds the profile for the new version.

**Try a level preset (OG Compat)**
1. **Back Up Current Profile** first if you want to keep your progress.
2. Click **Set lvl 1 Profile** or **Set lvl 100 Profile**.

---

## Troubleshooting

- **All profile buttons are turned off** — no game root is set or found. Find or set
  your game on the Settings tab first.
- **Level preset buttons are turned off** — the version it found isn't OG Compat.
  The lvl 1 / lvl 100 presets come only for `v3591`.
- **"No Steam R.U.S.E. profile directories found."** — there is no
  `userdata/<id>/21970/` profile folder. Launch R.U.S.E. once (signed into Steam)
  so it makes a profile, then try again.
- **"No applicable backed-up profile…"** — you have no backup for the current
  version *or any older* version. Back up first, or switch to a version that has
  one.
- **The restore didn't seem to work** — the game writes the profile while it's
  running. Close R.U.S.E. before you restore, then start it again.

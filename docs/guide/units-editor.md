# Units & Buildings Editor

The **Units & Buildings editor** is a window inside the Mod Editor. Use it to
change how the units and buildings in your mod behave in the game. Every change
you make is held in your project until you click **Save**. When you save, the
whole project is written to disk as one game data file.

You open it from the [Mod Editor hub](mod-editor.md) when you have a project
open. It works on the same project as the [Economy editor](economy-editor.md).
For a big-picture look at the Mod Manager and how mod projects work, see the
[project README](../../README.md).

---

> ## ⚠️ Read this first — what does NOT work yet
>
> Two things are **not finished yet — please don't use them**:
>
> - **"Duplicate this unit"** (making a copy of a unit).
> - **Changing a unit's faction/nation** — either with the **"Migrate to
>   nation…"** button *or* by typing a new `Nationalite` value by hand.
>
> These do not make a unit that actually works in the game yet.
>
> **Editing units that already exist works fine.** You can safely change their
> stats, weapons, in-game names, upgrade chains, and where they show up in a
> building's menu — as long as you keep the unit in its own faction.
>
> The Duplicate and Migrate buttons are still there (and the `Nationalite` field
> is still shown) so the team can test them. But treat any unit you copy or move
> to another nation as broken in-game for now.

---

## Window layout

The editor has two tabs:

| Tab | What it's for |
| --- | --- |
| **Units & Buildings** | Find a unit or building. Change its stats, where it shows up in a build menu, its upgrade chain, its in-game name, and which ammo each of its weapons fires. |
| **Ammo** | Every set of weapon stats (called `TAmmunition`). These are shared, and each one is labelled with the units that use it. |

At the bottom of the window (shared by both tabs) you'll find:

- The **Save mod (.dat)** button — writes ALL your changes to the mod's game
  data file.
- A small status message (on the right) that shows how the last save went.
- A helpful note:
  > *"Apply commits the current selection's edits into the project. "Save mod
  > (.dat)" writes ALL accumulated changes to the mod's .dat. Weapon stats are
  > shared — editing one affects every unit that uses it."*

### Apply vs Save — the two-step model

There are two steps to saving your work:

1. **Apply** (a button next to each unit or ammo) locks in the edits for the
   item you have selected right now. This just holds them in memory — nothing is
   written to disk yet.
2. **Save mod (.dat)** writes every change you've made across the whole project
   to the mod's game data file. Save first Applies whichever tab has an edit
   waiting, so you won't lose an edit you forgot to Apply.

If there's nothing new to save, **Save mod (.dat)** tells you *"No pending
changes to save."* If saving fails (for example, the game can't find the
original game file), the error reminds you to set the **Game Root** in Settings.

---

## Tab 1 — Units & Buildings

### What's in the list

Each row is one unit or building. The editor shows four kinds:

| Kind | Label | Type |
| --- | --- | --- |
| `TUniteAuSolDescriptor` | Ground | unit |
| `TAvionDescriptor` | Air | unit |
| `TInfanterieDescriptor` | Infantry | unit |
| `TBatimentDescriptor` | Building | building |

The list is sorted by **nation, then category, then name**. Each row starts with
a letter in brackets to tell you the kind: `G`round, `A`ir, `I`nfantry, or
`B`uilding. Next comes the in-game name (if a name file is loaded), then the
unit's internal code name. If a row is too long, a scrollbar at the bottom lets
you scroll sideways. The count at the top-right shows `X of Y` — how many rows
match your filters out of the total.

### Finding a unit — the three filters

The toolbar has three filters. They all work together (a row must match all of
them).

#### 1. Faction

A dropdown: **All, US, Germany, France, UK, USSR, Italy, Japan**. Faction is the
nation a unit belongs to, stored as a number called `Nationalite`:

| `Nationalite` value | Faction |
| --- | --- |
| (none set) | US |
| 1 | Germany |
| 2 | UK |
| 3 | France |
| 4 | Italy |
| 5 | USSR |
| 6 | Japan |

US units (and neutral things) have no `Nationalite` value at all. When you
change the faction, the **Building / Type** dropdown refreshes to show that
faction's buildings and resets to *"All buildings"*.

#### 2. Building / Type

This dropdown answers the question *"which units show up in this building's
menu?"*. It has three groups of choices:

- **All buildings** — no building filter.
- **Building types** — for example *"Any Armor — all factions (TB=10)"*: every
  faction's units of one building type. This is combined with the Faction box if
  you also pick a faction.
- **Specific buildings** — for example *"Germany: ExperimentalFactoryGR
  (TB=12)"*: one exact factory (a nation plus a building type). This lets you
  tell apart two factories that share the same type — for example
  `ExperimentalFactoryGR` and `Usine_Atomique_GER`, which are both `TB=12`.

A unit shows up under a building when **both** of these are true:

```
unit.Nationalite == building.Nationalite  AND  unit.Factory == building.TypeBatiment
```

In other words, the unit and building must be the same nation, and the unit's
`Factory` must match the building's type. Only real factories appear (a building
must have a proper `TypeBatiment` number; empty placeholder buildings are
skipped). Buildings themselves never show up under a build-menu filter, since
they aren't *in* a menu.

#### 3. Name (localisation language)

This dropdown only shows up **if the loaded data includes at least one language
of in-game names** (the `baseunite.dic` name file). It picks which language's
in-game names appear in the list next to the code names. It starts on your
Settings language.

#### 4. Search

A text box. It matches **either** the unit's internal code name **or** its
in-game name. The list filters as you type.

Here are the build-menu category names used throughout the editor:

| `Factory` / `TypeBatiment` value | Category |
| --- | --- |
| 8 | Barracks |
| 9 | Airfield |
| 10 | Armor |
| 11 | Anti-Tank |
| 12 | Prototype |
| 13 | Artillery & AA |
| 3 | Turret / Defense |
| (building) | Buildings |
| (other) | Other |

---

### The detail panel

When you click a row, you see a header (the unit's code name) and a line under
it shaped like this:

```
<class> · <nation> (Nationalite=<raw>) · <category> · instance #<idx>
```

The raw `Nationalite` number is shown next to the friendly nation name so you
can double-check exactly what's set on the unit.

Three buttons sit under the header:

- **Apply changes to this unit** — locks in this panel's edits into the project.
- **Duplicate this unit** — ⚠️ see the warning above; don't rely on it.
- **Migrate to nation…** — ⚠️ see the warning above; don't rely on it.

Below that is a scrollable panel of fields, split into sections.

#### Display name (in-game)

You edit this **one language at a time**. In-game names aren't stored with the
unit's stats — they live in a separate name file (`baseunite.dic`, one per
language) inside `ZZ_Win.dat`. Each name is matched to a unit by the unit's
**`NameInMenuToken`** (a special name tag).

This section changes depending on what's available:

- If `ZZ_Win.dat` is **not** in the project: *"Name editing needs ZZ_Win.dat
  (not in this mod's sources)."*
- If the unit has **no** `NameInMenuToken`: *"(this descriptor has no
  NameInMenuToken)."*
- If no name exists in any name file for this unit: *"(name not found in
  baseunite.dic for this unit)."*

Otherwise you get:

- **Unit name** row — shows the current name and gives you a box to type a new
  one for the chosen language.
- **Language** row — a dropdown of the languages you can edit (starting on your
  Settings language).

**To set names in more than one language:**

1. Pick a language.
2. Type the name.
3. Pick another language and type its name.
4. Click **Apply changes to this unit**.

As you switch languages, the editor remembers what you typed for each one. When
you click **Apply**, every language you changed is written into the mod's
`ZZ_Win.dat`. Changing only a name doesn't touch the gameplay data file — just
the name file. After you Apply, the unit list refreshes to show the new name.

#### Unit / building stats

A table with three columns: **Field | Current | New value**. Here are the fields
you can edit:

| Field (label) | Property | Type |
| --- | --- | --- |
| HP (SeuilMort) | `SeuilMort` | scalar |
| Pinned threshold (SeuilPinned) | `SeuilPinned` | scalar |
| Speed (VitesseLineaire) | `VitesseLineaire` | scalar |
| Combat speed (VitesseCombat) | `VitesseCombat` | scalar |
| Acceleration (MaxAcceleration) | `MaxAcceleration` | scalar |
| Deceleration (MaxDeceleration) | `MaxDeceleration` | scalar |
| U-turn time (TempsDemiTour) | `TempsDemiTour` | scalar |
| Road speed bonus (SpeedBonusOnRoad) | `SpeedBonusOnRoad` | scalar |
| Vision range (DetectionBase) | `DetectionBase` | scalar |
| Air vision (PorteeVisionVolant) | `PorteeVisionVolant` | scalar |
| Radar signature (SignatureRadar) | `SignatureRadar` | scalar |
| Air attack range (PorteeAttackReflexAir) | `PorteeAttackReflexAir` | scalar |
| Ground attack range (PorteeAttackReflexSol) | `PorteeAttackReflexSol` | scalar |
| Build time (ProductionTime) | `ProductionTime` | scalar |
| Price per game mode (ProductionPrice) | `ProductionPrice` | list |
| Build menu (Factory) | `Factory` | factory |
| Menu slot (PositionInMenu) | `PositionInMenu` | scalar |
| Show in game mode (ShowInMenu) | `ShowInMenu` | boollist |
| Upgrade price (UpgradePrice) | `UpgradePrice` | scalar |
| Upgrade time (UpgradeTime) | `UpgradeTime` | scalar |
| Building type (TypeBatiment) | `TypeBatiment` | scalar |
| Build menu id (Menu) | `Menu` | scalar |

A field only shows up if the selected unit or building actually has it. Anything
it doesn't have is simply left out (so, for example, a building won't show
fields that only make sense for units). The rows have alternating shading so
they're easier to read.

**Lists with one value per game mode (`ProductionPrice`, `ShowInMenu`).** These
have 5 values, one for each game mode:

| Index | Game mode |
| --- | --- |
| 0 | 1945 |
| 1 | 1942 |
| 2 | 1939 |
| 3 | Total War |
| 4 | Nuclear War |

- **Price per game mode (`ProductionPrice`)** — type five whole numbers,
  separated by commas or semicolons, one per mode. If you type a **single**
  number, it's used for **all five** modes.
- **Show in game mode (`ShowInMenu`)** — five `0`/`1` flags. The label spells
  out the order: `[1945,1942,1939,TotalWar,NuclearWar]`. `1`, `true`, `yes`, or
  `on` means shown. Typing one value sets all five.

**The Build menu (`Factory`) dropdown.** This is the important field for putting
a unit in a building's build menu. It's not a text box — it's a dropdown that
**lists the real buildings in the unit's own nation**. Picking a building sets
the unit's `Factory` to match that building's type.

The **Current** value for this row is extra helpful. It shows not just the raw
number and category, but *which real building(s) actually host this unit*, like
this:

```
12 (Prototype): ExperimentalFactoryGR, Usine_Atomique_GER
```

Or `(no matching building)` if nothing hosts it. Seeing more than one building is
normal (a nation plus a type can match several buildings). This is the fastest
way to figure out *"why isn't my unit showing up where I expect?"* — the host
list shows what the game actually sees.

**Menu slot (`PositionInMenu`)** sets where the unit sits within that menu.

**Real-world helper boxes.** For distance, speed, and acceleration fields, the
row has a **second box** in real-world units. It's linked to the raw box, so
changing one updates the other:

| Field kind | Properties | Unit shown | Conversion |
| --- | --- | --- | --- |
| speed | `VitesseLineaire`, `VitesseCombat` | km/h | 130 raw = 1 km/h |
| accel | `MaxAcceleration`, `MaxDeceleration` | m/s² | 260 raw per unit |
| distance | `DetectionBase`, `PorteeVisionVolant`, `PorteeAttackReflexAir`, `PorteeAttackReflexSol`, `PorteeMaximale`, `PorteeMinimale` | km | 260000 raw = 1 km |

Edit either box and the other updates right away, before you Apply. **The raw
value is still what gets saved** — the helper box just does the math for you.

#### Upgrade chain

This section shows for units that have an `UpgradeRequire` property. It has two
controls:

- **Is an upgrade** checkbox — *"upgradable (adds price/time 50; hidden until
  researched)."* This sets the `IsUpgrade` flag.
- **Upgrades from** dropdown — the parent unit this one upgrades from. The
  choices are *"(standalone - not an upgrade)"* plus every other **unit of the
  same nation and the same build-menu category**. The current parent is always
  in the list, even if it wouldn't otherwise fit.

The two controls affect each other:

- If you pick a real parent in **Upgrades from**, the **Is an upgrade** box turns
  on and gets locked (a unit that upgrades *from* something must be an upgrade).
- If a unit already has a parent, the box starts on and locked.
- Choosing *"(standalone - not an upgrade)"* unlocks the box so you can turn the
  flag on or off on its own.

**When you Apply:**

- Setting a parent links this unit to that parent; clearing it removes the link.
- The `IsUpgrade` flag is set to match.
- When `IsUpgrade` is first turned **on**, `UpgradePrice` and `UpgradeTime` are
  added with a **default of 50** each (if they aren't already there). When it's
  turned **off**, both are removed. You can change those defaults in the stats
  table (Upgrade price / Upgrade time).

#### Weapons — pick each weapon's ammo

For units that have a weapon, this section lists each **weapon** the unit
carries. Every weapon points to an ammo, which holds its real stats.

Each weapon row shows the weapon's name (its `EffectTag`, or *"Weapon N"* if it
has none), the ammo it currently fires (`ammo #<id>`), and a **dropdown** of all
the ammo ids (`#<id>`). There is **no separate "Set ammo" button** per weapon —
the ammo you pick is saved when you click the main **Apply changes to this
unit**.

Below the weapon rows:

- **Remove all weapons from this unit** button — asks you to confirm (*"Remove
  the weapon from <name> (it will be unable to attack)?"*), then removes the
  unit's weapon. After that the unit can no longer attack.
- A note: *"Each weapon fires its own ammo. To make a unique weapon, duplicate an
  ammo on the Ammo tab, pick it for the weapon here, then click Apply. Editing a
  shared ammo below affects every unit using it."*

Then, for each **different** ammo the unit uses, an inline **Ammo #<id> stats**
section appears (the same fields as the Ammo tab — see below). Its title tells
you whether the ammo is *"(private to this unit)"* or *"(shared by N units)"*, so
you know before you edit whether changing it will affect other units.

#### Other unit fields (raw)

A catch-all section. Every remaining **number** setting (whole numbers, decimals,
or on/off flags) that the sections above didn't cover shows up here as an
editable row, listed by its raw property name — nothing is hidden. Settings that
aren't numbers (references, lists, name tags, or text) are listed **read-only**
under a heading *"Non-editable (ref / list / hash) — present on descriptor:"*,
with a short summary of each. This way you can see every setting the unit
actually has.

Note: `Nationalite` is shown here as an editable number on purpose (it's *not*
hidden), for advanced users testing how the game behaves. Editing it by hand
falls under the ⚠️ warning above — it does not make a working in-game unit.

#### Notes

A private **Notes** box, tied to this unit (keyed by `unit:<name>`). Notes are
saved with your mod project (in `notes.json`, not the game data) and save
automatically when you click away.

---

## Tab 2 — Ammo (the shared weapon stats)

The real weapon stats live in a shared **`TAmmunition`** entry. **Only the ammo
is shared** — each unit has its own weapon setup, but several units can point
their weapons at the *same* ammo. So editing an ammo changes it for **every**
unit that uses it.

### Finding an ammo

Ammo has **no readable name** — each one is labelled **"Ammo #<id>"** (its
`AmmunitionId`). The **Search ammo (id or unit)** box matches either the ammo id
or the names of the units that use it. The list is sorted by id, and the count
shows `X of Y`.

When you pick an ammo, a *"Used by: …"* line shows which units fire it. If more
than one unit shares it, it adds *"— editing affects all of them"*.

### Editable ammo fields

| Field (label) | Property |
| --- | --- |
| Damage (Puissance) | `Puissance` |
| Max range (PorteeMaximale) | `PorteeMaximale` |
| Min range (PorteeMinimale) | `PorteeMinimale` |
| Time between shots (TempsEntreDeuxTirs) | `TempsEntreDeuxTirs` |
| Shots per volley (NbTirParSalves) | `NbTirParSalves` |
| Reload between volleys (TempsEntreDeuxSalves) | `TempsEntreDeuxSalves` |
| Dispersion (AngleDispersion) | `AngleDispersion` |
| Pin radius (RayonPinned) | `RayonPinned` |
| % direct fire (PourcentageTirDirect) | `PourcentageTirDirect` |
| % direct moving (PourcentageTirDirectEnMouvement) | `PourcentageTirDirectEnMouvement` |
| Indirect fire 0/1 (TirIndirect) | `TirIndirect` |
| Allow ambush 0/1 (AllowAmbushShot) | `AllowAmbushShot` |
| Weapon level (Level) | `Level` |
| Weapon class (Arme) | `Arme` |
| Projectile type (ProjectileType) | `ProjectileType` |

`PorteeMaximale` and `PorteeMinimale` get the km helper box, just like on the
unit stats. An **Other ammo fields (raw)** catch-all shows any leftover number
settings (and lists the non-editable ones), exactly like the unit panel. There's
also a per-ammo **Notes** box (keyed by `ammo:<id>`).

- **Apply changes to this ammo** locks in your edits into the project.
- **Duplicate this ammo** — see the unique-weapon recipe below.

---

## Recipes

### Give a unit a unique weapon (without affecting others)

Since the ammo is the only shared part, the trick is to make a copy of the ammo
and point just that unit's weapon at the copy:

1. Go to the **Ammo** tab and select the ammo the unit uses now.
2. Click **Duplicate this ammo**. A new **Ammo #<id>** (a copy with a new id) is
   made and selected for you.
3. Edit the copy's stats and click **Apply changes to this ammo**.
4. Go back to the **Units & Buildings** tab and select your unit.
5. In the **Weapons** section, set that weapon's ammo dropdown to the new
   `#<id>`.
6. Click **Apply changes to this unit**.

Now only that unit fires the changed ammo; everyone else keeps the original.

### Move a unit to a different production building (same nation)

1. Select the unit.
2. In the stats table, open the **Build menu (Factory)** dropdown and pick the
   building you want, from the unit's own nation.
3. If you like, set **Menu slot (PositionInMenu)**.
4. Click **Apply changes to this unit**, then check the **Current** value of the
   Factory row — it lists the building(s) that now host the unit.

### Make a unit an upgrade of another

1. Select the unit that will be the upgrade.
2. In **Upgrade chain → Upgrades from**, pick the parent (only units of the same
   nation and same build menu show up).
3. (Optional) Change **Upgrade price** / **Upgrade time** in the stats table
   (they default to 50 each when the flag is first turned on).
4. Click **Apply changes to this unit**.

### Rename a unit in-game (multiple languages)

1. Make sure the project includes `ZZ_Win.dat` (the name file). Without it, name
   editing is turned off.
2. Select the unit. In **Display name (in-game)**, pick a language and type the
   name.
3. Switch languages and type more as needed.
4. Click **Apply changes to this unit** — every changed language is written into
   the mod's `ZZ_Win.dat`.

---

## See also

- [Mod Editor hub](mod-editor.md)
- [Economy editor](economy-editor.md)
- [Project README](../../README.md)

# AI Editor

The **AI Editor** is a window inside the [Mod Editor](mod-editor.md). It lets you
change how R.U.S.E.'s **computer opponent** plays a skirmish game (a quick battle
against the computer). "AI" just means the computer player's brain.

With this editor you can change four things:

- How the computer opponent behaves.
- The bonus "cheats" the computer gets on harder difficulties.
- The Ruse cards (the deception/trick cards) it is allowed to play.
- The computer's script files — but these you can only **look at**, not change yet.

> **Where the info is stored.** Three of the four tabs (AI Profiles, Difficulty
> Handicap, Ruse Cards) all edit the same game file, called **`everything.cpp`**.
> The game reads these numbers while you play, so changing them really does change
> how the bots play. The fourth tab (AI Scripts) reads the computer's script files
> out of a different file (called **`IA_Common.dat`**) and is **read-only for now**
> — meaning you can view it but not save changes.

See also the matching [Economy Editor](economy-editor.md) (it edits the same
`everything.cpp` file) and the project [README](../../README.md).

---

## Opening the editor

You open the AI Editor from the **Mod Editor hub** while a mod project is open. It
is not a separate program — it lives inside the Mod Editor, which draws the "Back"
bar around it. When it opens, it loads your project's game data. If it can't load
that data (for example, you haven't set a Game Root and there's no backup), it
shows an error and closes.

---

## The four tabs

| Tab | What it changes | Where the info comes from | Can you edit it? |
|-----|-------|-------------|-----------|
| **AI Profiles** | The computer's behaviour settings (`TIAProfil`) | `everything.cpp` | Yes |
| **Difficulty Handicap** | The computer's cheat bonuses (`TAISpecificBonus`) | `everything.cpp` | Yes |
| **Ruse Cards** | The deception/trick cards (`TBluffCardDescriptor`) | `everything.cpp` | Yes |
| **AI Scripts** | The computer's script files (`.xyz` scripts) | `IA_Common.dat` | **View only** |

The exact tab names (with extra spaces around them) are: `  AI Profiles  `,
`  Difficulty Handicap  `, `  Ruse Cards  `, `  AI Scripts  `.

The first three tabs all work the same way: a **list on the left, an editor on the
right**. Pick an item in the list, change its values on the right, click **Apply
changes**, then click **Save mod (.dat)** at the bottom of the window.

---

## How to use it (the main steps)

There are two separate actions. **Apply** puts your changes into memory so far.
**Save** writes them to the game file on your disk. You Apply many times, then Save
once.

1. **Select** an item in the left-hand list (a profile, a bonus, or a card).
2. **Edit** the values in the boxes on the right. Each row shows: a label, a dim
   preview of the *current value*, and a box you type into.
3. Click **Apply changes** (this button stays greyed out until you pick something).
   This writes your edits into memory, marks the game file as changed, and refreshes
   the panel so it shows exactly what it stored. The status line says
   *"Applied N change(s)"* or *"No changes"*.
4. Repeat for as many items and tabs as you like. Your edits **add up** across the
   whole project — nothing is lost between items.
5. Click **Save mod (.dat)** (bottom-left of the window) to write **all** of your
   collected changes to the mod's game files.

> **Save also applies anything you forgot to apply.** When you click *Save mod
> (.dat)*, the editor first quietly applies any values you typed but hadn't applied
> yet (across the Profiles, Difficulty Handicap, and Ruse Cards tabs). That way,
> what gets saved matches what you see. If nothing has changed, it tells you
> *"No pending changes to save."*. If it works, it shows you the files it wrote. If
> it fails, it suggests setting the **Game Root** in Settings.

The bottom of the window says the same thing: *"Apply commits the current selection
into the project. 'Save mod (.dat)' writes ALL accumulated changes to the mod's
.dat. These TIAProfil / TAISpecificBonus values are read by the C++ skirmish AI."*

### How your typed text is understood

When you Apply, the editor turns your typed text into the right kind of value, to
match the value that was already there:

- **Yes/no values (Booleans)** → typing `1`, `true`, `yes`, or `on` (in any
  capitalization) means *yes/true*. Anything else means *no/false*.
- **Decimal numbers (Floats)** → read as a decimal number.
- **Whole numbers (Ints)** → read as a whole number (so `3.0` becomes `3`).
- **Lists** → a set of whole numbers separated by commas or semicolons.
  **Special case:** if you type just *one* number and the list already has several
  items, that one number is copied into **every** item of the list.

If you type something that doesn't fit, a *"Invalid value(s)"* box pops up and lists
the properties it couldn't read. Any good rows in the same Apply still go through.

### Private notes

Every profile, bonus, and card has a **Notes** box at the bottom of its panel. These
are your own *private notes* — they are saved in your project's `notes.json` file
(not in the game data itself). They save automatically when you click away or when
the panel redraws. They stay with your mod **project**, and are not shipped inside
the finished game file.

---

## Tab 1 — AI Profiles (`TIAProfil`)

This tab changes the **`TIAProfil`** settings — the "brains" the computer opponent
reads. The editor finds them by walking through the game's profile lists (Default,
Difficulty, and AI personality lists) down to the real profile inside each one. If
it can't find them that way, it falls back to just listing every raw profile as
`Profile 0…N`.

There are usually about **10–11** profiles, in these groups:

| List label | Entries |
|------------|---------|
| **Default** | one baseline profile (`Default`) |
| **Difficulty: …** | `Easy` (idx 0), `Medium` (idx 1), `Hard` (idx 2) |
| **AI: …** (personalities) | `Regular` (0), `Air Force` (1), `Howitzer` (2), `Prototype` (3), `Blitzkrieg` (4), `Turtle` (5), `Random` (6) |

These numbers are exactly how the game names them — difficulties are `0=Easy /
1=Medium / 2=Hard`, and personalities are `0=Regular … 6=Random`. The personality
names hint at how each one likes to play (for example, *Air Force* leans on planes,
*Howitzer* on big guns/artillery, and *Turtle* on defence).

### Field groups

The editor shows about 48 profile settings, sorted into labelled sections. It only
shows settings that the chosen profile actually has. Any leftover number that
doesn't fit a section goes into a final **Other** group.

| Group | Fields (game property names) |
|-------|------------------------------|
| **Attack / offense** | `AttaqueTempsActivation` (attack activation time, s), `OffensiveNbMissionMax` (max offensive missions, −1=inf), `MissionFacteurLancementAttaque` (attack launch factor), `MissionFacteurEnoughToDestroy` (commit / enough-to-destroy factor) |
| **Defense** | `DefenseDistanceMenace` (threat distance), `DefenseDistanceUrgent` (urgent distance), `DefenseMaxUnitOnPosition` (max units per position), `DefenseNbMissionMax` (max defense missions) |
| **Harassment** | `HarcelementActif` (harassment active 0/1), `UpgradeActifPourHarcelement` (upgrade for harassment 0/1) |
| **Economy** | `CashReserveTempsActivation` (cash-reserve activation, s), `PercentMoneyToReserveForBatimentAdmin` (% money reserved for admin), `PercentMoneyToUseForIdle` (% money for idle build), `DeviseBonusIA` (**money bonus — cheat**), `IncomeBonusIA` (**income bonus — cheat**), `NbBatimentAdministratifEnPlusAvantDesactivation` (extra admin buildings before deactivate) |
| **Logistics / depots** | `DepotNbEnPlusAvantDesactivation` (extra depots before deactivate), `ValueDepotMinForTruckFactory`, `MinutesLeftMinForTruckFactory`, `NbDepotMinForTruckFactory`, `NbUnitsDangerousDepot` (units = dangerous depot), `SeuilDistanceGroupeDepot` (depot group distance), `SeuilDistanceTruckFactory` (truck factory distance) |
| **Production (idle build counts)** | `NbMaxProductionForEnnemyUnit` (max production vs enemy unit), `NbProdIdleInfanterie`, `NbProdIdleTank`, `NbProdIdleAntitank`, `NbProdIdleArti`, `NbProdIdleDCA` (idle AA), `NbProdIdleChasseur` (idle fighters), `NbProdIdleBomber`, `NbProdIdleChasseurBomber` (idle fighter-bombers), `ProdIdleCanLaunchResearch` (idle can launch research 0/1) |
| **Unit-type weighting** | `BonusUnitesAvions` (aircraft), `BonusUnitesArtillerie` (artillery), `BonusUnitesExperimentales` (experimental), `BonusUnitesRecherche` (research), `BonusBatimentOTF` (on-the-fly building) |
| **Deception (ruse cards)** | `PourcentChanceUtiliserCarteManipAuDebut` (% chance to open with a manip card), `NbCarteDansLaReservePourLaDifficulte` (cards in reserve by difficulty), `NbCarteDansLaReservePourLeProfil` (cards in reserve by profile) |
| **Retaliation** | `RepresailleFacteurUniteCombattante` (combatant retaliation factor), `RepresailleFacteurUniteNonCombattante` (non-combatant factor), `TimeOutRepresailles` (retaliation timeout, s) |
| **Intel / stealth** | `ProbaRepereFake` (chance to spot fakes/bluffs), `TempsMemorisationUnitInvisible` (invisible-unit memory, s), `TempsActivationBatimentCamoufle` (camouflaged-building activation, s) |
| **Other** | any leftover number not in the groups above, shown by its raw game name |

> **The cheats.** `DeviseBonusIA` and `IncomeBonusIA` in the **Economy** group are
> the computer's money and income cheat multipliers — the main "the AI gets free
> resources" knobs. Compare these with the Difficulty Handicap tab below, which is
> the *other* way the computer cheats.

### Editing a profile

1. Open the **AI Profiles** tab.
2. Pick a profile in the left list, for example *Difficulty: Hard* or *AI: Blitzkrieg*.
3. Scroll through the grouped fields on the right and change the ones you want.
4. Click **Apply changes**, then later **Save mod (.dat)**.

---

## Tab 2 — Difficulty Handicap (`TAISpecificBonus`)

This tab changes the **`TAISpecificBonus`** entries — the computer's *handicap /
cheat bonus*. This is a different cheat from the `…BonusIA` fields inside profiles.
Each entry is listed as `Bonus 0…N`.

### Fields

| Label | Game property | Kind |
|-------|--------------|------|
| Bonus value (the cheat amount) | `BonusValue` | single number |
| Consider stack as one enemy (0/1) | `ConsiderStackAsOneEnnemy` | single number |
| AI difficulties scope | `AIDifficulties` | list |
| AI profiles scope | `AIProfiles` | list |
| War modes scope | `WarModes` | list |
| Unit IDs scope | `UnitIDs` | list |

The **scope lists** decide *which* difficulties and *which* profiles actually get
the bonus. The panel reminds you what the numbers mean:

- **Difficulties:** `0=Easy, 1=Medium, 2=Hard`
- **Profiles:** `0=Regular, 1=Air Force, 2=Howitzer, 3=Prototype, 4=Blitzkrieg, 5=Turtle, 6=Random`

To edit a list, type whole numbers separated by commas or semicolons (for example
`1,2` to cover Medium + Hard). Remember: if the list already has items and you type
just one number, that number is copied into **every** item (see [How your typed text
is understood](#how-your-typed-text-is-understood) above). That's handy for making
every item the same, but watch out if you only meant to change the first one.

---

## Tab 3 — Ruse Cards (`TBluffCardDescriptor`)

This tab changes the **`TBluffCardDescriptor`** entries — the deception ("Ruse")
cards. The cards are sorted by their menu slot (`PositionInMenu`).

### Fields

| Label | Game property | Kind |
|-------|--------------|------|
| Effect duration (LifeDuration s) | `LifeDuration` | single number |
| Shown in menu (0/1) | `ShowInMenu` | single number |
| Menu slot | `PositionInMenu` | single number |

### How cards are named

R.U.S.E.'s readable card names live in a separate name database that this editor
can't read, so it identifies each card by a fixed code (a hash of its `Title`). For
known cards, it shows a friendly name, a one-line description of the effect, and the
game's own id (`BluffCardEnum`). Cards it doesn't recognise get a plain label like
`Card (slot N)`. The cards it recognises are:

| Card | `BluffCardEnum` id | Effect |
|------|--------------------|--------|
| Blitzkrieg | `Blitz` | Speeds up your units' movement and rate of fire in the zone |
| Fanaticism | `Fanatisme` | Units in the zone ignore morale and fight to the death |
| Terror | `Propagande` | Lowers enemy combat effectiveness / morale in the zone |
| Decoy: Barracks / Armor / Anti-Tank / Airfield | `BatimentFake_*` | Places a fake building to bluff the enemy |
| Fake Offensive / Fake Armored Assault / Fake Air Assault | `OffensiveFake_*` | Spawns a ghost (fake) army advancing |
| Spy | `Espion` | Reveals real vs. fake identity of enemy units/buildings |
| Decryption | `Decryptage` | Reveals enemy unit types in the zone |
| Radio Silence | `SilenceRadio` | Hides your units' identities from enemy Spy/Decryption |
| Camo Net | `BatimentsCamoufles` | Camouflages your buildings from the enemy |
| Reverse Intel | `Brouillage` | Counter-intel — scrambles the enemy's intel |

> Note: the in-game name is often different from the French internal id (Terror =
> *Propagande*, Reverse Intel = *Brouillage*).

Some cards are **hidden** (`ShowInMenu = None`): the leftover `BatimentFake` decoy
factories (Artillery, Prototype), some generic templates (`BatimentFake`,
`OffensiveFake`), and the campaign-only `FauxPlan` ("False Plan"). They exist in the
data and you can edit them, but they aren't part of the normal skirmish deck. One
card in slot 300 stays unidentified and keeps the plain fallback label.

### Editing a card

Pick a card, then change **LifeDuration** (how long the effect lasts), **ShowInMenu**
(set to 0 to hide it), or **PositionInMenu** (its deck slot). Then click **Apply
changes** and **Save mod (.dat)**.

---

## Tab 4 — AI Scripts (view-only)

This tab is a **view-only reader** for the computer's `.xyz` script files, which live
inside **`IA_Common.dat`**. It reads that file from your mod folder if it's there,
otherwise from a backup, otherwise from the game's own copy. If it can't read the
file, the tab shows a message telling you to set the **Game Root** in Settings or to
make a backup.

### What it does

- The left list shows every `.xyz` script (named by the last part of its path — the
  long folder prefix is trimmed off so it's easier to read).
- When you pick a script, the editor **turns the compiled script back into readable
  code** and shows it in the read-only text pane. (The scripts ship as compiled
  Python 2.5, so the editor decompiles them for you.)
- If it can't fully turn a script back into code, it shows a **summary** instead:
  which modules it uses, which AI actions and conditions it calls (things like
  Descriptor*, Condition*, Comportement*, Sequential, IfThenElse, SetVariable, …),
  and the first ~80 pieces of text found inside it.

### Export…

The **Export…** button (top-right of the tab) saves the script you've selected into
the `test_output/ai_scripts/` folder as two files:

- `<name>.py` — the readable, decompiled code.
- `<name>.marshal.bin` — the raw uncompressed compiled script.

When it's done, it tells you which folder it saved to. (These files always go into
`test_output/`, never into the game's own files.)

### You can't edit scripts yet

> **Important:** the AI Scripts tab is **view / export only**. You **cannot** edit
> or re-save scripts here yet. As the tab itself says, saving changed script
> behaviour would need a step that re-compiles the Python 2.5 code, and that
> recompiler isn't built yet (the file wrapper part is solved, but the recompiler
> isn't). The library of AI building blocks these scripts use lives in the
> `ZZ_Win.dat` `.ipk` packs.

---

## Tips & gotchas

- **Apply is not Save.** Applying only stages your edits in memory and marks the
  game file as changed. Nothing is written to disk until you click **Save mod
  (.dat)**. (Save will apply any unapplied rows first.)
- **List broadcast.** Typing one number into a list field overwrites *every* item —
  type the full comma-separated list if you want to set items one by one.
- **Only fields that exist show up.** If a profile doesn't have a certain field, that
  field just won't appear for it. The **Other** group catches numbers the named
  groups don't cover.
- **Profiles, bonuses, and cards all live in `everything.cpp`** — the same file the
  [Economy Editor](economy-editor.md) edits — so a Save collects changes from both
  editors.
- **Set the Game Root.** Reading `IA_Common.dat` and saving the game file both need a
  valid Game Root (or a project backup). Set it in Settings if you get read or save
  errors.

---

## Related docs

- [Mod Editor](mod-editor.md) — the hub that hosts this window.
- [Economy Editor](economy-editor.md) — sibling editor for the same `everything.cpp`.
- [Project README](../../README.md).

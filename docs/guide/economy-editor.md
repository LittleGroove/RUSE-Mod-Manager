# Economy Editor

The **Economy editor** is a window inside the [Mod Editor](mod-editor.md). It's
the one place to tune R.U.S.E.'s money, supply, production limits, population cap
(how many units you can have), the deception-card pool, how the AI produces and
attacks, and the stats of economy buildings (depots, administrative buildings,
and truck factories).

It changes **two sets of settings**, but both live in the same shared game data
file:

| What you edit | Which tab | What it controls |
|---|---|---|
| Global constants | Global Economy tab | Match-wide economy numbers (money, supply, limits, AI, population, cards) |
| Economy buildings | Economy Buildings tab | Stats for depots, admin buildings, and truck factories |

Because both live in the same game data file, your edits **build up** in the open
project. The window's **Save mod (.dat)** button writes everything out to the
mod's game data file — the constants, the buildings, and anything the Units or AI
editors changed too.

> The economy is run by **global numbers** shared by the whole match. There is
> **no per-building money rate** anywhere in the data. See
> [How depot money works](#how-depot-money-works) below before you go looking for
> a depot "income" field — it doesn't exist.

Related guides: [Mod Editor hub](mod-editor.md) · [Units editor](units-editor.md) ·
[AI editor](ai-editor.md) · [Mod Manager README](../../README.md).

---

## Opening the editor

Open it from the **Mod Editor** hub with a mod project loaded. It opens inside
the Mod Editor, which gives you the **Back** bar around it. If the game data
can't be loaded (no game root set, or a broken data file) you'll get a "Could not
load gameplay data" error and the window closes.

The window has **two tabs** and a shared bottom bar:

- `  Global Economy  ` — the match-wide economy numbers.
- `  Economy Buildings  ` — per-building stats for depots, admin buildings, and
  truck factories.

Bottom bar (shown on both tabs):

- **Save mod (.dat)** button — locks in any waiting edits, then writes the whole
  project to the mod's game data file.
- A status message on the right showing how the last save went.

---

## The edit -> Apply -> Save workflow

Both tabs work the same way. **Apply is not the same as Save.** Apply locks your
typed values into the project in memory (and redraws the panel so the current
value and any calculated rows update). **Save mod (.dat)** is what actually
writes the file to disk.

1. **Edit** — type new values into the boxes (right-hand column).
2. **Apply** — click the tab's Apply button:
   - Global Economy: **Apply global economy changes**
   - Economy Buildings: **Apply changes**
   The panel redraws in place (your scroll position stays put): the middle
   **current-value** column and any **calculated** rows now show what you just
   set, so you can see the effect right away. The status message says
   `Applied N change(s)` or `No changes`.
3. **Save mod (.dat)** — writes everything to the mod's game data file. If you
   click Save with text still typed but not yet Applied, the editor **Applies it
   first**, so what gets saved matches what you see.

If a value can't be read, you get an "Invalid value(s)" dialog listing the boxes
with problems. The other, valid rows in the same Apply still go through.

### Row layout

Every editable row has three columns:

1. **Label** (left, wraps to fit) — a friendly name, with the raw setting name in
   parentheses.
2. **Current value** (middle, read-only, dimmed) — the value as it stands after
   the last Apply.
3. **Entry** (right) — type the new value here.

**Calculated rows** have no entry box. They show a computed, gold, read-only
value that updates on Apply (see the depot section).

### Value formats you type

- **Scalar** — a single number or on/off flag. Flags accept `1/0`, `true/false`,
  `yes/no`, `on/off`. Decimals stay decimals, whole numbers stay whole numbers
  (the editor matches the field's existing type).
- **List** — separated by commas (or semicolons), like `100, 150, 200`. Type
  **one** value to set **every** item in the list to it; type **several** to set
  them one by one.
- **Bool list** — same list rules, but each item is an on/off flag (shown as
  `1`/`0`).

---

## Tab 1 — Global Economy

This tab edits the match-wide economy numbers. They're grouped into the sections
below. **A group or row only shows up if that number exists in your game
version**; missing ones are quietly left out. After the named groups, a catch-all
section shows **every remaining number, flag, or list, by its raw name**.

### Group: Money & income

| Label | Property | Type | What it means |
|---|---|---|---|
| Starting money | `QteDeviseInitiale` | scalar | Money each player starts with |
| Auto-income interval, s | `TempsGenAutoDevises` | scalar | Seconds between automatic income payments |
| Auto-income amount | `QuantiteGenAutoDevises` | scalar | Money given each automatic payment |
| Extra money on Easy | `StockDeviseSupplementaireFacile` | scalar | Bonus money a **player** gets on Easy difficulty |

> **`StockDeviseSupplementaireFacile` is a bonus for the player on Easy, NOT
> depot money.** It adds money to a player's wallet on Easy difficulty. It has
> nothing to do with depots or convoys.

### Group: Depot money output (supply convoys)

This is the **only** way to change how much money a depot gives. A depot has no
money field of its own — how much it pays per convoy *and* its total supply both
come from these convoy numbers.

| Label | Property | Type | What it means |
|---|---|---|---|
| Money each truck delivers | `QteDeviseParCamion` | scalar | Money per supply truck |
| Trucks per convoy (also scales depot total) | `NbCamionParConvoi` | scalar | Trucks in one convoy |
| => money per convoy | `_derived_per_convoy` | **calculated** | Read-only: trucks × money per truck |
| => depot TOTAL supply ~ per-convoy × 25 | `_derived_depot_total` | **calculated** | Read-only: money per convoy × 25 convoys |
| Seconds between convoys | `TempsENtreDeuxConvois` | scalar | Gap between convoys |
| Min truck spacing | `TempsENtreDeuxCamionsEnConvoiMin` | scalar | Smallest gap between trucks in a convoy |
| Max truck spacing | `TempsENtreDeuxCamionsEnConvoiMax` | scalar | Largest gap between trucks in a convoy |
| Depot nearly-depleted ratio | `RatioForDepotNearlyDepleted` | scalar | How empty a depot must be to count as "nearly empty" |

The two **calculated** rows are read-only and recompute every time you Apply, so
you can watch the effect of your edits live:

- **money per convoy** = `NbCamionParConvoi` × `QteDeviseParCamion`
- **depot TOTAL supply** ≈ money per convoy × **25** (a fixed number of convoys
  that's built into the game and can't be changed here)

### Group: Building value (AI/score)

| Label | Property | Type | What it means |
|---|---|---|---|
| Depot added value | `BP_DepotAddedValue` | scalar | How much a depot counts toward AI and score |
| HQ value | `BP_HqValue` | scalar | How much an HQ counts toward AI and score |

### Group: Production limits

| Label | Property | Type | What it means |
|---|---|---|---|
| Min production time | `MinProductionTime` | scalar | Lowest allowed production time |
| Max buildings producing | `MaximumBatimentProduction` | scalar | Most buildings that can produce at once |
| Max production queue | `MaxProductionQueueSize` | scalar | Longest a production queue can be |
| Max bldg+techno at once | `MaxBatimentAndTechnoProductionSimultaneous` | scalar | Most buildings + technos producing at the same time |
| Virtual factory queue slots | `VirtualFactoryQueueMaximumSlot` | scalar | Slots in the virtual factory queue |
| Planes per airfield | `NbAvionsParAeroport` | scalar | How many aircraft one airfield holds |

### Group: AI production queue & attack trigger

| Label | Property | Type | What it means |
|---|---|---|---|
| Army value to force an attack | `ArmyValueForceLaunchAttack` | scalar | Army strength that makes the AI attack |
| Max waiting production requests | `MaxWaitingRequest` | scalar | Most AI production requests that can wait in line |
| Waiting reqs before new factory | `NbMaxWaitingRequestBeforeRequestingNewFactory` | scalar | Backlog before the AI builds another factory |
| Max time waiting request, s | `MaxTimeWaitingRequest` | scalar | How long a request can wait |
| Cancel stale waiting requests | `CheckAndCancelWaitingRequest` | scalar | `0/1` — drop old requests |

### Group: Decoy / fake-building (bluff)

| Label | Property | Type |
|---|---|---|
| Decoy units min, general offensive | `NbMin_UniteLeurre_OffensiveGenerale` | scalar |
| Decoy units max, general offensive | `NbMax_UniteLeurre_OffensiveGenerale` | scalar |
| Decoy units min, air offensive | `NbMin_UniteLeurre_OffensiveAerienne` | scalar |
| Decoy units max, air offensive | `NbMax_UniteLeurre_OffensiveAerienne` | scalar |
| Decoy units min, armor offensive | `NbMin_UniteLeurre_OffensiveBlinde` | scalar |
| Decoy units max, armor offensive | `NbMax_UniteLeurre_OffensiveBlinde` | scalar |
| Fake-building construction delay min, s | `ConstructionDelayForFakeBuildingsMin` | scalar |
| Fake-building construction delay max, s | `ConstructionDelayForFakeBuildingsMax` | scalar |

These control how the AI bluffs — how many decoy (fake) units it fields for each
kind of attack, and how long fake buildings take to "build".

### Group: Population cap

| Label | Property | Type | What it means |
|---|---|---|---|
| Total pop cap | `PopCapTotal` | scalar | Hard limit across the whole match |
| Pop cap per alliance | `PopCapPerAlliance` | scalar | Limit per alliance (team) |
| Pop cap per player | `PopCapPerPlayer` | scalar | Limit per player |
| Ghost cap | `GhostCap` | scalar | Limit on ghost (decoy) units |

### Group: Deception-card pool

| Label | Property | Type | What it means |
|---|---|---|---|
| Max cards in pool | `NbMaxCardsInPool` | scalar | Biggest the card pool can be |
| Max cards per zone | `MaxNbCardsPerZoneByAlliance` | scalar | Limit per zone, per alliance |
| Initial cards, alliance size 1..4 | `NbInitialCardsInPoolForAllianceTaille_1` … `_4` | scalar | Starting cards, by how many players are in the alliance |
| Min army value to use manip card | `MinArmyValueToUseManipulationCard` | scalar | Army strength the AI needs before it plays a manipulation card |
| New-card time thresholds, size 1..4 | `PaliersTempsToChooseNewCardForAllianceTaille_1` … `_4` | **list** | Time steps for drawing a new card, by alliance size |

The four `Paliers...` rows are **lists** — type comma-separated time thresholds.

### Group: All other global constants (advanced)

A catch-all section titled `All other global constants (N — advanced, not just
economy)`. It lists **every setting on the global constants object that wasn't
already shown above** and is a number, on/off flag, number list, or flag list. It
sorts them by name and labels each with its raw name. This shows you the whole
constants object — not just economy — so edit carefully; many of these change
gameplay in big ways.

---

## Tab 2 — Economy Buildings

This tab edits the economy buildings. The list on the left shows only economy
buildings: ones whose code name contains **`Depot`**, **`Administratif`**, or
**`TruckFactory`**, while leaving out anything with `Leurre` (decoy) or `Fake` in
its name. The list is sorted by name.

Select a building on the left. Its fields appear on the right under a **Building
economy / stats** header. Edit them, click **Apply changes** (which turns on once
you've selected a building), and finally **Save mod (.dat)**.

### Per-building fields

| Label | Property | Type | What it means |
|---|---|---|---|
| Cost (all phases) | `ProductionPrice` | **list** | Build cost per phase — one value per game phase |
| Build time | `ProductionTime` | scalar | Seconds to build |
| HP | `SeuilMort` | scalar | Hit points (how much damage it takes to destroy) |
| Show in menu (per phase) | `ShowInMenu` | **boollist** | Whether it shows in the menu, per phase (`1`/`0` each) |
| Is depot for AI | `IsDepotForIA` | scalar | `0/1` — does the AI treat this as a depot |
| Distance to road | `DistanceToRoad` | scalar | How close to a road it must be placed |
| Vision | `DetectionBase` | scalar | Sight / detection radius |

Notes:

- **`ProductionPrice` is a list** — the cost is per phase. Type one number to set
  every phase to that cost, or a comma-separated list to set each phase on its
  own.
- **`ShowInMenu` is a flag list** — also per phase. Use it to hide or show the
  building in the build menu in certain phases.
- A field only shows up if the selected building actually has it.

### Other numeric fields (catch-all)

Below the named fields, an `Other numeric fields (N — raw, advanced)` section
lists **every remaining number, flag, or list** on the selected building, by its
raw name, sorted A–Z. This lets you reach any other setting on the building even
if the editor doesn't name it directly.

---

## How depot money works

A common mistake is to look for a "depot income" or "money per second" field on
the depot building. **It doesn't exist.** R.U.S.E. depots have no money field of
their own in the data. Money comes from three global sources only:

1. **Starting money** — `QteDeviseInitiale` (a one-time amount at the start of
   the match).
2. **Auto-income tick** — `QuantiteGenAutoDevises`, given every
   `TempsGenAutoDevises` seconds.
3. **Supply convoys** — the depot money lever, run entirely by the convoy
   numbers.

For depots specifically:

```
money per convoy   = NbCamionParConvoi  x  QteDeviseParCamion
depot TOTAL supply ≈ money per convoy   x  25            (25 = fixed convoy count, built into the game)
```

So to make depots richer, raise `QteDeviseParCamion` and/or `NbCamionParConvoi`
— **both scale the depot total** (tested in-game: raising the product took a
depot's total from about 250 to about 1500). The editor shows both effects live
through the two read-only calculated rows in the **Depot money output** group, so
you can dial in the numbers before you save.

Things that are **not** depot money:

- `StockDeviseSupplementaireFacile` — a bonus for the **player** on Easy
  difficulty, not depot money.
- The depot building's own fields on Tab 2 (`ProductionPrice`, `SeuilMort`, etc.)
  — these are the depot's build cost, HP, and vision, not its money output.

---

## Quick recipes

- **Faster economy** — raise `QuantiteGenAutoDevises` and/or lower
  `TempsGenAutoDevises` (Money & income).
- **Richer depots** — raise `QteDeviseParCamion` and `NbCamionParConvoi`; watch
  the calculated total.
- **Cheaper / tougher depots** — Tab 2: lower `ProductionPrice`, raise
  `SeuilMort`.
- **Bigger armies** — raise `PopCapTotal` / `PopCapPerPlayer` (Population cap).
- **Faster building spam** — raise `MaxProductionQueueSize` /
  `MaximumBatimentProduction` (Production limits).
- **More deception cards** — raise `NbMaxCardsInPool` and the
  `NbInitialCardsInPoolForAllianceTaille_*`.

Always finish with **Save mod (.dat)** to write your changes to disk.

---

## See also

- [Mod Editor hub](mod-editor.md) — opening editors, projects, Save-all.
- [Units editor](units-editor.md) — unit stats, costs, menu wiring.
- [AI editor](ai-editor.md) — AI behaviour data (goes with the AI tuning constants here).
- [Mod Manager README](../../README.md) — installing and running the Mod Manager.

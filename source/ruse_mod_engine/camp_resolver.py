"""Resolve a placement's `Camp` number to the CAMP that controls it — and so to its nationality.

WHY THIS ISN'T GUESSWORK
------------------------
A scenario placement stores only an integer (`TGameDesignAddOn_Spawn.Camp`). The camp's nationality
lives in the paired `.xyz` mission script. The link between the two is spelled out in the game's own
decompiled framework, `leveldesign/launcheffetmap.py`::

    camp_dico = {}
    camp_dico[world.camp_neutre.teamnum] = world.camp_neutre
    for (i, variable) in enumerate(self.descriptor.CampList):
        team = world.GetCampByTeamNumber(i)
        variable.set_camp(team)
        if variable.CampNumber == -1:
            camp_dico[i] = team
        else:
            camp_dico[variable.CampNumber] = team

and consumed in `leveldesign/helper.py` (`TagHelper.Prepare`)::

    if isinstance(addon, _gamedesign.TGameDesignAddOn_SpawnBase):
        if addon.Camp in camp_dico.keys():
            team = camp_dico[addon.Camp]

So `Camp` is a DIRECT KEY into `camp_dico`: the index of the camp in the effetmap descriptor's
`CampList`, unless that camp declares an explicit `CampNumber`, which then wins. `parse_camp_map`
below reproduces exactly that, in that order.

Two traps this exists to avoid:
  * **`CampList` order is not the order the camps appear in the file.** In `m04_cotentin` the list is
    `[IR_152, IR_151, IR_150, ... IR_144]` — the reverse of the assignment order. Reading assignments
    top-down and numbering them 0,1,2… gets the wrong nation for nearly every camp.
  * **A `Camp` value with no entry in `camp_dico` never spawns at all.** The engine's `if addon.Camp
    in camp_dico.keys()` silently drops the placement. `unresolved_camps()` reports those so the
    editor can warn instead of drawing a unit that will never exist in play.

NO CAMP AT ALL IS A DIFFERENT CASE — do not confuse the two.
An ABSENT `Camp` property is not "a camp that doesn't exist": the engine reads the NDF integer
default, so it resolves through `camp_dico` like any other value and the placement DOES spawn,
owned by camp 0 (the first camp — in campaign scenarios typically the player's side). That matches
the placement docs, which describe an absent `Camp` as "Team 1" (the same camp, counted from 1).
`camp_nation_icon` therefore maps None -> key 0, and the editor's "never spawns" warning is raised
ONLY for an explicit value with no camp behind it, never for an absent one.

Nationality only exists for SCRIPTED scenarios (campaign chapters, operations, challenges).
Multiplayer scenarios have no script: their camps are lobby slots carrying allegiance only, no
nation, so there is nothing to resolve and the caller should show none.
"""
import re
from typing import Dict, List, Optional

# Nationalite enum member -> the icon basename in icons/nation_icons/. The enum uses FRENCH
# abbreviations (see operation_roster.NATIONALITE, which is the authoritative table).
NATION_ICON = {
    "EU":        "USA.png",     # Etats-Unis — US / Western Allies
    "Allemagne": "GER.png",
    "RU":        "GB.png",      # Royaume-Uni
    "France":    "FR.png",
    "Italie":    "ITA.png",
    "URSS":      "USSR.png",
    "Japon":     "JAP.png",
}

# The everything.cpp `Nationalite` INT on a unit/building descriptor -> the same icon basename.
# (US carries no Nationalite property at all, hence the None key.) Matches units_editor._NATION_BY_VALUE.
NATION_ICON_BY_INT = {
    None: "USA.png", 0: "USA.png", 1: "GER.png", 2: "GB.png",
    3: "FR.png", 4: "ITA.png", 5: "USSR.png", 6: "JAP.png",
}

# Display names, for the details panel / tooltips.
NATION_LABEL = {
    "USA.png": "USA", "GER.png": "Germany", "GB.png": "Britain", "FR.png": "France",
    "ITA.png": "Italy", "USSR.png": "USSR", "JAP.png": "Japan",
}

NEUTRAL_CAMP = -1          # world.camp_neutre.teamnum — the visible/capturable neutral owner

_VARCAMP_RE = re.compile(r"(\w+)\s*=\s*leveldesign\.camps\.VariableCamp\(", re.M)
_CAMPLIST_RE = re.compile(r"CampList\s*=\s*\[([^\]]*)\]", re.S)


def _call_body(src: str, open_paren_idx: int) -> str:
    """Text inside the parentheses starting at `open_paren_idx`, respecting nesting."""
    depth, i, n = 0, open_paren_idx, len(src)
    while i < n:
        c = src[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return src[open_paren_idx + 1:i]
        i += 1
    return ""


def _kw(body: str, name: str) -> Optional[str]:
    m = re.search(r"\b%s\s*=\s*([^,)]+)" % re.escape(name), body)
    return m.group(1).strip() if m else None


def parse_variable_camps(src: str) -> Dict[str, dict]:
    """{var name -> {nation, camp_number, alliance, colour, ia}} for every VariableCamp in `src`."""
    out = {}
    for m in _VARCAMP_RE.finditer(src):
        var = m.group(1)
        body = _call_body(src, m.end() - 1)
        nat = _kw(body, "Nationalite")
        cn = _kw(body, "CampNumber")
        try:
            camp_number = int(cn) if cn is not None else -1
        except ValueError:
            camp_number = -1
        out[var] = {
            "var": var,
            "nation": nat.rsplit(".", 1)[-1] if nat else None,
            "camp_number": camp_number,
            "alliance": _kw(body, "Alliance"),
            "colour": (_kw(body, "Couleur") or "").rsplit(".", 1)[-1] or None,
            "ia": (_kw(body, "NiveauIA") or "").rsplit(".", 1)[-1] or None,
        }
    return out


def parse_camp_list(src: str) -> List[str]:
    """The DescriptorLaunchEffetMap `CampList` in ORDER — the list the engine enumerates.

    Order matters and is NOT the assignment order in the file; see the module docstring."""
    m = _CAMPLIST_RE.search(src)
    if not m:
        return []
    return [p.strip() for p in m.group(1).replace("\n", " ").split(",") if p.strip()]


def parse_camp_map(src: str) -> Dict[int, dict]:
    """`{camp key -> camp info}` exactly as the engine builds `camp_dico`.

    The key is what a placement's `Camp` field holds. Missing keys mean a placement pointing there
    NEVER SPAWNS (the engine's membership test drops it)."""
    camps = parse_variable_camps(src)
    order = parse_camp_list(src)
    if not order:
        # No CampList: nothing to enumerate, so only explicit CampNumbers can be keyed. Better to
        # return those than to invent an ordering from assignment order (which is wrong — see above).
        return {c["camp_number"]: c for c in camps.values() if c["camp_number"] != -1}
    out = {}
    for i, var in enumerate(order):
        info = camps.get(var)
        if info is None:
            continue
        key = i if info["camp_number"] == -1 else info["camp_number"]
        out[key] = info
    return out


def camp_nation_icon(camp_map: Dict[int, dict], camp_value) -> Optional[str]:
    """Icon basename for whoever controls a placement with this `Camp` value, or None if unknown.

    `camp_value` None means the property is absent. The engine reads the NDF default (0) in that
    case, so it resolves as camp 0 — which is also why the placement docs describe an absent Camp as
    'Team 1' (the same camp, counted from 1)."""
    if camp_value == NEUTRAL_CAMP:
        return None                       # neutral: owned by nobody, so no nation to show
    key = 0 if camp_value is None else camp_value
    info = camp_map.get(key)
    if not info or not info.get("nation"):
        return None
    return NATION_ICON.get(info["nation"])


def alliance_nation_icon(camp_map: Dict[int, dict], alliance) -> Optional[str]:
    """Icon basename for an HQ's ALLIANCE, or None when it isn't a single answer.

    An HQ carries `AllianceNum`, not `Camp` — a different axis: several camps can share one alliance
    (`VariableCamp(Alliance=...)`), and they do NOT have to share a nationality. Measured across the
    shipped corpus: 47 alliances resolve to one nation, but **41 are mixed** (compassrose Alliance 1
    is EU + RU + URSS). So this returns a nation only when every camp in the alliance agrees, and
    None otherwise — an HQ flag is worth nothing if it might be the wrong one of three."""
    if alliance is None:
        return None
    nations = {info.get("nation") for info in camp_map.values()
               if str(info.get("alliance")) == str(alliance) and info.get("nation")}
    if len(nations) != 1:
        return None
    return NATION_ICON.get(nations.pop())


def unresolved_camps(camp_map: Dict[int, dict], camp_values) -> List[int]:
    """Camp values used by placements that have NO camp behind them — the engine drops these, so
    anything placed under them never appears in play. Sorted, neutral/None excluded."""
    bad = set()
    for v in camp_values:
        if v is None or v == NEUTRAL_CAMP:
            continue
        if v not in camp_map:
            bad.add(v)
    return sorted(bad)

"""Read/write an operation's per-camp ROSTER (which bases + units each camp can build).

This is the engine layer under a roster editor UI. Given an operation's effetmap SOURCE (decompiled
via script_logic.decompile_xyz), it exposes:
  - parse_camps(src)   -> the camps, each with its nation + role (Player/Scripted/...)
  - parse_rosters(src) -> per camp: the WHITELIST (DescriptorDebloqueTechno TypeUnits = the ALLOWED classes),
                          whether all-tech is blocked (DescriptorBloqueAllTechno), and any explicit BLACKLIST
                          (DescriptorBloqueTechno). "Available roster" == the whitelist when block_all is set.
  - set_camp_whitelist(src, camp_var, allowed) -> new src with that camp's DebloqueTechno TypeUnits rewritten.
  - add_to_whitelist / remove_from_whitelist    -> convenience deltas.

The roster mechanism is a WHITELIST: BloqueAllTechno (block everything) + DebloqueTechno (re-allow a set),
per camp. PROVEN in-game (op_test_roster2). See our internal scenario notes.

Class names are `parametres.Classes.<Name>`; nation of each class comes from the class catalog
(tools/output/class_catalog.json, built from everything.cpp). Nationalite enum here matches that catalog.
"""
import json
import re
from typing import Dict, List, Optional

from . import script_logic
from ._resources import data_dir

# AUTHORITATIVE Nationalite map. The game enum `_enum_for_game_play.Nationalite.<X>` uses FRENCH abbreviations
# that do NOT obviously map to nations — using a wrong one (e.g. "USA", which does NOT exist) raises a Python
# AttributeError at effetmap load and crashes the map (RE'd 2026-07-12; see our internal scenario notes). These 7 are
# the ONLY valid members (harvested from every shipped effetmap). int = the everything.cpp Nationalite int
# (from the class catalog). ALWAYS use enum names from this table for Nationalite / NationList / choose_nation_from.
NATIONALITE = {
    "EU":        {"english": "USA",     "int": 0, "means": "Etats-Unis (US / Western Allies)"},
    "Allemagne": {"english": "Germany", "int": 1, "means": "Allemagne"},
    "RU":        {"english": "Britain", "int": 2, "means": "Royaume-Uni (UK)"},
    "France":    {"english": "France",  "int": 3, "means": "France"},
    "Italie":    {"english": "Italy",   "int": 4, "means": "Italie"},
    "URSS":      {"english": "USSR",    "int": 5, "means": "Union Sovietique"},
    "Japon":     {"english": "Japan",   "int": 6, "means": "Japon"},
}
VALID_NATIONALITES = set(NATIONALITE)  # the ONLY strings valid after _enum_for_game_play.Nationalite.

# enum member (or common English/legacy alias) -> the everything.cpp catalog nation name (English), for the
# roster catalog lookup. Kept lenient for reading existing scripts; for EMITTING, use VALID_NATIONALITES only.
NATION_ALIASES = {
    "Allemagne": "Allemagne", "Angleterre": "Angleterre", "France": "France", "Italie": "Italie",
    "URSS": "URSS", "Japon": "Japon", "USA": "USA", "EU": "USA", "RU": "Angleterre", "US": "USA",
}


def valid_nationalite(name: str) -> bool:
    """True iff `name` is a real Nationalite enum member (safe to emit as Nationalite.<name>)."""
    return name in VALID_NATIONALITES

_CLASS_RE = re.compile(r"parametres\.Classes\.(\w+)")

# ── class catalog (the blockable set, per-nation, from everything.cpp) ────────────────────────────────
_CATALOG_CACHE = None


def load_class_catalog() -> Dict:
    """The bundled class catalog (ruse_mod_engine/data/class_catalog.json): 345 game-accepted
    parametres.Classes.* names, each with {family: Building|Unit|Avion, nation, nation_source}.
    Returns {} if the resource is missing (the roster UI should then degrade to a free-text list)."""
    global _CATALOG_CACHE
    if _CATALOG_CACHE is None:
        path = data_dir() / "class_catalog.json"
        try:
            with open(str(path), "r") as f:
                _CATALOG_CACHE = json.load(f)
        except (OSError, ValueError):
            _CATALOG_CACHE = {}
    return _CATALOG_CACHE


def classes_for_nation(nation: str, family: Optional[str] = None) -> List[str]:
    """The catalog class names buildable by `nation` (e.g. 'Allemagne'), optionally filtered to one
    family ('Building'|'Unit'|'Avion'), sorted. `nation` accepts effetmap tokens too (EU/RU aliased).
    This is the full per-nation roster a camp of that nation could have; the editor checks the ones
    currently on the whitelist and lets the user toggle."""
    want = NATION_ALIASES.get(nation, nation)
    cat = load_class_catalog().get("classes", {})
    out = [name for name, rec in cat.items()
           if rec.get("nation") == want and (family is None or rec.get("family") == family)]
    return sorted(out)


def _kw(body: str, name: str) -> Optional[str]:
    m = re.search(r"\b" + re.escape(name) + r"\s*=\s*([\w.]+)", body)
    return m.group(1) if m else None


def _typeunits(body: str) -> List[str]:
    # TypeUnits lists hold only dotted parametres.Classes.* names -> no nested brackets, DOTALL is safe.
    m = re.search(r"TypeUnits\s*=\s*\[(.*?)\]", body, re.S)
    return _CLASS_RE.findall(m.group(1)) if m else []


def parse_camps(src: str) -> List[Dict]:
    """Every VariableCamp in the script: {var, niveau_ia, nationalite, nation, couleur, alliance}."""
    out = []
    irs = script_logic.ir_assignments(src)
    for var, info in irs.items():
        if info["type"] != "VariableCamp":
            continue
        body = info["body"]
        niv = _kw(body, "NiveauIA")
        nat = _kw(body, "Nationalite")
        col = _kw(body, "Couleur")
        aln = _kw(body, "Alliance")
        nat_name = nat.rsplit(".", 1)[-1] if nat else None
        out.append({
            "var": var,
            "niveau_ia": niv.rsplit(".", 1)[-1] if niv else None,
            "nationalite": nat_name,
            "nation": NATION_ALIASES.get(nat_name, nat_name),
            "couleur": col.rsplit(".", 1)[-1] if col else None,
            "alliance": aln,
            "choose_nation_from": _choose_nation_for(irs, var),
        })
    return out


def _choose_nation_for(irs: Dict, camp_var: str) -> List[str]:
    """Nations offered to `camp_var` by a DescriptorShowChooseNationScreen (empty if none). See doc 11."""
    for info in irs.values():
        if info["type"] == "DescriptorShowChooseNationScreen" and _kw(info["body"], "Camp") == camp_var:
            return re.findall(r"Nationalite\.(\w+)", info["body"])
    return []


def parse_rosters(src: str) -> Dict[str, Dict]:
    """Per camp var: {block_all, allowed, blocked, debloque_ir, bloqueall_ir, bloque_irs}.
    allowed = union of that camp's DescriptorDebloqueTechno TypeUnits (the buildable whitelist).
    blocked = union of that camp's DescriptorBloqueTechno TypeUnits (explicit blacklist).
    block_all = the camp has a DescriptorBloqueAllTechno (so only `allowed` is buildable)."""
    irs = script_logic.ir_assignments(src)
    rosters: Dict[str, Dict] = {}

    def slot(camp):
        return rosters.setdefault(camp, {"block_all": False, "allowed": [], "blocked": [],
                                         "debloque_ir": None, "bloqueall_ir": None, "bloque_irs": []})

    for ir, info in irs.items():
        t = info["type"]
        if t not in ("DescriptorDebloqueTechno", "DescriptorBloqueTechno", "DescriptorBloqueAllTechno"):
            continue
        camp = _kw(info["body"], "Camp")
        if not camp:
            continue
        s = slot(camp)
        if t == "DescriptorBloqueAllTechno":
            s["block_all"] = True
            s["bloqueall_ir"] = ir
        elif t == "DescriptorDebloqueTechno":
            s["allowed"].extend(_typeunits(info["body"]))
            s["debloque_ir"] = ir
        else:  # DescriptorBloqueTechno
            s["blocked"].extend(_typeunits(info["body"]))
            s["bloque_irs"].append(ir)
    return rosters


def _format_typeunits(classes: List[str]) -> str:
    return ",\n ".join("parametres.Classes.%s" % c for c in classes)


def set_camp_whitelist(src: str, camp_var: str, allowed: List[str]) -> str:
    """Rewrite the DescriptorDebloqueTechno.TypeUnits for `camp_var` to exactly `allowed`.
    Preserves order-of-appearance de-duplication of the input list. Raises if the camp has no DebloqueTechno
    (creating one from scratch + wiring it into the boot sequence is the generator's job, not this editor)."""
    seen, dedup = set(), []
    for c in allowed:
        if c not in seen:
            seen.add(c)
            dedup.append(c)

    irs = script_logic.ir_assignments(src)
    target = None
    for ir, info in irs.items():
        if info["type"] == "DescriptorDebloqueTechno" and _kw(info["body"], "Camp") == camp_var:
            target = (ir, info)
            break
    if target is None:
        raise ValueError("camp %s has no DescriptorDebloqueTechno to edit "
                         "(use the generator's allowed_technos to create one)" % camp_var)

    ir, info = target
    body = info["body"]
    new_list = "TypeUnits=[\n %s]" % _format_typeunits(dedup)
    new_body = re.sub(r"TypeUnits\s*=\s*\[.*?\]", lambda _m: new_list, body, count=1, flags=re.S)
    return src[:info["body_start"]] + new_body + src[info["end"]:]


def add_to_whitelist(src: str, camp_var: str, classes: List[str]) -> str:
    """Append classes to a camp's whitelist (skipping any already present)."""
    cur = parse_rosters(src).get(camp_var, {}).get("allowed", [])
    return set_camp_whitelist(src, camp_var, list(cur) + [c for c in classes if c not in cur])


def remove_from_whitelist(src: str, camp_var: str, classes: List[str]) -> str:
    """Remove classes from a camp's whitelist."""
    drop = set(classes)
    cur = parse_rosters(src).get(camp_var, {}).get("allowed", [])
    return set_camp_whitelist(src, camp_var, [c for c in cur if c not in drop])

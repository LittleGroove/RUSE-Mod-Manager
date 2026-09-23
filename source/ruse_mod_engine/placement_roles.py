"""Map-editor placement ICON ROLES — what a thing IS, so the canvas can draw a telling picture of it.

The map editor used to draw almost everything as a coloured dot: a Tiger, a paratrooper, a machine-gun
nest and a script waypoint were four dots in three colours. This module is the vocabulary that fixes
that — every placeable class is assigned a ROLE, and every role has its own icon.

WHERE THE ROLES COME FROM (this is not guesswork)
-------------------------------------------------
The game already classifies its own units. Each descriptor in ``everything.cpp`` carries:
  * ``TypeUnitHintToken`` — a LocHash resolving to the type line the build menu shows
    ('Medium Tank', 'Recon Infantry', 'Heavy Bomber', 'Very Resistant Bunker', ...): 59 distinct
    labels across the 457 placeable classes.
  * ``NameInMenuToken``  — the menu name, which for BUILDINGS is the function ('BARRACKS',
    'AIRFIELD', 'ANTI-TANK BASE', 'DECOY ARMOR BASE', 'MAGINOT BUNKER'...).
  * ``Factory``          — the producing building (8 Barracks, 9 Airfield, 10 Armor, 11 Anti-Tank,
    12 Prototype, 13 Artillery & AA, 3 Turret/Defense). This is what separates PARATROOPERS from
    line infantry: identical 'Elite Infantry' type label, but Factory 9 (Airfield) instead of 8.

``role_from_game_fields`` below maps those fields to a role. It is a pure function so it can be
tested; ``tools/test_scripts/build_placement_icon_roles.py`` feeds it the real values pulled from
everything.cpp + the .dic tables and bakes the answer into ``data/placement_icon_roles.json``, which
is what the editor loads (no game files needed at draw time).

Probes behind the taxonomy: tools/test_scripts/probe_unit_type_labels.py (the game's own labels) and
probe_placement_universe.py (what actually ships in the 102 scenarios).

ICON GRAMMAR (see tools/test_scripts/gen_map_icons.py, which draws from this table)
-----------------------------------------------------------------------------------
Each role is (glyph, family, weight, badge) and the drawn icon is the composition of those, the way a
military map symbol composes:
  * FAMILY  -> the chip colour (infantry green, armour amber, artillery rust, air sky-blue, ...)
  * GLYPH   -> the silhouette (tank, towed gun, parachute, hangar, pillbox, ...)
  * WEIGHT  -> 1-4 pips along the top edge = light / medium / heavy / super-heavy
  * BADGE   -> a corner mark: 'adv' advanced (star), 'atomic' (atom), 'decoy' (hollow/dashed),
               'air' airborne-delivered (chevron)
So a Sherman and a Tiger share the tank glyph and differ by pips; a Panther and a Tiger share weight
and differ by the advanced star. That keeps ~90 roles legible from ~30 drawn silhouettes.
"""
import json
from typing import Dict, Optional

from ._resources import data_dir

_DATA = str(data_dir() / "placement_icon_roles.json")
_cache = None


# ── families (chip colour + what the family means) ────────────────────────────────
# RGB is the chip fill; the generator derives the border/shadow from it.
FAMILIES = {
    "infantry":  ((74, 124, 68),  "Foot soldiers"),
    "airborne":  ((64, 132, 118), "Airborne / paradropped troops"),
    "armour":    ((178, 126, 46), "Tanks and armoured vehicles"),
    "antitank":  ((166, 88, 52),  "Anti-tank"),
    "artillery": ((150, 70, 62),  "Artillery"),
    "antiair":   ((118, 84, 150), "Anti-aircraft"),
    "recon":     ((72, 128, 168), "Reconnaissance"),
    "support":   ((96, 106, 122), "Transport, supply and construction"),
    "air":       ((58, 118, 178), "Aircraft"),
    "naval":     ((44, 88, 140),  "Ships and landing craft"),
    "building":  ((92, 96, 108),  "Base buildings"),
    "defense":   ((122, 66, 74),  "Static defences"),
    "special":   ((150, 74, 140), "Nuclear and experimental"),
    "marker":    ((104, 112, 128), "Scenario markers, labels and zones"),
}


# What the weight pips MEAN, so the UI can say "Heavy" instead of "3 bars".
WEIGHT_NAMES = {0: "", 1: "light", 2: "medium", 3: "heavy", 4: "super_heavy"}


class Role:
    """One icon role: how to draw it and how to describe it in the UI."""

    __slots__ = ("id", "label", "family", "glyph", "weight", "badge", "desc")

    def __init__(self, rid, label, family, glyph, weight=0, badge=None, desc=""):
        self.id = rid
        self.label = label
        self.family = family
        self.glyph = glyph
        self.weight = weight          # 0 = no pips, 1..4 = light..super-heavy
        self.badge = badge            # None | "adv" | "atomic" | "decoy" | "air"
        self.desc = desc

    @property
    def icon(self):
        """Icon filename inside icons/map_icons/roles/."""
        return self.id + ".png"


def _r(*a, **kw):
    return Role(*a, **kw)


# ── the role table ────────────────────────────────────────────────────────────────
# Ordered so the legend reads top-down the way a player thinks: men, armour, guns, air, sea,
# structures, defences, then the scenario furniture that isn't a unit at all.
ROLES: Dict[str, Role] = {r.id: r for r in [
    # infantry ─────────────────────────────────────────────────────────────────────
    _r("inf_light",      "Light infantry",     "infantry", "infantry",  1,
       desc="Cheap line infantry. Captures bases, ambushes from woods and city squares, "
            "and moves faster on roads."),
    _r("inf_heavy",      "Heavy infantry",     "infantry", "infantry",  3,
       desc="More firepower than light infantry, and still captures and ambushes."),
    _r("inf_elite",      "Elite infantry",     "infantry", "infantry",  2, "adv",
       desc="Significantly more firepower than light infantry (Legionnaires). Captures and "
            "ambushes like the rest."),
    _r("inf_recon",      "Recon infantry",     "recon",    "recon",     1,
       desc="Infantry with a long line of sight. Still captures bases and ambushes."),
    _r("inf_at",         "Anti-tank infantry", "antitank", "inf_at",    2,
       desc="Infantry armed to kill tanks — very effective against armour."),
    _r("inf_sniper",     "Snipers",            "infantry", "sniper",    1,
       desc="Long-range rifles with a slow rate of fire. Deadly to infantry; captures and ambushes."),
    _r("inf_engineer",   "Combat engineers",   "infantry", "engineer",  2,
       desc="Demolition troops (satchel charges and a flamethrower). Deadly against buildings, and "
            "the only infantry that CANNOT capture — they burn structures down instead."),
    # airborne ─────────────────────────────────────────────────────────────────────
    _r("inf_para",       "Paratroopers",       "airborne", "para",      2, "air",
       desc="Elite infantry built at the AIRFIELD and dropped in. Captures and ambushes like any "
            "other infantry once it lands."),
    _r("inf_para_recon", "Airborne recon",     "airborne", "para_recon", 1, "air",
       desc="Airfield-built scouts with a long line of sight (Giretsu Kuteitai), delivered by air."),
    _r("para_drop",      "Airborne drop",      "airborne", "para_drop", 2, "air",
       desc="The airdrop entity itself (the Para_* classes) rather than the squad once it lands."),
    # armour ───────────────────────────────────────────────────────────────────────
    _r("tank_light",     "Light tank",         "armour", "tank",   1,
       desc="Fast tank with a short-range gun. Fires on the move; cannot enter woods."),
    _r("tank_light_adv", "Advanced light tank", "armour", "tank",  1, "adv",
       desc="Light tank with more firepower than the common ones (Chaffee)."),
    _r("tank_medium",    "Medium tank",        "armour", "tank",   2,
       desc="Medium-range gun, fires on the move, cannot enter woods — the workhorse tank."),
    _r("tank_medium_adv", "Advanced medium tank", "armour", "tank", 2, "adv",
       desc="Medium tank with greater firepower (Panther, Firefly)."),
    _r("tank_heavy",     "Heavy tank",         "armour", "tank",   3,
       desc="Long-range gun and thick armour. Fires on the move; cannot enter woods."),
    _r("tank_heavy_adv", "Advanced heavy tank", "armour", "tank",  3, "adv",
       desc="Heavy tank with even greater firepower (King Tiger, ARL 44, Carro P26)."),
    _r("tank_super",     "Super-heavy tank",   "armour", "tank",   4,
       desc="The biggest tanks of all. More firepower than an advanced heavy — though some are "
            "turretless and only fire while stopped."),
    _r("tank_destroyer", "Tank destroyer",     "antitank", "td",   2,
       desc="Long-range anti-tank gun on tracks. Most only fire while stopped; cannot enter woods."),
    _r("tank_destroyer_adv", "Advanced tank destroyer", "antitank", "td", 3, "adv",
       desc="Very long-range anti-tank gun (Jagdpanther, SU-100, Jackson)."),
    _r("assault_gun",    "Assault gun",        "armour", "assault", 2,
       desc="Fires short-range indirect shells at ground it cannot even identify, but only while "
            "stopped. Very effective against buildings."),
    _r("assault_gun_heavy", "Heavy assault gun", "armour", "assault", 3,
       desc="Deadly against buildings, well armoured, fires indirect while stopped (Sturmtiger, AVRE)."),
    _r("tank_flame",     "Flamethrower tank",  "armour", "flame",  2,
       desc="Short flame bursts, even on the move and at targets it cannot identify. Deadly to infantry."),
    _r("rocket_tank",    "Rocket launcher tank", "artillery", "rocket", 2,
       desc="Rocket salvos from a tank hull (Calliope). Fires at unseen ground, but only while stopped."),
    _r("rocket_vehicle", "Rocket launcher vehicle", "artillery", "rocket_truck", 2,
       desc="Truck-mounted rocket salvos (Katyusha). Fires while stopped; faster on roads."),
    _r("rocket_vehicle_heavy", "Heavy rocket launcher", "artillery", "rocket_truck", 3,
       desc="Long-range rocket salvos (Wurfrahmen). Deadly against buildings; fires while stopped."),
    _r("recon_armored",  "Armoured recon",     "recon", "car",     2,
       desc="Armoured car: a long line of sight and an anti-tank gun. Ambushes; faster on roads."),
    _r("recon_unarmed",  "Unarmed recon",      "recon", "jeep",    1,
       desc="A long line of sight and no weapon at all. Faster on roads."),
    # towed guns ───────────────────────────────────────────────────────────────────
    _r("at_gun",         "Anti-tank gun",      "antitank", "at_gun", 2,
       desc="Towed medium-range anti-tank gun. Cannot fire while moving; ambushes from woods and "
            "city squares."),
    _r("at_gun_adv",     "Advanced anti-tank gun", "antitank", "at_gun", 3, "adv",
       desc="Towed LONG-range anti-tank gun (PaK 40, 17-pounder, ZiS-3)."),
    _r("at_aa_gun",      "AT / AA gun",        "antitank", "at_aa", 3,
       desc="One gun that kills both tanks and aircraft (88mm Flak, Breda 90/53). Must be stopped "
            "or deployed to fire."),
    _r("aa_gun",         "Anti-aircraft gun",  "antiair", "aa_gun", 2,
       desc="Towed anti-aircraft gun. Cannot fire while moving; ambushes from woods and city squares."),
    _r("aa_armored",     "Armoured AA",        "antiair", "aa_tank", 2,
       desc="Anti-aircraft gun on an armoured chassis (Wirbelwind, Skink). Fires while moving."),
    _r("aa_mobile",      "Mobile AA",          "antiair", "aa_truck", 1,
       desc="Anti-aircraft gun on a soft-skinned vehicle. Fires while moving; faster on roads."),
    # artillery ────────────────────────────────────────────────────────────────────
    _r("arty_light",     "Light artillery",    "artillery", "howitzer", 1,
       desc="Medium-range indirect salvos onto ground it cannot even identify — but only once in "
            "position."),
    _r("arty_medium",    "Medium artillery",   "artillery", "howitzer", 2,
       desc="Long-range indirect salvos onto unseen ground, once in position."),
    _r("arty_heavy",     "Heavy artillery",    "artillery", "howitzer", 3,
       desc="VERY long-range indirect salvos onto unseen ground, once in position (150mm and up)."),
    _r("arty_armored",   "Armoured artillery", "artillery", "spg",      2,
       desc="Self-propelled: medium-range indirect salvos once stopped, with some armour "
            "(Priest, Sexton)."),
    _r("arty_armored_heavy", "Heavy armoured artillery", "artillery", "spg", 3,
       desc="Self-propelled with LONG-range salvos and some armour (ARL 40, M40, Ha-To)."),
    _r("arty_nuclear",   "Nuclear artillery",  "special", "howitzer",  3, "atomic",
       desc="Very long-range NUCLEAR shells once deployed. Sets off a nuclear explosion when destroyed."),
    _r("atomic_missile", "Atomic missile",     "special", "missile",   4, "atomic",
       desc="Nuclear missile: hits unseen ground anywhere in range, and cannot move at all."),
    _r("rocket_v2",      "V2 rocket",          "artillery", "missile", 3,
       desc="Conventional V2 ballistic rocket — deadly against buildings."),
    # support ──────────────────────────────────────────────────────────────────────
    _r("truck_supply",   "Supply truck",       "support", "truck",     1,
       desc="Carries supply from a depot back to your headquarters."),
    _r("truck_construction", "Construction truck", "support", "truck_build", 1,
       desc="Called ENGINEERS or SAPEURS in game — this is the truck that BUILDS your base."),
    _r("truck_decoy",    "Decoy truck",        "support", "truck",     1, "decoy",
       desc="Fake truck, for selling a ruse."),
    _r("transport_infantry", "Infantry transport", "support", "transport", 1,
       desc="Unarmed. Carries an infantry squad."),
    _r("transport_gun",  "Gun tractor",        "support", "tractor",   1,
       desc="Unarmed. Tows a gun into position."),
    _r("transport_hq",   "HQ truck",           "support", "hq_truck",  2,
       desc="Unarmed. Deploys into your headquarters."),
    _r("empty_placeholder", "Empty placeholder", "marker", "blank",    0,
       desc="Engine placeholder class (Unite_Vide) — not a real unit."),
    # air ──────────────────────────────────────────────────────────────────────────
    _r("plane_fighter",  "Fighter",            "air", "fighter",       1,
       desc="Engages any air target, and can strafe light ground units."),
    _r("plane_fighter_adv", "Advanced fighter", "air", "fighter",      2, "adv",
       desc="Fighter with greater firepower than the common ones."),
    _r("plane_jet_fighter", "Jet fighter",     "air", "jet",           3, "adv",
       desc="More firepower AND more speed than a common fighter (Me 262)."),
    _r("plane_fighter_bomber", "Fighter-bomber", "air", "fighter_bomber", 2,
       desc="Attacks any ground target, and can still defend itself against most fighters."),
    _r("plane_fighter_bomber_adv", "Advanced fighter-bomber", "air", "fighter_bomber", 3, "adv",
       desc="Ground attack with greater firepower (Typhoon, Sturmovik)."),
    _r("plane_bomber_light",  "Light bomber",  "air", "bomber",        1,
       desc="One run destroys a FRAGILE structure. Bombs unseen ground; fends off fighters."),
    _r("plane_bomber_medium", "Medium bomber", "air", "bomber",        2,
       desc="One run destroys a RESISTANT structure."),
    _r("plane_bomber_heavy",  "Heavy bomber",  "air", "bomber_heavy",  3,
       desc="One run destroys a VERY RESISTANT structure — the four-engine heavies."),
    _r("plane_bomber_atomic", "Atomic bomber", "special", "bomber_heavy", 4, "atomic",
       desc="Carries the atomic bomb."),
    _r("plane_jet_bomber",    "Jet bomber",    "air", "jet",           3,
       desc="Jet bomber (Ar 234). Its bombs spread over a wider area than a normal medium bomber's."),
    _r("plane_recon",         "Air recon",     "recon", "recon_plane", 2,
       desc="A long line of sight, with a defensive machine gun only."),
    _r("plane_recon_unarmed", "Unarmed air recon", "recon", "recon_plane", 1,
       desc="A long line of sight and no weapons at all."),
    _r("plane_transport",     "Air transport", "airborne", "transport_plane", 2, "air",
       desc="Drops paratroopers on an area you choose."),
    # naval ────────────────────────────────────────────────────────────────────────
    _r("ship_battleship", "Battleship",        "naval", "battleship",  4,
       desc="Offshore battleship — deadly against anything on the ground."),
    _r("ship_cruiser",    "Heavy cruiser",     "naval", "warship",     3,
       desc="Offshore cruiser — very effective against ground targets."),
    _r("ship_destroyer",  "Destroyer",         "naval", "warship",     2,
       desc="Offshore destroyer — deadly against AIRCRAFT, not ground."),
    _r("landing_craft",   "Landing craft",     "naval", "landing",     1,
       desc="Unarmed beach landing craft (LCVP, LST)."),
    # base buildings ───────────────────────────────────────────────────────────────
    _r("bld_hq",          "Headquarters",      "building", "hq",       4,
       desc="Your headquarters: fields engineers, receives supply convoys, and earns $1 every 4s. "
            "Losing it usually loses the game."),
    _r("bld_hq2",         "Secondary HQ",      "building", "hq",       2,
       desc="Forward second headquarters. Fields engineers and receives supply convoys."),
    _r("bld_barracks",    "Barracks",          "building", "barracks", 2,
       desc="Fields infantry, plus light recon, armoured recon or light tanks depending on the nation."),
    _r("bld_armor_base",  "Armor base",        "building", "factory_tank", 2,
       desc="Fields tanks — and armoured recon or anti-air units for some nations."),
    _r("bld_at_base",     "Anti-tank base",    "building", "factory_at",   2,
       desc="Fields anti-tank units."),
    _r("bld_arty_base",   "Artillery & AA base", "building", "factory_arty", 2,
       desc="Fields artillery and anti-air units (and assault guns for Germany)."),
    _r("bld_prototype_base", "Prototype base", "building", "factory_proto", 3,
       desc="Fields prototypes."),
    _r("bld_atomic_center",  "Atomic center",  "special",  "atom_bld", 4, "atomic",
       desc="Fields nuclear howitzers. Sets off a nuclear explosion when destroyed."),
    _r("bld_airfield",    "Airfield",          "building", "airfield", 3,
       desc="Fields up to 8 aircraft each, plus airborne units."),
    _r("bld_supply_depot", "Supply depot building", "building", "depot_bld", 2,
       desc="Sends out supply convoys carrying $9 every 30s. (The capturable pad is its own marker.)"),
    _r("bld_admin",       "Administrative building", "building", "admin", 1,
       desc="Civilian building that earns $1 every 4s."),
    # decoys ───────────────────────────────────────────────────────────────────────
    _r("bld_barracks_decoy",   "Decoy barracks",   "building", "barracks", 2, "decoy",
       desc="Fake barracks — booby-trapped to kill any unit that tries to capture it."),
    _r("bld_armor_base_decoy", "Decoy armor base", "building", "factory_tank", 2, "decoy",
       desc="Fake armor base — booby-trapped to kill any unit that tries to capture it."),
    _r("bld_at_base_decoy",    "Decoy anti-tank base", "building", "factory_at", 2, "decoy",
       desc="Fake anti-tank base — booby-trapped to kill any unit that tries to capture it."),
    _r("bld_arty_base_decoy",  "Decoy artillery base", "building", "factory_arty", 2, "decoy",
       desc="Fake artillery & AA base — booby-trapped to kill any unit that tries to capture it."),
    _r("bld_prototype_base_decoy", "Decoy prototype base", "building", "factory_proto", 3, "decoy",
       desc="Fake prototype base — booby-trapped to kill any unit that tries to capture it."),
    _r("bld_airfield_decoy",   "Decoy airfield",   "building", "airfield", 3, "decoy",
       desc="Fake airfield — booby-trapped to kill any unit that tries to capture it."),
    _r("def_decoy",            "Decoy defence",    "defense",  "bunker",   1, "decoy",
       desc="Fake defensive position. Harmless — but the enemy doesn't know that, and may keep clear."),
    # static defences ──────────────────────────────────────────────────────────────
    _r("def_mg",        "Machine-gun nest",   "defense", "mg_nest",   1,
       desc="Two heavy machine guns (.50 cal or MG42). Very effective against infantry, no use "
            "against armour."),
    _r("def_at",        "Anti-tank position", "defense", "at_bunker", 2,
       desc="Dug-in anti-tank gun — a 57mm, a 76mm, or a 90mm dual-purpose."),
    _r("def_aa",        "Anti-air position",  "defense", "aa_bunker", 2,
       desc="Dug-in anti-aircraft guns — twin Bofors, or triple machine guns."),
    _r("def_mg_at_aa",  "Combined defence nest", "defense", "combo_nest", 2,
       desc="One position combining machine guns, anti-tank and anti-air."),
    _r("def_arty",      "Artillery position", "defense", "arty_pos",  2,
       desc="A 105mm medium-range artillery battery, emplaced."),
    _r("def_arty_heavy", "Heavy artillery position", "defense", "arty_pos", 3,
       desc="Two 155mm long-range artillery batteries, emplaced."),
    _r("def_arty_shelter", "Artillery shelter", "defense", "arty_shelter", 2,
       desc="The same 105mm battery as an artillery position, but under hard cover."),
    _r("def_105mm",     "105mm position",     "defense", "at_pos_heavy", 2,
       desc="A 105mm heavy ANTI-TANK gun position — it kills tanks, not buildings."),
    _r("def_pillbox",   "Pillbox",            "defense", "pillbox",   3,
       desc="Buried concrete with two short-barrelled 75mm dual-purpose guns."),
    _r("def_maginot",   "Maginot bunker",     "defense", "fort",      4,
       desc="Maginot fort: two 47mm anti-tank guns and a flamethrower."),
    _r("def_siegfried", "Siegfried blockhaus", "defense", "blockhaus", 4,
       desc="Siegfried blockhaus: an 88mm heavy anti-tank gun and a flamethrower."),
    _r("def_fortified", "Fortified position", "defense", "bunker",    2,
       desc="A 47mm anti-tank gun and a Bofors anti-aircraft gun."),
    _r("def_outpost",   "Outpost",            "recon",   "outpost",   1,
       desc="Observation post: a long line of sight and a light machine gun."),
    # scenario markers (not units) ─────────────────────────────────────────────────
    _r("depot",         "Supply depot",       "support", "depot",     2,
       desc="Capturable supply pad — the income of the map. Feeds convoys back to your HQ."),
    _r("hq_start",      "Player start / HQ",  "building", "start",    4,
       desc="Where a player's HQ spawns, with its opening camera."),
    _r("waypoint",      "Named point",        "marker",  "waypoint",  0,
       desc="A named position the mission script can reference (TagPosition)."),
    _r("label_city",    "City label",         "marker",  "city",      0,
       desc="City name printed on the map."),
    _r("label_mountain", "Mountain label",    "marker",  "mountain",  0,
       desc="Mountain name printed on the map."),
    _r("zone_circle",   "Circular zone",      "marker",  "zone_circle", 0,
       desc="Circular trigger/detection zone (radius)."),
    _r("zone_rect",     "Rectangular zone",   "marker",  "zone_rect",   0,
       desc="Rectangular trigger/detection zone (width x height)."),
    _r("spawn_other",   "Other spawn",        "marker",  "spawn",     0,
       desc="A camp-owned scenario entity that isn't a unit or a building."),
    _r("unknown",       "Unknown",            "marker",  "unknown",   0,
       desc="Placement whose class the editor doesn't recognise."),
]}


# ── classification from the game's own fields ─────────────────────────────────────
# type label -> role, for everything the label alone settles.
_BY_TYPE_LABEL = {
    "Light Infantry":              "inf_light",
    "Heavy Infantry":              "inf_heavy",
    "Elite Infantry":              "inf_elite",
    "Recon Infantry":              "inf_recon",
    "Sharpshooters":               "inf_sniper",
    "Combat Engineers":            "inf_engineer",
    "Light Tank":                  "tank_light",
    "Advanced Light Tank":         "tank_light_adv",
    "Medium Tank":                 "tank_medium",
    "Advanced Medium Tank":        "tank_medium_adv",
    "Heavy Tank":                  "tank_heavy",
    "Advanced Heavy Tank":         "tank_heavy_adv",
    "Super-Heavy Tank":            "tank_super",
    "Tank Destroyer":              "tank_destroyer",
    "Advanced Tank Destroyer":     "tank_destroyer_adv",
    "Assault Gun":                 "assault_gun",
    "Heavy Assault Gun":           "assault_gun_heavy",
    "Flamethrower Tank":           "tank_flame",
    "Rocket Launcher Tank":        "rocket_tank",
    "Rocket Launcher Vehicle":     "rocket_vehicle",
    "Heavy Rocket Launcher Vehicle": "rocket_vehicle_heavy",
    "Armored Recon":               "recon_armored",
    "Unarmed Recon":               "recon_unarmed",
    "Anti-tank Gun":               "at_gun",
    "Advanced Anti-tank Gun":      "at_gun_adv",
    "Anti-tank & Anti-aircraft Gun": "at_aa_gun",
    "Anti-aircraft Gun":           "aa_gun",
    "Armored Anti-aircraft Gun":   "aa_armored",
    "Mobile Anti-aircraft Gun":    "aa_mobile",
    "Light Artillery":             "arty_light",
    "Medium Artillery":            "arty_medium",
    "Heavy Artillery":             "arty_heavy",
    "Armored Artillery":           "arty_armored",
    "Heavy Armored Artillery":     "arty_armored_heavy",
    "Nuclear Artillery":           "arty_nuclear",
    "Atomic Missile":              "atomic_missile",
    "Construction truck":          "truck_construction",
    "Supply truck":                "truck_supply",
    "Decoy truck":                 "truck_decoy",
    "Fighter":                     "plane_fighter",
    "Advanced Fighter":            "plane_fighter_adv",
    "Jet Fighter":                 "plane_jet_fighter",
    "Fighter-bomber":              "plane_fighter_bomber",
    "Advanced Fighter-bomber":     "plane_fighter_bomber_adv",
    "Light Bomber":                "plane_bomber_light",
    "Medium Bomber":               "plane_bomber_medium",
    "Heavy Bomber":                "plane_bomber_heavy",
    "Jet Bomber":                  "plane_jet_bomber",
    "Air Recon":                   "plane_recon",
    "Unarmed Air Recon":           "plane_recon_unarmed",
    "Air Transport":               "plane_transport",
}

# building/defence MENU NAME -> role. The menu name is the building's function; the type label for a
# building only says how tough it is ('Resistant building'), which is not what you need on a map.
_BY_MENU_NAME = {
    "HEADQUARTERS":                 "bld_hq",
    "SECONDARY HEADQUARTERS":       "bld_hq2",
    "BARRACKS":                     "bld_barracks",
    "ARMOR BASE":                   "bld_armor_base",
    "ANTI-TANK BASE":               "bld_at_base",
    "ARTILLERY & ANTI-AIR BASE":    "bld_arty_base",
    "PROTOTYPE BASE":               "bld_prototype_base",
    "ATOMIC CENTER":                "bld_atomic_center",
    "AIRFIELD":                     "bld_airfield",
    "SUPPLY DEPOT":                 "bld_supply_depot",
    "ADMINISTRATIVE BUILDING":      "bld_admin",
    "DECOY BARRACKS":               "bld_barracks_decoy",
    "DECOY ARMOR BASE":             "bld_armor_base_decoy",
    "DECOY AT BASE":                "bld_at_base_decoy",
    "DECOY ARTILLERY & AA BASE":    "bld_arty_base_decoy",
    "DECOY AIRFIELD":               "bld_airfield_decoy",
    "DECOY ADMIN. BUILDING":        "bld_prototype_base_decoy",   # class family is ExperimentalFactory
    "DECOY DEFENSE":                "def_decoy",
    "MACHINE GUN NEST":             "def_mg",
    "MACHINE-GUN POSITION":         "def_mg",
    "ANTI-TANK BUNKER":             "def_at",
    "ANTI-TANK POSITION":           "def_at",
    "90MM ANTI-TANK POSITION":      "def_at",
    "ANTI-AIR BUNKER":              "def_aa",
    "AA POSITION":                  "def_aa",
    "ARTILLERY POSITION":           "def_arty",
    "HEAVY ARTILLERY POSITION":     "def_arty_heavy",
    "ARTILLERY SHELTER":            "def_arty_shelter",
    "105MM POSITION":               "def_105mm",
    "PILLBOX":                      "def_pillbox",
    "MAGINOT BUNKER":               "def_maginot",
    "SIEGFRIED BLOCKHAUS":          "def_siegfried",
    "FORTIFIED POSITION":           "def_fortified",
    "OUTPOST":                      "def_outpost",
    "BATTLESHIP":                   "ship_battleship",
    "HEAVY CRUISER":                "ship_cruiser",
    "DESTROYER":                    "ship_destroyer",
    "LCVP":                         "landing_craft",
    "LST":                          "landing_craft",
    "HQ TRUCK":                     "transport_hq",
    "V2 LAUNCHER":                  "rocket_v2",
}

FACTORY_AIRFIELD = 9        # Factory value that marks a unit as airfield-produced (= paradropped)


def _clean(s):
    """Menu/type strings come out of the .dic tables HTML-escaped ('&amp;') and sometimes padded."""
    if not s:
        return ""
    return s.replace("&amp;", "&").replace("&#39;", "'").strip()


def role_from_game_fields(class_name, catalog_category=None, type_label=None,
                          menu_name=None, factory=None) -> str:
    """The icon role for one placeable class, decided from the game's own descriptor fields.

    Pure function (no game files) so the mapping is testable and the build script can bake it.
      class_name       : e.g. 'Unit_M4_Sherman' / 'Building_Caserne' / 'Para_Soldat_US_Para'
      catalog_category : placement_catalog category ('unit'/'building'/'plane'/'para'/'depot')
      type_label       : resolved TypeUnitHintToken   ('Medium Tank')
      menu_name        : resolved NameInMenuToken     ('BARRACKS')
      factory          : the Factory int (9 = Airfield -> airborne)
    """
    tl = _clean(type_label)
    mn = _clean(menu_name).upper()

    # The depot pad is a scenario placement, not a catalogued descriptor.
    if class_name.endswith("DalleBatimentDepot") or catalog_category == "depot":
        return "depot"

    # Airborne first: paratroopers are ordinary infantry labels built at the AIRFIELD (Factory 9).
    # That single field is what separates 'Elite Infantry' the Legionnaires from 'Elite Infantry'
    # the Fallschirmjager, so it has to be tested before the plain type-label lookup.
    if catalog_category == "para":
        return "para_drop"
    if factory == FACTORY_AIRFIELD and tl in ("Elite Infantry", "Recon Infantry",
                                              "Light Infantry", "Heavy Infantry"):
        return "inf_para_recon" if tl == "Recon Infantry" else "inf_para"

    # Atomic bombers carry a plain 'Heavy Bomber' label; the class name is what marks the payload.
    if "Atomic_bomber" in class_name:
        return "plane_bomber_atomic"

    # Buildings and static defences: function comes from the menu name.
    if mn in _BY_MENU_NAME:
        return _BY_MENU_NAME[mn]

    if tl in _BY_TYPE_LABEL:
        return _BY_TYPE_LABEL[tl]

    # Classes the game leaves unlabelled — settle them by name, which is unambiguous here.
    if "Soldat" in class_name and "Antichar" in class_name:
        return "inf_at"
    if class_name.startswith("Unit_TransportSoldier"):
        return "transport_infantry"
    if class_name.startswith("Unit_TransportCanon"):
        return "transport_gun"
    if class_name.startswith("Unit_Unite_Vide"):
        return "empty_placeholder"
    if "DCAMGATNest" in class_name:
        return "def_mg_at_aa"

    # Last resort by broad catalog category, so nothing ever falls through to a bare dot.
    return {"plane": "plane_fighter", "building": "bld_admin",
            "unit": "spawn_other"}.get(catalog_category, "unknown")


# ── kinds that aren't Spawn classes at all (the scenario furniture) ───────────────
KIND_ROLE = {
    "hq":       "hq_start",
    "depot":    "depot",
    "name":     "waypoint",
    "ville":    "label_city",
    "montagne": "label_mountain",
    "circle":   "zone_circle",
    "rect":     "zone_rect",
    "spawn":    "spawn_other",
    "unknown":  "unknown",
}


# ── runtime lookup (what the editor calls) ────────────────────────────────────────
def load() -> dict:
    """The baked {class name -> role} map (data/placement_icon_roles.json)."""
    global _cache
    if _cache is None:
        try:
            with open(_DATA, encoding="utf-8") as f:
                _cache = json.load(f)
        except Exception:
            _cache = {"classes": {}}
    return _cache


def _short(pyclass):
    """'front.parametres.generated_data.ParamsUnites.Unit_M4_Sherman' -> 'Unit_M4_Sherman'."""
    return pyclass.rsplit(".", 1)[-1] if pyclass else ""


def role_for(kind, pyclass=None) -> str:
    """Icon role id for a parsed placement. `kind` is the map editor's placement kind; `pyclass` is
    the AddOn's PythonClassName for the Spawn-derived kinds. Always returns a role that exists."""
    if kind in ("unit", "building", "spawn", "depot") and pyclass:
        if pyclass.endswith("DalleBatimentDepot"):
            return "depot"
        rid = load().get("classes", {}).get(_short(pyclass))
        if rid and rid in ROLES:
            return rid
    rid = KIND_ROLE.get(kind)
    if rid and rid in ROLES:
        return rid
    return "spawn_other" if kind in ("unit", "building", "spawn") else "unknown"


def nation_for(pyclass) -> Optional[str]:
    """The unit's OWN nationality as an icons/nation_icons/ basename, or None.

    Fixed per class (the descriptor's `Nationalite`), and quite separate from who CONTROLS the
    placement — a French camp can be given German tanks. camp_resolver answers the controlling
    side; this answers what the thing itself is."""
    if not pyclass:
        return None
    return load().get("nations", {}).get(_short(pyclass))


def role(rid) -> Optional[Role]:
    return ROLES.get(rid)


def icon_name(rid) -> str:
    r = ROLES.get(rid)
    return r.icon if r else "unknown.png"


def all_roles():
    """Roles in table order — the legend order."""
    return list(ROLES.values())

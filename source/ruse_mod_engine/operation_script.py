"""Operation mission-script GENERATOR — structured operation model -> Python-2 `effetmap.py` source.

This is the scripting backbone of the Operations editor. The user builds an operation as DATA (camps,
game rules, objectives, AI behaviour, RUSE/bluff permissions, zone hooks); this emits the declarative
`leveldesign*` DSL source that the shipped operations use. The source is then compiled to `.xyz` by
`ruse_mod_engine/xyz_compile.compile_to_xyz` (bundled Python-2.5.1 worker) and written into IA_Common.dat.

Grounded in the decompiled operations (e.g. supercrossroads4/scripting_challenge): every operation is
imports -> Tag bindings -> VariableCamps -> GameRulesChallenge -> objective trees -> a
DescriptorLaunchEffetMap(CampList, GameRules, EffetMap, PostInit, TagList) -> def launch().

The generator is engine-only (no tkinter, no interpreter) and fully in-project. Source generation is
deterministic and validated (parses as Python; structurally matches the shipped scripts). The only
external dependency is the final source->bytecode step (CPython 2.7 worker).
"""
from dataclasses import dataclass, field
from typing import List, Optional, Any, Dict

from . import dsl_catalog

# ── enums surfaced from the DSL (_enum_for_game_play) — the values the shipped scripts use ──────────
NATIONS = ["EU", "Allemagne", "France", "Italie", "Japon", "RU", "URSS"]
AI_LEVELS = ["Player", "Scripted", "Easy", "Normal", "Hard"]          # NiveauIA.*
DIFFICULTES = ["Facile", "Normal", "Difficile"]                       # TDifficultes.*
COLORS = ["Color_01_Blue", "Color_02_Red", "Color_03_Green", "Color_04_Yellow",
          "Color_Neutre"]                                             # defines.colors.*
# RUSE / bluff cards a camp may be allowed or blocked from using (BluffCardEnum.*)
BLUFF_CARDS = ["BatimentFake_UsinePrototype", "BatimentFake_UsineChar", "BatimentFake_UsineArtillerie",
               "BatimentFake_UsineAntiChar", "BatimentFake_Caserne", "BatimentFake_Aeroport",
               "OffensiveFake_Generale", "OffensiveFake_Blindes", "OffensiveFake_Aviation",
               "Blitz", "Brouillage", "FauxPlan", "Espion", "Decryptage", "BatimentsCamoufles"]

OBJECTIVE_KINDS = ("eliminate_camp", "survive_time", "units_in_zone")


@dataclass
class Camp:
    var: str                              # python identifier, e.g. 'Camp_Player'
    alliance: int = 1
    nationalite: str = "EU"
    niveau_ia: str = "Player"             # 'Player' = human seat; 'Scripted'/'Normal'/... = AI
    couleur: str = "Color_01_Blue"
    difficulte: str = "Normal"
    message: str = ""
    partage_camion: bool = True
    player_name_challenge: bool = False
    activate_full_ai: bool = False        # emit DescriptorIAAll so the AI plays the whole map
    blocked_ruses: List[str] = field(default_factory=list)   # BluffCardEnum names this camp can't use


@dataclass
class GameRules:
    fow: int = 1
    game_time: int = 1500
    countdown: bool = True
    use_score: bool = True
    max_score: int = 0
    min_score: int = -1000
    construction_necessite_hq: bool = True
    disable_capture: bool = False
    ruse_zones_hud: bool = True           # show RUSE zones in the HUD (DescriptorHudElementsVisibility)


# ── generic DSL node model (the spine: forms now, a node-graph later, both produce this tree) ─────────
@dataclass
class Ref:
    """A symbolic reference resolved at emit time. kind ∈ {tag_pos,tag_zone,tag_unit,camp,var,group,timer}.
    tag_* refs are collected and emitted as Tag* handles (into TagList); the rest name a declared var."""
    kind: str
    name: str


@dataclass
class Block:
    """A catalog DSL class instantiation: `cls(**params)`. A param value may be a literal, a Ref, another
    Block, or a list of those. The catalog (dsl_catalog) supplies the import path, param types, and the
    defaults used to fill any param the user didn't set."""
    cls: str
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Objective:
    tracking_id: str                      # e.g. 'CH99_OB01'
    text: int = 0                         # label LocHash (0 = none); set via the loc tooling later
    bonus_score: int = 0                  # 0 = primary; 100-1500 = secondary/bonus points
    show_on_map: bool = True
    is_victory: bool = False              # success appends DescriptorDeclencheVictoireChallenge
    is_defeat: bool = False               # failure appends DescriptorDeclencheDefaite
    # NEW composable form: any condition tree + extra success/fail action blocks
    condition: Optional[Block] = None     # success condition (overrides the legacy `kind` path below)
    condition_fail: Optional[Block] = None
    actions_success: List[Block] = field(default_factory=list)
    actions_fail: List[Block] = field(default_factory=list)
    # LEGACY back-compat: the original 3 hardcoded kinds (used only when `condition` is None)
    kind: Optional[str] = None            # OBJECTIVE_KINDS or None
    target_camp: Optional[str] = None     # camp var (eliminate_camp)
    zone_tag: Optional[str] = None        # TagZoneDetection name (units_in_zone)
    seconds: int = 600                    # survive_time
    count: int = 1                        # units_in_zone threshold


@dataclass
class Event:
    """A scripted trigger: when `condition` becomes true, run `actions`. repeat=True re-arms forever."""
    name: str
    condition: Block
    actions: List[Block] = field(default_factory=list)
    repeat: bool = False


@dataclass
class AIDirective:
    """One or more AI control blocks (DescriptorIA*/ModifieParametresIA) applied to a camp, run at boot."""
    camp: str
    blocks: List[Block] = field(default_factory=list)


@dataclass
class VarDecl:
    """A named variable/timer/group declared once and referenced by Ref(...) elsewhere."""
    name: str
    block: Block                          # VariableInteger/VariableTimer/VariableUnitGroup(...)


@dataclass
class Operation:
    camps: List[Camp]
    rules: GameRules = field(default_factory=GameRules)
    objectives: List[Objective] = field(default_factory=list)
    events: List[Event] = field(default_factory=list)
    ai: List[AIDirective] = field(default_factory=list)
    variables: List[VarDecl] = field(default_factory=list)
    position_tags: List[str] = field(default_factory=list)   # TagPosition names referenced
    zone_tags: List[str] = field(default_factory=list)       # TagZoneDetection names referenced
    unit_tags: List[str] = field(default_factory=list)       # TagUnit names referenced
    defeat_on_no_units: Optional[str] = None                 # player camp var -> lose if it has 0 units


_IMPORTS = ("import _enum_for_game_play, defines.colors, leveldesign.camps, leveldesign.condition, "
            "leveldesign.descriptor, leveldesign.launcheffetmap, leveldesign.operator, "
            "leveldesign.variable, leveldesignsolo.condition, leveldesignsolo.contrainte_unit, "
            "leveldesignsolo.fx, leveldesignsolo.helper, leveldesignsolo.manipulation_card, "
            "leveldesignsolo.objectif, leveldesignsolo.strategic_ia, leveldesignsolo.timer, "
            "leveldesignsolo.units, parametres.Classes")


def _b(v):
    return "True" if v else "False"


def _s(v):
    return repr(str(v))


class _Emitter:
    """Accumulates `name = expr` statements, tracks PostInit/TagList registration, emits valid source."""
    def __init__(self):
        self.lines = []
        self.post_init = []     # names to register in DescriptorLaunchEffetMap.PostInit
        self.tags = []          # tag var names -> TagList
        self._n = 0

    def tmp(self, prefix="V"):
        self._n += 1
        return "%s_%d" % (prefix, self._n)

    def emit(self, name, expr, post_init=False):
        self.lines.append("%s = %s" % (name, expr))
        if post_init:
            self.post_init.append(name)
        return name


# PostInit holds ONLY these kinds (RE-confirmed across all 45 shipped scripts: camps, variables,
# conditions, objectives — never actions/AI/operators/control-flow).
_POST_INIT_KINDS = {"camp", "variable", "condition", "objective"}
_VAR_PREFIX = {"objective": "OBJ", "condition": "CND", "variable": "VAR", "camp": "CAMP",
               "ai": "AI", "action": "ACT", "control": "SEQ", "operator": "OP"}


def _elem_pspec(elem):
    """Param-spec for a list element kind (so list members format like the right type)."""
    return {
        "action": {"type": "block_ref"}, "condition": {"type": "block_ref"},
        "tag_pos": {"type": "tag", "tag_kind": "pos"}, "tag_zone": {"type": "tag", "tag_kind": "zone"},
        "tag_unit": {"type": "tag", "tag_kind": "unit"},
        "unit_type": {"type": "unit_type"}, "ref_unit": {"type": "ref"},
    }.get(elem, {"type": "object"})


def _format_default(p):
    """Source text for a param the user left unset — the catalog default, formatted by type."""
    t = p.get("type", "object"); d = p.get("default")
    if t == "bool":
        return "True" if d else "False"
    if t in ("int", "float"):
        return str(d) if d is not None else "0"
    if t in ("text", "loc"):
        return _s(d) if d is not None else "''"
    if t in ("list_of", "list"):
        return "[]"
    if t == "enum" and d and p.get("enum_domain"):
        return "_enum_for_game_play.%s.%s" % (p["enum_domain"], d)
    return "None"


def _resolve_value(e, val, p, ctx):
    """Render a param value to source: Block->recurse, Ref->resolved name, list->[...], else by type."""
    if isinstance(val, Block):
        return _emit_block(e, val, ctx)
    if isinstance(val, Ref):
        if val.kind.startswith("tag"):
            return ctx["tags"].get((val.kind, val.name)) or _emit_tag(e, ctx, val.kind, val.name)
        return val.name                                  # camp/var/group/timer = a declared var name
    if isinstance(val, list):
        ep = _elem_pspec((p or {}).get("elem"))
        return "[%s]" % ", ".join(_resolve_value(e, x, ep, ctx) for x in val)
    t = (p or {}).get("type", "object")
    if val is None:
        return "None"
    if t == "enum":
        return "_enum_for_game_play.%s.%s" % ((p or {}).get("enum_domain"), val)
    if t == "unit_type":
        return "parametres.Classes.%s" % str(val).rsplit(".", 1)[-1]
    if t == "bool":
        return _b(val)
    if t in ("int", "float"):
        return str(val)
    if t in ("text", "loc"):
        return _s(val)
    if t in ("camp_ref", "ref"):
        return str(val)                                  # raw declared-var name
    if isinstance(val, bool):
        return _b(val)
    if isinstance(val, (int, float)):
        return str(val)
    return _s(val)


def _emit_block(e, block, ctx, name=None):
    """Emit `name = path.Cls(kw=...)` for a Block, filling every catalog param (user value or default).
    Children emit first (post-order). Returns the assigned var name."""
    rec = dsl_catalog.info_for(block.cls) or {}
    path = rec.get("path")
    qual = (path + "." + block.cls) if path else block.cls
    params = rec.get("params") or []
    kw, seen = [], set()
    for p in params:
        pn = p["name"]; seen.add(pn)
        v = _resolve_value(e, block.params[pn], p, ctx) if pn in block.params else _format_default(p)
        kw.append("%s=%s" % (pn, v))
    for pn, raw in block.params.items():                 # advanced: user params the catalog didn't list
        if pn not in seen:
            kw.append("%s=%s" % (pn, _resolve_value(e, raw, {"type": "object"}, ctx)))
    var = name or e.tmp(_VAR_PREFIX.get(rec.get("kind"), "B"))
    e.emit(var, "%s(%s)" % (qual, ", ".join(kw)), post_init=(rec.get("kind") in _POST_INIT_KINDS))
    return var


def _emit_tag(e, ctx, kind, nm):
    """Emit (once) a Tag* handle for a placement Name and register it in TagList."""
    key = (kind, nm)
    if key in ctx["tags"]:
        return ctx["tags"][key]
    helper = {"tag_pos": "TagPosition", "tag_zone": "TagZoneDetection", "tag_unit": "TagUnit"}[kind]
    v = e.tmp("T")
    e.emit(v, "leveldesignsolo.helper.%s(%s)" % (helper, _s(nm)))
    e.tags.append(v); ctx["tags"][key] = v
    return v


def _iter_values(v):
    """Yield every value node (Blocks, Refs, leaves) in a param-value tree."""
    yield v
    if isinstance(v, Block):
        for pv in v.params.values():
            for x in _iter_values(pv):
                yield x
    elif isinstance(v, list):
        for it in v:
            for x in _iter_values(it):
                yield x


def _all_top_blocks(op):
    """Every top-level Block in an operation (for tag collection)."""
    blocks = []
    for o in op.objectives:
        blocks += [o.condition, o.condition_fail] + list(o.actions_success) + list(o.actions_fail)
    for ev in op.events:
        blocks += [ev.condition] + list(ev.actions)
    for d in op.ai:
        blocks += list(d.blocks)
    for vd in op.variables:
        blocks.append(vd.block)
    return [b for b in blocks if isinstance(b, Block)]


def generate_source(op: Operation) -> str:
    """Render `op` to a Python-2.7 effetmap.py source string (deterministic, parseable)."""
    e = _Emitter()
    out = [_IMPORTS, ""]

    # ── tag bindings: explicit op.*_tags PLUS every Ref(tag_*) used anywhere in the block tree ─────
    ctx = {"tags": {}}
    for kind, names in (("tag_pos", op.position_tags), ("tag_zone", op.zone_tags),
                        ("tag_unit", op.unit_tags)):
        for nm in names:
            _emit_tag(e, ctx, kind, nm)
    for b in _all_top_blocks(op):
        for v in _iter_values(b):
            if isinstance(v, Ref) and v.kind.startswith("tag"):
                _emit_tag(e, ctx, v.kind, v.name)

    # ── camps ──────────────────────────────────────────────────────────────────────
    for c in op.camps:
        expr = ("leveldesign.camps.VariableCamp(Alliance=%d, CampNumber=-1, "
                "Couleur=defines.colors.%s, Nationalite=_enum_for_game_play.Nationalite.%s, "
                "NiveauIA=_enum_for_game_play.NiveauIA.%s, "
                "Difficulte=_enum_for_game_play.TDifficultes.%s, "
                "Profil=_enum_for_game_play.TProfils.ProfilStandard, PartageCamion=%s, "
                "PlayerNameChallenge=%s, Message=%s, AllianceName='', LocalizedName=None)"
                % (c.alliance, c.couleur, c.nationalite, c.niveau_ia, c.difficulte,
                   _b(c.partage_camion), _b(c.player_name_challenge), _s(c.message)))
        e.emit(c.var, expr, post_init=True)

    # ── game rules ──────────────────────────────────────────────────────────────────
    e.emit("GameRules", ("leveldesign.launcheffetmap.GameRulesChallenge(IsMapMulti=False, "
           "UseScore=%s, GameTimeMode=_enum_for_game_play.TGameTimerMode.%s, GameTime=%d, "
           "MinScore=%d, MaxScore=%d, Fow=%d, ConstructionNecessiteHQ=%s, DisableCapture=%s, "
           "ModeFixedRandomSeed=True, PopCapLimit=_enum_for_game_play.TPopCapConfig.PopCapSolo)"
           % (_b(op.rules.use_score), "Countdown" if op.rules.countdown else "Chronometer",
              op.rules.game_time, op.rules.min_score, op.rules.max_score, op.rules.fow,
              _b(op.rules.construction_necessite_hq), _b(op.rules.disable_capture))))

    # ── AI activation + RUSE/bluff permissions per camp ──────────────────────────────
    boot = []     # actions run once at start (DescriptorSimultaneous)
    for c in op.camps:
        if c.activate_full_ai and c.niveau_ia != "Player":
            v = e.tmp("AI")
            e.emit(v, "leveldesignsolo.strategic_ia.DescriptorIAAll(Camp=%s)" % c.var, post_init=True)
            boot.append(v)
        for card in c.blocked_ruses:
            v = e.tmp("RUSE")
            e.emit(v, ("leveldesignsolo.manipulation_card.DescriptorBloqueCarteBluff("
                       "AppliqueAuxCartesDuMemeType=True, Bloque=True, Camp=%s, "
                       "Carte=_enum_for_game_play.BluffCardEnum.%s, ClearCartesActives=False)"
                       % (c.var, card)), post_init=True)
            boot.append(v)
    if op.rules.ruse_zones_hud:
        v = e.tmp("HUD")
        e.emit(v, ("leveldesign.camps.DescriptorHudElementsVisibility(AlertMessages=True, BluffMenu=True,"
                   " BuildMenu=True, Devises=True, EtiquettesJoueurs=True, FastStrike=True, Highlight=True,"
                   " PadInfos=True, RuseZones=True, Score=True, ScoreAdverse=True, Selection=True,"
                   " SituationAwareness=True, TimeRemaining=True, UnitHints=True)"), post_init=True)
        boot.append(v)

    # ── declared variables / timers / groups (named, referenced by Ref elsewhere) ─────
    for vd in op.variables:
        _emit_block(e, vd.block, ctx, name=vd.name)

    # ── per-camp AI directives (DescriptorIA*/ModifieParametresIA), run at boot ────────
    for d in op.ai:
        for blk in d.blocks:
            boot.append(_emit_block(e, blk, ctx))

    # ── objectives (primary + secondary/bonus) ────────────────────────────────────────
    obj_vars = []
    for o in op.objectives:
        cond = _emit_block(e, o.condition, ctx) if o.condition is not None else _emit_condition(e, o, ctx)
        cond_fail = _emit_block(e, o.condition_fail, ctx) if o.condition_fail is not None else "None"
        succ = [_emit_block(e, a, ctx) for a in o.actions_success]
        if o.is_victory:
            succ.append(e.emit(e.tmp("WIN"),
                               "leveldesignsolo.objectif.DescriptorDeclencheVictoireChallenge()"))
        fail = [_emit_block(e, a, ctx) for a in o.actions_fail]
        if o.is_defeat:
            fail.append(e.emit(e.tmp("LOSE"), "leveldesignsolo.objectif.DescriptorDeclencheDefaite()"))
        label = e.tmp("LBL")
        e.emit(label, ("leveldesignsolo.fx.DescriptorDrawLabel(DoesAllFusionnedObjectivesNeedSucces=True,"
               " FoldedText=%d, Group=None, HeriteVisibiliteFromUnitGroup=True, IdForFusion=0,"
               " IsObjectif=True, ListTimers=[], ListTimersForFoldedText=[], ListVariables=[],"
               " ListVariablesForFoldedText=[], Position=None, Style=1, Text=%d, VisibleByCamp=None)"
               % (o.text, o.text)))
        ov = e.tmp("OBJ")
        e.emit(ov, ("leveldesignsolo.objectif.DescriptorGereObjectifAvecLabel(ActionsEchec=%s,"
               " ActionsReussi=%s, BonusScoreMission=%d, Condition=%s, ConditionEchec=%s,"
               " ConditionFin=None, IdForAchievement=0, ImmediateSubActions=False, Label=%s,"
               " LabelsScondaires=[], ShowFailAnimation=False, ShowNewObjectif=True, ShowOnMap=%s,"
               " TrackingId=%s, UnitGroupPosition=None)"
               % (_seq(e, fail), _seq(e, succ), o.bonus_score, cond, cond_fail, label,
                  _b(o.show_on_map), _s(o.tracking_id))), post_init=True)
        obj_vars.append(ov)

    # ── events: when <condition> then <actions> (re-arming if repeat) ──────────────────
    for ev in op.events:
        cond = _emit_block(e, ev.condition, ctx)
        wc = e.emit(e.tmp("WC"),
                    "leveldesign.descriptor.DescriptorWaitCondition(Condition=%s)" % cond)
        acts = [_emit_block(e, a, ctx) for a in ev.actions]
        boot.append(e.emit(e.tmp("EV"),
                    "leveldesign.descriptor.DescriptorSequential(Repeat=%s, SubActions=[%s])"
                    % (_b(ev.repeat), ", ".join([wc] + acts))))

    # ── defeat: player loses all units ────────────────────────────────────────────────
    if op.defeat_on_no_units:
        lose = e.tmp("LOSE")
        e.emit(lose, "leveldesignsolo.objectif.DescriptorDeclencheDefaite()", post_init=True)
        op0 = OperatorLE(e, 0)
        flt = e.tmp("FLT")
        e.emit(flt, "leveldesignsolo.contrainte_unit.ContrainteOnUnitTeam(Camp=%s, OperatorType=0)"
               % op.defeat_on_no_units)
        grp = e.tmp("GRP"); e.emit(grp, "leveldesignsolo.units.VariableUnitGroup(UnitList=[])",
                                   post_init=True)
        addall = e.tmp("ADD")
        e.emit(addall, "leveldesignsolo.units.DescriptorAddAllCampUnitsToUnitGroup(Camp=%s, Group=%s,"
               " IncludeFake=False)" % (op.defeat_on_no_units, grp))
        cond = e.tmp("CND")
        e.emit(cond, "leveldesignsolo.condition.ConditionUnitGroup(Operator=%s, Variable=%s)" % (op0, grp),
               post_init=True)
        waitlose = e.tmp("WL")
        e.emit(waitlose, "leveldesign.descriptor.DescriptorSequential(Repeat=False, SubActions=[%s])"
               % e.emit(e.tmp("WC"),
                        "leveldesign.descriptor.DescriptorWaitCondition(Condition=%s)" % cond))
        defeat_seq = e.tmp("DSEQ")
        e.emit(defeat_seq, "leveldesign.descriptor.DescriptorSequential(Repeat=False, SubActions=[%s, %s])"
               % (waitlose, lose))
        boot.append(addall); boot.append(defeat_seq)

    # ── assemble the EffetMap action tree ──────────────────────────────────────────────
    effet_subs = boot + obj_vars
    e.emit("EffetMap", "leveldesign.descriptor.DescriptorSimultaneous(SubActions=[%s])"
           % ", ".join(effet_subs) if effet_subs else
           "leveldesign.descriptor.DescriptorSimultaneous(SubActions=[])")

    # ── launcher + launch() ─────────────────────────────────────────────────────────────
    camp_list = ", ".join(c.var for c in op.camps)
    post = ", ".join(e.post_init)
    tags = ", ".join(e.tags)
    e.emit("ER_ROOT", ("leveldesign.launcheffetmap.DescriptorLaunchEffetMap(CampList=[%s], "
           "EffetMap=EffetMap, GameRules=GameRules, PostInit=[%s], TagList=[%s], "
           "UnitToSpawnList=[], CampaignInfo=None, MapChapterPos=None)" % (camp_list, post, tags)))

    out += e.lines
    out += ["", "def launch():", "    import leveldesign.launcher",
            "    return leveldesign.launcher.launch(ER_ROOT)", ""]
    return "\n".join(out) + "\n"


def OperatorLE(e, value):
    v = e.tmp("OP")
    e.emit(v, "leveldesign.operator.OperatorIntegerLessOrEqual(Value=%d, Variable=None)" % value)
    return v


def OperatorGE(e, value):
    v = e.tmp("OP")
    e.emit(v, "leveldesign.operator.OperatorIntegerMoreOrEqual(Value=%d, Variable=None)" % value)
    return v


def _seq(e, var_names):
    """Wrap action var names in a DescriptorSequential, or 'None' if empty (for objective Actions*)."""
    if not var_names:
        return "None"
    return e.emit(e.tmp("SEQ"), "leveldesign.descriptor.DescriptorSequential(Repeat=False, "
                  "SubActions=[%s])" % ", ".join(var_names))


def _emit_condition(e, o: Objective, ctx):
    """LEGACY: emit the success condition for the 3 hardcoded objective kinds; return its var name."""
    if o.kind == "eliminate_camp" and o.target_camp:
        grp = e.tmp("GRP"); e.emit(grp, "leveldesignsolo.units.VariableUnitGroup(UnitList=[])",
                                   post_init=True)
        add = e.tmp("ADD")
        e.emit(add, "leveldesignsolo.units.DescriptorAddAllCampUnitsToUnitGroup(Camp=%s, Group=%s,"
               " IncludeFake=False)" % (o.target_camp, grp))
        op0 = OperatorLE(e, 0)
        cond = e.tmp("CND")
        e.emit(cond, "leveldesignsolo.condition.ConditionUnitGroup(Operator=%s, Variable=%s)" % (op0, grp),
               post_init=True)
        return cond
    if o.kind == "survive_time":
        timer = e.tmp("TMR")
        e.emit(timer, "leveldesignsolo.timer.VariableTimer(Duree=%d)" % o.seconds, post_init=True)
        start = e.tmp("ST"); e.emit(start, "leveldesignsolo.timer.DescriptorStartTimer(Timer=%s)" % timer)
        op0 = OperatorLE(e, 0)
        cond = e.tmp("CND")
        e.emit(cond, "leveldesignsolo.timer.ConditionTimer(Operator=%s, Variable=%s)" % (op0, timer),
               post_init=True)
        return cond
    if o.kind == "units_in_zone" and o.zone_tag:
        zv = _emit_tag(e, ctx, "tag_zone", o.zone_tag)
        opn = OperatorGE(e, o.count)
        cond = e.tmp("CND")
        e.emit(cond, ("leveldesignsolo.condition.ConditionDetectUnitDansZoneDetection(ComparaisonNbUnits=%s,"
               " Contrainte=None, Detecteur=%s, OrbitIgnore=False)" % (opn, zv)), post_init=True)
        return cond
    # fallback: always-false placeholder via an unsatisfiable integer condition
    op0 = OperatorGE(e, 1)
    iv = e.tmp("IV"); e.emit(iv, "leveldesign.variable.VariableInteger(Value=0)", post_init=True)
    cond = e.tmp("CND")
    e.emit(cond, "leveldesign.condition.ConditionVariableInteger(Operator=%s, Variable=%s)" % (op0, iv),
           post_init=True)
    return cond


def starter_operation(player_nation="EU", enemy_nation="Allemagne", game_time=1500):
    """A minimal complete operation: human vs scripted AI, eliminate victory, lose-all defeat."""
    player = Camp("Camp_Player", alliance=1, nationalite=player_nation, niveau_ia="Player",
                  couleur="Color_01_Blue", player_name_challenge=True)
    enemy = Camp("Camp_Enemy", alliance=2, nationalite=enemy_nation, niveau_ia="Scripted",
                 couleur="Color_02_Red", partage_camion=False, activate_full_ai=True)
    objs = [Objective("CH_OB01", kind="eliminate_camp", target_camp="Camp_Enemy", is_victory=True,
                      bonus_score=0)]
    return Operation(camps=[player, enemy], rules=GameRules(game_time=game_time),
                     objectives=objs, defeat_on_no_units="Camp_Player")


# ── starter TEMPLATES (the "hand-holding": pre-built operations the user tweaks) ──────────────────────
def template_1v1(player_nation="EU", enemy_nation="Allemagne"):
    """You vs one scripted AI — destroy the enemy. + a bonus objective for finishing under a timer."""
    op = starter_operation(player_nation, enemy_nation, game_time=1800)
    op.variables.append(VarDecl("T_Bonus", Block("VariableTimer", {"Duree": 900})))
    op.objectives.append(Objective("CH_OB02", bonus_score=1000,
                                   condition=Block("ConditionTimer", {"Timer": Ref("timer", "T_Bonus")})))
    op.events.append(Event("start_bonus_timer", condition=Block("ConditionTimer", {"Timer": Ref("timer", "T_Bonus")})))
    return op


def template_1vall(player_nation="EU", enemy_nations=("Allemagne", "Italie")):
    """You vs several AI camps (1 vs All). Win by eliminating them all; lose if wiped out."""
    camps = [Camp("Camp_Player", 1, player_nation, "Player", player_name_challenge=True)]
    objs = []
    for i, nat in enumerate(enemy_nations, start=1):
        v = "Camp_Enemy%d" % i
        camps.append(Camp(v, alliance=i + 1, nationalite=nat, niveau_ia="Scripted",
                          couleur="Color_02_Red", partage_camion=False, activate_full_ai=True))
        objs.append(Objective("CH_OB%02d" % i, kind="eliminate_camp", target_camp=v,
                              is_victory=(i == len(enemy_nations))))
    return Operation(camps=camps, rules=GameRules(game_time=2400), objectives=objs,
                     defeat_on_no_units="Camp_Player")


def template_survive(minutes=10, enemy_nation="Allemagne"):
    """Hold out: survive a countdown while the AI attacks. Win at time-out; lose if wiped."""
    op = starter_operation("EU", enemy_nation, game_time=minutes * 60)
    op.objectives = [Objective("CH_OB01", kind="survive_time", seconds=minutes * 60, is_victory=True)]
    op.ai = [AIDirective("Camp_Enemy", [Block("DescriptorIAAll", {"Camp": "Camp_Enemy"})])]
    return op


def template_capture_zone(zone_name="ZoneObjectif", enemy_nation="Allemagne"):
    """Take and hold a zone: win when your units occupy it. Lose if wiped out."""
    op = starter_operation("EU", enemy_nation)
    op.zone_tags = [zone_name]
    op.objectives = [Objective("CH_OB01", kind="units_in_zone", zone_tag=zone_name, count=1, is_victory=True)]
    return op


TEMPLATES = [
    ("1 vs 1 (destroy the enemy)", template_1v1),
    ("1 vs All (beat every AI)", template_1vall),
    ("Survive the timer", template_survive),
    ("Capture a zone", template_capture_zone),
]

"""Operation/mission registration backend for the editor.

Emits the proven globals.cpp change-set that makes a NEW operation appear in-game: 4 TUIResourceTexture
creates (briefing/map/end-game art) + 1 TChallengeMapInfo create (name/brief LocHashes, GUID, popcaps,
bonuses) + 1 TChallengePack patch (append to ChallengeList). Templated off the in-game-proven Overlord
registration; parameterized. ObjRefs are wired by `local_id` $ref so the applier resolves them on the
target build.

  reg = OperationReg(guid=..., name_hash=..., brief_hashes=[...], textures=[...])
  changes = operation_registration(reg, existing_challenges)   # -> List[ModChange] for globals.cpp

`existing_challenges` = the target build's current TChallengePack.ChallengeList (list of {"inst","class"}
ObjRef dicts) so the new challenge is APPENDED, not replacing. Read it from the target globals.cpp via
`current_challenge_list(globals_ndf)`. The GUID must match the host map slot's TMapLoadInfo GUID (the
scenario you overwrite); new LocHashes need matching .dic entries (see dic.py / [[reference-lochash-dic]]).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

from .mod_format import ModChange, ModValueDef

# UsefulnessMask values from the proven Overlord registration (briefing/map/endgame-win, endgame-lose)
_TEX_MASKS = [16793600, 16793600, 16793600, 33570816]
_TEX_IDS = ["tex_brief", "tex_map", "tex_endgame_win", "tex_endgame_lose"]
CHALLENGE_PACK_INDEX = 96            # TChallengePack._index (byte-stable across modern builds)


@dataclass
class OperationReg:
    guid: str                                   # 32 hex chars; must match host slot's TMapLoadInfo GUID
    name_hash: str                              # Description LocHash (operation name), 16 hex
    brief_hashes: List[str] = field(default_factory=list)   # LongDescription1..4 LocHashes (pad to 4)
    textures: List[str] = field(default_factory=list)       # 4 DataDir png paths (brief/map/eg-win/eg-lose)
    tracking_id: str = "CHXX"
    nbplayers: int = 1
    popcap_player: int = 70
    popcap_ia: int = 170
    category_id: int = 4
    bonus_time: List[int] = field(default_factory=lambda: [1800, 2400])
    bonus_survival: List[int] = field(default_factory=lambda: [0, 0])
    colors: List[int] = field(default_factory=lambda: [0, 0])
    nations: List[int] = field(default_factory=lambda: [1, 1])


def _v(type_name, value):
    return ModValueDef(type_name=type_name, value=value)


def _ref(local_id):
    return ModValueDef(type_name="$ref", value=local_id)


def current_challenge_list(globals_ndf) -> List[Dict[str, int]]:
    """Read the target build's TChallengePack.ChallengeList as a list of ObjRef dicts (for appending)."""
    nb = globals_ndf
    ci = next((c.index for c in nb.classes if c.name == "TChallengePack"), None)
    if ci is None:
        return []
    inst = nb.instances[CHALLENGE_PACK_INDEX] if CHALLENGE_PACK_INDEX < len(nb.instances) else None
    if inst is None or inst.class_index != ci:
        inst = next((x for x in nb.instances if x.class_index == ci), None)
    if inst is None:
        return []
    p = nb.prop_by_name_and_class("ChallengeList", ci)
    v = inst.get(p.index) if p else None
    out = []
    if v is not None and hasattr(v, "raw"):
        for item in v.raw:
            r = getattr(item, "raw", item)
            if isinstance(r, tuple) and len(r) == 2 and isinstance(r[1], tuple):
                out.append({"inst": r[1][0], "class": r[1][1]})
    return out


def operation_registration(reg: OperationReg,
                           existing_challenges: Optional[List[Dict[str, int]]] = None,
                           pack_index: int = CHALLENGE_PACK_INDEX) -> List[ModChange]:
    changes: List[ModChange] = []

    # 1) textures (pad/truncate to 4)
    tex = list(reg.textures)[:4] + [""] * (4 - len(reg.textures))
    for tid, png, mask in zip(_TEX_IDS, tex, _TEX_MASKS):
        changes.append(ModChange(action="create", table="TUIResourceTexture", match=None,
                                 set_props={"UsefulnessMask": _v("UInt32", mask),
                                            "FileName": _v("PathRef", png)},
                                 local_id=tid))

    # 2) the operation entry
    briefs = (list(reg.brief_hashes) + [reg.name_hash] * 4)[:4]
    sp: Dict[str, ModValueDef] = {
        "CategoryId": _v("Int32", reg.category_id),
        "Guid": _v("Guid", reg.guid),
        "Description": _v("LocHash", reg.name_hash),
        "LongDescription1": _v("LocHash", briefs[0]),
        "LongDescription2": _v("LocHash", briefs[1]),
        "LongDescription3": _v("LocHash", briefs[2]),
        "LongDescription4": _v("LocHash", briefs[3]),
        "TrackingId": _v("StringRef", reg.tracking_id),
        "NbPlayers": _v("Int32", reg.nbplayers),
        "TextureEndGamePath": _ref("tex_endgame_win"),
        "BonusTime": _v("List<Int32>", list(reg.bonus_time)),
        "BonusSurvival": _v("List<Int32>", list(reg.bonus_survival)),
        "PopCapPlayer": _v("Int32", reg.popcap_player),
        "PopCapIA": _v("Int32", reg.popcap_ia),
        "PlayersColors": _v("List<Int32>", list(reg.colors)),
        "PlayersNations": _v("List<Int32>", list(reg.nations)),
    }
    changes.append(ModChange(action="create", table="TChallengeMapInfo", match=None, set_props=sp,
                             local_id="op_challenge"))

    # 3) append to the pack's ChallengeList
    new_list: List[Any] = list(existing_challenges or []) + [{"$ref": "op_challenge"}]
    changes.append(ModChange(action="patch", table="TChallengePack",
                             match={"_index": str(pack_index)},
                             set_props={"ChallengeList": _v("List<ObjRef>", new_list)}))
    return changes

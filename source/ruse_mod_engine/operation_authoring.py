"""Operation authoring engine — the backend for the map editor's "Names & Descriptions" tab.

Generalizes the proven Desert Lion recipe:
  - SCAN a base operation: its registration GUID + every objective's text LocHash (+ current text).
  - EDIT name / description / each objective text in a plain draft model.
  - EMIT a deployable rmod: a NEW TChallengeMapInfo (registration.py) + loc_patches that EDIT the live,
    populated .dics (flash_txt for name/brief, the base op's localization.dic for objectives) — NEVER
    rebuilding from the zero-filled source dats (see memory: project-clean-operation-authoring).

The GUI calls scan_base_operation(...) to populate fields, mutates the OperationDraft, then to_rmod().
"""
from __future__ import annotations

import re
import glob
import os
import struct
from dataclasses import dataclass, field
from typing import List, Optional, Dict

from . import edata, ndfbin
from .script_edit import Script
from .registration import OperationReg, operation_registration, current_challenge_list
from .mod_format import RuseMod, ModPatchGroup, ModLocGroup, ModLocEntry

ZZ_WIN = "Data/PC/190852/ZZ_Win.dat"
ZZ_GLAD = "Data/PC/190852/ZZ_GladPatchableWin.dat"
GLOBALS_NDF = "genglad/patchable/misc/globals.cpp.gladndfbin"
BS = "\\"


@dataclass
class Objective:
    label: str                      # script var (e.g. IR_100) — for display
    text_key: str                   # localization.dic key (hex of LE-u64 script LocHash)
    text: str = ""                  # current/edited text
    folded_key: Optional[str] = None
    folded_text: str = ""
    is_objective: bool = False      # IsObjectif True => always shown; False => unit-group-bound
    group: Optional[str] = None


@dataclass
class OperationDraft:
    """Editable model the GUI binds to."""
    base_map: str                   # e.g. "alpha"
    base_guid: str                  # TMapLoadInfo GUID to reuse (the working map-load)
    loc_dic_paths: List[str]        # the base op's localization.dic paths (all languages)
    flash_paths: List[str]          # flash_txt.dic paths (all languages)
    objectives: List[Objective] = field(default_factory=list)
    name: str = "New Operation"
    description: str = ""
    name_hash: str = "01de5e7110000000"
    brief_hash: str = "02de5e7110000000"
    tracking_id: str = "OP01"
    category_id: int = 4
    nbplayers: int = 1
    textures: List[str] = field(default_factory=list)

    # ---- emit ----
    def to_rmod(self, existing_challenges, mod_name=None, mod_id=None) -> RuseMod:
        reg = OperationReg(guid=self.base_guid, name_hash=self.name_hash,
                           brief_hashes=[self.brief_hash] * 4, textures=self.textures,
                           tracking_id=self.tracking_id, nbplayers=self.nbplayers,
                           category_id=self.category_id)
        changes = operation_registration(reg, existing_challenges)

        mod = RuseMod(schema="ruse-mod/v1", id=mod_id or ("operation_" + re.sub(r"\W+", "_", self.name.lower())),
                      name=mod_name or self.name, version="1.0.0", author="ModManager",
                      description="Authored operation on the %s map." % self.base_map,
                      game_version="190852", patches=[], file_patches=[], loc_patches=[])

        mod.patches.append(ModPatchGroup(dat=ZZ_GLAD, ndf=GLOBALS_NDF, changes=changes,
                                         create_if_missing=False, index_map=None))

        # name + brief -> flash_txt (edit live populated .dic; applier add/sets)
        for vp in self.flash_paths:
            mod.loc_patches.append(ModLocGroup(dat=ZZ_WIN, dic=vp.replace(BS, "/"), entries=[
                ModLocEntry(key=self.name_hash, value=self.name, add=True),
                ModLocEntry(key=self.brief_hash, value=self.description, add=True),
            ]))

        # objective text -> base op localization.dic (set existing keys, preserve the rest)
        obj_entries = []
        for o in self.objectives:
            if o.text:
                obj_entries.append(ModLocEntry(key=o.text_key, value=o.text, add=False))
            if o.folded_key and o.folded_text:
                obj_entries.append(ModLocEntry(key=o.folded_key, value=o.folded_text, add=False))
        if obj_entries:
            for vp in self.loc_dic_paths:
                mod.loc_patches.append(ModLocGroup(dat=ZZ_WIN, dic=vp.replace(BS, "/"),
                                                   entries=[ModLocEntry(e.key, e.value, e.add) for e in obj_entries]))
        return mod


def _open(src_dir, dat_name):
    return edata.open_dat(glob.glob(os.path.join(src_dir, "Data", "PC", "*", dat_name))[0])


def list_base_operations(game_root) -> List[Dict[str, str]]:
    """Discover playable single-player operations to clone: each map's scripting folder with an effetmap.
    Returns [{map, script_folder, label}] for the "Scenarios" tab / base-op dropdown."""
    ia = _open(game_root, "IA_Common.dat")
    out = []
    seen = set()
    for v in ia.list():
        vl = v.lower()
        if not vl.endswith("effetmap.xyz"):
            continue
        parts = v.replace("/", BS).split(BS)
        # .../test/map/<map>/<script_folder>/effetmap.xyz
        try:
            mi = parts.index([p for p in parts if p.lower() == "map"][0])
            mp = parts[mi + 1]
            sf = parts[mi + 2]
        except Exception:
            continue
        key = (mp.lower(), sf.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"map": mp, "script_folder": sf, "label": "%s / %s" % (mp, sf)})
    out.sort(key=lambda d: d["label"].lower())
    return out


def scan_base_operation(base_map, src_dir, script_folder="scripting_bh", live_dat=None) -> OperationDraft:
    """Build an OperationDraft from a base operation: objective LocHashes from its effetmap, current text
    from the LIVE populated localization.dic (if `live_dat` given), the map-load GUID, and the .dic paths."""
    ia = _open(src_dir, "IA_Common.dat")
    ev = next(v for v in ia.list() if v.lower().endswith("effetmap.xyz")
              and (BS + base_map + BS) in v.lower() and script_folder.lower() in v.lower())
    src = Script.load(ia.get(ev)).decompile()

    objectives: List[Objective] = []
    seen = set()
    for m in re.finditer(r"DescriptorDrawLabel\(([^)]*)\)", src):
        body = m.group(1)
        def f(k):
            mm = re.search(k + r"=([0-9]+)L", body)
            return int(mm.group(1)) if mm else None
        t = f("Text")
        fld = f("FoldedText")
        if t is None or t in seen:
            continue
        seen.add(t)
        isobj = bool(re.search(r"IsObjectif=True", body))
        grp = re.search(r"Group=(IR_\d+|None)", body)
        objectives.append(Objective(
            label="obj%d" % (len(objectives) + 1),
            text_key=struct.pack("<Q", t).hex(),
            folded_key=(struct.pack("<Q", fld).hex() if fld is not None and fld != t else None),
            is_objective=isobj, group=(grp.group(1) if grp else None)))

    # map-load GUID + the op's localization.dic / flash_txt paths
    zz = _open(src_dir, "ZZ_Win.dat")
    loc_paths = [v for v in zz.list() if v.lower().endswith("localization.dic")
                 and (BS + base_map + BS) in v.lower() and script_folder.lower() in v.lower()]
    flash_paths = [v for v in zz.list() if v.lower().endswith("flash_txt.dic")]

    glad = _open(src_dir, "ZZ_GladPatchableWin.dat")
    m = ndfbin.read(glad.get(next(v for v in glad.list() if v.lower().endswith("mapinfo.cpp.gladndfbin"))))
    mci = next(c.index for c in m.classes if c.name == "TMapLoadInfo")
    guid = ""
    for inst in [x for x in m.instances if x.class_index == mci]:
        nm = ""
        for pv in inst.props:
            if m.properties[pv.prop_index].name == "Name":
                try:
                    nm = m.get_string(pv.value.raw)
                except Exception:
                    nm = ""
        if "challenge" in nm.lower() and base_map.lower() in nm.lower():
            for pv in inst.props:
                if m.properties[pv.prop_index].name == "GUID":
                    guid = pv.value.raw.hex() if isinstance(pv.value.raw, bytes) else str(pv.value.raw)

    # current objective text from the populated localization.dic (game_root ZZ_Win, or a given live_dat).
    # When src_dir == game_root (the live install), `zz` is already populated, so no separate dat is needed.
    from . import dic as _dic
    ld = edata.open_dat(live_dat) if live_dat else zz
    usvp = next((v for v in ld.list() if v.replace(BS, "/").lower().endswith(
        "translations/us/%s/%s/localization.dic" % (base_map.lower(), script_folder.lower()))), None)
    if usvp:
        blob = ld.get(usvp)
        if blob[:3] == b"TRA":
            for o in objectives:
                cur = _dic.get_entry(blob, bytes.fromhex(o.text_key))
                if cur:
                    o.text = cur

    # default textures (reuse existing TUIResourceTexture pngs)
    gnb = ndfbin.read(glad.get(next(v for v in glad.list() if v.lower().endswith("globals.cpp.gladndfbin"))))
    tci = next(c.index for c in gnb.classes if c.name == "TUIResourceTexture")
    tex = []
    for inst in [x for x in gnb.instances if x.class_index == tci]:
        for pv in inst.props:
            if gnb.properties[pv.prop_index].name == "FileName":
                try:
                    fn = gnb.get_string(pv.value.raw)
                    if fn.lower().endswith(".png"):
                        tex.append(fn)
                except Exception:
                    pass
        if len(tex) >= 4:
            break

    return OperationDraft(base_map=base_map, base_guid=guid, loc_dic_paths=loc_paths,
                          flash_paths=flash_paths, objectives=objectives, textures=tex[:4])


def existing_challenges(src_dir):
    glad = _open(src_dir, "ZZ_GladPatchableWin.dat")
    gnb = ndfbin.read(glad.get(next(v for v in glad.list() if v.lower().endswith("globals.cpp.gladndfbin"))))
    return current_challenge_list(gnb)

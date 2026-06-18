"""Clone a descriptor (unit / building) inside everything.cpp.

Extends the ammo-clone pattern (units_editor.py:_clone_ammo) to TUniteAuSolDescriptor,
TAvionDescriptor, TInfanterieDescriptor, TBatimentDescriptor — the four "buildable" classes.

What gets minted on the clone:
  - ClassNameForDebug (StringRef)   -> orig name + name_suffix, deduped via ensure_string
  - DescriptorId (Int32)            -> max(DescriptorId across all instances) + 1
  - NameInMenuToken (8-byte LocHash) -> md5(new_name)[:8] (only if the source has one)
  - Nationalite (Int32)             -> the `nationalite` arg, if given and the prop exists
  - UpgradeRequire / IsUpgrade      -> cleared (the clone is not an upgrade of the source's parent)

The deepcopy carries the rest forward — including the private WeaponDescriptor chain (only
TAmmunition is shared between units, and ammo is cloned separately by the editor's Ammo tab).

The new instance is appended to ndf.instances; if the source was in top_objects, the clone is too
(mirrors _clone_ammo's TOPO discipline at units_editor.py:310-311). Returns (new_idx, new_name,
new_token).
"""
import copy
import hashlib

from . import ndfbin


CLONEABLE_CLASSES = (
    "TUniteAuSolDescriptor",
    "TAvionDescriptor",
    "TInfanterieDescriptor",
    "TBatimentDescriptor",
)


def _prop_idx(ndf, class_index, name):
    p = ndf.prop_by_name_and_class(name, class_index)
    return p.index if p else None


def _max_descriptor_id(ndf) -> int:
    """Highest DescriptorId seen across all instances that carry one. 0 if none found."""
    best = 0
    for inst in ndf.instances:
        pidx = _prop_idx(ndf, inst.class_index, "DescriptorId")
        if pidx is None:
            continue
        v = inst.get(pidx)
        if v is not None and isinstance(v.raw, int) and v.raw > best:
            best = v.raw
    return best


def _mint_lochash(name: str) -> bytes:
    """Deterministic 8-byte LocHash for a cloned descriptor (truncated MD5 of the new name).
    Different keyspace from the mission-map keys in dic.py (those use the MAP_KEY_SUFFIX scheme)."""
    return hashlib.md5(name.encode("utf-8")).digest()[:8]


def clone_descriptor(ndf, src_idx: int, *, nationalite=None,
                     name_suffix: str = "_COPY") -> tuple:
    """Append a clone of `ndf.instances[src_idx]` and return (new_idx, new_class_name, new_token).

    Args:
        ndf:           the NdfBinary to mutate (typically project.everything()).
        src_idx:       index of the descriptor to clone.
        nationalite:   if not None and the source class has a Nationalite prop, set it on the clone.
        name_suffix:   appended to the source ClassNameForDebug to derive the clone's internal name.

    `new_token` is the new NameInMenuToken (bytes) or None if the source didn't have one.
    Caller is responsible for adding the matching display-name entries to baseunite.dic per language
    via dic.add_baseunite_entries (otherwise the clone shows as its internal name in the UI).

    Raises ValueError if the source class is not one of CLONEABLE_CLASSES.
    """
    if not 0 <= src_idx < len(ndf.instances):
        raise ValueError(f"src_idx {src_idx} out of range")
    src = ndf.instances[src_idx]
    src_cls = ndf.classes[src.class_index].name if 0 <= src.class_index < len(ndf.classes) else "?"
    if src_cls not in CLONEABLE_CLASSES:
        raise ValueError(f"refusing to clone {src_cls}: only {CLONEABLE_CLASSES} are supported")

    clone = copy.deepcopy(src)
    cidx = clone.class_index

    cn_pidx = _prop_idx(ndf, cidx, "ClassNameForDebug")
    if cn_pidx is None:
        raise ValueError(f"{src_cls} has no ClassNameForDebug property — can't mint a unique name")
    cn_val = clone.get(cn_pidx)
    if cn_val is None or not isinstance(cn_val.raw, int):
        raise ValueError("source instance has no readable ClassNameForDebug — refusing to clone")
    src_name = ndf.get_string(cn_val.raw)
    new_name = src_name + name_suffix
    while new_name in ndf.strings:    # ensure uniqueness even if the user clones twice
        new_name += name_suffix
    cn_val.raw = ndf.ensure_string(new_name)

    did_pidx = _prop_idx(ndf, cidx, "DescriptorId")
    if did_pidx is not None:
        did_val = clone.get(did_pidx)
        if did_val is not None and isinstance(did_val.raw, int):
            did_val.raw = _max_descriptor_id(ndf) + 1

    new_token = None
    nm_pidx = _prop_idx(ndf, cidx, "NameInMenuToken")
    if nm_pidx is not None:
        nm_val = clone.get(nm_pidx)
        if nm_val is not None and isinstance(nm_val.raw, (bytes, bytearray)) and len(nm_val.raw) == 8:
            new_token = _mint_lochash(new_name)
            nm_val.raw = new_token

    if nationalite is not None:
        nat_pidx = _prop_idx(ndf, cidx, "Nationalite")
        if nat_pidx is not None:
            nat_val = clone.get(nat_pidx)
            if nat_val is not None:
                nat_val.raw = int(nationalite)
            else:
                clone.set(nat_pidx, ndfbin.NdfValue(ndfbin.T.Int32, int(nationalite)))

    upr_pidx = _prop_idx(ndf, cidx, "UpgradeRequire")
    if upr_pidx is not None and clone.get(upr_pidx) is not None:
        clone.remove(upr_pidx)
    isup_pidx = _prop_idx(ndf, cidx, "IsUpgrade")
    if isup_pidx is not None and clone.get(isup_pidx) is not None:
        clone.get(isup_pidx).raw = False

    # Re-pick PositionInMenu so the clone doesn't collide with the source on the same slot in the
    # same nation+factory. Keep the source row (slot // 100) so the clone stays in the right menu
    # row — picking max-of-all + 1 would push it into a non-existent column of the last row.
    fpidx = _prop_idx(ndf, cidx, "Factory")
    fv = clone.get(fpidx) if fpidx is not None else None
    ppidx = _prop_idx(ndf, cidx, "PositionInMenu")
    pv = clone.get(ppidx) if ppidx is not None else None
    clone_nat = _nationalite_of(ndf, clone)
    if pv is not None and isinstance(pv.raw, int) and fv is not None and isinstance(fv.raw, int):
        free = _free_position_in_menu(ndf, clone_nat, fv.raw, pv.raw, exclude_idx=src_idx)
        if free is not None:
            pv.raw = free

    new_idx = len(ndf.instances)
    ndf.instances.append(clone)
    if src_idx in set(ndf.top_objects):
        ndf.top_objects.append(new_idx)
    return new_idx, new_name, new_token


def _nationalite_of(ndf, inst) -> "int | None":
    """Read Nationalite as int, or None if the prop is absent (= USA / shared)."""
    pidx = _prop_idx(ndf, inst.class_index, "Nationalite")
    if pidx is None:
        return None
    v = inst.get(pidx)
    return v.raw if v is not None and isinstance(v.raw, int) else None


def _set_or_remove_nationalite(ndf, inst, target_nat) -> bool:
    """Set Nationalite to `target_nat` (int) or remove the prop if `target_nat` is None.
    Returns True if a change was made. USA is encoded as "no prop", not Nationalite=0.
    Assumes the class defines Nationalite (true for all CLONEABLE_CLASSES)."""
    pidx = _prop_idx(ndf, inst.class_index, "Nationalite")
    cur = inst.get(pidx) if pidx is not None else None
    if target_nat is None:
        if cur is None:
            return False
        inst.remove(pidx)
        return True
    if cur is None:
        inst.set(pidx, ndfbin.NdfValue(ndfbin.T.Int32, int(target_nat)))
        return True
    if cur.raw == int(target_nat):
        return False
    cur.raw = int(target_nat)
    return True


def _free_position_in_menu(ndf, target_nat, factory_int, source_slot,
                           exclude_idx: int = -1) -> "int | None":
    """Find a free PositionInMenu slot for a unit landing in (target_nat, factory_int), keeping the
    row of `source_slot`. PositionInMenu is encoded as `row * 100 + column`, where each Factory has
    a small set of valid rows (e.g. F=10 tanks use row 3 and row 6=Nuclear-War). Picking max + 1
    across ALL rows pushes the unit to a non-existent column of the last row and it never renders.

    Returns None if Factory or source_slot isn't an int (descriptor isn't a buildable unit, e.g.
    some buildings). `exclude_idx` skips the descriptor being migrated itself."""
    if not isinstance(factory_int, int) or not isinstance(source_slot, int):
        return None
    target_row = source_slot // 100
    row_lo, row_hi = target_row * 100, target_row * 100 + 99
    best = row_lo - 1
    for i, inst in enumerate(ndf.instances):
        if i == exclude_idx:
            continue
        if _nationalite_of(ndf, inst) != target_nat:
            continue
        fpidx = _prop_idx(ndf, inst.class_index, "Factory")
        if fpidx is None:
            continue
        fv = inst.get(fpidx)
        if fv is None or fv.raw != factory_int:
            continue
        ppidx = _prop_idx(ndf, inst.class_index, "PositionInMenu")
        if ppidx is None:
            continue
        pv = inst.get(ppidx)
        if pv is not None and isinstance(pv.raw, int) and row_lo <= pv.raw <= row_hi and pv.raw > best:
            best = pv.raw
    return best + 1 if best + 1 <= row_hi else None


def migrate_descriptor(ndf, inst_idx: int, target_nat) -> dict:
    """Re-home a unit/building from its current nation to `target_nat` (an int 1..6+, or None for
    USA / shared). Modifies the descriptor IN PLACE (no clone). Returns a dict describing what
    changed — usable for a user-facing summary.

    The engine resolves "which units appear in nation X's vehicle factory" via the join
        unit.Nationalite == building.Nationalite AND unit.Factory == building.TypeBatiment
    (see memory/project_nation_building_wiring.md), so changing Nationalite IS the migration.

    Side effects:
      - PositionInMenu reassigned to max(existing in target nation+factory) + 1 to avoid the visual
        slot collision the source's slot would otherwise create.
      - UpgradeRequire CLEARED if the parent's Nationalite differs from target_nat (vanilla has no
        cross-nation upgrade chains; the engine's response to one is unverified).
    """
    if not 0 <= inst_idx < len(ndf.instances):
        raise ValueError(f"inst_idx {inst_idx} out of range")
    inst = ndf.instances[inst_idx]
    src_cls = ndf.classes[inst.class_index].name if 0 <= inst.class_index < len(ndf.classes) else "?"
    if src_cls not in CLONEABLE_CLASSES:
        raise ValueError(f"refusing to migrate {src_cls}: only {CLONEABLE_CLASSES} are supported")

    old_nat = _nationalite_of(ndf, inst)
    if (target_nat is None and old_nat is None) or (target_nat is not None and old_nat == int(target_nat)):
        return {"changed": False, "old_nat": old_nat, "new_nat": target_nat,
                "old_slot": None, "new_slot": None, "upgrade_cleared": False}

    _set_or_remove_nationalite(ndf, inst, target_nat)

    old_slot = None
    new_slot = None
    fpidx = _prop_idx(ndf, inst.class_index, "Factory")
    fv = inst.get(fpidx) if fpidx is not None else None
    ppidx = _prop_idx(ndf, inst.class_index, "PositionInMenu")
    if ppidx is not None and fv is not None and isinstance(fv.raw, int) and target_nat is not None:
        pv = inst.get(ppidx)
        if pv is not None and isinstance(pv.raw, int):
            old_slot = pv.raw
            free = _free_position_in_menu(ndf, int(target_nat), fv.raw, old_slot, exclude_idx=inst_idx)
            if free is not None and free != pv.raw:
                pv.raw = free
                new_slot = free

    upgrade_cleared = False
    upr_pidx = _prop_idx(ndf, inst.class_index, "UpgradeRequire")
    if upr_pidx is not None:
        upr = inst.get(upr_pidx)
        if upr is not None and upr.type_id == ndfbin.T.Reference:
            marker, ref = upr.raw
            if marker == ndfbin.OBJ_REF_MARKER and 0 <= ref[0] < len(ndf.instances):
                parent = ndf.instances[ref[0]]
                parent_nat = _nationalite_of(ndf, parent)
                if parent_nat != target_nat:
                    inst.remove(upr_pidx)
                    upgrade_cleared = True

    return {"changed": True, "old_nat": old_nat, "new_nat": target_nat,
            "old_slot": old_slot, "new_slot": new_slot,
            "upgrade_cleared": upgrade_cleared}


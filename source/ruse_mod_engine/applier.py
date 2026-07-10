"""
Mod applier — applies one or more .rmod patches to the RUSE game data.

Usage (Python API)
------------------
    from ruse_mod_engine.applier import apply_mod, apply_mods

    # Apply a single mod to game data
    apply_mod(
        mod_path="mods/my_mod.rmod",
        game_data_dir="C:/Steam/steamapps/common/R.U.S.E/Data",
    )

    # Apply multiple mods in order (later mods override earlier for the same property)
    apply_mods(
        mod_paths=["mods/balance.rmod", "mods/cheat.rmod"],
        game_data_dir="C:/Steam/steamapps/common/R.U.S.E/Data",
        backup=True,   # back up each .dat before modifying
    )

How it works
------------
For each .rmod file:
  1. For each patch group (dat + ndf target):
     a. Open the .dat archive.
     b. Extract the target .ndfbin bytes.
     c. Parse the NDF binary into an NdfBinary object.
     d. For each change entry:
        - "patch":  find matching instances, update their properties.
        - "delete": find matching instances, remove them.
        - "create": add a new instance with the given properties.
     e. Re-serialise the NdfBinary back to bytes.
     f. Replace the .ndfbin inside the .dat archive.
     g. Write the patched .dat back to disk.

Conflict handling
-----------------
When two mods patch the same property on the same instance, the last mod applied
wins.  The applier logs all changes so you can see what happened.

Error handling
--------------
If a match produces zero results the change is skipped with a warning.
If a match produces multiple results all of them are patched (useful for
applying the same change to every instance of a class).
"""

import logging
import os
import shutil
import zlib
from pathlib import Path
from typing import Any, List, Optional

from . import edata as edata_mod
from . import ndfbin as ndfbin_mod
from . import mod_format
from . import path_map as path_map_mod
from . import dic as dic_mod
from . import sdb as sdb_mod
from . import scenario as scenario_mod

log = logging.getLogger(__name__)


# ── Main entry points ─────────────────────────────────────────────────────────

def _within(root: Path, candidate: Path) -> bool:
    """True only if `candidate` resolves to inside `root`.

    SECURITY: .rmod files are shared between users, so every filesystem path they supply (the per-patch
    `dat`) is untrusted.  Joining an attacker path that contains ``..`` or is absolute / drive-qualified
    to the game root can escape it (``Path('C:/game/Data') / 'C:/Windows/x'`` becomes ``C:/Windows/x``),
    which would let a malicious mod read, back up, overwrite, or delete files outside the game folder.
    Resolving both sides and checking containment refuses all of those — the applier skips any group
    whose dat path fails this check, so an rmod can only ever touch files under the game/output root.
    """
    try:
        root_r = root.resolve()
        cand_r = candidate.resolve()
    except Exception:
        return False
    return cand_r == root_r or root_r in cand_r.parents


def resolve_dat_for_version(dat_rel: str, game_version: str) -> str:
    """Translate an rmod dat path to the real path (relative to the GAME ROOT) for the installed game
    version.

    rmod dat paths are GAME-ROOT-relative: Data/PC/<sub>/<dat> for the core dats, Maps/PC/<dat> for the
    shared per-terrain dats.  Back-compat: older rmods stored paths relative to Data/ (PC/<sub>/<dat>);
    those are normalized here by prepending "Data/", so existing community mods keep applying.

    OG/Compat keeps the original PC sub-folder (Data/PC/99/..., Data/PC/1360/...).  R.U.S.E. (the Steam
    re-release) flattens every Data/PC dat under Data/PC/190852/.  Maps/ dats are shared across versions.
    """
    p = dat_rel.replace("\\", "/")
    low = p.lower()
    if low.startswith("pc/"):                 # legacy Data-relative path -> game-root-relative
        p = "Data/" + p
        low = p.lower()
    if low.startswith("data/pc/") and game_version == "public":
        return "Data/PC/190852/" + p.split("/")[-1]
    return p


def apply_mod(
    mod_path: str,
    game_data_dir: str,
    *,
    backup: bool = True,
    dry_run: bool = False,
    output_dir: Optional[str] = None,
    game_version: str = "compat",
    progress=None,
    deploy_state: Optional[dict] = None,
    source_build: Optional[str] = None,
    target_build: Optional[str] = None,
) -> "ApplyResult":
    """Apply a single .rmod file to the game data directory.

    If output_dir is set the patched .dat files are written there
    (mirroring the Data/ sub-path) and the originals are never touched.

    .compat.rmod files applied in public mode receive runtime path/index
    translation.  Fully pre-translated .rmod files are applied verbatim.

    ``progress`` (optional): a callable ``progress(dat_rel)`` invoked just before each .dat is opened —
    the slow step for large data files.  Lets the caller show live "preparing <dat>…" feedback.
    """
    mod = mod_format.load(mod_path)
    is_compat_mod = Path(mod_path).name.lower().endswith(".compat.rmod")
    # Cross-build deploy: if the mod was made for a DIFFERENT build than we're deploying onto, pre-translate
    # its positional addresses via the direct version map so the applier can run it verbatim.  from_build
    # defaults to the mod's own game_version stamp.  No-op (map_report.map False) when same build / no map.
    src = source_build or getattr(mod, "game_version", None)
    map_report = {"map": False, "remapped": 0, "stale": []}
    if target_build and src and str(src) != str(target_build):
        from . import version_map as _vm
        mod, map_report = _vm.remap_mod(mod, src, target_build)
    result = _apply(mod, game_data_dir, backup=backup, dry_run=dry_run,
                    output_dir=output_dir, game_version=game_version,
                    is_compat_mod=is_compat_mod, progress=progress, deploy_state=deploy_state)
    result.map_report = map_report
    return result


def apply_mods(
    mod_paths: List[str],
    game_data_dir: str,
    *,
    backup: bool = True,
    dry_run: bool = False,
    output_dir: Optional[str] = None,
    game_version: str = "compat",
) -> List["ApplyResult"]:
    """Apply multiple .rmod files in order.

    Each mod's changes are layered on top of the previous mod's output so that
    mods lower in the list override earlier ones for the same properties.
    """
    if output_dir and not dry_run:
        clear_output_dats(mod_paths, output_dir, game_version=game_version)
    results = []
    deploy_state: dict = {}   # shared touched-set across the stack → cross-mod conflict detection
    for path in mod_paths:
        result = apply_mod(path, game_data_dir, backup=backup, dry_run=dry_run,
                           output_dir=output_dir, game_version=game_version,
                           deploy_state=deploy_state)
        results.append(result)
    return results


def clear_output_dats(mod_paths: List[str], output_dir: str,
                      game_version: str = "compat") -> None:
    """Delete any output .dat files touched by the given mods so a fresh apply run
    always starts from the original game data rather than stale prior output."""
    out_root = Path(output_dir)
    to_delete: set = set()
    for path in mod_paths:
        try:
            mod = mod_format.load(path)
            for pg in mod.patches:
                to_delete.add(resolve_dat_for_version(pg.dat, game_version).replace("/", os.sep))
            for fg in mod.file_patches:
                to_delete.add(resolve_dat_for_version(fg.dat, game_version).replace("/", os.sep))
            for lg in mod.loc_patches:
                to_delete.add(resolve_dat_for_version(lg.dat, game_version).replace("/", os.sep))
            for sg in mod.sdb_patches:
                to_delete.add(resolve_dat_for_version(sg.dat, game_version).replace("/", os.sep))
            for sg in mod.scenario_patches:
                to_delete.add(resolve_dat_for_version(sg.dat, game_version).replace("/", os.sep))
        except Exception:
            # A mod we can't load here won't get its stale output dats cleared → a fresh apply could
            # build on stale output. Record which mod rather than silently skipping it.
            logging.getLogger(__name__).warning(
                "Output-dat cleanup skipped for unreadable mod: %s", path, exc_info=True)
    for dat_rel in to_delete:
        p = out_root / dat_rel
        if not _within(out_root, p):
            log.warning("clear_output_dats: refused unsafe path from mod: %r", dat_rel)
            continue
        if p.exists():
            p.unlink()
            log.info("Cleared stale output: %s", p.name)


# ── Result object ─────────────────────────────────────────────────────────────

class ChangeRecord:
    """Before/after record for one property change."""
    __slots__ = ("table", "instance_id", "prop", "old_val", "new_val")

    def __init__(self, table: str, instance_id: str, prop: str,
                 old_val: str, new_val: str):
        self.table = table
        self.instance_id = instance_id
        self.prop = prop
        self.old_val = old_val
        self.new_val = new_val

    def __str__(self):
        return f"  {self.table}[{self.instance_id}].{self.prop}: {self.old_val!r} -> {self.new_val!r}"


class RepairFinding:
    """A change whose target could not be resolved on THIS game build — the mod no longer fits and
    needs repair/re-migration.  Raised (not silently skipped) so the manager can tell the user WHICH
    changes broke and WHY: a key that was renamed between builds, an instance that was removed, or a
    positional _index that drifted off its class.  Feeds the harden-further / rmod_validate loop."""
    def __init__(self, dat, ndf, table, match, reason):
        self.dat = dat
        self.ndf = ndf
        self.table = table
        self.match = match
        self.reason = reason

    def __str__(self):
        return f"  {self.dat}::{self.ndf} {self.table} match={self.match}: {self.reason}"


class ConflictFinding:
    """This mod overwrote an edit an EARLIER mod in the same deploy already made to the same
    instance+property.  Not an error — last-writer-wins is intended — but the user should know the
    earlier mod's change was superseded (e.g. stacking two balance mods that both set the same HP)."""
    def __init__(self, dat, ndf, instance_id, prop, overwrote_mod):
        self.dat = dat
        self.ndf = ndf
        self.instance_id = instance_id
        self.prop = prop
        self.overwrote_mod = overwrote_mod

    def __str__(self):
        return f"{self.instance_id}.{self.prop} — overwrites '{self.overwrote_mod}'"


class ApplyResult:
    def __init__(self, mod_name: str):
        self.mod_name = mod_name
        self.changes_applied: int = 0
        self.warnings: List[str] = []
        self.errors: List[str] = []
        self.change_log: List[ChangeRecord] = []
        self.requires_repair: List[RepairFinding] = []
        self.conflicts: List[ConflictFinding] = []
        self.map_report: dict = {"map": False, "remapped": 0, "stale": []}  # cross-build remap outcome
        self._ctx_dat = None   # set per patch group so findings carry dat/ndf context
        self._ctx_ndf = None

    def warn(self, msg: str):
        self.warnings.append(msg)
        log.warning("[%s] %s", self.mod_name, msg)

    def error(self, msg: str):
        self.errors.append(msg)
        log.error("[%s] %s", self.mod_name, msg)

    def set_ndf_context(self, dat, ndf):
        self._ctx_dat, self._ctx_ndf = dat, ndf

    def flag_repair(self, table, match, reason):
        """Record an unresolved-target finding as its own category (NOT a generic warning), so the manager
        can surface 'this mod needs repair for build X' distinctly.  Still logged for dev visibility."""
        f = RepairFinding(self._ctx_dat, self._ctx_ndf, table, match, reason)
        self.requires_repair.append(f)
        log.warning("[%s] REQUIRES REPAIR: %s", self.mod_name, f)

    def flag_conflict(self, instance_id, prop, overwrote_mod):
        """Record that this mod overwrote an earlier mod's edit to instance_id.prop (soft; last wins)."""
        f = ConflictFinding(self._ctx_dat, self._ctx_ndf, instance_id, prop, overwrote_mod)
        self.conflicts.append(f)
        log.info("[%s] conflict: %s", self.mod_name, f)

    def needs_repair(self) -> bool:
        return len(self.requires_repair) > 0

    def ok(self):
        return len(self.errors) == 0

    def summary(self) -> str:
        status = "OK" if self.ok() else "ERRORS"
        rep = f", {len(self.requires_repair)} need-repair" if self.requires_repair else ""
        con = f", {len(self.conflicts)} conflict(s)" if self.conflicts else ""
        return (f"[{status}] {self.mod_name}: {self.changes_applied} change(s) applied, "
                f"{len(self.warnings)} warning(s), {len(self.errors)} error(s){rep}{con}")

    def __repr__(self):
        return self.summary()


# ── Core application logic ────────────────────────────────────────────────────

def _apply(
    mod: mod_format.RuseMod,
    game_data_dir: str,
    backup: bool,
    dry_run: bool,
    output_dir: Optional[str] = None,
    game_version: str = "compat",
    is_compat_mod: bool = False,
    progress=None,
    deploy_state: Optional[dict] = None,
) -> ApplyResult:
    result = ApplyResult(mod.name)
    # touched-set shared across a deploy stack: (dat, ndf, inst_identity, prop) -> mod that wrote it.
    # None caller (single mod) → a local dict, so intra-mod tracking still works but there is nothing to
    # conflict with.
    if deploy_state is None:
        deploy_state = {}
    data_root = Path(game_data_dir)
    out_root = Path(output_dir) if output_dir else None
    # Tracks which .dat files have already been copied to output this run.
    # Only the first patch_group for a given .dat copies from source; subsequent
    # groups for the same .dat work on the already-patched copy so their changes
    # are not overwritten.
    copied_dats: set = set()

    for patch_group in mod.patches:
        dat_rel = resolve_dat_for_version(patch_group.dat, game_version).replace("/", os.sep)
        src_dat_path = data_root / dat_rel
        if not _within(data_root, src_dat_path):
            result.error(f"refused unsafe dat path in mod (escapes game folder): {dat_rel!r}")
            continue

        if not src_dat_path.exists():
            result.error(f"DAT file not found: {src_dat_path}")
            continue

        # Determine which path to actually open/write
        if out_root is not None:
            work_dat_path = out_root / dat_rel
            if not _within(out_root, work_dat_path):
                result.error(f"refused unsafe dat path in mod (escapes output folder): {dat_rel!r}")
                continue
            work_dat_path.parent.mkdir(parents=True, exist_ok=True)
            if dat_rel not in copied_dats:
                if not work_dat_path.exists():
                    # File not yet in output — copy fresh from source.
                    # If it already exists, a previous mod in the chain already wrote it; use that.
                    log.info("Copying %s -> %s", src_dat_path.name, work_dat_path)
                    shutil.copy2(str(src_dat_path), str(work_dat_path))
                copied_dats.add(dat_rel)
        else:
            work_dat_path = src_dat_path
            if not dry_run and backup:
                _make_backup(work_dat_path)

        log.info("Opening %s", work_dat_path)

        if progress:
            progress(dat_rel)          # about to open this .dat — the slow step for large files
        try:
            edat = edata_mod.open_dat(str(work_dat_path))
        except Exception as e:
            result.error(f"Failed to open {work_dat_path}: {e}")
            continue

        ndf_path_in_dat = patch_group.ndf
        # Only .compat.rmod files need runtime NDF-path translation; .rmod files are
        # already fully pre-translated and must be applied verbatim.
        if game_version == "public" and is_compat_mod:
            ndf_path_in_dat = path_map_mod.translate_ndf_path(ndf_path_in_dat)

        # For R.U.S.E., prepare index/ObjRef translation for chain-NDF patches.
        # Applies to chqlmapinfo.cpp (instance index shifts, TMapLoadInfo/TUIResourceTexture)
        # and globals.cpp (newly-created TMultiMapInfo ObjRefs in MultiList/ChallengeList).
        # patch_index_map is applied independently per-change without activating full ObjRef
        # translation (which is only safe for chqlmapinfo/globals where creates are managed).
        # compat->public uses the legacy hardcoded "public" map; modern build->build
        # migration (issue #14) stores a per-target-build map under the build id, which
        # the generic per-change translation branch below consumes the same way.
        patch_index_map: Optional[dict] = (
            patch_group.index_map.get("public") if game_version == "public" and is_compat_mod
            else patch_group.index_map.get(game_version)
        )
        translate_changes = (
            game_version == "public" and is_compat_mod
            and (path_map_mod.is_chqlmapinfo_ndf(ndf_path_in_dat)
                 or "globals.cpp.gladndfbin" in ndf_path_in_dat.lower()
                 or "mapinfo.cpp.gladndfbin" in ndf_path_in_dat.lower())
        )
        # everything.cpp: 17 instances were removed in the public remaster at indices
        # >= 48888, so any compat _index at or above that threshold must shift by -17.
        translate_everything_cpp = (
            game_version == "public" and is_compat_mod
            and path_map_mod.is_everything_cpp_ndf(ndf_path_in_dat)
        )
        objref_to_local_id: dict = (
            _build_objref_local_id_map(patch_group) if translate_changes else {}
        )
        ndf_bytes = edat.get(ndf_path_in_dat)
        if ndf_bytes is None:
            ndf_bytes = edat.get(ndf_path_in_dat.replace("/", "\\"))

        is_new_file = (ndf_bytes is None)
        if is_new_file:
            if not patch_group.create_if_missing:
                result.error(
                    f"NDF file {ndf_path_in_dat!r} not found in {work_dat_path.name}. "
                    f"Available: {edat.list()[:10]}"
                )
                continue
            log.info("create_if_missing: starting with blank NDF for %s", ndf_path_in_dat)
            ndf = ndfbin_mod.NdfBinary()
        else:
            log.info("Parsing NDF: %s", ndf_path_in_dat)
            try:
                ndf = ndfbin_mod.read(ndf_bytes)
            except Exception as e:
                result.error(f"Failed to parse NDF {ndf_path_in_dat!r}: {e}")
                continue

        # Enable ObjRef class coercion ONLY for the everything.cpp descriptor NDF, where clean data proves
        # declared==actual universally and a stale class (compat index or a mis-captured legacy-mod class)
        # makes the engine build the wrong type over the target's memory -> deterministic main-menu crash.
        # All other NDFs keep the source class (see applier._objref_value).
        ndf._coerce_objref_class = path_map_mod.is_everything_cpp_ndf(ndf_path_in_dat)

        # local_id → (inst_idx, class_idx): populated by create actions for $ref resolution
        create_map: dict = {}

        # Capture pre-create count so list translation can drop OG-only positions
        # (positions ≥ upd_pre_count that aren't in objref_to_local_id are OG-specific)
        upd_pre_count: int = len(ndf.instances) if translate_changes else 0

        changed = False
        # Collects (inst, table_name, {prop_name: val_def}) for stable_ref ObjRef props
        # that could not be resolved during the create pass because their target instances
        # had not been created yet.  Resolved in a second pass after all creates are done.
        deferred_objref: list = []

        def _translate_change(change):
            if translate_changes:
                return _translate_change_for_public(
                    change, objref_to_local_id, upd_pre_count, patch_index_map
                )
            elif translate_everything_cpp:
                if "_index" not in change.match:
                    return change
                og_idx = int(change.match["_index"])
                new_idx = path_map_mod.translate_everything_cpp_index(og_idx)
                if new_idx is None:
                    return None  # instance removed from public (e.g. Uplay-specific)
                if new_idx == og_idx:
                    return change
                new_match = dict(change.match)
                new_match["_index"] = str(new_idx)
                return mod_format.ModChange(
                    action=change.action, table=change.table, match=new_match,
                    set_props=change.set_props, del_props=change.del_props,
                    local_id=change.local_id,
                )
            elif patch_index_map:
                new_match = dict(change.match)
                if "_index" in new_match:
                    og_idx = int(new_match["_index"])
                    upd_idx = patch_index_map.get(str(og_idx))
                    if upd_idx is not None and upd_idx != og_idx:
                        new_match["_index"] = str(upd_idx)

                segments: Optional[list] = patch_index_map.get("_instance_segments")
                instance_offset = int(patch_index_map.get("_instance_offset", 0))
                raw_class_map = patch_index_map.get("_class_map", {})
                class_map: Optional[dict] = (
                    {int(k): int(v) for k, v in raw_class_map.items()}
                    if raw_class_map else None
                )
                new_set_props = change.set_props
                if segments or instance_offset != 0 or class_map:
                    new_set_props = {}
                    for prop_name, val_def in change.set_props.items():
                        translated = _translate_objref_by_offset(
                            val_def.value, instance_offset, class_map, segments
                        )
                        if translated is val_def.value:
                            new_set_props[prop_name] = val_def
                        else:
                            new_set_props[prop_name] = mod_format.ModValueDef(
                                type_name=val_def.type_name, value=translated
                            )

                return mod_format.ModChange(
                    action=change.action,
                    table=change.table,
                    match=new_match,
                    set_props=new_set_props,
                    del_props=change.del_props,
                    local_id=change.local_id,
                )
            return change

        # Surgical IMPR/EXPR merge, phase A — ADD the patch group's new import/export
        # paths BEFORE applying instance changes, so TransRef values (stored as portable
        # paths) resolve to a real ordinal. Removals + the dense renumber happen in phase
        # B (after patches) so re-pointed instances are seen before an import is dropped.
        if _apply_ref_adds(ndf, patch_group, result):
            changed = True

        # Pass 1: creates only — populate create_map so local_id refs resolve in patches.
        # Sort creates by inst number encoded in local_id (e.g. "inst_63840") so new
        # instances are appended in the same global-index order as the original mod.
        def _inst_sort_key(c: mod_format.ModChange) -> int:
            lid = c.local_id or ""
            if lid.startswith("inst_") and lid[5:].isdigit():
                return int(lid[5:])
            return 10 ** 9

        result.set_ndf_context(patch_group.dat, patch_group.ndf)
        non_creates = []
        raw_creates = []
        for change in patch_group.changes:
            translated = _translate_change(change)
            if translated is None:
                continue  # instance removed from public — skip
            change = translated
            if change.action == "create":
                raw_creates.append(change)
            else:
                non_creates.append(change)
        raw_creates.sort(key=_inst_sort_key)

        # Snapshot every non-create match address to a concrete index against the PRISTINE NDF — BEFORE
        # the create pass runs.  A mod's own create can introduce a duplicate stable key (e.g. Balanced
        # Realism adds a 2nd Unit_Sniper), and a duplicate key destroys the GLOBAL-key uniqueness an anchor
        # root (or a key match) relies on — so an anchor resolved AFTER creates returns None and the target
        # is stranded.  That silently dropped ~1000 everything.cpp edits: it failed the converter's lossless
        # self-check (whole-file raw fallback) AND mis-applied in-game (main-menu crash).  Creates only
        # APPEND (existing instance indices never shift), so an index snapshotted here stays valid through
        # the create pass.  A match that instead targets a to-be-created instance simply gets 0 hits now and
        # is resolved later in the apply loop (the create-then-patch-by-new-key case still works).
        _preresolve_addresses(non_creates, ndf)

        # Pass 1: creates only — populate create_map so local_id refs resolve in patches.
        for change in raw_creates:
            # Use deferred variant so stable_ref ObjRefs that reference instances
            # not yet created are retried after all creates are registered.
            n, deferred = _action_create_deferred(change, ndf, result, create_map)
            if n > 0:
                changed = True
                result.changes_applied += n
                if deferred:
                    inst_idx = create_map.get(change.local_id, (len(ndf.instances) - 1, 0))[0]
                    deferred_objref.append((ndf.instances[inst_idx], change.table, deferred))

        # Pass 2: patches, deletes, etc. (create_map is now fully populated).
        # Then apply whole-instance DELETES last, in DESCENDING index order, regardless of the order they
        # appear in the rmod.  A delete does `del instances[idx]`, shifting every higher index down by one;
        # running deletes after all patches (and highest-first) means a delete can never invalidate a
        # patch/delete_props address that was just snapshotted to a now-stale _index.  Converter output is
        # already ordered this way, so this is a no-op for our own rmods and a safety net for hand-authored
        # or hostile ones that interleave a delete before a later patch.  (stable sort keeps non-delete order.)
        def _apply_order_key(ch):
            if ch.action == "delete":
                try:
                    idx = int((ch.match or {}).get("_index", -1))
                except (TypeError, ValueError):
                    idx = -1
                return (1, -idx)
            return (0, 0)
        non_creates.sort(key=_apply_order_key)
        for change in non_creates:
            n = _apply_change(change, ndf, result, create_map, deploy_state)
            if n > 0:
                changed = True
                result.changes_applied += n

        # Second pass: resolve deferred stable_ref ObjRef props now that all
        # creates have been registered and are findable by find_instance_by_key.
        for inst, table_name, deferred in deferred_objref:
            for prop_name, val_def in deferred.items():
                try:
                    ndf_val = _make_ndf_value(val_def, prop_name, ndf, create_map)
                except Exception as e:
                    result.warn(
                        f"deferred stable_ref: cannot set '{prop_name}' "
                        f"on '{table_name}': {e}"
                    )
                    continue
                if ndf.set_property(inst, prop_name, ndf_val, create=True):
                    result.changes_applied += 1
                    changed = True
                else:
                    result.warn(
                        f"deferred stable_ref: property '{prop_name}' "
                        f"not found in '{table_name}'"
                    )

        # Surgical IMPR/EXPR merge, phase B — now that instance patches have re-pointed
        # any TransRef away from a to-be-removed import, apply import_remove and reassign
        # dense ordinals (remapping every instance TransRef). Runs last so removals never
        # dangle a still-referenced import.
        if _finalize_ref_edits(ndf, patch_group, result):
            changed = True

        if changed and not dry_run:
            log.info("Re-serialising NDF and repacking DAT…")
            try:
                compress = (not is_new_file) and ndf.is_compressed
                new_ndf_bytes = ndfbin_mod.write(ndf, compress=compress)
                if is_new_file:
                    edat.add(ndf_path_in_dat, new_ndf_bytes)
                else:
                    edat.replace(ndf_path_in_dat, new_ndf_bytes)
                log.info("Wrote %s", work_dat_path.name)
            except Exception as e:
                result.error(f"Failed to write patched NDF: {e}")

    # ── Raw file replacements ─────────────────────────────────────────────────
    for file_group in mod.file_patches:
        dat_rel = resolve_dat_for_version(file_group.dat, game_version).replace("/", os.sep)
        src_dat_path = data_root / dat_rel
        if not _within(data_root, src_dat_path):
            result.error(f"refused unsafe dat path in mod (escapes game folder): {dat_rel!r}")
            continue

        if not src_dat_path.exists():
            result.error(f"DAT file not found (file_patches): {src_dat_path}")
            continue

        if out_root is not None:
            work_dat_path = out_root / dat_rel
            if not _within(out_root, work_dat_path):
                result.error(f"refused unsafe dat path in mod (escapes output folder): {dat_rel!r}")
                continue
            work_dat_path.parent.mkdir(parents=True, exist_ok=True)
            if dat_rel not in copied_dats:
                if not work_dat_path.exists():
                    log.info("Copying %s -> %s", src_dat_path.name, work_dat_path)
                    shutil.copy2(str(src_dat_path), str(work_dat_path))
                copied_dats.add(dat_rel)
        else:
            work_dat_path = src_dat_path
            if not dry_run and backup:
                _make_backup(work_dat_path)

        if progress:
            progress(dat_rel)          # about to open this .dat — the slow step for large files
        try:
            edat = edata_mod.open_dat(str(work_dat_path))
        except Exception as e:
            result.error(f"Failed to open {work_dat_path} for file_patches: {e}")
            continue

        translate_dat_paths = (
            game_version == "public" and is_compat_mod
            and "datamap" in file_group.dat.lower()
        )

        if dry_run:
            result.changes_applied += len(file_group.files)
            continue

        # Collect all replacements and additions for this dat into one batch
        to_replace: dict = {}   # {canonical_path: bytes}
        to_add: dict = {}       # {canonical_path: bytes}

        for fp in file_group.files:
            fp_path = fp.path
            if translate_dat_paths:
                fp_path = path_map_mod.translate_datamap_path(fp_path)
            path_bs = fp_path.replace("/", "\\")

            # Resolve canonical path in the existing dat
            actual_path: Optional[str] = None
            if edat.get(path_bs) is not None:
                actual_path = path_bs
            elif edat.get(fp_path) is not None:
                actual_path = fp_path
            elif game_version == "public":
                actual_path = _find_dat_entry_by_suffix(edat, path_bs)

            if actual_path is not None:
                to_replace[actual_path] = fp.data
            else:
                # File doesn't exist in dat — will be added
                to_add[path_bs] = fp.data

        if not to_replace and not to_add:
            continue

        try:
            if edat._header.version == 1:
                edat.batch_update(to_replace, to_add)
            else:
                # v2: batch replace only (add not supported); fall back to serial replace
                for actual_path, new_data in to_replace.items():
                    edat.replace(actual_path, new_data)
                if to_add:
                    result.warn(f"file_patch: {len(to_add)} file(s) could not be added to v2 dat {work_dat_path.name} — skipped")

            log.info("file_patch: applied %d replace(s) and %d add(s) to %s",
                     len(to_replace), len(to_add), work_dat_path.name)
            result.changes_applied += len(to_replace) + len(to_add)
        except Exception as e:
            result.error(f"file_patch: batch update failed for {work_dat_path.name}: {e}")

    # ── localization (.dic) patches: surgical per-entry edits to TRA dictionaries ──
    # Group by dat so every .dic in one archive is written in a single rebuild.
    loc_by_dat: dict = {}
    for lg in mod.loc_patches:
        dr = resolve_dat_for_version(lg.dat, game_version).replace("/", os.sep)
        loc_by_dat.setdefault(dr, []).append(lg)

    for dat_rel, groups in loc_by_dat.items():
        src_dat_path = data_root / dat_rel
        if not _within(data_root, src_dat_path):
            result.error(f"refused unsafe dat path in mod (escapes game folder): {dat_rel!r}")
            continue
        if not src_dat_path.exists():
            result.error(f"DAT file not found (loc_patches): {src_dat_path}")
            continue
        if out_root is not None:
            work_dat_path = out_root / dat_rel
            if not _within(out_root, work_dat_path):
                result.error(f"refused unsafe dat path in mod (escapes output folder): {dat_rel!r}")
                continue
            work_dat_path.parent.mkdir(parents=True, exist_ok=True)
            if dat_rel not in copied_dats:
                if not work_dat_path.exists():
                    shutil.copy2(str(src_dat_path), str(work_dat_path))
                copied_dats.add(dat_rel)
        else:
            work_dat_path = src_dat_path
            if not dry_run and backup:
                _make_backup(work_dat_path)
        if progress:
            progress(dat_rel)          # about to open this .dat — the slow step for large files
        try:
            edat = edata_mod.open_dat(str(work_dat_path))
        except Exception as e:
            result.error(f"Failed to open {work_dat_path} for loc_patches: {e}")
            continue
        if dry_run:
            result.changes_applied += sum(len(g.entries) for g in groups)
            continue

        to_replace: dict = {}   # {canonical .dic path: new TRA bytes}
        for lg in groups:
            path_bs = lg.dic.replace("/", "\\")
            actual_path = None
            if edat.get(path_bs) is not None:
                actual_path = path_bs
            elif edat.get(lg.dic) is not None:
                actual_path = lg.dic
            else:
                actual_path = _find_dat_entry_by_suffix(edat, path_bs)
            if actual_path is None:
                result.warn(f"loc_patch: .dic not found in {work_dat_path.name}: {lg.dic}")
                continue
            blob = to_replace.get(actual_path)        # accumulate if another group hit the same dic
            if blob is None:
                blob = edat.get(actual_path)
            applied = 0
            for e in lg.entries:
                try:
                    key = bytes.fromhex(e.key)
                except ValueError:
                    result.warn(f"loc_patch: bad key {e.key!r}")
                    continue
                try:
                    if dic_mod.get_entry(blob, key) is None:
                        blob = dic_mod.add_entry(blob, key, e.value)   # new key
                    else:
                        blob = dic_mod.set_entry(blob, key, e.value)   # change existing
                    applied += 1
                except Exception as ex:
                    result.warn(f"loc_patch: {e.key} in {lg.dic.split('/')[-1]}: {ex}")
            to_replace[actual_path] = blob
            result.changes_applied += applied

        if not to_replace:
            continue
        try:
            if edat._header.version == 1:
                edat.batch_update(to_replace, {})
            else:
                for ap, data in to_replace.items():
                    edat.replace(ap, data)
            log.info("loc_patch: applied %d .dic edit(s) to %s", len(to_replace), work_dat_path.name)
        except Exception as e:
            result.error(f"loc_patch: batch update failed for {work_dat_path.name}: {e}")

    # ── SDB layer patches: replace whole AI-terrain layers inside mapinfo.win buffer4 ──
    sdb_by_dat: dict = {}
    for sg in mod.sdb_patches:
        dr = resolve_dat_for_version(sg.dat, game_version).replace("/", os.sep)
        sdb_by_dat.setdefault(dr, []).append(sg)

    for dat_rel, groups in sdb_by_dat.items():
        src_dat_path = data_root / dat_rel
        if not _within(data_root, src_dat_path):
            result.error(f"refused unsafe dat path in mod (escapes game folder): {dat_rel!r}")
            continue
        if not src_dat_path.exists():
            result.error(f"DAT file not found (sdb_patches): {src_dat_path}")
            continue
        if out_root is not None:
            work_dat_path = out_root / dat_rel
            if not _within(out_root, work_dat_path):
                result.error(f"refused unsafe dat path in mod (escapes output folder): {dat_rel!r}")
                continue
            work_dat_path.parent.mkdir(parents=True, exist_ok=True)
            if dat_rel not in copied_dats:
                if not work_dat_path.exists():
                    shutil.copy2(str(src_dat_path), str(work_dat_path))
                copied_dats.add(dat_rel)
        else:
            work_dat_path = src_dat_path
            if not dry_run and backup:
                _make_backup(work_dat_path)
        if progress:
            progress(dat_rel)          # about to open this .dat — the slow step for large files
        try:
            edat = edata_mod.open_dat(str(work_dat_path))
        except Exception as e:
            result.error(f"Failed to open {work_dat_path} for sdb_patches: {e}")
            continue
        if dry_run:
            result.changes_applied += sum(len(g.layers) for g in groups)
            continue

        to_replace: dict = {}   # {canonical mapinfo.win path: new win bytes}
        for sg in groups:
            path_bs = sg.win.replace("/", "\\")
            actual_path = None
            if edat.get(path_bs) is not None:
                actual_path = path_bs
            elif edat.get(sg.win) is not None:
                actual_path = sg.win
            else:
                actual_path = _find_dat_entry_by_suffix(edat, path_bs)
            if actual_path is None:
                result.warn(f"sdb_patch: mapinfo.win not found in {work_dat_path.name}: {sg.win}")
                continue
            win = to_replace.get(actual_path)
            if win is None:
                win = edat.get(actual_path)
            parts = sdb_mod.split_mapinfo(win)
            if not parts or not sdb_mod.is_sdb(parts[1][3]):
                result.warn(f"sdb_patch: {sg.win} has no editable SDB buffer — skipped")
                continue
            buf = parts[1][3]
            specs = [(ly.bit, zlib.decompress(ly.mask), sg.grid_size) for ly in sg.layers]
            try:
                # FAST path: parse the tree, set each layer to its target by editing IN PLACE
                # (numpy diff + paint only the changed cells), then serialize. ~9x faster than the
                # decode→apply→full-rebuild path, and byte-exact on the resulting layer state.
                parsed = sdb_mod.parse(buf)
                sdb_mod.apply_layer_masks_inplace(parsed, specs)
                new_sdb = sdb_mod.serialize(parsed)
            except Exception as e:                       # numpy missing / unexpected tree → legacy path
                log.info("sdb_patch: in-place apply unavailable (%s) — using legacy rebuild", e)
                g = sdb_mod.decode_grid(buf)
                if g["R"] != sg.grid_size:
                    result.warn(f"sdb_patch: grid size {g['R']} != patch {sg.grid_size} for {sg.win} — skipped")
                    continue
                grid = g["grid"]
                for bit, raw_mask, _ in specs:
                    sdb_mod.apply_layer_mask(grid, g["R"], bit, raw_mask)
                new_sdb = sdb_mod.encode_from_grid(g["orig"], grid, g["R"])
            to_replace[actual_path] = sdb_mod.replace_buffer4(win, new_sdb)
            result.changes_applied += len(sg.layers)

        if not to_replace:
            continue
        try:
            if edat._header.version == 1:
                edat.batch_update(to_replace, {})
            else:
                for ap, data in to_replace.items():
                    edat.replace(ap, data)
            log.info("sdb_patch: applied SDB layers to %s", work_dat_path.name)
        except Exception as e:
            result.error(f"sdb_patch: batch update failed for {work_dat_path.name}: {e}")

    # ── Scenario patches: apply NDF changes to the placement NDF embedded inside a .scenario ──
    scn_by_dat: dict = {}
    for sg in mod.scenario_patches:
        dr = resolve_dat_for_version(sg.dat, game_version).replace("/", os.sep)
        scn_by_dat.setdefault(dr, []).append(sg)

    for dat_rel, groups in scn_by_dat.items():
        src_dat_path = data_root / dat_rel
        if not _within(data_root, src_dat_path):
            result.error(f"refused unsafe dat path in mod (escapes game folder): {dat_rel!r}")
            continue
        if not src_dat_path.exists():
            result.error(f"DAT file not found (scenario_patches): {src_dat_path}")
            continue
        if out_root is not None:
            work_dat_path = out_root / dat_rel
            if not _within(out_root, work_dat_path):
                result.error(f"refused unsafe dat path in mod (escapes output folder): {dat_rel!r}")
                continue
            work_dat_path.parent.mkdir(parents=True, exist_ok=True)
            if dat_rel not in copied_dats:
                if not work_dat_path.exists():
                    shutil.copy2(str(src_dat_path), str(work_dat_path))
                copied_dats.add(dat_rel)
        else:
            work_dat_path = src_dat_path
            if not dry_run and backup:
                _make_backup(work_dat_path)
        if progress:
            progress(dat_rel)          # about to open this .dat — the slow step for large files
        try:
            edat = edata_mod.open_dat(str(work_dat_path))
        except Exception as e:
            result.error(f"Failed to open {work_dat_path} for scenario_patches: {e}")
            continue
        if dry_run:
            result.changes_applied += sum(len(g.changes) for g in groups)
            continue

        to_replace: dict = {}   # {canonical .scenario path: new scenario bytes}
        for sg in groups:
            path_bs = sg.scenario.replace("/", "\\")
            actual_path = None
            if edat.get(path_bs) is not None:
                actual_path = path_bs
            elif edat.get(sg.scenario) is not None:
                actual_path = sg.scenario
            else:
                actual_path = _find_dat_entry_by_suffix(edat, path_bs)
            if actual_path is None:
                result.warn(f"scenario_patch: .scenario not found in {work_dat_path.name}: {sg.scenario}")
                continue
            raw = to_replace.get(actual_path)
            if raw is None:
                raw = edat.get(actual_path)
            try:
                scn = scenario_mod.read(raw)
                ndf = ndfbin_mod.read(scn["ndf_data"])
            except Exception as e:
                result.error(f"scenario_patch: failed to parse {sg.scenario}: {e}")
                continue
            if _apply_changes_to_ndf(sg.changes, ndf, result):
                new_ndf = ndfbin_mod.write(ndf, compress=ndf.is_compressed)
                new_ndf += b"\x00" * (-len(new_ndf) % 4)        # scenario layout pads ndf_data to 4 bytes
                scn["ndf_data"] = new_ndf
                to_replace[actual_path] = scenario_mod.write(scn)

        if not to_replace:
            continue
        try:
            if edat._header.version == 1:
                edat.batch_update(to_replace, {})
            else:
                for ap, data in to_replace.items():
                    edat.replace(ap, data)
            log.info("scenario_patch: applied to %s", work_dat_path.name)
        except Exception as e:
            result.error(f"scenario_patch: batch update failed for {work_dat_path.name}: {e}")

    return result


def _find_dat_entry_by_suffix(edat, path_bs: str) -> Optional[str]:
    """Find a dat entry whose path ends with path_bs's trailing components.

    Tries increasingly longer suffixes (from 2 components up) until exactly
    one match is found.  Returns None if no unique match is found.
    Used as a fallback when compat-game path prefixes differ from Updated.
    """
    parts = [p for p in path_bs.replace("/", "\\").split("\\") if p]
    entry_paths = [e.path for e in edat._entries]
    for n in range(2, len(parts) + 1):
        suffix = "\\".join(parts[-n:]).lower()
        matches = [p for p in entry_paths if p.lower().endswith(suffix)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) == 0:
            return None
    return None


def _get_inst_offset(inst: int, segments: list) -> int:
    """Return the offset for an inst value using segment-based rules.

    Each segment is a dict with optional 'from'/'to' keys (inclusive bounds)
    and a required 'offset' key.  The first matching segment wins.
    """
    for seg in segments:
        lo = seg.get("from", 0)
        hi = seg.get("to", float("inf"))
        if lo <= inst <= hi:
            return int(seg["offset"])
    return 0


def _translate_objref_by_offset(
    value: Any,
    offset: int,
    class_map: Optional[dict] = None,
    segments: Optional[list] = None,
) -> Any:
    """Recursively shift 'inst' (and optionally 'class') in ObjRef dicts.

    Handles plain ObjRef dicts, lists of ObjRefs, and lists of [key, ObjRef]
    pairs (Map<StringRef,ObjRef>).  Returns the original object unchanged when
    there is nothing to translate (avoids unnecessary copies).
    class_map maps OG class index → Updated class index.
    segments overrides offset with per-range offsets: [{from, to, offset}, ...]
    """
    if isinstance(value, dict) and "inst" in value:
        inst = value["inst"]
        actual_offset = _get_inst_offset(inst, segments) if segments else offset
        new = {**value, "inst": inst + actual_offset}
        if class_map and "class" in new:
            new["class"] = class_map.get(new["class"], new["class"])
        return new
    if isinstance(value, list):
        changed = False
        new_list = []
        for elem in value:
            new_elem = _translate_objref_by_offset(elem, offset, class_map, segments)
            new_list.append(new_elem)
            if new_elem is not elem:
                changed = True
        return new_list if changed else value
    return value


def _build_objref_local_id_map(patch_group: mod_format.ModPatchGroup) -> dict:
    """Return {inst_number: local_id} for creates whose local_id is 'inst_N'."""
    import re
    m = {}
    for change in patch_group.changes:
        if change.action == "create" and change.local_id:
            match = re.match(r"^inst_(\d+)$", change.local_id)
            if match:
                m[int(match.group(1))] = change.local_id
    return m


def _translate_change_for_public(
    change: mod_format.ModChange,
    objref_to_local_id: dict,
    upd_pre_count: int = 0,
    patch_index_map: Optional[dict] = None,
) -> mod_format.ModChange:
    """Return a copy of change with _index and ObjRef values translated for R.U.S.E.

    Translates:
    - '_index' in match using patch_index_map first, then path_map table (chqlmapinfo.cpp)
    - ObjRef{inst:N} in set_props → $ref{local_id} when N is a created instance
    - List<ObjRef> items: created-instance elements → {"$ref": local_id};
      OG-only elements (position ≥ upd_pre_count, not in map) are dropped
    """
    new_match = dict(change.match)
    if "_index" in new_match:
        og_idx = int(new_match["_index"])
        if patch_index_map and str(og_idx) in patch_index_map:
            new_idx = patch_index_map[str(og_idx)]
        else:
            new_idx = path_map_mod.translate_index(change.table, og_idx)
        if new_idx != og_idx:
            new_match["_index"] = str(new_idx)

    new_set_props = dict(change.set_props)
    for prop_name, val_def in change.set_props.items():
        if val_def.type_name.lower() == "objref" and isinstance(val_def.value, dict):
            inst_n = val_def.value.get("inst")
            if inst_n is not None and int(inst_n) in objref_to_local_id:
                local_id = objref_to_local_id[int(inst_n)]
                new_set_props[prop_name] = mod_format.ModValueDef(
                    type_name="$ref", value=local_id
                )
        elif isinstance(val_def.value, list):
            # Translate ObjRef elements inside List<ObjRef> (e.g. MultiList, ChallengeList).
            # Created-instance refs → {"$ref": local_id} so they resolve via create_map.
            # OG-only positions (≥ upd_pre_count, not in map) are dropped — they reference
            # instances that exist in the OG NDF but not in the Updated NDF layout.
            new_list = []
            list_changed = False
            for elem in val_def.value:
                if isinstance(elem, dict) and "inst" in elem and "class" in elem:
                    inst_n = int(elem["inst"])
                    if inst_n in objref_to_local_id:
                        local_id = objref_to_local_id[inst_n]
                        new_list.append({"$ref": local_id})
                        list_changed = True
                    elif upd_pre_count > 0 and inst_n >= upd_pre_count:
                        # OG-specific position beyond Updated's pre-create instance count
                        list_changed = True  # drop this element
                    else:
                        new_list.append(elem)
                else:
                    new_list.append(elem)
            if list_changed:
                new_set_props[prop_name] = mod_format.ModValueDef(
                    type_name=val_def.type_name, value=new_list
                )

    return mod_format.ModChange(
        action=change.action,
        table=change.table,
        match=new_match,
        set_props=new_set_props,
        del_props=change.del_props,
        local_id=change.local_id,
    )


def _preresolve_addresses(changes, ndf):
    """Snapshot every change's match ADDRESS to a concrete {"_index": idx} against the CURRENT (post-create,
    pre-patch) NDF, in place, BEFORE any patch mutates instances.  Covers all durable address forms:
      * {"anchor": {...}}          — keyed-ancestor + path (H3)
      * {"ClassNameForDebug": ...} — a stable key (also DescriptorId / _ShortDatabaseName / Name)

    Why up front: a change's target must be resolved while it is still findable.  Two ways a later change
    would otherwise break an earlier address if resolved lazily during the patch loop:
      1. A PATCH that RENAMES the instance's key (e.g. the Zombie/Endless mod patches Unit_..._Antichar's
         ClassNameForDebug to "Undead").  A following delete_props/patch matched on the OLD key then finds
         nothing — the edit is silently dropped and the whole NDF falls back to a raw file patch.
      2. A patch that edits a list/ObjRef ON an anchor's path, moving where that anchor resolves.
    Snapshotting fixes each target index while the address is still valid; ORDER between the remaining
    index-addressed changes then no longer matters.  The address's cross-BUILD durability job is already
    done at resolve time (it resolved against THIS build's pristine NDF), so applying by index afterwards is
    exact.  Only a UNIQUELY-resolving match is snapshotted; 0 matches (renamed/removed on this build) or an
    ambiguous >1 are left as-is so find_instances -> requires-repair still reports them (the durability
    report and the create-then-patch-by-new-key case both keep working)."""
    for ch in changes:
        m = getattr(ch, "match", None)
        if m and "_index" not in m:
            if "anchor" in m:
                idx = ndf.resolve_anchor(m["anchor"])
                if idx is not None:
                    ch.match = {"_index": str(idx)}
            else:
                # A stable-key match: snapshot to index so an intra-batch rename can't strand it.  find_
                # instances here also REPLACES the O(n) key search the apply loop would do — net perf-neutral
                # (the expensive scan happens once; apply then takes the O(1) _index fast-path).
                hits = ndf.find_instances(ch.table, m)
                if len(hits) == 1:
                    ch.match = {"_index": str(hits[0][0])}
        # ObjRef-value anchors nested in set_props (single ObjRef, List<ObjRef>, Map/Pair) — same risk.
        for vd in (getattr(ch, "set_props", None) or {}).values():
            _preresolve_objref_anchors(getattr(vd, "value", None), ndf)


def _preresolve_objref_anchors(value, ndf):
    """Rewrite ObjRef {"anchor":...} -> {"inst": idx} in a set-block value (recursing lists/dicts) against
    the pristine NDF, so a mid-apply patch can't break the reference's path."""
    if isinstance(value, dict):
        if "anchor" in value and "class" in value:      # an ObjRef anchor
            idx = ndf.resolve_anchor(value["anchor"])
            if idx is not None:
                value.pop("anchor")
                value["inst"] = idx
        else:
            for v in value.values():
                _preresolve_objref_anchors(v, ndf)
    elif isinstance(value, list):
        for v in value:
            _preresolve_objref_anchors(v, ndf)


def _apply_change(
    change: mod_format.ModChange,
    ndf: ndfbin_mod.NdfBinary,
    result: ApplyResult,
    create_map: dict,
    deploy_state: Optional[dict] = None,
) -> int:
    """Apply one ModChange.  Returns number of instances affected."""

    if change.action == "patch":
        return _action_patch(change, ndf, result, create_map, deploy_state)
    if change.action == "delete":
        return _action_delete(change, ndf, result)
    if change.action == "delete_props":
        return _action_delete_props(change, ndf, result)
    if change.action == "create":
        return _action_create(change, ndf, result, create_map)

    result.warn(f"Unknown action '{change.action}' for table '{change.table}'")
    return 0


def _inst_identity(inst, ndf, idx):
    """A hashable identity for an instance, stable across the mods in one deploy: its stable key when it
    has one (units/buildings do), else its position.  Used to key the deploy touched-set for conflict
    detection so two mods that address the same object by different means still collide."""
    for kp in ("ClassNameForDebug", "DescriptorId", "_ShortDatabaseName", "Name"):
        p = ndf.prop_by_name_and_class(kp, inst.class_index)
        if p is not None:
            v = inst.get(p.index)
            if v is not None and v.type_id in (ndfbin_mod.T.StringRef, ndfbin_mod.T.PathRef):
                s = ndf.get_string(v.raw)
                if s and s != "None":
                    return ("k", kp, s)
    return ("i", idx)


def _repair_reason(match, action: str) -> str:
    """Why an unresolved match needs repair — distinguishes a drifted position from a changed/removed key."""
    if match and "_index" in match:
        return (f"{action}: positional _index target not found on this build "
                f"(index drifted off class, or the instance was removed)")
    return (f"{action}: keyed target not found on this build "
            f"(the key was renamed, or the instance was removed)")


def _action_patch(
    change: mod_format.ModChange,
    ndf: ndfbin_mod.NdfBinary,
    result: ApplyResult,
    create_map: dict,
    deploy_state: Optional[dict] = None,
) -> int:
    matches = ndf.find_instances(change.table, change.match or None)
    if not matches:
        result.flag_repair(change.table, change.match, _repair_reason(change.match, "patch"))
        return 0

    # Key-based patches (no _index) must not hit instances created in this session.
    # A mod may create a new unit with the same ClassNameForDebug as an OG unit it
    # also patches; without this guard the patch would overwrite the newly-created one.
    if "_index" not in (change.match or {}):
        created_indices = {inst_idx for inst_idx, _ in create_map.values()}
        if created_indices:
            matches = [(idx, inst) for idx, inst in matches if idx not in created_indices]
            if not matches:
                result.warn(
                    f"patch: no *pre-existing* instances of '{change.table}' matched "
                    f"{change.match} after excluding session creates — skipped"
                )
                return 0

    affected = 0
    for inst_idx, inst in matches:
        # Determine a human-readable id for this instance (ClassNameForDebug preferred)
        instance_id = _instance_label(inst, ndf, inst_idx)
        identity = _inst_identity(inst, ndf, inst_idx) if deploy_state is not None else None

        for prop_name, val_def in change.set_props.items():
            try:
                ndf_val = _make_ndf_value(val_def, prop_name, ndf, create_map)
            except Exception as e:
                result.warn(f"patch: cannot create value for '{prop_name}': {e}")
                continue

            # Capture old value before overwriting
            old_val_str = _read_prop_str(inst, prop_name, ndf)

            # create=True: a prop the class doesn't have yet is ADDED (lets a mod introduce a new
            # property, e.g. a new gdconstante, not just change existing ones).
            ok = ndf.set_property(inst, prop_name, ndf_val, create=True)
            if not ok:
                result.warn(
                    f"patch: property '{prop_name}' not found in class '{change.table}' "
                    f"(instance #{inst_idx})"
                )
            else:
                new_val_str = str(ndf.resolve_value(ndf_val))
                rec = ChangeRecord(change.table, instance_id, prop_name,
                                   old_val_str, new_val_str)
                result.change_log.append(rec)
                log.info("%s", rec)
                # Conflict detection: did an EARLIER mod in this deploy already set this instance+prop?
                # Last-writer-wins is intended, but tell the user the earlier edit was superseded.
                if deploy_state is not None:
                    tkey = (result._ctx_dat, result._ctx_ndf, identity, prop_name)
                    prev = deploy_state.get(tkey)
                    if prev is not None and prev != result.mod_name:
                        result.flag_conflict(instance_id, prop_name, prev)
                    deploy_state[tkey] = result.mod_name
        affected += 1

    return affected


def _instance_label(inst, ndf: ndfbin_mod.NdfBinary, idx: int) -> str:
    """Return ClassNameForDebug string or AmmunitionId or fallback to index."""
    for candidate in ("ClassNameForDebug", "AmmunitionId", "_ShortDatabaseName"):
        # Must look up by class to get the correct property index for this class
        prop = ndf.prop_by_name_and_class(candidate, inst.class_index)
        if prop is None:
            continue
        val = inst.get(prop.index)
        if val is not None:
            return str(ndf.resolve_value(val))
    return f"#{idx}"


def _read_prop_str(inst, prop_name: str, ndf: ndfbin_mod.NdfBinary) -> str:
    """Return the current string representation of a property, or '?' if absent."""
    prop = ndf.prop_by_name_and_class(prop_name, inst.class_index)
    if prop is None:
        prop = ndf.prop_by_name(prop_name)
    if prop is None:
        return "?"
    val = inst.get(prop.index)
    if val is None:
        return "?"
    return str(ndf.resolve_value(val))


def _action_delete(
    change: mod_format.ModChange,
    ndf: ndfbin_mod.NdfBinary,
    result: ApplyResult,
) -> int:
    import bisect
    matches = ndf.find_instances(change.table, change.match or None)
    if not matches:
        result.flag_repair(change.table, change.match, _repair_reason(change.match, "delete"))
        return 0

    indices_to_remove = sorted([idx for idx, _ in matches], reverse=True)
    removed_set = set(indices_to_remove)
    removed_asc = sorted(removed_set)

    for idx in indices_to_remove:
        del ndf.instances[idx]

    # Remap top_objects: drop entries for deleted instances, shift remaining.
    def remap(i):
        return i - bisect.bisect_left(removed_asc, i)

    ndf.top_objects = [remap(i) for i in ndf.top_objects if i not in removed_set]

    # Remap inbound ObjRef VALUES too: deleting instances shifts every higher index down by one, so any
    # Reference that points ABOVE a deletion is now off by the number of deletions below it.  top_objects
    # was the only list remapped before, leaving in-instance ObjRefs dangling.  Walk all values and fix
    # OBJ_REF targets; a ref to a just-deleted instance is genuinely dangling (its target is gone) — remap
    # it to the deletion point and count it so the loss isn't silent.  (No-op when nothing points above a
    # deletion, e.g. deletes at the tail — so it doesn't perturb mods whose deletes don't move any ref.)
    dangling = 0
    for v in ndf.iter_values():
        if (v.type_id == ndfbin_mod.T.Reference and isinstance(v.raw, tuple)
                and v.raw[0] == ndfbin_mod.OBJ_REF_MARKER and isinstance(v.raw[1], tuple)):
            oi, ci = v.raw[1]
            if isinstance(oi, int) and oi >= 0:
                if oi in removed_set:
                    dangling += 1
                new_oi = remap(oi)
                if new_oi != oi:
                    v.raw = (ndfbin_mod.OBJ_REF_MARKER, (new_oi, ci))
    if dangling:
        # Info, not a user warning: real mods (e.g. the Operation clustermaps) legitimately delete a whole
        # cluster whose members reference each other, so refs to deleted instances are expected and harmless
        # (byte-identical to the long-shipped output).  Log for diagnostics without alarming the user.
        log.info("delete: %d ObjRef(s) referenced a deleted '%s' instance", dangling, change.table)

    log.info("delete: removed %d instance(s) of '%s'", len(indices_to_remove), change.table)
    return len(indices_to_remove)


def _action_delete_props(
    change: mod_format.ModChange,
    ndf: ndfbin_mod.NdfBinary,
    result: ApplyResult,
) -> int:
    matches = ndf.find_instances(change.table, change.match or None)
    if not matches:
        result.flag_repair(change.table, change.match, _repair_reason(change.match, "delete_props"))
        return 0

    affected = 0
    for inst_idx, inst in matches:
        for prop_name in change.del_props:
            prop = ndf.prop_by_name_and_class(prop_name, inst.class_index)
            if prop is None:
                prop = ndf.prop_by_name(prop_name)
            if prop is None:
                result.warn(
                    f"delete_props: property '{prop_name}' not found in NDF — skipped"
                )
                continue
            removed = inst.remove(prop.index)
            if not removed:
                result.warn(
                    f"delete_props: property '{prop_name}' not present on instance "
                    f"#{inst_idx} — skipped"
                )
            else:
                log.info("delete_props: removed '%s' from %s[%d]",
                         prop_name, change.table, inst_idx)
        affected += 1

    return affected


def _action_create(
    change: mod_format.ModChange,
    ndf: ndfbin_mod.NdfBinary,
    result: ApplyResult,
    create_map: dict,
) -> int:
    n, _ = _action_create_deferred(change, ndf, result, create_map)
    return n


def _action_create_deferred(
    change: mod_format.ModChange,
    ndf: ndfbin_mod.NdfBinary,
    result: ApplyResult,
    create_map: dict,
) -> tuple:
    """Create an instance, deferring stable_ref ObjRef props that can't resolve yet.

    Returns (count, deferred_props) where deferred_props is a dict of
    {prop_name: val_def} for properties that need to be set after all
    creates in this patch group have been registered.
    """
    cls = ndf.class_by_name(change.table)
    if cls is None:
        result.warn(f"create: class '{change.table}' not found in NDF — skipped")
        return 0, {}

    inst = ndfbin_mod.NdfInstance(class_index=cls.index)
    deferred = {}

    for prop_name, val_def in change.set_props.items():
        try:
            ndf_val = _make_ndf_value(val_def, prop_name, ndf, create_map)
        except ValueError as e:
            msg = str(e)
            if "stable_ref not found" in msg or "local_id not in create_map" in msg:
                # Target instance not yet created — defer until second pass
                deferred[prop_name] = val_def
                continue
            result.warn(f"create: cannot create value for '{prop_name}': {e}")
            continue
        except Exception as e:
            result.warn(f"create: cannot create value for '{prop_name}': {e}")
            continue
        ok = ndf.set_property(inst, prop_name, ndf_val, create=True)
        if not ok:
            result.warn(f"create: property '{prop_name}' not found in class '{change.table}'")

    inst_idx = len(ndf.instances)
    ndf.instances.append(inst)
    if change.is_top_object:
        ndf.top_objects.append(inst_idx)

    if change.local_id:
        create_map[change.local_id] = (inst_idx, cls.index)
        log.info("create: added new instance to '%s' as '%s' (idx=%d)",
                 change.table, change.local_id, inst_idx)
    else:
        log.info("create: added new instance to '%s' (idx=%d)", change.table, inst_idx)
    return 1, deferred


# ── Value construction ────────────────────────────────────────────────────────

def _objref_value(ndf: ndfbin_mod.NdfBinary, idx, stored_cls, class_name=None) -> ndfbin_mod.NdfValue:
    """Build an ObjRef NdfValue, choosing its declared class by a clear precedence:

      1. the resolved TARGET instance's actual class (only when coercion is enabled for this NDF);
      2. the portable ``class_name`` resolved against THIS build's class table (cross-build remap);
      3. the raw stored class.

    (1) enforces the PROVEN clean-data invariant "declared class == target's actual class" (0 declared!=actual
    across 240k+ ObjRefs on both builds) — a no-op on valid refs, and the fix for a stale/mis-captured class
    that would make the engine build the wrong type and crash.  Coercion is OPT-IN (default off) because that
    invariant is NOT universal: it holds in everything.cpp but not e.g. camera-path .ndfbin (a TCameraPathKey
    ref legitimately targets a TCameraPath).  The deploy loop enables it only for everything.cpp; every other
    NDF and the converter self-check keep the source class faithfully.

    ``idx``/``class`` are normalised with int() to match ndfbin.make_value (rmod JSON may encode them as
    strings).  A NULL ObjRef (idx 0xFFFFFFFF, out of range) keeps its stored class.  Raises ValueError on a
    non-integer index or when no class can be determined, instead of emitting a malformed NdfValue.
    """
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        raise ValueError(f"ObjRef 'inst' must be an integer, got {idx!r}")
    cls = None
    if getattr(ndf, "_coerce_objref_class", False) and 0 <= idx < len(ndf.instances):
        cls = ndf.instances[idx].class_index
    if cls is None and isinstance(class_name, str):
        cls = ndf.class_index_by_name(class_name)
    if cls is None:
        cls = stored_cls
    if cls is None:
        raise ValueError(
            f"ObjRef to inst {idx}: cannot determine class (no coercion target, "
            f"no resolvable class_name, and no stored 'class')"
        )
    try:
        cls = int(cls)
    except (TypeError, ValueError):
        raise ValueError(f"ObjRef 'class' must be an integer, got {cls!r}")
    return ndfbin_mod.NdfValue(ndfbin_mod.T.Reference, (ndfbin_mod.OBJ_REF_MARKER, (idx, cls)))


def _resolve_stable_ref_objref(value: dict, ndf: ndfbin_mod.NdfBinary,
                               create_map: dict) -> ndfbin_mod.NdfValue:
    """Resolve an ObjRef dict that carries a stable_ref key into an NdfValue.

    Looks up the target instance by ClassNameForDebug/DescriptorId in the live
    NDF (which includes any instances already created this apply run).
    Raises ValueError if not found so the caller can defer or warn.
    """
    key_prop = value["stable_ref"]
    key_val  = value["key_val"]
    found = ndf.find_instance_by_key(key_prop, key_val)
    if found is None:
        raise ValueError(f"stable_ref not found: {key_prop}='{key_val}'")
    inst_idx, found_inst = found
    # Use the class index from the live NDF instance rather than the rmod's stored
    # class (which may be a compat index if the rmod was not fully pre-translated).
    return ndfbin_mod.NdfValue(
        ndfbin_mod.T.Reference,
        (ndfbin_mod.OBJ_REF_MARKER, (inst_idx, found_inst.class_index))
    )


def _make_ndf_value(
    val_def: mod_format.ModValueDef,
    prop_name: str,
    ndf: ndfbin_mod.NdfBinary,
    create_map: dict = None,
) -> ndfbin_mod.NdfValue:
    """Convert a ModValueDef into an NdfValue, resolving string refs and $refs.

    ObjRef class selection (target-coercion / portable class_name / stored class) is centralised in
    _objref_value — see it for the precedence and why (a stale, build-specific class index otherwise makes
    the engine construct the WRONG type and crash; see RE_DATA/EVIDENCE_DOSSIER.md)."""
    # Resolve symbolic reference to a created instance
    if val_def.type_name == "$ref":
        if create_map is None or val_def.value not in create_map:
            raise ValueError(
                f"$ref '{val_def.value}' not found in create_map "
                f"(create must appear before the patch that references it)"
            )
        inst_idx, cls_idx = create_map[val_def.value]
        return ndfbin_mod.NdfValue(
            ndfbin_mod.T.Reference,
            (ndfbin_mod.OBJ_REF_MARKER, (inst_idx, cls_idx))
        )

    # Resolve stable_ref ObjRef (cross-file-portable instance reference by stable key)
    if (val_def.type_name.lower() == "objref"
            and isinstance(val_def.value, dict)
            and "stable_ref" in val_def.value):
        return _resolve_stable_ref_objref(val_def.value, ndf, create_map or {})

    # Resolve anchor ObjRef (H3): a durable keyless reference — keyed ancestor + path — resolved on the
    # target NDF.  (Match anchors are pre-resolved before the patch loop; ObjRef-value anchors likewise via
    # _preresolve_addresses, so by here it's usually already an {inst}; this is the direct/deferred path.)
    if (val_def.type_name.lower() == "objref"
            and isinstance(val_def.value, dict)
            and "anchor" in val_def.value):
        idx = ndf.resolve_anchor(val_def.value["anchor"])
        if idx is None or not (0 <= idx < len(ndf.instances)):
            raise ValueError(f"ObjRef anchor did not resolve on target NDF: {val_def.value.get('anchor')}")
        # An ObjRef's declared class MUST equal the target instance's actual runtime class.  This is a
        # PROVEN invariant of clean R.U.S.E. data: 0 declared!=actual across 240k+ ObjRefs on both builds
        # (compat-2 & public).  A stale stored class (a compat index, or a class the source mod captured
        # wrong) makes the engine construct the WRONG type over the target's memory and crash on load
        # (proven root cause: class 246 -> TBoneAlgorithmBase half-built AddRef; see RE_DATA dossier).
        # Derive the class from the resolved target — identical to the stable_ref path — so the output
        # always satisfies the invariant.  No-op on valid refs (stored already == target); build-agnostic.
        return _objref_value(ndf, idx, val_def.value.get("class"), val_def.value.get("class_name"))

    # Resolve local_id ObjRef (intra-rmod reference to a sibling create action)
    if (val_def.type_name.lower() == "objref"
            and isinstance(val_def.value, dict)
            and "local_id" in val_def.value):
        lid = val_def.value["local_id"]
        entry = (create_map or {}).get(lid)
        if entry is None:
            raise ValueError(f"local_id not in create_map: '{lid}'")
        # Use the class index registered at create time (from the live NDF's class table),
        # not the class from the rmod (which may be a compat index pre-translation).
        inst_idx, cls_idx = entry
        return ndfbin_mod.NdfValue(
            ndfbin_mod.T.Reference,
            (ndfbin_mod.OBJ_REF_MARKER, (inst_idx, cls_idx))
        )

    # Plain {inst, class} ObjRef: derive the declared class from the resolved target instead of trusting
    # the stored class.  Same invariant as the stable_ref/anchor paths — see _objref_value.  (Anchors are
    # usually pre-resolved to {inst} before we get here, so this is the dominant coercion site.)
    if (val_def.type_name.lower() == "objref"
            and isinstance(val_def.value, dict)
            and "inst" in val_def.value):
        return _objref_value(ndf, val_def.value["inst"], val_def.value.get("class"),
                             val_def.value.get("class_name"))

    # For List types, elements in the raw list may themselves be $ref dicts, stable_ref
    # ObjRef dicts, local_id ObjRef dicts, or plain ObjRef dicts
    key = val_def.type_name.lower().strip()
    if key.startswith("list<") and key.endswith(">") and isinstance(val_def.value, list):
        elem_type = key[5:-1].strip()
        items = []
        for elem in val_def.value:
            if isinstance(elem, dict) and "$ref" in elem:
                elem_def = mod_format.ModValueDef(type_name="$ref", value=str(elem["$ref"]))
            elif isinstance(elem, dict) and "stable_ref" in elem:
                # Inline stable_ref element — resolve directly
                elem_def = mod_format.ModValueDef(type_name="ObjRef", value=elem)
            elif isinstance(elem, dict) and "local_id" in elem:
                # Intra-rmod create reference stored as local_id
                elem_def = mod_format.ModValueDef(type_name="ObjRef", value=elem)
            elif isinstance(elem, dict) and "anchor" in elem:
                # Durable keyless reference (H3)
                elem_def = mod_format.ModValueDef(type_name="ObjRef", value=elem)
            elif isinstance(elem, dict) and "type" in elem:
                elem_type_here = elem["type"]
                elem_val = elem.get("value", elem)
                if elem_type_here == "$ref":
                    elem_def = mod_format.ModValueDef(type_name="$ref", value=str(elem_val))
                else:
                    elem_def = mod_format.ModValueDef(type_name=elem_type_here, value=elem_val)
            else:
                # Infer Reference subtype from dict keys so mixed ObjRef/TransRef lists work
                if isinstance(elem, dict) and "trans" in elem and "inst" not in elem:
                    actual_elem_type = "TransRef"
                elif isinstance(elem, dict) and "inst" in elem and "class" in elem:
                    actual_elem_type = "ObjRef"
                else:
                    actual_elem_type = elem_type
                elem_def = mod_format.ModValueDef(type_name=actual_elem_type, value=elem)
            items.append(_make_ndf_value(elem_def, prop_name, ndf, create_map))
        return ndfbin_mod.NdfValue(ndfbin_mod.T.List, items)

    # Handle Pair with typed elements [{type, value}, {type, value}] so that
    # ObjRef local_id/stable_ref inside a Pair are resolved via _make_ndf_value
    # rather than falling through to ndfbin.make_value which only handles raw dicts.
    if key == "pair" and isinstance(val_def.value, (list, tuple)) and len(val_def.value) == 2:
        k_raw, v_raw = val_def.value
        if isinstance(k_raw, dict) and "type" in k_raw and isinstance(v_raw, dict) and "type" in v_raw:
            k_ndf = _make_ndf_value(
                mod_format.ModValueDef(type_name=k_raw["type"], value=k_raw.get("value", k_raw)),
                prop_name, ndf, create_map)
            v_ndf = _make_ndf_value(
                mod_format.ModValueDef(type_name=v_raw["type"], value=v_raw.get("value", v_raw)),
                prop_name, ndf, create_map)
            return ndfbin_mod.NdfValue(ndfbin_mod.T.Pair, (k_ndf, v_ndf))

    # Handle Map with typed elements so ObjRef local_id/stable_ref values in map
    # entries are resolved correctly instead of going to ndfbin.make_value.
    if key.startswith("map<") and key.endswith(">") and isinstance(val_def.value, list):
        inner = key[4:-1].strip()
        depth, split_pos = 0, -1
        for i, ch in enumerate(inner):
            if ch == "<":
                depth += 1
            elif ch == ">":
                depth -= 1
            elif ch == "," and depth == 0:
                split_pos = i
                break
        if split_pos != -1:
            k_type = inner[:split_pos].strip()
            v_type = inner[split_pos + 1:].strip()
            pairs = []
            for pair in val_def.value:
                k_ndf = _make_ndf_value(
                    mod_format.ModValueDef(type_name=k_type, value=pair[0]),
                    prop_name, ndf, create_map)
                v_ndf = _make_ndf_value(
                    mod_format.ModValueDef(type_name=v_type, value=pair[1]),
                    prop_name, ndf, create_map)
                pairs.append((k_ndf, v_ndf))
            return ndfbin_mod.NdfValue(ndfbin_mod.T.Map, pairs)

    ndf_val = ndfbin_mod.make_value(val_def.type_name, val_def.value)
    _resolve_string_refs(ndf_val, ndf)
    return ndf_val


def _resolve_string_refs(
    ndf_val: ndfbin_mod.NdfValue,
    ndf: ndfbin_mod.NdfBinary,
) -> None:
    """Recursively resolve string/trans table references to live NDF table indices.

    make_value() leaves StringRef/PathRef raws as Python strings, and TransRef
    raws as Python strings (when stored in the portable name-based format), so the
    caller can resolve them against the live NDF tables.  This function walks the
    value tree (including List and Map containers) and performs that resolution in-place.
    """
    t = ndf_val.type_id
    if t in (ndfbin_mod.T.StringRef, ndfbin_mod.T.PathRef):
        if isinstance(ndf_val.raw, str):
            ndf_val.raw = ndf.ensure_string(ndf_val.raw)
    elif t == ndfbin_mod.T.Reference:
        marker, ref = ndf_val.raw
        if marker == ndfbin_mod.TRANS_REF_MARKER and isinstance(ref, str):
            # A TransRef references an IMPORT by ordinal. Portable rmods store the
            # import's full path ('$/IA/Cluster/PackMesh_All'); map it to this NDF's
            # ordinal (adding the import if missing). A bare name has no '/' — treat
            # as a legacy TRAN string (pre-surgical-imports rmods).
            if "/" in ref:
                ndf_val.raw = (marker, ndf.add_import_path(ref))
            else:
                # Legacy bare-name TransRef: resolves to a TRAN index, but the game reads a TransRef ordinal
                # as an IMPR IMPORT ordinal — so this only points correctly when the TRAN index coincidentally
                # equals the intended import's ordinal on the ORIGIN build; it mis-points after migration/
                # reconversion on another build. Re-convert the rmod on its origin build to get a portable
                # '$/...' path. (See RE_DATA/EVIDENCE_DOSSIER.md Evidence #1.) Behaviour unchanged; flagged.
                log.warning("legacy bare-name TransRef %r resolved via TRAN index — migration-unsafe; "
                            "re-convert this rmod on its origin build for a portable '$/' import path", ref)
                ndf_val.raw = (marker, ndf.ensure_trans(ref))
    elif t == ndfbin_mod.T.List:
        for item in ndf_val.raw:
            _resolve_string_refs(item, ndf)
    elif t == ndfbin_mod.T.Map:
        for k, v in ndf_val.raw:
            _resolve_string_refs(k, ndf)
            _resolve_string_refs(v, ndf)
    elif t == ndfbin_mod.T.Pair:
        k, v = ndf_val.raw
        _resolve_string_refs(k, ndf)
        _resolve_string_refs(v, ndf)


# ── Surgical IMPR/EXPR (import/export tree) merge ───────────────────────────────

def _remove_ref_paths(ndf, kind: str, paths) -> None:
    """Drop the given full paths from the NDF's import (kind='import') or export tree."""
    get = ndf.import_ordinal_paths if kind == "import" else ndf.export_ordinal_paths
    cur = get()
    drop = set(paths)
    kept = {o: p for o, p in cur.items() if p not in drop}
    tree = ndfbin_mod.serialize_ref_tree(ndfbin_mod.build_ref_tree(kept, ndf.ensure_trans))
    if kind == "import":
        ndf.import_list = tree
    else:
        ndf.export_list = tree


def _apply_ref_adds(ndf, patch_group, result) -> bool:
    """Phase A of the surgical IMPR/EXPR merge — run BEFORE instance patches.

    Only *adds* imports/exports (appends new ordinals) so that TransRef values in the
    patches (stored as portable paths) resolve to a real ordinal.  Removals + the dense
    renumber are deferred to phase B (_finalize_ref_edits) so they run AFTER the instance
    patches have re-pointed any TransRef away from a to-be-removed import — otherwise a
    still-referenced import would be dropped and its referrers left dangling."""
    changed = False
    try:
        for path in getattr(patch_group, "import_add", []) or []:
            ndf.add_import_path(path); changed = True
        for path in getattr(patch_group, "export_add", []) or []:
            ndf.add_export_path(path); changed = True
    except Exception as e:  # noqa: BLE001
        result.warn(f"import/export add failed: {e}")
    return changed


def _finalize_ref_edits(ndf, patch_group, result) -> bool:
    """Phase B — run AFTER instance patches.  Applies import_remove and reassigns DENSE
    import ordinals (0..N-1), remapping every instance TransRef by path identity.

    The engine sizes the import table to the node count and indexes it by ordinal, so a
    gap (from a removal) or an over-max ordinal (from an append) makes referring instances
    read past the table — an out-of-bounds crash on load.  We never drop an import that an
    instance still references post-patch (that would dangle the referrer); such removals
    are skipped with a warning instead."""
    changed = False
    try:
        imp_add = list(getattr(patch_group, "import_add", []) or [])
        imp_rem = set(getattr(patch_group, "import_remove", []) or [])
        exp_rem = set(getattr(patch_group, "export_remove", []) or [])
        if imp_add or imp_rem:
            cur = ndf.import_ordinal_paths()  # already includes phase-A adds
            # Which imports are still referenced by some instance (post-patch)?
            used_ords = {v.raw[1] for v in ndf.iter_values()
                         if v.type_id == ndfbin_mod.T.Reference and isinstance(v.raw, tuple)
                         and v.raw[0] == ndfbin_mod.TRANS_REF_MARKER and isinstance(v.raw[1], int)}
            used_paths = {cur[o] for o in used_ords if o in cur}
            # Refuse to drop a still-referenced import — keep it to avoid dangling refs.
            kept_referenced = {p for p in imp_rem if p in used_paths}
            if kept_referenced:
                result.warn("import_remove kept still-referenced import(s) to avoid "
                            f"dangling refs: {sorted(kept_referenced)}")
            drop = imp_rem - kept_referenced
            survivors = [p for _o, p in sorted(cur.items()) if p not in drop]
            ndf.set_import_paths(survivors)  # dense reassign + in-place TransRef remap
            changed = True
        if exp_rem:
            # Exports aren't referenced by instance ordinals, so a simple rebuild is safe.
            _remove_ref_paths(ndf, "export", exp_rem)
            changed = True
    except Exception as e:  # noqa: BLE001
        result.warn(f"import/export finalize failed: {e}")
    return changed


# ── Shared NDF change application ──────────────────────────────────────────────

def _apply_changes_to_ndf(changes, ndf, result) -> bool:
    """Apply a list of ModChange to an in-memory NdfBinary and return True if anything changed.

    Creates run first (so local_id $refs resolve in later patches), then patches/deletes, then a second
    pass resolves stable_ref ObjRef props whose targets were only created in this batch.  No
    compat→public translation — used for already-translated change lists (e.g. the placement NDF
    embedded inside a .scenario).  Increments result.changes_applied."""
    create_map: dict = {}
    deferred_objref: list = []
    changed = False

    def _inst_sort_key(c) -> int:
        lid = c.local_id or ""
        return int(lid[5:]) if (lid.startswith("inst_") and lid[5:].isdigit()) else 10 ** 9

    creates = sorted([c for c in changes if c.action == "create"], key=_inst_sort_key)
    non_creates = [c for c in changes if c.action != "create"]

    # Snapshot every match address against the PRISTINE NDF — BEFORE creates run.  A mod's own create can
    # introduce a duplicate stable key that destroys the global-key uniqueness an anchor root/key match
    # needs, stranding the target if resolved post-create (see the mirror comment in the patch-group path).
    _preresolve_addresses(non_creates, ndf)

    for change in creates:
        n, deferred = _action_create_deferred(change, ndf, result, create_map)
        if n > 0:
            changed = True
            result.changes_applied += n
            if deferred:
                inst_idx = create_map.get(change.local_id, (len(ndf.instances) - 1, 0))[0]
                deferred_objref.append((ndf.instances[inst_idx], change.table, deferred))

    for change in non_creates:
        n = _apply_change(change, ndf, result, create_map)
        if n > 0:
            changed = True
            result.changes_applied += n

    for inst, table_name, deferred in deferred_objref:
        for prop_name, val_def in deferred.items():
            try:
                ndf_val = _make_ndf_value(val_def, prop_name, ndf, create_map)
            except Exception as e:
                result.warn(f"deferred stable_ref: cannot set '{prop_name}' on '{table_name}': {e}")
                continue
            if ndf.set_property(inst, prop_name, ndf_val, create=True):
                result.changes_applied += 1
                changed = True
            else:
                result.warn(f"deferred stable_ref: property '{prop_name}' not found in '{table_name}'")
    return changed


# ── Backup helpers ────────────────────────────────────────────────────────────

def _make_backup(dat_path: Path) -> None:
    backup_path = dat_path.with_suffix(".dat.bak")
    if not backup_path.exists():
        shutil.copy2(str(dat_path), str(backup_path))
        log.info("Backup: %s → %s", dat_path.name, backup_path.name)
    else:
        log.debug("Backup already exists: %s", backup_path.name)


def restore_backup(dat_path: str) -> bool:
    """Restore a .dat from its .bak backup if one exists.  Returns True on success."""
    p = Path(dat_path)
    backup = p.with_suffix(".dat.bak")
    if not backup.exists():
        log.warning("No backup found for %s", p.name)
        return False
    shutil.copy2(str(backup), str(p))
    log.info("Restored: %s from backup", p.name)
    return True


# ── Conflict detection ────────────────────────────────────────────────────────

def check_conflicts(
    mod_paths: List[str],
    game_data_dir: str,
) -> List[str]:
    """
    Dry-run all mods and return a list of conflict descriptions where two mods
    patch the same property on the same instance.

    Returns a list of human-readable conflict strings (empty = no conflicts).
    """
    # Map (dat, ndf, table, match_key, prop_name) → list of mod names
    patches: dict = {}
    conflicts: List[str] = []

    for mod_path in mod_paths:
        try:
            mod = mod_format.load(mod_path)
        except Exception as e:
            # A single unreadable/hostile rmod must not crash the whole dry-run conflict preview.
            conflicts.append(f"(could not read {os.path.basename(mod_path)} for conflict check: {e})")
            continue
        for pg in mod.patches:
            for ch in pg.changes:
                if ch.action != "patch":
                    continue
                # `match` can be absent on a malformed patch — treat as empty rather than AttributeError.
                match_key = ";".join(f"{k}={v}" for k, v in sorted((ch.match or {}).items()))
                for prop_name in ch.set_props:
                    key = (pg.dat, pg.ndf, ch.table, match_key, prop_name)
                    if key not in patches:
                        patches[key] = []
                    patches[key].append(mod.name)

    for key, mod_names in patches.items():
        if len(mod_names) > 1:
            dat, ndf, table, match_key, prop_name = key
            conflicts.append(
                f"Conflict on {table}.{prop_name} [{match_key}] in {ndf}: "
                f"{' vs '.join(mod_names)}"
            )

    return conflicts

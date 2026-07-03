"""ruse_mod_engine/game_versions.py
================================================================================
Build-id awareness for the Mod Manager (issue #14).

The whole tool keys on the Steam BUILD ID now (backups, mod targeting, migration
maps), not the branch name — because a branch is just a moving pointer at a build
and branches even reorder (compat-3 is an older build than compat-2). The build id
is stable. We still DISPLAY the friendly branch name so users can confirm they
picked the branch they wanted.

A small registry SHIPS WITH the engine (data/game_versions.json) mapping each known
build id to {branch, dataver}. End users don't have the multi-GB dev snapshots, so
this committed table is how the app shows branch names and resolves the data-version
folder (99 = OG/compat format, 190852 = remaster) for any build.

Generation (maintainer): `python -m ruse_mod_engine.game_versions <sources_dir>`
rebuilds the table from sources/RUSE/versions.json + the snapshots.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from ._resources import data_dir
_DATA = data_dir() / "game_versions.json"
_OG_DATAVER = "99"          # OG / compat format
_REMASTER_DATAVER = "190852"  # everything compat-2 onward


# ── runtime registry ────────────────────────────────────────────────────────
def _load() -> dict:
    try:
        return json.loads(_DATA.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return {"builds": {}, "branches": {}}


def branch_for_build(build_id: str) -> Optional[str]:
    """Friendly branch name a build is/was served by (for display), or None."""
    info = _load()["builds"].get(str(build_id))
    return info.get("branch") if info else None


def dataver_for_build(build_id: str) -> Optional[str]:
    """Data-version folder ('99' OG vs '190852' remaster) for a build, or None."""
    info = _load()["builds"].get(str(build_id))
    return info.get("dataver") if info else None


def build_for_branch(branch: str) -> Optional[str]:
    """Current build id a branch points at (for migrating branch-named projects)."""
    return _load()["branches"].get(branch)


def known_builds() -> dict:
    """build_id -> {branch, dataver} for every build in the shipped registry."""
    return _load()["builds"]


def display_name(build_id: str) -> str:
    """e.g. 'compat-2 (v23661872)' for the UI; falls back to the raw build id."""
    br = branch_for_build(build_id)
    return f"{br} (v{build_id})" if br else f"v{build_id}"


# ── build-id detection from a Steam install ─────────────────────────────────
RUSE_APPID = "21970"


def detect_build_id(game_root) -> Optional[str]:
    """Read the Steam buildid for an installed game from its appmanifest.

    game_root is .../steamapps/common/R.U.S.E (so the manifest is two levels up at
    .../steamapps/appmanifest_21970.acf). Dev snapshots keep a steamapps/ too.
    """
    p = Path(game_root)
    candidates = [
        p.parent.parent / f"appmanifest_{RUSE_APPID}.acf",     # steamapps/common/X -> steamapps/
        p / "steamapps" / f"appmanifest_{RUSE_APPID}.acf",     # a snapshot root holding steamapps/
        p.parent / f"appmanifest_{RUSE_APPID}.acf",
    ]
    for acf in candidates:
        try:
            if acf.is_file():
                m = re.search(r'"buildid"\s+"(\d+)"', acf.read_text(encoding="utf-8", errors="ignore"))
                if m:
                    return m.group(1)
        except Exception:  # noqa: BLE001
            continue
    return None


def detect_dataver(game_root) -> str:
    """Data-version folder actually present in the install ('190852' remaster else
    '99' OG). Robust even when the build id isn't in the registry."""
    if (Path(game_root) / "Data" / "PC" / _REMASTER_DATAVER).is_dir():
        return _REMASTER_DATAVER
    return _OG_DATAVER


# ── maintainer: (re)generate the shipped registry from the dev snapshots ─────
def generate(sources) -> dict:
    from . import migrate
    tl = migrate.release_timeline(sources, include_og=True)
    builds, branches = {}, {}
    for e in tl:
        # record the data-version actually present (OG patchable = 99, not the
        # dat-count-heavy 1360 dir release_timeline may pick).
        dv = detect_dataver(e["root"])
        builds[e["buildid"]] = {"branch": e["branch"], "dataver": dv}
        branches[e["branch"]] = e["buildid"]
    reg = {"builds": builds, "branches": branches}
    _DATA.parent.mkdir(parents=True, exist_ok=True)
    _DATA.write_text(json.dumps(reg, indent=2), encoding="utf-8")
    return reg


if __name__ == "__main__":
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else "sources/RUSE"
    reg = generate(src)
    print(f"wrote {_DATA} with {len(reg['builds'])} builds:")
    for b, info in reg["builds"].items():
        print(f"  v{b}  {info['branch']:<12} dataver {info['dataver']}")

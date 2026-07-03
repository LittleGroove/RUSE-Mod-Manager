"""Update-readiness validator — "will this rmod actually RUN on the target public build?"

Migrating an OG/compat rmod to a new public branch is NOT just remapping data indices. The hard-won
lesson (Endless Defence / Fortress Europa / Overlord, 2026-07) is that the crashes that survive a clean
index-migration live in the *whole-file script + scenario* payload the migrator can't touch, and in the
engine's C++ behaviour changing between builds. This module runs every check for those failure modes so
the tool can tell the user, up front, **what a migration can auto-fix vs what needs a hand edit**.

Each Finding carries `migratable`:
  - migratable=True  → the data-layer migrator / applier can (or already does) fix this automatically.
  - migratable=False → a script/scenario/authoring change is required; migration will NOT fix it.

Public API:
    validate_rmod(path, game_root=None, target_build=None) -> Report
    validate(rmod_obj, *, game_root=None, target_build=None, rmod_path=None) -> Report

`game_root` (a clean source install for `target_build`) enables the apply-time checks (import density,
dangling refs). Without it, the static checks (scenario completeness, tag rules, camps, script, RCE,
version stamp) still run — they catch the crash-causing cases.

See docs/design/scenario-designer/22-mission-script-runtime-and-authoring.md and the rmod-update regimen.
"""
from __future__ import annotations
import base64
import json
import re
from dataclasses import dataclass, field
from typing import List, Optional

from . import scenario_validate as _sv

ERROR, WARN, INFO = "ERROR", "WARN", "INFO"

# Unsandboxed RCE — the Mod Manager must refuse any rmod whose script reaches these (see
# reference_executepython_rce). Detected in decompiled effetmap source.
_RCE_MARKERS = ("ExecutePython", "ConditionPython")


@dataclass
class Finding:
    severity: str          # ERROR / WARN / INFO
    category: str          # "version" | "scenario" | "tag" | "camps" | "script" | "security" | "imports"
    message: str
    fix: str = ""
    migratable: bool = False   # True = the migrator/applier handles it; False = needs a hand edit


@dataclass
class Report:
    findings: List[Finding] = field(default_factory=list)
    is_operation: bool = False    # has a scenario+effetmap payload (mission mod)

    @property
    def errors(self):
        return [f for f in self.findings if f.severity == ERROR]

    @property
    def warnings(self):
        return [f for f in self.findings if f.severity == WARN]

    @property
    def ok(self) -> bool:
        """Ready to run: no ERROR-level findings."""
        return not self.errors

    @property
    def needs_hand_fix(self) -> bool:
        """Has a blocking finding a migration can't fix on its own."""
        return any(f.severity == ERROR and not f.migratable for f in self.findings)

    def summary(self) -> str:
        e, w = len(self.errors), len(self.warnings)
        head = "READY" if self.ok else ("NEEDS HAND-FIX" if self.needs_hand_fix else "MIGRATE THEN OK")
        return "%s — %d error(s), %d warning(s)" % (head, e, w)


# ── payload extraction ─────────────────────────────────────────────────────────

def _payload(rmod: dict):
    """Return (scenario_bytes|None, effetmap_bytes|None) from an operation rmod's file_patches."""
    scen = eff = None
    for fp in rmod.get("file_patches", []) or []:
        for f in fp.get("files", []) or []:
            path = (f.get("path") or "").replace("\\", "/")
            data = f.get("data")
            if data is None:
                continue
            raw = base64.b64decode(data) if isinstance(data, str) else data
            if path.endswith(".scenario"):
                scen = raw
            elif path.endswith("effetmap.xyz"):
                eff = raw
    return scen, eff


# ── individual checks ──────────────────────────────────────────────────────────

def _check_version(rmod: dict, target_build, out: List[Finding]):
    gv = rmod.get("game_version")
    if gv in (None, "", "compat", "99"):
        out.append(Finding(WARN, "version",
                           "game_version is %r — the manager can't confirm this rmod matches the running build" % gv,
                           "stamp game_version with the target build id (run_conversion does this)", migratable=True))
    elif target_build is not None and str(gv) != str(target_build):
        out.append(Finding(WARN, "version",
                           "game_version %s != target build %s" % (gv, target_build),
                           "re-stamp for the target build", migratable=True))


def _tag_ir_map(src: str):
    """IR/IT variable -> ('TagKind', name), so we can see how each tag is *used* downstream."""
    out = {}
    for m in re.finditer(r"(\w+)\s*=\s*\w[\w.]*\.(Tag(?:Position|Unit|ZoneDetection))\('([^']*)'\)", src):
        out[m.group(1)] = (m.group(2), m.group(3))
    return out


def _call_bodies(src: str, classname: str):
    """Yield the balanced-paren argument body of every `…<classname>(...)` call in the source."""
    i = 0
    needle = classname + "("
    while True:
        j = src.find(needle, i)
        if j < 0:
            break
        k = j + len(needle)
        depth = 1
        start = k
        while k < len(src) and depth:
            c = src[k]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            k += 1
        yield src[start:k - 1]
        i = k


def _label_position_irs(src: str):
    """IRs used as a DrawLabel `Position=` in a label whose `Group=None`.

    That exact shape is what crashes on public: with no unit Group, `unit_label_ptr` stays None, so
    `display_label` reaches `info_panel.PositionRef = position.position`, and the public setter iterates
    the (unbound → None) position. A label WITH a real unit Group sets `unit_label_ptr` and skips the
    PositionRef path entirely (Overlord's `Usine_neutre` labels — unbound but harmless). And a non-label
    position consumer (DescriptorMove) tolerates None outright (ED's `Player_HQ`). Both must NOT count."""
    irs = set()
    for body in _call_bodies(src, "DescriptorDrawLabel"):
        gm = re.search(r"Group\s*=\s*(\w+)\b", body)
        if not gm or gm.group(1) != "None":
            continue   # a real unit Group sets unit_label_ptr → PositionRef never runs
        pm = re.search(r"Position\s*=\s*(\w+)\b", body)
        if pm and pm.group(1) != "None":
            irs.add(pm.group(1))
    return irs


def _check_effetmap(eff: bytes, out: List[Finding]):
    """Static checks on the mission script: RCE markers, camp NiveauIA, recompilability."""
    try:
        src = _sv._decompile_effetmap(eff)
    except Exception as e:  # noqa: BLE001
        out.append(Finding(WARN, "script", "effetmap could not be decompiled for validation: %s" % (str(e)[:80]),
                           "check the .xyz container / interpreter", migratable=False))
        return
    for marker in _RCE_MARKERS:
        if marker in src:
            out.append(Finding(ERROR, "security",
                               "script references %s — unsandboxed remote code execution" % marker,
                               "remove the %s call; the manager refuses such rmods" % marker, migratable=False))
    # Exactly one human-player camp expected in single-player missions.
    players = len(re.findall(r"NiveauIA\.Player\b", src))
    if players == 0:
        out.append(Finding(WARN, "camps", "no NiveauIA.Player camp — no human-controlled camp defined",
                           "one VariableCamp must be Niveau=NiveauIA.Player", migratable=False))
    elif players > 1:
        out.append(Finding(WARN, "camps", "%d NiveauIA.Player camps — usually exactly one (the human)" % players,
                           "AI camps should be Niveau=NiveauIA.Scripted (the Endless Defence crash class)",
                           migratable=False))
    # Recompilability (needed to hand-fix the script at all).
    try:
        from . import script_logic
        if script_logic.have_compiler():
            script_logic.recompile_source_to_xyz(script_logic._sanitize_decompiled(src), xyz_for_meta=eff)
    except Exception as e:  # noqa: BLE001
        out.append(Finding(INFO, "script",
                           "effetmap does not cleanly recompile via 2.5.1 (%s)" % (str(e)[:60]),
                           "hand-fixes to this script may need a sanitizer tweak", migratable=False))


def _severity_for_unbound(kind: str, usage: Optional[str]) -> str:
    """An unbound tag CRASHES the public engine only when its None is dereferenced: a zone tag scanned
    for .radius (always, via the condition loop), or a tag used as a Group=None label Position (the
    PositionRef setter unpacks it). Every other unbound tag is tolerated (latent, non-crashing)."""
    if kind == "TagZoneDetection":
        return ERROR
    if usage == "position":
        return ERROR
    return WARN


def _check_scenario(scen: bytes, eff: bytes, out: List[Finding]):
    """Tag → placement binding (scenario completeness), graded by how each UNBOUND tag is *used*.

    Binding is by string name (TagHelper registers each placement under item.name); a tag binds iff the
    scenario carries a placement of that name+kind. Name LENGTH is irrelevant — long names bind fine when
    the placement exists (Overlord's 21-char `dropp_parasleurres_01` works). An unbound tag only CRASHES
    the public engine when its None is dereferenced (zone .radius, or a label Position → PositionRef)."""
    src = _sv._decompile_effetmap(eff)
    ir_map = _tag_ir_map(src)
    label_pos = _label_position_irs(src)
    name_to_usage = {}   # tag name -> crash usage ('position' if used as a DrawLabel Position)
    for ir, (kind, name) in ir_map.items():
        if ir in label_pos and name_to_usage.get(name) is None:
            name_to_usage[name] = "position"

    rep = _sv.validate(scen, eff)
    if rep.tags_total:
        pct = rep.bound * 100 // rep.tags_total
        sev = INFO if pct >= 90 else WARN
        out.append(Finding(sev, "scenario",
                           "scenario binds %d/%d script tags (%d%%)%s"
                           % (rep.bound, rep.tags_total, pct,
                              " — near-zero binding means the WRONG/generic scenario is shipped" if pct < 25 else ""),
                           "" if pct >= 90 else "restore the mission's authored scenario or complete the placements",
                           migratable=False))
    for iss in rep.issues:
        sev = _severity_for_unbound(iss.kind, name_to_usage.get(iss.tag))
        note = " — dereferenced as a %s (crashes on public)" % (name_to_usage.get(iss.tag) or "zone") \
            if sev == ERROR else " — registered/tolerated (latent, won't crash)"
        out.append(Finding(sev, "scenario",
                           "%s('%s'): %s%s" % (iss.kind, iss.tag, iss.problem, note),
                           iss.fix, migratable=False))


def _check_imports(rmod_path, game_root, target_build, out: List[Finding]):
    """Apply on a clean target install and confirm every patched NDF has dense import ordinals + in-range
    TransRefs. The applier now auto-densifies, so a residual problem here is a real defect."""
    import os, tempfile
    from . import applier, ndfbin, edata
    if not rmod_path:
        return
    td = tempfile.mkdtemp()
    try:
        applier.apply_mod(rmod_path, game_root, backup=False, dry_run=False,
                          output_dir=td, game_version=str(target_build) if target_build else "public")
    except Exception as e:  # noqa: BLE001
        out.append(Finding(ERROR, "imports", "rmod fails to APPLY on the target build: %s" % (str(e)[:100]),
                           "inspect the failing patch group", migratable=False))
        return
    for r, _ds, fs in os.walk(td):
        for fn in fs:
            if not fn.endswith(".dat"):
                continue
            try:
                dd = edata.open_dat(os.path.join(r, fn))
            except Exception:
                continue
            for e in dd.list():
                if not (e.endswith(".gladndfbin") or e.endswith(".ndfbin")):
                    continue
                raw = dd.get(e)
                try:
                    n = ndfbin.read(raw)
                except Exception:
                    continue
                imp = n.import_ordinal_paths()
                if not imp:
                    continue
                if sorted(imp) != list(range(len(imp))):
                    out.append(Finding(ERROR, "imports",
                                       "%s has non-dense import ordinals after apply" % os.path.basename(e),
                                       "the applier should densify — investigate the ref edits", migratable=True))


# ── entry points ───────────────────────────────────────────────────────────────

def validate(rmod: dict, *, game_root: Optional[str] = None, target_build=None,
             rmod_path: Optional[str] = None) -> Report:
    findings: List[Finding] = []
    _check_version(rmod, target_build, findings)
    scen, eff = _payload(rmod)
    rep = Report(findings=findings, is_operation=bool(scen and eff))
    if eff:
        _check_effetmap(eff, findings)
    if scen and eff:
        _check_scenario(scen, eff, findings)
    if game_root:
        _check_imports(rmod_path, game_root, target_build, findings)
    return rep


def validate_rmod(path: str, game_root: Optional[str] = None, target_build=None) -> Report:
    with open(path, encoding="utf-8-sig") as fh:
        rmod = json.load(fh)
    return validate(rmod, game_root=game_root, target_build=target_build, rmod_path=path)


# ── CLI / batch report ─────────────────────────────────────────────────────────

_SEV_ORDER = {ERROR: 0, WARN: 1, INFO: 2}


def format_report(name: str, rep: Report) -> str:
    lines = ["%-44s %s" % (name, rep.summary())]
    for f in sorted(rep.findings, key=lambda x: (_SEV_ORDER[x.severity], x.category)):
        if f.severity == INFO:
            continue
        tag = "[%s/%s]" % (f.severity, f.category)
        lines.append("    %-16s %s" % (tag, f.message))
        if f.fix and f.severity == ERROR:
            lines.append("    %-16s   fix: %s%s" % ("", f.fix, "" if f.migratable else "  (NOT auto-migratable)"))
    return "\n".join(lines)


def main(argv=None):
    import argparse, glob, os, logging
    logging.getLogger("ruse_mod_engine.xyz_compile").setLevel(logging.ERROR)  # quiet the size-header notices
    ap = argparse.ArgumentParser(description="Validate rmod(s) for readiness on a target public build.")
    ap.add_argument("path", help="an .rmod file or a folder of them")
    ap.add_argument("--game-root", default=None, help="clean install for the target build (enables apply-time checks)")
    ap.add_argument("--build", default=None, help="target build id (e.g. 24003166)")
    ap.add_argument("--errors-only", action="store_true", help="only print rmods with ERROR findings")
    args = ap.parse_args(argv)

    paths = ([args.path] if args.path.endswith(".rmod")
             else sorted(glob.glob(os.path.join(args.path, "*.rmod"))))
    ready = hand = 0
    for p in paths:
        try:
            rep = validate_rmod(p, game_root=args.game_root, target_build=args.build)
        except Exception as e:  # noqa: BLE001
            print("%-44s COULD NOT VALIDATE: %s" % (os.path.basename(p), str(e)[:80]))
            continue
        if rep.ok:
            ready += 1
        else:
            hand += 1
        if args.errors_only and rep.ok:
            continue
        print(format_report(os.path.basename(p), rep))
    print("\n%d ready, %d need attention (of %d)" % (ready, hand, len(paths)))
    return 0 if hand == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())

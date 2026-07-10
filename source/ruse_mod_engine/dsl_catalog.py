"""DSL catalog for the Mission Logic editor (operations authoring).

Loads ruse_mod_engine/data/dsl_catalog.json (auto-extracted from the RE artifacts by
archive/ai_unit_tests/claude/build_dsl_catalog.py: 1,133 DSL classes with param names/types/defaults, enum
domains, usage counts, inferred kind/category) and DEEP-MERGES the hand-written
ruse_mod_engine/data/dsl_catalog_overlay.json on top. The overlay supplies friendly labels/help, refined
param types (block_ref/tag/camp_ref...), COMPLETE enum domains (usage-mining is incomplete), and a
`curated` flag for the high-value blocks. Auto extraction stays the base of truth for structure; the
overlay only enriches — so every one of the 1,133 classes is usable via the generic "advanced block" form
even before it is curated.
"""
import json
import os

from ._resources import data_dir
_BASE = str(data_dir() / "dsl_catalog.json")
_OVERLAY = str(data_dir() / "dsl_catalog_overlay.json")
_cache = None


def _merge(base, overlay):
    """Deep-merge the overlay into the base catalog (in place on a copy of base)."""
    classes = base.get("classes", {})
    for name, orec in (overlay.get("classes") or {}).items():
        rec = classes.get(name)
        if rec is None:
            rec = {"module": "", "kind": orec.get("kind", "other"), "category": "Actions (other)",
                   "label": name, "used_count": 0, "example_scenarios": [], "params": [], "source": "overlay"}
            classes[name] = rec
        # class-level fields the overlay may set
        for f in ("label", "category", "kind", "help", "path"):
            if f in orec:
                rec[f] = orec[f]
        rec["curated"] = True
        # some auto-extracted param lists are the wrong (base-class) ones; replace_params lets the overlay
        # define the real user-facing param set from scratch (ordered by param_order or insertion).
        if orec.get("replace_params"):
            rec["params"] = []
        # per-param overrides (overlay params keyed by NAME); merge into the ordered base list
        pov = orec.get("params") or {}
        by_name = {p["name"]: p for p in rec.get("params", [])}
        for pname, pfields in pov.items():
            tgt = by_name.get(pname)
            if tgt is None:        # overlay introduces a param the auto-extract missed
                tgt = {"name": pname, "type": "object", "enum_domain": None, "default": None,
                       "required": False, "help": ""}
                rec.setdefault("params", []).append(tgt); by_name[pname] = tgt
            tgt.update(pfields)
        # optional explicit param ORDER from the overlay (so curated forms read top-to-bottom sensibly)
        order = orec.get("param_order")
        if order:
            ordered = [by_name[n] for n in order if n in by_name]
            ordered += [p for p in rec["params"] if p["name"] not in order]
            rec["params"] = ordered
    # enums: overlay completes/overrides
    enums = base.setdefault("enums", {})
    for dom, members in (overlay.get("enums") or {}).items():
        enums[dom] = {"members": list(members), "complete": True}
    return base


def load():
    global _cache
    if _cache is None:
        try:
            with open(_BASE, encoding="utf-8") as f:
                base = json.load(f)
        except Exception:
            base = {"classes": {}, "enums": {}}
        try:
            with open(_OVERLAY, encoding="utf-8") as f:
                overlay = json.load(f)
            base = _merge(base, overlay)
        except FileNotFoundError:
            pass
        except Exception:
            pass
        _cache = base
    return _cache


def info_for(name):
    return load().get("classes", {}).get(name)


def params_for(name):
    """Resolved param specs (ordered) for form generation, or [] if unknown."""
    rec = info_for(name)
    return rec.get("params", []) if rec else []


def is_curated(name):
    rec = info_for(name)
    return bool(rec and rec.get("curated"))


def by_kind(kind):
    """[(name, rec)] of a given kind ('action'|'condition'|'objective'|'ai'|'control'|'camp'|'variable'|
    'operator'|'other'), curated first then by usage desc then name."""
    out = [(n, r) for n, r in load().get("classes", {}).items() if r.get("kind") == kind]
    out.sort(key=lambda kv: (not kv[1].get("curated"), -(kv[1].get("used_count") or 0), kv[0]))
    return out


def by_category(curated_only=False):
    """{category: [(name, rec)]} for palette grouping; curated first within each category."""
    cats = {}
    for n, r in load().get("classes", {}).items():
        if curated_only and not r.get("curated"):
            continue
        cats.setdefault(r.get("category", "Actions (other)"), []).append((n, r))
    for lst in cats.values():
        lst.sort(key=lambda kv: (not kv[1].get("curated"), -(kv[1].get("used_count") or 0), kv[0]))
    return cats


def search(kind=None, category=None, query="", curated_only=False, limit=None):
    q = (query or "").lower()
    out = []
    for n, r in load().get("classes", {}).items():
        if kind and r.get("kind") != kind:
            continue
        if category and r.get("category") != category:
            continue
        if curated_only and not r.get("curated"):
            continue
        if q and q not in n.lower() and q not in (r.get("label", "")).lower():
            continue
        out.append((n, r))
    out.sort(key=lambda kv: (not kv[1].get("curated"), -(kv[1].get("used_count") or 0), kv[0]))
    return out[:limit] if limit else out


def enum_members(domain):
    """Members for an enum domain (overlay-completed where curated), or [] if unknown."""
    e = load().get("enums", {}).get(domain)
    return list(e.get("members", [])) if e else []


def enum_is_complete(domain):
    e = load().get("enums", {}).get(domain)
    return bool(e and e.get("complete"))


def reset_cache():
    """Test helper — drop the cached merge so a rebuilt catalog/overlay is re-read."""
    global _cache
    _cache = None

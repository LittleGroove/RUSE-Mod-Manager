"""Recompile-based editing of RUSE mission scripts (effetmap.xyz) — THE GOLDEN PATH.

Every script edit goes through: decompile (uncompyle6) -> edit the decompiled SOURCE -> recompile with the
game's OWN Python 2.5.1 -> marshal -> XYZ0 pack. In-place constant / raw-byte patching is DEPRECATED (it
proved unreliable in-game: shared/interned constants alias, size changes break marshal offsets). The recompile
rebuilds the whole code object consistently, so it's correct for tunable VALUES and for STRUCTURE alike.

Proven (2026-06-27): 2.5.1's marshal is engine-accepted (re-marshalled effetmap played in-game) and
decompile->recompile is semantically faithful (`decompile(orig)==decompile(recompile)` line-for-line). See
docs/operation_editor/05-editing-a-script.md.

API:
  decompile_xyz / recompile_source_to_xyz / edit_xyz   -- the core round-trip
  parse_objectives                                      -- read DescriptorGereObjectif* + their tunable values
  set_kwarg + set_score/set_condition_value/set_label_hash -- rewrite one literal in the source
"""
import os
import re
import subprocess

from . import xyz_compile

_ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
_PY251_DIR = os.path.join(_ENGINE_DIR, "python251")


# ── the game's own Python 2.5.1 (engine-loadable emit) ────────────────────────────
def _py251_interpreter():
    p = os.path.join(_PY251_DIR, "python.exe")
    return p if os.path.isfile(p) else None


def _worker_path():
    return os.path.join(_PY251_DIR, "compile_worker.py")


def have_compiler():
    """True when bundled Python 2.5.1 is present (recompile works). See python251/README.md."""
    return _py251_interpreter() is not None and os.path.isfile(_worker_path())


def _launch_exe():
    """The interpreter to spawn — a NON-"python"-named copy of python.exe when we can make one.

    A parent debugger (debugpy/pydevd, e.g. VS Code) auto-attaches to child Python processes by injecting
    `pydevd` into them — but pydevd is modern-Python source that won't parse under 2.5.1 ('with' isn't yet a
    keyword) so the worker dies before compiling. pydevd's subprocess monkey-patch only injects into
    executables whose basename starts with "python", so we run a renamed copy to slip past it. Falls back to
    python.exe if the copy can't be made (then -E/-S + the cleaned env are the remaining defenses)."""
    base = _py251_interpreter()
    if base is None:
        return None
    alias = os.path.join(_PY251_DIR, "ruse_compile251.exe")
    try:
        import shutil
        if not os.path.isfile(alias) or os.path.getmtime(alias) < os.path.getmtime(base):
            shutil.copy2(base, alias)
        return alias
    except Exception:
        return base


def _clean_env():
    """Subprocess env with a parent debugger's hooks stripped (the other half of the pydevd defense): drop
    PYTHONPATH / sitecustomize entry points and any PYDEV*/DEBUGPY* vars so nothing bootstraps pydevd."""
    env = dict(os.environ)
    for k in list(env):
        ku = k.upper()
        if ku in ("PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "PYTHONINSPECT", "PYTHONCASEOK") \
                or "PYDEV" in ku or "DEBUGPY" in ku:
            env.pop(k, None)
    return env


def compile_source(source_text, co_filename="effetmap.py", timeout=60):
    """Python SOURCE -> raw 2.5.1 marshal bytes via the bundled game-version interpreter.
    Raises FileNotFoundError if 2.5.1 isn't installed, RuntimeError(stderr) on a compile error."""
    exe = _launch_exe()
    if exe is None:
        raise FileNotFoundError(
            "ruse_mod_engine/python251/python.exe not found - see ruse_mod_engine/python251/README.md")
    # -E ignore PYTHON* env vars, -S skip site.py — both block a parent debugger's pydevd auto-attach,
    # together with the renamed exe (_launch_exe) and the cleaned env.
    proc = subprocess.run([exe, "-E", "-S", _worker_path(), co_filename],
                          input=source_text.encode("utf-8", "replace"),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=_clean_env())
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip() or "py2.5.1 worker failed")
    return proc.stdout


# ── decompile (recompilable source) ───────────────────────────────────────────────
def _strip_module_return(src):
    """uncompyle6 emits a module-level (indent-0) `return`/`return None` for the module RETURN_VALUE, which is
    a SyntaxError ('return outside function'). Drop it; the compiler re-adds RETURN None on its own."""
    out = []
    for ln in src.split("\n"):
        if ln.strip() in ("return", "return None") and ln[:1] not in (" ", "\t"):
            continue
        out.append(ln)
    return "\n".join(out)


import re as _re

# uncompyle6 (running on Python 3) renders some Python-2 string constants with syntax the game's
# Python 2.5.1 `compile()` rejects.  Two artifacts seen in tool-authored (ProLution) effetmaps:
_BYTES_PREFIX_RE = _re.compile(r"(?<![A-Za-z0-9_])([bB])(['\"])")


def _strip_byte_prefixes(src):
    """Drop the `b'...'`/`B"..."` byte-literal PREFIX (a Python 2.6+/3 form) — in Python 2.5 `str` already
    IS bytes, so `b'x'` is a SyntaxError but `'x'` is the identical value.  Only strips the prefix letter
    (never touches content), and only when not part of an identifier (negative lookbehind)."""
    return _BYTES_PREFIX_RE.sub(r"\2", src)


def _fix_unescaped_quotes(src):
    """Re-escape unescaped inner quotes in single-line string literals (e.g. uncompyle6 emitting
    `u'Dementor's Kiss'`, where the apostrophe closes the string early → SyntaxError).

    Scans each line as a char stream.  At a string open (optional u/r/b prefix + quote), a later quote of
    the same kind CLOSES the string only if what follows is a statement boundary (whitespace / , ) ] } : + %
    ; = and friends / end-of-line); a quote followed by an identifier/`(`/`.` char is CONTENT and gets
    backslash-escaped.  Machine-generated one-line kwarg values only — leaves well-formed source untouched."""
    BOUNDARY = set(" \t,)]}:;+%=<>/*|&\r\n")
    out_lines = []
    for line in src.split("\n"):
        i, n = 0, len(line)
        out = []
        while i < n:
            c = line[i]
            if c in "'\"":
                q = c
                out.append(c); i += 1
                # walk the string body
                while i < n:
                    ch = line[i]
                    if ch == "\\":            # keep existing escapes verbatim (2 chars)
                        out.append(line[i:i + 2]); i += 2; continue
                    if ch == q:
                        nxt = line[i + 1] if i + 1 < n else "\n"
                        if nxt in BOUNDARY or i + 1 == n:
                            out.append(ch); i += 1; break      # real closer
                        out.append("\\" + ch); i += 1; continue  # inner quote → escape
                    out.append(ch); i += 1
                continue
            if c == "#":                      # rest of line is a comment; copy verbatim
                out.append(line[i:]); break
            out.append(c); i += 1
        out_lines.append("".join(out))
    return "\n".join(out_lines)


# A malformed \x / \u / \U escape (too few hex digits) is a hard tokenizer error in Python 2.5. xdis
# mangles some Python-2 non-ASCII string CONSTANTS during unmarshal (e.g. rendering byte 0xe9 as the text
# "0xe9" and inserting a bad "\uf…"), so the decompiled inline literal can carry an invalid escape. Make it
# LITERAL (double the active backslash) so the script still recompiles. NOTE: only inline non-ASCII text is
# affected — LocHash-int text (operations/objectives) is untouched — and such text is xdis-approximate.
_BAD_X = _re.compile(r"(?<!\\)\\x(?![0-9a-fA-F]{2})")
_BAD_U = _re.compile(r"(?<!\\)\\u(?![0-9a-fA-F]{4})")
_BAD_U8 = _re.compile(r"(?<!\\)\\U(?![0-9a-fA-F]{8})")


def _fix_malformed_escapes(src):
    src = _BAD_X.sub(r"\\\\x", src)
    src = _BAD_U.sub(r"\\\\u", src)
    src = _BAD_U8.sub(r"\\\\U", src)
    return src


def _sanitize_decompiled(src):
    """Apply all recompile-safety fixups to freshly decompiled source (idempotent)."""
    return _fix_malformed_escapes(_fix_unescaped_quotes(_strip_byte_prefixes(_strip_module_return(src))))


_DECOMP_CACHE = {}


def decompile_xyz(xyz_bytes):
    """effetmap .xyz -> recompilable Python source (module-return artifact stripped). Cached by content hash
    (decompile is ~30s) so multiple views of the same script share one decompile; the key changes after a
    recompile, so edits invalidate naturally."""
    import hashlib
    key = hashlib.md5(xyz_bytes).hexdigest()
    cached = _DECOMP_CACHE.get(key)
    if cached is not None:
        return cached
    from .script_edit import Script
    src = _sanitize_decompiled(Script.load(xyz_bytes).decompile())
    if len(_DECOMP_CACHE) > 8:
        _DECOMP_CACHE.clear()
    _DECOMP_CACHE[key] = src
    return src


def original_co_filename(xyz_bytes):
    try:
        code = xyz_compile.load_code(xyz_compile.unpack(xyz_bytes)["marshal"])
        fn = getattr(code, "co_filename", None)
        if fn:
            return fn
    except Exception:
        pass
    return "effetmap.py"


def recompile_source_to_xyz(source_text, xyz_for_meta=None, co_filename=None, hash16=None):
    """Source -> ready-to-write effetmap .xyz (2.5.1 recompile + XYZ0 pack). Preserves the original
    co_filename + XYZ0 hash when xyz_for_meta is supplied."""
    if co_filename is None:
        co_filename = original_co_filename(xyz_for_meta) if xyz_for_meta else "effetmap.py"
    if hash16 is None and xyz_for_meta is not None:
        hash16 = xyz_compile.unpack(xyz_for_meta)["hash"]
    marshal_bytes = compile_source(source_text, co_filename)
    return xyz_compile.pack(marshal_bytes, hash16 if hash16 is not None else b"\x00" * 16)


def edit_xyz(xyz_bytes, edit_fn):
    """Decompile, run edit_fn(source)->source, recompile. The one-call golden-path edit."""
    return recompile_source_to_xyz(edit_fn(decompile_xyz(xyz_bytes)), xyz_for_meta=xyz_bytes)


# ── source parsing: objectives + their tunable values ─────────────────────────────
def _balanced(s, i):
    """i points at '('; return index of the matching ')'."""
    d = 0
    while i < len(s):
        c = s[i]
        if c == "(":
            d += 1
        elif c == ")":
            d -= 1
            if d == 0:
                return i
        i += 1
    return len(s) - 1


# Any `IR_N = some.dotted.ClassName(...)` call (descriptors, conditions, operators, variables, effects).
_ASSIGN = re.compile(r"(?m)^(IR_\d+)\s*=\s*([\w.]+)\(")
_VALUE = r"(-?\d+L|-?\d+\.\d+|-?\d+|True|False|None|IR_\d+|u?'(?:[^'\\]|\\.)*')"
_SCALAR = re.compile(r"(\w+)\s*=\s*" + _VALUE)


def ir_assignments(src):
    """{ir_name: {'type','body':'(...)','start','end','body_start'}} for every `IR_N = path.Class(...)` call.
    'type' is the bare class name (last dotted component), e.g. ConditionVariableInteger, VariableInteger,
    DescriptorGereObjectifAvecLabel, OperatorIntegerMoreOrEqual."""
    out = {}
    for m in _ASSIGN.finditer(src):
        open_i = m.end() - 1
        close_i = _balanced(src, open_i)
        out[m.group(1)] = {"type": m.group(2).rsplit(".", 1)[-1], "body": src[open_i:close_i + 1],
                           "start": m.start(), "end": close_i + 1, "body_start": open_i}
    return out


def scalar_kwargs(body):
    """{kwarg: value_str} for simple scalar / IR-ref kwargs in a Class(...) body (nested lists skipped)."""
    return {m.group(1): m.group(2) for m in _SCALAR.finditer(body)}


_LISTVARS = re.compile(r"List(?:Variables|Timers)(?:ForFoldedText)?\s*=\s*\[([^\]]*)\]")


def text_wired_vars(src):
    """The set of IR_N that feed a DrawLabel's ListVariables/ListTimers — i.e. whose value is formatted INTO
    the objective's on-screen text (the '%d'/'%s' the player sees). Editing such a variable/timer updates the
    displayed value as well as the trigger, since it's the same object."""
    out = set()
    for m in _LISTVARS.finditer(src):
        out.update(re.findall(r"IR_\d+", m.group(1)))
    return out


def _resolve_threshold(irs, cond_ir):
    """Walk Condition -> Operator -> (Variable|Value) to find the trigger threshold. Returns
    (condition_type, comparison_op, threshold_str, threshold_ir, threshold_field, kind) where kind is:
      'target'  — operator compares against a VariableInteger (a real tunable quantity: kill/$ N). Edit it.
      'literal' — operator compares against its own non-zero Value literal. Editable, a direct comparison.
      'flag'    — operator compares against 0 with no variable: a boolean 'did it happen' (the counter is a
                  flag set to 1). NOT a tunable quantity — editing it breaks the objective. Don't expose numeric.
    threshold_ir/field say WHICH assignment to edit."""
    cond = irs.get(cond_ir)
    if not cond:
        return (None, None, None, None, None, None)
    ckw = scalar_kwargs(cond["body"])
    # timer condition: the tunable is the referenced VariableTimer's Duree (the time limit, in seconds)
    if cond["type"] == "ConditionTimer":
        tv = irs.get(ckw.get("Variable"))
        if tv and tv["type"] == "VariableTimer":
            return (cond["type"], ckw.get("Operator") and irs.get(ckw["Operator"], {}).get("type"),
                    scalar_kwargs(tv["body"]).get("Duree"), ckw.get("Variable"), "Duree", "timer")
    op = irs.get(ckw.get("Operator"))
    if not op:
        return (cond["type"], None, None, None, None, None)
    opkw = scalar_kwargs(op["body"])
    var = irs.get(opkw.get("Variable"))
    if var and var["type"] == "VariableInteger":
        return (cond["type"], op["type"], scalar_kwargs(var["body"]).get("Value"),
                opkw.get("Variable"), "Value", "target")
    val = opkw.get("Value")
    kind = "flag" if val in ("0", "-0") else "literal"
    return (cond["type"], op["type"], val, ckw.get("Operator"), "Value", kind)


def label_info(irs, label_ir):
    """For a DrawLabel IR: {text_hash, folded_hash, list_vars, folded_vars} — the on-screen `Text` + folded/menu
    `FoldedText` .dic LocHashes, and the variables whose runtime values fill each string's %d (ListVariables ->
    Text, ListVariablesForFoldedText -> FoldedText). {} if not a known label."""
    info = irs.get(label_ir)
    if not info:
        return {}
    body = info["body"]
    kw = scalar_kwargs(body)

    def lst(name):
        # `ListVariables=` won't match `ListVariablesForFoldedText=` (the '=' must follow immediately)
        m = re.search(name + r"\s*=\s*\[([^\]]*)\]", body)
        return re.findall(r"IR_\d+", m.group(1)) if m else []

    return {"text_hash": kw.get("Text"), "folded_hash": kw.get("FoldedText"),
            "list_vars": lst("ListVariables"), "folded_vars": lst("ListVariablesForFoldedText"),
            "timer_vars": lst("ListTimers"), "folded_timers": lst("ListTimersForFoldedText")}


def parse_objectives(src):
    """List of objectives with their tunable values, resolving the Condition/Operator/Variable + Label chains.
    Each dict: {ir, score, condition_ir, condition_type, comparison, threshold, threshold_ir, threshold_field,
    threshold_kind, threshold_in_text, label_ir, label_hash, actions_reussi_ir, actions_echec_ir, show_on_map}.
    threshold_kind classifies the trigger value (target/literal/flag — see _resolve_threshold);
    threshold_in_text is True when editing it also changes the on-screen number. threshold_ir/threshold_field
    point at the exact assignment + kwarg to edit (via set_kwarg)."""
    irs = ir_assignments(src)
    in_text = text_wired_vars(src)
    objs = []
    for ir, info in irs.items():
        if "GereObjectif" not in info["type"]:
            continue
        kw = scalar_kwargs(info["body"])
        ctype, comp, thr, thr_ir, thr_field, kind = _resolve_threshold(irs, kw.get("Condition"))
        li = label_info(irs, kw.get("Label"))
        objs.append({
            "ir": ir,
            "score": kw.get("BonusScoreMission"),
            "condition_ir": kw.get("Condition"),
            "condition_type": ctype,
            "comparison": comp,
            "threshold": thr,
            "threshold_ir": thr_ir,
            "threshold_field": thr_field,
            "threshold_kind": kind,
            "threshold_in_text": bool(thr_ir and thr_ir in in_text),
            "label_ir": kw.get("Label"),
            "label_hash": li.get("text_hash"),          # the on-screen Text LocHash
            "folded_hash": li.get("folded_hash"),        # the menu/folded FoldedText LocHash
            "list_vars": li.get("list_vars", []),        # vars feeding Text's %d
            "folded_vars": li.get("folded_vars", []),    # vars feeding FoldedText's %d
            "timer_vars": li.get("timer_vars", []),      # timers feeding Text's %s
            "folded_timers": li.get("folded_timers", []),
            "actions_reussi_ir": kw.get("ActionsReussi"),
            "actions_echec_ir": kw.get("ActionsEchec"),
            "rewards": parse_rewards(irs, kw.get("ActionsReussi")),       # on success
            "fail_rewards": parse_rewards(irs, kw.get("ActionsEchec")),   # on failure
            "show_on_map": kw.get("ShowOnMap"),
        })
    return objs


def set_kwarg(src, ir_name, kwarg, new_value_str):
    """Rewrite `kwarg=<old>` inside the `IR_name = Descriptor(...)` assignment to new_value_str. Returns new
    src. Raises KeyError if the assignment or kwarg isn't found."""
    info = ir_assignments(src).get(ir_name)
    if info is None:
        raise KeyError("no assignment %s" % ir_name)
    pat = re.compile(r"(\b" + re.escape(kwarg) + r"\s*=\s*)" + _VALUE)
    mm = pat.search(info["body"])
    if not mm:
        raise KeyError("kwarg %s not found in %s" % (kwarg, ir_name))
    abs_start = info["body_start"] + mm.start(2)
    abs_end = info["body_start"] + mm.end(2)
    return src[:abs_start] + new_value_str + src[abs_end:]


# ── convenience setters (format the literal the game expects) ──────────────────────
def set_score(src, objective_ir, value):
    """BonusScoreMission (objective score points)."""
    return set_kwarg(src, objective_ir, "BonusScoreMission", str(int(value)))


def set_threshold(src, objective, value):
    """The trigger threshold of an objective (kill/hold N, $ target, timer). Pass a parse_objectives() dict;
    edits its threshold_ir/threshold_field (the operator's VariableInteger or the operator literal)."""
    if not objective.get("threshold_ir"):
        raise KeyError("objective %s has no resolvable threshold" % objective.get("ir"))
    return set_kwarg(src, objective["threshold_ir"], objective["threshold_field"], str(int(value)))


def set_label_hash(src, label_ir, lochash):
    """Point a DrawLabel at a different LocHash (text key). LocHashes are emitted as Python longs (N L)."""
    return set_kwarg(src, label_ir, "Text", str(int(lochash)) + "L")


# ── timers / mission time (VariableTimer.Duree, GameRulesChallenge.GameTime — seconds) ─────────────
def secs_to_mmss(value):
    """Seconds -> 'M:SS' for display (916 -> '15:16'). Non-numeric passes through."""
    try:
        total = int(round(float(value)))
    except Exception:
        return str(value)
    m, s = divmod(total, 60)
    return "%d:%02d" % (m, s)


def mmss_to_secs(text):
    """'M:SS' or a bare seconds string -> int seconds. Raises ValueError on garbage."""
    text = str(text).strip()
    if ":" in text:
        m, s = text.split(":", 1)
        return int(m) * 60 + int(s)
    return int(text)


def parse_timers(src):
    """Mission time controls. Returns {'game': {ir, field, seconds} | None,
    'timers': [{ir, field='Duree', seconds, shown, gates}]} — the countdown `GameTime` (GameRulesChallenge) plus
    every `VariableTimer` (its `Duree` in seconds), flagging whether each timer is shown on-screen (in a label's
    `ListTimers`, i.e. fills a `%s`) and/or drives a `ConditionTimer`."""
    irs = ir_assignments(src)
    shown = set()
    for m in re.finditer(r"ListTimers(?:ForFoldedText)?\s*=\s*\[([^\]]*)\]", src):
        shown.update(re.findall(r"IR_\d+", m.group(1)))
    gated = set()
    for info in irs.values():
        if info["type"] == "ConditionTimer":
            v = scalar_kwargs(info["body"]).get("Variable")
            if v:
                gated.add(v)
    game = None
    timers = []
    for ir, info in irs.items():
        if info["type"] == "VariableTimer":
            timers.append({"ir": ir, "field": "Duree", "seconds": scalar_kwargs(info["body"]).get("Duree"),
                           "shown": ir in shown, "gates": ir in gated})
        elif "GameRules" in info["type"] and game is None:
            gt = scalar_kwargs(info["body"]).get("GameTime")
            if gt is not None:
                game = {"ir": ir, "field": "GameTime", "seconds": gt}
    return {"game": game, "timers": timers}


def set_seconds(src, ir, field, seconds):
    """Set a time field (GameTime / VariableTimer.Duree) to an integer number of seconds."""
    return set_kwarg(src, ir, field, str(int(seconds)))


# ── match ruleset (GameRules) ──────────────────────────────────────────────────────
_ENUM_VAL = r"[A-Za-z_][\w.]*\.[A-Za-z_]\w*"   # e.g. _enum_for_game_play.TGameTimerMode.Countdown
_ENUM_KW = re.compile(r"(\w+)\s*=\s*(" + _ENUM_VAL + r")")


def parse_gamerules(src):
    """The mission's GameRules descriptor (match ruleset), for the lifecycle editor. Returns
    {ir, type, scalars:{field:value}, enums:{field:dotted_value}} or None. scalars = int/bool/float fields
    (GameTime, MaxScore, MinScore, UseScore, ConstructionNecessiteHQ, DisableCapture, …); enums = the dotted
    enum fields (GameTimeMode, Fow, PopCapLimit, GameType)."""
    for ir, info in ir_assignments(src).items():
        if "GameRules" in info["type"]:
            return {"ir": ir, "type": info["type"], "scalars": scalar_kwargs(info["body"]),
                    "enums": {m.group(1): m.group(2) for m in _ENUM_KW.finditer(info["body"])}}
    return None


def set_enum_field(src, ir, field, dotted_value):
    """Set an enum field (e.g. GameTimeMode) to a dotted enum value (e.g.
    '_enum_for_game_play.TGameTimerMode.Clock'). Rewrites `field=<dotted>` in the IR's assignment."""
    info = ir_assignments(src).get(ir)
    if info is None:
        raise KeyError("no assignment %s" % ir)
    pat = re.compile(r"(\b" + re.escape(field) + r"\s*=\s*)(" + _ENUM_VAL + r")")
    mm = pat.search(info["body"])
    if not mm:
        raise KeyError("enum field %s not found in %s" % (field, ir))
    abs_start = info["body_start"] + mm.start(2)
    abs_end = info["body_start"] + mm.end(2)
    return src[:abs_start] + dotted_value + src[abs_end:]


# ── rewards / consequences (ActionsReussi / ActionsEchec) ──────────────────────────
# leaf consequence type -> (category, friendly label). Category drives editor grouping.
_REWARD_CAT = {
    "DescriptorDonneRessourceACamp": ("cash", "give money"),
    "DescriptorSetRessourceCamp": ("cash", "set money"),
    "DescriptorCreateUnit": ("units", "create units"),
    "DescriptorProduitUnits": ("units", "produce units"),
    "DescriptorParachutage": ("units", "paradrop"),
    "DescriptorSetScoreCamp": ("score", "set score"),
    "DescriptorAddScoreCamp": ("score", "add score"),
    "DescriptorDeclencheVictoireChallenge": ("win", "VICTORY"),
    "DescriptorDeclencheVictoire": ("win", "VICTORY"),
    "DescriptorDeclencheDefaite": ("lose", "DEFEAT"),
    "DescriptorDonneTechno": ("tech", "unlock tech"),
    "DescriptorDebloqueTechno": ("tech", "unlock tech"),
    "DescriptorBloqueTechno": ("tech", "lock tech"),
    "DescriptorSetVariableInteger": ("state", "set variable"),
    "DescriptorIncrementeVariableInteger": ("state", "increment variable"),
    "DescriptorIAAll": ("ai", "AI directive"),
    "DescriptorDefendZone": ("ai", "defend zone"),
    "DescriptorAfficheHint": ("ui", "hint"),
}
# the editable numeric tunable, by priority, inside a consequence
_VALUE_FIELDS = ("Quantite", "NbUnit", "NbPattern", "Value", "Niveau")
_NUM = re.compile(r"^-?\d+(?:\.\d+)?L?$")


def _subactions(body):
    m = re.search(r"SubActions\s*=\s*\[([^\]]*)\]", body)
    return re.findall(r"IR_\d+", m.group(1)) if m else []


def resolve_actions(irs, action_ir, _seen=None):
    """Flatten an ActionsReussi/ActionsEchec target — which may be a Sequential/Simultaneous of `SubActions`
    (possibly nested) or a single descriptor — into the flat list of LEAF consequence IRs (in order)."""
    if _seen is None:
        _seen = set()
    if not action_ir or action_ir in _seen:
        return []
    _seen.add(action_ir)
    info = irs.get(action_ir)
    if not info:
        return []
    subs = _subactions(info["body"])
    if subs:
        out = []
        for s in subs:
            out.extend(resolve_actions(irs, s, _seen))
        return out
    return [action_ir]


def parse_rewards(irs, action_ir):
    """List of consequences for an ActionsReussi/Echec, each:
    {ir, type, cat, label, field, value} — `cat` in cash/units/score/win/lose/tech/state/ai/ui/objective/other;
    `field`/`value` = the editable numeric tunable (money Quantite, unit NbUnit/NbPattern, score Value) or None."""
    out = []
    for leaf in resolve_actions(irs, action_ir):
        info = irs.get(leaf)
        typ = info["type"]
        if "GereObjectif" in typ:
            cat, label = "objective", "activates objective"
        else:
            cat, label = _REWARD_CAT.get(typ, ("other", typ.replace("Descriptor", "")))
        kw = scalar_kwargs(info["body"])
        field = next((f for f in _VALUE_FIELDS if _NUM.match(kw.get(f) or "")), None)
        out.append({"ir": leaf, "type": typ, "cat": cat, "label": label,
                    "field": field, "value": kw.get(field) if field else None})
    return out


# ── flow / timeline (the EffetMap control-flow spine) ──────────────────────────────
_FLOW_CTRL = ("Sequential", "Simultane", "Competition", "IfThenElse", "OnContrainte")
# action type (no 'Descriptor' prefix) -> (category, friendly verb) for the storyboard
_ACTION_LABEL = {
    "StartTimer": ("time", "start timer"), "StopTimer": ("time", "stop timer"),
    "SetRessourceCamp": ("economy", "set economy"), "DonneRessourceACamp": ("economy", "give money"),
    "GetRessourceCamp": ("economy", "read economy"),
    "BloqueTechno": ("tech", "lock tech"), "DebloqueTechno": ("tech", "unlock tech"),
    "BloqueAllTechno": ("tech", "lock all tech"), "DonneTechno": ("tech", "give tech"),
    "SetScoreCamp": ("score", "set score"), "AddScoreCamp": ("score", "add score"),
    "GetScoreCamp": ("score", "read score"),
    "CreateUnit": ("units", "spawn units"), "ProduitUnits": ("units", "produce units"),
    "Parachutage": ("units", "paradrop"), "Move": ("ai", "move"), "MoveFollowUnit": ("ai", "move"),
    "Attack": ("ai", "attack"), "DefendZone": ("ai", "defend zone"), "IAAll": ("ai", "AI directive"),
    "ChangeCamp": ("ai", "change side"),
    "GereObjectifAvecLabel": ("objective", "objective"), "GereObjectif": ("objective", "objective"),
    "DeclencheVictoireChallenge": ("win", "VICTORY"), "DeclencheVictoire": ("win", "VICTORY"),
    "DeclencheDefaite": ("lose", "DEFEAT"),
    "PlayMusic": ("av", "music"), "StopMusic": ("av", "stop music"), "InitSoundAndMusic": ("av", "init audio"),
    "StartCameraPath": ("av", "camera"), "PlayDialogSimplifie": ("av", "dialogue"),
    "CutsceneDialog": ("av", "cutscene"), "LoadingDone": ("av", "loading done"),
    "HudElementsVisibility": ("ui", "HUD"), "DrawLabel": ("ui", "label"), "AfficheHint": ("ui", "hint"),
    "IncrementeVariableInteger": ("state", "increment var"), "SetVariableInteger": ("state", "set var"),
    "AjoutVariableInteger": ("state", "add to var"), "SoustraitVariableInteger": ("state", "subtract var"),
    "AddCampHqToUnitGroup": ("setup", "track HQ"), "AddAllCampUnitsToUnitGroup": ("setup", "track units"),
    "BloqueCarteBluff": ("setup", "lock bluff card"), "BloqueZoneForRuseAndOrder": ("setup", "lock zone"),
    "IfThenElse": ("flow", "if/then"),
}


def flow_root(src):
    """The IR of the operation's flow root — the LaunchEffetMap's `EffetMap` (an ER_ assignment, so read the
    raw source). None if not found."""
    m = re.search(r"\bEffetMap\s*=\s*(IR_\d+)", src)
    return m.group(1) if m else None


def _humanize(typ):
    return typ.replace("Descriptor", "")


def parse_flow(src):
    """The operation's control-flow spine as a tree, for the Timeline/storyboard view. Returns the root node
    (or None). Each node: {ir, type, kind('flow'|'wait'|'action'), label, cat, duree, condition, children[]}.
    `duree` = WaitDuree/AfterWait seconds; `condition` = the IR a WaitCondition waits on; children = SubActions
    (Sequential = ordered/over time, Simultaneous = parallel)."""
    irs = ir_assignments(src)
    root = flow_root(src)
    if not root or root not in irs:
        return None

    def build(ir, seen):
        if ir in seen or ir not in irs:
            return None
        seen = seen | {ir}
        info = irs[ir]
        t = _humanize(info["type"])
        kw = scalar_kwargs(info["body"])
        node = {"ir": ir, "type": t, "duree": None, "condition": None, "children": [],
                "label": None, "cat": None}
        if "WaitDuree" in t or "AfterWait" in t:
            node["kind"] = "wait"; node["duree"] = kw.get("Duree")
        elif "WaitCondition" in t:
            node["kind"] = "wait"; node["condition"] = kw.get("Condition")
            c = irs.get(kw.get("Condition"))
            node["condition_type"] = _humanize(c["type"]) if c else None
        elif any(c in t for c in _FLOW_CTRL):
            node["kind"] = "flow"
            for s in _subactions(info["body"]):
                ch = build(s, seen)
                if ch:
                    node["children"].append(ch)
        else:
            node["kind"] = "action"
            cat, label = _ACTION_LABEL.get(t, ("other", _humanize(t)))
            node["cat"] = cat; node["label"] = label
        return node

    return build(root, set())

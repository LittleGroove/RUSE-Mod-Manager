"""script_emit — the AUTHORING emitter: build NEW mission-logic STRUCTURE into a decompiled effetmap source,
then recompile via the 2.5.1 golden path. Where `script_logic` EDITS existing literals, this ADDS / WIRES nodes
(events, conditions, actions, objectives). See docs/operation_editor/06-09.

Ordering-safe model (Python evaluates the script top→bottom): every new `IR_N = path(...)` assignment is inserted
just BEFORE the EffetMap root assignment, so it can reference any pre-existing node, and the EffetMap + launcher
(defined after it) can reference the new node. The two non-negotiable authoring rules are enforced:
  • every reactive node (Variable*/Condition*/GereObjectif*) MUST be in the launcher's PostInit — else it never
    subscribes to the world and is silently DEAD;
  • every Tag* must be in the launcher's TagList.
`validate()` flags violations + refuses ExecutePython/ConditionPython (RCE).
"""
import re

from . import script_logic as SL

# framework module paths (from the decompiled maps) for the node builders
P_DESC = "leveldesign.descriptor"
P_VAR = "leveldesign.variable"
P_OP = "leveldesign.operator"
P_COND = "leveldesign.condition"
P_CAMPS = "leveldesignsolo.camps"
P_TIMER = "leveldesignsolo.timer"
P_OBJ = "leveldesignsolo.objectif"

_REACTIVE_PREFIX = ("Variable", "Condition", "GereObjectif")   # need PostInit registration
_FORBIDDEN = ("ConditionPython", "ExecutePython")


def _balanced(src, i, op, cl):
    d = 0
    while i < len(src):
        c = src[i]
        if c == op:
            d += 1
        elif c == cl:
            d -= 1
            if d == 0:
                return i
        i += 1
    return len(src) - 1


def _assignment(src, name):
    """(start, paren_open, paren_close_excl) for `name = path(...)` — name may be IR_ or ER_; None if absent."""
    m = re.search(r"(?m)^" + re.escape(name) + r"\s*=\s*[\w.]+\(", src)
    if not m:
        return None
    open_i = m.end() - 1
    return (m.start(), open_i, _balanced(src, open_i, "(", ")") + 1)


def _list_bracket(src, lo, hi, field):
    """(open_idx, close_idx) of `field=[...]` within [lo, hi); None if absent."""
    m = re.compile(r"\b" + re.escape(field) + r"\s*=\s*\[").search(src, lo, hi)
    if not m:
        return None
    ob = m.end() - 1
    return (ob, _balanced(src, ob, "[", "]"))


class ScriptModel:
    """Mutable view over a decompiled effetmap source. Build nodes, wire them, emit() → recompilable source."""

    def __init__(self, src):
        self.src = src
        m = re.search(r"(?m)^((?:ER|IR)_\d+)\s*=\s*[\w.]*LaunchEffetMap\b", src)
        self.launcher = m.group(1) if m else None
        self.effetmap = SL.flow_root(src)
        nums = [int(k[3:]) for k in SL.ir_assignments(src) if k[3:].isdigit()]
        nums += [int(x) for x in re.findall(r"(?m)^ER_(\d+)\s*=", src)]
        self._next = (max(nums) + 1) if nums else 1
        self._added = {}   # ir -> type (the nodes this session created)

    # ---- low-level ----
    def alloc(self):
        ir = "IR_%d" % self._next
        self._next += 1
        return ir

    def add_descriptor(self, path, kwargs):
        """Append a new `IR_N = path(k=v, ...)` (values are already source strings) just before the EffetMap
        root, and return the new IR name. Registers reactive types for PostInit checking."""
        ir = self.alloc()
        kw = ", ".join("%s=%s" % (k, v) for k, v in kwargs.items())
        line = "%s = %s(%s)\n" % (ir, path, kw)
        span = _assignment(self.src, self.effetmap)
        ins = span[0] if span else len(self.src)
        self.src = self.src[:ins] + line + self.src[ins:]
        self._added[ir] = path.rsplit(".", 1)[-1]
        return ir

    def _append_list(self, name, field, item):
        span = _assignment(self.src, name)
        if not span:
            raise KeyError("no assignment %s" % name)
        lb = _list_bracket(self.src, span[1], span[2], field)
        if not lb:
            raise KeyError("%s.%s list not found" % (name, field))
        ob, cb = lb
        inner = self.src[ob + 1:cb]
        if item in re.findall(r"(?:IR|ER|IT)_\d+", inner):
            return False
        sep = "" if inner.strip() == "" else ", "
        self.src = self.src[:cb] + sep + item + self.src[cb:]
        return True

    def ensure_postinit(self, ir):
        return self._append_list(self.launcher, "PostInit", ir) if self.launcher else False

    def ensure_taglist(self, ir):
        return self._append_list(self.launcher, "TagList", ir) if self.launcher else False

    def add_effetmap_branch(self, ir):
        """Add a top-level branch to the EffetMap root (runs in parallel with the existing tracks)."""
        return self._append_list(self.effetmap, "SubActions", ir)

    # ---- node builders (return the new IR) ----
    def var_integer(self, value=0):
        return self.add_descriptor(P_VAR + ".VariableInteger", {"Value": str(int(value))})

    def operator(self, kind, value=0, variable=None):
        kw = {"Value": str(int(value)), "Variable": variable or "None"}
        return self.add_descriptor(P_OP + ".OperatorInteger" + kind, kw)

    def condition_variable_integer(self, operator_ir, variable_ir):
        return self.add_descriptor(P_COND + ".ConditionVariableInteger",
                                   {"Operator": operator_ir, "Variable": variable_ir})

    def wait_duree(self, seconds):
        return self.add_descriptor(P_DESC + ".DescriptorWaitDuree", {"Duree": str(int(seconds))})

    def wait_condition(self, condition_ir):
        return self.add_descriptor(P_DESC + ".DescriptorWaitCondition", {"Condition": condition_ir})

    def sequential(self, subaction_irs, repeat=False):
        lst = "[" + ", ".join(subaction_irs) + "]"
        return self.add_descriptor(P_DESC + ".DescriptorSequential",
                                   {"Repeat": "True" if repeat else "False", "SubActions": lst})

    def donne_ressource(self, camp_ir, quantite):
        return self.add_descriptor(P_CAMPS + ".DescriptorDonneRessourceACamp",
                                   {"Camp": camp_ir, "Quantite": str(int(quantite))})

    def set_variable_integer(self, variable_ir, value):
        return self.add_descriptor(P_DESC + ".DescriptorSetVariableInteger",
                                   {"Variable": variable_ir, "Value": str(int(value))})

    def condition_timer(self, operator_ir, timer_ir):
        return self.add_descriptor(P_TIMER + ".ConditionTimer", {"Operator": operator_ir, "Variable": timer_ir})

    def set_score(self, camp_ir, score):
        return self.add_descriptor(P_CAMPS + ".DescriptorSetScoreCamp",
                                   {"Camp": camp_ir, "Score": str(int(score))})

    def declenche_victoire_challenge(self):
        return self.add_descriptor(P_OBJ + ".DescriptorDeclencheVictoireChallenge", {})

    def declenche_defaite(self):
        return self.add_descriptor(P_OBJ + ".DescriptorDeclencheDefaite", {})

    # ---- high-level ----
    def add_event(self, condition_ir, action_irs, reactive_irs=()):
        """Wire a "when CONDITION, do ACTIONS" branch into the EffetMap as a new parallel track:
        Sequential([WaitCondition(condition_ir), *action_irs]). Registers the condition + any reactive_irs into
        PostInit (so the trigger is live). Returns the branch IR."""
        wc = self.wait_condition(condition_ir)
        branch = self.sequential([wc] + list(action_irs))
        self.add_effetmap_branch(branch)
        for ir in (condition_ir,) + tuple(reactive_irs):
            self.ensure_postinit(ir)
        return branch

    def add_timed_branch(self, seconds, action_irs):
        """Wire an unconditional "after N seconds, do ACTIONS" branch (no condition → no PostInit needed)."""
        branch = self.sequential([self.wait_duree(seconds)] + list(action_irs))
        self.add_effetmap_branch(branch)
        return branch

    # ---- camps / refs discovery (helpers for callers) ----
    def find_camps(self):
        """[(ir, kwargs)] for every VariableCamp (so a UI can pick a camp). kwargs includes 'NiveauIA'
        (Player/Scripted) + 'Nationalite' resolved from the enum fields."""
        out = []
        for ir, info in SL.ir_assignments(self.src).items():
            if info["type"] == "VariableCamp":
                kw = SL.scalar_kwargs(info["body"])
                for f in ("NiveauIA", "Nationalite", "Couleur"):
                    mm = re.search(r"\b" + f + r"\s*=\s*[\w.]*\.(\w+)", info["body"])
                    if mm:
                        kw[f] = mm.group(1)
                out.append((ir, kw))
        return out

    def find_timers(self):
        """[(ir, seconds)] for every VariableTimer (so a UI can trigger on the mission timer)."""
        return [(t["ir"], t["seconds"]) for t in SL.parse_timers(self.src)["timers"]]

    # ---- emit + validate ----
    def emit(self):
        return self.src

    def validate(self):
        """List of problems with the CURRENT source. Empty = safe to recompile."""
        issues = []
        irs = SL.ir_assignments(self.src)
        for ir, info in irs.items():
            if any(f in info["type"] for f in _FORBIDDEN):
                issues.append("FORBIDDEN (RCE): %s = %s — refuse to emit" % (ir, info["type"]))
        # reactive nodes we ADDED must be in PostInit (avoid false-positives on the untouched base script)
        postinit = set()
        if self.launcher:
            span = _assignment(self.src, self.launcher)
            lb = span and _list_bracket(self.src, span[1], span[2], "PostInit")
            if lb:
                postinit = set(re.findall(r"IR_\d+", self.src[lb[0]:lb[1]]))
        for ir, typ in self._added.items():
            if any(typ.startswith(p) for p in _REACTIVE_PREFIX) and ir not in postinit:
                issues.append("DEAD TRIGGER: added %s (%s) not in PostInit — will never fire" % (ir, typ))
        return issues

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
expander.py
===========

Walks a workflow YAML the way the engine would, once per node, and produces the
flat step list a MOP is made of.

Mirrors cliautomation/exec/ExecutionOrchestrator.java:

    executeSteps()      phases in declaration order, phase-level `when`
    executeLoopStep()   :573-605  for_each / item_var / max_iterations, and
                                  continue_when which runs the body when TRUE
    step gating         `when` runs the step, `skip_when` skips it

and climopgeneration/MopGenerator.java, which scopes each node to its own
nodeGroup before walking so the outer `for_each: "${nodeGroups}"` yields exactly
one iteration and no other zone's steps bleed into this node's document.

THREE-STATE GATING
------------------
A MOP is written before the node has been touched, so a condition may depend on
a value that only exists at run time. Every gate is therefore evaluated to one of

    True      the step runs - every operand was known
    False     the step is skipped - every operand was known
    None      undecidable here, because an operand comes from command output

An undecidable step is ALWAYS kept in the document, carrying its expression, so
the page can decide it live once the operator supplies the data. Dropping it
would hide a step the operator may well have to run.

ASSUMED SUCCESS
---------------
Steps are expanded on the happy path: whatever a step's validation success
criteria IMPLY about its registers is applied (`IMSIRESULT == "success"` ->
IMSIRESULT=success, `IDLE_OK_COUNT >= 8` -> 8), and anything not derivable from
the criteria stays blank. That keeps later commands rendering with real values
without asking the operator for output they have not collected yet.
"""

import copy

import synth
from engine import (Context, apply_registers, split_top_level,
                    find_operator_outside_quotes)

LOOP_TYPES = ("loop",)
_TRUE, _FALSE, _UNKNOWN = True, False, None

# How a phase is rendered. The health checks are a fixed list of commands with a
# fixed pass/fail reading, so they keep the old checklist presentation. The
# activity and its rollback are where the operator needs per-command control,
# because every later command depends on what the node just said.
CHECKLIST, INTERACTIVE = "checklist", "interactive"
_ACTIVITY_WORDS = ("ACTIVITY", "ROLLBACK", "CONFIG", "EXECUTION")
_CHECK_WORDS = ("HEALTH", "PRECHECK", "PRE_CHECK", "POSTCHECK", "POST_CHECK",
                "PRE_NODE", "POST_NODE")


def render_mode_for(phase_id, phase_name):
    """Checklist for pre/post health checks, interactive for activity/rollback."""
    label = ("%s %s" % (phase_id or "", phase_name or "")).upper()
    if any(word in label for word in _CHECK_WORDS):
        return CHECKLIST
    if any(word in label for word in _ACTIVITY_WORDS):
        return INTERACTIVE
    return INTERACTIVE


class Step(object):
    """One row of the MOP."""

    __slots__ = ("uid", "phase", "phase_name", "phase_description", "seq",
                 "kind", "node_ref", "node_target", "description", "step_description",
                 "send_raw", "send", "unresolved", "when", "when_state",
                 "skip_when", "skip_state", "on_failure", "retries",
                 "retry_delay_sec", "timeout_sec", "register", "validation",
                 "implied", "consumes", "produces", "loop_path", "email",
                 "logs", "hide_when_skipped", "use_exit_code",
                 "success_output", "failure_output", "render_mode",
                 "description_raw")

    def __init__(self, **kw):
        for slot in self.__slots__:
            setattr(self, slot, kw.get(slot))

    def as_dict(self):
        out = {}
        for slot in self.__slots__:
            value = getattr(self, slot)
            if slot == "unresolved":
                value = [{"token": u.token, "reason": u.reason} for u in value or []]
            out[slot] = value
        return out


class Expansion(object):
    def __init__(self, node, steps, params, warnings):
        self.node = node
        self.steps = steps
        self.params = params
        self.warnings = warnings

    def as_dict(self):
        return {"node": self.node,
                "steps": [s.as_dict() for s in self.steps],
                "params": self.params,
                "warnings": self.warnings}


# --------------------------------------------------------------------------- #
#  implied values from validation criteria
# --------------------------------------------------------------------------- #
def implied_values(criteria, context, first_clause_only=True):
    """
    What a success criteria block says the registers must hold.

    `${IMSIRESULT == "success"}`                     -> {IMSIRESULT: "success"}
    `${APACHESTATUS == "running" && INSTANCE...}`    -> both
    `${IDLE_OK_COUNT >= 8}`                          -> {IDLE_OK_COUNT: "8"}
    `${XMLEXISTS == "true" || TABLEIMSICOUNT == "0"}`-> first clause only
    `${LDAPFIELDS != ""}`                            -> {} (not derivable)
    """
    out = {}
    if not isinstance(criteria, dict):
        return out
    for key in ("all", "any"):
        block = criteria.get(key)
        if isinstance(block, list):
            for item in block:
                out.update(implied_values(item, context, first_clause_only))
                if key == "any" and first_clause_only and out:
                    break
    expr = criteria.get("expr")
    if isinstance(expr, str) and expr.strip():
        out.update(_implied_from_expr(expr, context, first_clause_only))
    return out


def _implied_from_expr(expr, context, first_clause_only):
    text = expr.strip()
    if text.startswith("${") and text.endswith("}"):
        text = text[2:-1].strip()
    out = {}
    clauses = split_top_level(text, "||")
    if first_clause_only and clauses:
        clauses = clauses[:1]
    for clause in clauses:
        for leaf in split_top_level(clause, "&&"):
            name, value = _implied_from_leaf(leaf.strip(), context)
            if name and name not in out:
                out[name] = value
    return out


def _implied_from_leaf(leaf, context):
    for op in ("==", ">=", "<=", ">", "<"):
        at = find_operator_outside_quotes(leaf, op)
        if at <= 0:
            continue
        name = leaf[:at].strip()
        raw = leaf[at + len(op):].strip()
        if not name.replace("_", "").isalnum():
            return None, None          # a dotted path is CIQ data, not a register
        quoted = len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\""
        literal = context.operand(raw)
        if not quoted and literal == raw and _looks_like_identifier(raw):
            # The right-hand side is another VARIABLE, not a literal - as in
            # `${LINK_UP_COUNT == REMOTE_NODE_COUNT}`. The criteria assert a
            # relation between two registers, so no single value is implied;
            # reading "REMOTE_NODE_COUNT" as a literal would have the
            # synthesiser emit LINK_UP_COUNT=REMOTE_NODE_COUNT.
            return None, None
        if op == "==":
            # An empty literal is a real assertion, not a missing one:
            # `${BLOCKED == ""}` says the step passes precisely when
            # nothing was captured. synth.success_output() reads "" as
            # "emit no line for this register", so the register keeps its
            # declared default and the criteria actually pass.
            return name, literal
        try:
            number = float(literal)
        except (TypeError, ValueError):
            return None, None
        if op == ">":
            number += 1
        elif op == "<":
            number -= 1
        return name, str(int(number)) if number == int(number) else str(number)
    return None, None


# --------------------------------------------------------------------------- #
#  the walk
# --------------------------------------------------------------------------- #
class Expander(object):

    def __init__(self, workflow, data, params, template=None, tracer=None):
        self.workflow = workflow or {}
        self.data = data or {}
        self.params = dict(params or {})
        self.template = template
        self.tracer = tracer
        self.defaults = ((self.workflow.get("globals") or {}).get("defaults")) or {}

    # -- context -------------------------------------------------------------
    def base_context(self, node):
        globals_block = self.workflow.get("globals") or {}
        secrets = {}
        for name, spec in (globals_block.get("secrets") or {}).items():
            secrets[name] = spec.get("value") if isinstance(spec, dict) else spec

        variables = {}
        variables.update(self.data)
        variables.update(self.params)

        # The node being documented. Its OWN fields are in scope, not just the
        # root data section: the MRF and SBC workflows loop over `${configData}`
        # and gate on `${node == nodeName}`, and both of those live on the node,
        # not at the root. Without promoting them the loop resolves to nothing,
        # every step inside it disappears, and the document comes out with the
        # health checks and no activity at all.
        for key, value in node.items():
            variables[key] = value

        variables["node_row"] = node
        name = node.get("node") or node.get("nodeGroup") or ""
        variables["NODE_NAME"] = name
        variables["nodeName"] = name
        variables["metadata"] = {"nodeName": name}
        variables["nodeGroup"] = node.get("nodeGroup") or ""
        variables["crGroup"] = node.get("crGroup") or ""

        # MopGenerator scopes the outer loop to this node's own nodeGroup
        group = self._node_group_of(node)
        variables["nodeGroups"] = [group] if group else []
        variables["nodes"] = [node]

        context = Context(variables, secrets)

        # globals.vars in declaration order, each interpolated against what is
        # known so far - the same order a real run establishes them in.
        for name, value in (globals_block.get("vars") or {}).items():
            if name in self.params:
                continue                      # a request parameter always wins
            context.put(name, context.interpolate(value, record_unresolved=False)
                        if isinstance(value, str) else value)
        return context

    def _node_group_of(self, node):
        name = node.get("nodeGroup")
        for group in self.data.get("nodeGroups") or []:
            if isinstance(group, dict) and group.get("nodeGroup") == name:
                scoped = dict(group)
                scoped["nodes"] = [node]
                return scoped
        return None

    # -- entry ---------------------------------------------------------------
    def expand(self, node):
        context = self.base_context(node)
        state = {"seq": 0, "steps": [], "warnings": [], "halted": False}

        for phase_id, phase in (self.workflow.get("phases") or {}).items():
            if not isinstance(phase, dict):
                continue
            gate, _t = self._gate(context, phase.get("when"))
            phase_ctx = {"id": phase_id,
                         "name": phase.get("name") or phase_id,
                         "description": phase.get("description") or "",
                         "when": phase.get("when"),
                         "when_state": gate,
                         "render_mode": render_mode_for(phase_id, phase.get("name"))}
            if gate is _FALSE:
                state["warnings"].append(
                    "phase %s skipped: when %s is false" % (phase_id, phase.get("when")))
                continue
            for name, value in (phase.get("vars") or {}).items():
                context.put(name, context.interpolate(value, record_unresolved=False)
                            if isinstance(value, str) else value)
            self._walk(phase.get("steps") or [], context, phase_ctx, state, [])

        return Expansion(self._node_meta(node), state["steps"],
                         self.params, state["warnings"])

    def _node_meta(self, node):
        return {"node": node.get("node") or node.get("nodeGroup"),
                "nodeGroup": node.get("nodeGroup"),
                "crGroup": node.get("crGroup"),
                "email": node.get("email"),
                "activity": self.data.get("activity"),
                "nodeType": self.data.get("nodeType"),
                "niamIds": self._niam_ids(node)}

    @staticmethod
    def _niam_ids(node):
        out = {}
        for key, value in node.items():
            if isinstance(value, dict) and any(k.startswith("niamID") for k in value):
                out[key] = value
        return out

    # -- recursion -----------------------------------------------------------
    def _walk(self, steps, context, phase_ctx, state, loop_path):
        for raw in steps:
            if not isinstance(raw, dict):
                continue
            if raw.get("type") in LOOP_TYPES:
                self._walk_loop(raw, context, phase_ctx, state, loop_path)
            else:
                self._emit(raw, context, phase_ctx, state, loop_path)

    def _walk_loop(self, loop, context, phase_ctx, state, loop_path):
        item_var = loop.get("item_var") or "item"
        items = context.resolve_for_each(loop.get("for_each"))
        limit = loop.get("max_iterations")
        if isinstance(limit, int) and limit >= 0:
            items = items[:limit]
        if loop.get("reverse_order") is True:
            items = list(reversed(items))

        for index, item in enumerate(items):
            context.put(item_var, item)

            cont = loop.get("continue_when")
            if cont is not None:
                gate, _t = self._gate(context, cont)
                # ExecutionOrchestrator:591 - the body runs only when TRUE.
                # UNKNOWN keeps the iteration: a MOP must not drop steps whose
                # filter depends on runtime data.
                if gate is _FALSE:
                    continue
            brk = loop.get("break_when")
            if brk is not None and self._gate(context, brk)[0] is _TRUE:
                break

            self._walk(loop.get("steps") or [], context, phase_ctx, state,
                       loop_path + [{"var": item_var,
                                     "index": index,
                                     "label": self._label(item)}])

    @staticmethod
    def _label(item):
        if isinstance(item, dict):
            for key in ("nodeGroup", "configSeq", "table", "node"):
                if item.get(key):
                    return str(item[key])
            data = item.get("data")
            if isinstance(data, dict):
                for key in ("Test IMSI", "IMSI"):
                    if data.get(key):
                        return str(data[key])
            return "item"
        return "" if item is None else str(item)

    # -- one step ------------------------------------------------------------
    def _emit(self, raw, context, phase_ctx, state, loop_path):
        when_state, when_trace = self._gate(context, raw.get("when"))
        # An ABSENT skip_when must not skip the step. _gate() defaults an empty
        # expression to TRUE, which is right for `when` and exactly wrong here.
        skip_raw = raw.get("skip_when")
        if skip_raw is None or not str(skip_raw).strip():
            skip_state, skip_trace = _FALSE, []
        else:
            skip_state, skip_trace = self._gate(context, skip_raw)

        context.unresolved = []
        send_raw = raw.get("send")
        send = context.interpolate(send_raw) if isinstance(send_raw, str) else send_raw
        description = context.interpolate(raw.get("command_description") or "")
        node_ref = raw.get("node")
        node_target = context.interpolate(node_ref) if isinstance(node_ref, str) else node_ref
        unresolved = self._explain(list(context.unresolved), state)
        context.unresolved = []

        validation = self._validation(raw, context)
        implied = validation.get("impliedOnSuccess") or {}
        register = copy.deepcopy(raw.get("register") or [])
        ok_output = synth.success_output(register, validation["successCriteria"],
                                         implied, counts=implied)
        bad_output = synth.failure_output(register, validation["successCriteria"],
                                          implied)

        consumes = sorted(set(
            [t for t in context.tokens_in(send_raw or "")] +
            [t for t in context.tokens_in(str(raw.get("when") or ""))] +
            [t for t in context.tokens_in(str(raw.get("skip_when") or ""))]))

        state["seq"] += 1
        step = Step(
            uid="s%04d" % state["seq"],
            phase=phase_ctx["id"],
            phase_name=phase_ctx["name"],
            phase_description=phase_ctx["description"],
            seq=state["seq"],
            kind="email" if raw.get("email") else ("local" if node_target == "local" else "remote"),
            node_ref=node_ref,
            node_target=node_target,
            description=description,
            description_raw=raw.get("command_description") or "",
            step_description=raw.get("description") or "",
            send_raw=send_raw,
            send=send,
            unresolved=unresolved,
            when=raw.get("when"),
            when_state=when_state,
            skip_when=raw.get("skip_when"),
            skip_state=skip_state,
            on_failure=self._on_failure(raw),
            retries=raw.get("retries", self.defaults.get("retries")),
            retry_delay_sec=raw.get("retry_delay_sec", self.defaults.get("retry_delay_sec")),
            timeout_sec=raw.get("timeout_sec", self.defaults.get("timeout_sec")),
            register=register,
            validation=validation,
            implied=implied,
            consumes=consumes,
            produces=[],
            loop_path=loop_path,
            email=self._email(raw, context),
            logs=raw.get("logs"),
            hide_when_skipped=bool(raw.get("hide_when_skipped")),
            use_exit_code=bool(raw.get("use_exit_code")),
            success_output=ok_output,
            failure_output=bad_output,
            render_mode=phase_ctx["render_mode"],
        )

        if self.tracer:
            self.tracer.step(step, context, when_trace + skip_trace)

        # Assumed success: deterministic name/value registers, then whatever the
        # success criteria imply. Applied only when the step is not definitely
        # skipped, so a skipped step cannot poison later interpolation.
        produced = []
        if when_state is not _FALSE and skip_state is not _TRUE:
            apply_registers(context, step.register, ok_output, produced)
            # Whatever no register captured, but the criteria still assert, is
            # set directly - e.g. a criteria on a variable another step owns.
            for name, value in implied.items():
                if context.resolve_name(name) in (None, ""):
                    context.put(name, value)
                    produced.append((name, value, "implied by success criteria"))
            for name, value in (validation.get("varsOnSuccess") or {}).items():
                context.put(name, value)
                produced.append((name, value, "validation success vars"))
        step.produces = [{"name": n, "value": v, "source": s} for n, v, s in produced]

        state["steps"].append(step)

    # -- what the mapping has to say about a blank ---------------------------
    def _explain(self, items, state):
        """
        Ask the json-output mapping why a CIQ reference resolved to nothing.

        `${rec_row.data.Call Type}` blank has two very different causes, and
        the operator cannot tell them apart from the rendered command: either
        the mapping fills that column and this order had no value in it, or the
        mapping never produces the column at all - in which case the MOP is
        reading a field that no CIQ for this activity can carry, and the
        workflow or the mapping is wrong. The second is recorded as a warning
        so it shows up once per document rather than once per command.
        """
        if not self.template or not getattr(self.template, "doc", None):
            return items
        for item in items:
            field = _ciq_field(item.token)
            if not field:
                continue
            note = self.template.explain(field)
            if not note:
                continue
            item.reason = "%s - %s" % (item.reason, note)
            if self.template.emits(field) is False:
                warning = "the mapping never fills ${%s}" % item.token
                if warning not in state["warnings"]:
                    state["warnings"].append(warning)
        return items

    def _validation(self, raw, context):
        block = raw.get("validation") or {}
        success = block.get("success") or {}
        failure = block.get("failure") or {}
        warning = block.get("warning") or {}
        out = {
            "enabled": bool(block.get("enabled")),
            "description": context.interpolate(block.get("description") or ""),
            "successCriteria": success.get("criteria") or {},
            "successMessage": success.get("message") or "",
            "failureMessage": failure.get("message") or warning.get("message") or "",
            "hasWarningBranch": bool(warning),
            "impliedOnSuccess": implied_values(success.get("criteria") or {}, context),
            "varsOnSuccess": self._branch_vars(success, context),
            "varsOnFailure": self._branch_vars(failure or warning, context),
        }
        return out

    @staticmethod
    def _branch_vars(branch, context):
        out = {}
        for entry in (branch or {}).get("vars") or []:
            if isinstance(entry, dict) and entry.get("name"):
                out[entry["name"]] = context.interpolate(entry.get("value", ""),
                                                         record_unresolved=False)
        return out

    def _on_failure(self, raw):
        behaviour = raw.get("on_failure", self.defaults.get("on_failure") or "stop")
        if isinstance(behaviour, dict):
            if behaviour.get("run"):
                return {"mode": "run", "step": behaviour["run"]}
            return {"mode": str(next(iter(behaviour.values()), "stop"))}
        return {"mode": str(behaviour)}

    def _email(self, raw, context):
        block = raw.get("email")
        if not isinstance(block, dict):
            return None
        def render(value):
            if isinstance(value, list):
                return [context.interpolate(v) for v in value]
            return context.interpolate(value) if isinstance(value, str) else value
        return dict((key, render(value)) for key, value in block.items())

    # -- gates ---------------------------------------------------------------
    def _gate(self, context, expression):
        """
        Evaluate a gate to True / False / None(unknown).

        Unknown means an operand resolved to nothing, i.e. it comes from command
        output that does not exist yet. Note the engine itself has no such
        notion - it compares against the empty string - so a gate reported
        unknown here is one whose real outcome depends on the run.
        """
        if expression is None or not str(expression).strip():
            return _TRUE, []
        text = str(expression).strip()
        if text.startswith("${") and text.endswith("}"):
            # The whole condition is one ${...} wrapper, so the placeholder
            # pattern returns the entire expression as a single "token"
            # ('TargetNodeRDS == ""'), which resolves to nothing and made every
            # such gate look undecidable. Read the identifiers out of the
            # expression instead.
            tokens = _bare_names(text[2:-1])
        else:
            tokens = context.tokens_in(text)
        missing = [t for t in tokens if context.resolve_name(t) is None]
        result, trace = context.evaluate_traced(expression)
        if missing:
            return _UNKNOWN, trace
        return (_TRUE if result else _FALSE), trace


def _ciq_field(token):
    """
    The CIQ column a token is reaching for, or None if it is not CIQ data.

    `rec_row.data.Call Type` -> `Call Type`; `ng_row.nodeGroup` -> `nodeGroup`;
    a bare `LDAPFIELDS` is an engine variable and has no mapping to blame.
    """
    token = (token or "").strip()
    if not token or "." not in token:
        return None
    if token.startswith(("ENV.", "SECRET.", "base64:")):
        return None
    return token.rsplit(".", 1)[-1].split("[")[0].strip() or None


def _looks_like_identifier(text):
    text = (text or "").strip()
    return bool(text) and not text[0].isdigit() and all(
        ch.isalnum() or ch == "_" for ch in text)


def _bare_names(expr):
    """Identifiers in a condition written without ${} around each operand."""
    out, buf = [], []
    in_quote = None
    for char in expr:
        if in_quote:
            if char == in_quote:
                in_quote = None
            continue
        if char in "'\"":
            in_quote = char
            continue
        if char.isalnum() or char in "._[]":
            buf.append(char)
        else:
            if buf:
                out.append("".join(buf))
                buf = []
    if buf:
        out.append("".join(buf))
    return [t for t in out if t and not t[0].isdigit()]

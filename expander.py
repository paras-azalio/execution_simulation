#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
expander.py
===========

Walks a workflow YAML the way the engine would, once per node, and produces the
flat step list a MOP is made of - plus the two tables the page needs to run it
the way the engine runs it: the PHASES (when / on_failure / then) and the LOOP
instances (item list, when, continue_when, break_when, on_failure).

Mirrors cliautomation/exec/ExecutionOrchestrator.java:

    executeSteps()      phases in declaration order, phase-level `when`
    executeLoopStep()   :590-623  for_each / item_var / max_iterations, and
                                  continue_when which runs the body when TRUE
    step gating         `when` runs the step, `skip_when` skips it

cliautomation/exec/YamlWorkflowLoader.java:

    ensureStepIds()     every step gets an id - its own, or auto_step_N
    normalizePhases()   a phase's id is its `name:` when it has one

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

Phases are never dropped at all. A rollback phase gated on
`${ROLLBACK_REQUIRED == "true"}` is false on the happy path and true the moment
a step's failure branch sets ROLLBACK_REQUIRED - which is precisely the path an
operator is walking when they press Failure. The same holds for a loop gate or
a continue_when that reads a variable some step sets at run time (the checksum
retry loops read CHECKSUM_STATUS): it is kept and decided by the page.

ASSUMED SUCCESS
---------------
Steps are expanded on the happy path: whatever a step's validation success
criteria IMPLY about its registers is applied (`IMSIRESULT == "success"` ->
IMSIRESULT=success, `IDLE_OK_COUNT >= 8` -> 8), the synthesised success output
is run through the step's registers (and a REST step's response_template), and
the success branch's `vars:` are set - exactly what a passing step leaves behind.
"""

import copy

import synth
from engine import (Context, apply_registers, apply_response_template,
                    split_top_level, find_operator_outside_quotes,
                    interpolate_object, group_names)

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

    __slots__ = ("uid", "step_id", "phase", "phase_name", "phase_description", "seq",
                 "kind", "node_ref", "node_target", "description", "step_description",
                 "send_raw", "send", "unresolved", "when", "when_state",
                 "skip_when", "skip_state", "on_failure", "retries",
                 "retry_delay_sec", "timeout_sec", "register", "validation",
                 "implied", "consumes", "produces", "loop_path", "email",
                 "logs", "hide_when_skipped", "use_exit_code", "ignore_exit",
                 "rest", "sftp",
                 "success_output", "success_status", "failure_output",
                 "failure_status", "failure_exit", "render_mode",
                 "description_raw")

    def __init__(self, **kw):
        for slot in self.__slots__:
            setattr(self, slot, kw.get(slot))

    def as_dict(self):
        out = {}
        for slot in self.__slots__:
            value = getattr(self, slot)
            if slot == "unresolved":
                value = [u.as_dict() if hasattr(u, "as_dict") else
                         {"token": u.token, "reason": u.reason} for u in value or []]
            out[slot] = value
        return out


class Expansion(object):
    def __init__(self, node, steps, params, warnings, phases=None, loops=None):
        self.node = node
        self.steps = steps
        self.params = params
        self.warnings = warnings
        self.phases = phases or []
        self.loops = loops or {}

    def as_dict(self):
        return {"node": self.node,
                "steps": [s.as_dict() for s in self.steps],
                "params": self.params,
                "warnings": self.warnings,
                "phases": self.phases,
                "loops": self.loops}


# --------------------------------------------------------------------------- #
#  step ids - YamlWorkflowLoader.ensureStepIds()
# --------------------------------------------------------------------------- #
def _phase_blocks(workflow):
    phases = (workflow or {}).get("phases") or {}
    return [(key, block) for key, block in phases.items() if isinstance(block, dict)]


def assign_step_ids(workflow):
    """
    The id the engine gives every step: its own `id:`, or `auto_step_N`.

    Ids are collected across the top-level `steps:` and every phase, depth
    first, and only then are the missing ones numbered - in the same order, so
    auto_step_3 here is auto_step_3 in a real run's report. Returned as
    {id(step dict): step id}; the workflow itself is not modified.
    """
    blocks = [(workflow or {}).get("steps") or []] + \
             [block.get("steps") or [] for _key, block in _phase_blocks(workflow)]
    used, seen = set(), set()

    def collect(steps):
        for step in steps or []:
            if not isinstance(step, dict) or id(step) in seen:
                continue
            seen.add(id(step))
            sid = step.get("id")
            if sid is not None and str(sid).strip():
                used.add(str(sid).strip())
            collect(step.get("steps"))

    for steps in blocks:
        collect(steps)

    ids, seq = {}, [1]
    seen = set()

    def assign(steps):
        for step in steps or []:
            if not isinstance(step, dict) or id(step) in seen:
                continue
            seen.add(id(step))
            sid = step.get("id")
            if sid is None or not str(sid).strip():
                while "auto_step_%d" % seq[0] in used:
                    seq[0] += 1
                sid = "auto_step_%d" % seq[0]
                used.add(sid)
                seq[0] += 1
            ids[id(step)] = str(sid).strip()
            assign(step.get("steps"))

    for steps in blocks:
        assign(steps)
    return ids


# --------------------------------------------------------------------------- #
#  what can change at run time
# --------------------------------------------------------------------------- #
def runtime_producers(workflow):
    """
    Every variable a step can set while the workflow runs, and the first step
    that sets it: register captures and names, count_vars, REST
    response_template / response_headers names, and the `vars:` of every
    validation branch. {name: (step id, how)}.

    A gate reading one of these can change after generation - ROLLBACK_REQUIRED
    flips when a failure branch runs, CHECKSUM_STATUS when a checksum fails - so
    a phase or loop gated on one is kept for the page to decide, and a blank
    reference to one is explained as "set at run time" rather than "not defined".
    """
    ids = assign_step_ids(workflow)
    out = {}

    def note(name, step, how):
        name = str(name or "").strip()
        if name and name not in out:
            out[name] = (ids.get(id(step), step.get("id")), how)

    def walk(steps):
        for step in steps or []:
            if not isinstance(step, dict):
                continue
            for entry in step.get("register") or []:
                if not isinstance(entry, dict):
                    continue
                for name in group_names(str(entry.get("regex") or "")):
                    note(name, step, "register")
                note(entry.get("count_var"), step, "register count")
                note(entry.get("name"), step, "register")
            rest = step.get("rest")
            if isinstance(rest, dict):
                for rule in rest.get("response_template") or []:
                    if isinstance(rule, dict):
                        note(rule.get("name"), step, "REST response_template")
                for rule in rest.get("response_headers") or []:
                    if isinstance(rule, dict):
                        note(rule.get("name"), step, "REST response header")
                note("http_status", step, "REST status")
            validation = step.get("validation")
            if isinstance(validation, dict):
                for branch in ("success", "failure", "warning"):
                    for name in branch_var_items(validation.get(branch)):
                        note(name[0], step, "validation %s vars" % branch)
            walk(step.get("steps"))

    walk((workflow or {}).get("steps"))
    for _key, block in _phase_blocks(workflow):
        walk(block.get("steps"))
    return out


def branch_var_items(branch):
    """
    A validation branch's `vars:` as [(name, raw value)].

    ValidationBranchDefinition.vars is a Map<String,Object>, so a mapping is
    the form the engine reads. A list of {name, value} is accepted as well - it
    is what the page used to expect - though SnakeYAML would refuse to bind it,
    which lint.py reports.
    """
    raw = (branch or {}).get("vars") if isinstance(branch, dict) else None
    if isinstance(raw, dict):
        return [(str(k), v) for k, v in raw.items()]
    if isinstance(raw, list):
        return [(str(e["name"]), e.get("value", "")) for e in raw
                if isinstance(e, dict) and e.get("name")]
    return []


# --------------------------------------------------------------------------- #
#  implied values from validation criteria
# --------------------------------------------------------------------------- #
def implied_values(criteria, context, first_clause_only=True, owned=None):
    """
    What a success criteria block says the registers must hold.

    `${IMSIRESULT == "success"}`                     -> {IMSIRESULT: "success"}
    `${APACHESTATUS == "running" && INSTANCE...}`    -> both
    `${IDLE_OK_COUNT >= 8}`                          -> {IDLE_OK_COUNT: "8"}
    `${XMLEXISTS == "true" || TABLEIMSICOUNT == "0"}`-> first clause only
    `${LDAPFIELDS != ""}`                            -> {} (not derivable)
    `http_status: 200`                               -> {http_status: "200"}
    """
    out = {}
    if not isinstance(criteria, dict):
        return out
    for key in ("all", "any"):
        block = criteria.get(key)
        if isinstance(block, list):
            for item in block:
                out.update(implied_values(item, context, first_clause_only, owned))
                if key == "any" and first_clause_only and out:
                    break
    expr = criteria.get("expr")
    if isinstance(expr, str) and expr.strip():
        out.update(_implied_from_expr(expr, context, first_clause_only, owned))
    if criteria.get("http_status") is not None and "http_status" not in out:
        out["http_status"] = str(criteria["http_status"])
    return out


def _implied_from_expr(expr, context, first_clause_only, owned=None):
    text = expr.strip()
    if text.startswith("${") and text.endswith("}"):
        text = text[2:-1].strip()
    out = {}
    clauses = split_top_level(text, "||")
    if first_clause_only and clauses:
        clauses = clauses[:1]
    for clause in clauses:
        for leaf in split_top_level(clause, "&&"):
            name, value = _implied_from_leaf(leaf.strip(), context, owned)
            if name and name not in out:
                out[name] = value
    return out


def _implied_from_leaf(leaf, context, owned=None):
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
        if op == "==" and owned and not quoted and _looks_like_identifier(raw) and \
                raw in owned and name not in owned:
            # `${EARLIER == MINE}`: this step's own register is the side to
            # set - success means it captures what the earlier step captured
            known = context.resolve_name(name)
            if known not in (None, ""):
                return raw, known if isinstance(known, str) else context._stringify(known)
        if not quoted and literal == raw and _looks_like_identifier(raw):
            # The right-hand side is another VARIABLE, not a literal - as in
            # `${LINK_UP_COUNT == REMOTE_NODE_COUNT}`. The criteria assert a
            # relation between two registers, so no single value is implied;
            # reading "REMOTE_NODE_COUNT" as a literal would have the
            # synthesiser emit LINK_UP_COUNT=REMOTE_NODE_COUNT.
            #
            # Unless the LEFT side is already known: the checksum pattern
            # `${BACKUPNODECHECKSUM == BACKUPREPOCHECKSUM}` compares what an
            # earlier step captured with what this one captures, and success
            # means this one captures that same value.
            known = context.resolve_name(name)
            if op == "==" and known not in (None, ""):
                return raw, known if isinstance(known, str) else context._stringify(known)
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
        self.nodes = self.workflow.get("nodes") or {}
        self.step_ids = assign_step_ids(self.workflow)
        self.producers = runtime_producers(self.workflow)
        # a gate on a request parameter can also change: the page edits them
        self.mutable = set(self.producers) | set(self.params)
        self.by_id = {}
        for step, sid in self._all_steps():
            self.by_id.setdefault(sid, []).append(step)

    def _all_steps(self):
        out = []

        def walk(steps):
            for step in steps or []:
                if isinstance(step, dict):
                    out.append((step, self.step_ids.get(id(step))))
                    walk(step.get("steps"))
        walk(self.workflow.get("steps"))
        for _key, block in _phase_blocks(self.workflow):
            walk(block.get("steps"))
        return out

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

        # CliAutomationEngine.executeForNode(): the aliases a real run adds
        _promote_niam(variables, node)
        if node.get("node") is not None:
            variables["currentNode"] = node.get("node")
        if node.get("crGroup") is not None:
            variables.setdefault("currentCrGroup", node.get("crGroup"))
        variables["currentNodeData"] = node
        collection = next((v for v in node.values() if isinstance(v, list)), None)
        if collection is not None:
            variables["currentNodeCollection"] = collection
        for target, source in (("VERSION", "version"), ("version", "VERSION")):
            if target not in variables and variables.get(source) is not None:
                variables[target] = variables[source]

        # MopExecutionUtil: a rollback-only run sets both flags
        if str(variables.get("ROLLBACK_ONLY", "")).lower() == "true":
            variables["ROLLBACK_REQUIRED"] = "true"

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
            if name == "ROLLBACK_REQUIRED" and \
                    str(self.params.get("ROLLBACK_ONLY", "")).lower() == "true":
                continue                      # MopExecutionUtil set it already
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
        state = {"seq": 0, "steps": [], "warnings": [], "loops": {}, "loop_seq": 0}
        phases = []

        for phase_id, phase in _phase_blocks(self.workflow):
            gate, _t = self._gate(context, phase.get("when"))
            java_id = str(phase.get("name") or "").strip() or phase_id
            then = phase.get("then")
            then = [str(t) for t in then] if isinstance(then, list) else \
                   ([str(then)] if then else [])
            descriptor = {
                "key": phase_id,
                "id": java_id,
                "name": phase.get("name") or phase_id,
                "description": phase.get("description") or "",
                "when": phase.get("when"),
                "when_state": gate,
                "on_failure": (str(phase.get("on_failure")).strip()
                               if phase.get("on_failure") is not None else None),
                "then": then,
                # ExecutionOrchestrator.isPostFailurePhase(): a ROLLBACK phase,
                # or one that declares then:, still runs after a stop
                "post_failure": "ROLLBACK" in java_id.upper() or bool(then),
                "render_mode": render_mode_for(phase_id, phase.get("name")),
                "steps": 0,
            }
            if not phase.get("steps"):
                # normalizePhases() drops a phase with no steps
                continue
            phases.append(descriptor)
            phase_ctx = dict(descriptor)
            phase_ctx["id"] = phase_id

            # A phase the happy path gates off is still documented - its gate
            # may open at run time - but whatever its steps would register must
            # not leak into the phases after it.
            snapshot = copy.copy(context.vars) if gate is _FALSE else None
            for name, value in (phase.get("vars") or {}).items():
                context.put(name, context.interpolate(value, record_unresolved=False)
                            if isinstance(value, str) else value)
            before = len(state["steps"])
            self._walk(phase.get("steps") or [], context, phase_ctx, state, [])
            descriptor["steps"] = len(state["steps"]) - before
            if snapshot is not None:
                context.vars = snapshot

        return Expansion(self._node_meta(node), state["steps"], self.params,
                         state["warnings"], phases, state["loops"])

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

    def _decided_false(self, context, expression):
        """
        A loop gate that is FALSE and reads nothing a run can change. Only then
        may the walk act on it the way the engine will; anything that reads a
        runtime variable or a request parameter is left for the page.
        """
        gate, _t = self._gate(context, expression)
        if gate is not _FALSE:
            return False
        return not (set(_names_in(expression)) & self.mutable)

    def _decided_true(self, context, expression):
        gate, _t = self._gate(context, expression)
        return gate is _TRUE and not (set(_names_in(expression)) & self.mutable)

    def _walk_loop(self, loop, context, phase_ctx, state, loop_path):
        # ExecutionOrchestrator: a loop's own when/skip_when is evaluated ONCE,
        # before the first item is bound.
        if loop.get("when") is not None and self._decided_false(context, loop.get("when")):
            return
        if loop.get("skip_when") is not None and str(loop.get("skip_when")).strip() and \
                self._decided_true(context, loop.get("skip_when")):
            return

        item_var = loop.get("item_var") or "item"
        items = context.resolve_for_each(loop.get("for_each"))
        limit = loop.get("max_iterations")
        overflow = isinstance(limit, int) and limit >= 0 and len(items) > limit
        if isinstance(limit, int) and limit >= 0:
            items = items[:limit]
        if loop.get("reverse_order") is True:
            items = list(reversed(items))

        state["loop_seq"] += 1
        uid = "L%04d" % state["loop_seq"]
        sid = self.step_ids.get(id(loop))
        state["loops"][uid] = {
            "uid": uid,
            "step_id": sid,
            "phase": phase_ctx["id"],
            "var": item_var,
            "items": _jsonable(items),
            "count": len(items),
            "when": loop.get("when"),
            "skip_when": loop.get("skip_when"),
            "continue_when": loop.get("continue_when"),
            "break_when": loop.get("break_when"),
            "max_iterations": limit if isinstance(limit, int) else None,
            "overflow": bool(overflow),
            "on_failure": self._loop_on_failure(loop),
            "depth": len(loop_path),
            "parent": loop_path[-1]["loop"] if loop_path else None,
        }
        if overflow:
            state["warnings"].append(
                "loop %s (%s) has more items than max_iterations %s - the engine "
                "FAILS the loop after the first %s" % (sid, item_var, limit, limit))

        for index, item in enumerate(items):
            context.put(item_var, item)

            cont = loop.get("continue_when")
            if cont is not None and self._decided_false(context, cont):
                # ExecutionOrchestrator:609 - the body runs only when TRUE
                continue
            brk = loop.get("break_when")
            if brk is not None and self._decided_true(context, brk):
                break

            self._walk(loop.get("steps") or [], context, phase_ctx, state,
                       loop_path + [{"var": item_var,
                                     "index": index,
                                     "label": self._label(item),
                                     "loop": uid}])

    def _loop_on_failure(self, loop):
        """
        What happens after a failure ends this loop: `continue` / `warning`
        absorb it, anything else - including an on_failure map, which the loop
        code does not read - stops the enclosing body.
        """
        behaviour = loop.get("on_failure")
        if behaviour is None:
            behaviour = self.defaults.get("on_failure")
        if isinstance(behaviour, str):
            low = behaviour.strip().lower()
            return low if low in ("continue", "warning") else "stop"
        return "stop"

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

        node_ref = raw.get("node")
        context.unresolved = []
        node_target = context.interpolate(node_ref) if isinstance(node_ref, str) else node_ref
        kind, send_raw, rest, sftp = self._command(raw, context, node_target)
        send = context.interpolate(send_raw) if isinstance(send_raw, str) else send_raw
        if rest and isinstance(rest.get("body_raw"), str):
            context.interpolate(rest["body_raw"])
        description = context.interpolate(raw.get("command_description") or "")
        unresolved = self._explain(list(context.unresolved), state)
        context.unresolved = []

        validation = self._validation(raw, context)
        implied = validation.get("impliedOnSuccess") or {}
        register = copy.deepcopy(raw.get("register") or [])
        # captureVariables() interpolates a register regex before compiling it,
        # so the synthesiser has to sample the pattern the engine will match.
        live_register = _interpolated_register(register, context)
        if rest:
            ok_output, ok_status, bad_output, bad_status = synth.rest_outputs(
                rest.get("response_template"), validation["successCriteria"], implied,
                live_register, self._preferred_statuses(raw))
            # no status fails it: only a connection error can (exit_code -1)
            bad_exit = 0 if bad_status is not None else -1
        else:
            ok_output = synth.success_output(live_register, validation["successCriteria"],
                                             implied, counts=implied)
            bad_output = synth.failure_output(live_register, validation["successCriteria"],
                                              implied)
            ok_status = bad_status = None
            # A derivable failing output fails on its own words, with the
            # command itself exiting 0 - which is also what reaches a warning
            # branch. Where no words can fail the criteria, the command fails.
            bad_exit = 0 if bad_output != synth.MARKER else 1

        consumes = sorted(set(
            [t for t in context.tokens_in(send_raw or "")] +
            [t for t in context.tokens_in((rest or {}).get("body_raw") or "")] +
            [t for t in context.tokens_in(str(raw.get("when") or ""))] +
            [t for t in context.tokens_in(str(raw.get("skip_when") or ""))]))

        prompt = raw.get("prompt_regex")
        state["seq"] += 1
        step = Step(
            uid="s%04d" % state["seq"],
            step_id=self.step_ids.get(id(raw)),
            phase=phase_ctx["id"],
            phase_name=phase_ctx["name"],
            phase_description=phase_ctx["description"],
            seq=state["seq"],
            kind=kind,
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
            # shouldIgnoreExitCode(): a step-level prompt_regex means the
            # command completes on the prompt and its exit code is NOT read -
            # whatever use_exit_code says, since the engine never reads that
            ignore_exit=bool(isinstance(prompt, str) and prompt.strip()),
            rest=rest,
            sftp=sftp,
            success_output=ok_output,
            success_status=ok_status,
            failure_output=bad_output,
            failure_status=bad_status,
            failure_exit=bad_exit,
            render_mode=phase_ctx["render_mode"],
        )

        if self.tracer:
            self.tracer.step(step, context, when_trace + skip_trace)

        # Assumed success: what a passing run of this step leaves behind - the
        # REST response_template and status, the registers over the success
        # output, whatever the criteria still assert, and the success branch's
        # vars. Applied only when the step is not definitely skipped, so a
        # skipped step cannot poison later interpolation.
        produced = []
        if when_state is not _FALSE and skip_state is not _TRUE:
            record = []
            if rest:
                apply_response_template(context, rest.get("response_template"),
                                        ok_output, record)
                context.put("http_status", ok_status)
                record.append(("http_status", str(ok_status), "REST status"))
            apply_registers(context, step.register, ok_output, record)
            for name, value, source in record:
                if value is not None:
                    produced.append((name, value, source))
            # Whatever no register captured, but the criteria still assert, is
            # set directly - e.g. a criteria on a variable another step owns.
            for name, value in implied.items():
                if context.resolve_name(name) in (None, ""):
                    context.put(name, value)
                    produced.append((name, value, "implied by success criteria"))
            for name, value in (validation.get("varsOnSuccess") or {}).items():
                context.put(name, value)
                produced.append((name, value, "validation success vars"))
        step.produces = [{"name": n, "value": v if isinstance(v, str) else
                          context._stringify(v), "source": s} for n, v, s in produced]

        state["steps"].append(step)

    # -- the command a step sends ------------------------------------------
    def _command(self, raw, context, node_target):
        """
        (kind, command template, rest block, sftp block).

        describeStepCommand(): a step's `send`; for an SFTP step
        "sftp <operation> local=<path> remote=<path>"; for a REST step
        "<METHOD> <base_url><path>"; the page shows the REST body under it.
        """
        node_def = self.nodes.get(node_target) if isinstance(node_target, str) else None
        node_type = str((node_def or {}).get("type") or "").lower()
        if raw.get("email"):
            kind = "email"
        elif isinstance(raw.get("rest"), dict) or node_type == "rest":
            kind = "rest"
        elif isinstance(raw.get("sftp"), dict):
            kind = "sftp"
        elif node_target == "local" or node_type == "local":
            kind = "local"
        else:
            kind = "remote"

        send = raw.get("send")
        if isinstance(send, str):
            return kind, send, None, None

        sftp = raw.get("sftp")
        if isinstance(sftp, dict):
            template = "sftp %s local=%s remote=%s" % (
                sftp.get("operation") or "sftp", sftp.get("local_path") or "",
                sftp.get("remote_path") or "")
            return kind, template, None, copy.deepcopy(sftp)

        rest = raw.get("rest")
        if isinstance(rest, dict):
            method = str(rest.get("method") or "GET").upper().strip()
            path_raw = str(rest.get("path") or "").strip()
            base_raw = str(((node_def or {}).get("rest") or {}).get("base_url") or "").strip()
            path = context.interpolate(path_raw, record_unresolved=False)
            base = context.interpolate(base_raw, record_unresolved=False)
            joiner = "/" if path and base and not path.startswith("/") else ""
            template = (method + " " + base_raw + joiner + path_raw).strip() \
                if (path_raw or base_raw) else None
            body, body_type = None, None
            for key in ("body_json", "body_map", "body_file", "multipart"):
                if rest.get(key) is not None:
                    body_type = key
                    value = rest.get(key)
                    body = value if isinstance(value, str) else synth.dumps(value)
                    break
            block = {
                "method": method,
                "path_raw": path_raw,
                "base_url_raw": base_raw,
                "body_raw": body,
                "body_type": body_type,
                "content_type": rest.get("body_content_type"),
                "query": rest.get("query"),
                "response_template": copy.deepcopy(rest.get("response_template") or []),
                "response_headers": copy.deepcopy(rest.get("response_headers") or []),
                "download_to": rest.get("download_to"),
                "has_base_url": bool(base),
            }
            return kind, template, block, None
        return kind, send, None, None

    def _preferred_statuses(self, raw):
        """
        HTTP statuses this step's on_failure handlers are waiting for.

        A DSR step fails into `run: [step:config_token_refresh_on_401, ...]`
        and those handlers are gated on `last_failed_http_status == "401"`. A
        synthesised failure of 500 would skip them all, so the Failure button
        uses the status the handlers expect - it is the path the author wrote.
        """
        behaviour = raw.get("on_failure")
        if not isinstance(behaviour, dict):
            return []
        targets = behaviour.get("run")
        targets = targets if isinstance(targets, list) else ([targets] if targets else [])
        found = []
        for target in targets:
            name = str(target)
            if name.lower().startswith("step:"):
                name = name[5:].strip()
            for step in self.by_id.get(name) or []:
                for literal in _status_literals(step.get("when")):
                    if literal not in found:
                        found.append(literal)
        return found

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

        A third cause needs no mapping at all: a variable some step SETS at run
        time - a register, a REST response field, a branch's vars - is blank
        only because that step has not run yet. Such a reference is marked
        `runtime`, and the page does not offer it as a parameter to fill in.
        """
        seen, unique = set(), []
        for item in items:
            if item.token not in seen:
                seen.add(item.token)
                unique.append(item)
        items = unique
        for item in items:
            root = item.token.split(".")[0].split("[")[0].strip()
            producer = self.producers.get(root)
            if producer:
                item.reason = "%s is set at run time by step %s (%s)" % (
                    root, producer[0], producer[1])
                item.runtime = True
                continue
            if not self.template or not getattr(self.template, "doc", None):
                continue
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
        block = raw.get("validation")
        block = block if isinstance(block, dict) else {}
        success = block.get("success") if isinstance(block.get("success"), dict) else {}
        failure = block.get("failure") if isinstance(block.get("failure"), dict) else {}
        warning = block.get("warning") if isinstance(block.get("warning"), dict) else {}
        out = {
            # applyValidation(): validation runs unless it is ABSENT or says
            # `enabled: false` explicitly
            "enabled": bool(block) and block.get("enabled") is not False,
            "description": context.interpolate(block.get("description") or ""),
            "successCriteria": success.get("criteria") or {},
            "successMessage": success.get("message") or "",
            "failureMessage": failure.get("message") or warning.get("message") or "",
            "warningMessage": warning.get("message") or "",
            "failureCriteria": failure.get("criteria"),
            "hasSuccessBranch": "success" in block,
            "hasFailureBranch": "failure" in block,
            "hasWarningBranch": bool(warning),
            "impliedOnSuccess": implied_values(success.get("criteria") or {}, context,
                                               owned=_owned_names(raw)),
            "varsOnSuccess": self._branch_vars(success, context),
            "varsOnFailure": self._branch_vars(failure or warning, context),
            # the raw templates, for the page to interpolate when the branch runs
            "branchVars": {"success": _raw_vars(success),
                           "failure": _raw_vars(failure),
                           "warning": _raw_vars(warning)},
        }
        return out

    @staticmethod
    def _branch_vars(branch, context):
        """
        applyValidation(): every value is interpolated against the context as
        it stood BEFORE any of them is set, then all of them are set.
        """
        items = branch_var_items(branch)
        return dict((name, interpolate_object(context, value) if not isinstance(value, str)
                     else context.interpolate(value, record_unresolved=False))
                    for name, value in items)

    def _on_failure(self, raw):
        """
        handleFailure(), normalised for the page:

            {"mode": "stop" | "continue" | "warning" | "email" | "run",
             "run": [targets], "then": [targets], "next": "continue" | None,
             "email": bool}

        A map without `next: continue` stops after its handlers - even when the
        handlers succeed. `mode` says which way the step goes once they ran.
        """
        behaviour = raw.get("on_failure", self.defaults.get("on_failure"))
        if behaviour is None:
            return {"mode": "stop", "raw": None}
        if isinstance(behaviour, dict):
            run = behaviour.get("run")
            run = [str(r) for r in run] if isinstance(run, list) else ([str(run)] if run else [])
            then = behaviour.get("then")
            then = [str(t) for t in then] if isinstance(then, list) else \
                   ([str(then)] if then else [])
            nxt = "continue" if str(behaviour.get("next") or "").strip().lower() == "continue" \
                else None
            email = "email" in behaviour and behaviour.get("email") is not False
            out = {"mode": "run" if run else ("continue" if nxt else "stop"),
                   "run": run, "then": then, "next": nxt, "email": email}
            if run:
                out["step"] = run
            return out
        text = str(behaviour).strip()
        low = text.lower()
        return {"mode": low if low in ("continue", "warning", "email", "stop") else "stop",
                "raw": text}

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
        tokens = _names_in(expression, context)
        missing = [t for t in tokens if context.resolve_name(t) is None]
        result, trace = context.evaluate_traced(expression)
        if missing:
            return _UNKNOWN, trace
        return (_TRUE if result else _FALSE), trace


def _names_in(expression, context=None):
    """The variables a condition reads."""
    text = str(expression or "").strip()
    if text.startswith("${") and text.endswith("}"):
        # The whole condition is one ${...} wrapper, so the placeholder
        # pattern returns the entire expression as a single "token"
        # ('TargetNodeRDS == ""'), which resolves to nothing and made every
        # such gate look undecidable. Read the identifiers out of the
        # expression instead.
        return [t.split(".")[0].split("[")[0] if context is None else t
                for t in _bare_names(text[2:-1])]
    tokens = (context or Context({})).tokens_in(text)
    return tokens if context is not None else \
        [t.split(".")[0].split("[")[0] for t in tokens]


def _status_literals(expression):
    """3-digit HTTP statuses a gate compares a status variable against."""
    import re
    out = []
    for name, value in re.findall(r"([A-Za-z_]*status[A-Za-z_]*)\s*==\s*['\"]?(\d{3})['\"]?",
                                  str(expression or ""), re.I):
        if int(value) not in out:
            out.append(int(value))
    return out


def _interpolated_register(register, context):
    out = []
    for entry in register or []:
        if isinstance(entry, dict) and isinstance(entry.get("regex"), str):
            entry = dict(entry)
            entry["regex"] = context.interpolate(entry["regex"], record_unresolved=False)
        out.append(entry)
    return out


def _owned_names(raw):
    """The variables a step's own registers and REST response set."""
    out = set()
    for entry in raw.get("register") or []:
        if isinstance(entry, dict):
            out |= set(group_names(str(entry.get("regex") or "")))
            for key in ("count_var", "name"):
                if entry.get(key):
                    out.add(str(entry[key]))
    rest = raw.get("rest")
    if isinstance(rest, dict):
        for rule in rest.get("response_template") or []:
            if isinstance(rule, dict) and rule.get("name"):
                out.add(str(rule["name"]))
    return out


def _raw_vars(branch):
    return dict(branch_var_items(branch)) if branch_var_items(branch) else {}


def _promote_niam(variables, node):
    """CliAutomationEngine.promoteNiamId() + the niamID/niamId/NIAM_ID aliases."""
    niam = next((node.get(k) for k in ("niamID", "niamId", "NIAM_ID")
                 if node.get(k) is not None), None)
    if niam is None:
        niam = _find_nested(node, ("niamID", "niamId", "NIAM_ID"))
    if niam is not None:
        for key in ("niamID", "niamId", "NIAM_ID"):
            variables[key] = niam


def _find_nested(value, keys):
    if isinstance(value, dict):
        for key in keys:
            if value.get(key) is not None:
                return value[key]
        for child in value.values():
            found = _find_nested(child, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_nested(child, keys)
            if found is not None:
                return found
    return None


def _jsonable(value):
    """Loop items travel to the page as JSON; YAML dates and the like do not."""
    if isinstance(value, dict):
        return dict((str(k), _jsonable(v)) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


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

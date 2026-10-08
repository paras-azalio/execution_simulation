#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
lint.py - what the engine will do with a workflow that its author did not mean
=============================================================================

    python lint.py <workflow.yaml> [--ciq CIQ.json] [--json]
    python lint.py --all                      every workflow in templates/yaml

Walking a workflow the way the engine does turns up things no reviewer sees,
because they are only visible once you know what ExecutionContext and
ExecutionOrchestrator actually do with the text. Each finding names the java
behaviour behind it:

  error    the engine will not do what the YAML says
  warning  very likely not what was meant
  info     worth knowing, often deliberate

Every rule here was found in the templates/yaml of 2026-09 - see RULES.
template/lint.js is the browser twin, so the runner reports the same findings
for a file dropped into it; test_expander_parity.py compares the two.
"""

import argparse
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ciq as ciq_mod                                      # noqa: E402
from engine import PLACEHOLDER, java_regex_error, split_top_level, \
    find_operator_outside_quotes                           # noqa: E402
from expander import (assign_step_ids, runtime_producers,  # noqa: E402
                      branch_var_items, _bare_names)

RULES = {
    "shell-expansion": "a shell ${VAR#...} / ${VAR:-...} / ${#VAR} is read by the engine as its own "
                       "placeholder and replaced (usually with nothing) before the shell sees it",
    "tautology": "criteria that cannot fail - a clause and its negation OR-ed together",
    "expr-in-all-any": "an all[]/any[] item is matched as a result attribute; an `expr:` there is "
                       "compared to an attribute called \"expr\" and is always false",
    "criteria-regex-placeholder": "a criteria regex is NOT interpolated, and java.util.regex refuses "
                                  "the '{' of ${...}",
    "java-regex": "java.util.regex rejects this pattern; the step fails with an exception",
    "python-group-syntax": "(?P<name>) is python syntax; java.util.regex rejects it",
    "run-without-next": "on_failure: run: without next: continue stops the run after the "
                        "handlers, even when the handlers succeed",
    "handler-not-found": "an on_failure / then: target that does not exist - the engine silently "
                         "does nothing",
    "phase-stop-blind": "phase-level on_failure: stop only sees failures of the phase's top-level "
                        "steps and loops (phaseHadAnyFailure is local to each executeSteps call)",
    "use-exit-code-ignored": "use_exit_code is never read by the engine, and a step-level "
                             "prompt_regex switches the exit-code check off",
    "exit-code-variable": "the exit code is a result attribute, never a ${variable}",
    "parentheses": "conditions have no parentheses: splitTopLevel() splits || before && and a "
                   "'(' becomes part of a variable name",
    "unknown-node": "a step names a node the workflow does not define - validateWorkflow() refuses "
                    "to load it",
    "vars-list": "validation vars must be a mapping (ValidationBranchDefinition.vars is a Map); "
                 "SnakeYAML cannot bind a list",
    "loop-gate-item-var": "a loop's when/skip_when is evaluated ONCE, before the first item is "
                          "bound, so a gate on the loop's own item_var reads the previous value",
    "undefined-variable": "a condition reads a variable nothing ever sets - it compares as \"\"",
    "duplicate-step-id": "two steps share an id: on_failure run: and the phase lookup find only one",
    "duplicate-key": "a mapping key appears twice; SnakeYAML keeps the LAST and silently drops the "
                     "earlier block",
    "rest-no-base-url": "a REST step whose node has no rest.base_url",
}

_CONDITION_FIELDS = ("when", "skip_when", "continue_when", "break_when")
_TEXT_FIELDS = ("send", "command_description", "description", "message", "value",
                "path", "body_json", "body_file", "local_path", "remote_path", "subject",
                "body_text", "body_html")
_SHELL_TOKEN = re.compile(r"^[#!]|[#%/^,]|:[-=+?]|\[[@*]\]")
_KNOWN_RUNTIME = set("""
NODE_NAME nodeName node metadata nodeGroup crGroup nodeGroups nodes node_row niamID niamId
NIAM_ID currentNode currentCrGroup currentNodeData currentNodeCollection VERSION version
http_status __HAS_CONTINUED_FAILURE ACTIVITY_EXECUTION_STATUS ACTIVITY_COMMAND_SUCCESS_LIST
ACTIVITY_COMMAND_FAILURE_LIST ACTIVITY_COMMAND_SKIPPED_LIST LAST_ACTIVITY_EXECUTED
ROLLBACK_ENABLED nodeType activity tableKeys schemaVersion meta step execution phase failure
true false null
""".split())
_WORD_OPERATORS = {"contains", "notContains", "startsWith", "notStartsWith"}


class Finding(object):
    __slots__ = ("rule", "severity", "message", "where")

    def __init__(self, rule, severity, message, where=""):
        self.rule, self.severity, self.message, self.where = rule, severity, message, where

    def as_dict(self):
        return {"rule": self.rule, "severity": self.severity,
                "message": self.message, "where": self.where}


_ORDER = {"error": 0, "warning": 1, "info": 2}


# --------------------------------------------------------------------------- #
#  the walk
# --------------------------------------------------------------------------- #
def lint(workflow, data=None, params=None, duplicate_keys=None):
    """Findings for one loaded workflow, most severe first."""
    workflow = workflow or {}
    out = []
    ids = assign_step_ids(workflow)
    producers = runtime_producers(workflow)
    nodes = workflow.get("nodes") or {}
    phases = [(k, v) for k, v in (workflow.get("phases") or {}).items() if isinstance(v, dict)]
    phase_ids = set(str(v.get("name") or k).strip().lower() for k, v in phases)
    all_steps = []                                     # (step, phase key, loop stack)

    def walk(steps, phase, stack):
        for step in steps or []:
            if not isinstance(step, dict):
                continue
            all_steps.append((step, phase, stack))
            if step.get("type") == "loop":
                walk(step.get("steps"), phase, stack + [step])

    walk(workflow.get("steps"), None, [])
    for key, block in phases:
        walk(block.get("steps"), key, [])
    by_id = {}
    for step, _p, _s in all_steps:
        by_id.setdefault(ids.get(id(step)), []).append(step)

    known = set(_KNOWN_RUNTIME) | set(producers) | set(ciq_mod.DEFAULT_PARAMS) | set(params or {})
    known |= set(((workflow.get("globals") or {}).get("vars") or {}).keys())
    known |= set("SECRET.%s" % k for k in ((workflow.get("globals") or {}).get("secrets") or {}))
    for step, _p, _s in all_steps:
        if step.get("type") == "loop" and step.get("item_var"):
            known.add(str(step["item_var"]))
    for _k, block in phases:
        known |= set((block.get("vars") or {}).keys())
    if isinstance(data, dict):
        known |= set(data.keys())
        for node in data.get("nodes") or []:
            if isinstance(node, dict):
                known |= set(node.keys())

    def add(rule, severity, message, where=""):
        out.append(Finding(rule, severity, message, where))

    run_no_next, exit_ignored = [], []
    for step, phase, stack in all_steps:
        sid = ids.get(id(step))
        where = "step %s" % sid + (" (phase %s)" % phase if phase else "")

        # -- text fields: shell expansions the engine swallows ----------------
        for field, text in _strings_of(step, _TEXT_FIELDS):
            for token in PLACEHOLDER.findall(text):
                bare = token.strip()
                if bare.startswith(("base64:", "ENV.", "SECRET.")):
                    continue
                if _SHELL_TOKEN.search(bare):
                    add("shell-expansion", "error",
                        "${%s} in %s is replaced by the engine before the shell runs - it "
                        "becomes \"\"; write $%s or escape it" % (bare, field, _shell_name(bare)),
                        where)

        # -- conditions --------------------------------------------------------
        for field in _CONDITION_FIELDS:
            expr = step.get(field)
            if not isinstance(expr, str) or not expr.strip():
                continue
            _check_condition(add, expr, "%s %s" % (where, field), known)
        if step.get("type") == "loop":
            item = step.get("item_var")
            for field in ("when", "skip_when"):
                expr = step.get(field)
                if isinstance(expr, str) and item and _reads(expr, item):
                    add("loop-gate-item-var", "error",
                        "loop %s gates on its own item_var %s in %s - evaluated once, before "
                        "any item is bound" % (sid, item, field), where)

        # -- nodes ------------------------------------------------------------
        node = step.get("node")
        if step.get("type") != "loop" and isinstance(node, str) and node.strip() \
                and "${" not in node and node.strip().lower() != "local" and node not in nodes:
            add("unknown-node", "error",
                "node %s is not defined under nodes: - the workflow will not load" % node, where)
        rest = step.get("rest")
        if isinstance(rest, dict) and isinstance(node, str) and node in nodes:
            if not ((nodes.get(node) or {}).get("rest") or {}).get("base_url"):
                add("rest-no-base-url", "warning", "node %s has no rest.base_url" % node, where)

        # -- registers --------------------------------------------------------
        for entry in step.get("register") or []:
            if not isinstance(entry, dict) or not isinstance(entry.get("regex"), str):
                continue
            pattern = entry["regex"]
            if "(?P<" in pattern:
                add("python-group-syntax", "error",
                    "register regex %s uses (?P<name>) - java needs (?<name>)" % _clip(pattern), where)
            reason = java_regex_error(PLACEHOLDER.sub("x", pattern))
            if reason:
                add("java-regex", "error", "register regex %s: %s" % (_clip(pattern), reason), where)

        # -- validation -------------------------------------------------------
        validation = step.get("validation")
        if isinstance(validation, dict):
            for branch in ("success", "failure", "warning"):
                block = validation.get(branch)
                if not isinstance(block, dict):
                    continue
                if isinstance(block.get("vars"), list):
                    add("vars-list", "error",
                        "validation %s vars is a list - it has to be a mapping" % branch, where)
                criteria = block.get("criteria")
                if isinstance(criteria, dict):
                    _check_criteria(add, criteria, "%s %s criteria" % (where, branch),
                                    branch == "success", known)
                    expr = criteria.get("expr")
                    if isinstance(expr, str):
                        _check_condition(add, expr, "%s %s criteria" % (where, branch), known)

        # -- on_failure -------------------------------------------------------
        behaviour = step.get("on_failure")
        if isinstance(behaviour, dict):
            run = behaviour.get("run")
            run = run if isinstance(run, list) else ([run] if run else [])
            then = behaviour.get("then")
            then = then if isinstance(then, list) else ([then] if then else [])
            for target in run + then:
                _check_target(add, str(target), by_id, phase_ids, where)
            if run and str(behaviour.get("next") or "").lower() != "continue":
                run_no_next.append(sid)

        # -- exit code --------------------------------------------------------
        prompt = step.get("prompt_regex")
        if step.get("use_exit_code") is True and isinstance(prompt, str) and prompt.strip():
            exit_ignored.append(sid)

    if run_no_next:
        add("run-without-next", "warning",
            "%d step(s) run on_failure handlers without `next: continue` - after the handlers "
            "(a token refresh and a retry, say) the engine STOPS even if they succeeded: %s"
            % (len(run_no_next), _few(run_no_next)))
    if exit_ignored:
        add("use-exit-code-ignored", "warning",
            "%d step(s) set use_exit_code: true and a step-level prompt_regex - the engine never "
            "reads use_exit_code and does NOT check their exit code: %s"
            % (len(exit_ignored), _few(exit_ignored)))

    # -- phases ---------------------------------------------------------------
    for key, block in phases:
        then = block.get("then")
        then = then if isinstance(then, list) else ([then] if then else [])
        for target in then:
            _check_target(add, str(target), by_id, phase_ids, "phase %s then:" % key)
        if str(block.get("on_failure") or "").strip() == "stop":
            blind = _blind_spots(block.get("steps") or [], ids)
            if blind:
                add("phase-stop-blind", "warning",
                    "phase %s declares on_failure: stop, but the engine never sees a failure "
                    "absorbed inside a loop - %s. A failure there lets the run go on to the next "
                    "phase." % (block.get("name") or key, "; ".join(blind[:4]) +
                                (" ..." if len(blind) > 4 else "")),
                    "phase %s" % key)

    # -- ids and keys -----------------------------------------------------------
    for sid, steps in sorted(by_id.items(), key=lambda kv: str(kv[0])):
        if len(steps) > 1:
            add("duplicate-step-id", "warning",
                "id %s is used by %d steps - on_failure run: finds only the last" % (sid, len(steps)))
    for text in duplicate_keys or []:
        add("duplicate-key", "warning", text)

    out.sort(key=lambda f: _ORDER.get(f.severity, 9))
    return out


def _check_target(add, target, by_id, phase_ids, where):
    name, kind = target.strip(), None
    if name.lower().startswith("step:"):
        kind, name = "step", name[5:].strip()
    elif name.lower().startswith("phase:"):
        kind, name = "phase", name[6:].strip()
    if "${" in name:
        return
    if kind == "step" and name not in by_id:
        add("handler-not-found", "error", "on_failure target step:%s does not exist" % name, where)
    elif kind == "phase" and name.lower() not in phase_ids:
        add("handler-not-found", "error",
            "target phase:%s matches no phase id (a phase's id is its name:)" % name, where)
    elif kind is None and name not in by_id and name.lower() not in phase_ids:
        add("handler-not-found", "error", "target %s is neither a step id nor a phase" % name, where)


def _check_criteria(add, criteria, where, is_success, known):
    pattern = criteria.get("regex")
    if isinstance(pattern, str):
        _check_criteria_regex(add, pattern, where)
    for key in ("all", "any"):
        for item in criteria.get(key) or []:
            if not isinstance(item, dict):
                continue
            if "expr" in item:
                add("expr-in-all-any", "error",
                    "%s[] item `expr: %s` is compared to a result attribute named \"expr\" and "
                    "is always false - move it up to the criteria's own expr" % (key, _clip(item["expr"])),
                    where)
            if isinstance(item.get("regex"), str):
                _check_criteria_regex(add, item["regex"], where)
    expr = criteria.get("expr")
    if is_success and isinstance(expr, str) and _tautology(expr):
        add("tautology", "warning",
            "criteria %s is true whatever the command prints - the step can never fail on it"
            % _clip(expr), where)


def _check_criteria_regex(add, pattern, where):
    if "${" in pattern:
        add("criteria-regex-placeholder", "error",
            "criteria regex %s: criteria regexes are not interpolated, and java rejects the '{'"
            % _clip(pattern), where)
        return
    if "(?P<" in pattern:
        add("python-group-syntax", "error", "criteria regex %s uses (?P<name>)" % _clip(pattern), where)
    reason = java_regex_error(pattern)
    if reason:
        add("java-regex", "error", "criteria regex %s: %s" % (_clip(pattern), reason), where)


def _check_condition(add, expr, where, known):
    text = expr.strip()
    inner = text[2:-1] if text.startswith("${") and text.endswith("}") else text
    if "(" in _unquoted(inner):
        add("parentheses", "error",
            "%s uses parentheses, which conditions do not support" % _clip(expr), where)
    for left, right in _operands(inner):
        for side, operand in (("left", left), ("right", right)):
            if operand is None:
                continue
            raw = operand.strip()
            if raw.startswith("${") and raw.endswith("}"):
                raw = raw[2:-1].strip()
            if not raw or raw[0] in "'\"" or raw[0].isdigit() or raw[0] == "-":
                continue
            root = raw.split(".")[0].split("[")[0].strip("() ")
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", root):
                continue
            if root in ("exitCode", "exit_code", "EXIT_CODE"):
                add("exit-code-variable", "error",
                    "%s reads %s, which the engine never sets - use criteria exit_code: 0"
                    % (_clip(expr), root), where)
            elif root in known or "." in raw or root.startswith("SECRET"):
                continue
            elif side == "left":
                add("undefined-variable", "warning",
                    "%s reads %s, which no step, global, parameter or CIQ field sets - it "
                    "compares as \"\"" % (_clip(expr), root), where)
            elif "_" in root:
                # resolveConditionOperand(): an unknown bare word is taken as
                # the literal text itself
                add("undefined-variable", "warning",
                    "%s compares against %s, which nothing sets - so against the literal "
                    "text \"%s\"" % (_clip(expr), root, root), where)


def _operands(inner):
    """(left, right) of every leaf, split the way evaluateCondition() splits."""
    out = []
    for clause in split_top_level(inner, "||"):
        for leaf in split_top_level(clause, "&&"):
            leaf = leaf.strip()
            found = False
            for op in (" notStartsWith ", " startsWith ", " notContains ", " contains "):
                at = leaf.find(op)
                if at > 0:
                    out.append((leaf[:at], leaf[at + len(op):]))
                    found = True
                    break
            if found:
                continue
            for op in ("==", "!=", ">=", "<=", ">", "<"):
                at = find_operator_outside_quotes(leaf, op)
                if at > 0:
                    out.append((leaf[:at], leaf[at + len(op):]))
                    found = True
                    break
            if not found:
                out.append((leaf, None))
    return out


def _tautology(expr):
    """`X == "a" || X != "a"` in any order, among the OR clauses."""
    text = expr.strip()
    if text.startswith("${") and text.endswith("}"):
        text = text[2:-1]
    eq, ne = set(), set()
    for clause in split_top_level(text, "||"):
        if len(split_top_level(clause, "&&")) > 1:
            continue
        for op, bucket in (("==", eq), ("!=", ne)):
            at = find_operator_outside_quotes(clause, op)
            if at > 0:
                bucket.add((clause[:at].strip(), clause[at + 2:].strip()))
                break
    return bool(eq & ne)


def _blind_spots(steps, ids):
    """Where a failure is absorbed inside a loop and never reaches the phase check."""
    out = []

    def walk(items, depth):
        for step in items or []:
            if not isinstance(step, dict):
                continue
            mode = step.get("on_failure")
            mode = mode.strip().lower() if isinstance(mode, str) else ""
            if step.get("type") == "loop":
                if depth > 0 and mode in ("continue", "warning"):
                    out.append("loop %s (on_failure: %s)" % (ids.get(id(step)), mode))
                walk(step.get("steps"), depth + 1)
            elif depth > 0 and mode in ("continue", "warning"):
                out.append("step %s (on_failure: %s)" % (ids.get(id(step)), mode))
    walk(steps, 0)
    return out


def _reads(expr, name):
    text = expr.strip()
    inner = text[2:-1] if text.startswith("${") and text.endswith("}") else text
    names = _bare_names(inner) if inner is not text else [t.strip() for t in PLACEHOLDER.findall(text)]
    return any(n.split(".")[0].split("[")[0] == name for n in names)


def _unquoted(text):
    out, quote = [], None
    for char in text:
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
            continue
        out.append(char)
    return "".join(out)


def _strings_of(step, fields):
    """(field, text) for the text fields of one step, including rest/sftp/email/validation."""
    out = []
    for field in fields:
        value = step.get(field)
        if isinstance(value, str):
            out.append((field, value))
    for key in ("rest", "sftp", "email"):
        block = step.get(key)
        if isinstance(block, dict):
            for field in fields:
                if isinstance(block.get(field), str):
                    out.append(("%s.%s" % (key, field), block[field]))
    for entry in step.get("register") or []:
        if isinstance(entry, dict) and isinstance(entry.get("value"), str):
            out.append(("register value", entry["value"]))
    validation = step.get("validation")
    if isinstance(validation, dict):
        for branch in ("success", "failure", "warning"):
            block = validation.get(branch)
            if isinstance(block, dict):
                if isinstance(block.get("message"), str):
                    out.append(("%s message" % branch, block["message"]))
                for name, value in branch_var_items(block):
                    if isinstance(value, str):
                        out.append(("%s vars %s" % (branch, name), value))
    return out


def _shell_name(token):
    match = re.match(r"^[#!]?([A-Za-z_][A-Za-z0-9_]*)", token)
    return "{%s}" % token if not match else match.group(1)


def _clip(text, width=70):
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[:width - 3] + "..."


def _few(ids, n=6):
    return ", ".join(str(i) for i in ids[:n]) + (" ..." if len(ids) > n else "")


# --------------------------------------------------------------------------- #
#  duplicate keys - what SnakeYAML silently resolves
# --------------------------------------------------------------------------- #
def duplicate_keys(text):
    """'key X repeated at line N (first at line M)' for every duplicated key."""
    import yaml

    found = []

    base = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

    class Loader(base):
        pass

    def construct(loader, node, deep=False):
        seen = {}
        for key_node, _value in node.value:
            try:
                key = loader.construct_object(key_node, deep=deep)
            except Exception:                              # noqa: BLE001
                continue
            try:
                hash(key)
            except TypeError:
                continue
            line = key_node.start_mark.line + 1
            if key in seen:
                found.append("key %r repeated at line %d (first at line %d) - the earlier block "
                             "is dropped" % (key, line, seen[key]))
            else:
                seen[key] = line
        return base.construct_mapping(loader, node, deep)

    Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct)
    try:
        yaml.load(text, Loader=Loader)
    except yaml.YAMLError:
        pass
    return found


def lint_file(path, data=None, params=None, workflow=None):
    text = io.open(path, encoding="utf-8").read()
    if workflow is None:
        workflow = ciq_mod.read_yaml(path)
    return lint(workflow, data, params, duplicate_keys(text))


# --------------------------------------------------------------------------- #
#  cli
# --------------------------------------------------------------------------- #
def main(argv=None):
    p = argparse.ArgumentParser(description="Report what the engine will do with a workflow "
                                            "that its author did not mean.")
    p.add_argument("yaml", nargs="*", help="workflow YAML(s)")
    p.add_argument("--all", action="store_true", help="every workflow in templates/yaml")
    p.add_argument("--ciq", default="", help="a CIQ, so its fields count as defined")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    paths = list(args.yaml)
    if args.all:
        import runall
        paths += sorted(os.path.join(runall.DEFAULT_YAML_DIR, f)
                        for f in os.listdir(runall.DEFAULT_YAML_DIR) if f.endswith(".yaml"))
    if not paths:
        p.error("name a workflow, or --all")
    data = ciq_mod.load_ciq(args.ciq) if args.ciq else None

    report, errors = {}, 0
    for path in paths:
        findings = lint_file(path, data)
        report[os.path.basename(path)] = [f.as_dict() for f in findings]
        errors += sum(1 for f in findings if f.severity == "error")
        if args.json:
            continue
        print("%s  %d finding(s)" % (os.path.basename(path), len(findings)))
        for f in findings:
            print("  %-7s %-26s %s%s" % (f.severity, f.rule, f.message,
                                         ("  [%s]" % f.where) if f.where else ""))
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

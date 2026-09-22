#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
engine.py
=========

A faithful python port of the CLICR engine's variable and condition semantics,
so a MOP generated here resolves exactly what a real run would resolve.

Ported from, and deliberately bug-compatible with:

    cliautomation/exec/ExecutionContext.java
        interpolate()            :120-133   unresolved ${...} -> EMPTY STRING
        evaluateCondition()      :303-414   || split before &&, NO parentheses
        splitTopLevel()          :476-508   quote-aware, paren-blind
        safeEquals()             :646-654   case SENSITIVE
        resolveForEachValue()    :281-301   a bare "${x}" resolves to the OBJECT
        findOperatorOutsideQuotes()         '>'/'<' inside quotes are literals
    cliautomation/exec/ResultProcessor.java
        register names are used verbatim, never interpolated (:68)
    cliautomation/exec/ExecutionOrchestrator.java
        executeLoopStep()        :573-605   continue_when runs the body when TRUE

Every quirk below is intentional. The point of this module is not to be a nicer
expression language - it is to predict what the node will actually be sent.
"""

import base64
import json
import os
import re

# ${...} placeholder. Stops at the first closing brace, which is why a nested
# ${LDAPFIELDS${imsi}} can never work in a workflow (see the 1051 YAML comment).
PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")

# Java named groups are (?<NAME>...); python needs (?P<NAME>...). Workflows in
# this repo use both spellings, so accept either.
_JAVA_GROUP = re.compile(r"\(\?<([A-Za-z][A-Za-z0-9]*)>")

# Operators, longest first - the order the java evaluator tries them in.
_WORD_OPS = (" notStartsWith ", " startsWith ", " notContains ", " contains ")
_CMP_OPS = ("==", "!=", ">=", "<=", ">", "<")


def to_java_regex(pattern):
    """Rewrite Java-style named groups to python ones."""
    return _JAVA_GROUP.sub(r"(?P<\1>", pattern or "")


def _is_number(text):
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


class Unresolved(object):
    """
    One ${...} that resolved to nothing.

    The engine substitutes an empty string, which is exactly how a real run
    behaves - but a MOP has to SHOW the operator that a value is missing rather
    than silently render a blank. Every unresolved reference is recorded so the
    page can turn it into an input field.
    """

    __slots__ = ("token", "reason")

    def __init__(self, token, reason):
        self.token = token
        self.reason = reason


class Context(object):
    """
    Runtime variable scope: globals.vars + secrets + the CIQ data section,
    plus everything registered as the walk proceeds.
    """

    def __init__(self, variables=None, secrets=None, env=None):
        self.vars = dict(variables or {})
        self.secrets = dict(secrets or {})
        self.env = dict(env if env is not None else os.environ)
        self.unresolved = []          # [Unresolved] seen since the last reset

    # -- scope ---------------------------------------------------------------
    def copy(self):
        clone = Context(self.vars, self.secrets, self.env)
        return clone

    def put(self, name, value):
        self.vars[name] = value

    def get(self, name):
        return self.vars.get(name)

    # -- resolution ----------------------------------------------------------
    def resolve_name(self, name):
        """
        ExecutionContext.resolveForVariableName(). Returns None when nothing
        matches - the caller decides whether that becomes "" or an input field.
        """
        name = (name or "").strip()
        if name.startswith("ENV."):
            return self.env.get(name[4:])
        if name.startswith("SECRET."):
            return self.secrets.get(name[7:])
        if name.startswith("base64:"):
            inner = self.resolve_name(name[7:])
            if inner is None:
                return None
            raw = inner if isinstance(inner, str) else str(inner)
            return base64.b64encode(raw.encode("utf-8")).decode("ascii")
        return self._walk_path(name)

    def _walk_path(self, path):
        """
        Dotted path over maps and lists, tolerating keys that contain spaces
        ("rec_row.data.Test IMSI") and list indexes ("vars.hosts[0]").

        A key with a dot in it would be ambiguous; the engine resolves greedily
        from the left, and so does this, but it also tries progressively longer
        keys so "meta.activity" still works when a map holds the literal key
        "meta.activity".
        """
        if path in self.vars:
            return self.vars[path]
        parts = path.split(".")
        current = None
        matched = False
        for cut in range(len(parts), 0, -1):
            head = ".".join(parts[:cut])
            if head in self.vars:
                current, matched, rest = self.vars[head], True, parts[cut:]
                break
            # the head itself may carry an index - "hosts[1]", "nodes[0].PGW"
            bracket = re.match(r"^(.*?)\[(\d+)\]$", head)
            if bracket and bracket.group(1) in self.vars:
                current, ok = self._step_into(self.vars[bracket.group(1)],
                                              "[%s]" % bracket.group(2))
                if not ok:
                    return None
                matched, rest = True, parts[cut:]
                break
        if not matched:
            return None
        for part in rest:
            current, ok = self._step_into(current, part)
            if not ok:
                return None
        return current

    def _step_into(self, current, part):
        index = None
        match = re.match(r"^(.*?)\[(\d+)\]$", part)
        if match:
            part, index = match.group(1), int(match.group(2))
        if part:
            if isinstance(current, dict):
                if part not in current:
                    return None, False
                current = current[part]
            else:
                return None, False
        if index is not None:
            if not isinstance(current, (list, tuple)) or index >= len(current):
                return None, False
            current = current[index]
        return current, True

    # -- interpolation -------------------------------------------------------
    def interpolate(self, raw, record_unresolved=True):
        """
        ExecutionContext.interpolate(): every ${...} becomes its resolved value
        or an EMPTY STRING. Unresolved tokens are recorded, not flagged inline,
        so the rendered command stays byte-identical to what a run would send.
        """
        if raw is None:
            return None
        if not isinstance(raw, str):
            return raw
        if "${" not in raw:
            return raw

        def repl(match):
            token = match.group(1).strip()
            value = self.resolve_name(token)
            if value is None:
                if record_unresolved:
                    self.unresolved.append(Unresolved(token, self._why(token)))
                return ""
            return value if isinstance(value, str) else self._stringify(value)

        return PLACEHOLDER.sub(repl, raw)

    def tokens_in(self, raw):
        """Every ${...} token in a string, in order, deduplicated."""
        if not isinstance(raw, str):
            return []
        seen, out = set(), []
        for token in PLACEHOLDER.findall(raw):
            token = token.strip()
            if token not in seen:
                seen.add(token)
                out.append(token)
        return out

    def _why(self, token):
        if token.startswith("ENV."):
            return "environment variable %s is not set" % token[4:]
        if token.startswith("SECRET."):
            return "secret %s was not supplied" % token[7:]
        if token in self.vars:
            return "%s is empty" % token
        root = token.split(".")[0].split("[")[0]
        if root in self.vars:
            return "%s has no member %s" % (root, token[len(root) + 1:])
        return "%s is not defined" % token

    @staticmethod
    def _stringify(value):
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            text = repr(value)
            return text[:-2] if text.endswith(".0") else text
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, ensure_ascii=False)
        return "" if value is None else str(value)

    # -- conditions ----------------------------------------------------------
    def evaluate(self, expression):
        """
        ExecutionContext.evaluateCondition(). An empty or missing expression is
        TRUE, matching the java default.
        """
        ok, _trace = self.evaluate_traced(expression)
        return ok

    def evaluate_traced(self, expression):
        """As evaluate(), plus the per-clause trace the debug panel shows."""
        if expression is None or not str(expression).strip():
            return True, []
        expr = self._unwrap(str(expression))
        trace = []
        result = self._eval(expr, trace)
        return result, trace

    @staticmethod
    def _unwrap(expression):
        expr = expression.strip()
        if expr.startswith("${") and expr.endswith("}"):
            expr = expr[2:-1].strip()
        return expr

    def _eval(self, expr, trace):
        or_parts = split_top_level(expr, "||")
        if len(or_parts) > 1:
            for part in or_parts:
                if self._eval(part, trace):
                    return True
            return False

        and_parts = split_top_level(expr, "&&")
        if len(and_parts) > 1:
            for part in and_parts:
                if not self._eval(part, trace):
                    return False
            return True

        return self._eval_leaf(expr.strip(), trace)

    def _eval_leaf(self, expr, trace):
        # stringValue() (ExecutionContext.java:642) turns an unset variable into
        # the EMPTY STRING before any comparison, so `${MISSING == ""}` is true
        # and no operator ever sees a null. The trace still shows <unset>, which
        # is how the debug panel tells "empty" from "never defined" apart.
        for op in _WORD_OPS:
            at = expr.find(op)
            if at > 0:
                left_raw, right_raw = expr[:at].strip(), expr[at + len(op):].strip()
                left, shown = self._string_of(left_raw)
                right = self.operand(right_raw)
                name = op.strip()
                if name == "notStartsWith":
                    result = not left.startswith(right)
                elif name == "startsWith":
                    result = left.startswith(right)
                elif name == "notContains":
                    result = right not in left
                else:
                    result = right in left
                trace.append((expr, left_raw, shown, right, result))
                return result

        for op in _CMP_OPS:
            at = find_operator_outside_quotes(expr, op)
            if at > 0:
                left_raw, right_raw = expr[:at].strip(), expr[at + len(op):].strip()
                left, shown = self._string_of(left_raw)
                right = self.operand(right_raw)
                if op == "==":
                    result = left == right
                elif op == "!=":
                    result = left != right
                else:
                    cmp = compare_values(left, right)
                    result = {">=": cmp >= 0, "<=": cmp <= 0,
                              ">": cmp > 0, "<": cmp < 0}[op]
                trace.append((expr, left_raw, shown, right, result))
                return result

        raw = self.resolve_name(expr)
        if isinstance(raw, bool):
            result = raw
        else:
            result = raw is not None and str(raw).lower() != "false"
        trace.append((expr, expr, None if raw is None else self._stringify(raw), "", result))
        return result

    def _string_of(self, name):
        """(value for comparison, value for the debug trace)."""
        value = self.resolve_name(name)
        if value is None:
            return "", None
        text = self._stringify(value)
        return text, text

    def operand(self, text):
        """
        Right-hand side of a comparison: a quoted literal stays literal, a bare
        word is resolved as a variable and falls back to itself.
        """
        text = (text or "").strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
            return text[1:-1]
        value = self.resolve_name(text)
        return text if value is None else self._stringify(value)

    # -- for_each ------------------------------------------------------------
    def resolve_for_each(self, raw):
        """
        ExecutionContext.resolveForEachValue() + ExecutionOrchestrator.toIterable().

        A bare "${nodeGroups}" yields the LIST itself; any other string is
        interpolated to text; a YAML list literal is returned as-is; anything
        that is not iterable becomes a one-element list (the "[1]" wrapper idiom).
        """
        if raw is None:
            return []
        if isinstance(raw, str):
            trimmed = raw.strip()
            if trimmed.startswith("${") and trimmed.endswith("}"):
                value = self.resolve_name(trimmed[2:-1].strip())
            else:
                value = self.interpolate(raw, record_unresolved=False)
        else:
            value = raw
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return list(value)
        return [value]


def split_top_level(expr, operator):
    """
    ExecutionContext.splitTopLevel(): quote-aware, parenthesis-BLIND. Writing
    "(a || b) && c" in a workflow does not group - the '(' ends up inside a
    variable name. Conditions must be an OR of AND-chains.
    """
    parts = []
    if not expr:
        return parts
    index = last = 0
    in_single = in_double = False
    while index < len(expr):
        char = expr[index]
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        if not in_single and not in_double and expr.startswith(operator, index):
            piece = expr[last:index].strip()
            if piece:
                parts.append(piece)
            index += len(operator)
            last = index
            continue
        index += 1
    tail = expr[last:].strip()
    if tail:
        parts.append(tail)
    return parts


def find_operator_outside_quotes(expr, op):
    """ExecutionContext.findOperatorOutsideQuotes(): -1 when only inside quotes."""
    if not expr or not op:
        return -1
    in_single = in_double = False
    for i in range(0, len(expr) - len(op) + 1):
        char = expr[i]
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if not in_single and not in_double and expr.startswith(op, i):
            return i
    return -1


def compare_values(left, right):
    """Numeric when both sides parse as numbers, else lexicographic."""
    left = "" if left is None else str(left)
    right = "" if right is None else str(right)
    if _is_number(left) and _is_number(right):
        a, b = float(left), float(right)
        return (a > b) - (a < b)
    return (left > right) - (left < right)


def apply_registers(context, register, output, record=None):
    """
    ResultProcessor: apply a step's `register` list to one command's output.

    Entry forms, in the order the workflow uses them:
        - name: X            value: "..."     set X (value is interpolated)
        - regex: '(?<X>..)'                   set each named group it captures
        - regex: '...'  loop: true  count_var: N
                                              set X_1..X_n and N=<count>
        - name: X  when: '${..}'  value: ".." conditional set

    `record` collects (name, value, source) for the debug panel.
    """
    output = output or ""
    for entry in register or []:
        if not isinstance(entry, dict):
            continue
        when = entry.get("when")
        if when is not None and not context.evaluate(when):
            if record is not None:
                record.append((entry.get("name") or entry.get("regex"), None,
                               "skipped: when is false"))
            continue

        if entry.get("regex"):
            pattern = to_java_regex(entry["regex"])
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                if record is not None:
                    record.append((entry["regex"], None, "bad regex: %s" % exc))
                continue
            if entry.get("loop"):
                matches = compiled.findall(output)
                count = len(matches)
                base = (list(compiled.groupindex) or ["MATCH"])[0]
                for i, hit in enumerate(matches, 1):
                    text = hit if isinstance(hit, str) else (hit[0] if hit else "")
                    context.put("%s_%d" % (base, i), text)
                if entry.get("count_var"):
                    context.put(entry["count_var"], str(count))
                    if record is not None:
                        record.append((entry["count_var"], str(count),
                                       "loop count over %s" % entry["regex"]))
                continue
            match = compiled.search(output)
            if not match:
                if record is not None:
                    record.append((", ".join(compiled.groupindex) or entry["regex"],
                                   None, "no match in output"))
                continue
            for name in compiled.groupindex:
                value = match.group(name) or ""
                context.put(name, value)
                if record is not None:
                    record.append((name, value, "captured by %s" % entry["regex"]))
            continue

        name = entry.get("name")
        if not name:
            continue
        value = context.interpolate(entry.get("value", ""), record_unresolved=False)
        context.put(name, value)
        if record is not None:
            record.append((name, value, "set from value"))

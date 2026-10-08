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
        resolvePath()            :527-570   a missing key falls back to the ONE key
                                            ending in ".<part>"; a list is indexed
                                            by a dotted number (hosts.0)
    cliautomation/exec/ResultProcessor.java
        captureVariables()       :16-72     the regex is INTERPOLATED, compiled
                                            MULTILINE, the LAST match wins, no
                                            match clears the groups to "", and a
                                            name entry's `when` gates only itself
        isSuccess()              :75-159    exit_code / regex / expr / http_status
                                            / transfer_status / all[] / any[]
        register names are used verbatim, never interpolated (:68)
    cliautomation/plugins/RestProtocolPlugin.java
        applyInlineResponseTemplate() :591  json_path captures, default, required
        evaluateJsonPath()       :664-775   $.a.b, $.a[0], length()
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

# ResultProcessor.tryResolveGroupName(): the i-th CAPTURING group is given the
# i-th NAME in the pattern text. An unnamed group ahead of a named one therefore
# shifts every name by one - a real engine quirk, reproduced on purpose.
_GROUP_NAME = re.compile(r"\(\?P?<([a-zA-Z][a-zA-Z0-9_]*)>")

# java.util.regex rejects a '{' that does not open a {n}, {n,} or {n,m}
# quantifier ("Illegal repetition"). python and JS read it as a literal, so a
# criteria regex such as "${NEW_FILE_NAME}" - which the engine never
# interpolates - compiles here and throws there.
_BAD_BRACE = re.compile(r"(?<!\\)\{(?!\d+(?:,\d*)?\})")


def java_regex_error(pattern):
    """Why java.util.regex would refuse this pattern, or None if it compiles."""
    text = pattern or ""
    for name in _GROUP_NAME.findall(text):
        if "_" in name:
            return "group name <%s> contains '_', which java.util.regex rejects" % name
    # (?P<name>) is python spelling and java rejects it too, but the page has
    # always accepted it; lint.py reports it instead of the walk refusing it.
    # a brace inside a character class is a literal to java as well
    stripped = re.sub(r"\[(?:\\.|[^\]\\])*\]", "", text)
    if _BAD_BRACE.search(stripped):
        return "a '{' that is not a {n,m} quantifier (Illegal repetition)"
    return None


def compile_java(pattern):
    """
    Pattern.compile(pattern, Pattern.MULTILINE), as captureVariables() and
    regexMatches() both call it. Raises re.error for anything java rejects.
    """
    reason = java_regex_error(pattern)
    if reason:
        raise re.error(reason)
    return re.compile(to_java_regex(pattern), re.M)

# Operators, longest first - the order the java evaluator tries them in.
_WORD_OPS = (" notStartsWith ", " startsWith ", " notContains ", " contains ")
_CMP_OPS = ("==", "!=", ">=", "<=", ">", "<")


_ENVIRON = dict(os.environ)


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

    __slots__ = ("token", "reason", "runtime")

    def __init__(self, token, reason, runtime=False):
        self.token = token
        self.reason = reason
        # True when a step SETS this variable at run time - a register, a REST
        # response field, a branch's vars - so the blank is only "not yet"
        self.runtime = runtime

    def as_dict(self):
        out = {"token": self.token, "reason": self.reason}
        if self.runtime:
            out["runtime"] = True
        return out


class Context(object):
    """
    Runtime variable scope: globals.vars + secrets + the CIQ data section,
    plus everything registered as the walk proceeds.
    """

    def __init__(self, variables=None, secrets=None, env=None):
        self.vars = dict(variables or {})
        self.secrets = dict(secrets or {})
        # the synthesiser builds a throwaway Context per check; copying
        # os.environ each time was a quarter of the walk
        self.env = dict(env) if env is not None else _ENVIRON
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
                    # resolvePath(): a missing key falls back to the single key
                    # that ENDS in ".<part>" - "Report.Email" answers "Email".
                    # Two such keys and the lookup gives up.
                    hits = [k for k in current if isinstance(k, str) and k.endswith("." + part)]
                    if len(hits) != 1:
                        return None, False
                    part = hits[0]
                current = current[part]
            elif isinstance(current, (list, tuple)) and part.isdigit():
                # resolvePath() indexes a list with a DOTTED number: hosts.0
                if int(part) >= len(current):
                    return None, False
                current = current[int(part)]
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


def interpolate_object(context, value):
    """ExecutionContext.interpolateObject(): strings, maps and lists, recursively."""
    if isinstance(value, str):
        return context.interpolate(value, record_unresolved=False)
    if isinstance(value, dict):
        return dict((k, interpolate_object(context, v)) for k, v in value.items())
    if isinstance(value, list):
        return [interpolate_object(context, v) for v in value]
    return value


def group_names(pattern):
    """The group names of a pattern in the order they appear in its text."""
    return _GROUP_NAME.findall(pattern or "")


def apply_registers(context, register, output, record=None):
    """
    ResultProcessor.captureVariables(): apply a step's `register` list to one
    command's output.

    Entry forms, in the order the workflow uses them:
        - name: X            value: "..."     set X (value is interpolated)
        - regex: '(?<X>..)'                   set each named group it captures
        - regex: '...'  loop: true  count_var: N
                                              set X_1..X_n and N=<count>
        - name: X  when: '${..}'  value: ".." conditional set

    Faithful to the java, including the parts that surprise people:

      * the regex is interpolated first - `IP address ${rec_row.data.IP}` works
      * it is compiled MULTILINE, so ^ and $ are line anchors with or without
        a leading (?m)
      * every match is visited and the LAST one wins
      * a regex that matches nothing clears its named groups to "" - so a value
        from an earlier loop iteration cannot survive as if it matched
      * in loop mode EVERY named group is numbered: X_1, Y_1, X_2, Y_2 ...
      * a `name` entry is applied even when the same entry carries a regex, and
        its `when` gates only the name, never the regex
      * a name entry with no `value` sets nothing

    `record` collects (name, value, source) for the debug panel.
    """
    output = "" if output is None else output
    for entry in register or []:
        if not isinstance(entry, dict):
            continue

        if entry.get("regex") is not None:
            resolved = context.interpolate(str(entry["regex"]), record_unresolved=False)
            names = group_names(resolved)
            try:
                compiled = compile_java(resolved)
            except re.error as exc:
                if record is not None:
                    record.append((entry["regex"], None, "bad regex: %s" % exc))
                compiled = None
            if compiled is not None:
                loop = bool(entry.get("loop"))
                index = entry.get("start_index")
                index = int(index) if isinstance(index, int) else 1
                count = 0
                for match in compiled.finditer(output):
                    count += 1
                    for i in range(1, compiled.groups + 1):
                        name = names[i - 1] if i - 1 < len(names) else None
                        if not name:
                            continue
                        key = "%s_%d" % (name, index) if loop else name
                        value = match.group(i)
                        if value is None:
                            context.vars.pop(key, None)       # putVariable(k, null)
                        else:
                            context.put(key, value)
                        if record is not None:
                            record.append((key, value, "captured by %s" % entry["regex"]))
                    if loop:
                        index += 1
                if count == 0 and not loop:
                    for i in range(1, compiled.groups + 1):
                        name = names[i - 1] if i - 1 < len(names) else None
                        if name:
                            context.put(name, "")
                            if record is not None:
                                record.append((name, None, "no match in output - cleared to \"\""))
                if loop and entry.get("count_var"):
                    context.put(entry["count_var"], str(count))
                    if record is not None:
                        record.append((entry["count_var"], str(count),
                                       "loop count over %s" % entry["regex"]))

        name = entry.get("name")
        if not name or not str(name).strip():
            continue
        when = entry.get("when")
        if when is not None and not context.evaluate(when):
            if record is not None:
                record.append((name, None, "skipped: when is false"))
            continue
        if entry.get("value") is None:
            continue
        value = interpolate_object(context, entry.get("value"))
        context.put(name, value)
        if record is not None:
            record.append((name, context._stringify(value) if not isinstance(value, str)
                           else value, "set from value"))


# --------------------------------------------------------------------------- #
#  criteria - ResultProcessor.isSuccess()
# --------------------------------------------------------------------------- #
def regex_matches(pattern, output):
    """regexMatches(): Pattern.compile(p, MULTILINE).matcher(output).find()."""
    if output is None:
        return False
    try:
        return compile_java(str(pattern)).search(output) is not None
    except re.error:
        return False


def _attr_equals(expected, actual):
    """safeEquals(String.valueOf(expected), String.valueOf(actual))."""
    def text(value):
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)
    return text(expected) == text(actual)


def matches_condition(condition, output, attrs):
    """
    matchesCondition() - an all[]/any[] item is NOT a nested criteria block:
    a `regex` is matched against the output, and every other key is compared
    to the RESULT ATTRIBUTE of that name (exit_code, http_status ...). So an
    `expr:` inside all[] is compared to an attribute called "expr", which never
    exists, and the item is always false.
    """
    if not isinstance(condition, dict) or not condition:
        return True
    if condition.get("regex") is not None:
        return regex_matches(condition["regex"], output)
    for key, expected in condition.items():
        if not _attr_equals(expected, (attrs or {}).get(key)):
            return False
    return True


def eval_criteria(criteria, context, output, attrs=None):
    """
    isSuccess(). `attrs` carries the protocol result attributes the java reads:
    exit_code (ssh/local/sftp), http_status (rest), transfer_status (sftp).
    An absent attribute fails any criterion that asks for it, as
    intAttr(..., Integer.MIN_VALUE) does.
    """
    attrs = attrs or {}
    if criteria is None:
        code = attrs.get("exit_code")
        return code is None or str(code) == "0"
    if not isinstance(criteria, dict):
        return True
    if criteria.get("exit_code") is not None:
        if attrs.get("exit_code") is None or str(attrs["exit_code"]) != str(criteria["exit_code"]):
            return False
    if criteria.get("regex") is not None and not regex_matches(criteria["regex"], output):
        return False
    expr = criteria.get("expr")
    if isinstance(expr, str) and expr.strip() and not context.evaluate(expr):
        return False
    if criteria.get("http_status") is not None:
        if attrs.get("http_status") is None or str(attrs["http_status"]) != str(criteria["http_status"]):
            return False
    if criteria.get("transfer_status") is not None:
        if not _attr_equals(criteria["transfer_status"], attrs.get("transfer_status")):
            return False
    if isinstance(criteria.get("all"), list) and criteria["all"]:
        if not all(matches_condition(c, output, attrs) for c in criteria["all"]):
            return False
    if isinstance(criteria.get("any"), list) and criteria["any"]:
        if not any(matches_condition(c, output, attrs) for c in criteria["any"]):
            return False
    return True


# --------------------------------------------------------------------------- #
#  REST - RestProtocolPlugin
# --------------------------------------------------------------------------- #
def parse_body(body):
    """
    The response body as the plugin reads it: `\\/` unescaped, then loaded.
    Returns (root, error). An empty body loads as nothing, which is not an
    error - every json_path then resolves to its default.
    """
    text = (body or "").replace("\\/", "/")
    if not text.strip():
        return None, None
    try:
        return json.loads(text), None
    except ValueError:
        pass
    try:
        import yaml                                       # SnakeYAML reads more than JSON
        return yaml.safe_load(text), None
    except Exception as exc:                              # noqa: BLE001
        return None, "failed to parse response body for inline response_template: %s" % exc


def _split_path_tokens(body):
    tokens, buf, depth = [], [], 0
    for char in body:
        if char == "." and depth == 0:
            tokens.append("".join(buf))
            buf = []
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        buf.append(char)
    if buf:
        tokens.append("".join(buf))
    return tokens


def _navigate(current, token):
    at = token.find("[")
    key = token if at < 0 else token[:at]
    pos = 0
    if key:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        pos = len(key)
    while pos < len(token):
        if token[pos] != "[":
            return None
        close = token.find("]", pos)
        if close < 0:
            return None
        raw = token[pos + 1:close].strip()
        if not re.match(r"^-?\d+$", raw) or not isinstance(current, list):
            return None
        index = int(raw)
        if index < 0 or index >= len(current):
            return None
        current = current[index]
        pos = close + 1
    return current


def json_path(root, path):
    """evaluateJsonPath(): $, $.a.b, $.a[0].b, $.list.length() - nothing more."""
    if root is None or path is None:
        return None
    path = str(path).strip()
    if not path:
        return None
    if path == "$":
        return root
    if not path.startswith("$"):
        return None
    body = path[1:]
    if body.startswith("."):
        body = body[1:]
    current = root
    for token in _split_path_tokens(body):
        if not token:
            continue
        if token == "length()":
            if isinstance(current, (list, dict, str)):
                current = len(current)
                continue
            return None
        current = _navigate(current, token)
        if current is None:
            return None
    return current


def apply_response_template(context, rules, body, record=None):
    """
    applyInlineResponseTemplate(). Returns an error string when a `required`
    field is missing or the body cannot be read - the plugin throws there, and
    the step fails with that message - otherwise None.
    """
    if not isinstance(rules, list):
        return None
    root, error = parse_body(body)
    if error:
        return error
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        name = context.interpolate(str(rule.get("name") or ""), record_unresolved=False)
        if not name.strip():
            continue
        path = context.interpolate(str(rule.get("json_path") or ""), record_unresolved=False)
        value = json_path(root, path)
        source = "json_path %s" % path
        if value is None and "default" in rule:
            value = interpolate_object(context, rule.get("default"))
            source = "default (nothing at %s)" % path
        if value is None and rule.get("required"):
            return "required response_template field missing: %s path=%s" % (name, path)
        if value is not None:
            context.put(name, value)
        if record is not None:
            record.append((name, None if value is None else context._stringify(value)
                           if not isinstance(value, str) else value, source))
    return None

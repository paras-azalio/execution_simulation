#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
synth.py
========

Turns a step's success criteria into the WORDS a command would have to print for
that step to pass - and, for the failure button, words that would make it fail.

Why this exists
---------------
A MOP is written before the node is touched, so the three buttons on a step mean:

    Success   the command printed what success looks like
    Failure   it printed something that fails the criteria
    Custom    the operator pastes what the node actually printed

All three then go through exactly the same pipeline - apply the step's
`register` regexes to the output, evaluate the criteria against the result,
route on the outcome - so Success is not a special case in the page's logic. It
is only a different source of output text. That is what keeps the document
honest: a step marked Success has been through the same evaluation a real run
would perform.

How the words are produced
--------------------------
The step's own register regexes ARE the template. `(?m)^XML_EXISTS=(?<XMLEXISTS>
true|false)` is enough to know that the command prints `XML_EXISTS=` followed by
`true` or `false`, and the criteria `${XMLEXISTS == "true"}` says which one. So
the sampler walks the pattern and emits one string that matches it, substituting
the criteria's value into each named group. Where the criteria say nothing, the
group gets whatever the pattern itself implies (the first alternative, a digit
for \\d, and so on).

Loop registers with a count are repeated: `IDLE_OK_COUNT >= 8` over a per-sample
`sar` line emits eight matching lines, so the count register really does count
to eight.

Supported regex constructs: literals and escapes, \\d \\w \\s \\S \\W \\D, '.',
character classes incl. ranges and negation, groups (capturing, named,
non-capturing), alternation (first branch wins), and the quantifiers ? * + {n}
{n,} {n,m}. Anchors and inline flags are consumed and contribute nothing. This
covers every pattern in the CLICR workflows; anything it cannot parse degrades
to a marker line rather than raising.
"""

import json
import re

import engine
from engine import to_java_regex

_CLASS_SAMPLE = {
    "d": "7", "w": "x", "s": " ", "S": "x", "W": "-", "D": "x",
}
_MARKER = "<output not derivable from the criteria - use Custom>"
MARKER = _MARKER

# The statuses tried, in order, for a REST step's Failure - after any the
# step's own on_failure handlers are waiting for.
_FAIL_STATUSES = (500, 404, 401, 400, 503)


def dumps(value):
    """json.dumps as the page's pyDumps() writes it: ', ' and ': ' separators."""
    return json.dumps(value, ensure_ascii=False)


# --------------------------------------------------------------------------- #
#  regex -> one matching string
# --------------------------------------------------------------------------- #
class _Sampler(object):
    """
    Emits one string that matches a pattern.

    Deliberately minimal: it walks the pattern once, left to right, and for
    every construct emits the shortest thing that satisfies it. Alternation
    always takes the first branch, because a criteria value - when there is one
    - is substituted into the group afterwards, and the first branch is what the
    workflow author wrote first (`true|false`, `9\\d\\.\\d{2}|100\\.00`).
    """

    def __init__(self, pattern, group_values=None):
        self.pattern = pattern or ""
        self.group_values = group_values or {}
        self.index = 0

    def sample(self):
        return self._sequence(stop_at_pipe=False)

    # -- structure -----------------------------------------------------------
    def _sequence(self, stop_at_pipe):
        out = []
        while self.index < len(self.pattern):
            char = self.pattern[self.index]
            if char == ")":
                break
            if char == "|":
                if stop_at_pipe:
                    break
                # top-level alternation: first branch wins, discard the rest
                self._skip_to_close()
                break
            piece, name = self._atom()
            if piece is None:
                continue
            piece = self._quantify(piece)
            if name and name in self.group_values:
                piece = self.group_values[name]
            out.append(piece)
        return "".join(out)

    def _atom(self):
        char = self.pattern[self.index]

        if char == "(":
            return self._group()

        if char == "[":
            return self._char_class(), None

        if char == "\\":
            self.index += 1
            if self.index >= len(self.pattern):
                return "\\", None
            esc = self.pattern[self.index]
            self.index += 1
            if esc in _CLASS_SAMPLE:
                return _CLASS_SAMPLE[esc], None
            if esc == "n":
                return "\n", None
            if esc == "t":
                return "\t", None
            if esc == "b":
                return "", None
            return esc, None

        if char in "^$":
            self.index += 1
            return None, None

        if char == ".":
            self.index += 1
            return "x", None

        self.index += 1
        return char, None

    def _group(self):
        self.index += 1                                    # consume '('
        name = None
        if self.pattern.startswith("?", self.index):
            rest = self.pattern[self.index:]
            match = re.match(r"\?P?<([A-Za-z][A-Za-z0-9]*)>", rest)
            if match:
                name = match.group(1)
                self.index += match.end()
            elif re.match(r"\?[:=!]", rest):
                self.index += 2
            elif re.match(r"\?[a-zA-Z]+\)", rest):         # inline flags (?m)(?s)
                self.index += rest.index(")")
                self.index += 1
                return None, None
            else:
                self.index += 1
        body = self._sequence(stop_at_pipe=True)
        self._skip_to_close()
        if self.index < len(self.pattern) and self.pattern[self.index] == ")":
            self.index += 1
        return body, name

    def _skip_to_close(self):
        depth = 0
        while self.index < len(self.pattern):
            char = self.pattern[self.index]
            if char == "\\":
                self.index += 2
                continue
            if char == "[":
                self._char_class()
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    return
                depth -= 1
            self.index += 1

    def _char_class(self):
        self.index += 1                                    # consume '['
        negated = self.pattern.startswith("^", self.index)
        if negated:
            self.index += 1
        members = []
        while self.index < len(self.pattern) and self.pattern[self.index] != "]":
            char = self.pattern[self.index]
            if char == "\\":
                self.index += 1
                esc = self.pattern[self.index] if self.index < len(self.pattern) else "x"
                members.append(_CLASS_SAMPLE.get(esc, esc))
                self.index += 1
                continue
            if (self.index + 2 < len(self.pattern)
                    and self.pattern[self.index + 1] == "-"
                    and self.pattern[self.index + 2] != "]"):
                members.append(char)
                self.index += 3
                continue
            members.append(char)
            self.index += 1
        if self.index < len(self.pattern):
            self.index += 1                                # consume ']'
        if negated:
            for candidate in "xyz0189":
                if candidate not in members:
                    return candidate
            return "x"
        return members[0] if members else "x"

    def _quantify(self, piece):
        if self.index >= len(self.pattern):
            return piece
        char = self.pattern[self.index]
        if char in "?*":
            self.index += 1
            self._consume_lazy()
            return piece if char == "?" else ""
        if char == "+":
            self.index += 1
            self._consume_lazy()
            return piece
        if char == "{":
            match = re.match(r"\{(\d+)(,(\d+)?)?\}", self.pattern[self.index:])
            if match:
                self.index += match.end()
                self._consume_lazy()
                return piece * int(match.group(1))
        return piece

    def _consume_lazy(self):
        if self.index < len(self.pattern) and self.pattern[self.index] in "?+":
            self.index += 1


def sample_for(pattern, group_values=None):
    """One string matching `pattern`, with named groups forced where given."""
    try:
        text = _Sampler(to_java_regex(pattern), group_values).sample()
    except Exception:                                      # never fail a MOP
        return ""
    return text


# --------------------------------------------------------------------------- #
#  criteria -> output
# --------------------------------------------------------------------------- #
def _passes(register, criteria, output, exit_code=0):
    """
    Would this output pass these criteria, run through these registers?

    The synthesiser checks its own work: emitting "success" words that the
    step's criteria then reject would make the page report FAILURE on a step
    the operator just called successful.
    """
    expr = (criteria or {}).get("expr")
    context = engine.Context({})
    engine.apply_registers(context, register, output)
    if isinstance(expr, str) and expr.strip() and not context.evaluate(expr):
        return False
    pattern = (criteria or {}).get("regex")
    if isinstance(pattern, str) and pattern.strip():
        if not engine.regex_matches(pattern, output or ""):
            return False
    return True


def _criteria_regexes(criteria, out=None):
    """Every `regex` a criteria block asserts, including inside all[]/any[]."""
    out = [] if out is None else out
    if isinstance(criteria, dict):
        if isinstance(criteria.get("regex"), str):
            out.append(criteria["regex"])
        for key in ("all", "any"):
            for item in criteria.get(key) or []:
                _criteria_regexes(item, out)
    return out


def _build_success(step_register, criteria, implied, counts=None, loops="all"):
    """
    The words a passing command prints.

    `implied` is what the criteria say each register must hold (see
    expander.implied_values). `counts` gives the number of repeats a loop
    register needs, keyed by its count_var.
    """
    counts = counts or {}
    lines, repeated = [], []
    keep_loop = _specific_loop(step_register) if loops == "specific" else None

    for entry in step_register or []:
        if not isinstance(entry, dict) or not entry.get("regex"):
            continue
        pattern = entry["regex"]
        if entry.get("loop") and keep_loop is not None and pattern != keep_loop:
            # Two loop counters over the same lines are usually related by the
            # criteria ("all remote nodes show Link UP" - LINK_UP_COUNT ==
            # REMOTE_NODE_COUNT). A sample per pattern gives the broader one
            # more matches than the narrower one and the relation fails, so only
            # the most specific line is emitted and the broader pattern counts
            # that same line.
            continue
        if entry.get("loop"):
            # A loop register counts matches, so the sample line is repeated as
            # many times as the criteria demand (IDLE_OK_COUNT >= 8 -> eight
            # lines). Repeats are exempt from the dedupe below: collapsing them
            # would leave the counter at 1 and so fail the very criteria this
            # output exists to satisfy.
            repeat = 1
            count_var = entry.get("count_var")
            if count_var and str(counts.get(count_var, "")).isdigit():
                repeat = max(1, int(counts[count_var]))
            line = sample_for(pattern)
            if line.strip():
                repeated.extend([line] * repeat)
            continue
        groups = {}
        try:
            names = list(re.compile(to_java_regex(pattern)).groupindex)
        except re.error:
            names = []
        # A group the criteria assert to be EMPTY must not be captured at all,
        # so no line is emitted for this register and the variable keeps the
        # default its `- name: X  value: ""` entry gave it. Emitting a sampled
        # line here would set BLOCKED=x and fail `${BLOCKED == ""}` - the very
        # criteria this output exists to satisfy.
        if any(name in implied and implied[name] == "" for name in names):
            continue
        for name in names:
            if name in implied:
                groups[name] = implied[name]
        line = sample_for(pattern, groups)
        nonempty = _nonempty_names(criteria)
        if nonempty & set(names):
            # `(?<DSRUSER>.*)` samples as nothing, and `${DSRUSER != ""}` then
            # fails the very output meant to pass it
            try:
                match = engine.compile_java(pattern).search(line)
            except re.error:
                match = None
            for name in names:
                if name in nonempty and name not in groups and match is not None \
                        and not (match.group(name) or ""):
                    groups[name] = "%s_1" % name.lower()
            line = sample_for(pattern, groups)
        if line.strip():
            lines.append(line)

    for pattern in _criteria_regexes(criteria):
        line = sample_for(pattern)
        if line.strip():
            lines.append(line)

    # A step with no regex anywhere is judged on its exit code alone, so any
    # output satisfies it; inventing node output would only mislead.
    if not lines and not repeated:
        return ""
    return "\n".join(_dedupe(lines) + repeated)


def _nonempty_names(criteria):
    """Variables the criteria assert to be non-empty: `${X != ""}`."""
    expr = str((criteria or {}).get("expr") or "")
    return set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*!=\s*(?:\"\"|'')", expr))


def _specific_loop(step_register):
    """The longest loop pattern - the most specific line to emit."""
    patterns = [e["regex"] for e in step_register or []
                if isinstance(e, dict) and e.get("regex") and e.get("loop")]
    return max(patterns, key=len) if len(patterns) > 1 else None


def success_output(step_register, criteria, implied, counts=None):
    """
    The words a passing command prints, verified against the criteria.

    A first attempt emits a sample per register. If that does not satisfy the
    criteria - which happens when two loop counters are compared to each other -
    it is retried with only the most specific loop line. If neither passes the
    best attempt is still returned: the page evaluates it in front of the
    operator, so an honest FAILURE beats silently inventing a pass.
    """
    for mode in ("all", "specific"):
        output = _build_success(step_register, criteria, implied, counts, mode)
        if _passes(step_register, criteria, output):
            return output
    return _build_success(step_register, criteria, implied, counts, "specific")


def failure_output(step_register, criteria, implied):
    """
    Words that make the criteria fail.

    Built by flipping each implied value to something it is not, so the same
    registers capture a value the criteria reject. Where nothing is implied, the
    register produces no match at all, which is itself a failure for any
    criteria that tests the captured value.
    """
    flipped = dict((name, _not(value)) for name, value in (implied or {}).items())
    lines = []
    for entry in step_register or []:
        if not isinstance(entry, dict) or not entry.get("regex") or entry.get("loop"):
            continue
        pattern = entry["regex"]
        try:
            names = list(re.compile(to_java_regex(pattern)).groupindex)
        except re.error:
            names = []
        groups = dict((n, flipped[n]) for n in names if n in flipped)
        if not groups:
            continue
        line = sample_for(pattern, groups)
        if line.strip():
            lines.append(line)
    if not lines:
        return _MARKER
    output = "\n".join(_dedupe(lines))
    if _passes(step_register, criteria, output, exit_code=1):
        # The flipped values still satisfy the criteria, so emit nothing a
        # register can capture: every variable then keeps the default its
        # `- name: X  value: ""` entry gave it, which is what a failing
        # step really looks like.
        return _MARKER
    return output


def _not(value):
    """
    A value the criteria reject.

    Prefixed, never suffixed: `^(?<WRITEOK>WRITE_OK)` would still match the
    prefix of "WRITE_OK_NOT_MATCHING" and capture WRITE_OK, so the "failure"
    output would pass. "NOT_MATCHING_WRITE_OK" cannot match at a line start.
    """
    text = "" if value is None else str(value)
    if text == "true":
        return "false"
    if text == "false":
        return "true"
    if text == "running":
        return "stopped"
    if text == "success":
        return "failure"
    if text.isdigit():
        return str(int(text) + 1)
    return "NOT_MATCHING_" + text if text else "unexpected"


# --------------------------------------------------------------------------- #
#  REST - a response body and a status
# --------------------------------------------------------------------------- #
def _path_tokens(path):
    """`$.data.items[0].name` -> ["data", "items", 0, "name"], or None."""
    text = str(path or "").strip()
    if not text.startswith("$") or "length()" in text:
        return None
    body = text[1:].lstrip(".")
    if not body:
        return None
    out = []
    for token in engine._split_path_tokens(body):
        if not token:
            continue
        match = re.match(r"^([^\[]*)((?:\[\d+\])*)$", token)
        if not match:
            return None
        if match.group(1):
            out.append(match.group(1))
        for index in re.findall(r"\[(\d+)\]", match.group(2)):
            out.append(int(index))
    return out or None


def _set_path(root, tokens, value):
    current = root
    for i, token in enumerate(tokens):
        last = i == len(tokens) - 1
        nxt = None if last else tokens[i + 1]
        container = [] if isinstance(nxt, int) else {}
        if isinstance(token, int):
            while len(current) <= token:
                current.append(None)
            if last:
                current[token] = value
            else:
                if not isinstance(current[token], (dict, list)):
                    current[token] = container
                current = current[token]
        else:
            if last:
                current[token] = value
            else:
                if not isinstance(current.get(token), (dict, list)):
                    current[token] = container
                current = current[token]


def _build_body(rules, values, force=()):
    """
    A JSON body that gives each response_template rule its value.

    A rule whose name has a value (from the criteria) gets it; a `required`
    rule gets a sample; a rule with a `default` is left out, so the default is
    what the step captures - the way a real response without that field reads.
    A path that is a prefix of another is not set on its own: the longer path
    builds the object it names.
    """
    wanted = []
    for rule in rules or []:
        if not isinstance(rule, dict) or not rule.get("name"):
            continue
        tokens = _path_tokens(rule.get("json_path"))
        if not tokens:
            continue
        name = str(rule["name"])
        if name in values:
            wanted.append((tokens, values[name]))
        elif rule.get("required") or "default" not in rule or name in force:
            wanted.append((tokens, "%s_1" % name))
    if not wanted:
        return None
    root = {}
    for tokens, value in wanted:
        if any(len(o) > len(tokens) and o[:len(tokens)] == tokens for o, _v in wanted):
            continue
        _set_path(root, tokens, value)
    return root


def _rest_passes(rules, criteria, register, body, status):
    context = engine.Context({})
    if engine.apply_response_template(context, rules, body):
        return False
    context.put("http_status", status)
    engine.apply_registers(context, register, body)
    return engine.eval_criteria(criteria or {}, context, body, {"http_status": status})


def rest_outputs(rules, criteria, implied, register=None, preferred=None):
    """
    (success body, success status, failure body, failure status) for a REST step.

    Success is the status the criteria imply (200 when they say nothing) and a
    body carrying every value they assert. Failure flips those values and uses
    the first status the criteria reject - trying first any status this step's
    own on_failure handlers are gated on, so pressing Failure walks the retry
    path the author wrote (DSR: 401 -> refresh the token -> retry).

    A status of None for the failure means nothing a server could answer fails
    this step - only a connection error can - and the page simulates that.
    """
    implied = dict(implied or {})
    raw_status = str(implied.pop("http_status", "") or
                     (criteria or {}).get("http_status") or "200")
    ok_status = int(raw_status) if raw_status.isdigit() else 200
    # a field the criteria read without asserting a value (`${DSRUSER != ""}`)
    # has to be IN a passing response, default or not
    mentioned = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*",
                               str((criteria or {}).get("expr") or "")))
    ok_body = _build_body(rules, implied, mentioned)
    ok_text = dumps(ok_body) if ok_body is not None else ""

    flipped = dict((name, _not(value)) for name, value in implied.items())
    bad_body = _build_body(rules, flipped)
    bad_text = dumps(bad_body) if bad_body is not None else ""
    bad_status = None
    for status in list(preferred or []) + list(_FAIL_STATUSES):
        if status == ok_status:
            continue
        if not _rest_passes(rules, criteria, register, bad_text, status):
            bad_status = status
            break
    return ok_text, ok_status, bad_text, bad_status


def _exit_only(criteria):
    if not isinstance(criteria, dict):
        return False
    return ("exit_code" in criteria or "http_status" in criteria
            or "transfer_status" in criteria) and not criteria.get("expr")


def _dedupe(lines):
    seen, out = set(), []
    for line in lines:
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out

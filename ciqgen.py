#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ciqgen.py - a CIQ for an activity that has no sample order
==========================================================

Every activity in templates/yaml can be turned into a MOP, but only a couple of
them have a CIQ JSON lying around. The rest cannot even be walked: the workflow
loops over `${nodeGroups}` and reads `${rec_row.data.Action}`, and with no data
there is nothing to loop over and nothing to render.

The mapping already describes that data exactly. `*_json-output.yaml` is the
spec for building the CIQ JSON out of the CIQ workbook, so reading it forwards
gives the shape of a valid CIQ for the activity - the nesting, the field names,
which levels are lists. What it cannot give is the values, because those live
in a workbook nobody has here.

So the values come from the workflow itself:

    ${rec_row.data.Action}                 the record needs an `Action` column
    ${rec_row.data.Action} == "ENABLE"     and "ENABLE" is a value it tests for
    ${table_row.table} == "Call Barring"   and that is one of the table names

Harvesting the literals a workflow compares against is what makes a generated
CIQ useful rather than merely well-formed: a record carrying Action=ENABLE
walks the branch the author wrote, where Action=Action_1 would fall through
every one of them and document nothing.

    python ciqgen.py --json-template <activity_json-output.yaml>
                     [--yaml <workflow.yaml>] [--groups 2] [--rows 2]
                     [--out CIQ.json]

The result is a CIQ, not THE CIQ: it stands in for a real order so the document
can be generated, reviewed and tested. Every generated file says so in
`meta.generatedBy`.
"""

import argparse
import io
import json
import os
import re
import sys

import ciq as ciq_mod

# A comparison against a quoted literal, where the left side is a data path.
# `${rec_row.data.Action} == "ENABLE"` and `${table_row.table} != "RSI"` both
# tell us a column name and one value the workflow expects to see in it. The
# `\}?` matters: a workflow writes the placeholder closed - `${x.y} == "z"` -
# so without it the path never reaches the operator and nothing is harvested.
_LITERAL = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_ \-]+)+?)\s*\}?\s*"
    r"(?:==|!=|contains|startsWith)\s*"
    r"['\"]([^'\"]{1,60})['\"]")

# ${something.data.Column} - the record fields the workflow actually reads.
_DATA_FIELD = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\.data\.([^}]+)\}")

_GROUP_NAMES = ["North1", "South1", "East1", "West1", "Central1"]


# --------------------------------------------------------------------------- #
#  what the workflow needs
# --------------------------------------------------------------------------- #
class Demand(object):
    """The CIQ fields and values one workflow asks for."""

    def __init__(self, columns=None, values=None):
        self.columns = list(columns or [])          # record columns, in order
        self.values = dict(values or {})            # {column: [literal, ...]}

    def literals(self, column):
        return self.values.get(column) or []

    def as_dict(self):
        return {"columns": self.columns,
                "values": dict((k, v) for k, v in sorted(self.values.items()))}


def _strings(node):
    """Every string in a loaded YAML document, in document order."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str):
                yield key
            for item in _strings(value):
                yield item
    elif isinstance(node, list):
        for value in node:
            for item in _strings(value):
                yield item
    elif isinstance(node, str):
        yield node


def demand_of(workflow):
    """
    Scan a workflow for the record columns and literals it depends on.

    The scan walks the loaded YAML string by string rather than json.dumps()ing
    it: a dumped document escapes every quote, and the literals being harvested
    are quoted.
    """
    columns, values = [], {}
    for text in _strings(workflow):
        for _var, field in _DATA_FIELD.findall(text):
            field = field.strip()
            if field and field not in columns:
                columns.append(field)
        for path, literal in _LITERAL.findall(text):
            column = path.rsplit(".", 1)[-1].strip()
            literal = literal.strip()
            if not column or not literal:
                continue
            bucket = values.setdefault(column, [])
            if literal not in bucket:
                bucket.append(literal)
    return Demand(columns, values)


# --------------------------------------------------------------------------- #
#  values
# --------------------------------------------------------------------------- #
def _synth(column, index, scope, demand, hint=""):
    """
    One cell. A literal the workflow compares against always wins - that is the
    whole point - and otherwise the column NAME decides the shape, so an IMSI
    column gets something that looks like an IMSI and a path column gets a path.

    Most rules match whole WORDS of the column name, not substrings: "Report
    Email" must not be read as a port because it contains the letters PORT.
    `hint` is the mapping expression the cell came from, and only varies values
    that would otherwise repeat - the PGW and RDS niamIDs differ solely by a
    `WHERE IP.Node_Name = ...` clause.
    """
    column = (column or "value").strip()
    literals = demand.literals(column) if demand else []
    if literals:
        return literals[index % len(literals)]

    upper = column.upper()
    words = set(w for w in re.split(r"[^A-Za-z0-9]+", upper) if w)
    squashed = upper.replace(" ", "").replace("_", "")
    group = scope.get("__group__") or _GROUP_NAMES[0]
    node_type = (scope.get("__nodeType__") or "NODE").upper()
    n = index + 1
    # a stable spread for the values a repeated mapping expression would clone
    spread = (sum(ord(c) for c in (hint or column)) % 7) + 1
    tail = re.search(r"(\d+)$", column)
    slot = int(tail.group(1)) if tail else spread

    def has(*needles):
        return any(needle in upper for needle in needles)

    if has("IMSI"):
        return "40496000000%04d" % (112 + index)
    if has("MSISDN") or words & {"NUMBER", "NUM", "MSISDN"}:
        return "9188888888%02d" % n
    if has("EMAIL") or words & {"MAIL"}:
        return "clicr.ops@example.net"
    if has("NIAM"):
        return "PB-NOKIA-%s-%s-D%d-172.37.%d.%d-CLI" % (
            node_type, group[:2].upper(), slot, 15 + spread, 146 + slot)
    if has("IPV6") or words & {"V6"}:
        return "2401:4900:24:439::%d" % slot
    if words & {"IP", "IPADDRESS", "ADDR", "ADDRESS", "HOST", "IPV4"}:
        return "10.72.%d.%d" % (30 + spread, 130 + index)
    if words & {"PORT"}:
        return str(5060 + index)
    if words & {"VER", "VERSION"}:
        return "24.7"
    if words & {"ZONE", "NODEGROUP"} or squashed == "NODEGROUP":
        return group
    if squashed.startswith("CRGROUP") or words & {"CRGRP"}:
        return "CR1"
    if words & {"PATH", "DIR", "DIRECTORY"}:
        return "/mnt/shared_data/%s" % column.lower().replace(" ", "_")
    if words & {"FILE", "FILENAME"}:
        return "%s_%d.txt" % (column.lower().replace(" ", "_"), n)
    if words & {"COUNT", "QTY", "SIZE", "PRIORITY", "WEIGHT", "INDEX"}:
        return str(n)
    if words & {"ACTION"}:
        return "ENABLE"
    if words & {"NODE", "NODENAME"}:
        # one name per node: the engine selects a node BY NAME, so two
        # entries called North1 are one node and a document that overwrites
        return group if index == 0 else "%s-%d" % (group, n)
    return "%s_%d" % (re.sub(r"[^A-Za-z0-9]+", "_", column).strip("_") or "VALUE", n)


def _put(row, column, value):
    """
    Set one record field. A dotted column - `${record.data.conditions.appId.value}`
    - is a NESTED field: resolvePath() splits on the dots, so a flat key
    "conditions.appId.value" would never be found.
    """
    parts = [p for p in column.split(".") if p]
    if len(parts) < 2 or " " in column:
        row.setdefault(column, value)
        return
    current = row
    for part in parts[:-1]:
        if not isinstance(current.get(part), dict):
            current[part] = {}
        current = current[part]
    current.setdefault(parts[-1], value)


def _alias_value(alias, index, scope, demand):
    """A value for a `_each ... AS $alias` binding."""
    lowered = (alias or "").lower()
    if "zone" in lowered or "ng" == lowered or "nodegroup" in lowered or "group" in lowered:
        return _GROUP_NAMES[index % len(_GROUP_NAMES)]
    if "table" in lowered:
        names = demand.literals("table") or demand.literals("TABLES") if demand else []
        if names:
            return names[index % len(names)]
        return "Table%d" % (index + 1)
    if "seq" in lowered or "step" in lowered:
        return "Step%d" % (index + 1)
    if "cr" in lowered:
        return "CR%d" % (index + 1)
    if "node" in lowered:
        return _GROUP_NAMES[index % len(_GROUP_NAMES)]
    return _synth(alias, index, scope, demand)


# --------------------------------------------------------------------------- #
#  the renderer
# --------------------------------------------------------------------------- #
class Generator(object):
    """Walks a json-output mapping and emits a CIQ data section."""

    def __init__(self, template, demand=None, groups=2, rows=2, node_type=None):
        self.template = template
        self.demand = demand or Demand()
        self.groups = max(1, int(groups))
        self.rows = max(1, int(rows))
        self.node_type = node_type or template.node_type or "NODE"
        self.meta = {}

    # -- entry ---------------------------------------------------------------
    def build(self):
        data = self.template.data or {}
        scope = {"__nodeType__": self.node_type, "__group__": _GROUP_NAMES[0]}
        # meta first: a `_ref: "meta.tableKeys.$table..."` elsewhere reads it.
        if isinstance(data.get("meta"), dict):
            self.meta = self._render(data["meta"], scope)
        out = self._render(data, scope)
        if isinstance(out, dict):
            if self.meta:
                out["meta"] = self.meta
            out.setdefault("meta", {})
            if isinstance(out.get("meta"), dict):
                out["meta"]["generatedBy"] = (
                    "ciqgen.py from %s - synthetic order data, not a real CIQ"
                    % (os.path.basename(self.template.path) if self.template.path
                       else "a json-output mapping"))
        return out

    # -- recursion -----------------------------------------------------------
    def _render(self, node, scope):
        if isinstance(node, dict):
            if isinstance(node.get("_each"), str):
                return self._render_each(node, scope)
            return self._render_map(node, scope)
        if isinstance(node, list):
            return [self._render(item, scope) for item in node]
        if isinstance(node, str):
            return self._render_string(node, scope)
        return node

    def _render_each(self, node, scope):
        """A `_each` level is a list; the alias it binds varies per entry."""
        each = node["_each"]
        alias = self._alias_of(each)
        count = self._count_for(each, alias)
        out = []
        for index in range(count):
            inner = dict(scope)
            if alias:
                value = _alias_value(alias, index, inner, self.demand)
                inner["$" + alias] = value
                if _is_group_alias(alias):
                    inner["__group__"] = value
            inner["__index__"] = index
            inner["__sheet__"] = self.template._sheet_of(each) or scope.get("__sheet__")
            out.append(self._render_map(node, inner, skip=("_each",)))
        return out

    def _render_map(self, node, scope, skip=()):
        # `_row: "*"` stands for the whole workbook row, which is where the
        # columns the workflow reads have to appear - nothing else in the
        # mapping names them.
        if "_row" in node:
            row = self._row(scope)
            out = dict(row)
            for key, value in node.items():
                if key.startswith("_") or key in skip:
                    continue
                out[key] = self._directive(value, scope, row, key)
            return out

        out = {}
        for key, value in node.items():
            if key in skip:
                continue
            if key.startswith("_"):
                continue
            out[key] = self._directive(value, scope, None, key)
        return out

    def _directive(self, value, scope, row, key=None):
        """A value that may itself be a `_ref` / `_col` / `_join` block."""
        if isinstance(value, dict):
            if "_ref" in value:
                return self._ref(value["_ref"], scope)
            if "_col" in value:
                column = self._directive(value["_col"], scope, row)
                column = str(column or "").split(",")[0].strip()
                if row and column in row:
                    return row[column]
                return _synth(column or "primaryKey", scope.get("__index__", 0),
                              scope, self.demand)
            if "_row_join" in value:
                return self._row_join(value["_row_join"], scope, row)
            if "_join" in value:
                sep = value.get("separator", ",")
                base = self._render_string(str(value["_join"]), scope)
                return sep.join([base, base + "_2"]) if base else ""
            if isinstance(value.get("_each"), str):
                return self._render_each(value, scope)
            return self._render_map(value, scope)
        if isinstance(value, str):
            return self._render_string(value, scope, key)
        return self._render(value, scope)

    def _row_join(self, spec, scope, row):
        row = row or self._row(scope)
        exclude = set(str(x) for x in (spec.get("exclude") or []))
        fmt = spec.get("format") or "{col}={val}"
        sep = spec.get("separator", " ")
        parts = [fmt.replace("{col}", str(k)).replace("{val}", str(v))
                 for k, v in row.items() if k not in exclude]
        return sep.join(parts)

    def _ref(self, path, scope):
        """`meta.tableKeys.$table.recordPrimaryKeys` against the rendered meta."""
        text = self._substitute(str(path), scope)
        parts = [p for p in text.split(".") if p]
        current = {"meta": self.meta}
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return ""
        return current if isinstance(current, str) else current

    def _row(self, scope):
        """One workbook row: every record column the workflow reads."""
        index = scope.get("__index__", 0)
        row = {}
        for column in self.demand.columns:
            _put(row, column, _synth(column.rsplit(".", 1)[-1] if "." in column else column,
                                     index, scope, self.demand))
        sheet = scope.get("__sheet__")
        for column in self.template.columns_for(sheet) if sheet else []:
            row.setdefault(column, _synth(column, index, scope, self.demand))
        if not row:
            row["VALUE"] = _synth("VALUE", index, scope, self.demand)
        # The columns every CIQ sheet carries, so a WHERE on them is not blank.
        row.setdefault("ZONE", scope.get("__group__") or _GROUP_NAMES[0])
        row.setdefault("NODEGROUP", scope.get("__group__") or _GROUP_NAMES[0])
        return row

    # -- strings -------------------------------------------------------------
    def _render_string(self, text, scope, key=None):
        raw = text.strip()
        if not raw:
            return ""
        if raw.startswith("$") and " " not in raw and raw in scope:
            return scope[raw]
        substituted = self._substitute(raw, scope)
        cell = self.template._cell_of(substituted)
        if cell:
            return _synth(cell[1], scope.get("__index__", 0), scope, self.demand,
                          hint=raw)
        if "$" in substituted:
            return substituted
        # A bare word inside a `_each` level may be a column of that sheet, but
        # a bare word is far more often a literal (`table: RSI`, `configSeq:
        # Step1`), and emitting "RSI" as a made-up value would break every
        # `${table_row.table} == "RSI"` in the workflow. Three things mark a
        # real column reference: the field repeats its own name, which is the
        # mapping's idiom for "take this column as it is" (`INPUT_FILE:
        # INPUT_FILE`); the workflow reads a record field of that name; or the
        # mapping itself uses it as a column of this sheet elsewhere.
        sheet = scope.get("__sheet__")
        known = (key is not None and key == substituted and sheet) or             substituted in self.demand.columns or (
                sheet and substituted in self.template.columns_for(sheet))
        if known:
            return _synth(substituted, scope.get("__index__", 0), scope, self.demand,
                          hint=raw)
        return substituted

    @staticmethod
    def _substitute(text, scope):
        def repl(match):
            key = "$" + match.group(1)
            return str(scope.get(key, match.group(0)))
        return re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)", repl, text)

    # -- sizing --------------------------------------------------------------
    def _alias_of(self, each):
        match = re.search(r"\bAS\s+\$([A-Za-z_][A-Za-z0-9_]*)", each, re.I)
        if match:
            return match.group(1)
        # `_each: "IP WHERE IP.NODEGROUP = $ng"` binds nothing; the level is
        # still a list, it just has no name for the current entry.
        return None

    def _count_for(self, each, alias):
        lowered = (alias or "").lower()
        if _is_group_alias(alias):
            return self.groups
        if "table" in lowered:
            names = self.demand.literals("table")
            return max(1, min(len(names), 4)) if names else 2
        if "seq" in lowered or "step" in lowered:
            return 1
        return self.rows


def _is_group_alias(alias):
    lowered = (alias or "").lower()
    return lowered in ("ng", "zone", "nodegroup", "group") or "zone" in lowered


# --------------------------------------------------------------------------- #
#  api + cli
# --------------------------------------------------------------------------- #
def generate(template, workflow=None, groups=2, rows=2):
    """Build a CIQ data section for `template`, shaped by `workflow`."""
    demand = demand_of(workflow) if workflow else Demand()
    return Generator(template, demand, groups, rows).build()


def minimal(workflow, groups=2, rows=2, activity=None, node_type=None):
    """
    A CIQ for a workflow that has no mapping at all.

    Some workflows are not activities with a workbook behind them - TEST_EMAIL
    sends one mail and touches no order data. They still need a node to be
    documented against, because a MOP is written per node. This builds the
    conventional v2 shape with nothing in it but the nodes, and adds records
    only if the workflow actually reads record fields.
    """
    demand = demand_of(workflow) if workflow else Demand()
    name = activity or (workflow or {}).get("name") or "activity"
    node_type = (node_type or "NODE").upper()
    scope = {"__nodeType__": node_type}

    tables = []
    if demand.columns:
        for table in (demand.literals("table") or ["Table1"])[:4]:
            records = []
            for index in range(max(1, rows)):
                inner = dict(scope)
                inner["__index__"] = index
                data = {}
                for column in demand.columns:
                    _put(data, column, _synth(column.rsplit(".", 1)[-1] if "." in column
                                              else column, index, inner, demand))
                records.append({"data": data})
            tables.append({"table": table, "records": records})

    node_groups = []
    for index in range(max(1, groups)):
        group = _GROUP_NAMES[index % len(_GROUP_NAMES)]
        scoped = dict(scope)
        scoped["__group__"] = group
        scoped["__index__"] = index
        entry = {"nodeGroup": group, "crGroup": "CR1",
                 "email": "clicr.ops@example.net",
                 "nodes": [{"node": group,
                            "niamID": _synth("NIAM_NAME", index, scoped, demand)}]}
        if tables:
            entry["configSequences"] = [{"configSeq": "Step1",
                                         "tables": json.loads(json.dumps(tables))}]
        node_groups.append(entry)

    return {"schemaVersion": "2",
            "meta": {"nodeType": node_type,
                     "activity": str(name),
                     "generatedBy": "ciqgen.py from the workflow alone (no "
                                    "json-output mapping) - synthetic order "
                                    "data, not a real CIQ"},
            "nodeGroups": node_groups}


def generate_file(json_template, workflow_yaml=None, out_path=None,
                  groups=2, rows=2):
    workflow = ciq_mod.read_yaml(workflow_yaml) if workflow_yaml else None
    if json_template:
        template = ciq_mod.load_output_template(json_template)
        data = generate(template, workflow, groups, rows)
    else:
        data = minimal(workflow, groups, rows)
    if out_path:
        directory = os.path.dirname(os.path.abspath(out_path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        io.open(out_path, "w", encoding="utf-8", newline="\n").write(
            json.dumps(data, ensure_ascii=False, indent=2))
    return data


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Synthesise a CIQ JSON for an activity from its "
                    "json-output mapping and its workflow.")
    p.add_argument("--json-template", default="", dest="json_template",
                   help="the activity mapping; without it a minimal CIQ is built "
                        "from the workflow alone")
    p.add_argument("--yaml", default="",
                   help="the workflow, for column and value discovery")
    p.add_argument("--groups", type=int, default=2, help="how many nodeGroups (default 2)")
    p.add_argument("--rows", type=int, default=2, help="records per table (default 2)")
    p.add_argument("--out", default="", help="write here instead of stdout")
    args = p.parse_args(argv)

    data = generate_file(args.json_template, args.yaml or None, args.out or None,
                         args.groups, args.rows)
    if args.out:
        print("wrote %s" % args.out)
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

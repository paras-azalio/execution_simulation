#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ciq.py
======

Loads the three inputs and puts them into the shape the expander needs.

    CIQ JSON            the data the workflow loops over
    workflow YAML       the activity definition
    json-output YAML    the CIQ-workbook -> CIQ-JSON mapping spec

Ported from cliautomation/api/JsonSchemaSupport.java:

    extractDataSection()    :55-62    a "data" wrapper is unwrapped, else the
                                      payload IS the data section
    normalizeDataSection()  :89-140   schemaVersion "2" only: meta.nodeType /
                                      meta.activity / meta.tableKeys promoted to
                                      the root, and nodeGroups[].nodes[] flattened
                                      into nodes[], each node inheriting
                                      nodeGroup / crGroup / email /
                                      configSequences. Every mutation is
                                      ADDITIVE - an existing key always wins.

The json-output template is not a report format: it is the mapping that builds
the CIQ JSON out of the CIQ workbook ("DISTINCT Index.ZONE AS $zone",
"IP.NIAM_NAME1 WHERE IP.ZONE = $zone AND IP.Node_Name = PGW"). Reading it
properly answers the question a blank in a MOP always raises - was this value
missing from the order, or does the mapping never produce it at all? See
OutputTemplate.explain(). ciqgen.py reads the same structure the other way
round, to build a CIQ for an activity that has no sample order yet.
"""

import io
import json
import os
import re

try:
    import yaml
except ImportError:                                    # pragma: no cover
    raise SystemExit("PyYAML is required: python -m pip install pyyaml")


def read_json(path):
    with io.open(path, encoding="utf-8") as handle:
        return json.load(handle)


# libyaml parses these 400 KB workflows ten times faster than the pure-python
# scanner, and reads them identically.
_SAFE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def read_yaml(path):
    with io.open(path, encoding="utf-8") as handle:
        return yaml.load(handle, Loader=_SAFE_LOADER)


def extract_data_section(payload):
    """JsonSchemaSupport.extractDataSection()."""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload


def normalize_data_section(data):
    """
    JsonSchemaSupport.normalizeDataSection(). Mutates and returns `data`.

    V1 (no schemaVersion, or "1") is already canonical and untouched.
    """
    if not isinstance(data, dict):
        return data
    version = str(data.get("schemaVersion", "1")).strip()
    if version != "2":
        return data

    meta = data.get("meta")
    if isinstance(meta, dict):
        for key in ("nodeType", "activity", "tableKeys"):
            if key not in data and key in meta:
                data[key] = meta[key]

    node_groups = data.get("nodeGroups")
    if isinstance(node_groups, list) and node_groups and "nodes" not in data:
        flat = []
        for group in node_groups:
            if not isinstance(group, dict):
                continue
            inherited = (("nodeGroup", group.get("nodeGroup")),
                         ("crGroup", group.get("crGroup")),
                         ("email", group.get("email")),
                         ("configSequences", group.get("configSequences")))
            for entry in group.get("nodes") or []:
                if not isinstance(entry, dict):
                    continue
                node = dict(entry)
                for key, value in inherited:
                    if key not in node and value is not None:
                        node[key] = value
                flat.append(node)
        data["nodes"] = flat
    return data


def load_ciq(path):
    """Read, unwrap and normalise a CIQ JSON. Returns the data section."""
    data = extract_data_section(read_json(path))
    return normalize_data_section(data)


# --------------------------------------------------------------------------- #
#  json-output template
# --------------------------------------------------------------------------- #
# The mapping is a small language, not free text. Reading it properly is what
# lets a MOP say WHY a ${rec_row.data.X} came out blank:
#
#   _each: "DISTINCT INDEX.NODEGROUP AS $ng"   this level is a LIST, one entry
#                                              per distinct value, bound to $ng
#   _each: "IP WHERE IP.NODEGROUP = $ng"       one entry per matching IP row
#   _each: "$table WHERE NODEGROUP = $ng"      the sheet name is itself bound
#   _row:  "*"                                 copy every column of the row
#   _ref:  "meta.tableKeys.$table.record..."   look the value up under meta
#   _col:  {_ref: ...}                         the COLUMN NAME comes from a ref
#   _row_join: {exclude, format, separator}    fold the row into one string
#   _join: "INDEX.GROUP" (+ separator)         fold repeated values into one
#   "Index.Email WHERE Index.ZONE = $zone"     one cell
#   "IP.'NIAM NAME'"                           one cell, column name quoted
#   INPUT_FILE                                 a column of the enclosing sheet
#   Step1                                      a literal
_SQL_WORDS = ("DISTINCT", "AS", "WHERE", "AND", "OR", "IN", "FROM", "NOT")

# Sheet.Column, bare or with the column quoted:  IP.'NIAM NAME'
#
# A CIQ column name may contain spaces - "Index.Report Email WHERE ..." - so
# the bare form keeps taking words until it hits one of the query keywords.
# Without that, `Index.Report Email` reads as the column `Report`, and the
# generated value is chosen for the wrong name.
_CELL = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_ ]*?)\."
    r"(?:'([^']+)'|\"([^\"]+)\"|"
    r"([A-Za-z_][A-Za-z0-9_]*(?:\s+(?!(?:WHERE|AND|OR|IN|AS|NOT|FROM)\b)[A-Za-z0-9_]+)*))",
    re.I)
_ALIAS = re.compile(r"\bAS\s+\$([A-Za-z_][A-Za-z0-9_]*)", re.I)
_DIRECTIVES = ("_ref", "_col", "_row_join", "_join")


class MappingSource(object):
    """Where one emitted CIQ field is supposed to come from."""

    __slots__ = ("field", "sheet", "column", "wildcard")

    def __init__(self, field, sheet=None, column=None, wildcard=False):
        self.field = field
        self.sheet = sheet
        self.column = column
        self.wildcard = wildcard

    def describe(self):
        if self.wildcard:
            return "every column of the %s row" % (self.sheet or "source")
        if self.sheet and self.column and self.sheet != self.column:
            return "%s.%s" % (self.sheet, self.column)
        return self.column or self.field


class OutputTemplate(object):
    """
    What the json-output mapping promises the CIQ JSON will contain.

    Two questions a MOP needs answered about an unresolved ${...}:

        does the mapping emit this field at all?    -> emits()
        if so, where was it meant to come from?     -> explain()

    A field the mapping never emits is a DOCUMENT DEFECT: the workflow reads
    something no CIQ built from this mapping can ever carry, and re-running the
    order will not fill it. A field the mapping does emit but this CIQ lacks is
    an ORDER GAP: the workbook had no value in that column, and the page turns
    it into an input for the operator. Both render as a blank in the command,
    which is precisely why the document has to tell them apart.
    """

    def __init__(self, doc=None, path=None):
        self.path = path
        self.doc = doc or {}
        self.output_mode = str(self.doc.get("output_mode", "")).strip()
        data = self.doc.get("data") if isinstance(self.doc.get("data"), dict) else {}
        self.data = data
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        self.meta = meta
        self.activity = meta.get("activity") or data.get("activity")
        self.node_type = meta.get("nodeType") or data.get("nodeType")

        self.sheet_columns = {}        # {sheet: set(column)} - the cells it reads
        self.row_sheets = []           # the sheets a _each iterates
        self.wildcard_sheets = set()   # sheets copied whole by _row: "*"
        self.fields = {}               # {emitted field: MappingSource}
        self._scan(data, None)

    # -- scanning ------------------------------------------------------------
    def _scan(self, node, sheet):
        if isinstance(node, dict):
            each = node.get("_each")
            if isinstance(each, str):
                sheet = self._sheet_of(each) or sheet
                self.row_sheets.append(sheet)
                self._note_cells(each)
            if "_row" in node:
                if sheet:
                    self.wildcard_sheets.add(sheet)
                self.fields.setdefault("*", MappingSource("*", sheet, "*", True))
            for key, value in node.items():
                if key.startswith("_"):
                    if key not in ("_each", "_row"):
                        self._scan(value, sheet)
                    continue
                self._record(key, value, sheet)
                self._scan(value, sheet)
        elif isinstance(node, list):
            for item in node:
                self._scan(item, sheet)
        elif isinstance(node, str):
            self._note_cells(node)

    def _record(self, key, value, sheet):
        """A leaf under `key` becomes an emitted CIQ field."""
        if isinstance(value, dict):
            if set(value) & set(_DIRECTIVES):
                self.fields.setdefault(key, MappingSource(key, sheet, key))
            return
        if isinstance(value, list):
            return
        if not isinstance(value, str):
            self.fields.setdefault(key, MappingSource(key, sheet, key))
            return
        cell = self._cell_of(value)
        if cell:
            self.fields[key] = MappingSource(key, cell[0], cell[1])
        else:
            self.fields.setdefault(key, MappingSource(key, sheet, value.strip()))

    def _note_cells(self, text):
        for sheet, column in self._cells_in(text):
            self.sheet_columns.setdefault(sheet, set()).add(column)

    @staticmethod
    def _cells_in(text):
        out = []
        for match in _CELL.finditer(text or ""):
            sheet = match.group(1).strip()
            column = (match.group(2) or match.group(3) or match.group(4) or "").strip()
            if not sheet or not column:
                continue
            if sheet.upper() in _SQL_WORDS or column.upper() in _SQL_WORDS:
                continue
            out.append((sheet, column))
        return out

    def _cell_of(self, text):
        cells = self._cells_in(text)
        return cells[0] if cells else None

    @staticmethod
    def _sheet_of(each):
        """The sheet a `_each` iterates: the first word that is not SQL noise."""
        for word in re.findall(r"\$?[A-Za-z_][A-Za-z0-9_ ]*", each or ""):
            word = word.strip()
            if not word or word.upper() in _SQL_WORDS:
                continue
            return word.split(".")[0]
        return None

    # -- the questions -------------------------------------------------------
    def aliases(self):
        """Every $name a _each binds - $ng, $zone, $table, $seq."""
        return sorted(set(_ALIAS.findall(json.dumps(self.doc, default=str))))

    def columns_for(self, sheet):
        return sorted(self.sheet_columns.get(sheet, ()))

    @property
    def copies_whole_rows(self):
        return "*" in self.fields

    def emits(self, field):
        """
        True  - a CIQ built from this mapping carries a field of this name
        None  - undecidable here: no mapping, or `_row: "*"` copies the row
        False - the mapping never produces it
        """
        if not self.doc:
            return None
        if field in self.fields:
            return True
        for columns in self.sheet_columns.values():
            if field in columns:
                return True
        if self.copies_whole_rows:
            return None
        return False

    def explain(self, field):
        """Why a record field is blank, in the mapping's own terms."""
        if not self.doc:
            return None
        name = os.path.basename(self.path) if self.path else "the json-output mapping"
        verdict = self.emits(field)
        if verdict is True:
            source = self.fields.get(field)
            where = (" from %s" % source.describe()) if source else ""
            return "%s fills it%s, so this order has no value in that column" % (name, where)
        if verdict is None:
            return ('%s copies whole rows (_row: "*"), so this column exists only if '
                    'the CIQ workbook carried it - this one did not' % name)
        return "%s never fills it - no sheet in the mapping produces %s" % (name, field)

    def as_dict(self):
        return {"path": os.path.basename(self.path) if self.path else None,
                "outputMode": self.output_mode,
                "activity": self.activity,
                "nodeType": self.node_type,
                "copiesWholeRows": self.copies_whole_rows,
                "fields": sorted(k for k in self.fields if k != "*"),
                "sheetColumns": dict((k, sorted(v)) for k, v in self.sheet_columns.items())}


def load_output_template(path):
    if not path:
        return OutputTemplate()
    return OutputTemplate(read_yaml(path), path)


# --------------------------------------------------------------------------- #
#  request parameters
# --------------------------------------------------------------------------- #
# The macro-server Arglist values a real run supplies. Defaults mirror
# SimActivityRunner.java so a generated MOP is runnable against the simulator
# without a params file; every one of them stays overridable in the page.
#
# The first seven are the order's own. The rest are what a real run puts in
# scope besides: CliAutomationEngine.extractArglistVariables() turns EVERY
# Arglist argument into a variable (INPUT_JSON_FILE_NAME, the report paths,
# SUB_ACTIVITY_NAME ...), and MopExecutionUtil adds the GRC-section values
# (REPO_*, NIAM_IP, M2MPORT) and the M2M login. Without them ${REPO_USER} and
# friends came out blank in almost every document.
DEFAULT_PARAMS = {
    "ORDER_NO": "12345",
    "PARENT_REQ_ID": "12345",
    "CHILD_REQ_ID": "10057",
    "CR_GROUP": "CR1",
    "CR_NAME": "CR1",
    "NODE_TYPE": "PGW_RDS",
    "REQ_TYPE": "1",
    "SUB_ACTIVITY_NAME": "",
    "ROLLBACK_ONLY": "false",
    "INPUT_JSON_FILE_NAME": "/opt/clicr/input/order.json",
    "OUTPUT_LOGS_FILE_LOCATION": "/opt/clicr/runs/10057/reports/logs",
    "OUTPUT_JSON_REPORT_NAME": "/opt/clicr/runs/10057/reports/json",
    "MOP_EXEC_LOG_FILE": "/opt/clicr/runs/10057/reports/logs/execution.log",
    "REPO_IP": "127.0.0.1",
    "REPO_USER": "installer",
    "REPO_PASSWORD": "ROOT",
    "NIAM_IP": "127.0.0.1",
    "M2MPORT": "22",
    "M2MUSER": "admin",
    "M2MPASSWORD": "admin",
}


def activity_params(stem, data, ciq_name=None):
    """
    NODE_TYPE, SUB_ACTIVITY_NAME and INPUT_JSON_FILE_NAME for one activity.

    MopExecutionUtil loads `<NODE_TYPE>_<SUB_ACTIVITY_NAME>.yaml`, so the node
    type is what is left of the file name once the CIQ's activity is taken off
    the end: PGW_RDS_1051_SUBSCRIBER_PROFILE_CONFIGURATION with activity
    1051_SUBSCRIBER_PROFILE_CONFIGURATION is PGW_RDS. The old default claimed
    PGW_RDS for every SBC, DSR and EIR document.
    """
    out = {}
    activity = str((data or {}).get("activity") or "").strip()
    node_type = str((data or {}).get("nodeType") or "").strip()
    stem = re.sub(r"\.ya?ml$", "", str(stem or ""), flags=re.I)
    if activity and stem.upper().endswith("_" + activity.upper()):
        node_type = stem[:len(stem) - len(activity) - 1]
    if not node_type and stem:
        node_type = stem.split("_")[0]
    if node_type:
        out["NODE_TYPE"] = node_type
    if activity:
        out["SUB_ACTIVITY_NAME"] = activity
    if ciq_name:
        out["INPUT_JSON_FILE_NAME"] = "/opt/clicr/input/" + ciq_name
    return out


def explicit_keys(path=None, overrides=None):
    """The parameter names the caller set on purpose - they beat activity_params."""
    keys = set(_file_params(path)) if path else set()
    for item in overrides or []:
        if "=" in item:
            keys.add(item.split("=", 1)[0].strip())
    return keys


def _file_params(path):
    """A params file: JSON ({"ORDER_NO": "12345"}) or key=value lines."""
    out = {}
    text = io.open(path, encoding="utf-8").read().strip()
    if text.startswith("{"):
        for key, value in json.loads(text).items():
            out[str(key)] = "" if value is None else str(value)
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def load_params(path=None, overrides=None):
    """
    Request parameters: DEFAULT_PARAMS, then a params file, then --param k=v.

    The file may be JSON ({"ORDER_NO": "12345"}) or key=value lines.
    """
    params = dict(DEFAULT_PARAMS)
    if path:
        params.update(_file_params(path))
    for item in overrides or []:
        if "=" in item:
            key, value = item.split("=", 1)
            params[key.strip()] = value.strip()
    return params

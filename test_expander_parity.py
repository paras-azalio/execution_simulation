#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_expander_parity.py
=======================

    python test_expander_parity.py            every workflow in templates/yaml
    python test_expander_parity.py SBC_147    just the matching ones
    python test_expander_parity.py -v         print the first differing step

The runner page expands a workflow in the browser; mopgen.py expands the same
workflow in python. If the two disagree, the same order produces two different
documents depending on which door you came in by - so every workflow in the
repo is walked through both and compared step by step.

This is the same idea as test_engine_js.js, one level up: that pins the
expression engine, this pins the whole walk - loops, gating, register
application, and the synthesised Success and Failure text.

Run as a script it prints a table; `test_execution_flow.ExpanderParity` runs it
as part of the suite.
"""

import io
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ciq as CIQ                                          # noqa: E402
import ciqgen                                              # noqa: E402
import runall                                              # noqa: E402
from expander import Expander                              # noqa: E402

EXPAND_JS = os.path.join(HERE, "expand_js.js")

# The fields that decide what the operator is shown and what the page then
# does with a click. `produces` and `validation.description` are left out: they
# are debug detail, not document content.
COMPARED = ["uid", "seq", "phase", "phase_name", "render_mode", "kind",
            "node_ref", "node_target", "description", "send", "when",
            "when_state", "skip_when", "skip_state", "success_output",
            "failure_output", "implied", "consumes", "on_failure", "register",
            "retries", "timeout_sec", "hide_when_skipped", "use_exit_code"]


def _norm(value):
    """
    JSON round-tripping differences that are not disagreements.

    python emits a tuple-free structure already; the only real nuisance is that
    a YAML integer arrives as `60` from python and `60` from JS but as `60.0`
    once either side has been through a float, and that `None` and a missing
    key mean the same thing here.
    """
    if isinstance(value, float) and value == int(value):
        return int(value)
    if isinstance(value, dict):
        return dict((k, _norm(v)) for k, v in value.items())
    if isinstance(value, list):
        return [_norm(v) for v in value]
    return value


def _tokens(step):
    return sorted(u["token"] for u in step.get("unresolved") or [])


def _labels(step):
    return [entry.get("label") for entry in step.get("loop_path") or []]


def python_expansion(workflow, data, template, params, node):
    expansion = Expander(workflow, data, params, template).expand(node)
    steps = []
    for step in expansion.steps:
        payload = step.as_dict()
        payload["descriptionRaw"] = payload.pop("description_raw", "")
        payload.pop("step_description", None)
        steps.append(payload)
    return {"steps": steps, "warnings": expansion.warnings}


def js_expansion(yaml_path, ciq_path, mapping, node_index, params):
    command = ["node", EXPAND_JS, "--yaml", yaml_path, "--ciq", ciq_path,
               "--node-index", str(node_index)]
    if mapping:
        command += ["--json-template", mapping]
    for key, value in sorted(params.items()):
        command += ["--param", "%s=%s" % (key, value)]
    proc = subprocess.Popen(command, cwd=HERE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    out, err = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError("expand_js.js failed: %s"
                           % err.decode("utf-8", "replace").strip()[:500])
    return json.loads(out.decode("utf-8", "replace"))


def compare(name, yaml_path, mapping, ciq_path, node_index=0):
    """Walk one workflow through both sides. Returns a list of differences."""
    workflow = CIQ.read_yaml(yaml_path)
    data = CIQ.load_ciq(ciq_path)
    template = CIQ.load_output_template(mapping or "")
    params = CIQ.load_params()
    nodes = data.get("nodes") or []
    if not nodes:
        return ["%s: the CIQ has no nodes" % name]

    py = python_expansion(workflow, data, template, params, nodes[node_index])
    js = js_expansion(yaml_path, ciq_path, mapping, node_index, params)

    problems = []
    if len(py["steps"]) != len(js["steps"]):
        problems.append("%s: %d steps in python, %d in the browser"
                        % (name, len(py["steps"]), len(js["steps"])))
        return problems

    for a, b in zip(py["steps"], js["steps"]):
        for field in COMPARED:
            left, right = _norm(a.get(field)), _norm(b.get(field))
            if left != right:
                problems.append("%s %s: %s\n    python: %r\n    browser: %r"
                                % (name, a["uid"], field, left, right))
        if _tokens(a) != _tokens(b):
            problems.append("%s %s: unresolved\n    python: %r\n    browser: %r"
                            % (name, a["uid"], _tokens(a), _tokens(b)))
        if _labels(a) != _labels(b):
            problems.append("%s %s: loop_path\n    python: %r\n    browser: %r"
                            % (name, a["uid"], _labels(a), _labels(b)))
    if sorted(py["warnings"]) != sorted(js["warnings"]):
        problems.append("%s: warnings\n    python: %r\n    browser: %r"
                        % (name, sorted(py["warnings"]), sorted(js["warnings"])))
    return problems


def inputs_for(stem, yaml_dir, mapping_dir, ciq_dirs, work_dir):
    """The same pairing runall.py does, with a synthetic CIQ where needed."""
    yaml_path = os.path.join(yaml_dir, stem + ".yaml")
    mapping = runall.find_mapping(stem, mapping_dir)
    ciq_path = runall.find_ciq(stem, ciq_dirs)
    if not ciq_path:
        ciq_path = os.path.join(work_dir, stem + ".json")
        workflow = CIQ.read_yaml(yaml_path)
        if mapping:
            data = ciqgen.generate(CIQ.load_output_template(mapping), workflow)
        else:
            data = ciqgen.minimal(workflow)
        io.open(ciq_path, "w", encoding="utf-8", newline="\n").write(
            json.dumps(data, ensure_ascii=False, indent=2))
    return yaml_path, mapping, ciq_path


def run(only=None, verbose=False, yaml_dir=None, mapping_dir=None):
    yaml_dir = yaml_dir or os.path.join(
        HERE, "..", "JAVA_NOKIA_CLICR_AUTOMATION", "src", "main", "resources",
        "templates", "yaml")
    yaml_dir = os.path.normpath(yaml_dir)
    mapping_dir = mapping_dir or os.path.normpath(
        os.path.join(yaml_dir, "..", "jsonTemplate"))
    if not os.path.isdir(yaml_dir):
        print("no workflow directory: %s" % yaml_dir)
        return 2

    names = sorted(os.path.splitext(f)[0] for f in os.listdir(yaml_dir)
                   if f.endswith(".yaml"))
    if only:
        names = [n for n in names if any(t.lower() in n.lower() for t in only)]

    work = tempfile.mkdtemp(prefix="parity_")
    failures, checked = [], 0
    for stem in names:
        yaml_path, mapping, ciq_path = inputs_for(
            stem, yaml_dir, mapping_dir, runall.DEFAULT_CIQ_DIRS, work)
        try:
            problems = compare(stem, yaml_path, mapping, ciq_path)
        except Exception as exc:                           # noqa: BLE001
            problems = ["%s: %s" % (stem, exc)]
        checked += 1
        mark = "ok  " if not problems else "FAIL"
        print("%s %-62s %s" % (mark, stem[:62],
                               "identical" if not problems
                               else "%d difference(s)" % len(problems)))
        if problems:
            failures.extend(problems)
            if verbose:
                print("     " + problems[0].replace("\n", "\n     "))

    print("")
    print("%d of %d workflows expand identically in python and the browser"
          % (checked - len(set(p.split(" ")[0] for p in failures)), checked))
    if failures and not verbose:
        print("first difference:")
        print("  " + failures[0].replace("\n", "\n  "))
    return 0 if not failures else 1


if __name__ == "__main__":
    argv = [a for a in sys.argv[1:] if a != "-v"]
    sys.exit(run(argv or None, verbose="-v" in sys.argv))

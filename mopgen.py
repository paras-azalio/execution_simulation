#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
mopgen.py - entry point
=======================

Builds one interactive MOP per node from the three CLICR inputs.

    python mopgen.py --yaml   <workflow.yaml>
                     --ciq    <CIQ.json>
                     [--json-template <activity_json-output.yaml>]
                     [--params params.json] [--param KEY=VALUE ...]
                     [--out DIR] [--node NAME] [--json] [--quiet]

Debugger (command-wise, over the activity and rollback phases - the health
checks are a fixed checklist and are not stepped):

    --step                    break on every activity/rollback command
    --break ACTIVITY_CONFIG   break on the phases whose id or name matches
    --break s0042             break on one step id
    --break-imsi 4049600...   break on the commands for one subscriber

At a break:  (n)ext  (c)ontinue  (v)ars  (s)end  (q)uit

Output, one file per node (plus --json for the same data as a machine-readable
expansion, which is what the sim report tooling can diff against a real run):

    <out>/mop_<activity>_<cr>_<node>.html
    <out>/mop_<activity>_<cr>_<node>.json
"""

import argparse
import io
import json
import os
import re
import sys

import ciq as ciq_mod
from expander import Expander, INTERACTIVE

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "template", "runbook.html")
ENGINE_JS = os.path.join(HERE, "template", "engine.js")
UI_JS = os.path.join(HERE, "template", "ui.js")
UI_CSS = os.path.join(HERE, "template", "ui.css")

# camelCase aliases the page's JS reads.
_ALIAS = {"description_raw": "descriptionRaw", "success_output": "success_output",
          "failure_output": "failure_output", "render_mode": "render_mode"}


def log(message, quiet=False):
    if not quiet:
        print(message)


# --------------------------------------------------------------------------- #
#  debugger
# --------------------------------------------------------------------------- #
class Tracer(object):
    """
    Command-wise stepper over the expansion.

    Only the interactive phases (activity, rollback) are stepped: the health
    checks are a fixed list with a fixed reading, so breaking on them is noise.
    """

    def __init__(self, step_all=False, breaks=None, imsi=None, stream=None):
        self.step_all = step_all
        self.breaks = [b for b in (breaks or []) if b]
        self.imsi = imsi
        self.out = stream or sys.stdout
        self.continuing = False
        self.interactive = sys.stdin is not None and sys.stdin.isatty()

    def _wanted(self, step):
        if step.render_mode != INTERACTIVE:
            return False
        if self.continuing:
            return False
        if self.step_all:
            return True
        for token in self.breaks:
            low = token.lower()
            if low in (step.phase or "").lower() or low in (step.phase_name or "").lower():
                return True
            if low == (step.uid or "").lower():
                return True
        if self.imsi:
            haystack = " ".join(str(x) for x in
                                [step.send, step.description] +
                                [l.get("label") for l in step.loop_path or []])
            if self.imsi in haystack:
                return True
        return False

    def step(self, step, context, trace):
        if not self._wanted(step):
            return
        write = self.out.write
        write("\n" + "-" * 74 + "\n")
        write("[brk] %s  phase=%s  step=%s\n" % (step.uid, step.phase, step.seq))
        for entry in step.loop_path or []:
            write("      %-14s %s\n" % (entry["var"], entry["label"]))
        write("  node    %s%s\n" % (step.node_target or "local",
                                    "" if step.node_ref == step.node_target
                                    else "   (from %s)" % step.node_ref))
        write("  desc    %s\n" % (step.description or ""))
        if step.when:
            write("  when    %s\n" % step.when)
        for item in trace:
            expr, left_raw, left, right, result = item
            write("          %s\n          [%s] vs [%s] -> %s\n" % (
                expr, "<unset>" if left is None else left, right,
                "TRUE" if result else "FALSE"))
        write("  gates   when=%s skip_when=%s\n" % (_tri(step.when_state), _tri(step.skip_state)))
        if step.send:
            write("  send    %s\n" % _clip(step.send))
        for u in step.unresolved or []:
            write("  UNRES   ${%s}  %s\n" % (u.token, u.reason))
        if step.implied:
            write("  implied %s\n" % json.dumps(step.implied))
        if step.success_output:
            write("  ok out  %s\n" % _clip(step.success_output.replace("\n", " | ")))
        if not self.interactive:
            return
        while True:
            self.out.write("(n)ext (c)ontinue (v)ars (s)end (q)uit > ")
            self.out.flush()
            answer = (sys.stdin.readline() or "q").strip().lower()
            if answer in ("", "n"):
                return
            if answer == "c":
                self.continuing = True
                return
            if answer == "q":
                raise SystemExit("debugger: quit")
            if answer == "v":
                for key in sorted(context.vars):
                    value = context.vars[key]
                    if isinstance(value, (dict, list)):
                        continue
                    write("    %-28s %s\n" % (key, _clip(str(value), 120)))
            if answer == "s":
                write("\n%s\n\n" % (step.send or "(no command)"))


def _tri(value):
    return "unknown" if value is None else ("true" if value else "false")


def _clip(text, width=160):
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[:width - 3] + "..."


# --------------------------------------------------------------------------- #
#  html
# --------------------------------------------------------------------------- #
def step_payload(step):
    data = step.as_dict()
    for key, alias in _ALIAS.items():
        if key in data and alias != key:
            data[alias] = data.pop(key)
    # the page never needs these, and they are the bulk of the payload
    data.pop("step_description", None)
    return data


def build_html(expansion, meta, base_vars, params, globals_vars,
               template_html, assets):
    payload = [step_payload(s) for s in expansion.steps]
    title = "MOP - %s - %s" % (meta.get("activity") or "activity", meta.get("node"))
    subtitle = " &middot; ".join(filter(None, [
        "Node type: %s" % meta.get("nodeType"),
        "CR: %s" % meta.get("crGroup"),
        "NodeGroup: %s" % meta.get("nodeGroup"),
        "%d steps" % len(payload),
        "Generated %s" % meta.get("generated"),
    ]))
    # The engine, the renderer and the styles are inlined rather than linked:
    # the page has to work as a single file that is mailed around and opened
    # from disk.
    return (template_html
            .replace("{{UI_CSS}}", assets["css"])
            .replace("{{ENGINE_JS}}", assets["engine"])
            .replace("{{UI_JS}}", assets["ui"])
            .replace("{{TITLE}}", _esc(title))
            .replace("{{SUBTITLE}}", subtitle)
            .replace("{{META_JSON}}", _json(meta))
            .replace("{{STEPS_JSON}}", _json(payload))
            .replace("{{VARS_JSON}}", _json(base_vars))
            .replace("{{GLOBALS_JSON}}", _json(globals_vars))
            .replace("{{PARAMS_JSON}}", _json(params)))


def _json(value):
    return json.dumps(value, ensure_ascii=False, default=str).replace("</", "<\\/")


def _esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _slug(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(text or "x")).strip("_")


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        description="Generate an interactive per-node MOP from a CLICR workflow, "
                    "a CIQ JSON and the json-output mapping.")
    p.add_argument("--yaml", required=True, help="workflow YAML (the master)")
    p.add_argument("--ciq", required=True, help="CIQ JSON")
    p.add_argument("--json-template", default="", dest="json_template",
                   help="the activity's *_json-output.yaml mapping")
    p.add_argument("--params", default="", help="request parameters (JSON or key=value lines)")
    p.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                   help="one request parameter; repeatable")
    p.add_argument("--out", default="out", help="output directory (default: ./out)")
    p.add_argument("--node", action="append", default=[],
                   help="only this node (repeatable); default is every node in the CIQ")
    p.add_argument("--json", action="store_true", help="also write the raw expansion JSON")
    p.add_argument("--step", action="store_true", help="break on every activity/rollback command")
    p.add_argument("--break", action="append", default=[], dest="breaks",
                   metavar="PHASE|STEPID", help="break on a phase or step id; repeatable")
    p.add_argument("--break-imsi", default="", dest="break_imsi",
                   help="break on the commands that mention this IMSI")
    p.add_argument("--quiet", action="store_true")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    workflow = ciq_mod.read_yaml(args.yaml)
    data = ciq_mod.load_ciq(args.ciq)
    template = ciq_mod.load_output_template(args.json_template)
    params = ciq_mod.load_params(args.params, args.param)

    if not os.path.isfile(TEMPLATE):
        raise SystemExit("page template missing: %s" % TEMPLATE)
    template_html = io.open(TEMPLATE, encoding="utf-8").read()
    assets = {}
    for key, path in (("engine", ENGINE_JS), ("ui", UI_JS), ("css", UI_CSS)):
        if not os.path.isfile(path):
            raise SystemExit("page asset missing: %s" % path)
        assets[key] = io.open(path, encoding="utf-8").read()

    nodes = data.get("nodes") or []
    if not nodes:
        raise SystemExit("no nodes in %s - is it a CIQ with nodeGroups[].nodes[]?" % args.ciq)
    if args.node:
        wanted = set(args.node)
        nodes = [n for n in nodes
                 if str(n.get("node")) in wanted or str(n.get("nodeGroup")) in wanted]
        if not nodes:
            raise SystemExit("none of --node %s found in the CIQ" % ", ".join(args.node))

    tracer = None
    if args.step or args.breaks or args.break_imsi:
        tracer = Tracer(args.step, args.breaks, args.break_imsi)

    if not os.path.isdir(args.out):
        os.makedirs(args.out)

    generated = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    activity = data.get("activity") or template.activity or "activity"
    written = []

    log("workflow : %s (%s)" % (os.path.basename(args.yaml), workflow.get("name")), args.quiet)
    log("ciq      : %s  (%d node%s)" % (os.path.basename(args.ciq), len(nodes),
                                        "" if len(nodes) == 1 else "s"), args.quiet)
    if args.json_template:
        log("mapping  : %s  (%d sheet%s)" % (
            os.path.basename(args.json_template), len(template.sheet_columns),
            "" if len(template.sheet_columns) == 1 else "s"), args.quiet)

    for node in nodes:
        expander = Expander(workflow, data, params, template, tracer)
        expansion = expander.expand(node)
        meta = dict(expansion.node)
        meta.update({"generated": generated, "activity": activity,
                     "workflow": os.path.basename(args.yaml),
                     "ciq": os.path.basename(args.ciq),
                     "mapping": os.path.basename(args.json_template) if args.json_template else None,
                     "steps": len(expansion.steps)})

        base_vars = _display_vars(expander, node)
        html = build_html(expansion, meta, base_vars, params,
                          _globals_vars(workflow), template_html, assets)
        name = "mop_%s_%s_%s" % (_slug(activity), _slug(meta.get("crGroup")),
                                 _slug(meta.get("node")))
        path = os.path.join(args.out, name + ".html")
        io.open(path, "w", encoding="utf-8", newline="\n").write(html)
        written.append(path)

        interactive = sum(1 for s in expansion.steps if s.render_mode == INTERACTIVE)
        unresolved = sorted(set(u.token for s in expansion.steps for u in s.unresolved or []))
        log("  %-28s %3d steps (%d interactive, %d checklist)%s"
            % (meta.get("node"), len(expansion.steps), interactive,
               len(expansion.steps) - interactive,
               "  %d unresolved" % len(unresolved) if unresolved else ""), args.quiet)
        for warning in expansion.warnings:
            log("      ! %s" % warning, args.quiet)

        if args.json:
            jpath = os.path.join(args.out, name + ".json")
            io.open(jpath, "w", encoding="utf-8", newline="\n").write(
                json.dumps({"meta": meta, "params": params,
                            "mapping": template.as_dict(),
                            "steps": [step_payload(s) for s in expansion.steps],
                            "warnings": expansion.warnings},
                           ensure_ascii=False, indent=2, default=str))
            written.append(jpath)

    log("", args.quiet)
    for path in written:
        log("wrote %s" % path, args.quiet)
    return 0


def _globals_vars(workflow):
    """
    globals.vars as an ORDERED [name, raw] list, for the page.

    A request parameter rarely appears in a command directly; it reaches one
    through a global - `LOCAL_PATH: /mnt/shared_data/${CHILD_REQ_ID}/...`. The
    page therefore has to re-derive the globals whenever a parameter changes,
    in declaration order, exactly as Expander.base_context() does, or the
    Request parameters panel silently does nothing for those commands.
    """
    block = (workflow.get("globals") or {}).get("vars") or {}
    return [[name, value] for name, value in block.items()]


def _display_vars(expander, node):
    """
    The scalar part of the seed context, for the page's variable resolution.

    Lists and maps the CIQ owns (nodeGroups, nodes, tables) are needed too: the
    page re-interpolates ${rec_row...} style references when a custom output
    changes a value, so the whole data section goes in.
    """
    context = expander.base_context(node)
    out = {}
    for key, value in context.vars.items():
        out[key] = value
    return out


if __name__ == "__main__":
    sys.exit(main())

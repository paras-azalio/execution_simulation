#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
runall.py - a MOP for every workflow in templates/yaml
======================================================

    python runall.py                       everything, into out/all
    python runall.py --only SBC_147        just the ones whose name matches
    python runall.py --rows 3 --groups 3   bigger synthetic orders

For each `*.yaml` in the workflow directory it pairs up the other two inputs
the generator needs and writes one MOP per node:

    the mapping   <activity>_json-output.yaml from templates/jsonTemplate,
                  matched by name, with the _local / _MOPALIGNED / _PRECHECK
                  variants falling back to the base activity's mapping
    the CIQ       a real order JSON if one is lying about in the search dirs,
                  otherwise ciqgen.py synthesises one from that same mapping

The second half is the point. Only a couple of these activities have a sample
order in the repo, and without data a workflow cannot be walked at all - the
outer `for_each: "${nodeGroups}"` has nothing to iterate and not one command
renders. A synthetic order is not a real one and the document says so, but it
is enough to prove the workflow expands, to see every command the operator
would be given, and to find the references that resolve to nothing.

Each run writes, under --out:

    index.html          every activity, node and gap, linked
    index.json          the same as data
    <activity>/         the MOPs themselves, one HTML + JSON per node
    _ciq/<activity>.json   whatever order data was synthesised, kept so the
                           numbers in a document can be traced to their input
"""

import argparse
import io
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ciq as ciq_mod                                      # noqa: E402
import ciqgen                                              # noqa: E402

MOPGEN = os.path.join(HERE, "mopgen.py")

# Where the three inputs live by default, relative to this file.
DEFAULT_YAML_DIR = os.path.normpath(os.path.join(
    HERE, "..", "JAVA_NOKIA_CLICR_AUTOMATION", "src", "main", "resources",
    "templates", "yaml"))
DEFAULT_TEMPLATE_DIR = os.path.normpath(os.path.join(
    HERE, "..", "JAVA_NOKIA_CLICR_AUTOMATION", "src", "main", "resources",
    "templates", "jsonTemplate"))
DEFAULT_CIQ_DIRS = [
    os.path.normpath(os.path.join(HERE, "..", "version")),
    os.path.normpath(os.path.join(HERE, "..", "version", "1051")),
    os.path.normpath(os.path.join(HERE, "..", "version", "1058")),
    os.path.normpath(os.path.join(HERE, "..", "dmp", "version")),
]

# A workflow that is a variant of another activity shares its mapping.
_VARIANT_SUFFIXES = ("_MOPALIGNED", "_local", "_PRECHECK", "_ROLLBACK")


# --------------------------------------------------------------------------- #
#  pairing the three inputs
# --------------------------------------------------------------------------- #
def base_names(stem):
    """The activity names to try for a workflow file, most specific first."""
    out = [stem]
    changed = True
    current = stem
    while changed:
        changed = False
        for suffix in _VARIANT_SUFFIXES:
            if current.upper().endswith(suffix.upper()):
                current = current[:-len(suffix)]
                out.append(current)
                changed = True
    return out


def find_mapping(stem, template_dir):
    if not os.path.isdir(template_dir):
        return None
    available = dict((f.lower(), f) for f in os.listdir(template_dir)
                     if f.lower().endswith(".yaml"))
    for name in base_names(stem):
        key = (name + "_json-output.yaml").lower()
        if key in available:
            return os.path.join(template_dir, available[key])
    # last resort: the longest mapping name that is a prefix of this workflow
    candidates = []
    for lower, actual in available.items():
        head = lower[:-len("_json-output.yaml")]
        if head and stem.lower().startswith(head):
            candidates.append((len(head), actual))
    if candidates:
        return os.path.join(template_dir, max(candidates)[1])
    return None


def find_ciq(stem, ciq_dirs):
    """A real order JSON for this activity, if the checkout happens to have one."""
    wanted = [n.lower() + ".json" for n in base_names(stem)]
    for directory in ciq_dirs:
        if not os.path.isdir(directory):
            continue
        present = dict((f.lower(), f) for f in os.listdir(directory)
                       if f.lower().endswith(".json"))
        for name in wanted:
            if name in present:
                return os.path.join(directory, present[name])
    return None


# --------------------------------------------------------------------------- #
#  one activity
# --------------------------------------------------------------------------- #
def run_one(yaml_path, mapping, ciq_path, out_dir, synthetic, extra_args):
    """Generate the MOPs for one workflow. Returns a result record."""
    stem = os.path.splitext(os.path.basename(yaml_path))[0]
    record = {"activity": stem, "workflow": os.path.basename(yaml_path),
              "mapping": os.path.basename(mapping) if mapping else None,
              "ciq": os.path.basename(ciq_path) if ciq_path else None,
              "ciqSynthetic": synthetic, "out": out_dir,
              "ok": False, "nodes": [], "documents": [], "error": None,
              "steps": 0, "interactive": 0, "unresolved": [], "warnings": []}

    command = [sys.executable, MOPGEN, "--yaml", yaml_path, "--ciq", ciq_path,
               "--out", out_dir, "--json", "--quiet"]
    if mapping:
        command += ["--json-template", mapping]
    command += list(extra_args or [])

    started = time.time()
    proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, cwd=HERE)
    out, _ = proc.communicate()
    record["seconds"] = round(time.time() - started, 2)
    text = out.decode("utf-8", "replace").strip()

    if proc.returncode != 0:
        record["error"] = _last_error(text)
        return record

    for name in sorted(os.listdir(out_dir)) if os.path.isdir(out_dir) else []:
        if not name.endswith(".json"):
            continue
        doc = json.load(io.open(os.path.join(out_dir, name), encoding="utf-8"))
        meta = doc.get("meta") or {}
        steps = doc.get("steps") or []
        interactive = sum(1 for s in steps if s.get("render_mode") == "interactive")
        tokens = sorted(set(u["token"] for s in steps
                            for u in (s.get("unresolved") or [])))
        record["nodes"].append({
            "node": meta.get("node"), "crGroup": meta.get("crGroup"),
            "steps": len(steps), "interactive": interactive,
            "unresolved": tokens,
            "html": name[:-5] + ".html"})
        record["documents"].append(os.path.join(out_dir, name[:-5] + ".html"))
        record["steps"] += len(steps)
        record["interactive"] += interactive
        for token in tokens:
            if token not in record["unresolved"]:
                record["unresolved"].append(token)
        for warning in doc.get("warnings") or []:
            if warning not in record["warnings"]:
                record["warnings"].append(warning)

    record["ok"] = bool(record["nodes"])
    if not record["ok"] and not record["error"]:
        record["error"] = "no document was produced"
    return record


def _last_error(text):
    """The useful line out of a traceback or a SystemExit message."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return "failed with no output"
    for line in reversed(lines):
        if not line.startswith(("File ", "Traceback", "  ")):
            return line[:300]
    return lines[-1][:300]


# --------------------------------------------------------------------------- #
#  the report
# --------------------------------------------------------------------------- #
def write_index(results, out_root, started):
    payload = {"generated": time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(started)),
               "activities": results}
    io.open(os.path.join(out_root, "index.json"), "w", encoding="utf-8",
            newline="\n").write(json.dumps(payload, indent=2, ensure_ascii=False))

    rows = []
    for r in results:
        status = ("ok" if r["ok"] else "bad")
        docs = "".join(
            '<a href="%s">%s</a>' % (
                _rel(out_root, os.path.join(r["out"], n["html"])), _esc(n["node"]))
            for n in r["nodes"]) or "&mdash;"
        gaps = "".join('<li><code>${%s}</code></li>' % _esc(t)
                       for t in r["unresolved"][:12]) or ""
        warn = "".join('<li>%s</li>' % _esc(w) for w in r["warnings"][:8]) or ""
        rows.append(
            '<tr class="%s"><td>%s<div class="k">%s</div></td>'
            '<td>%s</td><td>%s</td><td class="n">%s</td><td class="n">%s</td>'
            '<td>%s</td><td><ul>%s%s</ul>%s</td></tr>' % (
                status, _esc(r["activity"]),
                _esc(r["mapping"] or "no mapping"),
                ('<span class="pill p-%s">%s</span>' %
                 ("syn" if r["ciqSynthetic"] else "real",
                  "synthesised" if r["ciqSynthetic"] else "real order")),
                _esc(r["ciq"] or ""), r["steps"], r["interactive"], docs,
                gaps, warn,
                ('<div class="bad">%s</div>' % _esc(r["error"])) if r["error"] else ""))

    ok = sum(1 for r in results if r["ok"])
    html = _INDEX_HTML.replace("{{ROWS}}", "\n".join(rows)) \
                      .replace("{{GENERATED}}", payload["generated"]) \
                      .replace("{{SUMMARY}}", "%d of %d workflows expanded, "
                                              "%d documents, %d commands" % (
                                                  ok, len(results),
                                                  sum(len(r["nodes"]) for r in results),
                                                  sum(r["steps"] for r in results)))
    io.open(os.path.join(out_root, "index.html"), "w", encoding="utf-8",
            newline="\n").write(html)


def _rel(root, path):
    return os.path.relpath(path, root).replace(os.sep, "/")


def _esc(text):
    return (str(text if text is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


_INDEX_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CLICR MOPs</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--ink:#1b1f24;--muted:#5b6673;--line:#dde2e8;
 --ok:#1a7f4b;--okbg:#e8f6ee;--bad:#b4232a;--badbg:#fdecec;--warn:#8a5a00;
 --warnbg:#fff6e0;--accent:#0b5cad}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
 --bg:#12161b;--panel:#1a1f26;--ink:#e6edf3;--muted:#9aa7b4;--line:#2b333d;
 --ok:#5ddc9a;--okbg:#10291d;--bad:#ff8d8d;--badbg:#2d1414;--warn:#ffcc66;
 --warnbg:#2d2410;--accent:#6db3ff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1280px;margin:0 auto;padding:22px 16px 60px}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--muted);margin-bottom:18px}
table{width:100%;border-collapse:collapse;background:var(--panel);
 border:1px solid var(--line);border-radius:8px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted)}
td.n{text-align:right;font-variant-numeric:tabular-nums}
tr.bad td{background:var(--badbg)}
.k{color:var(--muted);font-size:12px}
a{color:var(--accent);margin-right:10px}
ul{margin:0;padding-left:16px}li{font-size:12px;color:var(--muted)}
code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
.pill{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px;font-weight:600}
.p-real{background:var(--okbg);color:var(--ok)}
.p-syn{background:var(--warnbg);color:var(--warn)}
.bad{color:var(--bad);font-size:12px}
</style></head><body><div class="wrap">
<h1>CLICR interactive MOPs</h1>
<div class="sub">{{SUMMARY}} &middot; generated {{GENERATED}}</div>
<table><thead><tr><th>Activity</th><th>Order data</th><th>CIQ</th>
<th class="n">Steps</th><th class="n">Interactive</th><th>Documents</th>
<th>Unresolved references</th></tr></thead>
<tbody>
{{ROWS}}
</tbody></table>
<p class="k">A synthesised order stands in for a real CIQ so the workflow can be
walked; the values in those documents are made up, and each one says so in its
<code>meta.generatedBy</code>.</p>
</div></body></html>
"""


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        description="Generate interactive MOPs for every workflow in the "
                    "templates/yaml directory.")
    p.add_argument("--yaml-dir", default=DEFAULT_YAML_DIR, dest="yaml_dir")
    p.add_argument("--template-dir", default=DEFAULT_TEMPLATE_DIR, dest="template_dir")
    p.add_argument("--ciq-dir", action="append", default=[], dest="ciq_dirs",
                   help="where to look for a real order JSON; repeatable")
    p.add_argument("--out", default=os.path.join("out", "all"))
    p.add_argument("--only", action="append", default=[],
                   help="only workflows whose name contains this; repeatable")
    p.add_argument("--groups", type=int, default=2,
                   help="nodeGroups in a synthesised CIQ (default 2)")
    p.add_argument("--rows", type=int, default=2,
                   help="records per table in a synthesised CIQ (default 2)")
    p.add_argument("--no-synth", action="store_true",
                   help="skip activities that have no real order JSON")
    p.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--quiet", action="store_true")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    ciq_dirs = args.ciq_dirs or DEFAULT_CIQ_DIRS

    if not os.path.isdir(args.yaml_dir):
        raise SystemExit("no workflow directory: %s" % args.yaml_dir)

    workflows = sorted(f for f in os.listdir(args.yaml_dir) if f.endswith(".yaml"))
    if args.only:
        workflows = [f for f in workflows
                     if any(token.lower() in f.lower() for token in args.only)]
    if not workflows:
        raise SystemExit("no workflow matched")

    out_root = os.path.abspath(args.out)
    ciq_root = os.path.join(out_root, "_ciq")
    for directory in (out_root, ciq_root):
        if not os.path.isdir(directory):
            os.makedirs(directory)

    extra = []
    for item in args.param:
        extra += ["--param", item]

    started = time.time()
    results = []
    for name in workflows:
        stem = os.path.splitext(name)[0]
        yaml_path = os.path.join(args.yaml_dir, name)
        mapping = find_mapping(stem, args.template_dir)
        ciq_path = find_ciq(stem, ciq_dirs)
        synthetic = False

        if not ciq_path:
            if args.no_synth:
                results.append({
                    "activity": stem, "workflow": name,
                    "mapping": os.path.basename(mapping) if mapping else None,
                    "ciq": None, "ciqSynthetic": False, "ok": False,
                    "nodes": [], "documents": [], "steps": 0, "interactive": 0,
                    "unresolved": [], "warnings": [], "out": out_root,
                    "error": "no CIQ (--no-synth)"})
                _log(results[-1], args.quiet)
                continue
            ciq_path = os.path.join(ciq_root, stem + ".json")
            try:
                ciqgen.generate_file(mapping, yaml_path, ciq_path,
                                     args.groups, args.rows)
                synthetic = True
            except Exception as exc:                       # noqa: BLE001
                results.append({
                    "activity": stem, "workflow": name,
                    "mapping": os.path.basename(mapping) if mapping else None,
                    "ciq": None,
                    "ciqSynthetic": False, "ok": False, "nodes": [],
                    "documents": [], "steps": 0, "interactive": 0,
                    "unresolved": [], "warnings": [], "out": out_root,
                    "error": "could not synthesise a CIQ: %s" % exc})
                _log(results[-1], args.quiet)
                continue

        out_dir = os.path.join(out_root, stem)
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        record = run_one(yaml_path, mapping, ciq_path, out_dir, synthetic, extra)
        results.append(record)
        _log(record, args.quiet)

    write_index(results, out_root, started)

    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    if not args.quiet:
        print("")
        print("%d of %d workflows expanded, %d documents, %d commands, %.1fs"
              % (len(ok), len(results), sum(len(r["nodes"]) for r in ok),
                 sum(r["steps"] for r in ok), time.time() - started))
        if bad:
            print("did not expand:")
            for r in bad:
                print("  %-58s %s" % (r["activity"], r["error"]))
        print("index: %s" % os.path.join(out_root, "index.html"))
    return 0 if not bad else 1


def _log(record, quiet):
    if quiet:
        return
    mark = "ok " if record["ok"] else "FAIL"
    source = "synth" if record.get("ciqSynthetic") else ("real " if record.get("ciq") else "  -  ")
    detail = record["error"] or "%2d node(s)  %4d steps  %3d interactive%s" % (
        len(record["nodes"]), record["steps"], record["interactive"],
        "  %d unresolved" % len(record["unresolved"]) if record["unresolved"] else "")
    print("%s %-5s %-60s %s" % (mark, source, record["activity"][:60], detail))


if __name__ == "__main__":
    sys.exit(main())

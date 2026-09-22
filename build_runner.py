#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
build_runner.py - the one-file CLICR runner
===========================================

    python build_runner.py [--out out/clicr-runner.html]

Inlines the vendored js-yaml, the engine mirror, the synthesiser, the walk, the
lenient YAML loader and the renderer into a single HTML file. Open it from
disk, drop in a workflow, a CIQ and the json-output mapping, and walk the
activity - no python, no server, no network.

It is the same code the generated MOPs carry, and the same walk mopgen.py runs:
test_expander_parity.py expands every workflow in the repo through both sides
and compares them step by step, so a document produced here is the document
produced there.
"""

import argparse
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "template")

PARTS = [
    ("{{UI_CSS}}", os.path.join(TEMPLATE, "ui.css")),
    ("{{JSYAML}}", os.path.join(TEMPLATE, "vendor", "js-yaml.umd.min.js")),
    ("{{ENGINE_JS}}", os.path.join(TEMPLATE, "engine.js")),
    ("{{SYNTH_JS}}", os.path.join(TEMPLATE, "synth.js")),
    ("{{EXPANDER_JS}}", os.path.join(TEMPLATE, "expander.js")),
    ("{{YAMLLOAD_JS}}", os.path.join(TEMPLATE, "yamlload.js")),
    ("{{UI_JS}}", os.path.join(TEMPLATE, "ui.js")),
]


def build():
    page = os.path.join(TEMPLATE, "runner.html")
    if not os.path.isfile(page):
        raise SystemExit("missing %s" % page)
    html = io.open(page, encoding="utf-8").read()
    for token, path in PARTS:
        if not os.path.isfile(path):
            raise SystemExit("missing %s" % path)
        html = html.replace(token, io.open(path, encoding="utf-8").read())
    if "{{" in html:
        leftover = html[html.index("{{"):html.index("{{") + 40]
        raise SystemExit("a placeholder was not filled: %s" % leftover)
    return html


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[3])
    parser.add_argument("--out", default=os.path.join("out", "clicr-runner.html"))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    html = build()
    directory = os.path.dirname(os.path.abspath(args.out))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    io.open(args.out, "w", encoding="utf-8", newline="\n").write(html)
    if not args.quiet:
        print("wrote %s  (%.0f KB, self-contained)" % (args.out, len(html) / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())

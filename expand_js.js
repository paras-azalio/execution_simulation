/* expand_js.js - run the BROWSER walk from the command line.
 *
 *     node expand_js.js --yaml w.yaml --ciq c.json [--json-template m.yaml]
 *                       [--node-index 0] [--param K=V]
 *
 * The page's expander has to agree with expander.py, or a workflow dropped
 * into the runner produces a different document from the same workflow run
 * through mopgen.py. This exposes the JS side so the two can be compared step
 * by step (test_expander_js.js does exactly that, over every workflow in the
 * repo).
 *
 * The modules are concatenated and run in ONE scope, which is how the browser
 * gets them - inlined into a single page - so this loads them the same way
 * rather than through require().
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const TEMPLATE = path.join(__dirname, "template");
const PARTS = ["engine.js", "synth.js", "expander.js"];

/** The browser API, loaded the way the browser loads it. */
function loadApi() {
  const source = PARTS.map(f => fs.readFileSync(path.join(TEMPLATE, f), "utf8")).join("\n\n") +
    "\n;globalThis.__API__ = { Expander, OutputTemplate, normalizeCiq, impliedValues," +
    " renderModeFor, DEFAULT_PARAMS, successOutput, failureOutput, sampleFor," +
    " interpolate, evalCond, applyRegisters, evalCriteria, resolveName };\n";

  const sandbox = {
    console, JSON, Math, Date, RegExp, Object, Array, String, Number, Boolean,
    Set, Map, Error, TypeError, parseInt, parseFloat, isNaN, isFinite,
    btoa: (s) => Buffer.from(s, "binary").toString("base64"),
    unescape, encodeURIComponent, decodeURIComponent,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: "clicr-browser-bundle.js" });
  return sandbox.__API__;
}

/** The vendored js-yaml, for reading a workflow outside a browser. */
function loadYaml() {
  const yaml = require(path.join(TEMPLATE, "vendor", "js-yaml.umd.min.js"));
  const loader = require(path.join(TEMPLATE, "yamlload.js"));
  return { load: (text) => loader.loadWorkflow(yaml, text).doc };
}

function parseArgs(argv) {
  const args = { param: [] };
  for (let i = 2; i < argv.length; i++) {
    const key = argv[i];
    if (!key.startsWith("--")) continue;
    const name = key.slice(2).replace(/-/g, "_");
    const value = argv[i + 1];
    if (name === "param") { args.param.push(value); i++; continue; }
    args[name] = value;
    i++;
  }
  return args;
}

function main() {
  const args = parseArgs(process.argv);
  if (!args.yaml || !args.ciq) {
    console.error("usage: node expand_js.js --yaml w.yaml --ciq c.json " +
                  "[--json-template m.yaml] [--node-index 0] [--param K=V]");
    process.exit(2);
  }
  const api = loadApi();
  const yaml = loadYaml();

  const workflow = yaml.load(fs.readFileSync(args.yaml, "utf8"));
  const data = api.normalizeCiq(JSON.parse(fs.readFileSync(args.ciq, "utf8")));
  const template = args.json_template
    ? new api.OutputTemplate(yaml.load(fs.readFileSync(args.json_template, "utf8")),
                             path.basename(args.json_template))
    : null;

  const params = Object.assign({}, api.DEFAULT_PARAMS);
  for (const item of args.param) {
    const at = String(item).indexOf("=");
    if (at > 0) params[item.slice(0, at).trim()] = item.slice(at + 1).trim();
  }

  const nodes = data.nodes || [];
  const index = parseInt(args.node_index || "0", 10);
  if (!nodes.length) {
    console.error("no nodes in " + args.ciq);
    process.exit(1);
  }
  const expander = new api.Expander(workflow, data, params, template);
  const expansion = expander.expand(nodes[index]);

  process.stdout.write(JSON.stringify({
    meta: expansion.node,
    params: params,
    steps: expansion.steps,
    warnings: expansion.warnings,
  }, null, 2));
}

if (require.main === module) main();

module.exports = { loadApi, loadYaml, PARTS, TEMPLATE };

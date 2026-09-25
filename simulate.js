/* simulate.js - walk a generated MOP headlessly and write the report set.
 *
 *     node simulate.js <mop_....json | mop_....html> [scenario] [--out DIR]
 *
 * scenario, any number of each - a step is named by its uid (s0040) or by its
 * YAML id (config_check_peerroutetable_exists; every instance of it):
 *
 *     --fail STEP               Failure, the synthesised failing output
 *     --success STEP            Success
 *     --custom STEP=FILE[:N]    what the node printed, from FILE; N is the exit
 *                               code, or the HTTP status for a REST step
 *     --timeout STEP            the command did not complete
 *     --all-success             every step the run reaches is Success
 *     --param KEY=VALUE         a request parameter (ROLLBACK_ONLY=true ...)
 *     --strict                  ExecutionOrchestrator exactly, including where
 *                               phase-level on_failure: stop misses a failure
 *                               inside a loop
 *
 * This is the run the page does, without the page: the same runtime.js, the
 * same report.js. It writes what a production run leaves behind, into its own
 * folder (default out/runs/<activity>_<node>_<time>/):
 *
 *     <ACT>_<node>_EXECUTION_REPORT.html        summary, failures, every step
 *     reports/json/<ACT>_EXECUTION_REPORT_<node>.json
 *     reports/logs/<node>_execution.log
 *     RESULTS.md                                 the short version
 *
 * Exit code: 0 when the walk ends SUCCESS, 1 on FAIL, 2 when steps are left
 * pending, 3 on bad arguments.
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const TEMPLATE = path.join(__dirname, "template");

function loadApi() {
  const parts = ["engine.js", "runtime.js", "report.js"];
  const source = parts.map(f => fs.readFileSync(path.join(TEMPLATE, f), "utf8")).join("\n\n") +
    "\n;globalThis.__API__ = { simulate, divergence, labelOf, buildReport, buildLog };\n";
  const sandbox = { console, JSON, Math, Date, RegExp, Object, Array, String, Number, Boolean,
                    Set, Map, Error, TypeError, parseInt, parseFloat, isNaN, isFinite,
                    btoa: s => Buffer.from(s, "binary").toString("base64"),
                    unescape, encodeURIComponent, decodeURIComponent };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: "clicr-runtime-bundle.js" });
  return sandbox.__API__;
}

function loadDoc(file) {
  if (file.toLowerCase().endsWith(".html")) {
    const json = file.slice(0, -5) + ".json";
    if (!fs.existsSync(json)) {
      throw new Error("no " + path.basename(json) + " next to the page - generate it with mopgen.py --json");
    }
    file = json;
  }
  const doc = JSON.parse(fs.readFileSync(file, "utf8"));
  if (!doc.steps || !doc.phases) throw new Error(file + " is not a MOP expansion (mopgen.py --json)");
  return doc;
}

function parseArgs(argv) {
  const out = { file: null, fail: [], success: [], custom: [], timeout: [], params: [],
                allSuccess: false, strict: false, out: null };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    const next = () => { if (i + 1 >= argv.length) throw new Error(a + " needs a value"); return argv[++i]; };
    if (a === "--fail") out.fail.push(next());
    else if (a === "--success") out.success.push(next());
    else if (a === "--custom") out.custom.push(next());
    else if (a === "--timeout") out.timeout.push(next());
    else if (a === "--param") out.params.push(next());
    else if (a === "--all-success") out.allSuccess = true;
    else if (a === "--strict") out.strict = true;
    else if (a === "--out") out.out = next();
    else if (a === "-h" || a === "--help") { out.help = true; }
    else if (!a.startsWith("--") && !out.file) out.file = a;
    else throw new Error("unknown argument " + a);
  }
  return out;
}

/** The uids a name stands for: a uid, or every instance of a YAML id. */
function resolve(doc, name) {
  const hit = doc.steps.filter(s => s.uid === name || s.step_id === name).map(s => s.uid);
  if (!hit.length) throw new Error("no step " + name + " in this document");
  return hit;
}

function main() {
  let args;
  try { args = parseArgs(process.argv); } catch (e) { console.error(e.message); return 3; }
  if (args.help || !args.file) {
    console.log(fs.readFileSync(__filename, "utf8").split("*/")[0].replace(/^\/\* ?/gm, "").replace(/^ \* ?/gm, ""));
    return args.help ? 0 : 3;
  }
  const api = loadApi();
  let doc;
  try { doc = loadDoc(args.file); } catch (e) { console.error(e.message); return 3; }
  const run = { steps: doc.steps, phases: doc.phases, loops: doc.loops || {}, vars: doc.vars || {},
                globals: doc.globals || [], params: doc.params || {} };
  const state = { choices: {}, custom: {}, exit: {}, status: {}, timeout: {}, params: {} };
  try {
    for (const p of args.params) {
      const at = p.indexOf("=");
      if (at <= 0) throw new Error("--param needs KEY=VALUE");
      state.params[p.slice(0, at).trim()] = p.slice(at + 1).trim();
    }
    for (const n of args.success) for (const uid of resolve(doc, n)) state.choices[uid] = "success";
    for (const n of args.fail) for (const uid of resolve(doc, n)) state.choices[uid] = "failure";
    for (const n of args.timeout) for (const uid of resolve(doc, n)) { state.choices[uid] = "custom"; state.timeout[uid] = true; }
    for (const spec of args.custom) {
      const eq = spec.indexOf("=");
      if (eq <= 0) throw new Error("--custom needs STEP=FILE[:N]");
      const name = spec.slice(0, eq);
      let file = spec.slice(eq + 1), code = null;
      const m = /^(.*):(-?\d+)$/.exec(file);
      if (m && !fs.existsSync(file)) { file = m[1]; code = m[2]; }
      const text = fs.readFileSync(file, "utf8");
      for (const uid of resolve(doc, name)) {
        const st = doc.steps.find(s => s.uid === uid);
        state.choices[uid] = "custom";
        state.custom[uid] = text;
        if (code !== null) { if (st.kind === "rest") state.status[uid] = code; else state.exit[uid] = code; }
      }
    }
  } catch (e) { console.error(e.message); return 3; }

  let result = api.simulate(run, state, { strict: args.strict });
  if (args.allSuccess) {
    // a verdict can open a path that was closed - mark until nothing new
    for (let round = 0; round < 25; round++) {
      let changed = false;
      for (const st of doc.steps) {
        if (result.rt[st.uid].state === "pending" && !state.choices[st.uid]) {
          state.choices[st.uid] = "success"; changed = true;
        }
      }
      if (!changed) break;
      result = api.simulate(run, state, { strict: args.strict });
    }
  }
  const other = api.simulate(run, state, { strict: !args.strict });
  const gap = api.divergence(run, args.strict ? other : result, args.strict ? result : other);

  const meta = doc.meta || {};
  const act = String(meta.activity || "ACTIVITY").replace(/[^A-Za-z0-9._-]+/g, "_");
  const node = String(meta.node || "node").replace(/[^A-Za-z0-9._-]+/g, "_");
  const stamp = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14);
  const outDir = args.out || path.join(__dirname, "out", "runs", act + "_" + node + "_" + stamp);
  fs.mkdirSync(path.join(outDir, "reports", "json"), { recursive: true });
  fs.mkdirSync(path.join(outDir, "reports", "logs"), { recursive: true });

  const report = api.buildReport(run, result, meta, doc.lint || []);
  fs.writeFileSync(path.join(outDir, act + "_" + node + "_EXECUTION_REPORT.html"), report);
  fs.writeFileSync(path.join(outDir, "reports", "logs", node + "_execution.log"), api.buildLog(run, result, meta) + "\n");
  const json = {
    meta: meta, strictEngine: args.strict, scenario: { choices: state.choices, params: state.params },
    summary: result.summary, engineGap: gap,
    steps: doc.steps.map(s => {
      const r = result.rt[s.uid];
      return { seq: s.seq, uid: s.uid, id: s.step_id, phase: s.phase, node: s.node_target,
               command: r.send || s.send, state: r.state, choice: r.choice, note: r.note,
               message: r.messageText, output: r.output, exitCode: r.exit,
               httpStatus: s.kind === "rest" ? r.status : undefined, routes: r.routes, handlerOf: r.handlerOf };
    }),
    log: result.log, finalVariables: result.vars,
  };
  fs.writeFileSync(path.join(outDir, "reports", "json", act + "_EXECUTION_REPORT_" + node + ".json"),
                   JSON.stringify(json, null, 2));

  const s = result.summary, c = s.counts;
  const failures = doc.steps.filter(st => result.rt[st.uid].state === "failure");
  const md = ["# " + (meta.activity || "") + " on " + (meta.node || ""), "",
    "**" + s.status + "**" + (s.stoppedAt ? " - stops at " + s.stoppedAt : "") + (args.strict ? " (strict engine)" : ""), "",
    "| success | warning | failure | skipped | not executed | pending |", "|---|---|---|---|---|---|",
    "| " + [c.success, c.warning, c.failure, c.skipped, c.notrun, c.pending].map(x => x || 0).join(" | ") + " |", "",
    "ROLLBACK_REQUIRED=" + s.engine.ROLLBACK_REQUIRED + " · ROLLBACK_ENABLED=" + s.engine.ROLLBACK_ENABLED, ""]
    .concat(failures.length ? ["## Failures", ""].concat(failures.map(st => {
      const r = result.rt[st.uid];
      return "- " + st.uid + " `" + (st.step_id || "") + "` " + (r.desc || st.description) + " - " + (r.messageText || "") +
             ((r.routes || []).length ? "\n  - " + r.routes.join("\n  - ") : "");
    })) : [])
    .concat(gap ? ["", "## Engine gap", "", "From " + gap.uid + " the intended rule gives " + gap.intended +
                   ", the engine as written gives " + gap.engine + "."] : []);
  fs.writeFileSync(path.join(outDir, "RESULTS.md"), md.join("\n") + "\n");

  console.log((meta.activity || "") + " / " + (meta.node || "") + ": " + s.status +
              (s.stoppedAt ? " - stops at " + s.stoppedAt : ""));
  console.log("  success " + (c.success || 0) + "  warning " + (c.warning || 0) + "  failure " + (c.failure || 0) +
              "  skipped " + (c.skipped || 0) + "  not executed " + (c.notrun || 0) + "  pending " + (c.pending || 0));
  console.log("  ROLLBACK_REQUIRED=" + s.engine.ROLLBACK_REQUIRED + "  ROLLBACK_ENABLED=" + s.engine.ROLLBACK_ENABLED);
  if (gap) console.log("  engine gap from " + gap.uid + ": intended " + gap.intended + ", engine " + gap.engine);
  console.log("  wrote " + outDir);
  return s.status === "SUCCESS" ? 0 : (s.status === "FAIL" ? 1 : 2);
}

if (require.main === module) process.exit(main());
module.exports = { loadApi, loadDoc, resolve };

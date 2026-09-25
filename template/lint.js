/* lint.js - lint.py, in the browser.
 *
 * What the engine will do with a workflow that its author did not mean: the
 * same rules, the same messages, so a file dropped into the runner is judged
 * exactly as `python lint.py` judges it. test_expander_parity.py compares the
 * two over every workflow (duplicate keys aside - the runner reports those
 * from its own lenient loader).
 */

const LINT_CONDITION_FIELDS = ["when", "skip_when", "continue_when", "break_when"];
const LINT_TEXT_FIELDS = ["send", "command_description", "description", "message", "value",
                          "path", "body_json", "body_file", "local_path", "remote_path", "subject",
                          "body_text", "body_html"];
const LINT_SHELL_TOKEN = /^[#!]|[#%/^,]|:[-=+?]|\[[@*]\]/;
const LINT_KNOWN_RUNTIME = ("NODE_NAME nodeName node metadata nodeGroup crGroup nodeGroups nodes node_row niamID niamId " +
  "NIAM_ID currentNode currentCrGroup currentNodeData currentNodeCollection VERSION version " +
  "http_status __HAS_CONTINUED_FAILURE ACTIVITY_EXECUTION_STATUS ACTIVITY_COMMAND_SUCCESS_LIST " +
  "ACTIVITY_COMMAND_FAILURE_LIST ACTIVITY_COMMAND_SKIPPED_LIST LAST_ACTIVITY_EXECUTED " +
  "ROLLBACK_ENABLED nodeType activity tableKeys schemaVersion meta step execution phase failure " +
  "true false null").split(/\s+/);
const LINT_ORDER = { error: 0, warning: 1, info: 2 };

function lintClip(text, width) {
  width = width || 70;
  text = String(text).split(/\s+/).filter(Boolean).join(" ");
  return text.length <= width ? text : text.slice(0, width - 3) + "...";
}
function lintFew(ids, n) {
  n = n || 6;
  return ids.slice(0, n).map(String).join(", ") + (ids.length > n ? " ..." : "");
}
const lintIsMap = v => v && typeof v === "object" && !Array.isArray(v);

function lintWorkflow(workflow, data, params, duplicates) {
  workflow = workflow || {};
  const out = [];
  const ids = assignStepIds(workflow);
  const producers = runtimeProducers(workflow, ids);
  const nodes = workflow.nodes || {};
  const phases = phaseBlocks(workflow);
  const phaseIds = new Set(phases.map(([k, v]) => String(v.name || k).trim().toLowerCase()));
  const all = [];
  const walk = (steps, phase, stack) => {
    for (const step of steps || []) {
      if (!lintIsMap(step)) continue;
      all.push([step, phase, stack]);
      if (step.type === "loop") walk(step.steps, phase, stack.concat([step]));
    }
  };
  walk(workflow.steps, null, []);
  for (const [key, block] of phases) walk(block.steps, key, []);
  const byId = {};
  for (const [step] of all) { const sid = ids.get(step); (byId[sid] = byId[sid] || []).push(step); }

  const known = new Set(LINT_KNOWN_RUNTIME.concat(Object.keys(producers))
    .concat(Object.keys(DEFAULT_PARAMS)).concat(Object.keys(params || {}))
    .concat(Object.keys(((workflow.globals || {}).vars) || {}))
    .concat(Object.keys(((workflow.globals || {}).secrets) || {}).map(k => "SECRET." + k)));
  for (const [step] of all) if (step.type === "loop" && step.item_var) known.add(String(step.item_var));
  for (const [, block] of phases) for (const k of Object.keys(block.vars || {})) known.add(k);
  if (lintIsMap(data)) {
    for (const k of Object.keys(data)) known.add(k);
    for (const node of data.nodes || []) if (lintIsMap(node)) for (const k of Object.keys(node)) known.add(k);
  }

  const add = (rule, severity, message, where) => out.push({ rule, severity, message, where: where || "" });
  const runNoNext = [], exitIgnored = [];

  for (const [step, phase] of all) {
    const sid = ids.get(step);
    const where = "step " + sid + (phase ? " (phase " + phase + ")" : "");

    for (const [field, text] of lintStringsOf(step)) {
      const re = new RegExp(PH.source, "g");
      let m;
      while ((m = re.exec(text))) {
        const bare = m[1].trim();
        if (/^(base64:|ENV\.|SECRET\.)/.test(bare)) continue;
        if (LINT_SHELL_TOKEN.test(bare)) {
          add("shell-expansion", "error",
              "${" + bare + "} in " + field + " is replaced by the engine before the shell runs - it " +
              "becomes \"\"; write $" + lintShellName(bare) + " or escape it", where);
        }
      }
    }

    for (const field of LINT_CONDITION_FIELDS) {
      const expr = step[field];
      if (typeof expr !== "string" || !expr.trim()) continue;
      lintCondition(add, expr, where + " " + field, known);
    }
    if (step.type === "loop") {
      const item = step.item_var;
      for (const field of ["when", "skip_when"]) {
        const expr = step[field];
        if (typeof expr === "string" && item && lintReads(expr, item)) {
          add("loop-gate-item-var", "error",
              "loop " + sid + " gates on its own item_var " + item + " in " + field +
              " - evaluated once, before any item is bound", where);
        }
      }
    }

    const node = step.node;
    if (step.type !== "loop" && typeof node === "string" && node.trim() && node.indexOf("${") < 0 &&
        node.trim().toLowerCase() !== "local" && !(node in nodes)) {
      add("unknown-node", "error", "node " + node + " is not defined under nodes: - the workflow will not load", where);
    }
    if (lintIsMap(step.rest) && typeof node === "string" && (node in nodes)) {
      if (!(((nodes[node] || {}).rest || {}).base_url)) add("rest-no-base-url", "warning", "node " + node + " has no rest.base_url", where);
    }

    for (const entry of step.register || []) {
      if (!lintIsMap(entry) || typeof entry.regex !== "string") continue;
      const pattern = entry.regex;
      if (pattern.indexOf("(?P<") >= 0) {
        add("python-group-syntax", "error", "register regex " + lintClip(pattern) + " uses (?P<name>) - java needs (?<name>)", where);
      }
      const reason = javaRegexError(pattern.replace(new RegExp(PH.source, "g"), "x"));
      if (reason) add("java-regex", "error", "register regex " + lintClip(pattern) + ": " + reason, where);
    }

    const validation = step.validation;
    if (lintIsMap(validation)) {
      for (const branch of ["success", "failure", "warning"]) {
        const block = validation[branch];
        if (!lintIsMap(block)) continue;
        if (Array.isArray(block.vars)) add("vars-list", "error", "validation " + branch + " vars is a list - it has to be a mapping", where);
        const criteria = block.criteria;
        if (lintIsMap(criteria)) {
          lintCriteria(add, criteria, where + " " + branch + " criteria", branch === "success");
          if (typeof criteria.expr === "string") lintCondition(add, criteria.expr, where + " " + branch + " criteria", known);
        }
      }
    }

    const behaviour = step.on_failure;
    if (lintIsMap(behaviour)) {
      const list = v => Array.isArray(v) ? v : (v ? [v] : []);
      const run = list(behaviour.run), then = list(behaviour.then);
      for (const target of run.concat(then)) lintTarget(add, String(target), byId, phaseIds, where);
      if (run.length && String(behaviour.next === undefined || behaviour.next === null ? "" : behaviour.next).toLowerCase() !== "continue") runNoNext.push(sid);
    }

    const prompt = step.prompt_regex;
    if (step.use_exit_code === true && typeof prompt === "string" && prompt.trim()) exitIgnored.push(sid);
  }

  if (runNoNext.length) {
    add("run-without-next", "warning",
        runNoNext.length + " step(s) run on_failure handlers without `next: continue` - after the handlers " +
        "(a token refresh and a retry, say) the engine STOPS even if they succeeded: " + lintFew(runNoNext));
  }
  if (exitIgnored.length) {
    add("use-exit-code-ignored", "warning",
        exitIgnored.length + " step(s) set use_exit_code: true and a step-level prompt_regex - the engine never " +
        "reads use_exit_code and does NOT check their exit code: " + lintFew(exitIgnored));
  }

  for (const [key, block] of phases) {
    const then = Array.isArray(block.then) ? block.then : (block.then ? [block.then] : []);
    for (const target of then) lintTarget(add, String(target), byId, phaseIds, "phase " + key + " then:");
    if (String(block.on_failure === undefined || block.on_failure === null ? "" : block.on_failure).trim() === "stop") {
      const blind = lintBlindSpots(block.steps || [], ids);
      if (blind.length) {
        add("phase-stop-blind", "warning",
            "phase " + (block.name || key) + " declares on_failure: stop, but the engine never sees a failure " +
            "absorbed inside a loop - " + blind.slice(0, 4).join("; ") + (blind.length > 4 ? " ..." : "") +
            ". A failure there lets the run go on to the next phase.", "phase " + key);
      }
    }
  }

  for (const sid of Object.keys(byId).sort()) {
    if (byId[sid].length > 1) add("duplicate-step-id", "warning", "id " + sid + " is used by " + byId[sid].length + " steps - on_failure run: finds only the last");
  }
  for (const text of duplicates || []) add("duplicate-key", "warning", text);

  const rank = f => (f.severity in LINT_ORDER) ? LINT_ORDER[f.severity] : 9;
  return out.map((f, i) => [f, i]).sort((a, b) => (rank(a[0]) - rank(b[0])) || (a[1] - b[1]))
    .map(p => p[0]);
}

function lintTarget(add, target, byId, phaseIds, where) {
  let name = target.trim(), kind = null;
  if (name.toLowerCase().startsWith("step:")) { kind = "step"; name = name.slice(5).trim(); }
  else if (name.toLowerCase().startsWith("phase:")) { kind = "phase"; name = name.slice(6).trim(); }
  if (name.indexOf("${") >= 0) return;
  if (kind === "step" && !(name in byId)) add("handler-not-found", "error", "on_failure target step:" + name + " does not exist", where);
  else if (kind === "phase" && !phaseIds.has(name.toLowerCase())) {
    add("handler-not-found", "error", "target phase:" + name + " matches no phase id (a phase's id is its name:)", where);
  } else if (kind === null && !(name in byId) && !phaseIds.has(name.toLowerCase())) {
    add("handler-not-found", "error", "target " + name + " is neither a step id nor a phase", where);
  }
}

function lintCriteria(add, criteria, where, isSuccess) {
  if (typeof criteria.regex === "string") lintCriteriaRegex(add, criteria.regex, where);
  for (const key of ["all", "any"]) {
    for (const item of criteria[key] || []) {
      if (!lintIsMap(item)) continue;
      if ("expr" in item) {
        add("expr-in-all-any", "error",
            key + "[] item `expr: " + lintClip(item.expr) + "` is compared to a result attribute named \"expr\" and " +
            "is always false - move it up to the criteria's own expr", where);
      }
      if (typeof item.regex === "string") lintCriteriaRegex(add, item.regex, where);
    }
  }
  if (isSuccess && typeof criteria.expr === "string" && lintTautology(criteria.expr)) {
    add("tautology", "warning", "criteria " + lintClip(criteria.expr) + " is true whatever the command prints - the step can never fail on it", where);
  }
}

function lintCriteriaRegex(add, pattern, where) {
  if (pattern.indexOf("${") >= 0) {
    add("criteria-regex-placeholder", "error",
        "criteria regex " + lintClip(pattern) + ": criteria regexes are not interpolated, and java rejects the '{'", where);
    return;
  }
  if (pattern.indexOf("(?P<") >= 0) add("python-group-syntax", "error", "criteria regex " + lintClip(pattern) + " uses (?P<name>)", where);
  const reason = javaRegexError(pattern);
  if (reason) add("java-regex", "error", "criteria regex " + lintClip(pattern) + ": " + reason, where);
}

function lintCondition(add, expr, where, known) {
  const text = expr.trim();
  const inner = (text.startsWith("${") && text.endsWith("}")) ? text.slice(2, -1) : text;
  if (lintUnquoted(inner).indexOf("(") >= 0) add("parentheses", "error", lintClip(expr) + " uses parentheses, which conditions do not support", where);
  for (const [left, right] of lintOperands(inner)) {
    for (const [side, operand] of [["left", left], ["right", right]]) {
      if (operand === null) continue;
      let raw = operand.trim();
      if (raw.startsWith("${") && raw.endsWith("}")) raw = raw.slice(2, -1).trim();
      if (!raw || raw[0] === "'" || raw[0] === '"' || /^[0-9]/.test(raw) || raw[0] === "-") continue;
      const root = raw.split(".")[0].split("[")[0].replace(/^[() ]+|[() ]+$/g, "");
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(root)) continue;
      if (["exitCode", "exit_code", "EXIT_CODE"].indexOf(root) >= 0) {
        add("exit-code-variable", "error", lintClip(expr) + " reads " + root + ", which the engine never sets - use criteria exit_code: 0", where);
      } else if (known.has(root) || raw.indexOf(".") >= 0 || root.startsWith("SECRET")) {
        continue;
      } else if (side === "left") {
        add("undefined-variable", "warning", lintClip(expr) + " reads " + root +
            ", which no step, global, parameter or CIQ field sets - it compares as \"\"", where);
      } else if (root.indexOf("_") >= 0) {
        add("undefined-variable", "warning", lintClip(expr) + " compares against " + root +
            ", which nothing sets - so against the literal text \"" + root + "\"", where);
      }
    }
  }
}

function lintOperands(inner) {
  const out = [];
  for (const clause of splitTop(inner, "||")) {
    for (let leaf of splitTop(clause, "&&")) {
      leaf = leaf.trim();
      let found = false;
      for (const op of [" notStartsWith ", " startsWith ", " notContains ", " contains "]) {
        const at = leaf.indexOf(op);
        if (at > 0) { out.push([leaf.slice(0, at), leaf.slice(at + op.length)]); found = true; break; }
      }
      if (found) continue;
      for (const op of ["==", "!=", ">=", "<=", ">", "<"]) {
        const at = findOp(leaf, op);
        if (at > 0) { out.push([leaf.slice(0, at), leaf.slice(at + op.length)]); found = true; break; }
      }
      if (!found) out.push([leaf, null]);
    }
  }
  return out;
}

function lintTautology(expr) {
  let text = expr.trim();
  if (text.startsWith("${") && text.endsWith("}")) text = text.slice(2, -1);
  const eq = new Set(), ne = new Set();
  for (const clause of splitTop(text, "||")) {
    if (splitTop(clause, "&&").length > 1) continue;
    for (const [op, bucket] of [["==", eq], ["!=", ne]]) {
      const at = findOp(clause, op);
      if (at > 0) { bucket.add(clause.slice(0, at).trim() + "\u0000" + clause.slice(at + 2).trim()); break; }
    }
  }
  for (const k of eq) if (ne.has(k)) return true;
  return false;
}

function lintBlindSpots(steps, ids) {
  const out = [];
  const walk = (items, depth) => {
    for (const step of items || []) {
      if (!lintIsMap(step)) continue;
      const mode = typeof step.on_failure === "string" ? step.on_failure.trim().toLowerCase() : "";
      if (step.type === "loop") {
        if (depth > 0 && (mode === "continue" || mode === "warning")) out.push("loop " + ids.get(step) + " (on_failure: " + mode + ")");
        walk(step.steps, depth + 1);
      } else if (depth > 0 && (mode === "continue" || mode === "warning")) {
        out.push("step " + ids.get(step) + " (on_failure: " + mode + ")");
      }
    }
  };
  walk(steps, 0);
  return out;
}

function lintReads(expr, name) {
  const text = expr.trim();
  const wrapped = text.startsWith("${") && text.endsWith("}");
  const names = wrapped ? bareNames(text.slice(2, -1)) : tokensIn(text);
  return names.some(n => n.split(".")[0].split("[")[0] === name);
}

function lintUnquoted(text) {
  let out = "", quote = null;
  for (const c of text) {
    if (quote) { if (c === quote) quote = null; continue; }
    if (c === "'" || c === '"') { quote = c; continue; }
    out += c;
  }
  return out;
}

function lintStringsOf(step) {
  const out = [];
  for (const field of LINT_TEXT_FIELDS) if (typeof step[field] === "string") out.push([field, step[field]]);
  for (const key of ["rest", "sftp", "email"]) {
    const block = step[key];
    if (lintIsMap(block)) {
      for (const field of LINT_TEXT_FIELDS) if (typeof block[field] === "string") out.push([key + "." + field, block[field]]);
    }
  }
  for (const entry of step.register || []) {
    if (lintIsMap(entry) && typeof entry.value === "string") out.push(["register value", entry.value]);
  }
  const validation = step.validation;
  if (lintIsMap(validation)) {
    for (const branch of ["success", "failure", "warning"]) {
      const block = validation[branch];
      if (!lintIsMap(block)) continue;
      if (typeof block.message === "string") out.push([branch + " message", block.message]);
      for (const [name, value] of branchVarItems(block)) {
        if (typeof value === "string") out.push([branch + " vars " + name, value]);
      }
    }
  }
  return out;
}

function lintShellName(token) {
  const m = /^[#!]?([A-Za-z_][A-Za-z0-9_]*)/.exec(token);
  return m ? m[1] : "{" + token + "}";
}

if (typeof module !== "undefined" && module.exports) module.exports = { lintWorkflow };

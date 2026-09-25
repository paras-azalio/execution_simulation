/* expander.js - expander.py + the CIQ normalisation, in the browser.
 *
 * Walks a workflow the way the engine would, once per node, and produces the
 * flat step list a MOP is made of - the same payload mopgen.py emits, so the
 * page renders a workflow the operator has just dropped in exactly as it
 * renders one generated on the command line.
 *
 * Mirrors:
 *   cliautomation/exec/ExecutionOrchestrator.java   phases, loops, gating
 *   cliautomation/api/JsonSchemaSupport.java        the CIQ normalisation
 *   execution_simulation/expander.py                three-state gating,
 *                                                   assumed success
 *
 * THREE-STATE GATING: a gate is True (runs), False (skipped) or null
 * (undecidable here, because an operand comes from command output). An
 * undecidable step is ALWAYS kept, carrying its expression, so the page can
 * decide it once the operator supplies the data.
 *
 * test_expander_js.js runs every workflow in the repo through this and through
 * expander.py and compares the result step by step.
 */

const LOOP_TYPES = ["loop"];
const MODE_CHECKLIST = "checklist";
const MODE_INTERACTIVE = "interactive";
const ACTIVITY_WORDS = ["ACTIVITY", "ROLLBACK", "CONFIG", "EXECUTION"];
const CHECK_WORDS = ["HEALTH", "PRECHECK", "PRE_CHECK", "POSTCHECK", "POST_CHECK",
                     "PRE_NODE", "POST_NODE"];

/* ------------------------------------------------------------------ *
 *  CIQ                                                                *
 * ------------------------------------------------------------------ */
/** JsonSchemaSupport.extractDataSection() + normalizeDataSection(). */
function normalizeCiq(payload) {
  let data = payload;
  if (data && typeof data === "object" && !Array.isArray(data) &&
      data.data && typeof data.data === "object" && !Array.isArray(data.data)) {
    data = data.data;
  }
  if (!data || typeof data !== "object") return data;

  const version = String(data.schemaVersion === undefined ? "1" : data.schemaVersion).trim();
  if (version !== "2") return data;

  const meta = data.meta;
  if (meta && typeof meta === "object") {
    for (const key of ["nodeType", "activity", "tableKeys"]) {
      if (!(key in data) && key in meta) data[key] = meta[key];
    }
  }
  if (Array.isArray(data.nodeGroups) && data.nodeGroups.length && !("nodes" in data)) {
    const flat = [];
    for (const group of data.nodeGroups) {
      if (!group || typeof group !== "object") continue;
      const inherited = [["nodeGroup", group.nodeGroup], ["crGroup", group.crGroup],
                         ["email", group.email], ["configSequences", group.configSequences]];
      for (const entry of group.nodes || []) {
        if (!entry || typeof entry !== "object") continue;
        const node = Object.assign({}, entry);
        for (const [key, value] of inherited) {
          if (!(key in node) && value !== undefined && value !== null) node[key] = value;
        }
        flat.push(node);
      }
    }
    data.nodes = flat;
  }
  return data;
}

/* ------------------------------------------------------------------ *
 *  the json-output mapping, enough of it to explain a blank           *
 * ------------------------------------------------------------------ */
const SQL_WORDS = ["DISTINCT", "AS", "WHERE", "AND", "OR", "IN", "FROM", "NOT"];
const CELL_RE = new RegExp(
  "\\b([A-Za-z_][A-Za-z0-9_ ]*?)\\." +
  "(?:'([^']+)'|\"([^\"]+)\"|" +
  "([A-Za-z_][A-Za-z0-9_]*(?:\\s+(?!(?:WHERE|AND|OR|IN|AS|NOT|FROM)\\b)[A-Za-z0-9_]+)*))",
  "gi");

/**
 * What the mapping promises the CIQ will contain.
 *
 * A field the mapping never emits is a document defect - the workflow reads
 * something no CIQ for this activity can carry. A field it does emit but this
 * CIQ lacks is an order gap. Both render as a blank, which is why the document
 * has to tell them apart. Mirrors ciq.OutputTemplate.
 */
class OutputTemplate {
  constructor(doc, name) {
    this.name = name || "the json-output mapping";
    this.doc = doc || null;
    const data = (doc && typeof doc.data === "object") ? doc.data : {};
    this.sheetColumns = {};
    this.fields = {};
    this.wildcard = false;
    this.scan(data, null);
  }

  static cellsIn(text) {
    const out = [];
    CELL_RE.lastIndex = 0;
    let m;
    while ((m = CELL_RE.exec(String(text || "")))) {
      const sheet = (m[1] || "").trim();
      const column = (m[2] || m[3] || m[4] || "").trim();
      if (!sheet || !column) continue;
      if (SQL_WORDS.indexOf(sheet.toUpperCase()) >= 0) continue;
      if (SQL_WORDS.indexOf(column.toUpperCase()) >= 0) continue;
      out.push([sheet, column]);
    }
    return out;
  }

  static sheetOf(each) {
    const words = String(each || "").match(/\$?[A-Za-z_][A-Za-z0-9_ ]*/g) || [];
    for (const raw of words) {
      const word = raw.trim();
      if (!word || SQL_WORDS.indexOf(word.toUpperCase()) >= 0) continue;
      return word.split(".")[0];
    }
    return null;
  }

  noteCells(text) {
    for (const [sheet, column] of OutputTemplate.cellsIn(text)) {
      (this.sheetColumns[sheet] = this.sheetColumns[sheet] || new Set()).add(column);
    }
  }

  scan(node, sheet) {
    if (Array.isArray(node)) {
      for (const item of node) this.scan(item, sheet);
      return;
    }
    if (node && typeof node === "object") {
      if (typeof node._each === "string") {
        sheet = OutputTemplate.sheetOf(node._each) || sheet;
        this.noteCells(node._each);
      }
      if ("_row" in node) this.wildcard = true;
      for (const key of Object.keys(node)) {
        const value = node[key];
        if (key.charAt(0) === "_") {
          if (key !== "_each" && key !== "_row") this.scan(value, sheet);
          continue;
        }
        this.record(key, value, sheet);
        this.scan(value, sheet);
      }
      return;
    }
    if (typeof node === "string") this.noteCells(node);
  }

  record(key, value, sheet) {
    if (Array.isArray(value)) return;
    if (value && typeof value === "object") {
      if (["_ref", "_col", "_row_join", "_join"].some(d => d in value)) {
        if (!(key in this.fields)) this.fields[key] = { sheet: sheet, column: key };
      }
      return;
    }
    if (typeof value !== "string") {
      if (!(key in this.fields)) this.fields[key] = { sheet: sheet, column: key };
      return;
    }
    const cells = OutputTemplate.cellsIn(value);
    if (cells.length) this.fields[key] = { sheet: cells[0][0], column: cells[0][1] };
    else if (!(key in this.fields)) this.fields[key] = { sheet: sheet, column: value.trim() };
  }

  /** true / null (undecidable) / false */
  emits(field) {
    if (!this.doc) return null;
    if (field in this.fields) return true;
    for (const sheet of Object.keys(this.sheetColumns)) {
      if (this.sheetColumns[sheet].has(field)) return true;
    }
    return this.wildcard ? null : false;
  }

  explain(field) {
    if (!this.doc) return null;
    const verdict = this.emits(field);
    if (verdict === true) {
      const source = this.fields[field];
      const where = source && source.sheet && source.column && source.sheet !== source.column
        ? " from " + source.sheet + "." + source.column : "";
      return this.name + " fills it" + where + ", so this order has no value in that column";
    }
    if (verdict === null) {
      return this.name + ' copies whole rows (_row: "*"), so this column exists only if ' +
             "the CIQ workbook carried it - this one did not";
    }
    return this.name + " never fills it - no sheet in the mapping produces " + field;
  }
}

/** The CIQ column a token reaches for, or null if it is not CIQ data. */
function ciqField(token) {
  const text = String(token || "").trim();
  if (!text || text.indexOf(".") < 0) return null;
  if (/^(ENV\.|SECRET\.|base64:)/.test(text)) return null;
  const tail = text.split(".").pop().split("[")[0].trim();
  return tail || null;
}

/* ------------------------------------------------------------------ *
 *  implied values                                                     *
 * ------------------------------------------------------------------ */
/**
 * What a success criteria block says the registers must hold.
 *
 *   ${IMSIRESULT == "success"}      -> {IMSIRESULT: "success"}
 *   ${IDLE_OK_COUNT >= 8}          -> {IDLE_OK_COUNT: "8"}
 *   ${A == "x" || B == "y"}        -> first clause only
 *   ${LDAPFIELDS != ""}            -> {} (not derivable)
 */
function impliedValues(criteria, vars, firstClauseOnly, owned) {
  if (firstClauseOnly === undefined) firstClauseOnly = true;
  const out = {};
  if (!criteria || typeof criteria !== "object") return out;
  for (const key of ["all", "any"]) {
    const block = criteria[key];
    if (Array.isArray(block)) {
      for (const item of block) {
        Object.assign(out, impliedValues(item, vars, firstClauseOnly, owned));
        if (key === "any" && firstClauseOnly && Object.keys(out).length) break;
      }
    }
  }
  const expr = criteria.expr;
  if (typeof expr === "string" && expr.trim()) {
    Object.assign(out, impliedFromExpr(expr, vars, firstClauseOnly, owned));
  }
  if (criteria.http_status !== undefined && criteria.http_status !== null && !("http_status" in out)) {
    out.http_status = String(criteria.http_status);
  }
  return out;
}

function impliedFromExpr(expr, vars, firstClauseOnly, owned) {
  let text = expr.trim();
  if (text.startsWith("${") && text.endsWith("}")) text = text.slice(2, -1).trim();
  const out = {};
  let clauses = splitTop(text, "||");
  if (firstClauseOnly && clauses.length) clauses = clauses.slice(0, 1);
  for (const clause of clauses) {
    for (const leaf of splitTop(clause, "&&")) {
      const [name, value] = impliedFromLeaf(leaf.trim(), vars, owned);
      if (name && !(name in out)) out[name] = value;
    }
  }
  return out;
}

function impliedFromLeaf(leaf, vars, owned) {
  for (const op of ["==", ">=", "<=", ">", "<"]) {
    const at = findOp(leaf, op);
    if (at <= 0) continue;
    const name = leaf.slice(0, at).trim();
    const raw = leaf.slice(at + op.length).trim();
    if (!/^[A-Za-z0-9_]+$/.test(name)) return [null, null];   // a dotted path is CIQ data
    const quoted = raw.length >= 2 && raw[0] === raw[raw.length - 1] &&
                   (raw[0] === "'" || raw[0] === '"');
    const literal = operand(raw, vars);
    if (op === "==" && owned && !quoted && /^[A-Za-z_][A-Za-z0-9_]*$/.test(raw) &&
        owned.has(raw) && !owned.has(name)) {
      // `${EARLIER == MINE}`: this step's own register is the side to set
      const known = resolveName(name, vars);
      if (known !== undefined && known !== null && known !== "") {
        return [raw, typeof known === "string" ? known : stringify(known)];
      }
    }
    if (!quoted && literal === raw && /^[A-Za-z_][A-Za-z0-9_]*$/.test(raw)) {
      // The right-hand side is another VARIABLE, as in
      // `${LINK_UP_COUNT == REMOTE_NODE_COUNT}`: no single value is implied -
      // unless the LEFT side is already known (the checksum pattern), when
      // success means this step captures that same value.
      const known = resolveName(name, vars);
      if (op === "==" && known !== undefined && known !== null && known !== "") {
        return [raw, typeof known === "string" ? known : stringify(known)];
      }
      return [null, null];
    }
    if (op === "==") return [name, literal];
    const number = parseFloat(literal);
    if (isNaN(number)) return [null, null];
    let value = number;
    if (op === ">") value += 1;
    else if (op === "<") value -= 1;
    return [name, Number.isInteger(value) ? String(value) : String(value)];
  }
  return [null, null];
}

/* ------------------------------------------------------------------ *
 *  the walk                                                           *
 * ------------------------------------------------------------------ */
function renderModeFor(phaseId, phaseName) {
  const label = ((phaseId || "") + " " + (phaseName || "")).toUpperCase();
  if (CHECK_WORDS.some(w => label.indexOf(w) >= 0)) return MODE_CHECKLIST;
  return MODE_INTERACTIVE;
}

/** The macro-server Arglist values a real run supplies (ciq.DEFAULT_PARAMS).
 *  The first seven are SimActivityRunner's; the rest are what MopExecutionUtil
 *  and CliAutomationEngine put in scope from the GRC section and the Arglist,
 *  with the simulator's values. activityParams() fills NODE_TYPE,
 *  SUB_ACTIVITY_NAME and INPUT_JSON_FILE_NAME for the activity at hand. */
const DEFAULT_PARAMS = {
  ORDER_NO: "12345", PARENT_REQ_ID: "12345", CHILD_REQ_ID: "10057",
  CR_GROUP: "CR1", CR_NAME: "CR1", NODE_TYPE: "PGW_RDS", REQ_TYPE: "1",
  SUB_ACTIVITY_NAME: "", ROLLBACK_ONLY: "false",
  INPUT_JSON_FILE_NAME: "/opt/clicr/input/order.json",
  OUTPUT_LOGS_FILE_LOCATION: "/opt/clicr/runs/10057/reports/logs",
  OUTPUT_JSON_REPORT_NAME: "/opt/clicr/runs/10057/reports/json",
  MOP_EXEC_LOG_FILE: "/opt/clicr/runs/10057/reports/logs/execution.log",
  REPO_IP: "127.0.0.1", REPO_USER: "installer", REPO_PASSWORD: "ROOT",
  NIAM_IP: "127.0.0.1", M2MPORT: "22", M2MUSER: "admin", M2MPASSWORD: "admin",
};

/** NODE_TYPE, SUB_ACTIVITY_NAME and INPUT_JSON_FILE_NAME for one activity -
 *  ciq.activity_params(). The template file is <NODE_TYPE>_<activity>.yaml
 *  (MopExecutionUtil), so the node type is what is left of the file name once
 *  the CIQ's activity is taken off the end. */
function activityParams(stem, data, ciqName) {
  const out = {};
  const activity = String((data && data.activity) || "").trim();
  let nodeType = String((data && data.nodeType) || "").trim();
  stem = String(stem || "").replace(/\.ya?ml$/i, "");
  if (activity && stem.toUpperCase().endsWith("_" + activity.toUpperCase())) {
    nodeType = stem.slice(0, stem.length - activity.length - 1);
  }
  if (!nodeType && stem) nodeType = stem.split("_")[0];
  if (nodeType) out.NODE_TYPE = nodeType;
  if (activity) out.SUB_ACTIVITY_NAME = activity;
  if (ciqName) out.INPUT_JSON_FILE_NAME = "/opt/clicr/input/" + ciqName;
  return out;
}

/* ------------------------------------------------------------------ *
 *  step ids - YamlWorkflowLoader.ensureStepIds()                      *
 * ------------------------------------------------------------------ */
function phaseBlocks(workflow) {
  const phases = (workflow && workflow.phases) || {};
  return Object.keys(phases).filter(k => phases[k] && typeof phases[k] === "object" &&
                                         !Array.isArray(phases[k]))
    .map(k => [k, phases[k]]);
}

/** Every step's id: its own `id:`, or auto_step_N numbered the way the engine
 *  numbers them. A Map keyed by the step object; the workflow is untouched. */
function assignStepIds(workflow) {
  const blocks = [(workflow && workflow.steps) || []]
    .concat(phaseBlocks(workflow).map(([, b]) => b.steps || []));
  const used = new Set();
  let seen = new Set();
  const collect = steps => {
    for (const step of steps || []) {
      if (!step || typeof step !== "object" || seen.has(step)) continue;
      seen.add(step);
      const sid = step.id;
      if (sid !== undefined && sid !== null && String(sid).trim()) used.add(String(sid).trim());
      collect(step.steps);
    }
  };
  for (const steps of blocks) collect(steps);
  const ids = new Map();
  let seq = 1;
  seen = new Set();
  const assign = steps => {
    for (const step of steps || []) {
      if (!step || typeof step !== "object" || seen.has(step)) continue;
      seen.add(step);
      let sid = step.id;
      if (sid === undefined || sid === null || !String(sid).trim()) {
        while (used.has("auto_step_" + seq)) seq++;
        sid = "auto_step_" + seq;
        used.add(sid);
        seq++;
      }
      ids.set(step, String(sid).trim());
      assign(step.steps);
    }
  };
  for (const steps of blocks) assign(steps);
  return ids;
}

/** A validation branch's `vars:` as [[name, raw]] - a mapping, as
 *  ValidationBranchDefinition reads it; a {name, value} list is accepted too. */
function branchVarItems(branch) {
  const raw = (branch && typeof branch === "object") ? branch.vars : null;
  if (Array.isArray(raw)) {
    return raw.filter(e => e && typeof e === "object" && e.name)
      .map(e => [String(e.name), e.value === undefined ? "" : e.value]);
  }
  if (raw && typeof raw === "object") return Object.keys(raw).map(k => [String(k), raw[k]]);
  return [];
}

/** Every variable a step can set at run time, and the first step that sets
 *  it - expander.runtime_producers(). {name: [step id, how]} */
function runtimeProducers(workflow, ids) {
  ids = ids || assignStepIds(workflow);
  const out = {};
  const note = (name, step, how) => {
    name = String(name === undefined || name === null ? "" : name).trim();
    if (name && !(name in out)) out[name] = [ids.get(step) || step.id, how];
  };
  const walk = steps => {
    for (const step of steps || []) {
      if (!step || typeof step !== "object") continue;
      for (const entry of step.register || []) {
        if (!entry || typeof entry !== "object") continue;
        for (const name of groupNamesOf(String(entry.regex === undefined || entry.regex === null ? "" : entry.regex)))
          note(name, step, "register");
        note(entry.count_var, step, "register count");
        note(entry.name, step, "register");
      }
      const rest = step.rest;
      if (rest && typeof rest === "object" && !Array.isArray(rest)) {
        for (const rule of rest.response_template || []) if (rule && typeof rule === "object") note(rule.name, step, "REST response_template");
        for (const rule of rest.response_headers || []) if (rule && typeof rule === "object") note(rule.name, step, "REST response header");
        note("http_status", step, "REST status");
      }
      const validation = step.validation;
      if (validation && typeof validation === "object") {
        for (const branch of ["success", "failure", "warning"]) {
          for (const [name] of branchVarItems(validation[branch])) note(name, step, "validation " + branch + " vars");
        }
      }
      walk(step.steps);
    }
  };
  walk(workflow && workflow.steps);
  for (const [, block] of phaseBlocks(workflow)) walk(block.steps);
  return out;
}

class Expander {
  constructor(workflow, data, params, template) {
    this.workflow = workflow || {};
    this.data = data || {};
    this.params = Object.assign({}, params || {});
    this.template = template || null;
    this.defaults = ((this.workflow.globals || {}).defaults) || {};
    this.nodes = this.workflow.nodes || {};
    this.stepIds = assignStepIds(this.workflow);
    this.producers = runtimeProducers(this.workflow, this.stepIds);
    this.mutable = new Set(Object.keys(this.producers).concat(Object.keys(this.params)));
    this.byId = {};
    const walk = steps => {
      for (const step of steps || []) {
        if (!step || typeof step !== "object") continue;
        const sid = this.stepIds.get(step);
        (this.byId[sid] = this.byId[sid] || []).push(step);
        walk(step.steps);
      }
    };
    walk(this.workflow.steps);
    for (const [, block] of phaseBlocks(this.workflow)) walk(block.steps);
  }

  /** globals.vars as an ordered [name, raw] list, for the page's param panel. */
  globalsVars() {
    const block = (this.workflow.globals || {}).vars || {};
    return Object.keys(block).map(name => [name, block[name]]);
  }

  baseContext(node) {
    const secrets = (this.workflow.globals || {}).secrets || {};
    const vars = {};
    Object.assign(vars, this.data);
    Object.assign(vars, this.params);

    // Secrets are addressed as ${SECRET.name}; resolveName looks a literal key
    // up first, so storing them under that name is all it takes. There is no
    // ${ENV.x} in a browser, and leaving it unresolved is the honest answer.
    for (const name of Object.keys(secrets)) {
      const spec = secrets[name];
      vars["SECRET." + name] = (spec && typeof spec === "object") ? spec.value : spec;
    }

    // The node's OWN fields are in scope, not just the root data section: MRF
    // and SBC workflows loop over ${configData} and gate on ${node == nodeName},
    // and both live on the node.
    for (const key of Object.keys(node)) vars[key] = node[key];

    const name = node.node || node.nodeGroup || "";
    vars.node_row = node;
    vars.NODE_NAME = name;
    vars.nodeName = name;
    vars.metadata = { nodeName: name };
    vars.nodeGroup = node.nodeGroup || "";
    vars.crGroup = node.crGroup || "";

    // CliAutomationEngine.executeForNode(): the aliases a real run adds
    promoteNiam(vars, node);
    if (node.node !== undefined && node.node !== null) vars.currentNode = node.node;
    if (node.crGroup !== undefined && node.crGroup !== null && !("currentCrGroup" in vars))
      vars.currentCrGroup = node.crGroup;
    vars.currentNodeData = node;
    const collection = Object.keys(node).map(k => node[k]).find(v => Array.isArray(v));
    if (collection !== undefined) vars.currentNodeCollection = collection;
    for (const [target, source] of [["VERSION", "version"], ["version", "VERSION"]]) {
      if (!(target in vars) && vars[source] !== undefined && vars[source] !== null) vars[target] = vars[source];
    }

    // MopExecutionUtil: a rollback-only run sets both flags
    if (String(vars.ROLLBACK_ONLY === undefined ? "" : vars.ROLLBACK_ONLY).toLowerCase() === "true")
      vars.ROLLBACK_REQUIRED = "true";

    // MopGenerator scopes the outer loop to this node's own nodeGroup
    const group = this.nodeGroupOf(node);
    vars.nodeGroups = group ? [group] : [];
    vars.nodes = [node];

    // globals.vars in declaration order, each interpolated against what is
    // known so far; a request parameter always wins.
    const block = (this.workflow.globals || {}).vars || {};
    const rollbackOnly = String(this.params.ROLLBACK_ONLY === undefined ? "" : this.params.ROLLBACK_ONLY)
      .toLowerCase() === "true";
    for (const key of Object.keys(block)) {
      if (key in this.params) continue;
      if (key === "ROLLBACK_REQUIRED" && rollbackOnly) continue;
      const value = block[key];
      vars[key] = (typeof value === "string") ? interpolate(value, vars) : value;
    }
    return vars;
  }

  nodeGroupOf(node) {
    for (const group of this.data.nodeGroups || []) {
      if (group && typeof group === "object" && group.nodeGroup === node.nodeGroup) {
        const scoped = Object.assign({}, group);
        scoped.nodes = [node];
        return scoped;
      }
    }
    return null;
  }

  expand(node) {
    const vars = this.baseContext(node);
    const state = { seq: 0, steps: [], warnings: [], loops: {}, loopSeq: 0 };
    const phases = [];

    for (const [phaseId, phase] of phaseBlocks(this.workflow)) {
      const gate = this.gate(vars, phase.when);
      const javaId = String(phase.name === undefined || phase.name === null ? "" : phase.name).trim() || phaseId;
      let then = phase.then;
      then = Array.isArray(then) ? then.map(String) : (then ? [String(then)] : []);
      const descriptor = {
        key: phaseId,
        id: javaId,
        name: phase.name || phaseId,
        description: phase.description || "",
        when: phase.when === undefined ? null : phase.when,
        when_state: gate.state,
        on_failure: (phase.on_failure !== undefined && phase.on_failure !== null)
          ? String(phase.on_failure).trim() : null,
        then: then,
        post_failure: javaId.toUpperCase().indexOf("ROLLBACK") >= 0 || then.length > 0,
        render_mode: renderModeFor(phaseId, phase.name),
        steps: 0,
      };
      if (!phase.steps || !phase.steps.length) continue;     // normalizePhases()
      phases.push(descriptor);
      const phaseCtx = Object.assign({}, descriptor, { id: phaseId, mode: descriptor.render_mode });

      // A phase the happy path gates off is still documented - its gate may
      // open at run time - but its steps must not leak into later phases.
      const snapshot = gate.state === false ? Object.assign({}, vars) : null;
      const phaseVars = phase.vars || {};
      for (const key of Object.keys(phaseVars)) {
        const value = phaseVars[key];
        vars[key] = (typeof value === "string") ? interpolate(value, vars) : value;
      }
      const before = state.steps.length;
      this.walk(phase.steps || [], vars, phaseCtx, state, []);
      descriptor.steps = state.steps.length - before;
      if (snapshot) {
        for (const k of Object.keys(vars)) delete vars[k];
        Object.assign(vars, snapshot);
      }
    }

    return {
      node: this.nodeMeta(node),
      steps: state.steps,
      warnings: state.warnings,
      vars: vars,
      phases: phases,
      loops: state.loops,
    };
  }

  nodeMeta(node) {
    const niamIds = {};
    for (const key of Object.keys(node)) {
      const value = node[key];
      if (value && typeof value === "object" && !Array.isArray(value) &&
          Object.keys(value).some(k => k.indexOf("niamID") === 0)) {
        niamIds[key] = value;
      }
    }
    return { node: node.node || node.nodeGroup, nodeGroup: node.nodeGroup,
             crGroup: node.crGroup, email: node.email,
             activity: this.data.activity, nodeType: this.data.nodeType,
             niamIds: niamIds };
  }

  walk(steps, vars, phaseCtx, state, loopPath) {
    for (const raw of steps) {
      if (!raw || typeof raw !== "object") continue;
      if (LOOP_TYPES.indexOf(raw.type) >= 0) this.walkLoop(raw, vars, phaseCtx, state, loopPath);
      else this.emit(raw, vars, phaseCtx, state, loopPath);
    }
  }

  /** FALSE, and reading nothing a run can change - only then may the walk act
   *  on a loop gate the way the engine will. */
  decidedFalse(vars, expression) {
    if (this.gate(vars, expression).state !== false) return false;
    return !namesIn(expression, false).some(n => this.mutable.has(n));
  }

  decidedTrue(vars, expression) {
    if (this.gate(vars, expression).state !== true) return false;
    return !namesIn(expression, false).some(n => this.mutable.has(n));
  }

  walkLoop(loop, vars, phaseCtx, state, loopPath) {
    // a loop's own when/skip_when is evaluated ONCE, before any item is bound
    if (loop.when !== undefined && loop.when !== null && this.decidedFalse(vars, loop.when)) return;
    if (loop.skip_when !== undefined && loop.skip_when !== null && String(loop.skip_when).trim() &&
        this.decidedTrue(vars, loop.skip_when)) return;

    const itemVar = loop.item_var || "item";
    let items = resolveForEach(loop.for_each, vars);
    const limit = loop.max_iterations;
    const bounded = typeof limit === "number" && Number.isInteger(limit) && limit >= 0;
    const overflow = bounded && items.length > limit;
    if (bounded) items = items.slice(0, limit);
    if (loop.reverse_order === true) items = items.slice().reverse();

    state.loopSeq += 1;
    const uid = "L" + String(state.loopSeq).padStart(4, "0");
    const sid = this.stepIds.get(loop);
    state.loops[uid] = {
      uid: uid,
      step_id: sid,
      phase: phaseCtx.id,
      var: itemVar,
      items: jsonable(items),
      count: items.length,
      when: loop.when === undefined ? null : loop.when,
      skip_when: loop.skip_when === undefined ? null : loop.skip_when,
      continue_when: loop.continue_when === undefined ? null : loop.continue_when,
      break_when: loop.break_when === undefined ? null : loop.break_when,
      max_iterations: bounded ? limit : null,
      overflow: !!overflow,
      on_failure: this.loopOnFailure(loop),
      depth: loopPath.length,
      parent: loopPath.length ? loopPath[loopPath.length - 1].loop : null,
    };
    if (overflow) {
      state.warnings.push("loop " + sid + " (" + itemVar + ") has more items than max_iterations " +
                          limit + " - the engine FAILS the loop after the first " + limit);
    }

    for (let index = 0; index < items.length; index++) {
      vars[itemVar] = items[index];
      if (loop.continue_when !== undefined && loop.continue_when !== null &&
          this.decidedFalse(vars, loop.continue_when)) continue;
      if (loop.break_when !== undefined && loop.break_when !== null &&
          this.decidedTrue(vars, loop.break_when)) break;
      this.walk(loop.steps || [], vars, phaseCtx, state,
                loopPath.concat([{ var: itemVar, index: index, label: loopLabel(items[index]), loop: uid }]));
    }
  }

  loopOnFailure(loop) {
    let behaviour = loop.on_failure;
    if (behaviour === undefined || behaviour === null) behaviour = this.defaults.on_failure;
    if (typeof behaviour === "string") {
      const low = behaviour.trim().toLowerCase();
      return (low === "continue" || low === "warning") ? low : "stop";
    }
    return "stop";
  }

  emit(raw, vars, phaseCtx, state, loopPath) {
    const when = this.gate(vars, raw.when);
    // An ABSENT skip_when must not skip the step: an empty expression defaults
    // TRUE, which is right for `when` and exactly wrong here.
    const skipRaw = raw.skip_when;
    const skip = (skipRaw === undefined || skipRaw === null || !String(skipRaw).trim())
      ? { state: false, trace: [] } : this.gate(vars, skipRaw);

    const missing = [];
    const nodeRef = raw.node;
    const nodeTarget = (typeof nodeRef === "string") ? interpolate(nodeRef, vars, missing) : nodeRef;
    const cmd = this.command(raw, vars, nodeTarget);
    const sendRaw = cmd.send;
    const send = (typeof sendRaw === "string") ? interpolate(sendRaw, vars, missing) : sendRaw;
    if (cmd.rest && typeof cmd.rest.body_raw === "string") interpolate(cmd.rest.body_raw, vars, missing);
    const description = interpolate(raw.command_description || "", vars, missing);
    const unresolved = this.explain(missing, vars, state);

    const validation = this.validation(raw, vars);
    const implied = validation.impliedOnSuccess || {};
    const register = JSON.parse(JSON.stringify(raw.register || []));
    const liveRegister = interpolatedRegister(register, vars);
    let okOutput, okStatus = null, badOutput, badStatus = null, badExit;
    if (cmd.rest) {
      const r = restOutputs(cmd.rest.response_template, validation.successCriteria, implied,
                            liveRegister, this.preferredStatuses(raw));
      okOutput = r[0]; okStatus = r[1]; badOutput = r[2]; badStatus = r[3];
      badExit = badStatus !== null ? 0 : -1;
    } else {
      okOutput = successOutput(liveRegister, validation.successCriteria, implied, implied);
      badOutput = failureOutput(liveRegister, validation.successCriteria, implied);
      badExit = badOutput !== SYNTH_MARKER ? 0 : 1;
    }

    const consumes = Array.from(new Set(
      tokensIn(sendRaw || "")
        .concat(tokensIn((cmd.rest && cmd.rest.body_raw) || ""))
        .concat(tokensIn(String(raw.when || "")))
        .concat(tokensIn(String(raw.skip_when || ""))))).sort();

    const prompt = raw.prompt_regex;
    state.seq += 1;
    const step = {
      uid: "s" + String(state.seq).padStart(4, "0"),
      step_id: this.stepIds.get(raw),
      phase: phaseCtx.id,
      phase_name: phaseCtx.name,
      phase_description: phaseCtx.description,
      seq: state.seq,
      kind: cmd.kind,
      node_ref: nodeRef === undefined ? null : nodeRef,
      node_target: nodeTarget === undefined ? null : nodeTarget,
      description: description,
      descriptionRaw: raw.command_description || "",
      send_raw: sendRaw === undefined ? null : sendRaw,
      send: send === undefined ? null : send,
      unresolved: unresolved,
      when: raw.when === undefined ? null : raw.when,
      when_state: when.state,
      skip_when: raw.skip_when === undefined ? null : raw.skip_when,
      skip_state: skip.state,
      on_failure: this.onFailure(raw),
      retries: raw.retries !== undefined ? raw.retries : this.defaults.retries,
      retry_delay_sec: raw.retry_delay_sec !== undefined ? raw.retry_delay_sec
                                                         : this.defaults.retry_delay_sec,
      timeout_sec: raw.timeout_sec !== undefined ? raw.timeout_sec : this.defaults.timeout_sec,
      register: register,
      validation: validation,
      implied: implied,
      consumes: consumes,
      produces: [],
      loop_path: loopPath,
      email: this.email(raw, vars),
      logs: raw.logs === undefined ? null : raw.logs,
      hide_when_skipped: !!raw.hide_when_skipped,
      use_exit_code: !!raw.use_exit_code,
      ignore_exit: typeof prompt === "string" && !!prompt.trim(),
      rest: cmd.rest,
      sftp: cmd.sftp,
      success_output: okOutput,
      success_status: okStatus,
      failure_output: badOutput,
      failure_status: badStatus,
      failure_exit: badExit,
      render_mode: phaseCtx.mode,
    };

    // Assumed success: the REST response_template and status, the registers
    // over the success output, whatever the criteria still assert, and the
    // success branch's vars. Applied only when the step is not definitely
    // skipped, so a skipped step cannot poison later interpolation.
    const produced = [];
    if (when.state !== false && skip.state !== true) {
      const log = [];
      if (cmd.rest) {
        applyResponseTemplate(cmd.rest.response_template, okOutput, vars, log);
        vars.http_status = okStatus;
        log.push({ name: "http_status", value: String(okStatus), source: "REST status" });
      }
      applyRegisters(step.register, okOutput, vars, log);
      for (const entry of log) {
        if (entry.value !== null && entry.value !== undefined) {
          produced.push({ name: entry.name, value: entry.value, source: entry.source });
        }
      }
      for (const name of Object.keys(implied)) {
        const current = resolveName(name, vars);
        if (current === undefined || current === null || current === "") {
          vars[name] = implied[name];
          produced.push({ name: name, value: implied[name],
                          source: "implied by success criteria" });
        }
      }
      for (const name of Object.keys(validation.varsOnSuccess || {})) {
        vars[name] = validation.varsOnSuccess[name];
        produced.push({ name: name, value: stringify(vars[name]), source: "validation success vars" });
      }
    }
    step.produces = produced;
    state.steps.push(step);
  }

  /** {kind, send, rest, sftp} - describeStepCommand(). */
  command(raw, vars, nodeTarget) {
    const nodeDef = (typeof nodeTarget === "string" && this.nodes[nodeTarget]) || null;
    const nodeType = String((nodeDef && nodeDef.type) || "").toLowerCase();
    const isMap = v => v && typeof v === "object" && !Array.isArray(v);
    let kind;
    if (raw.email) kind = "email";
    else if (isMap(raw.rest) || nodeType === "rest") kind = "rest";
    else if (isMap(raw.sftp)) kind = "sftp";
    else if (nodeTarget === "local" || nodeType === "local") kind = "local";
    else kind = "remote";

    if (typeof raw.send === "string") return { kind: kind, send: raw.send, rest: null, sftp: null };

    if (isMap(raw.sftp)) {
      const s = raw.sftp;
      const text = "sftp " + (s.operation || "sftp") + " local=" + (s.local_path || "") +
                   " remote=" + (s.remote_path || "");
      return { kind: kind, send: text, rest: null, sftp: JSON.parse(JSON.stringify(s)) };
    }

    if (isMap(raw.rest)) {
      const r = raw.rest;
      const method = String(r.method || "GET").toUpperCase().trim();
      const pathRaw = String(r.path === undefined || r.path === null ? "" : r.path).trim();
      const baseRaw = String(((nodeDef && nodeDef.rest) || {}).base_url === undefined ||
                             ((nodeDef && nodeDef.rest) || {}).base_url === null
                             ? "" : nodeDef.rest.base_url).trim();
      const path = interpolate(pathRaw, vars);
      const base = interpolate(baseRaw, vars);
      const joiner = (path && base && !path.startsWith("/")) ? "/" : "";
      const text = (pathRaw || baseRaw) ? (method + " " + baseRaw + joiner + pathRaw).trim() : null;
      let body = null, bodyType = null;
      for (const key of ["body_json", "body_map", "body_file", "multipart"]) {
        if (r[key] !== undefined && r[key] !== null) {
          bodyType = key;
          body = typeof r[key] === "string" ? r[key] : pyDumps(r[key]);
          break;
        }
      }
      const block = {
        method: method,
        path_raw: pathRaw,
        base_url_raw: baseRaw,
        body_raw: body,
        body_type: bodyType,
        content_type: r.body_content_type === undefined ? null : r.body_content_type,
        query: r.query === undefined ? null : r.query,
        response_template: JSON.parse(JSON.stringify(r.response_template || [])),
        response_headers: JSON.parse(JSON.stringify(r.response_headers || [])),
        download_to: r.download_to === undefined ? null : r.download_to,
        has_base_url: !!base,
      };
      return { kind: kind, send: text, rest: block, sftp: null };
    }
    return { kind: kind, send: raw.send === undefined ? null : raw.send, rest: null, sftp: null };
  }

  /** HTTP statuses this step's on_failure handlers are gated on. */
  preferredStatuses(raw) {
    const behaviour = raw.on_failure;
    if (!behaviour || typeof behaviour !== "object" || Array.isArray(behaviour)) return [];
    let targets = behaviour.run;
    targets = Array.isArray(targets) ? targets : (targets ? [targets] : []);
    const found = [];
    for (const target of targets) {
      let name = String(target);
      if (name.toLowerCase().startsWith("step:")) name = name.slice(5).trim();
      for (const step of this.byId[name] || []) {
        for (const literal of statusLiterals(step.when)) if (found.indexOf(literal) < 0) found.push(literal);
      }
    }
    return found;
  }

  validation(raw, vars) {
    const block = (raw.validation && typeof raw.validation === "object") ? raw.validation : {};
    const pick = v => (v && typeof v === "object" && !Array.isArray(v)) ? v : {};
    const success = pick(block.success);
    const failure = pick(block.failure);
    const warning = pick(block.warning);
    return {
      enabled: Object.keys(block).length > 0 && block.enabled !== false,
      description: interpolate(block.description || "", vars),
      successCriteria: success.criteria || {},
      successMessage: success.message || "",
      failureMessage: failure.message || warning.message || "",
      warningMessage: warning.message || "",
      failureCriteria: failure.criteria === undefined ? null : failure.criteria,
      hasSuccessBranch: "success" in block,
      hasFailureBranch: "failure" in block,
      hasWarningBranch: !!Object.keys(warning).length,
      impliedOnSuccess: impliedValues(success.criteria || {}, vars, true, ownedNames(raw)),
      varsOnSuccess: branchVars(success, vars),
      varsOnFailure: branchVars(Object.keys(failure).length ? failure : warning, vars),
      branchVars: { success: rawVars(success), failure: rawVars(failure), warning: rawVars(warning) },
    };
  }

  /** handleFailure(), normalised - see expander._on_failure(). */
  onFailure(raw) {
    const behaviour = raw.on_failure !== undefined ? raw.on_failure : this.defaults.on_failure;
    if (behaviour === undefined || behaviour === null) return { mode: "stop", raw: null };
    if (typeof behaviour === "object" && !Array.isArray(behaviour)) {
      const list = v => Array.isArray(v) ? v.map(String) : (v ? [String(v)] : []);
      const run = list(behaviour.run);
      const then = list(behaviour.then);
      const next = String(behaviour.next === undefined || behaviour.next === null ? "" : behaviour.next)
        .trim().toLowerCase() === "continue" ? "continue" : null;
      const email = ("email" in behaviour) && behaviour.email !== false;
      const out = { mode: run.length ? "run" : (next ? "continue" : "stop"),
                    run: run, then: then, next: next, email: email };
      if (run.length) out.step = run;
      return out;
    }
    const text = String(behaviour).trim();
    const low = text.toLowerCase();
    return { mode: ["continue", "warning", "email", "stop"].indexOf(low) >= 0 ? low : "stop", raw: text };
  }

  email(raw, vars) {
    const block = raw.email;
    if (!block || typeof block !== "object" || Array.isArray(block)) return null;
    const out = {};
    for (const key of Object.keys(block)) {
      const value = block[key];
      out[key] = Array.isArray(value) ? value.map(v => interpolate(v, vars))
               : (typeof value === "string" ? interpolate(value, vars) : value);
    }
    return out;
  }

  /**
   * Evaluate a gate to true / false / null(unknown).
   *
   * Unknown means an operand resolved to nothing, i.e. it comes from command
   * output that does not exist yet. The engine itself has no such notion - it
   * compares against the empty string - so a gate reported unknown here is one
   * whose real outcome depends on the run.
   */
  gate(vars, expression) {
    if (expression === undefined || expression === null || !String(expression).trim()) {
      return { state: true, trace: [] };
    }
    const tokens = namesIn(expression, true);
    const missing = tokens.filter(t => {
      const v = resolveName(t, vars);
      return v === undefined || v === null;
    });
    const trace = [];
    const result = evalCond(expression, vars, trace);
    return { state: missing.length ? null : !!result, trace: trace };
  }

  /** Ask the mapping why a CIQ reference resolved to nothing - or say that a
   *  step sets it at run time, which needs no mapping at all. */
  explain(missing, vars, state) {
    const seen = new Set();
    const out = [];
    for (const token of missing) {
      if (seen.has(token)) continue;
      seen.add(token);
      let reason = whyUnresolved(token, vars);
      const root = token.split(".")[0].split("[")[0].trim();
      const producer = this.producers[root];
      if (producer) {
        out.push({ token: token, reason: root + " is set at run time by step " + producer[0] +
                   " (" + producer[1] + ")", runtime: true });
        continue;
      }
      if (this.template && this.template.doc) {
        const field = ciqField(token);
        if (field) {
          const note = this.template.explain(field);
          if (note) {
            reason = reason + " - " + note;
            if (this.template.emits(field) === false) {
              const warning = "the mapping never fills ${" + token + "}";
              if (state.warnings.indexOf(warning) < 0) state.warnings.push(warning);
            }
          }
        }
      }
      out.push({ token: token, reason: reason });
    }
    return out;
  }
}

/** applyValidation(): every value interpolated against the context as it
 *  stood BEFORE any of them is set. */
function branchVars(branch, vars) {
  const out = {};
  for (const [name, value] of branchVarItems(branch)) {
    out[name] = typeof value === "string" ? interpolate(value, vars) : interpolateObject(value, vars);
  }
  return out;
}

/** The variables a step's own registers and REST response set. */
function ownedNames(raw) {
  const out = new Set();
  for (const entry of raw.register || []) {
    if (!entry || typeof entry !== "object") continue;
    for (const n of groupNamesOf(String(entry.regex === undefined || entry.regex === null ? "" : entry.regex))) out.add(n);
    for (const key of ["count_var", "name"]) if (entry[key]) out.add(String(entry[key]));
  }
  const rest = raw.rest;
  if (rest && typeof rest === "object") {
    for (const rule of rest.response_template || []) if (rule && typeof rule === "object" && rule.name) out.add(String(rule.name));
  }
  return out;
}

function rawVars(branch) {
  const out = {};
  for (const [name, value] of branchVarItems(branch)) out[name] = value;
  return out;
}

/** The variables a condition reads; `full` keeps dotted paths whole. */
function namesIn(expression, full) {
  const text = String(expression === undefined || expression === null ? "" : expression).trim();
  const tokens = (text.startsWith("${") && text.endsWith("}")) ? bareNames(text.slice(2, -1))
                                                               : tokensIn(text);
  return full ? tokens : tokens.map(t => t.split(".")[0].split("[")[0]);
}

/** 3-digit HTTP statuses a gate compares a status variable against. */
function statusLiterals(expression) {
  const out = [];
  const re = /([A-Za-z_]*status[A-Za-z_]*)\s*==\s*['"]?(\d{3})['"]?/gi;
  let m;
  while ((m = re.exec(String(expression === undefined || expression === null ? "" : expression)))) {
    const n = parseInt(m[2], 10);
    if (out.indexOf(n) < 0) out.push(n);
  }
  return out;
}

function interpolatedRegister(register, vars) {
  return (register || []).map(entry => {
    if (entry && typeof entry === "object" && typeof entry.regex === "string") {
      return Object.assign({}, entry, { regex: interpolate(entry.regex, vars) });
    }
    return entry;
  });
}

/** CliAutomationEngine.promoteNiamId() + the niamID/niamId/NIAM_ID aliases. */
function promoteNiam(vars, node) {
  let niam;
  for (const k of ["niamID", "niamId", "NIAM_ID"]) {
    if (node[k] !== undefined && node[k] !== null) { niam = node[k]; break; }
  }
  if (niam === undefined) niam = findNested(node, ["niamID", "niamId", "NIAM_ID"]);
  if (niam !== undefined && niam !== null) {
    for (const k of ["niamID", "niamId", "NIAM_ID"]) vars[k] = niam;
  }
}

function findNested(value, keys) {
  if (Array.isArray(value)) {
    for (const child of value) {
      const found = findNested(child, keys);
      if (found !== undefined && found !== null) return found;
    }
  } else if (value && typeof value === "object") {
    for (const k of keys) if (value[k] !== undefined && value[k] !== null) return value[k];
    for (const k of Object.keys(value)) {
      const found = findNested(value[k], keys);
      if (found !== undefined && found !== null) return found;
    }
  }
  return undefined;
}

/** Loop items travel to the page as JSON; dates and the like do not. */
function jsonable(value) {
  if (Array.isArray(value)) return value.map(jsonable);
  if (value instanceof Date) return value.toISOString();
  if (value && typeof value === "object") {
    const out = {};
    for (const k of Object.keys(value)) out[String(k)] = jsonable(value[k]);
    return out;
  }
  return value === undefined ? null : value;
}

function loopLabel(item) {
  if (item && typeof item === "object" && !Array.isArray(item)) {
    for (const key of ["nodeGroup", "configSeq", "table", "node"]) {
      if (item[key]) return String(item[key]);
    }
    const data = item.data;
    if (data && typeof data === "object") {
      for (const key of ["Test IMSI", "IMSI"]) if (data[key]) return String(data[key]);
    }
    return "item";
  }
  return item === null || item === undefined ? "" : String(item);
}

/** Identifiers in a condition written without ${} around each operand. */
function bareNames(expr) {
  const out = [];
  let buf = "", quote = null;
  for (const ch of String(expr || "")) {
    if (quote) { if (ch === quote) quote = null; continue; }
    if (ch === "'" || ch === '"') { quote = ch; continue; }
    if (/[A-Za-z0-9._[\]]/.test(ch)) { buf += ch; continue; }
    if (buf) { out.push(buf); buf = ""; }
  }
  if (buf) out.push(buf);
  return out.filter(t => t && !/^[0-9]/.test(t));
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { Expander, OutputTemplate, normalizeCiq, impliedValues,
                     renderModeFor, DEFAULT_PARAMS, activityParams, bareNames, ciqField,
                     assignStepIds, runtimeProducers, branchVarItems,
                     MODE_CHECKLIST, MODE_INTERACTIVE };
}

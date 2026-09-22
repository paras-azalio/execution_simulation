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
function impliedValues(criteria, vars, firstClauseOnly) {
  if (firstClauseOnly === undefined) firstClauseOnly = true;
  const out = {};
  if (!criteria || typeof criteria !== "object") return out;
  for (const key of ["all", "any"]) {
    const block = criteria[key];
    if (Array.isArray(block)) {
      for (const item of block) {
        Object.assign(out, impliedValues(item, vars, firstClauseOnly));
        if (key === "any" && firstClauseOnly && Object.keys(out).length) break;
      }
    }
  }
  const expr = criteria.expr;
  if (typeof expr === "string" && expr.trim()) {
    Object.assign(out, impliedFromExpr(expr, vars, firstClauseOnly));
  }
  return out;
}

function impliedFromExpr(expr, vars, firstClauseOnly) {
  let text = expr.trim();
  if (text.startsWith("${") && text.endsWith("}")) text = text.slice(2, -1).trim();
  const out = {};
  let clauses = splitTop(text, "||");
  if (firstClauseOnly && clauses.length) clauses = clauses.slice(0, 1);
  for (const clause of clauses) {
    for (const leaf of splitTop(clause, "&&")) {
      const [name, value] = impliedFromLeaf(leaf.trim(), vars);
      if (name && !(name in out)) out[name] = value;
    }
  }
  return out;
}

function impliedFromLeaf(leaf, vars) {
  for (const op of ["==", ">=", "<=", ">", "<"]) {
    const at = findOp(leaf, op);
    if (at <= 0) continue;
    const name = leaf.slice(0, at).trim();
    const raw = leaf.slice(at + op.length).trim();
    if (!/^[A-Za-z0-9_]+$/.test(name)) return [null, null];   // a dotted path is CIQ data
    const quoted = raw.length >= 2 && raw[0] === raw[raw.length - 1] &&
                   (raw[0] === "'" || raw[0] === '"');
    const literal = operand(raw, vars);
    if (!quoted && literal === raw && /^[A-Za-z_][A-Za-z0-9_]*$/.test(raw)) {
      // The right-hand side is another VARIABLE, as in
      // `${LINK_UP_COUNT == REMOTE_NODE_COUNT}`: no single value is implied.
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

/** The macro-server Arglist values a real run supplies (ciq.DEFAULT_PARAMS). */
const DEFAULT_PARAMS = {
  ORDER_NO: "12345", PARENT_REQ_ID: "12345", CHILD_REQ_ID: "10057",
  CR_GROUP: "CR1", CR_NAME: "CR1", NODE_TYPE: "PGW_RDS", REQ_TYPE: "1",
};

class Expander {
  constructor(workflow, data, params, template) {
    this.workflow = workflow || {};
    this.data = data || {};
    this.params = Object.assign({}, params || {});
    this.template = template || null;
    this.defaults = ((this.workflow.globals || {}).defaults) || {};
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

    // MopGenerator scopes the outer loop to this node's own nodeGroup
    const group = this.nodeGroupOf(node);
    vars.nodeGroups = group ? [group] : [];
    vars.nodes = [node];

    // globals.vars in declaration order, each interpolated against what is
    // known so far; a request parameter always wins.
    const block = (this.workflow.globals || {}).vars || {};
    for (const key of Object.keys(block)) {
      if (key in this.params) continue;
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
    const state = { seq: 0, steps: [], warnings: [] };
    const phases = this.workflow.phases || {};

    for (const phaseId of Object.keys(phases)) {
      const phase = phases[phaseId];
      if (!phase || typeof phase !== "object") continue;
      const gate = this.gate(vars, phase.when);
      if (gate.state === false) {
        state.warnings.push("phase " + phaseId + " skipped: when " + phase.when + " is false");
        continue;
      }
      const phaseCtx = {
        id: phaseId,
        name: phase.name || phaseId,
        description: phase.description || "",
        mode: renderModeFor(phaseId, phase.name),
      };
      const phaseVars = phase.vars || {};
      for (const key of Object.keys(phaseVars)) {
        const value = phaseVars[key];
        vars[key] = (typeof value === "string") ? interpolate(value, vars) : value;
      }
      this.walk(phase.steps || [], vars, phaseCtx, state, []);
    }

    return {
      node: this.nodeMeta(node),
      steps: state.steps,
      warnings: state.warnings,
      vars: vars,
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

  walkLoop(loop, vars, phaseCtx, state, loopPath) {
    const itemVar = loop.item_var || "item";
    let items = resolveForEach(loop.for_each, vars);
    const limit = loop.max_iterations;
    if (typeof limit === "number" && limit >= 0) items = items.slice(0, limit);
    if (loop.reverse_order === true) items = items.slice().reverse();

    for (let index = 0; index < items.length; index++) {
      vars[itemVar] = items[index];

      if (loop.continue_when !== undefined && loop.continue_when !== null) {
        // ExecutionOrchestrator:591 - the body runs only when TRUE. UNKNOWN
        // keeps the iteration: a MOP must not drop steps whose filter depends
        // on runtime data.
        if (this.gate(vars, loop.continue_when).state === false) continue;
      }
      if (loop.break_when !== undefined && loop.break_when !== null &&
          this.gate(vars, loop.break_when).state === true) break;

      this.walk(loop.steps || [], vars, phaseCtx, state,
                loopPath.concat([{ var: itemVar, index: index, label: loopLabel(items[index]) }]));
    }
  }

  emit(raw, vars, phaseCtx, state, loopPath) {
    const when = this.gate(vars, raw.when);
    // An ABSENT skip_when must not skip the step: an empty expression defaults
    // TRUE, which is right for `when` and exactly wrong here.
    const skipRaw = raw.skip_when;
    const skip = (skipRaw === undefined || skipRaw === null || !String(skipRaw).trim())
      ? { state: false, trace: [] } : this.gate(vars, skipRaw);

    const missing = [];
    const sendRaw = raw.send;
    const send = (typeof sendRaw === "string") ? interpolate(sendRaw, vars, missing) : sendRaw;
    const description = interpolate(raw.command_description || "", vars, missing);
    const nodeRef = raw.node;
    const nodeTarget = (typeof nodeRef === "string") ? interpolate(nodeRef, vars, missing) : nodeRef;
    const unresolved = this.explain(missing, vars, state);

    const validation = this.validation(raw, vars);
    const implied = validation.impliedOnSuccess || {};
    const register = JSON.parse(JSON.stringify(raw.register || []));
    const okOutput = successOutput(register, validation.successCriteria, implied, implied);
    const badOutput = failureOutput(register, validation.successCriteria, implied);

    const consumes = Array.from(new Set(
      tokensIn(sendRaw || "")
        .concat(tokensIn(String(raw.when || "")))
        .concat(tokensIn(String(raw.skip_when || ""))))).sort();

    state.seq += 1;
    const step = {
      uid: "s" + String(state.seq).padStart(4, "0"),
      phase: phaseCtx.id,
      phase_name: phaseCtx.name,
      phase_description: phaseCtx.description,
      seq: state.seq,
      kind: raw.email ? "email" : (nodeTarget === "local" ? "local" : "remote"),
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
      success_output: okOutput,
      failure_output: badOutput,
      render_mode: phaseCtx.mode,
    };

    // Assumed success: the registers, then whatever the criteria imply.
    // Applied only when the step is not definitely skipped, so a skipped step
    // cannot poison later interpolation.
    const produced = [];
    if (when.state !== false && skip.state !== true) {
      const log = [];
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
        produced.push({ name: name, value: vars[name], source: "validation success vars" });
      }
    }
    step.produces = produced;
    state.steps.push(step);
  }

  validation(raw, vars) {
    const block = raw.validation || {};
    const success = block.success || {};
    const failure = block.failure || {};
    const warning = block.warning || {};
    return {
      enabled: !!block.enabled,
      description: interpolate(block.description || "", vars),
      successCriteria: success.criteria || {},
      successMessage: success.message || "",
      failureMessage: failure.message || warning.message || "",
      hasWarningBranch: !!Object.keys(warning).length,
      impliedOnSuccess: impliedValues(success.criteria || {}, vars),
      varsOnSuccess: branchVars(success, vars),
      varsOnFailure: branchVars(Object.keys(failure).length ? failure : warning, vars),
    };
  }

  onFailure(raw) {
    const behaviour = raw.on_failure !== undefined ? raw.on_failure
                                                   : (this.defaults.on_failure || "stop");
    if (behaviour && typeof behaviour === "object") {
      if (behaviour.run) return { mode: "run", step: behaviour.run };
      const first = Object.keys(behaviour).map(k => behaviour[k])[0];
      return { mode: String(first === undefined ? "stop" : first) };
    }
    return { mode: String(behaviour) };
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
    const text = String(expression).trim();
    let tokens;
    if (text.startsWith("${") && text.endsWith("}")) {
      // The whole condition is one ${...} wrapper, so the placeholder pattern
      // returns the entire expression as a single "token". Read the
      // identifiers out of the expression instead.
      tokens = bareNames(text.slice(2, -1));
    } else {
      tokens = tokensIn(text);
    }
    const missing = tokens.filter(t => {
      const v = resolveName(t, vars);
      return v === undefined || v === null;
    });
    const trace = [];
    const result = evalCond(expression, vars, trace);
    return { state: missing.length ? null : !!result, trace: trace };
  }

  /** Ask the mapping why a CIQ reference resolved to nothing. */
  explain(missing, vars, state) {
    const seen = new Set();
    const out = [];
    for (const token of missing) {
      if (seen.has(token)) continue;
      seen.add(token);
      let reason = whyUnresolved(token, vars);
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

function branchVars(branch, vars) {
  const out = {};
  // `vars:` is a LIST of {name, value}. Where a workflow writes it as a
  // mapping instead, python iterates the keys and discards them all because
  // they are not dicts; iterating a mapping in JS throws, so the shapes have
  // to be told apart explicitly to keep the two sides identical.
  const entries = (branch || {}).vars;
  if (!Array.isArray(entries)) return out;
  for (const entry of entries) {
    if (entry && typeof entry === "object" && entry.name) {
      out[entry.name] = interpolate(entry.value === undefined ? "" : entry.value, vars);
    }
  }
  return out;
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
                     renderModeFor, DEFAULT_PARAMS, bareNames, ciqField,
                     MODE_CHECKLIST, MODE_INTERACTIVE };
}

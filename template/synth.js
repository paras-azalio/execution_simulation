/* synth.js - synth.py, in the browser.
 *
 * Turns a step's success criteria into the WORDS a command would have to print
 * for that step to pass, and words that would make it fail.
 *
 * A mirror of execution_simulation/synth.py. The page needs it because the
 * runner expands a workflow the operator has just dropped in, so nothing has
 * pre-computed the Success and Failure text for it.
 *
 * test_expander_js.js runs every workflow in the repo through both this and
 * synth.py and compares the output, so the two cannot drift apart silently.
 */

/* eslint-disable no-unused-vars */
const CLASS_SAMPLE = { d: "7", w: "x", s: " ", S: "x", W: "-", D: "x" };
const SYNTH_MARKER = "<output not derivable from the criteria - use Custom>";

/* ------------------------------------------------------------------ *
 *  regex -> one matching string
 * ------------------------------------------------------------------ */
/**
 * Emits one string that matches a pattern.
 *
 * Deliberately minimal: it walks the pattern once, left to right, and for
 * every construct emits the shortest thing that satisfies it. Alternation
 * always takes the first branch, because a criteria value - when there is one -
 * is substituted into the group afterwards, and the first branch is what the
 * workflow author wrote first (`true|false`).
 */
class Sampler {
  constructor(pattern, groupValues) {
    this.pattern = pattern || "";
    this.groupValues = groupValues || {};
    this.index = 0;
  }

  sample() { return this.sequence(false); }

  sequence(stopAtPipe) {
    const out = [];
    while (this.index < this.pattern.length) {
      const ch = this.pattern[this.index];
      if (ch === ")") break;
      if (ch === "|") {
        if (stopAtPipe) break;
        this.skipToClose();                 // first branch wins
        break;
      }
      const [piece0, name] = this.atom();
      if (piece0 === null) continue;
      let piece = this.quantify(piece0);
      if (name && Object.prototype.hasOwnProperty.call(this.groupValues, name)) {
        piece = this.groupValues[name];
      }
      out.push(piece);
    }
    return out.join("");
  }

  atom() {
    const ch = this.pattern[this.index];
    if (ch === "(") return this.group();
    if (ch === "[") return [this.charClass(), null];
    if (ch === "\\") {
      this.index += 1;
      if (this.index >= this.pattern.length) return ["\\", null];
      const esc = this.pattern[this.index];
      this.index += 1;
      if (Object.prototype.hasOwnProperty.call(CLASS_SAMPLE, esc)) return [CLASS_SAMPLE[esc], null];
      if (esc === "n") return ["\n", null];
      if (esc === "t") return ["\t", null];
      if (esc === "b") return ["", null];
      return [esc, null];
    }
    if (ch === "^" || ch === "$") { this.index += 1; return [null, null]; }
    if (ch === ".") { this.index += 1; return ["x", null]; }
    this.index += 1;
    return [ch, null];
  }

  group() {
    this.index += 1;                                    // consume '('
    let name = null;
    if (this.pattern.startsWith("?", this.index)) {
      const rest = this.pattern.slice(this.index);
      const named = /^\?P?<([A-Za-z][A-Za-z0-9]*)>/.exec(rest);
      if (named) {
        name = named[1];
        this.index += named[0].length;
      } else if (/^\?[:=!]/.test(rest)) {
        this.index += 2;
      } else if (/^\?[a-zA-Z]+\)/.test(rest)) {         // inline flags (?m)(?s)
        this.index += rest.indexOf(")") + 1;
        return [null, null];
      } else {
        this.index += 1;
      }
    }
    const body = this.sequence(true);
    this.skipToClose();
    if (this.index < this.pattern.length && this.pattern[this.index] === ")") this.index += 1;
    return [body, name];
  }

  skipToClose() {
    let depth = 0;
    while (this.index < this.pattern.length) {
      const ch = this.pattern[this.index];
      if (ch === "\\") { this.index += 2; continue; }
      if (ch === "[") { this.charClass(); continue; }
      if (ch === "(") depth += 1;
      else if (ch === ")") {
        if (depth === 0) return;
        depth -= 1;
      }
      this.index += 1;
    }
  }

  charClass() {
    this.index += 1;                                    // consume '['
    const negated = this.pattern.startsWith("^", this.index);
    if (negated) this.index += 1;
    const members = [];
    while (this.index < this.pattern.length && this.pattern[this.index] !== "]") {
      const ch = this.pattern[this.index];
      if (ch === "\\") {
        this.index += 1;
        const esc = this.index < this.pattern.length ? this.pattern[this.index] : "x";
        members.push(Object.prototype.hasOwnProperty.call(CLASS_SAMPLE, esc) ? CLASS_SAMPLE[esc] : esc);
        this.index += 1;
        continue;
      }
      if (this.index + 2 < this.pattern.length &&
          this.pattern[this.index + 1] === "-" &&
          this.pattern[this.index + 2] !== "]") {
        members.push(ch);
        this.index += 3;
        continue;
      }
      members.push(ch);
      this.index += 1;
    }
    if (this.index < this.pattern.length) this.index += 1;   // consume ']'
    if (negated) {
      for (const candidate of "xyz0189") if (members.indexOf(candidate) < 0) return candidate;
      return "x";
    }
    return members.length ? members[0] : "x";
  }

  quantify(piece) {
    if (this.index >= this.pattern.length) return piece;
    const ch = this.pattern[this.index];
    if (ch === "?" || ch === "*") {
      this.index += 1;
      this.consumeLazy();
      return ch === "?" ? piece : "";
    }
    if (ch === "+") {
      this.index += 1;
      this.consumeLazy();
      return piece;
    }
    if (ch === "{") {
      const m = /^\{(\d+)(,(\d+)?)?\}/.exec(this.pattern.slice(this.index));
      if (m) {
        this.index += m[0].length;
        this.consumeLazy();
        return piece.repeat(parseInt(m[1], 10));
      }
    }
    return piece;
  }

  consumeLazy() {
    if (this.index < this.pattern.length &&
        (this.pattern[this.index] === "?" || this.pattern[this.index] === "+")) {
      this.index += 1;
    }
  }
}

/** One string matching `pattern`, with named groups forced where given. */
function sampleFor(pattern, groupValues) {
  try {
    return new Sampler(toJavaRegexSource(pattern), groupValues).sample();
  } catch (e) {
    return "";                                          // never fail a document
  }
}

/** Java writes (?<X>...), python (?P<X>...); the sampler accepts either, so
 *  this only normalises the spelling the way engine.to_java_regex does. */
function toJavaRegexSource(pattern) {
  return String(pattern || "").replace(/\(\?<([A-Za-z][A-Za-z0-9]*)>/g, "(?P<$1>");
}

/** The named groups of a pattern, in declaration order. */
function groupNames(pattern) {
  const out = [];
  const re = /\(\?P?<([A-Za-z][A-Za-z0-9]*)>/g;
  let m;
  while ((m = re.exec(String(pattern || "")))) out.push(m[1]);
  return out;
}

/* ------------------------------------------------------------------ *
 *  criteria -> output
 * ------------------------------------------------------------------ */
/**
 * Would this output pass these criteria, run through these registers?
 *
 * The synthesiser checks its own work: emitting "success" words that the
 * step's criteria then reject would make the page report FAILURE on a step the
 * operator just called successful.
 */
function synthPasses(register, criteria, output) {
  const expr = (criteria || {}).expr;
  const vars = {};
  applyRegisters(register, output, vars);
  if (typeof expr === "string" && expr.trim() && !evalCond(expr, vars)) return false;
  const pattern = (criteria || {}).regex;
  if (typeof pattern === "string" && pattern.trim()) {
    if (!regexMatches(pattern, output || "")) return false;
  }
  return true;
}

/** Every `regex` a criteria block asserts, including inside all[]/any[]. */
function criteriaRegexes(criteria, out) {
  out = out || [];
  if (criteria && typeof criteria === "object") {
    if (typeof criteria.regex === "string") out.push(criteria.regex);
    for (const key of ["all", "any"]) {
      for (const item of criteria[key] || []) criteriaRegexes(item, out);
    }
  }
  return out;
}

/** The longest loop pattern - the most specific line to emit. */
function specificLoop(register) {
  const patterns = (register || [])
    .filter(e => e && e.regex && e.loop).map(e => e.regex);
  if (patterns.length <= 1) return null;
  return patterns.reduce((a, b) => (b.length > a.length ? b : a));
}

function dedupe(lines) {
  const seen = new Set(), out = [];
  for (const line of lines) if (!seen.has(line)) { seen.add(line); out.push(line); }
  return out;
}

function buildSuccess(register, criteria, implied, counts, loops) {
  counts = counts || {};
  implied = implied || {};
  const lines = [], repeated = [];
  const keepLoop = loops === "specific" ? specificLoop(register) : null;

  for (const entry of register || []) {
    if (!entry || !entry.regex) continue;
    const pattern = entry.regex;
    if (entry.loop && keepLoop !== null && pattern !== keepLoop) {
      // Two loop counters over the same lines are usually related by the
      // criteria (LINK_UP_COUNT == REMOTE_NODE_COUNT). A sample per pattern
      // gives the broader one more matches than the narrower one and the
      // relation fails, so only the most specific line is emitted.
      continue;
    }
    if (entry.loop) {
      let repeat = 1;
      const countVar = entry.count_var;
      if (countVar && /^\d+$/.test(String(counts[countVar] === undefined ? "" : counts[countVar]))) {
        repeat = Math.max(1, parseInt(counts[countVar], 10));
      }
      const line = sampleFor(pattern);
      if (line.trim()) for (let i = 0; i < repeat; i++) repeated.push(line);
      continue;
    }
    const names = groupNames(pattern);
    // A group the criteria assert to be EMPTY must not be captured at all, so
    // no line is emitted and the variable keeps the default its
    // `- name: X  value: ""` entry gave it. Emitting a sampled line here would
    // set BLOCKED=x and fail `${BLOCKED == ""}`.
    if (names.some(n => Object.prototype.hasOwnProperty.call(implied, n) && implied[n] === "")) continue;
    const groups = {};
    for (const name of names) {
      if (Object.prototype.hasOwnProperty.call(implied, name)) groups[name] = implied[name];
    }
    let line = sampleFor(pattern, groups);
    const nonempty = nonemptyNames(criteria);
    if (names.some(n => nonempty.has(n))) {
      // `(?<DSRUSER>.*)` samples as nothing, and `${DSRUSER != ""}` then
      // fails the very output meant to pass it
      const re = compileJava(pattern);
      const m = re ? re.exec(line) : null;
      for (const name of names) {
        if (nonempty.has(name) && !Object.prototype.hasOwnProperty.call(groups, name) && m &&
            !((m.groups || {})[name] || "")) {
          groups[name] = name.toLowerCase() + "_1";
        }
      }
      line = sampleFor(pattern, groups);
    }
    if (line.trim()) lines.push(line);
  }

  for (const pattern of criteriaRegexes(criteria)) {
    const line = sampleFor(pattern);
    if (line.trim()) lines.push(line);
  }

  // A step with no regex anywhere is judged on its exit code alone, so any
  // output satisfies it; inventing node output would only mislead.
  if (!lines.length && !repeated.length) return "";
  return dedupe(lines).concat(repeated).join("\n");
}

/** Variables the criteria assert to be non-empty: `${X != ""}`. */
function nonemptyNames(criteria) {
  const expr = String(((criteria || {}).expr) || "");
  const out = new Set();
  const re = /([A-Za-z_][A-Za-z0-9_]*)\s*!=\s*(?:""|'')/g;
  let m;
  while ((m = re.exec(expr))) out.add(m[1]);
  return out;
}

/** The words a passing command prints, verified against the criteria. */
function successOutput(register, criteria, implied, counts) {
  for (const mode of ["all", "specific"]) {
    const output = buildSuccess(register, criteria, implied, counts, mode);
    if (synthPasses(register, criteria, output)) return output;
  }
  return buildSuccess(register, criteria, implied, counts, "specific");
}

/**
 * A value the criteria reject.
 *
 * Prefixed, never suffixed: `^(?<WRITEOK>WRITE_OK)` would still match the
 * prefix of "WRITE_OK_NOT_MATCHING", so the "failure" output would pass.
 */
function notValue(value) {
  const text = value === null || value === undefined ? "" : String(value);
  if (text === "true") return "false";
  if (text === "false") return "true";
  if (text === "running") return "stopped";
  if (text === "success") return "failure";
  if (/^\d+$/.test(text)) return String(parseInt(text, 10) + 1);
  return text ? "NOT_MATCHING_" + text : "unexpected";
}

/** Words that make the criteria fail. */
function failureOutput(register, criteria, implied) {
  const flipped = {};
  for (const name of Object.keys(implied || {})) flipped[name] = notValue(implied[name]);
  const lines = [];
  for (const entry of register || []) {
    if (!entry || !entry.regex || entry.loop) continue;
    const names = groupNames(entry.regex);
    const groups = {};
    let any = false;
    for (const name of names) {
      if (Object.prototype.hasOwnProperty.call(flipped, name)) { groups[name] = flipped[name]; any = true; }
    }
    if (!any) continue;
    const line = sampleFor(entry.regex, groups);
    if (line.trim()) lines.push(line);
  }
  if (!lines.length) return SYNTH_MARKER;
  const output = dedupe(lines).join("\n");
  if (synthPasses(register, criteria, output)) {
    // The flipped values still satisfy the criteria, so emit nothing a
    // register can capture: every variable keeps its declared default, which
    // is what a failing step really looks like.
    return SYNTH_MARKER;
  }
  return output;
}

/* ------------------------------------------------------------------ *
 *  REST - a response body and a status (synth.rest_outputs)
 * ------------------------------------------------------------------ */
const FAIL_STATUSES = [500, 404, 401, 400, 503];

/** `$.data.items[0].name` -> ["data", "items", 0, "name"], or null. */
function pathTokens(path) {
  const text = String(path === undefined || path === null ? "" : path).trim();
  if (!text.startsWith("$") || text.indexOf("length()") >= 0) return null;
  const body = text.slice(1).replace(/^\.+/, "");
  if (!body) return null;
  const out = [];
  for (const token of splitPathTokens(body)) {
    if (!token) continue;
    const m = /^([^\[]*)((?:\[\d+\])*)$/.exec(token);
    if (!m) return null;
    if (m[1]) out.push(m[1]);
    const re = /\[(\d+)\]/g;
    let i;
    while ((i = re.exec(m[2]))) out.push(parseInt(i[1], 10));
  }
  return out.length ? out : null;
}

function setPath(root, tokens, value) {
  let cur = root;
  tokens.forEach((token, i) => {
    const last = i === tokens.length - 1;
    const container = typeof tokens[i + 1] === "number" ? [] : {};
    if (typeof token === "number") {
      while (cur.length <= token) cur.push(null);
      if (last) cur[token] = value;
      else {
        if (!cur[token] || typeof cur[token] !== "object") cur[token] = container;
        cur = cur[token];
      }
    } else {
      if (last) cur[token] = value;
      else {
        if (!cur[token] || typeof cur[token] !== "object") cur[token] = container;
        cur = cur[token];
      }
    }
  });
}

function buildBody(rules, values, force) {
  force = force || new Set();
  const wanted = [];
  for (const rule of rules || []) {
    if (!rule || typeof rule !== "object" || !rule.name) continue;
    const tokens = pathTokens(rule.json_path);
    if (!tokens) continue;
    const name = String(rule.name);
    if (Object.prototype.hasOwnProperty.call(values, name)) wanted.push([tokens, values[name]]);
    else if (rule.required || !("default" in rule) || force.has(name)) wanted.push([tokens, name + "_1"]);
  }
  if (!wanted.length) return null;
  const root = {};
  const same = (a, b) => a.length === b.length && a.every((t, i) => t === b[i]);
  for (const [tokens, value] of wanted) {
    if (wanted.some(([o]) => o.length > tokens.length && same(o.slice(0, tokens.length), tokens))) continue;
    setPath(root, tokens, value);
  }
  return root;
}

function restPasses(rules, criteria, register, body, status) {
  const vars = {};
  if (applyResponseTemplate(rules, body, vars)) return false;
  vars.http_status = status;
  applyRegisters(register, body, vars);
  return evalCriteria(criteria || {}, vars, body, { http_status: status });
}

/** [success body, success status, failure body, failure status]; a failure
 *  status of null means only a connection error can fail this step. */
function restOutputs(rules, criteria, implied, register, preferred) {
  implied = Object.assign({}, implied || {});
  const rawStatus = String(implied.http_status || ((criteria || {}).http_status) || "200");
  delete implied.http_status;
  const okStatus = /^\d+$/.test(rawStatus) ? parseInt(rawStatus, 10) : 200;
  // a field the criteria read without asserting a value has to be IN a
  // passing response, default or not
  const mentioned = new Set(String(((criteria || {}).expr) || "").match(/[A-Za-z_][A-Za-z0-9_]*/g) || []);
  const okBody = buildBody(rules, implied, mentioned);
  const okText = okBody !== null ? pyDumps(okBody) : "";
  const flipped = {};
  for (const name of Object.keys(implied)) flipped[name] = notValue(implied[name]);
  const badBody = buildBody(rules, flipped);
  const badText = badBody !== null ? pyDumps(badBody) : "";
  let badStatus = null;
  for (const status of (preferred || []).concat(FAIL_STATUSES)) {
    if (status === okStatus) continue;
    if (!restPasses(rules, criteria, register, badText, status)) { badStatus = status; break; }
  }
  return [okText, okStatus, badText, badStatus];
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { sampleFor, successOutput, failureOutput, groupNames,
                     criteriaRegexes, specificLoop, notValue, SYNTH_MARKER, restOutputs };
}

/* yamlload.js - read a CLICR workflow the way the engine reads it.
 *
 * The engine uses SnakeYAML and the generator uses PyYAML. Both are lenient in
 * two places where every JavaScript YAML parser is strict, and a page that
 * refused a file the engine runs happily would be worse than useless:
 *
 *   duplicated mapping key    SnakeYAML and PyYAML keep the LAST one - so a
 *                             real run silently ignores the earlier block.
 *                             js-yaml throws unless asked for JSON semantics,
 *                             which have exactly that last-one-wins rule.
 *
 *   deficient indentation     a quoted scalar continued on a line indented no
 *                             further than its own key:
 *
 *                                 send: 'printf "status: %s
 *                             " "${x}"; exit 1'
 *
 *                             SnakeYAML 1.33 accepts it (verified against the
 *                             jar the project ships), PyYAML accepts it, the
 *                             YAML spec does not. The continuation is indented
 *                             here before the parse and the file is reported as
 *                             needing a fix. Folding is unaffected - a line
 *                             break inside a quoted scalar becomes a space
 *                             either way - so the value is unchanged, which
 *                             test_expander_parity.py checks by comparing the
 *                             whole expansion against PyYAML's.
 *
 * Both repairs are reported, never silent: `loadWorkflow` returns the document
 * together with the notes, and the runner shows them.
 */

/** A `key: '` or `key: "` that opens a quoted scalar and does not close it. */
function opensQuotedScalar(line) {
  const m = /^(\s*)(?:-\s+)?[^:#]+:\s*(['"])/.exec(line);
  if (!m) return null;
  const quote = m[2];
  const body = line.slice(line.indexOf(quote) + 1);
  let escaped = false;
  for (const ch of body) {
    if (escaped) { escaped = false; continue; }
    if (quote === '"' && ch === "\\") { escaped = true; continue; }
    if (ch === quote) return null;                 // closed on the same line
  }
  return { indent: m[1].length, quote: quote };
}

function closesQuotedScalar(line, quote) {
  let escaped = false;
  for (const ch of line) {
    if (escaped) { escaped = false; continue; }
    if (quote === '"' && ch === "\\") { escaped = true; continue; }
    if (ch === quote) return true;
  }
  return false;
}

/**
 * Indent the continuation lines of quoted scalars that sit at or below their
 * own key's indentation. Returns {text, notes}.
 */
function repairIndentation(text) {
  const lines = String(text).split("\n");
  const notes = [];
  let open = null;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (open) {
      const leading = line.length - line.replace(/^\s*/, "").length;
      if (line.trim() && leading <= open.indent) {
        lines[i] = " ".repeat(open.indent + 2) + line.replace(/^\s*/, "");
        notes.push("line " + (i + 1) + ": a quoted value continued at column " +
                   (leading + 1) + ", below its own key - indented to parse");
      }
      if (closesQuotedScalar(lines[i], open.quote)) open = null;
      continue;
    }
    open = opensQuotedScalar(line);
  }
  return { text: lines.join("\n"), notes: notes };
}

/**
 * Load a workflow YAML leniently.
 *
 * `yaml` is the js-yaml module. Returns {doc, notes}; throws only when the
 * document cannot be read even after the repairs.
 */
function loadWorkflow(yaml, text) {
  const notes = [];
  try {
    // `json: true` is last-one-wins on a duplicated key, which is what
    // SnakeYAML and PyYAML do.
    return { doc: yaml.load(text, { json: true }), notes: notes };
  } catch (first) {
    const repaired = repairIndentation(text);
    if (!repaired.notes.length) throw first;
    const doc = yaml.load(repaired.text, { json: true });
    return { doc: doc, notes: notes.concat(repaired.notes) };
  }
}

/** Duplicated mapping keys, which load silently and lose the earlier value. */
function duplicateKeys(text) {
  const seen = new Map();
  const out = [];
  for (const [i, line] of String(text).split("\n").entries()) {
    const m = /^(\s*)([A-Za-z_][A-Za-z0-9_.-]*)\s*:(?:\s|$)/.exec(line);
    if (!m) continue;
    const key = m[1].length + ":" + m[2];
    if (seen.has(key) && m[1].length <= 2) {
      out.push("line " + (i + 1) + ": '" + m[2] + "' repeats line " +
               (seen.get(key) + 1) + " - only the last one is used");
    }
    seen.set(key, i);
  }
  return out;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { loadWorkflow, repairIndentation, duplicateKeys,
                     opensQuotedScalar };
}

/* report.js - a walked document as the report a real run leaves behind.
 *
 *     buildReport(doc, result, meta, lint)  -> one standalone HTML file
 *     buildLog(doc, result, meta)           -> <node>_execution.log text
 *
 * A production run writes <ACT>_CR1_SUMMARY_REPORT.html, a failure report with
 * the command and what it printed, <ACT>_EXECUTION_REPORT_<node>.json and
 * <node>_execution.log. This is that set for a simulated walk: the summary,
 * per-phase counts, the execution summary variables the engine sets at the
 * end, every failure with its command, output and where it routed, every step,
 * and the log - one file, no scripts, readable anywhere.
 */

function reportEsc(s) {
  return String(s === undefined || s === null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

const REPORT_LABEL = { success: "SUCCESS", warning: "WARNING", failure: "FAILURE",
                       skipped: "SKIPPED", notrun: "NOT EXECUTED", pending: "PENDING" };

function buildReport(doc, result, meta, lint) {
  meta = meta || {};
  const e = reportEsc;
  const s = result.summary;
  const PH = {};
  for (const p of doc.phases || []) PH[p.key] = p;
  const steps = doc.steps || [];
  const rt = uid => result.rt[uid] || {};
  const pill = st => '<span class="p p-' + e(st) + '">' + e(REPORT_LABEL[st] || st) + '</span>';

  const phaseRows = Object.keys(s.phases).map(k => {
    const p = s.phases[k];
    return '<tr><td>' + e(p.id) + '</td>' +
      ["success", "warning", "failure", "skipped", "notrun", "pending"]
        .map(x => '<td class="n">' + (p[x] || 0) + '</td>').join("") + '</tr>';
  }).join("");

  const failures = steps.filter(st => rt(st.uid).state === "failure");
  const failureBlocks = failures.map(st => {
    const r = rt(st.uid);
    return '<div class="fail"><h3>' + e(st.seq) + '. ' + e(r.desc || st.description) + '</h3>' +
      '<div class="k">' + e((PH[st.phase] || {}).id || st.phase) + ' &middot; ' + e(st.step_id || st.uid) +
      ' &middot; ' + e(st.node_target || "local") + (r.handlerOf ? ' &middot; on_failure handler of ' + e(r.handlerOf) : "") + '</div>' +
      '<div class="lbl">command</div><pre>' + e(r.send || st.send || "") + (r.body ? "\n\n" + e(r.body) : "") + '</pre>' +
      '<div class="lbl">output' + (st.kind === "rest" ? " (HTTP " + e(r.status) + ")" : " (exit " + e(r.exit) + ")") + '</div>' +
      '<pre>' + e(r.output || (r.completed === false ? "(did not complete)" : "(none)")) + '</pre>' +
      (r.messageText ? '<div class="lbl">message</div><div>' + e(r.messageText) + '</div>' : "") +
      ((r.routes || []).length ? '<div class="lbl">what happened next</div><ul>' +
        r.routes.map(x => '<li>' + e(x) + '</li>').join("") + '</ul>' : "") +
      '</div>';
  }).join("") || '<p class="k">No step failed.</p>';

  const stepRows = steps.map(st => {
    const r = rt(st.uid);
    return '<tr class="r-' + e(r.state) + '"><td class="n">' + e(st.seq) + '</td>' +
      '<td>' + e((PH[st.phase] || {}).id || st.phase) + '</td>' +
      '<td class="mono">' + e(st.step_id || "") + '</td>' +
      '<td>' + e(r.desc || st.description) +
        (r.note ? '<div class="k">' + e(r.note) + '</div>' : "") +
        ((r.notes || []).length ? '<div class="k">' + e(r.notes.join(" · ")) + '</div>' : "") + '</td>' +
      '<td class="mono cmd">' + e(r.send || st.send || "") + '</td>' +
      '<td>' + pill(r.state) + (r.choice && r.choice !== "pending" ? '<div class="k">' + e(r.choice) + '</div>' : "") + '</td>' +
      '<td>' + e(r.messageText || "") + '</td></tr>';
  }).join("");

  const engineRows = Object.keys(s.engine).map(k =>
    '<tr><td class="mono">' + e(k) + '</td><td class="mono">' + e(s.engine[k]) + '</td></tr>').join("");

  const lintRows = (lint || []).map(f =>
    '<li class="' + e(f.severity) + '"><b>' + e(f.severity) + '</b> <span class="mono">' + e(f.rule) +
    '</span> ' + e(f.message) + (f.where ? ' <span class="k">' + e(f.where) + '</span>' : "") + '</li>').join("");

  const title = (meta.activity || "activity") + " - " + (meta.node || "node");
  return '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    '<title>' + e(title) + ' execution report</title><style>' + REPORT_CSS + '</style></head><body><div class="wrap">' +
    '<h1>' + e(title) + '</h1>' +
    '<div class="sub">Simulated execution report &middot; CR ' + e(meta.crGroup || "") +
    ' &middot; nodeGroup ' + e(meta.nodeGroup || "") + ' &middot; workflow ' + e(meta.workflow || "") +
    ' &middot; generated ' + e(meta.generated || "") + ' &middot; exported ' + e(new Date().toISOString().slice(0, 19).replace("T", " ")) +
    (result.strict ? ' &middot; strict engine' : '') + '</div>' +
    '<div class="status s-' + e(s.status) + '">' + e(s.status) +
    (s.stoppedAt ? ' <span>stopped at ' + e(s.stoppedAt) + '</span>' : "") + '</div>' +
    '<div class="tiles">' + ["success", "warning", "failure", "skipped", "notrun", "pending"].map(x =>
      '<div class="tile t-' + x + '"><b>' + (s.counts[x] || 0) + '</b><span>' + REPORT_LABEL[x] + '</span></div>').join("") + '</div>' +
    '<h2>Phases</h2><table><thead><tr><th>Phase</th><th class="n">Success</th><th class="n">Warning</th>' +
    '<th class="n">Failure</th><th class="n">Skipped</th><th class="n">Not executed</th><th class="n">Pending</th></tr></thead>' +
    '<tbody>' + phaseRows + '</tbody></table>' +
    '<h2>Execution summary variables</h2><p class="k">What ExecutionOrchestrator.applyExecutionSummaryVariables() ' +
    'would set at the end of this run.</p><table><tbody>' + engineRows + '</tbody></table>' +
    '<h2>Failures</h2>' + failureBlocks +
    '<h2>Every step</h2><table class="steps"><thead><tr><th class="n">#</th><th>Phase</th><th>Id</th><th>Step</th>' +
    '<th>Command</th><th>Result</th><th>Message</th></tr></thead><tbody>' + stepRows + '</tbody></table>' +
    (lintRows ? '<h2>Template findings</h2><ul class="lint">' + lintRows + '</ul>' : "") +
    '<h2>Execution log</h2><pre class="log">' + e(buildLog(doc, result, meta)) + '</pre>' +
    '<p class="k">Simulated: every output here was synthesised from the step criteria or pasted by the operator. ' +
    'Nothing touched a node.</p>' +
    '</div></body></html>\n';
}

function buildLog(doc, result, meta) {
  meta = meta || {};
  const lines = [];
  lines.push("[INFO] simulated execution of " + (meta.activity || "") + " on " + (meta.node || "") +
             (result.strict ? " (strict engine)" : ""));
  for (const line of result.log || []) lines.push(line);
  const c = result.summary.counts;
  lines.push("[SUMMARY] status=" + result.summary.status + " success=" + (c.success || 0) +
             " warning=" + (c.warning || 0) + " failed=" + (c.failure || 0) +
             " skipped=" + (c.skipped || 0) + " not_executed=" + (c.notrun || 0) +
             " pending=" + (c.pending || 0));
  for (const k of Object.keys(result.summary.engine)) lines.push("[SUMMARY] " + k + "=" + result.summary.engine[k]);
  return lines.join("\n");
}

const REPORT_CSS =
  ":root{--bg:#f6f7f9;--panel:#fff;--ink:#1b1f24;--muted:#5b6673;--line:#dde2e8;--ok:#1a7f4b;--okbg:#e8f6ee;" +
  "--bad:#b4232a;--badbg:#fdecec;--warn:#8a5a00;--warnbg:#fff6e0;--idle:#5b6673;--idlebg:#eef1f4}" +
  "@media (prefers-color-scheme:dark){:root{--bg:#12161b;--panel:#1a1f26;--ink:#e6edf3;--muted:#9aa7b4;--line:#2b333d;" +
  "--ok:#5ddc9a;--okbg:#10291d;--bad:#ff8d8d;--badbg:#2d1414;--warn:#ffcc66;--warnbg:#2d2410;--idle:#9aa7b4;--idlebg:#222932}}" +
  "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,Segoe UI,Roboto,Arial,sans-serif}" +
  ".wrap{max-width:1200px;margin:0 auto;padding:22px 16px 60px}h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:26px 0 8px}" +
  "h3{font-size:14px;margin:0 0 2px}.sub,.k{color:var(--muted);font-size:12px}" +
  ".status{display:inline-block;margin:14px 0 8px;padding:6px 14px;border-radius:8px;font-weight:700}" +
  ".status span{font-weight:400;font-size:12px;margin-left:8px}.s-SUCCESS{background:var(--okbg);color:var(--ok)}" +
  ".s-FAIL{background:var(--badbg);color:var(--bad)}.s-INCOMPLETE{background:var(--warnbg);color:var(--warn)}" +
  ".tiles{display:flex;flex-wrap:wrap;gap:8px}.tile{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 14px;min-width:110px}" +
  ".tile b{display:block;font-size:20px}.tile span{font-size:11px;color:var(--muted)}.t-failure b{color:var(--bad)}.t-warning b{color:var(--warn)}.t-success b{color:var(--ok)}" +
  "table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line)}" +
  "th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top;font-size:13px}th{font-size:12px;color:var(--muted)}" +
  "td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}.mono,pre{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}" +
  "td.cmd{max-width:380px;word-break:break-all}pre{white-space:pre-wrap;word-break:break-all;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px;margin:4px 0}" +
  ".fail{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--bad);border-radius:8px;padding:10px 12px;margin:8px 0}" +
  ".lbl{font-size:11px;color:var(--muted);margin-top:6px;text-transform:uppercase;letter-spacing:.04em}" +
  ".p{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px;font-weight:600}" +
  ".p-success{background:var(--okbg);color:var(--ok)}.p-failure{background:var(--badbg);color:var(--bad)}.p-warning{background:var(--warnbg);color:var(--warn)}" +
  ".p-skipped,.p-notrun,.p-pending{background:var(--idlebg);color:var(--idle)}tr.r-failure td{background:var(--badbg)}" +
  ".lint li{font-size:13px;margin:3px 0}.lint .error b{color:var(--bad)}.lint .warning b{color:var(--warn)}.log{max-height:480px;overflow:auto}";

if (typeof module !== "undefined" && module.exports) module.exports = { buildReport, buildLog };

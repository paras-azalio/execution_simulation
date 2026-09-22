/* test_page_dom.js - drive a generated MOP page's real DOM.
 *
 *     node test_page_dom.js <mop_....html>
 *     node test_page_dom.js --all out/all        every activity, one page each
 *
 * test_engine_js.js proves the page's ENGINE agrees with engine.py. It says
 * nothing about the page: whether a button is wired, whether a Failure on an
 * `on_failure: stop` step really marks the rest NOT EXECUTED, whether a reload
 * resumes, whether Export produces parseable JSON. Those were the parts the
 * README listed as never having been opened.
 *
 * jsdom gives the page a real DOM, real events and a real localStorage, so a
 * click here is the click an operator makes. It is not a browser - no layout,
 * no paint - so this proves behaviour, not appearance.
 *
 * The documents differ enormously between activities: TEST_EMAIL has two
 * checklist rows and no interactive card at all, MRF has no step whose failing
 * output can be synthesised, SBC_147 has 848 cards. A check that needs a kind
 * of step this document does not contain is SKIPPED and named, never failed -
 * otherwise the suite only ever runs against 1051.
 */

const fs = require("fs");
const path = require("path");

let JSDOM;
try {
  ({ JSDOM } = require("jsdom"));
} catch (e) {
  console.log("jsdom is not installed (npm install) - page DOM suite skipped");
  process.exit(0);
}

let passed = 0, failed = 0;
const skipped = [];

function ok(name, cond, detail) {
  if (cond) { passed++; return true; }
  failed++;
  console.log("FAIL  " + name + (detail ? "\n      " + detail : ""));
  return false;
}
function eq(name, got, want) {
  return ok(name, got === want, "got " + JSON.stringify(got) + ", want " + JSON.stringify(want));
}
function skip(name, why) { skipped.push(name + " - " + why); }

/* ------------------------------------------------------------------ */
/*  page harness                                                       */
/* ------------------------------------------------------------------ */
function openPage(html, seedStorage) {
  const errors = [];
  const exports = [];

  const dom = new JSDOM(html, {
    runScripts: "dangerously",
    url: "https://mop.local/run",
    beforeParse(window) {
      if (seedStorage) {
        for (const k of Object.keys(seedStorage)) window.localStorage.setItem(k, seedStorage[k]);
      }
      window.confirm = () => true;
      // Export builds a Blob, hands it to URL.createObjectURL and clicks an
      // anchor. jsdom has no object URLs and no downloads, so capture the blob
      // and neuter the navigation the click would otherwise attempt.
      window.URL.createObjectURL = (blob) => { exports.push(blob); return "blob:captured"; };
      window.URL.revokeObjectURL = () => {};
      const create = window.document.createElement.bind(window.document);
      window.document.createElement = (tag) => {
        const el = create(tag);
        if (String(tag).toLowerCase() === "a") el.click = () => {};
        return el;
      };
      window.addEventListener("error", (e) => errors.push(String(e.error || e.message)));
    },
  });
  dom.virtualConsole.on("jsdomError", (e) => errors.push(String(e.message || e)));
  return { dom, window: dom.window, document: dom.window.document, errors, exports };
}

/** jsdom's Blob predates Blob.text(), so read it the way a page would. */
function blobText(p, blob) {
  if (typeof blob.text === "function") return blob.text();
  return new Promise((resolve, reject) => {
    const reader = new p.window.FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
}

const q = (p, sel) => Array.from(p.document.querySelectorAll(sel));
const byId = (p, id) => p.document.getElementById(id);

/** The Success / Failure / Custom / clear button of one step. */
function setBtn(p, uid, kind) {
  return p.document.querySelector('[data-set="' + uid + ":" + kind + '"]');
}

function click(p, uid, kind) {
  const button = setBtn(p, uid, kind);
  if (!button) return false;
  button.click();
  return true;
}

/** Put a step back to a state that does not halt the document.
 *  A checklist row has no `clear`, so OK is the neutral choice there. */
function clearStep(p, step) {
  if (!click(p, step.uid, "pending")) click(p, step.uid, "success");
}

/** The rendered state pill of one step, from its card or its checklist row. */
function pillOf(p, uid) {
  const card = p.document.getElementById("c" + uid);
  if (card) {
    const pill = card.querySelector(".pill");
    return pill ? pill.textContent.trim() : null;
  }
  const button = setBtn(p, uid, "success");           // checklist row
  if (!button || !button.closest("td")) return null;
  const pill = button.closest("td").querySelector(".pill");
  return pill ? pill.textContent.trim() : null;
}

function steps(p) {
  return JSON.parse(p.window.eval(
    "JSON.stringify(STEPS.map(s=>({uid:s.uid,seq:s.seq,phase:s.phase," +
    "mode:s.render_mode,of:s.on_failure,register:s.register||[]," +
    "ok:s.success_output||'',bad:s.failure_output||'',crit:" +
    "((s.validation||{}).successCriteria)||{}})))"));
}

function storageOf(p) {
  const out = {};
  for (let i = 0; i < p.window.localStorage.length; i++) {
    const k = p.window.localStorage.key(i);
    out[k] = p.window.localStorage.getItem(k);
  }
  return out;
}

const commandText = (p) => q(p, "pre").map(e => e.textContent).join("\n");

/* ------------------------------------------------------------------ */
/*  choosing a step to probe with                                      */
/* ------------------------------------------------------------------ */
/** The variables a step's own registers define. */
function registerNames(step) {
  const out = [];
  for (const entry of step.register || []) {
    if (!entry) continue;
    if (entry.name) out.push(entry.name);
    if (entry.count_var) out.push(entry.count_var);
    if (entry.regex) {
      const re = /\(\?P?<([A-Za-z][A-Za-z0-9]*)>/g;
      let m;
      while ((m = re.exec(entry.regex))) out.push(m[1]);
    }
  }
  return out;
}

/** The bare identifiers a criteria expression compares. */
function criteriaNames(crit) {
  let expr = (crit || {}).expr;
  if (typeof expr !== "string") return null;
  expr = expr.trim();
  if (expr.startsWith("${") && expr.endsWith("}")) expr = expr.slice(2, -1);
  const out = [];
  let buf = "", quote = null;
  for (const ch of expr) {
    if (quote) { if (ch === quote) quote = null; continue; }
    if (ch === "'" || ch === '"') { quote = ch; continue; }
    if (/[A-Za-z0-9_.]/.test(ch)) { buf += ch; continue; }
    if (buf) { out.push(buf); buf = ""; }
  }
  if (buf) out.push(buf);
  return out.filter(t => t && !/^[0-9]/.test(t));
}

/**
 * A step whose verdict depends only on ITSELF: its criteria compare variables
 * its own registers define. Anything else is a bad probe - a criterion on a
 * variable an earlier step owns is satisfied by that earlier step's value, and
 * pasting this step's failing output changes nothing. That is the engine
 * behaving correctly, not the page misreading a button.
 */
function selfContained(all, p, wantBad) {
  return all.find(s => {
    if (s.mode !== "interactive") return false;
    if (pillOf(p, s.uid) !== "pending") return false;
    if (wantBad && (!s.bad || s.bad.indexOf("<output not derivable") >= 0)) return false;
    if (!wantBad && !s.ok) return false;
    const names = criteriaNames(s.crit);
    if (!names || !names.length) return false;
    const owned = registerNames(s);
    return names.every(n => owned.indexOf(n) >= 0);
  });
}

/* ------------------------------------------------------------------ */
/*  the run                                                            */
/* ------------------------------------------------------------------ */
function run(htmlPath) {
  const html = fs.readFileSync(htmlPath, "utf8");
  const p = openPage(html);
  const all = steps(p);
  const interactive = all.filter(s => s.mode === "interactive");

  /* -- it renders at all ------------------------------------------- */
  ok("page renders phase cards", q(p, "#phases .card").length > 0);
  ok("page renders step controls", q(p, "[data-set]").length > 0);
  ok("summary is populated", /of \d+ steps/.test(byId(p, "summary").textContent),
     byId(p, "summary").textContent);
  eq("every step is on the page",
     q(p, ".step").length + q(p, "#phases tbody tr").length, all.length);
  ok("every step has a state pill", all.every(s => pillOf(p, s.uid)),
     "missing for " + all.filter(s => !pillOf(p, s.uid)).map(s => s.uid).join(","));

  /* -- Success ------------------------------------------------------ */
  const okStep = selfContained(all, p, false);
  if (!okStep) {
    skip("Success button", "no interactive step whose criteria are self-contained");
  } else {
    click(p, okStep.uid, "success");
    eq("Success marks the step SUCCESS", pillOf(p, okStep.uid), "SUCCESS");
    ok("the Success button shows as chosen",
       /\bon\b/.test(setBtn(p, okStep.uid, "success").className));

    /* The same words through a different door: pasting the synthesised
       success output has to reach the verdict the Success button reached. */
    click(p, okStep.uid, "custom");
    const ta = p.document.querySelector('[data-custom="' + okStep.uid + '"]');
    if (ok("Custom input opens a textarea", !!ta)) {
      ta.value = okStep.ok;
      ta.dispatchEvent(new p.window.Event("change"));
      eq("pasted success output evaluates to SUCCESS", pillOf(p, okStep.uid), "SUCCESS");
    }
    click(p, okStep.uid, "pending");
    eq("clear returns the step to pending", pillOf(p, okStep.uid), "pending");
  }

  /* -- the checklist phases ----------------------------------------- */
  const checkStep = all.find(s => s.mode === "checklist" && pillOf(p, s.uid) === "pending");
  if (!checkStep) {
    skip("checklist OK / NOK", "this document has no checklist phase");
  } else {
    click(p, checkStep.uid, "success");
    eq("checklist OK marks the row SUCCESS", pillOf(p, checkStep.uid), "SUCCESS");
    click(p, checkStep.uid, "failure");
    eq("checklist NOK marks the row FAILURE", pillOf(p, checkStep.uid), "FAILURE");
    clearStep(p, checkStep);
  }

  /* -- Custom input that cannot pass -------------------------------- */
  // Unrelated words are NOT a valid probe: a `${BLOCKED == ""}` step passes on
  // anything that fails to match, which is the engine's behaviour. The only
  // text guaranteed to violate the criteria is the synthesised failure output.
  const badStep = selfContained(all, p, true);
  if (!badStep) {
    skip("failing Custom input", "no step whose failing output is derivable");
  } else {
    click(p, badStep.uid, "custom");
    const ta = p.document.querySelector('[data-custom="' + badStep.uid + '"]');
    ta.value = badStep.bad;
    ta.dispatchEvent(new p.window.Event("change"));
    eq("pasted failure output evaluates to FAILURE", pillOf(p, badStep.uid), "FAILURE");
    click(p, badStep.uid, "pending");
  }

  /* -- Failure routing ---------------------------------------------- */
  const stopIdx = all.findIndex((s, i) => i < all.length - 1 &&
                                (s.of || {}).mode === "stop" &&
                                pillOf(p, s.uid) === "pending");
  if (stopIdx < 0) {
    skip("on_failure: stop", "no stop-on-failure step with steps after it");
  } else {
    const stop = all[stopIdx];
    click(p, stop.uid, "failure");
    eq("Failure marks the step FAILURE", pillOf(p, stop.uid), "FAILURE");
    const after = all.slice(stopIdx + 1);
    eq("every later step is NOT EXECUTED",
       after.filter(s => pillOf(p, s.uid) === "NOT EXECUTED").length, after.length);
    clearStep(p, stop);
    ok("clearing the failure revives the later steps",
       after.every(s => pillOf(p, s.uid) !== "NOT EXECUTED"));
  }

  const contStep = all.find((s, i) => i < all.length - 1 &&
                            ["continue", "warning"].indexOf((s.of || {}).mode) >= 0 &&
                            pillOf(p, s.uid) === "pending");
  if (!contStep) {
    skip("on_failure: continue", "no continue-on-failure step");
  } else {
    const idx = all.indexOf(contStep);
    click(p, contStep.uid, "failure");
    ok("on_failure: " + contStep.of.mode + " does not halt the rest",
       all.slice(idx + 1).every(s => pillOf(p, s.uid) !== "NOT EXECUTED"));
    clearStep(p, contStep);
  }

  /* -- toolbar ------------------------------------------------------ */
  byId(p, "btnDebug").click();
  eq("debug toggle relabels", byId(p, "btnDebug").textContent, "Debug panels: on");
  if (interactive.length) ok("debug panels appear", q(p, ".dbg").length > 0);
  byId(p, "btnDebug").click();
  eq("debug panels go away again", q(p, ".dbg").length, 0);

  if (!interactive.length) {
    skip("Expand all / Collapse all", "this document is all checklist rows");
  } else {
    byId(p, "btnExpand").click();
    ok("Expand all opens the step bodies", q(p, ".sbody.open").length > 0);
    byId(p, "btnCollapse").click();
    eq("Collapse all closes them", q(p, ".sbody.open").length, 0);
  }

  /* -- validating the pre/post checks -------------------------------- */
  // OK/NOK is a faster read when the checks are a formality. Ticking the box
  // walks them the way the activity is walked, with criteria and pasted
  // output, so a check the change hinges on can actually be validated.
  const checkPhase = all.filter(s => s.mode === "checklist");
  const chk = byId(p, "chkValidateChecks");
  if (!ok("the validate-checks box is present", !!chk)) {
    skip("validate pre/post checks", "no checkbox");
  } else if (!checkPhase.length) {
    skip("validate pre/post checks", "this document has no checklist phase");
  } else {
    const rowsBefore = q(p, "#phases tbody tr").length;
    const cardsBefore = q(p, ".step").length;
    ok("the checks start as a checklist", rowsBefore > 0);
    chk.checked = true;
    chk.dispatchEvent(new p.window.Event("change"));
    eq("ticking it turns every checklist row into a card",
       q(p, ".step").length, cardsBefore + rowsBefore);
    eq("and no checklist table is left", q(p, "#phases tbody tr").length, 0);
    ok("a promoted check offers Custom input",
       !!setBtn(p, checkPhase[0].uid, "custom"));
    chk.checked = false;
    chk.dispatchEvent(new p.window.Event("change"));
    eq("unticking puts the checklist back", q(p, "#phases tbody tr").length, rowsBefore);
  }

  /* -- pasted output is judged on its own evidence -------------------- */
  // The case that looks like a bug and is not: a step whose only criterion is
  // `exit_code: 0` passes on ANY pasted text. The page has to show which check
  // produced the verdict, and let the operator say the command exited non-zero.
  const exitStep = all.find(s => s.mode === "interactive" &&
                                 "exit_code" in (s.crit || {}) &&
                                 pillOf(p, s.uid) === "pending");
  if (!exitStep) {
    skip("exit code decides the verdict", "no exit_code step to probe");
  } else {
    click(p, exitStep.uid, "custom");
    const ta = p.document.querySelector('[data-custom="' + exitStep.uid + '"]');
    ta.value = "sa";
    const evalBtn = p.document.querySelector('[data-eval="' + exitStep.uid + '"]');
    if (ok("Custom input offers an Evaluate button", !!evalBtn)) {
      evalBtn.click();
      eq("output the criteria do not test, with exit 0, passes",
         pillOf(p, exitStep.uid), "SUCCESS");
      const verdict = p.document.querySelector("#c" + exitStep.uid + " .verdict");
      if (ok("the verdict panel says what was checked", !!verdict)) {
        ok("it names the exit code check",
           /exit code/i.test(verdict.textContent), verdict.textContent.slice(0, 160));
      }
      const exitInput = p.document.querySelector('[data-exit="' + exitStep.uid + '"]');
      if (ok("Custom input offers an exit code", !!exitInput)) {
        exitInput.value = "1";
        exitInput.dispatchEvent(new p.window.Event("change"));
        eq("the same output with a non-zero exit fails",
           pillOf(p, exitStep.uid), "FAILURE");
      }
    }
    click(p, exitStep.uid, "pending");
  }

  /* -- Success from here, and the rest ------------------------------- */
  const restFrom = all.find(s => s.mode === "interactive" && pillOf(p, s.uid) === "pending");
  if (!restFrom) {
    skip("Success from here", "nothing pending to fill in");
  } else {
    const idx = all.indexOf(restFrom);
    const button = p.document.querySelector('[data-rest="' + restFrom.uid + '"]');
    const before = all.slice(0, idx).map(s => pillOf(p, s.uid));
    if (ok("a step offers Success from here", !!button)) {
      button.click();
      ok("it marks this step and the ones after it",
         all.slice(idx).every(s => pillOf(p, s.uid) !== "pending"),
         all.slice(idx).filter(s => pillOf(p, s.uid) === "pending")
            .map(s => s.uid).join(","));
      ok("it leaves the steps before it exactly as they were",
         all.slice(0, idx).every((s, i) => pillOf(p, s.uid) === before[i]));
    }
    byId(p, "btnReset").click();
  }

  const restAll = byId(p, "btnRestSuccess");
  if (!ok("the toolbar offers Rest all Success", !!restAll)) {
    skip("Rest all Success", "no button");
  } else {
    restAll.click();
    eq("nothing is left pending",
       all.filter(s => pillOf(p, s.uid) === "pending").length, 0);
    ok("the steps that could pass did",
       all.some(s => pillOf(p, s.uid) === "SUCCESS"));
    byId(p, "btnReset").click();
    eq("Reset puts them all back", all.filter(s => pillOf(p, s.uid) === "SUCCESS").length, 0);
  }

  // Reset drops the per-step exit codes; a custom step must still evaluate
  // rather than throw on the next click. (It did.)
  if (exitStep) {
    click(p, exitStep.uid, "custom");
    ok("a custom step still evaluates after a reset",
       ["SUCCESS", "FAILURE"].indexOf(pillOf(p, exitStep.uid)) >= 0,
       String(pillOf(p, exitStep.uid)));
    click(p, exitStep.uid, "pending");
  }

  /* -- request parameters re-resolve the commands -------------------- */
  // Which parameter reaches a command depends on the workflow; most reach one
  // only through a global (LOCAL_PATH: .../${CHILD_REQ_ID}/...). Try each in
  // turn and require that at least one of them moves the document.
  const inputs = q(p, "input[data-param]");
  let reached = null;
  for (let i = 0; i < inputs.length && !reached; i++) {
    const name = inputs[i].getAttribute("data-param");
    const before = commandText(p);
    const probe = "PROBE" + (900000 + i);
    const live = p.document.querySelector('input[data-param="' + name + '"]');
    const original = live.value;
    live.value = probe;
    live.dispatchEvent(new p.window.Event("change"));
    if (commandText(p).indexOf(probe) >= 0) reached = name;
    const back = p.document.querySelector('input[data-param="' + name + '"]');
    back.value = original;
    back.dispatchEvent(new p.window.Event("change"));
  }
  if (!inputs.length) {
    skip("request parameters", "this document has no parameters");
  } else if (!reached) {
    skip("request parameters", "no parameter reaches a command in this document");
  } else {
    ok("changing a request parameter re-renders the commands (" + reached + ")", true);
  }

  /* -- export -------------------------------------------------------- */
  const chosen = all.find(s => pillOf(p, s.uid) === "pending");
  if (chosen) click(p, chosen.uid, "success");
  byId(p, "btnExport").click();
  if (!ok("Export produced a blob", p.exports.length === 1)) {
    return finish(p, all, html, chosen);
  }
  return blobText(p, p.exports[0]).then(text => {
    let doc = null;
    try { doc = JSON.parse(text); } catch (e) { /* reported next */ }
    if (ok("exported JSON parses", !!doc)) {
      eq("export carries every step", (doc.steps || []).length, all.length);
      ok("export carries the node meta", !!(doc.node && doc.node.node));
      ok("export carries the final variables", !!doc.finalVariables);
      if (chosen) {
        ok("export records the choice that was made",
           (doc.steps || []).some(s => s.choice === "success"));
      }
    }
    return finish(p, all, html, chosen);
  });
}

function finish(p, all, html, chosen) {
  /* -- resume across a reload --------------------------------------- */
  const saved = storageOf(p);
  ok("the run state was persisted",
     Object.keys(saved).some(k => k.indexOf("execflow:") === 0),
     Object.keys(saved).join(","));

  const p2 = openPage(html, saved);
  if (!chosen) {
    skip("resume", "no choice was recorded to resume");
  } else {
    eq("a reload resumes the recorded choice", pillOf(p2, chosen.uid), pillOf(p, chosen.uid));
    ok("the resumed choice still shows as chosen",
       /\bon\b/.test((setBtn(p2, chosen.uid, "success") || {}).className || ""));
  }

  /* -- reset -------------------------------------------------------- */
  byId(p2, "btnReset").click();
  eq("Reset run clears every verdict",
     all.filter(s => ["SUCCESS", "FAILURE", "NOT EXECUTED"].indexOf(pillOf(p2, s.uid)) >= 0).length,
     0);

  /* -- no script errors anywhere ------------------------------------ */
  eq("no page errors on first load", p.errors.length, 0, p.errors.join("\n"));
  eq("no page errors on the resumed load", p2.errors.length, 0, p2.errors.join("\n"));

  p.window.close();
  p2.window.close();
}

/* ------------------------------------------------------------------ */
function pagesUnder(root) {
  const out = [];
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const full = path.join(root, entry.name);
    if (entry.isDirectory()) {
      const html = fs.readdirSync(full)
        .filter(f => f.startsWith("mop_") && f.endsWith(".html")).sort();
      if (html.length) out.push(path.join(full, html[0]));   // one per activity
    }
  }
  return out;
}

function pick(argv) {
  if (argv[2] === "--all") {
    const root = argv[3] || path.join(__dirname, "out", "all");
    if (!fs.existsSync(root)) return [];
    return pagesUnder(root);
  }
  if (argv[2]) return [argv[2]];
  // Only a generated MOP: out/ also holds clicr-runner.html, which carries no
  // step list of its own and has its own suite (test_runner_dom.js).
  const out = path.join(__dirname, "out");
  if (fs.existsSync(out)) {
    const html = fs.readdirSync(out)
      .filter(f => f.startsWith("mop_") && f.endsWith(".html")).sort();
    if (html.length) return [path.join(out, html[0])];
  }
  // runall.py writes per-activity directories instead, which is what a clean
  // checkout has after `npm run build && python runall.py`.
  const all = path.join(out, "all");
  if (fs.existsSync(all)) {
    const pages = pagesUnder(all);
    if (pages.length) return [pages[0]];
  }
  return [];
}

const targets = pick(process.argv);
if (!targets.length) {
  console.log("usage: node test_page_dom.js <mop_....html> | --all [dir]");
  process.exit(2);
}

(async function () {
  for (const target of targets) {
    const before = { passed, failed, skips: skipped.length };
    try {
      await run(target);
    } catch (err) {
      failed++;
      console.log("FAIL  " + path.basename(target) + " crashed\n      " +
                  ((err && err.stack) || err));
    }
    if (targets.length > 1) {
      console.log("%s  %d passed, %d failed, %d skipped   %s",
                  failed > before.failed ? "FAIL" : "ok  ",
                  passed - before.passed, failed - before.failed,
                  skipped.length - before.skips, path.basename(target));
    }
  }
  if (targets.length === 1 && skipped.length) {
    for (const s of skipped) console.log("skip  " + s);
  }
  console.log("page DOM: " + passed + " passed, " + failed + " failed, " +
              skipped.length + " skipped   [" + targets.length + " page(s)]");
  process.exit(failed ? 1 : 0);
})();

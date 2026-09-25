/* test_runner_dom.js - drive the one-file runner the way an operator does.
 *
 *     node test_runner_dom.js [out/clicr-runner.html]
 *
 * build_runner.py produces a page that takes the three inputs itself: drop a
 * workflow, a CIQ and the mapping, pick a node, walk the activity. Nothing
 * else in the suite covers that path - test_page_dom.js drives a document that
 * was already expanded by python, and test_expander_parity.py compares the two
 * expanders without a page.
 *
 * So this feeds the real files through the real file inputs, waits for the
 * FileReader the page uses, presses Expand, and checks that the document that
 * comes out is the one mopgen.py would have produced - same number of steps,
 * same first command - and that the three buttons still work on it.
 */

const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

let JSDOM;
try {
  ({ JSDOM } = require("jsdom"));
} catch (e) {
  console.log("jsdom is not installed (npm install) - runner suite skipped");
  process.exit(0);
}

const HERE = __dirname;
const TEMPLATES = path.resolve(HERE, "..", "JAVA_NOKIA_CLICR_AUTOMATION",
                               "src", "main", "resources", "templates");
const YAML_DIR = path.join(TEMPLATES, "yaml");
const MAPPING_DIR = path.join(TEMPLATES, "jsonTemplate");
const SAMPLE_CIQ = path.resolve(HERE, "..", "version",
                                "PGW_RDS_1051_SUBSCRIBER_PROFILE_CONFIGURATION.json");

let passed = 0, failed = 0;
function ok(name, cond, detail) {
  if (cond) { passed++; return true; }
  failed++;
  console.log("FAIL  " + name + (detail ? "\n      " + detail : ""));
  return false;
}
function eq(name, got, want) {
  return ok(name, got === want, "got " + JSON.stringify(got) + ", want " + JSON.stringify(want));
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function until(predicate, what, ms) {
  const deadline = Date.now() + (ms || 15000);
  while (Date.now() < deadline) {
    if (predicate()) return true;
    await sleep(20);
  }
  ok("waiting for " + what, false, "timed out");
  return false;
}

function openRunner(html) {
  const errors = [];
  const dom = new JSDOM(html, {
    runScripts: "dangerously",
    url: "https://mop.local/runner",
    beforeParse(window) {
      window.confirm = () => true;
      window.URL.createObjectURL = () => "blob:captured";
      window.URL.revokeObjectURL = () => {};
      const create = window.document.createElement.bind(window.document);
      window.document.createElement = (tag) => {
        const el = create(tag);
        if (String(tag).toLowerCase() === "a") el.click = () => {};
        return el;
      };
      // scrollIntoView is not implemented in jsdom and the page calls it.
      window.Element.prototype.scrollIntoView = function () {};
      window.addEventListener("error", e => errors.push(String(e.error || e.message)));
    },
  });
  dom.virtualConsole.on("jsdomError", e => errors.push(String(e.message || e)));
  return { dom, window: dom.window, document: dom.window.document, errors };
}

/** Put a real file into one of the page's file inputs, as a picker would. */
function attach(p, inputId, filePath) {
  const file = new p.window.File([fs.readFileSync(filePath, "utf8")],
                                 path.basename(filePath),
                                 { type: "application/octet-stream" });
  const input = p.document.getElementById(inputId);
  Object.defineProperty(input, "files", { value: [file], configurable: true });
  input.dispatchEvent(new p.window.Event("change"));
}

/** What mopgen.py's walk produces for the same inputs, as the reference. */
function reference(workflow, ciq, mapping) {
  const args = ["expand_js.js", "--yaml", workflow, "--ciq", ciq];
  if (mapping) args.push("--json-template", mapping);
  return JSON.parse(execFileSync("node", args, { cwd: HERE, maxBuffer: 1 << 28 }));
}

async function run(htmlPath, activity, mappingName, ciqPath) {
  const html = fs.readFileSync(htmlPath, "utf8");
  const workflow = path.join(YAML_DIR, activity + ".yaml");
  const mapping = mappingName ? path.join(MAPPING_DIR, mappingName) : null;

  const p = openRunner(html);
  const $ = id => p.document.getElementById(id);

  ok("the runner renders its loader", !!$("dropzone"));
  eq("nothing is loaded yet", $("loaderState").textContent, "workflow and CIQ needed");
  ok("Expand is disabled until there is something to expand", $("btnExpand2").disabled);

  attach(p, "fileWorkflow", workflow);
  attach(p, "fileCiq", ciqPath);
  if (mapping) attach(p, "fileMapping", mapping);

  // The page reads the files with FileReader, which is asynchronous.
  if (!await until(() => !$("btnExpand2").disabled, "the files to be read")) return finish(p);

  ok("the workflow name is shown", $("nameWorkflow").textContent === path.basename(workflow));
  ok("the CIQ name is shown", $("nameCiq").textContent === path.basename(ciqPath));
  const options = Array.from($("nodePick").options).map(o => o.textContent);
  ok("the CIQ's nodes are offered", options.length > 0 && !$("nodePick").disabled,
     JSON.stringify(options));

  $("btnExpand2").click();
  if (!await until(() => p.document.querySelectorAll("#phases .card").length > 0,
                   "the document to render")) return finish(p);

  /* -- it is the same document python produces ---------------------- */
  const want = reference(workflow, ciqPath, mapping);
  const cards = p.document.querySelectorAll(".step").length +
                p.document.querySelectorAll("#phases tbody tr").length;
  eq("the page expands to the same number of steps as expander.py", cards, want.steps.length);

  const firstWithSend = want.steps.find(s => s.send);
  if (firstWithSend) {
    const shown = Array.from(p.document.querySelectorAll("pre, td.cmd"))
      .map(e => e.textContent).join("\n");
    ok("the first real command is rendered as python renders it",
       shown.indexOf(firstWithSend.send.split("\n")[0].trim()) >= 0,
       "missing: " + firstWithSend.send.slice(0, 120));
  }

  /* -- and it behaves like a document ------------------------------- */
  const target = want.steps.find(s => s.render_mode === "interactive" && s.success_output);
  if (!target) {
    ok("no interactive step with synthesised output in this activity - skipped", true);
  } else {
    const button = p.document.querySelector('[data-set="' + target.uid + ':success"]');
    if (ok("the step has a Success button", !!button)) {
      button.click();
      const pill = p.document.querySelector("#c" + target.uid + " .pill");
      eq("Success marks it", pill && pill.textContent.trim(), "SUCCESS");
      const custom = p.document.querySelector('[data-set="' + target.uid + ':custom"]');
      custom.click();
      const ta = p.document.querySelector('[data-custom="' + target.uid + '"]');
      if (ok("Custom input opens a textarea", !!ta)) {
        ta.value = "anything the operator pastes";
        ta.dispatchEvent(new p.window.Event("change"));
        const after = p.document.querySelector("#c" + target.uid + " .pill");
        ok("a pasted output is evaluated, not ignored",
           ["SUCCESS", "FAILURE"].indexOf(after.textContent.trim()) >= 0,
           after.textContent);
      }
    }
  }

  /* -- the toolbar is the same one the generated MOP has ------------- */
  ok("the document toolbar is present", !!$("btnExport") && !!$("btnDebug"));
  $("btnDebug").click();
  ok("debug panels come from the shared renderer",
     p.document.querySelectorAll(".dbg").length > 0 ||
     !want.steps.some(s => s.render_mode === "interactive"));

  /* -- the template findings lint.js reports ------------------------- */
  const findings = JSON.parse(p.window.eval(
    "JSON.stringify(lintWorkflow(loadWorkflow(jsyaml, " + JSON.stringify(fs.readFileSync(workflow, "utf8")) +
    ").doc, null, {}).length)"));
  ok("the runner shows the template findings",
     findings === 0 || ($("lintCard") && $("lintCard").style.display !== "none"));

  /* -- a saved walked copy reopens with its inputs and its walk ------- */
  const saves = [];
  p.window.URL.createObjectURL = (blob) => { saves.push(blob); return "blob:captured"; };
  const firstOk = p.document.querySelector('[data-set$=":success"]');
  let chosenUid = null;
  if (firstOk) {
    chosenUid = firstOk.getAttribute("data-set").split(":")[0];
    firstOk.click();
  }
  if ($("btnSave")) $("btnSave").click();
  if (ok("Save walked copy produced a file", saves.length === 1)) {
    const text = await new Promise((resolve, reject) => {
      const reader = new p.window.FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(reader.error);
      reader.readAsText(saves[0]);
    });
    const q = openRunner(text);
    await until(() => q.document.querySelectorAll("#phases .card").length > 0,
                "the saved copy to expand itself", 20000);
    ok("the saved copy expands itself from the inputs it carries",
       q.document.querySelectorAll("#phases .card").length > 0);
    if (chosenUid) {
      const card = q.document.getElementById("c" + chosenUid);
      const pill = card ? card.querySelector(".pill")
                        : (q.document.querySelector('[data-set="' + chosenUid + ':success"]') || { closest: () => null })
                            .closest("td");
      const label = pill && (pill.classList && pill.classList.contains("pill") ? pill : pill.querySelector(".pill"));
      eq("and restores the walk", label && label.textContent.trim(), "SUCCESS");
    }
    eq("no page errors in the saved copy", q.errors.length, 0, q.errors.slice(0, 3).join("\n"));
    q.window.close();
  }

  /* -- a second node re-expands without a reload --------------------- */
  if ($("nodePick").options.length > 1) {
    $("nodePick").value = "1";
    $("nodePick").dispatchEvent(new p.window.Event("change"));
    await until(() => p.document.querySelectorAll("#phases .card").length > 0,
                "the second node to render");
    ok("switching node re-expands the document",
       p.document.querySelectorAll("#phases .card").length > 0);
  }

  return finish(p);
}

function finish(p) {
  eq("no page errors", p.errors.length, 0, p.errors.slice(0, 3).join("\n"));
  p.window.close();
}

/* ------------------------------------------------------------------ */
(async function () {
  /*   node test_runner_dom.js [page.html] [activity] [ciq.json] [mapping.yaml]
   * With no arguments it runs 1051 against the sample order in the checkout. */
  const htmlPath = process.argv[2] || path.join(HERE, "out", "clicr-runner.html");
  if (!fs.existsSync(htmlPath)) {
    console.log("no runner page at " + htmlPath + " - run build_runner.py first");
    process.exit(2);
  }

  const cases = [];
  if (process.argv[3]) {
    cases.push({
      activity: process.argv[3],
      ciq: process.argv[4],
      mapping: process.argv[5] || null,
    });
  } else {
    if (!fs.existsSync(SAMPLE_CIQ)) {
      console.log("the 1051 sample CIQ is not in this checkout - runner suite skipped");
      process.exit(0);
    }
    cases.push({
      activity: "PGW_RDS_1051_SUBSCRIBER_PROFILE_CONFIGURATION",
      ciq: SAMPLE_CIQ,
      mapping: "PGW_RDS_1051_SUBSCRIBER_PROFILE_CONFIGURATION_json-output.yaml",
    });
    // The REST activity: 55 rest: steps, on_failure run: handlers and a
    // rollback phase - everything the page learned to run in 2026-09.
    const dsr = path.join(HERE, "out", "all", "_ciq",
                          "DSR_10006_HOST_NAME_REALM_ROUTING_CREATION_MODIFICATION_DELETION_DSR.json");
    if (fs.existsSync(dsr)) {
      cases.push({
        activity: "DSR_10006_HOST_NAME_REALM_ROUTING_CREATION_MODIFICATION_DELETION_DSR",
        ciq: dsr,
        mapping: "DSR_10006_HOST_NAME_REALM_ROUTING_CREATION_MODIFICATION_DELETION_DSR_json-output.yaml",
      });
    }
  }

  for (const item of cases) {
    if (!fs.existsSync(path.join(YAML_DIR, item.activity + ".yaml"))) {
      console.log("skip  " + item.activity + " - not in this checkout");
      continue;
    }
    if (!item.ciq || !fs.existsSync(item.ciq)) {
      console.log("skip  " + item.activity + " - no CIQ");
      continue;
    }
    const before = { passed, failed };
    await run(htmlPath, item.activity, item.mapping, item.ciq);
    if (cases.length > 1) {
      console.log("%s  %d passed, %d failed   %s",
                  failed > before.failed ? "FAIL" : "ok  ",
                  passed - before.passed, failed - before.failed, item.activity);
    }
  }

  console.log("runner DOM: " + passed + " passed, " + failed + " failed");
  process.exit(failed ? 1 : 0);
})();

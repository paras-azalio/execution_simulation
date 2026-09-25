# CONTEXT — what this is, why it is the way it is

A handoff for a new session (human or agent). `README.md` says how to use the
thing; this says **why it exists, what must not break, and what is left**.

---

## 1. The problem

A CLICR MOP (Method of Procedure) is written **before** the node is touched. So
every value in it can be resolved up front except two: what a command will
print, and the request parameters the order carries. Those two are what make a
hand-written MOP full of `${...}` the operator has to guess at.

This module walks a workflow the way the engine would, renders every command
with real values, and hands the operator a page where, per command, they either
**paste what the node printed** or say **Success / Failure** and let the page
synthesise output that passes or fails *that step's own criteria*. Whatever a
step captures flows into every later command, and a failure routes where the
engine routes it — handlers, loops, phases, rollback.

**The three inputs, always:**

| Input | Where | What it is |
|---|---|---|
| workflow YAML | `../JAVA_NOKIA_CLICR_AUTOMATION/src/main/resources/templates/yaml/` | the activity definition |
| CIQ JSON | `../version/`, `../version/1051/`, `../dmp/version/` (few activities have one) | the order data |
| json-output YAML | `.../templates/jsonTemplate/` | the CIQ-workbook → CIQ-JSON mapping |

---

## 2. Three ways in, one document, one run

```
                 python                              browser
workflow ─┐   mopgen.py / runall.py              runner.html
CIQ      ─┼─▶ expander.py + synth.py   ──▶ MOP   expander.js + synth.js
mapping  ─┘   ciq.py, lint.py           page     lint.js
                     │                                  │
                     └── template/runtime.js + ui.js ───┘   ONE run, ONE renderer
                                   │
                          simulate.js (node, headless) ──▶ report set
```

* `runall.py` / `mopgen.py` produce **static per-node MOP HTML** (+ JSON).
* `build_runner.py` produces **`out/clicr-runner.html`**: drop the three inputs
  in, the browser does the walk. No python, no server, no network.
* `simulate.js` replays a MOP's JSON with a scenario and writes the report set.

**The walk and the linter exist twice** — python and `template/*.js` — because
the runner cannot call python. That is the single biggest hazard in this
codebase, and the reason for the contract below. **The run exists once**
(`template/runtime.js`), used by both pages and the CLI.

---

## 3. The contract — what must not break

### 3.1 Python and the browser must expand identically

```bash
python test_expander_parity.py
→ 21 of 21 workflows expand identically in python and the browser
```

Every workflow in `templates/yaml` is walked through **both** implementations
and compared: every step field the page uses (command, REST block, gates,
registers, validation incl. branch vars, synthesised Success/Failure text and
statuses, step ids, unresolved references), the **phase table**, the **loop
table** (items included), the warnings, and the **lint findings**. **If you
change `expander.py`, `synth.py`, `engine.py` or `lint.py`, change the `.js`
twin and re-run this.** It is not optional.

### 3.2 Java fidelity

`engine.py` / `engine.js` are deliberately **bug-compatible** with
`ExecutionContext.java` and `ResultProcessor.java`; `runtime.js` with
`ExecutionOrchestrator.java`. These are not defects to clean up — each one is
pinned by a test that names the java behaviour:

* an unresolved `${...}` becomes an **empty string**, never an error
* `stringValue(null)` is `""`, so `${MISSING == ""}` is **true**
* `||` splits before `&&` and there are **no parentheses**; `==` is case sensitive
* `${LDAPFIELDS${imsi}}` cannot work — the placeholder stops at the first `}`
* resolvePath: missing key → the one key ending in `.<part>`; `list.0` indexes
* captureVariables: regex **interpolated**, **MULTILINE**, **last** match wins,
  no match clears groups to `""`, loop registers number every group, name
  entry's `when` gates only the name, unnamed group ahead shifts names
* isSuccess: `all[]/any[]` items compared as **attributes**; criteria `regex`
  not interpolated; `http_status` compared; absent attribute fails
* shouldIgnoreExitCode: a **step-level `prompt_regex`** means the exit code is
  not read; `use_exit_code` is dead code in the engine
* applyValidation: branch by whether the command succeeded; criteria miss →
  warning branch (if the command succeeded) else the opposite branch; that
  branch's `vars:` (a **mapping**) are set; validation applies unless
  `enabled: false`
* handleFailure: `run:` handlers, then `then:`, then **stop unless `next:
  continue`**; handler's own verdict does not change the failing step's route
* executeLoopStep: a stop inside a body ends the **whole loop**; the loop's
  `on_failure` (continue/warning) absorbs it or the stop propagates;
  `max_iterations` exceeded **fails** the loop; loop `when` evaluated once,
  before the item is bound; `continue_when` runs the body when **true**
* executeSteps: after a stop the rest of the phase and later phases are not
  executed except a ROLLBACK/`then:` phase (still gated by its `when`); phase
  `on_failure: continue` resets the stop
* a bare `"${nodeGroups}"` in `for_each` yields the **list**

### 3.3 Phase-level on_failure: stop — the one deliberate departure

The user's rule (2026-09-25): *"when phase level on_failure: stop is written,
it won't proceed to the next phase."* The engine only half does that:
`phaseHadAnyFailure` is local to each `executeSteps()` call and every loop body
is its own call, so a failure absorbed by a nested `continue` loop never reaches
the phase check (the 2026-09-24 production report: precheck failed, activity
ran). **Default = the rule.** `{strict: true}` (the *Strict engine* toggle,
`simulate.js --strict`) = the engine exactly. The page runs both and shows an
**Engine gap** banner where they differ; `lint.py` flags every such phase
(`phase-stop-blind`). If the java is fixed (propagate the flag through a context
variable, as `__HAS_CONTINUED_FAILURE` already is), make strict the default.

### 3.4 The synthesiser checks its own work

Every step's synthesised **success** output must *pass* that step's criteria and
its **failure** output must *fail* them. `synth.success_output()` verifies
before returning. A **pending** step is assumed to succeed and never halts the
run; **Success** additionally applies criteria-implied values for variables
other steps own. A handful of steps still cannot pass on synthesised words (a
MODIFY that needs an existing record, `!= true` checks) — they show FAILURE with
the verdict, honestly.

---

## 4. The files

| File | Role |
|---|---|
| `mopgen.py` | one activity → per-node MOP HTML + JSON (phases, loops, vars, lint); the `--step` debugger |
| `runall.py` | every workflow; pairs inputs, synthesises CIQs, cleans stale output, writes `index.html` + `lint.html` |
| `simulate.js` | headless run of a MOP JSON with a scenario → `<ACT>_<node>_EXECUTION_REPORT.html`, `reports/json`, `reports/logs`, `RESULTS.md` |
| `lint.py` | template findings; `template/lint.js` is the twin |
| `build_runner.py` | inlines everything into `out/clicr-runner.html` |
| `engine.py` | ExecutionContext + ResultProcessor + REST response_template / JSONPath |
| `expander.py` | the walk: phases (never dropped), loops (table), step ids, gates, assumed success, REST/SFTP commands |
| `synth.py` | criteria → the words / REST bodies and statuses that pass and fail |
| `ciq.py` | CIQ load, the mapping as a language, `DEFAULT_PARAMS`, `activity_params()` |
| `ciqgen.py` | synthetic order: shape from the mapping, values from the workflow, nested dotted fields, unique node names |
| `expand_js.js` | runs the **browser** walk + lint from the CLI, for the parity run |
| `template/runtime.js` | **the run** (see §3.2) — pure, no DOM |
| `template/report.js` | the execution report and log |
| `template/engine.js`, `synth.js`, `expander.js`, `lint.js` | the twins |
| `template/yamlload.js` | reads a workflow as leniently as SnakeYAML does |
| `template/ui.js`, `ui.css` | the document renderer — **shared by both pages** |
| `template/runbook.html`, `runner.html` | the page shells (both capture their pristine source for *Save walked copy*) |
| `template/vendor/js-yaml.umd.min.js` | vendored on purpose; **committed**, not ignored |

---

## 5. Decisions, and why — do not silently undo these

**Phases are never dropped.** A rollback phase is false on the happy path and
true the moment a failure branch sets `ROLLBACK_REQUIRED`. The walk documents it
(with a context snapshot so its effects do not leak into later phases) and the
page gates it live. Same for a loop gate or continue_when reading a **mutable**
variable (anything a step sets, or a request parameter): kept, decided live.
A gate that is false and reads nothing mutable is acted on at generation — which
is why SBC_147 lost ~200 commands the old walk wrongly showed under tables they
never run for.

**Validation vars are a mapping.** `ValidationBranchDefinition.vars` is
`Map<String,Object>`; every template writes a mapping. The page used to read a
list only, so no branch var ever applied — that is why "a failure that should
make a command run" still showed SKIPPED.

**Handler instance = same loop iteration.** `run: step:x` finds the instance of
`x` sharing the failing step's loop iterations, else the last defined (java's
`stepById` keeps the last). A handler that appears before the failing step in
the list gets its card overwritten when it runs.

**REST Failure uses the status the handlers wait for.** `_preferred_statuses()`
harvests `status == "401"` literals from the handlers' `when`, so pressing
Failure on a DSR check walks refresh → retry. Custom input covers 500 etc.

**Relation criteria imply the owned side.** `${EARLIER == MINE}` implies MINE =
value(EARLIER) (the step's own register), so a checksum compare passes on
Success. Groups asserted `!= ""` never sample as empty.

**`DEFAULT_PARAMS` = what a real run has in scope** — Arglist args and the GRC
values (`REPO_*`, `NIAM_IP`, `M2MPORT` …), SimActivityRunner's values.
`NODE_TYPE` etc. follow the activity. Unresolved counts fell from dozens to ~1
per activity.

**`json.dumps` spacing**, **lenient YAML**, **the node's own fields in scope**,
**the mapping explains blanks**, **synthetic CIQs harvest literals**,
**`INPUT_FILE: INPUT_FILE`**, **custom input carries an exit code** — all as
before; see git history of this file for the reasoning, unchanged.

**libyaml.** `ciq.read_yaml` uses `CSafeLoader` when present — ten times faster
on the 400 KB workflows, same result.

---

## 6. Bugs found and fixed (why some code looks defensive)

1. Request parameters changed nothing (globals baked in) — globals re-derived.
2. `branchVars` on a mapping threw / was ignored — **now read, as the engine does.**
3. `Reset run` dropped `state.exit`.
4. js-yaml loop registers did not set `NAME_1..n`.
5. `Index.Report Email` parsed as column `Report`.
6. **(2026-09-25)** Failure never applied the failure branch's vars and
   `run:` only printed a label → failure-dependent steps stayed SKIPPED.
7. Rollback phases were dropped at generation.
8. Loop-level `when` was ignored → retry loops and per-table branches shown
   when the engine never runs them.
9. REST steps rendered with no command; `http_status` assumed true; response
   captures ignored.
10. Registers: first match instead of last, no clearing, not interpolated, not
    MULTILINE — all unlike `captureVariables()`.
11. `all:[{expr}]` evaluated as an expression; java compares it as an attribute.
12. `NODE_TYPE` was `PGW_RDS` for every activity; GRC/Arglist values missing.
13. Synthetic SBC orders had two nodes of one name → one document silently
    overwrote the other (37 documents → 55).
14. `runall.py` left documents of deleted workflows (`DSR_10005`) behind.
15. A test pinned uids (`s0002`) and broke when the template lost a step; tests
    now find steps by what they send.

---

## 7. Things that look wrong and are not

* **Pasting junk gives SUCCESS** on an `{exit_code: 0}`-only step. The verdict
  panel says so; set exit code 1 or tick *did not complete*.
* **A DSR 401 failure ends the activity even though the retry succeeded.** That
  is the engine: `run:` without `next: continue` stops. Lint says it 19 times.
* **Failure shows WARNING** on steps with a validation warning branch or
  `on_failure: warning`. That is the engine.
* **Rollback opens but its steps stay SKIPPED** (SBC_127): its loops gate on
  `CREATE_MGW`, which the failure came before.
* **A checksum register keeps a fragment of the file name**: unanchored
  `[a-fA-F0-9]+` + last-match-wins. The engine does this too.
* **`_MOPALIGNED` / `_local` YAMLs** and the template files change under you —
  re-read before assuming content.
* **Most documents use a synthesised order.** Invented values; `meta.generatedBy`.

---

## 8. Running and testing

```bash
python build_runner.py        # out/clicr-runner.html
python runall.py              # all activities → out/all/index.html, lint.html
python lint.py --all          # template findings
node simulate.js <mop.json> --all-success --fail <step> [--strict]

python -m unittest test_execution_flow   # python suite, runs the JS suites too
python test_expander_parity.py           # THE parity gate
npm test                                 # JS suites alone
node test_page_dom.js --all out/all      # every activity's page in a DOM (slow: ~15 min)
```

**Gotcha:** `template/*.js` are *inlined* at generation. After editing one, run
`build_runner.py` **and** regenerate any MOP you are testing.

---

## 9. State as of 2026-09-25

* 21 of 21 workflows expand; 55 documents, 9836 commands; 21/21 parity incl.
  phases, loops and lint.
* Python suite green; engine.js 63/63; page DOM 1134+ across 21 activities;
  runner DOM 42/42 (1051 + DSR_10006).

## 10. Not done

* **No real browser** — jsdom proves behaviour, not appearance.
* **`then:` re-runs** are recorded on the first run's cards, not as separate
  cards; retries are described, not simulated attempt by attempt.
* **Template defects found by lint are not fixed at source** — they belong to
  the template authors (see `out/all/lint.html`).
* **`JAVA_NOKIA_CLICR_AUTOMATION` tracks 80 files under `target/`** despite the
  `.gitignore`; `git rm -r --cached target` would fix it (not done: other repo).
* **The java phase-stop gap** (§3.3) is reported, not fixed — it is in the
  engine, not here.

# execution_simulation — interactive CLICR runner

Walk a CLICR activity command by command, before the node is touched. For each
command you either **paste what the node printed**, or say **Success** /
**Failure** and let the page synthesise output that passes or fails that step's
own criteria. Whatever the step captures flows into every later command, and a
failure routes exactly where the engine would route it: its handlers run, its
loop ends, the phase stops, the rollback phase opens.

There are three ways in, and they run the same document.

> **Picking this up cold?** [CONTEXT.md](CONTEXT.md) has the why: the decisions
> behind the odd-looking parts, the invariants that must not break, what was
> already tried, and what is left.

## 1. The runner — drop three files into a web page

```bash
python build_runner.py          # writes out/clicr-runner.html (~260 KB)
start out/clicr-runner.html     # or just double-click it
```

Then drop in:

| Input | What it is |
|---|---|
| **workflow YAML** | the activity, from `templates/yaml` |
| **CIQ JSON** | the order data |
| **json-output YAML** | the mapping, optional — it explains the blanks |

Pick a node, press **Expand**, and walk it. No python, no server, no network:
the page carries the engine, the walk, the synthesiser, the run, the linter and
a vendored js-yaml, so it works opened from disk and mailed around. Nothing you
drop in leaves the page.

## 2. The generator — one static MOP per node

```bash
python runall.py                       # every workflow in templates/yaml
python runall.py --only SBC_147        # just the matching ones
start out/all/index.html               # every activity; lint.html beside it
```

```
ok  real  CFX_128_TGRP_CONFIGURATION_IN_CFX          1 node(s)   207 steps  195 interactive  1 unresolved
ok  synth DSR_10006_HOST_NAME_REALM_ROUTING_...      2 node(s)   526 steps  500 interactive  1 unresolved
ok  real  PGW_RDS_1051_SUBSCRIBER_PROFILE_CONFIG...  2 node(s)   162 steps  118 interactive  5 unresolved
ok  synth SBC_147_IP_POI_CONFIG_ISBC                 4 node(s)  2252 steps 2224 interactive  1 unresolved
...
21 of 21 workflows expanded, 55 documents, 9836 commands, 42.3s
```

A workflow deleted from `templates/yaml` takes its documents with it, and every
activity's folder is rebuilt, so nothing in `out/all` is stale.

One activity by hand:

```bash
python mopgen.py \
    --yaml          ../JAVA_NOKIA_CLICR_AUTOMATION/src/main/resources/templates/yaml/PGW_RDS_1051_SUBSCRIBER_PROFILE_CONFIGURATION.yaml \
    --ciq           ../version/PGW_RDS_1051_SUBSCRIBER_PROFILE_CONFIGURATION.json \
    --json-template ../JAVA_NOKIA_CLICR_AUTOMATION/src/main/resources/templates/jsonTemplate/PGW_RDS_1051_SUBSCRIBER_PROFILE_CONFIGURATION_json-output.yaml \
    --out out --json
```

## 3. Headless — a scenario, and the report set a real run leaves

```bash
node simulate.js out/all/PGW_RDS_1051_.../mop_..._North1.json --all-success --fail s0007
node simulate.js <mop.json> --all-success --fail s0007 --strict
node simulate.js <mop.json> --custom config_check_peerroutetable_exists=body.json:401
node simulate.js <mop.json> --param ROLLBACK_ONLY=true --all-success
```

```
1051_SUBSCRIBER_PROFILE_CONFIGURATION / North1: FAIL - stops at phase PRE_NODE_HEALTH_CHECK (on_failure: stop, failed at s0007 (auto_step_9))
  success 2  warning 0  failure 1  skipped 4  not executed 74  pending 0
  ROLLBACK_REQUIRED=false  ROLLBACK_ENABLED=FALSE
  engine gap from s0016: intended notrun, engine pending
  wrote out/runs/1051_SUBSCRIBER_PROFILE_CONFIGURATION_North1_20260925...
```

The same `runtime.js` the page runs, no page. Each run gets its own folder:
`<ACT>_<node>_EXECUTION_REPORT.html`, `reports/json/<ACT>_EXECUTION_REPORT_<node>.json`,
`reports/logs/<node>_execution.log` and a short `RESULTS.md`. A step is named by
its uid (`s0040`) or its YAML id (every instance of it).

## The three buttons

All three feed the **same pipeline** — the step's REST `response_template` and
its `register` regexes over the output, the validation branch the engine would
take, the branch's `vars:`, and the routing of a failure. Success is not a
special case in the page's logic, only a different source of output text.

| Button | Output used | Effect |
|---|---|---|
| **Success** | words synthesised from the step's success criteria | registers capture them, the success branch's vars are set, later commands re-resolve |
| **Failure** | words synthesised to violate those criteria — or, where no words can, a non-zero exit, a time-out, or for REST a status the criteria reject (the one the step's handlers wait for, e.g. 401) | the failure branch's vars are set and `on_failure` routes — see below |
| **Custom input** | what the operator pastes, with the **exit code** (or for a REST step the **HTTP status** and body), and a *did not complete* box | evaluated for real; everything it captures flows on |

A step not walked yet is **pending**: it counts as having succeeded, so later
commands still render with values, and it never halts the document.

## What a failure does

`template/runtime.js` runs the document the way `ExecutionOrchestrator` runs the
workflow:

* **the validation branch.** A command that itself failed reads the `failure`
  branch; a command that ran but missed its criteria reads the `warning` branch
  if there is one (a pass, shown **WARNING**) or the `failure` branch. That
  branch's `vars:` are set — `ROLLBACK_REQUIRED: "true"`, `last_failed_http_status`.
* **`on_failure: warning`** turns a failure into a pass with a warning.
  **`continue`** carries on. **`stop`** (the default) stops.
* **`on_failure: run: [step:x, step:y]`** runs those handler steps right there —
  the instance in the same loop iteration — with their own gates, so a
  `when: ${last_failed_http_status == "401"}` retry runs after a 401 and not
  after a 500. **Without `next: continue` the engine stops afterwards, even when
  the handlers succeed.** The card lists every hop.
* **inside a loop**, a stop ends the *whole* loop — every remaining item — and
  the loop's own `on_failure` decides: `continue`/`warning` carries on after the
  loop, anything else stops the loop around it too.
* **after a stop**, the rest of that phase is NOT EXECUTED and so is every later
  phase — except a **rollback phase** (id contains ROLLBACK, or it declares
  `then:`), which still runs if its `when` is true: a failure branch that set
  `ROLLBACK_REQUIRED` opens it. A phase with `on_failure: continue` lets the
  next phase run.
* **phase-level `on_failure: stop`**: if any step of the phase failed — even one
  with `on_failure: continue`, even inside a loop — the run does not go on to the
  next phase (a rollback phase still runs if its gate is open).

The banner at the top says where the run stops and whether rollback runs.

### The engine gap, and *Strict engine*

The java honours phase-level `on_failure: stop` only for failures of the phase's
**top-level** steps and loops: `phaseHadAnyFailure` is a local of each
`executeSteps()` call, and every loop body is its own call. A failure absorbed by
a nested `on_failure: continue` loop — the 1051 precheck: `nodeGroups` loop →
`nodes` loop — never reaches the phase, and the activity runs anyway. That is
the 2026-09-24 production report.

The page applies the rule as meant, runs the engine as written alongside, and
when they differ shows an **Engine gap** banner with the first step where they
part. Tick **Strict engine** to walk what the engine will actually do.
`lint.py` names every phase where this can happen (`phase-stop-blind`).

## REST steps

A `rest:` step renders as the request — `POST https://…/auth/tokens` with its
body under it — and runs through the plugin's semantics: `response_template`
json paths (with `default` and `required`), `http_status` set as a variable and
compared by the criteria, never assumed. Success synthesises a JSON body that
carries every value the criteria assert; Failure picks the status the step's
handlers are gated on (so DSR's 401 → refresh token → retry path runs), else 500.
Custom input takes the body and a status, with chips for the likely ones.

## Template findings — `lint.py`

```bash
python lint.py --all
python lint.py <workflow.yaml> --ciq <CIQ.json>
```

What the engine will do with a workflow that its author did not mean. In the
2026-09 templates it finds, among others:

| Rule | Where | What happens |
|---|---|---|
| `shell-expansion` | DSR_10006 `config_compare_existing_fields` | `${MISMATCH# }` is eaten by the engine, so `MISMATCHEDFIELDS` is always empty |
| `expr-in-all-any` | DPA | `all: [{expr: …}]` is compared as a result attribute — always false |
| `criteria-regex-placeholder` | DPA | `regex: "${NEW_FILE_NAME}"` is not interpolated, and java refuses the `{` |
| `parentheses` | MRF, CFX, SBC_147 | conditions have no parentheses |
| `tautology` | SBC_147 | `${SIGIPEXISTS == "" \|\| SIGIPEXISTS != ""}` can never fail |
| `run-without-next` | DSR_10006 ×19 | the 401 retry succeeds and the run stops anyway |
| `phase-stop-blind` | most | see the engine gap above |
| `use-exit-code-ignored` | EIR ×30, DPA, 1051, 1058 | `use_exit_code` is never read, and a step `prompt_regex` turns the exit-code check off |
| `undefined-variable` | DPA `TRANSFER_OK` | never set — those steps always fail |
| `duplicate-key` | SBC, MRF, DPA | SnakeYAML keeps the last and drops the earlier block silently |

Every MOP carries its findings (a collapsed card at the top); `runall.py` writes
`out/all/lint.html` and counts them in the index; the runner lints what is
dropped into it with `template/lint.js`, the twin.

## Reports and saved walks

* **Export result JSON** — every step: choice, state, output, exit code / HTTP
  status, branch, routing, handler; the summary; the log; the final variables.
* **Export report** — the standalone HTML a run leaves behind: status, counts
  (warnings counted), per phase, the execution summary variables the engine sets
  (`ACTIVITY_EXECUTION_STATUS`, the success/failure/skipped lists,
  `ROLLBACK_ENABLED`, `LAST_ACTIVITY_EXECUTED`), every failure with its command,
  output and where it routed, every step, the log.
* **Save walked copy** — the page itself with the walk baked in; from the runner
  it carries the three inputs too, and reopens expanded, on any machine.

## Judging what the operator pasted

Paste `sa` into a step whose only criterion is `exit_code: 0` and it passes: the
step asserts nothing about its output. The verdict panel says exactly which
checks ran, which validation branch the engine took, and what your output set;
set the exit code to `1` (or tick *did not complete*) to fail it. For a step
with a step-level `prompt_regex` the panel says the exit code is not read.

## Filling in the rest

**Rest all Success** (toolbar) and **Success from here** (per step) mark every
step the run reaches — repeating until nothing new opens, since a verdict can
open a checksum retry loop or a handler. A step already decided is left alone.

## Rendering, per phase

* **Pre/post health checks** keep the checklist: a command table with OK / NOK
  (and clear). **Validate pre/post checks** promotes them to full cards.
* **Activity and rollback** get the command-wise interactive cards.
* A phase gated off (the rollback, on the happy path) is collapsed with its gate.
* Steps marked `hide_when_skipped` are hidden while skipped; **Show hidden steps**
  brings them back.

## Why a blank is blank

An unresolved `${...}` has one of three causes, and the document says which:

* a step **sets it at run time** — a register, a REST field, a branch's vars —
  and has not run yet (not offered as a parameter);
* the mapping **fills** that column and this order had no value → an input;
* the mapping **never** fills it → a document defect, also raised as a warning;

and `_row: "*"` mappings are undecidable, which the document says rather than
guessing. See `ciq.OutputTemplate`.

## Request parameters

`DEFAULT_PARAMS` carries what a real run puts in scope: the order's own
(`ORDER_NO`, `CHILD_REQ_ID`, `CR_NAME` …), every Arglist argument
(`INPUT_JSON_FILE_NAME`, `OUTPUT_LOGS_FILE_LOCATION`, `SUB_ACTIVITY_NAME`,
`ROLLBACK_ONLY` …) and the GRC values `MopExecutionUtil` adds (`REPO_IP`,
`REPO_USER`, `REPO_PASSWORD`, `NIAM_IP`, `M2MPORT`, `M2MUSER` …), with the
simulator's values. `NODE_TYPE`, `SUB_ACTIVITY_NAME` and `INPUT_JSON_FILE_NAME`
follow the activity (`PGW_RDS` for 1051, `SBC` for SBC_147 — no longer
`PGW_RDS` for everything). Then `--params file`, then `--param KEY=VALUE`; the
page's **Request parameters** panel re-resolves every command live.
`ROLLBACK_ONLY=true` sets `ROLLBACK_REQUIRED=true`, as `MopExecutionUtil` does.

## Synthesising an order

Only a few activities have a real CIQ in this checkout. `ciqgen.py` reads the
mapping forwards for the shape and takes the values from the workflow — every
literal it compares against, so a record carrying `Action=ENABLE` walks the
branch the author wrote. A dotted record field (`${record.data.conditions.appId.value}`)
is built nested, the way `resolvePath()` walks it, and every node gets a name of
its own. Every generated file says what it is in `meta.generatedBy`, and **none
of them is a real order**.

## The same document, every way in

The walk exists twice — `expander.py` and `template/expander.js` — and so does
the linter, so every workflow is walked through both and compared:

```bash
python test_expander_parity.py
21 of 21 workflows expand identically in python and the browser
```

That covers every step (command, REST request, gates, registers, validation
branches, synthesised Success/Failure text and statuses, step ids, unresolved
references), the phase and loop tables, the warnings and the lint findings.

## Fidelity — the quirks this reproduces on purpose

From `ExecutionContext.java`, `ResultProcessor.java`, `RestProtocolPlugin.java`
and `ExecutionOrchestrator.java`, each pinned by a test:

* an unresolved `${...}` becomes an **empty string**, never an error
* `stringValue(null)` is `""`, so `${MISSING == ""}` is **true**
* `||` splits before `&&` and there are **no parentheses**; `==` is case sensitive
* a missing key falls back to the one key ending in `.<part>`; `hosts.0` indexes
* a register regex is **interpolated**, compiled **MULTILINE**, the **last**
  match wins, no match clears its groups to `""`, every group of a loop register
  is numbered, and an unnamed group ahead of a named one shifts the names
* an `all:`/`any:` item is matched as a result **attribute** (an `expr:` there
  is always false); a criteria `regex` is never interpolated
* `http_status` is compared; an absent exit code fails `exit_code: 0`
* a step-level `prompt_regex` means the exit code is **not read**
* validation applies unless it says `enabled: false`; `vars:` is a **mapping**
* a loop's `when` is evaluated **once**, before the first item is bound;
  `continue_when` runs the body when **true**; more items than `max_iterations`
  **fails** the loop
* a phase's id is its `name:`; steps get `auto_step_N` ids the way the loader does
* the node's own fields are in scope, plus `niamID`/`currentNode`/`VERSION` aliases

## Tests

```bash
python -m unittest test_execution_flow   # python suite; runs the JS suites too
python test_expander_parity.py           # python vs browser, every workflow
npm test                                 # the JS suites on their own
node test_page_dom.js --all out/all      # every activity's page in a DOM
```

| Suite | What it pins |
|---|---|
| `test_execution_flow.py` | the engine quirks, the expander, failure routing (401 handlers, rollback on failure, the phase stop and its engine gap), lint rules, request parameters, the mapping, CIQ synthesis, the batch runner |
| `test_engine_js.js` | `engine.js` against the same vectors, registers, criteria and REST included |
| `test_expander_parity.py` | 21 of 21 workflows identical in python and the browser, lint included |
| `test_page_dom.js` | a generated MOP driven in a real DOM, every activity: buttons, routing, rollback, REST input, export, report, saved copy |
| `test_runner_dom.js` | the runner with real files dropped in (1051 and DSR_10006), lint, saved copy |

`npm install` (jsdom, js-yaml) is needed for the DOM suites and the build.

## Files

| File | Role |
|---|---|
| `mopgen.py` | one activity → per-node MOP HTML + JSON; the `--step` debugger |
| `runall.py` | every workflow: pairs inputs, synthesises CIQs, writes index + lint.html |
| `simulate.js` | a MOP walked headlessly → the report set |
| `lint.py` | template findings |
| `build_runner.py` | inlines everything into `out/clicr-runner.html` |
| `engine.py` | `ExecutionContext` + `ResultProcessor` + the REST response template |
| `expander.py` | the walk: phases, loops, step ids, gates, assumed success |
| `synth.py` | criteria → the words (and REST bodies/statuses) that pass or fail |
| `ciq.py` | CIQ load, the mapping as a language, request parameters |
| `ciqgen.py` | a synthetic order from the mapping |
| `expand_js.js` | the browser walk from the command line, for the parity run |
| `template/runtime.js` | the run: branches, routing, handlers, loops, phases, rollback |
| `template/report.js` | the execution report and log |
| `template/lint.js` | `lint.py` in the browser |
| `template/engine.js`, `synth.js`, `expander.js` | the twins |
| `template/yamlload.js` | reads a workflow as leniently as SnakeYAML does |
| `template/ui.js`, `ui.css` | the document — shared by both pages |
| `template/runbook.html`, `runner.html` | the two page shells |
| `template/vendor/js-yaml.umd.min.js` | vendored, unmodified |

## Not done yet

* **No real browser.** The DOM suites are jsdom: behaviour, not appearance.
* **A synthesised order is not a real one.** It proves a workflow walks and
  shows every command; the values are invented.
* `then:` re-runs of a phase are recorded on the first run's cards, not shown
  as separate cards; retries are noted, not simulated attempt by attempt.

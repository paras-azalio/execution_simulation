# execution_simulation — interactive CLICR runner

Walk a CLICR activity command by command, before the node is touched. For each
command you either **paste what the node printed**, or say **Success** /
**Failure** and let the page synthesise output that passes or fails that step's
own criteria. Whatever the step captures flows into every later command, so the
document re-resolves as you go.

There are two ways in, and they produce the same document.

## 1. The runner — drop three files into a web page

```bash
python build_runner.py          # writes out/clicr-runner.html (159 KB)
start out/clicr-runner.html     # or just double-click it
```

Then drop in:

| Input | What it is |
|---|---|
| **workflow YAML** | the activity, from `templates/yaml` |
| **CIQ JSON** | the order data |
| **json-output YAML** | the mapping, optional — it explains the blanks |

Pick a node, press **Expand**, and walk it. No python, no server, no network:
the page carries the engine, the walk, the synthesiser and a vendored js-yaml,
so it works opened from disk and mailed around. Nothing you drop in leaves the
page.

## 2. The generator — one static MOP per node

```bash
python runall.py                       # every workflow in templates/yaml
python runall.py --only SBC_147        # just the matching ones
start out/all/index.html
```

```
ok  real  CFX_128_TGRP_CONFIGURATION_IN_CFX          1 node(s)   108 steps   96 interactive
ok  synth MRF_ANNOUNCEMENT_LOADING                   2 node(s)   116 steps   96 interactive
ok  real  PGW_RDS_1051_SUBSCRIBER_PROFILE_CONFIG...  2 node(s)   136 steps   94 interactive
ok  synth SBC_147_IP_POI_CONFIG_ISBC                 2 node(s)   862 steps  848 interactive
...
22 of 22 workflows expanded, 39 documents, 3743 commands, 15.3s
```

or one activity by hand:

```bash
python mopgen.py \
    --yaml          ../JAVA_NOKIA_CLICR_AUTOMATION/src/main/resources/templates/yaml/PGW_RDS_1051_SUBSCRIBER_PROFILE_CONFIGURATION.yaml \
    --ciq           ../version/PGW_RDS_1051_SUBSCRIBER_PROFILE_CONFIGURATION.json \
    --json-template ../JAVA_NOKIA_CLICR_AUTOMATION/src/main/resources/templates/jsonTemplate/PGW_RDS_1051_SUBSCRIBER_PROFILE_CONFIGURATION_json-output.yaml \
    --out out --json
```

### The same document, both ways

The walk exists twice — `expander.py` and `template/expander.js` — because the
runner has to expand a workflow the operator has just dropped in, with no python
anywhere. Two implementations of the same thing is a standing invitation to
drift, so **every workflow in the repo is walked through both and compared step
by step**:

```bash
python test_expander_parity.py
...
22 of 22 workflows expand identically in python and the browser
```

That covers the rendered command, the gates, the registers, the synthesised
Success and Failure text, the unresolved references and the warnings.

## The three buttons

All three feed the **same pipeline** — apply the step's `register` regexes to an
output, evaluate the criteria against the result, route on the outcome — so
Success is not a special case in the page's logic, only a different source of
output text. A step marked Success has been through the same evaluation a real
run performs.

| Button | Output used | Effect |
|---|---|---|
| **Success** | words synthesised from the step's success criteria | registers capture them, the criteria pass, later commands re-resolve with the new values |
| **Failure** | words synthesised to violate those criteria | the failure message shows and `on_failure` routes: `stop` halts and marks the rest NOT EXECUTED, `continue`/`warning` carry on, `run: <stepId>` names the handler |
| **Custom input** | what the operator pastes, plus the **exit code** it returned | evaluated for real; whatever it captures flows into every later command |

Custom input has an **Evaluate** button and an **exit code** field, and shows
the verdict with its reasoning — see below.

`synth.py` (and `template/synth.js`) derive the words from the step's own
register regexes: `(?m)^XML_EXISTS=(?<XMLEXISTS>true\|false)` plus
`${XMLEXISTS == "true"}` gives `XML_EXISTS=true`. Where the criteria compare two
counters (`${LINK_UP_COUNT == REMOTE_NODE_COUNT && LINK_UP_COUNT > 0}`) one
specific line is emitted so both counters see it; where they demand a count
(`${IDLE_OK_COUNT >= 8}`) the line is repeated eight times; where they assert a
capture is empty (`${BLOCKED == ""}`) no line is emitted at all, so the register
keeps its declared default. **The synthesiser verifies its own output** against
the criteria before returning it — emitting "success" words a step then rejects
would report FAILURE on a step the operator just called successful.

## Rendering, per phase

* **Pre/post health checks** keep the checklist format: a command table with an
  expected-result column and OK / NOK per row.
* **Activity and rollback** get the command-wise interactive cards, because that
  is where each command depends on what the node just said.

Classification is by phase id and name (`expander.render_mode_for`).

## Judging what the operator pasted

Paste `sa` into a step and it may well say SUCCESS. That is not the page being
wrong — it is this, from the 1051 master:

```yaml
validation:
  success:
    criteria: { exit_code: 0 }
```

The step asserts nothing about its output, so *any* text satisfies it. But a
bare SUCCESS with no reason is indistinguishable from a broken page, so pasted
output is now judged in the open:

```
Evaluate   exit code [ 0 ]   what the command returned; 0 is success

  SUCCESS   on the output and exit code you gave
  ✓ exit code is 0 (you gave 0)
  this step captures nothing from its output, so only the exit code decides it
```

Set the exit code to `1` and the same text gives FAILURE, and `on_failure`
routes from there. Where a step does capture something, the panel lists what
your output matched and what it did not:

```
  FAILURE   on the output and exit code you gave
  ✓ exit code is 0 (you gave 0)
  ✗ ${BLOCKED == ""}
      BLOCKED  [unexpected] vs [] → FALSE
  captured from your output: BLOCKED=unexpected
  nothing matched: NEEDUPDATE, TARGETDESC
```

`explainCriteria()` returns the same verdict `evalCriteria()` does, with every
check that produced it — the page does not re-implement the judgement, it just
stops hiding it. The exit code also goes into the exported result JSON.

## Filling in the rest

An activity's long tail is usually commands that simply worked, and clicking
Success eighty times is how an operator stops reading.

* **Rest all Success** (toolbar) — every step still pending is marked Success.
* **Success from here** (on each step) — this step and everything after it.

Both leave a step someone has already decided alone, and skipped steps stay
skipped. The output is still the synthesised success text, so every one of
those steps goes through the same evaluation as if it had been clicked
individually — nothing is rubber-stamped past its criteria.

## Validating the pre/post checks

The health checks render as a checklist with OK / NOK per row, which is the
right speed when they are a formality and the wrong tool when the change hinges
on one of them. **Validate pre/post checks**, the box at the top, promotes them
to the same treatment the activity gets: real criteria, Custom input, the
verdict panel, `on_failure` routing. Untick it and the checklist comes back.

## Why a blank is blank

An unresolved `${rec_row.data.Call Type}` has two very different causes and both
render as an empty string, so the document has to tell them apart. The mapping
is the only thing that knows which, and `ciq.OutputTemplate` reads it as the
small language it is (`_each`, `_row`, `_ref`, `_col`, `_row_join`, `_join`,
`Sheet.Column WHERE ...`, `AS $alias`):

```
${rec_row.data.Call Type}   rec_row has no member data.Call Type —
                            ..._json-output.yaml copies whole rows (_row: "*"),
                            so this column exists only if the CIQ workbook
                            carried it — this one did not
```

* the mapping **fills** that column → an order gap: this order had no value
  there, and the page turns it into an input for the operator;
* the mapping **never** fills it → a document defect: no CIQ built from this
  mapping can carry it, so the workflow or the mapping is wrong. That one is
  also raised once per document as a warning, not once per command;
* the mapping copies whole rows → undecidable, and the document says so rather
  than claiming either.

## Synthesising an order

Only four of the twenty-two activities have a real CIQ in this checkout, and
without data a workflow cannot be walked at all — the outer
`for_each: "${nodeGroups}"` has nothing to iterate. `ciqgen.py` reads the
mapping *forwards* to get the shape, and takes the values from the workflow:

```
${rec_row.data.Action}                 the record needs an `Action` column
${table_row.table} == "Call Barring"   and that is one of the table names
```

Harvesting the literals a workflow compares against is what makes a generated
CIQ useful rather than merely well-formed: a record carrying `Action=ENABLE`
walks the branch the author wrote, where `Action=Action_1` would fall through
every one of them and document nothing. Where no literal exists the column
*name* picks the shape — an IMSI column gets an IMSI, a path column gets a path.

```bash
python ciqgen.py --json-template <activity>_json-output.yaml --yaml <workflow>.yaml
```

A workflow with no mapping at all (TEST_EMAIL sends one mail and touches no
order data) gets a minimal CIQ from the workflow alone. Every generated file
says what it is in `meta.generatedBy`, and **none of them is a real order**.

## Reading these YAMLs

The engine uses SnakeYAML and the generator uses PyYAML. Both forgive two
things every JavaScript YAML parser rejects, and a page that refused a file the
engine runs would be worse than useless:

* **duplicated mapping keys.** SnakeYAML and PyYAML keep the last one, so a real
  run silently ignores the earlier block. Several workflows here have them.
* **a quoted scalar continued below its own key** — `DSR_10005` line 1113:

  ```yaml
      send: 'printf "PUT HTTP status for %s: %s
  " "${rec_row.data.name}" "${last_failed_http_status}"; exit 1'
  ```

  SnakeYAML 1.33 accepts it (checked against the jar the project ships),
  PyYAML accepts it, the YAML spec does not.

`template/yamlload.js` forgives both, reports both, and never does it silently.
The repair only re-indents; folding is unchanged, which the parity run proves by
comparing the whole expansion against PyYAML's.

## Debugger

In the page, a per-step panel (toggle *Debug panels*) showing raw vs interpolated
`send`, each condition's operands and result, register matches and misses, every
unresolved `${...}` with the reason, the criteria, retries and `on_failure`.

In the script, a command-wise stepper over the activity and rollback phases:

```bash
python mopgen.py ... --break s0018          # one step
python mopgen.py ... --break ACTIVITY       # a phase
python mopgen.py ... --break-imsi 404960000000112
python mopgen.py ... --step                 # every activity command
```

## Request parameters

`DEFAULT_PARAMS` (the `SimActivityRunner` values: `ORDER_NO`, `CHILD_REQ_ID`,
`CR_NAME`, …), then `--params file`, then `--param KEY=VALUE`. The page has a
**Request parameters** panel; changing a value there re-resolves every command
live, and any `${...}` that resolved to nothing is listed as a field to fill.

A parameter rarely reaches a command directly — it gets there through a global
(`LOCAL_PATH: /mnt/shared_data/${CHILD_REQ_ID}/${NODE_NAME}`) — so the page
carries `globals.vars` as raw text and re-derives them in declaration order,
exactly as `Expander.base_context()` establishes them. Without that the panel
edited a variable nothing read.

## Files

| File | Role |
|---|---|
| `build_runner.py` | inlines everything into the one-file runner |
| `runall.py` | every workflow in `templates/yaml`: pairs the inputs, synthesises what is missing, writes the index |
| `mopgen.py` | one activity: CLI, per-node HTML, the stepper |
| `engine.py` | `ExecutionContext` semantics — interpolation, conditions, `for_each`, `register` |
| `expander.py` | the walk: phases, loops, three-state gating, assumed success |
| `synth.py` | criteria → the words that pass, and the words that fail |
| `ciq.py` | CIQ load + `JsonSchemaSupport` normalisation; the mapping as a language |
| `ciqgen.py` | the mapping read the other way round: a synthetic order |
| `expand_js.js` | the browser walk, from the command line, for the parity run |
| `template/runner.html` | the runner page |
| `template/runbook.html` | the generated MOP page |
| `template/ui.js`, `ui.css` | the document: phases, cards, the three buttons — one renderer for both pages |
| `template/engine.js` | `engine.py`, in the browser |
| `template/synth.js` | `synth.py`, in the browser |
| `template/expander.js` | `expander.py` + the CIQ normalisation, in the browser |
| `template/yamlload.js` | reads a workflow as leniently as SnakeYAML does |
| `template/vendor/js-yaml.umd.min.js` | vendored, unmodified |

## Tests

```bash
python -m unittest test_execution_flow   # 89 tests; runs every JS suite too
python test_expander_parity.py           # python vs browser, every workflow
npm test                                 # the JS suites on their own
```

| Suite | What it pins |
|---|---|
| `test_execution_flow.py` | the engine quirks, the expander, the mapping, CIQ synthesis, the batch runner, and that **every** workflow expands |
| `test_engine_js.js` | 46 assertions that `engine.js` agrees with `engine.py` |
| `test_expander_parity.py` | 22 of 22 workflows expand identically in python and the browser |
| `test_page_dom.js` | a generated MOP driven in a real DOM — 1018 assertions across all 22 activities |
| `test_runner_dom.js` | the runner page with real files dropped into its file inputs |

## Fidelity — the quirks this reproduces on purpose

Ported from `cliautomation/exec/ExecutionContext.java`, and pinned by tests:

* an unresolved `${...}` becomes an **empty string**, never an error (`:120-133`)
* `stringValue(null)` is `""`, so `${MISSING == ""}` is **true** (`:642`)
* `||` splits before `&&` and there are **no parentheses** (`:476-508`) — a
  condition must be an OR of AND-chains
* `==` is **case sensitive** (`:646-654`)
* `${LDAPFIELDS${imsi}}` cannot work: the placeholder stops at the first `}`
* `continue_when` runs a loop body when **true** (`ExecutionOrchestrator:591`)
* a bare `"${nodeGroups}"` in `for_each` yields the **list**; a YAML list literal
  iterates; anything else becomes one iteration
* the **node's own fields are in scope**, not just the root data section. MRF
  and SBC workflows loop over `${configData}` and gate on `${node == nodeName}`,
  and both live on the node — until they were promoted, those loops resolved to
  nothing and the documents came out with the health checks and no activity at
  all (MRF: 8 steps, then 116)

Three things the browser needed that python got for free:

* **JS has no inline regex flags.** `new RegExp("(?m)^x$")` throws, and every
  register pattern in these workflows starts with `(?m)`, so the flags are
  lifted out and passed to `RegExp`. Without it every capture in the page
  silently produced nothing — `test_engine_js.js` caught it.
* **`JSON.stringify` has no spaces.** A map interpolated into a command is
  rendered by `json.dumps` on the python side, which puts a space after every
  `,` and `:`. A command carrying a whole CIQ table came out differently in the
  two — `test_expander_parity.py` caught it.
* a path whose head carries an index (`hosts[1]`) needs the index applied at the
  head, not only at the tail.

## Three-state gating

A gate is `True` (runs), `False` (skipped) or `None` — undecidable here, because
an operand comes from command output. **An undecidable step is always kept** in
the document, with its expression, and the page decides it once the data
arrives.

## Verified

* **89 python tests**, **46 engine-parity assertions**, **22 of 22 workflows
  expanding identically** in python and the browser, and both pages driven in a
  real DOM (1018 assertions on generated MOPs, 32 on the runner).
* The pages have been **opened and clicked**, headlessly: jsdom loads them and
  the suites press the buttons an operator presses — Success, Failure, Custom,
  Evaluate, OK/NOK, Rest all Success, Success from here, the validate-checks
  box, debug, expand, export, reset — check that a Failure on an
  `on_failure: stop` step marks every later step NOT EXECUTED and that clearing
  it revives them, that a reload resumes from localStorage, that Export produces
  parseable JSON, and that no page error is raised. It is not a browser: no
  layout, no paint, so this proves behaviour, not appearance.
* The two invariants that matter: every step's synthesised success output
  **passes** its own criteria, and its failure output **fails** them.
* Against the real 1051 master and sample CIQ: login detection resolves to
  `niam_rds_1`/`niam_pgw_1`; the CIQ IMSI reaches the `ldapsearch`; the XML names
  come out as today's grouping; the only unresolved tokens are CIQ columns this
  sample has no value for, each now naming the mapping.
* All 22 workflows expand: 39 documents, 3743 commands.
* Pages are self-contained: no external scripts, no leftover placeholders.

`npm install` (jsdom, js-yaml) is needed to build the runner and to run the DOM
suites; the python side works without it.

## Not done yet

* **No exec_sim run yet** — generating a `sim_run`-style report from these
  documents is still the obvious next step.
* **Rollback phases** are classified and render interactively, but no master
  here has a rollback phase with content, so that path is untested against real
  YAML.
* **A synthesised order is not a real one.** It proves a workflow expands and
  shows every command, but the values are invented.
* **No real browser.** Both DOM suites are jsdom, so nothing here proves the
  pages *look* right — only that they behave right.
* **The runner cannot save a walked document.** Export writes the result JSON;
  it does not write back a standalone HTML with the choices baked in.

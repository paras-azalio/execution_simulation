/* runtime.js - runs a document the way ExecutionOrchestrator runs a workflow.
 *
 *     const result = simulate(doc, state, { strict: false })
 *
 * `doc` is what the expander produced: steps, phases, loops, the seed vars,
 * the globals and the request parameters. `state` is what the operator chose:
 * per step Success / Failure / Custom (with the output, exit code and HTTP
 * status they gave), plus their parameter edits. Nothing here touches the DOM,
 * so the page, the headless CLI (simulate.js) and the tests share it.
 *
 * What it reproduces from cliautomation/exec/ExecutionOrchestrator.java:
 *
 *   executeStep()       phase.when, when, skip_when - live, on every step
 *   applyValidation()   success/failure branch by whether the command itself
 *                       succeeded; criteria miss -> warning branch (command
 *                       succeeded) or the opposite branch; that branch's vars
 *   applyOnFailure-     on_failure: warning turns a failure into a pass
 *     WarningIfNeeded()
 *   handleFailure()     continue / email / run: [step:x, phase:y] / then: /
 *                       next: continue - and a map without next stops
 *   executeLoopStep()   a failure that stops a loop body ends the WHOLE loop;
 *                       the loop's own on_failure then decides whether the
 *                       enclosing body carries on (continue/warning) or stops
 *   executeSteps()      after a stop the rest of that phase is not executed,
 *                       later phases neither - except a ROLLBACK phase or one
 *                       declaring then:, which still runs if its when is true;
 *                       a phase with on_failure: continue lets the next run;
 *                       a phase with on_failure: stop halts the run at its end
 *                       if any of its steps failed
 *
 * PHASE-LEVEL on_failure: stop
 * ----------------------------
 * The rule, as it is meant to work: if any step of a phase that declares
 * `on_failure: stop` fails - even one whose own on_failure is continue, even
 * deep inside a loop - the run does not go on to the next phase (a rollback
 * phase still runs if its gate is open).
 *
 * The java only half does this. `phaseHadAnyFailure` is a local of each
 * executeSteps() call, and every loop body is its own call, so a failure
 * inside a loop never reaches the phase check - a precheck failure inside the
 * node loop lets the activity run anyway. `{strict: true}` reproduces the
 * engine exactly; the page runs both and says where they part company.
 */

const STATE_EXECUTED = ["success", "failure", "warning", "pending"];

function simulate(doc, state, options) {
  options = options || {};
  state = state || {};
  const strict = !!options.strict;
  const STEPS = doc.steps || [];
  const PHASES = doc.phases || [];
  const LOOPS = doc.loops || {};
  const choices = state.choices || {};
  const custom = state.custom || {};
  const exits = state.exit || {};
  const statuses = state.status || {};
  const timeouts = state.timeout || {};

  const PH = {};
  for (const p of PHASES) PH[p.key] = p;
  const phaseOf = st => PH[st.phase] || { key: st.phase, id: st.phase, when: null,
                                          on_failure: null, then: [], post_failure: false };

  /* -- the variables a run starts with ------------------------------- */
  const vars = Object.assign({}, doc.vars || {}, doc.params || {}, state.params || {});
  /* A request parameter usually reaches a command through a global
     (LOCAL_PATH: /mnt/shared_data/${CHILD_REQ_ID}/...), so the globals are
     re-derived in declaration order - a parameter always wins over a global
     of the same name, as it does in the expander. */
  const overridden = Object.assign({}, doc.params || {}, state.params || {});
  const rollbackOnly = String(overridden.ROLLBACK_ONLY === undefined ? "" : overridden.ROLLBACK_ONLY)
    .toLowerCase() === "true";
  for (const [name, raw] of doc.globals || []) {
    if (name in overridden) continue;
    if (name === "ROLLBACK_REQUIRED" && rollbackOnly) continue;
    vars[name] = (typeof raw === "string") ? interpolate(raw, vars) : raw;
  }
  /* MopExecutionUtil: a rollback-only run sets ROLLBACK_REQUIRED too */
  if (rollbackOnly) vars.ROLLBACK_REQUIRED = "true";
  const startVars = Object.assign({}, vars);

  const results = {};            // uid -> rt
  const handled = {};            // uid -> uid of the step whose on_failure ran it
  const onFailurePhases = new Set();
  const log = [];
  const byStepId = {};
  STEPS.forEach((st, i) => { (byStepId[st.step_id] = byStepId[st.step_id] || []).push(i); });

  const fresh = st => ({ log: [], missing: [], trace: [], routes: [], notes: [] });
  const where = st => st.uid + (st.step_id ? " (" + st.step_id + ")" : "");

  /* -- one step's evidence ------------------------------------------- */
  function evidence(st) {
    const choice = choices[st.uid] || "pending";
    if (choice === "custom") {
      const status = statuses[st.uid];
      return {
        choice: choice,
        output: custom[st.uid] || "",
        exit: parseInt(exits[st.uid] === undefined ? "0" : exits[st.uid], 10) || 0,
        status: (status === undefined || status === "") ? st.success_status
                                                       : parseInt(status, 10),
        completed: !timeouts[st.uid],
      };
    }
    if (choice === "failure") {
      const marker = st.failure_output && st.failure_output.indexOf("<output not derivable") === 0;
      const exit = st.failure_exit === undefined || st.failure_exit === null ? 1 : st.failure_exit;
      // A failing exit code that the engine would not read (a step-level
      // prompt_regex) cannot fail the step; neither can an HTTP status no
      // criterion tests. What is left is the command not completing.
      const completed = !((st.kind === "rest" && exit === -1) ||
                          (st.kind !== "rest" && st.ignore_exit && exit !== 0));
      return { choice: choice, output: marker ? "" : (st.failure_output || ""),
               exit: exit, status: st.failure_status, completed: completed };
    }
    return { choice: choice, output: st.success_output || "", exit: 0,
             status: st.success_status, completed: true };
  }

  /* -- applyValidation() + the verdict -------------------------------- */
  function judge(st, rt) {
    const ev = evidence(st);
    rt.choice = ev.choice;
    rt.output = ev.output;
    rt.exit = ev.exit;
    rt.completed = ev.completed;
    const attrs = {};
    let completed = ev.completed;
    let protocolMessage = completed ? "" : (st.kind === "rest" ? "connection failed" : "timeout");

    if (st.kind === "rest") {
      rt.status = ev.status;
      if (completed) {
        const err = applyResponseTemplate((st.rest || {}).response_template, ev.output, vars, rt.log);
        if (err) { completed = false; protocolMessage = err; }
        else {
          vars.http_status = ev.status;
          attrs.http_status = ev.status;
          rt.log.push({ name: "http_status", value: String(ev.status), source: "REST status" });
        }
      }
      if (!completed) attrs.exit_code = -1;
    } else {
      attrs.exit_code = completed ? ev.exit : -1;
      if (st.kind === "sftp" && completed && ev.exit === 0) attrs.transfer_status = "SUCCESS";
    }
    // captureVariables() runs on whatever came back, before validation
    applyRegisters(st.register, ev.output, vars, rt.log);

    const v0 = st.validation || {};
    if (ev.choice === "pending") {
      /* Not walked yet: the happy path is assumed, so later commands render
         with values - whatever the criteria imply, then the success branch's
         vars. It is never routed as a failure: an untouched step must not halt
         the document, even where no words can pass its criteria. */
      for (const k of Object.keys(st.implied || {})) {
        if (vars[k] === undefined || vars[k] === null || vars[k] === "") vars[k] = st.implied[k];
      }
      if (v0.enabled) {
        const raw = (v0.branchVars || {}).success || {};
        const evaluated = {};
        for (const k of Object.keys(raw)) evaluated[k] = interpolateObject(raw[k], vars);
        Object.assign(vars, evaluated);
      }
      const passes = completed && evalCriteria(v0.successCriteria || {}, vars, ev.output, attrs);
      if (!passes && v0.enabled && Object.keys(v0.successCriteria || {}).length) {
        rt.notes.push("the synthesised success output does not pass this step's own criteria - " +
                      "Custom input decides it (lint may say why)");
      }
      rt.checks = explainCriteria(v0.successCriteria || {}, vars, ev.output, attrs);
      rt.state = "pending";
      rt.messageText = "";
      return true;
    }

    if (ev.choice === "success") {
      /* Success is the operator saying the command did what it should. The
         synthesised words carry what the step's own registers capture; a
         value the criteria assert about a variable another step owns is set
         the way the happy path sets it, or Success could report FAILURE. */
      for (const k of Object.keys(st.implied || {})) {
        if (vars[k] === undefined || vars[k] === null || vars[k] === "") vars[k] = st.implied[k];
      }
    }

    const commandSuccess = completed && (st.kind === "rest" || st.ignore_exit ||
                                         attrs.exit_code === 0 || attrs.exit_code === undefined);
    const v = st.validation || {};
    let matched = true, isWarning = false, branch = null, crit = null;
    if (v.enabled) {
      branch = commandSuccess ? "success" : "failure";
      const has = b => b === "success" ? v.hasSuccessBranch !== false
                                       : (b === "failure" ? !!v.hasFailureBranch : !!v.hasWarningBranch);
      if (has(branch)) {
        crit = branch === "success" ? (v.successCriteria || {}) : v.failureCriteria;
        matched = (crit === null || crit === undefined) ? true : evalCriteria(crit, vars, ev.output, attrs);
        rt.checks = explainCriteria(branch === "success" ? crit : (v.successCriteria || {}),
                                    vars, ev.output, attrs);
        if (!matched) {
          if (commandSuccess && v.hasWarningBranch) { branch = "warning"; isWarning = true; matched = true; }
          else {
            const opposite = commandSuccess ? "failure" : "success";
            if (has(opposite)) branch = opposite;
          }
        }
        // every value interpolated before any is set, then all are set
        const raw = (v.branchVars || {})[branch] || {};
        const evaluated = {};
        for (const k of Object.keys(raw)) evaluated[k] = interpolateObject(raw[k], vars);
        for (const k of Object.keys(evaluated)) {
          vars[k] = evaluated[k];
          rt.log.push({ name: k, value: stringify(evaluated[k]), source: "validation " + branch + " vars" });
        }
      } else {
        branch = null;
      }
    }
    if (!rt.checks) rt.checks = explainCriteria(v.successCriteria || {}, vars, ev.output, attrs);
    rt.commandSuccess = commandSuccess;
    rt.branch = branch;

    let pass = commandSuccess && matched;
    const message = branch === "success" ? v.successMessage
                  : branch === "warning" ? (v.warningMessage || v.failureMessage)
                  : branch === "failure" ? v.failureMessage : "";
    rt.messageText = interpolate(message || "", vars) ||
                     (pass ? "" : (protocolMessage || (commandSuccess ? "criteria not met" :
                       (st.kind === "rest" ? "request failed" : "exit code " + attrs.exit_code))));
    rt.cause = pass ? null : (!completed ? (st.kind === "rest" ? "connection" : "timeout")
                             : (!commandSuccess ? "non_zero_exit" : "regex_miss"));

    // applyOnFailureWarningIfNeeded(): on_failure: warning is a pass, noted
    const warnMode = (st.on_failure || {}).mode === "warning";
    if (warnMode && (!pass || isWarning)) {
      rt.state = "warning";
      rt.notes.push(pass ? "passed on the validation warning branch"
                         : "failed, but on_failure: warning lets the run carry on as a pass");
      return true;
    }
    if (pass && isWarning) {
      rt.state = "warning";
      rt.notes.push("the criteria missed and the validation warning branch took it - a pass with a warning");
      return true;
    }
    rt.state = pass ? (ev.choice === "pending" ? "pending" : "success") : "failure";
    if (!pass && st.retries) {
      rt.notes.push("the engine retries this " + st.retries + " time(s)" +
                    (st.retry_delay_sec ? ", " + st.retry_delay_sec + "s apart" : "") +
                    " before giving up - the same output fails every attempt");
    }
    return pass;
  }

  /* -- a run of steps: executeSteps() --------------------------------- */
  function runSequence(indices, opts) {
    opts = opts || {};
    const run = {
      stoppedAtPhase: null, stopAt: null, onFailureRunTriggered: false,
      currentPhase: null, phaseHadAnyFailure: false, phaseFailureAt: null,
      wasStopped: false,
      loopDone: {}, loopEntered: {}, iter: {}, iterSkip: {},
    };
    let prevUnit = null, unitSkip = null, prevFrames = [];

    const markPhaseFailure = (st, topLevel) => {
      vars.__HAS_CONTINUED_FAILURE = "true";
      if (!strict || topLevel) {
        run.phaseHadAnyFailure = true;
        if (!run.phaseFailureAt) run.phaseFailureAt = st;
      }
    };

    /* A stop inside loop bodies: each loop it is in ends; the innermost
       loop's on_failure decides whether its parent body carries on. */
    const propagateStop = (st, rt, frames, fromLevel, why) => {
      for (let k = fromLevel; k >= 0; k--) {
        const L = LOOPS[frames[k].loop] || {};
        run.loopDone[L.uid || frames[k].loop] = { state: "notrun",
          note: "not executed: loop " + (L.step_id || L.var) + " ended at " + why };
        const mode = L.on_failure || "stop";
        if (mode === "continue" || mode === "warning") {
          markPhaseFailure(st, k === 0);
          if (rt) rt.routes.push("loop " + (L.step_id || L.var) + " ends here (every remaining item is skipped); its on_failure: " +
                                 mode + " lets the run carry on after the loop");
          return;
        }
        if (rt && k > 0) rt.routes.push("loop " + (L.step_id || L.var) + " ends; its on_failure is stop, so the loop around it ends too");
      }
      run.stoppedAtPhase = st.phase;
      run.stopAt = why;
      run.wasStopped = true;
      if (rt) rt.routes.push("the rest of phase " + phaseOf(st).id + " is not executed; later phases neither, " +
                             "except a rollback phase whose when is true");
    };

    const onLoopExit = (frames, level) => {
      const L = LOOPS[frames[level].loop];
      if (!L || run.loopDone[L.uid]) return;
      run.loopDone[L.uid] = { state: "notrun", note: "loop finished" };
      if (L.overflow) {
        // executeLoopStep(): more items than max_iterations is a FAILURE
        log.push("[LOOP] " + (L.step_id || L.var) + " exceeded max_iterations " + L.max_iterations + " - the loop fails");
        const fake = { uid: "(loop " + L.uid + ")", phase: L.phase, loop_path: frames.slice(0, level + 1) };
        propagateStop(fake, null, frames, level, "loop " + (L.step_id || L.var) + " (max_iterations " + L.max_iterations + ")");
      }
    };

    const exitLoops = (fromFrames, toFrames) => {
      let common = 0;
      while (common < fromFrames.length && common < toFrames.length &&
             fromFrames[common].loop === toFrames[common].loop) common++;
      for (let level = fromFrames.length - 1; level >= common; level--) onLoopExit(fromFrames, level);
    };

    const runThen = (phase, why) => {
      for (const target of phase.then || []) runTarget(target, null, "then: of phase " + phase.id);
      log.push("[THEN] phase " + phase.id + " declared then: " + (phase.then || []).join(", ") + " - execution stops after it");
    };

    /* the top-level checks executeSteps() makes before each top-level step */
    const decideUnit = st => {
      const ph = phaseOf(st);
      if (run.stoppedAtPhase !== null) {
        if (run.stoppedAtPhase === st.phase) {
          return { state: "notrun", note: "not executed: phase " + ph.id + " stopped at " + run.stopAt };
        }
        const stopped = PH[run.stoppedAtPhase] || {};
        if (stopped.on_failure === "continue") {
          run.stoppedAtPhase = null; run.wasStopped = false;
        } else if (!run.onFailureRunTriggered && ph.post_failure) {
          /* a rollback phase still runs after a stop */
        } else {
          return { state: "notrun", note: "not executed: the run stopped at " + run.stopAt +
                   (run.onFailureRunTriggered ? " and an on_failure phase already ran"
                                              : "; " + ph.id + " is not a rollback phase") };
        }
      }
      if (run.currentPhase !== null && st.phase !== run.currentPhase) {
        const cur = PH[run.currentPhase] || {};
        if (run.phaseHadAnyFailure && cur.on_failure === "stop") {
          const at = run.phaseFailureAt;
          run.stoppedAtPhase = run.currentPhase;
          run.stopAt = "phase " + cur.id + " (on_failure: stop, failed at " + (at ? where(at) : "?") + ")";
          run.wasStopped = true;
          run.phaseHadAnyFailure = false; run.phaseFailureAt = null;
          log.push("[PHASE] " + cur.id + " had a failure and declares on_failure: stop - the run does not go on to " + ph.id);
          if (strict || !ph.post_failure) {
            return { state: "notrun", note: "not executed: " + run.stopAt +
                     (strict && ph.post_failure ? " - the engine also skips the first step of a rollback phase here" : "") };
          }
        } else {
          run.phaseHadAnyFailure = false; run.phaseFailureAt = null;
          if ((cur.then || []).length) {
            runThen(cur);
            run.onFailureRunTriggered = true;
            run.stoppedAtPhase = st.phase;
            run.stopAt = "then: of phase " + cur.id;
            run.currentPhase = null;
            return { state: "notrun", note: "not executed: phase " + cur.id + " declared then: - execution stops after it" };
          }
        }
      }
      if (ph.when && !evalCond(ph.when, vars)) {
        return { state: "skipped", note: "phase " + ph.id + " when is false: " + ph.when };
      }
      run.currentPhase = st.phase;
      return null;
    };

    for (const i of indices) {
      const st = STEPS[i];
      const frames = st.loop_path || [];
      exitLoops(prevFrames, frames);
      prevFrames = frames;
      const unit = frames.length ? frames[0].loop : st.uid;
      if (unit !== prevUnit) { prevUnit = unit; unitSkip = decideUnit(st); }
      const rt = fresh(st);
      rt.send = interpolate(st.send_raw, vars, rt.missing);
      rt.body = (st.rest && typeof st.rest.body_raw === "string") ? interpolate(st.rest.body_raw, vars) : null;
      rt.desc = interpolate(st.descriptionRaw || st.description_raw || "", vars) || st.description;

      const keep = handled[st.uid] && opts.main;
      const skipWith = (s, note) => {
        if (keep) return;                     // it already ran as an on_failure handler
        rt.state = s; rt.note = note;
        if (opts.rerun && results[st.uid] && STATE_EXECUTED.indexOf(results[st.uid].state) >= 0) return;
        results[st.uid] = rt;
      };

      if (unitSkip) { skipWith(unitSkip.state, unitSkip.note); continue; }

      /* loop frames: when/skip_when on first entry, the item, continue_when,
         break_when - and a loop a failure already ended */
      let blocked = null;
      for (const f of frames) {
        const L = LOOPS[f.loop];
        if (!L) continue;
        if (run.loopDone[L.uid]) { blocked = run.loopDone[L.uid]; break; }
        if (!run.loopEntered[L.uid]) {
          run.loopEntered[L.uid] = true;
          if (L.when && !evalCond(L.when, vars)) {
            run.loopDone[L.uid] = { state: "skipped", note: "loop " + (L.step_id || L.var) + " when is false: " + L.when };
            blocked = run.loopDone[L.uid]; break;
          }
          if (L.skip_when && String(L.skip_when).trim() && evalCond(L.skip_when, vars)) {
            run.loopDone[L.uid] = { state: "skipped", note: "loop " + (L.step_id || L.var) + " skip_when is true: " + L.skip_when };
            blocked = run.loopDone[L.uid]; break;
          }
        }
        vars[L.var] = (L.items || [])[f.index];
        if (run.iter[L.uid] !== f.index) {
          run.iter[L.uid] = f.index;
          delete run.iterSkip[L.uid];
          if (L.continue_when !== null && L.continue_when !== undefined && !evalCond(L.continue_when, vars)) {
            run.iterSkip[L.uid] = "continue_when is false for this item: " + L.continue_when;
          } else if (L.break_when !== null && L.break_when !== undefined && evalCond(L.break_when, vars)) {
            run.loopDone[L.uid] = { state: "skipped", note: "loop " + (L.step_id || L.var) + " broke: break_when is true" };
            blocked = run.loopDone[L.uid]; break;
          }
        }
        if (run.iterSkip[L.uid]) { blocked = { state: "skipped", note: run.iterSkip[L.uid] }; break; }
      }
      if (blocked) { skipWith(blocked.state, blocked.note); continue; }

      // the interpolation above ran before the loop items were bound
      rt.missing = [];
      rt.send = interpolate(st.send_raw, vars, rt.missing);
      rt.body = (st.rest && typeof st.rest.body_raw === "string") ? interpolate(st.rest.body_raw, vars) : null;
      rt.desc = interpolate(st.descriptionRaw || st.description_raw || "", vars) || st.description;

      const ph = phaseOf(st);
      if (ph.when && !evalCond(ph.when, vars)) { skipWith("skipped", "phase " + ph.id + " when is false: " + ph.when); continue; }
      if (!evalCond(st.when, vars, rt.trace)) { skipWith("skipped", "when is false: " + st.when); continue; }
      if (st.skip_when && String(st.skip_when).trim() && evalCond(st.skip_when, vars, rt.trace)) {
        skipWith("skipped", "skip_when is true: " + st.skip_when); continue;
      }

      if (handled[st.uid]) rt.notes.push("also ran earlier as the on_failure handler of " + handled[st.uid]);
      const prior = results[st.uid];
      if (opts.rerun && prior && STATE_EXECUTED.indexOf(prior.state) >= 0) {
        // a phase re-run by then: or on_failure - the first run stays on the card
        prior.rerun = rt;
      } else {
        results[st.uid] = rt;
      }
      const pass = judge(st, rt);
      logStep(st, rt);
      if (pass) continue;

      const continued = handleFailure(st, rt);
      if (continued) {
        markPhaseFailure(st, frames.length === 0);
        rt.routes.unshift("on_failure lets the run continue");
      } else if (frames.length) {
        propagateStop(st, rt, frames, frames.length - 1, where(st));
      } else {
        run.stoppedAtPhase = st.phase;
        run.stopAt = where(st);
        run.wasStopped = true;
        run.onFailureRunTriggered = onFailurePhases.size > 0;
        rt.routes.push("the rest of phase " + ph.id + " is not executed; later phases neither, " +
                       "except a rollback phase whose when is true");
      }
    }
    exitLoops(prevFrames, []);

    // the end of the top-level run: the last phase's on_failure: stop, then:
    if (opts.main && run.currentPhase !== null) {
      const cur = PH[run.currentPhase] || {};
      if (run.phaseHadAnyFailure && cur.on_failure === "stop") run.wasStopped = true;
      if ((cur.then || []).length) runThen(cur);
    }
    return run;
  }

  /* -- handleFailure() ------------------------------------------------- */
  function handleFailure(st, rt) {
    const of = st.on_failure || { mode: "stop" };
    if (of.mode === "continue" && !(of.run || []).length && !(of.then || []).length) return true;
    if (of.mode === "email") {
      rt.routes.push("on_failure: email - a failure mail goes out and the run stops");
      return false;
    }
    if ((of.run || []).length || (of.then || []).length || of.next || of.email) {
      for (const target of of.run || []) runTarget(target, st, "on_failure of " + where(st));
      for (const target of of.then || []) runTarget(target, st, "on_failure then: of " + where(st));
      if (of.email) rt.routes.push("a failure mail goes out");
      if (of.next === "continue") return true;
      if ((of.run || []).length) {
        rt.routes.push("no `next: continue` - the engine stops here even when the handlers succeed");
      }
      return false;
    }
    return false;
  }

  function runTarget(raw, failing, why) {
    const target = String(interpolate(String(raw), vars) || "").trim();
    if (!target) return;
    const rt = failing ? results[failing.uid] : null;
    let kind = null, name = target;
    if (/^step:/i.test(target)) { kind = "step"; name = target.slice(5).trim(); }
    else if (/^phase:/i.test(target)) { kind = "phase"; name = target.slice(6).trim(); }
    else kind = byStepId[target] ? "step" : "phase";
    if (kind === "step") return runHandler(name, failing, rt, why);
    const phase = PHASES.find(p => String(p.id).toLowerCase() === name.toLowerCase());
    if (!phase) {
      if (rt) rt.routes.push("phase " + name + " not found - the engine does nothing");
      return;
    }
    onFailurePhases.add(phase.id.toLowerCase());
    if (rt) rt.routes.push("runs phase " + phase.id + " now (" + why + ")");
    log.push("[PHASE] " + phase.id + " runs as " + why);
    const indices = [];
    STEPS.forEach((s, i) => { if (s.phase === phase.key) indices.push(i); });
    const before = new Set(indices.filter(i => results[STEPS[i].uid]).map(i => STEPS[i].uid));
    runSequence(indices, { rerun: true });
    for (const i of indices) {
      const uid = STEPS[i].uid;
      if (!before.has(uid)) handled[uid] = failing ? failing.uid : ("then: " + phase.id);
    }
  }

  /** executeOnFailureStep(): the handler runs in the failing step's context -
   *  the instance in the same loop iteration, else the last one defined. */
  function runHandler(id, failing, rt, why) {
    const candidates = byStepId[id] || [];
    if (!candidates.length) {
      if (rt) rt.routes.push("handler step " + id + " not found - the engine does nothing");
      return;
    }
    const mine = (failing && failing.loop_path) || [];
    let best = candidates[candidates.length - 1], bestScore = -1;
    for (const i of candidates) {
      const theirs = STEPS[i].loop_path || [];
      let score = 0;
      while (score < theirs.length && score < mine.length &&
             theirs[score].loop === mine[score].loop && theirs[score].index === mine[score].index) score++;
      if (score === theirs.length && score > bestScore) { best = i; bestScore = score; }
    }
    const h = STEPS[best];
    const hrt = fresh(h);
    hrt.handlerOf = failing ? failing.uid : null;
    hrt.send = interpolate(h.send_raw, vars, hrt.missing);
    hrt.body = (h.rest && typeof h.rest.body_raw === "string") ? interpolate(h.rest.body_raw, vars) : null;
    hrt.desc = interpolate(h.descriptionRaw || h.description_raw || "", vars) || h.description;
    handled[h.uid] = failing ? failing.uid : why;
    results[h.uid] = hrt;
    const ph = phaseOf(h);
    if (ph.when && !evalCond(ph.when, vars)) {
      hrt.state = "skipped"; hrt.note = "ran as a handler (" + why + ") but phase " + ph.id + " when is false";
    } else if (!evalCond(h.when, vars, hrt.trace)) {
      hrt.state = "skipped"; hrt.note = "ran as a handler (" + why + ") but its when is false: " + h.when;
    } else if (h.skip_when && String(h.skip_when).trim() && evalCond(h.skip_when, vars, hrt.trace)) {
      hrt.state = "skipped"; hrt.note = "ran as a handler (" + why + ") but its skip_when is true";
    } else {
      hrt.notes.push("ran as the on_failure handler of " + (failing ? where(failing) : why));
      const pass = judge(h, hrt);
      logStep(h, hrt);
      // the handler's own on_failure runs, but its verdict does not change
      // what the failing step does next - executeOnFailureStep() ignores it
      if (!pass) handleFailure(h, hrt);
    }
    if (rt) rt.routes.push("runs handler " + h.uid + " (" + id + "): " + labelOf(hrt.state));
  }

  function logStep(st, rt) {
    log.push("[STEP] " + st.uid + " " + (st.step_id || "") + " " + (st.node_target || "local") + " :: " +
             (rt.send || rt.desc || "") + " -> " + labelOf(rt.state) +
             (rt.messageText ? " :: " + rt.messageText : ""));
  }

  /* -- the run ---------------------------------------------------------- */
  const main = runSequence(STEPS.map((_s, i) => i), { main: true });

  const rt = {};
  for (const st of STEPS) {
    rt[st.uid] = results[st.uid] || { state: "notrun", note: "not reached", log: [], routes: [], notes: [] };
  }
  return { rt: rt, vars: vars, startVars: startVars, log: log,
           summary: summarise(doc, rt, main, vars), strict: strict };
}

function labelOf(state) {
  return ({ success: "SUCCESS", failure: "FAILURE", warning: "WARNING", skipped: "SKIPPED",
            notrun: "NOT EXECUTED", pending: "pending" })[state] || String(state || "").toUpperCase();
}

/** The counts, per phase and overall, and the execution summary variables
 *  ExecutionOrchestrator.applyExecutionSummaryVariables() sets at the end. */
function summarise(doc, rt, run, vars) {
  const counts = { success: 0, failure: 0, warning: 0, skipped: 0, notrun: 0, pending: 0 };
  const phases = {};
  const success = [], failure = [], skipped = [];
  let lastPhase = "";
  let postExecuted = false, postAll = true, configExecuted = false, configAll = true;
  const norm = s => String(s || "").trim().toLowerCase().replace(/[ _-]/g, "");
  const PH = {};
  for (const p of doc.phases || []) PH[p.key] = p;
  for (const st of doc.steps || []) {
    const r = rt[st.uid] || {};
    const s = r.state || "pending";
    counts[s] = (counts[s] || 0) + 1;
    const p = phases[st.phase] = phases[st.phase] ||
      { key: st.phase, id: (PH[st.phase] || {}).id || st.phase,
        success: 0, failure: 0, warning: 0, skipped: 0, notrun: 0, pending: 0 };
    p[s] = (p[s] || 0) + 1;
    const id = st.step_id || st.uid;
    if (s === "skipped" || s === "notrun") { if (skipped.indexOf(id) < 0) skipped.push(id); continue; }
    const javaPhase = (PH[st.phase] || {}).id || st.phase;
    lastPhase = javaPhase;
    const ok = s !== "failure";
    if (ok) { if (success.indexOf(id) < 0) success.push(id); }
    else if (failure.indexOf(id) < 0) failure.push(id);
    if (norm(javaPhase) === "activitypostcheck") { postExecuted = true; if (!ok) postAll = false; }
    if (norm(javaPhase) === "activityconfiguration") { configExecuted = true; if (!ok) configAll = false; }
  }
  const failed = run.wasStopped || String(vars.__HAS_CONTINUED_FAILURE) === "true";
  const status = counts.pending ? "INCOMPLETE" : (failed ? "FAIL" : "SUCCESS");
  const rollbackEnabled = postExecuted ? postAll : (configExecuted && configAll);
  return {
    counts: counts,
    phases: phases,
    status: status,
    stoppedAt: run.stopAt,
    engine: {
      ACTIVITY_EXECUTION_STATUS: failed ? "FAIL" : "SUCCESS",
      ACTIVITY_COMMAND_SUCCESS_LIST: success.join(","),
      ACTIVITY_COMMAND_FAILURE_LIST: failure.join(","),
      ACTIVITY_COMMAND_SKIPPED_LIST: skipped.join(","),
      LAST_ACTIVITY_EXECUTED: lastPhase,
      ROLLBACK_ENABLED: rollbackEnabled ? "TRUE" : "FALSE",
      ROLLBACK_REQUIRED: vars.ROLLBACK_REQUIRED === undefined ? "" : stringify(vars.ROLLBACK_REQUIRED),
    },
  };
}

/** Where a strict (engine-exact) run and the intended run part company:
 *  the first step whose verdict differs, with both verdicts. */
function divergence(doc, intended, strictRun) {
  for (const st of doc.steps || []) {
    const a = (intended.rt[st.uid] || {}).state, b = (strictRun.rt[st.uid] || {}).state;
    const ran = s => s === "success" || s === "failure" || s === "warning" || s === "pending";
    if (ran(a) !== ran(b)) return { uid: st.uid, phase: st.phase, intended: a, engine: b };
  }
  return null;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { simulate, summarise, divergence, labelOf };
}

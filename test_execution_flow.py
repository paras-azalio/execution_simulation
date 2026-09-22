#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Tests for the execution_flow MOP generator.

    python -m unittest test_execution_flow

The engine tests pin the quirks this port exists to reproduce. If one of them
starts failing because the port was "cleaned up", the generated MOP no longer
predicts what the node is really sent - that is the whole point of the module,
so each test says which java line it mirrors.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ciq as CIQ                                          # noqa: E402
import engine as E                                         # noqa: E402
import synth as S                                          # noqa: E402
import ciqgen                                              # noqa: E402
import runall                                              # noqa: E402
from expander import (CHECKLIST, INTERACTIVE, Expander,     # noqa: E402
                      implied_values, render_mode_for)

MOPGEN = os.path.join(HERE, "mopgen.py")

# The workflows and their mappings live in the CLICR repo; the sample order for
# 1051 is the one real CIQ in this checkout.
TEMPLATES = os.path.abspath(os.path.join(
    HERE, "..", "JAVA_NOKIA_CLICR_AUTOMATION", "src", "main", "resources", "templates"))
YAML_DIR = os.path.join(TEMPLATES, "yaml")
MAPPING_DIR = os.path.join(TEMPLATES, "jsonTemplate")
MASTER = os.path.join(YAML_DIR, "PGW_RDS_1051_SUBSCRIBER_PROFILE_CONFIGURATION.yaml")
MAPPING = os.path.join(MAPPING_DIR,
                       "PGW_RDS_1051_SUBSCRIBER_PROFILE_CONFIGURATION_json-output.yaml")
SAMPLE_CIQ = os.path.abspath(os.path.join(
    HERE, "..", "version", "PGW_RDS_1051_SUBSCRIBER_PROFILE_CONFIGURATION.json"))


# --------------------------------------------------------------------------- #
#  engine: interpolation
# --------------------------------------------------------------------------- #
class Interpolation(unittest.TestCase):

    def ctx(self, **vars):
        return E.Context(vars, {"pw": "s3cret"}, {"SSH_KEY_PATH": "/k/id_rsa"})

    def test_unresolved_becomes_empty_string(self):
        """ExecutionContext.java:120-133 - NOT left as ${...}, NOT an error."""
        c = self.ctx()
        self.assertEqual(c.interpolate("a=${nope}b"), "a=b")

    def test_unresolved_is_recorded_for_the_page(self):
        c = self.ctx()
        c.interpolate("${nope}")
        self.assertEqual([u.token for u in c.unresolved], ["nope"])

    def test_env_and_secret(self):
        c = self.ctx()
        self.assertEqual(c.interpolate("${ENV.SSH_KEY_PATH}"), "/k/id_rsa")
        self.assertEqual(c.interpolate("${SECRET.pw}"), "s3cret")

    def test_base64(self):
        c = self.ctx(X="hi")
        self.assertEqual(c.interpolate("${base64:X}"), "aGk=")

    def test_dotted_path_with_a_space_in_the_key(self):
        c = self.ctx(rec_row={"data": {"Test IMSI": "404960000000112"}})
        self.assertEqual(c.interpolate("${rec_row.data.Test IMSI}"), "404960000000112")

    def test_list_index(self):
        c = self.ctx(hosts=["10.0.0.1", "10.0.0.2"])
        self.assertEqual(c.interpolate("${hosts[1]}"), "10.0.0.2")

    def test_nested_placeholder_cannot_work(self):
        """The pattern stops at the first '}', as the 1051 YAML comment says."""
        c = self.ctx(imsi="112", LDAPFIELDS112="x")
        self.assertEqual(c.interpolate("${LDAPFIELDS${imsi}}"), "}")


# --------------------------------------------------------------------------- #
#  engine: conditions
# --------------------------------------------------------------------------- #
class Conditions(unittest.TestCase):

    def ctx(self, **vars):
        return E.Context(vars)

    def test_empty_expression_is_true(self):
        self.assertTrue(self.ctx().evaluate(""))
        self.assertTrue(self.ctx().evaluate(None))

    def test_equality_is_case_sensitive(self):
        """ExecutionContext.java:646-654 uses String.equals()."""
        c = self.ctx(A="ENABLE")
        self.assertTrue(c.evaluate('${A == "ENABLE"}'))
        self.assertFalse(c.evaluate('${A == "enable"}'))

    def test_or_splits_before_and(self):
        c = self.ctx(A="1", B="0", C="1", D="1")
        self.assertTrue(c.evaluate('${A == "1" && B == "1" || C == "1" && D == "1"}'))

    def test_parentheses_are_not_supported(self):
        """splitTopLevel() tracks quotes only - '(' lands inside a name."""
        c = self.ctx(A="1", B="1")
        self.assertFalse(c.evaluate('${(A == "1" || B == "9") && A == "1"}'))

    def test_contains_and_startswith(self):
        c = self.ctx(LIST="112,113", NAME="niam_rds_1")
        self.assertTrue(c.evaluate('${LIST contains "113"}'))
        self.assertTrue(c.evaluate('${NAME startsWith "niam"}'))
        self.assertTrue(c.evaluate('${LIST notContains "999"}'))

    def test_numeric_comparison(self):
        c = self.ctx(N="9")
        self.assertTrue(c.evaluate("${N >= 8}"))
        self.assertFalse(c.evaluate("${N >= 10}"))

    def test_unset_compares_as_empty(self):
        self.assertTrue(self.ctx().evaluate('${MISSING == ""}'))

    def test_gt_inside_quotes_is_a_literal(self):
        c = self.ctx(V="<unset>")
        self.assertTrue(c.evaluate('${V == "<unset>"}'))


# --------------------------------------------------------------------------- #
#  engine: for_each and registers
# --------------------------------------------------------------------------- #
class ForEachAndRegisters(unittest.TestCase):

    def test_bare_placeholder_yields_the_list_object(self):
        c = E.Context(nodeGroups=[{"nodeGroup": "North1"}]) \
            if False else E.Context({"nodeGroups": [{"nodeGroup": "North1"}]})
        self.assertEqual(c.resolve_for_each("${nodeGroups}"), [{"nodeGroup": "North1"}])

    def test_yaml_list_literal_iterates(self):
        """for_each: ["REMOVE", "ENABLE"] - StepDefinition.for_each is Object."""
        c = E.Context({})
        self.assertEqual(c.resolve_for_each(["REMOVE", "ENABLE"]), ["REMOVE", "ENABLE"])

    def test_non_iterable_becomes_one_iteration(self):
        """The "[1]" wrapper idiom: toIterable() wraps a scalar."""
        c = E.Context({})
        self.assertEqual(len(c.resolve_for_each("[1]")), 1)

    def test_named_group_register_in_either_spelling(self):
        for pattern in (r"(?<X>OK)", r"(?P<X>OK)"):
            c = E.Context({})
            E.apply_registers(c, [{"regex": pattern}], "value is OK here")
            self.assertEqual(c.get("X"), "OK", pattern)

    def test_loop_register_counts(self):
        """(?m) is required for ^/$ to see line boundaries, exactly as in java -
        every loop register in the CLICR workflows carries it."""
        c = E.Context({})
        E.apply_registers(c, [{"regex": r"(?m)^line \d$", "loop": True,
                               "count_var": "N"}], "line 1\nline 2\nline 3")
        self.assertEqual(c.get("N"), "3")

    def test_loop_register_without_multiline_finds_nothing(self):
        """Not a bug to fix: java's Pattern behaves the same way."""
        c = E.Context({})
        E.apply_registers(c, [{"regex": r"^line \d$", "loop": True,
                               "count_var": "N"}], "line 1\nline 2")
        self.assertEqual(c.get("N"), "0")

    def test_register_when_gates_the_set(self):
        c = E.Context({"LOGIN": "no"})
        E.apply_registers(c, [{"name": "T", "when": '${LOGIN == "yes"}', "value": "x"}], "")
        self.assertIsNone(c.get("T"))


# --------------------------------------------------------------------------- #
#  criteria -> implied values -> synthesised output
# --------------------------------------------------------------------------- #
class Synthesis(unittest.TestCase):

    def ctx(self):
        return E.Context({})

    def test_implied_equality(self):
        got = implied_values({"expr": '${IMSIRESULT == "success"}'}, self.ctx())
        self.assertEqual(got, {"IMSIRESULT": "success"})

    def test_implied_and_chain(self):
        got = implied_values(
            {"expr": '${APACHESTATUS == "running" && INSTANCESTATUS == "running"}'}, self.ctx())
        self.assertEqual(got, {"APACHESTATUS": "running", "INSTANCESTATUS": "running"})

    def test_implied_threshold(self):
        self.assertEqual(implied_values({"expr": "${IDLE_OK_COUNT >= 8}"}, self.ctx()),
                         {"IDLE_OK_COUNT": "8"})

    def test_implied_takes_the_first_or_clause(self):
        got = implied_values({"expr": '${XMLEXISTS == "true" || TABLEIMSICOUNT == "0"}'},
                             self.ctx())
        self.assertEqual(got, {"XMLEXISTS": "true"})

    def test_not_equal_implies_nothing(self):
        """"must be non-empty" says nothing about WHAT - stays blank by design."""
        self.assertEqual(implied_values({"expr": '${LDAPFIELDS != ""}'}, self.ctx()), {})

    def test_sampler_builds_a_matching_line(self):
        out = S.sample_for(r"(?m)^XML_EXISTS=(?<XMLEXISTS>true|false)",
                           {"XMLEXISTS": "true"})
        self.assertEqual(out, "XML_EXISTS=true")

    def test_sampler_handles_the_provgw_pattern(self):
        out = S.sample_for(r"(?m)apache\s+\.\.\.\s+(?<APACHESTATUS>running)",
                           {"APACHESTATUS": "running"})
        self.assertEqual(out, "apache ... running")

    def test_success_output_feeds_its_own_registers(self):
        """The whole contract: the synthesised words must satisfy the criteria."""
        register = [{"name": "XMLEXISTS", "value": "false"},
                    {"regex": r"(?m)^XML_EXISTS=(?<XMLEXISTS>true|false)"}]
        criteria = {"expr": '${XMLEXISTS == "true"}'}
        implied = implied_values(criteria, self.ctx())
        output = S.success_output(register, criteria, implied)
        c = E.Context({})
        E.apply_registers(c, register, output)
        self.assertEqual(c.get("XMLEXISTS"), "true")
        self.assertTrue(c.evaluate(criteria["expr"]))

    def test_loop_output_repeats_to_reach_the_count(self):
        register = [{"name": "N", "value": "0"},
                    {"regex": r"(?m)^sample \d$", "loop": True, "count_var": "N"}]
        criteria = {"expr": "${N >= 8}"}
        implied = implied_values(criteria, self.ctx())
        output = S.success_output(register, criteria, implied, counts=implied)
        c = E.Context({})
        E.apply_registers(c, register, output)
        self.assertEqual(c.get("N"), "8")
        self.assertTrue(c.evaluate(criteria["expr"]))

    def test_failure_output_violates_the_criteria(self):
        register = [{"regex": r'result="(?<IMSIRESULT>[^"]+)"'}]
        criteria = {"expr": '${IMSIRESULT == "success"}'}
        implied = implied_values(criteria, self.ctx())
        output = S.failure_output(register, criteria, implied)
        c = E.Context({})
        E.apply_registers(c, register, output)
        self.assertFalse(c.evaluate(criteria["expr"]))


# --------------------------------------------------------------------------- #
#  phase classification
# --------------------------------------------------------------------------- #
class RenderMode(unittest.TestCase):

    def test_health_checks_stay_a_checklist(self):
        self.assertEqual(render_mode_for("preNodeHealthCheck", "PRE_NODE_HEALTH_CHECK"),
                         CHECKLIST)
        self.assertEqual(render_mode_for("postNodeHealthCheck", "POST_CHECK"), CHECKLIST)

    def test_activity_and_rollback_are_interactive(self):
        self.assertEqual(render_mode_for("activity_configuration",
                                         "ACTIVITY_CONFIGURATION"), INTERACTIVE)
        self.assertEqual(render_mode_for("rollback", "ROLLBACK"), INTERACTIVE)


# --------------------------------------------------------------------------- #
#  expander against the real 1051 workflow
# --------------------------------------------------------------------------- #
@unittest.skipUnless(os.path.isfile(MASTER) and os.path.isfile(SAMPLE_CIQ),
                     "the 1051 master workflow / sample CIQ is not in this checkout")
class RealWorkflow(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.workflow = CIQ.read_yaml(MASTER)
        cls.data = CIQ.load_ciq(SAMPLE_CIQ)
        cls.params = CIQ.load_params()
        node = cls.data["nodes"][0]
        cls.expansion = Expander(cls.workflow, cls.data, cls.params).expand(node)
        cls.steps = cls.expansion.steps

    def by_uid(self, uid):
        return next(s for s in self.steps if s.uid == uid)

    def test_ciq_is_normalised_into_flat_nodes(self):
        self.assertTrue(self.data["nodes"])
        self.assertEqual(self.data["nodes"][0]["nodeGroup"], "North1")
        self.assertIn("configSequences", self.data["nodes"][0])
        self.assertEqual(self.data.get("activity"), "1051_SUBSCRIBER_PROFILE_CONFIGURATION")

    def test_it_produces_steps_for_every_phase(self):
        phases = set(s.phase for s in self.steps)
        self.assertIn("preNodeHealthCheck", phases)
        self.assertIn("activity_configuration", phases)
        self.assertIn("postNodeHealthCheck", phases)

    def test_absent_skip_when_does_not_skip(self):
        """REGRESSION: an empty gate defaults TRUE, which is wrong for skip_when
        and silently stopped every register from being applied."""
        self.assertTrue(all(s.skip_state is False
                            for s in self.steps if not s.skip_when))

    def test_login_detection_resolves_to_the_first_node(self):
        """niamID-1 answers, so 2 and 3 are skipped - as the engine would."""
        self.assertEqual(self.by_uid("s0002").node_target, "niam_rds_1")
        self.assertIs(self.by_uid("s0003").when_state, False)
        self.assertIs(self.by_uid("s0004").when_state, False)

    def test_later_steps_target_the_detected_node(self):
        targets = set(s.node_target for s in self.steps
                      if s.phase == "postNodeHealthCheck" and s.node_target)
        self.assertTrue(targets.issubset({"niam_rds_1", "niam_pgw_1"}), targets)

    def test_the_imsi_reaches_the_ldapsearch(self):
        imsi = self.data["nodes"][0]["configSequences"][0]["tables"][1]["records"][0]["data"]["Test IMSI"]
        hits = [s for s in self.steps if s.send and "ldapsearch" in s.send and imsi in s.send]
        self.assertTrue(hits, "no ldapsearch carried the CIQ IMSI")

    def test_every_step_has_both_synthesised_outputs_or_is_exit_code_only(self):
        for step in self.steps:
            if not step.validation.get("enabled"):
                continue
            self.assertIsNotNone(step.success_output, step.uid)
            self.assertIsNotNone(step.failure_output, step.uid)

    def _captured_names(self, step):
        """The register group names this step's own output can fill."""
        names = set()
        for entry in step.register or []:
            if isinstance(entry, dict) and entry.get("regex"):
                try:
                    names |= set(__import__("re").compile(
                        E.to_java_regex(entry["regex"])).groupindex)
                except Exception:
                    pass
                if entry.get("count_var"):
                    names.add(entry["count_var"])
            elif isinstance(entry, dict) and entry.get("name"):
                names.add(entry["name"])
        return names

    def test_success_output_satisfies_the_criteria_it_came_from(self):
        """
        THE contract. Clicking Success feeds these words through the step's own
        registers and then its criteria; if the words do not pass, the page
        would report FAILURE on a step the operator just called successful.

        Only checked where every value the criteria assert is one this step's
        own registers can capture - otherwise the value belongs to another step
        and is legitimately not derivable here.
        """
        checked = 0
        for step in self.steps:
            criteria = (step.validation or {}).get("successCriteria") or {}
            if not criteria.get("expr"):
                continue
            implied = step.implied or {}
            if not implied or not set(implied) <= self._captured_names(step):
                continue
            context = E.Context({})
            E.apply_registers(context, step.register, step.success_output or "")
            passed = context.evaluate(criteria["expr"])
            detail = "%s criteria=%r implied=%r output=%r" % (
                step.uid, criteria["expr"], implied, step.success_output)
            self.assertTrue(passed, "success output fails its own criteria: " + detail)
            checked += 1
        self.assertGreater(checked, 5, "the invariant covered almost nothing")

    def test_failure_output_fails_the_criteria(self):
        checked = 0
        for step in self.steps:
            criteria = (step.validation or {}).get("successCriteria") or {}
            if not criteria.get("expr"):
                continue
            implied = dict((k, v) for k, v in (step.implied or {}).items() if v != "")
            if not implied or not set(implied) <= self._captured_names(step):
                continue
            context = E.Context({})
            E.apply_registers(context, step.register, step.failure_output or "")
            self.assertFalse(context.evaluate(criteria["expr"]),
                             "failure output still passes: " + step.uid)
            checked += 1
        self.assertGreater(checked, 3)

    def test_unresolved_tokens_are_ciq_gaps_only(self):
        """
        Whatever is still unresolved must be a CIQ field this sample does not
        carry - never an engine variable an earlier step should have produced.
        """
        tokens = set(u.token for s in self.steps for u in s.unresolved or [])
        for token in tokens:
            self.assertTrue(token.startswith("rec_row.data."),
                            "unresolved engine variable: %s" % token)


# --------------------------------------------------------------------------- #
#  the CLI
# --------------------------------------------------------------------------- #
@unittest.skipUnless(os.path.isfile(MASTER) and os.path.isfile(SAMPLE_CIQ),
                     "the 1051 master workflow / sample CIQ is not in this checkout")
class Cli(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="execflow_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *extra):
        proc = subprocess.Popen(
            [sys.executable, MOPGEN, "--yaml", MASTER, "--ciq", SAMPLE_CIQ,
             "--out", self.tmp] + list(extra),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=HERE)
        out, _ = proc.communicate()
        return proc.returncode, out.decode("utf-8", "replace")

    def test_writes_one_html_per_node(self):
        rc, out = self.run_cli()
        self.assertEqual(rc, 0, out)
        files = sorted(f for f in os.listdir(self.tmp) if f.endswith(".html"))
        self.assertEqual(len(files), 2, files)
        self.assertTrue(any("North1" in f for f in files))
        self.assertTrue(any("South1" in f for f in files))

    def test_node_filter(self):
        rc, out = self.run_cli("--node", "South1")
        self.assertEqual(rc, 0, out)
        files = [f for f in os.listdir(self.tmp) if f.endswith(".html")]
        self.assertEqual(len(files), 1, files)
        self.assertIn("South1", files[0])

    def test_page_is_self_contained_and_carries_the_step_payload(self):
        self.run_cli()
        path = [f for f in os.listdir(self.tmp) if f.endswith(".html")][0]
        html = io.open(os.path.join(self.tmp, path), encoding="utf-8").read()
        self.assertNotIn("{{", html)
        self.assertNotIn("<script src", html)
        self.assertIn("const STEPS", html)
        self.assertIn("Custom input", html)

    def test_param_override_reaches_the_commands(self):
        self.run_cli("--param", "ORDER_NO=99999", "--json")
        path = [f for f in os.listdir(self.tmp) if f.endswith(".json")][0]
        doc = json.load(io.open(os.path.join(self.tmp, path), encoding="utf-8"))
        self.assertEqual(doc["params"]["ORDER_NO"], "99999")
        self.assertTrue(any("99999" in (s["send"] or "") for s in doc["steps"]),
                        "the order number never reached a command")

    def test_unknown_node_is_a_clear_error(self):
        rc, out = self.run_cli("--node", "Nowhere")
        self.assertEqual(rc, 1)
        self.assertIn("none of --node", out)
        self.assertNotIn("Traceback", out)


# --------------------------------------------------------------------------- #
#  the browser engine
# --------------------------------------------------------------------------- #
class BrowserEngineParity(unittest.TestCase):
    """
    template/engine.js re-resolves every command when the operator supplies a
    Custom output, so it has to agree with engine.py. test_engine_js.js runs the
    same vectors through it; this just makes `python -m unittest` fail too.
    """

    def test_engine_js_parity_suite(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        script = os.path.join(HERE, "test_engine_js.js")
        self.assertTrue(os.path.isfile(script), script)
        proc = subprocess.Popen([node, script], cwd=HERE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = proc.communicate()
        out = out.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("0 failed", out)



# --------------------------------------------------------------------------- #
#  the json-output mapping, read as a language
# --------------------------------------------------------------------------- #
@unittest.skipUnless(os.path.isdir(MAPPING_DIR),
                     "the CLICR mappings are not in this checkout")
class MappingLanguage(unittest.TestCase):

    def test_a_column_name_may_contain_spaces(self):
        """REGRESSION: "Index.Report Email" was read as the column `Report`,
        and a value was then synthesised for the wrong name."""
        cells = CIQ.OutputTemplate._cells_in("Index.Report Email WHERE Index.ZONE = $zone")
        self.assertIn(("Index", "Report Email"), cells)

    def test_a_query_keyword_never_becomes_part_of_a_column(self):
        cells = CIQ.OutputTemplate._cells_in("IP.NIAM_NAME1 WHERE IP.ZONE = $zone")
        self.assertIn(("IP", "NIAM_NAME1"), cells)

    def test_a_quoted_column(self):
        self.assertEqual(CIQ.OutputTemplate._cells_in("IP.'NIAM NAME'")[0],
                         ("IP", "NIAM NAME"))

    def test_row_wildcard_is_recognised(self):
        self.assertTrue(CIQ.load_output_template(MAPPING).copies_whole_rows)

    def test_a_wildcard_mapping_cannot_rule_a_column_out(self):
        """`_row: "*"` copies whatever the workbook had, so a missing column is
        an order gap, not a document defect - the MOP must not claim otherwise."""
        template = CIQ.load_output_template(MAPPING)
        self.assertIsNone(template.emits("Call Type"))
        self.assertIn("copies whole rows", template.explain("Call Type"))

    def test_an_explicit_mapping_does_rule_a_column_out(self):
        mrf = os.path.join(MAPPING_DIR, "MRF_ANNOUNCEMENT_LOADING_json-output.yaml")
        if not os.path.isfile(mrf):
            self.skipTest("the MRF mapping is not in this checkout")
        template = CIQ.load_output_template(mrf)
        self.assertTrue(template.emits("INPUT_FILE"))
        self.assertFalse(template.emits("Forwarded-To Number"))
        self.assertIn("never fills it", template.explain("Forwarded-To Number"))

    def test_explain_names_the_mapping_file(self):
        template = CIQ.load_output_template(MAPPING)
        self.assertIn(os.path.basename(MAPPING), template.explain("Call Type"))

    def test_no_mapping_means_no_opinion(self):
        template = CIQ.load_output_template("")
        self.assertIsNone(template.emits("anything"))
        self.assertIsNone(template.explain("anything"))


@unittest.skipUnless(os.path.isfile(MASTER) and os.path.isfile(MAPPING)
                     and os.path.isfile(SAMPLE_CIQ),
                     "the 1051 inputs are not in this checkout")
class UnresolvedBlamesTheMapping(unittest.TestCase):
    """A blank in a command has to say which of the two things went wrong."""

    @classmethod
    def setUpClass(cls):
        workflow = CIQ.read_yaml(MASTER)
        data = CIQ.load_ciq(SAMPLE_CIQ)
        template = CIQ.load_output_template(MAPPING)
        cls.expansion = Expander(workflow, data, CIQ.load_params(),
                                 template).expand(data["nodes"][0])

    def reasons(self):
        return [u.reason for s in self.expansion.steps for u in s.unresolved or []]

    def test_a_ciq_reference_carries_the_mapping_verdict(self):
        reasons = self.reasons()
        self.assertTrue(reasons, "nothing was unresolved, so nothing to explain")
        self.assertTrue(all(os.path.basename(MAPPING) in r for r in reasons),
                        reasons[:2])

    def test_the_engine_reason_is_kept_as_well(self):
        self.assertTrue(any("has no member" in r for r in self.reasons()),
                        self.reasons()[:2])


# --------------------------------------------------------------------------- #
#  synthesising a CIQ
# --------------------------------------------------------------------------- #
@unittest.skipUnless(os.path.isfile(MASTER) and os.path.isfile(MAPPING),
                     "the 1051 inputs are not in this checkout")
class SynthesisedCiq(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.workflow = CIQ.read_yaml(MASTER)
        cls.template = CIQ.load_output_template(MAPPING)
        cls.demand = ciqgen.demand_of(cls.workflow)
        cls.data = CIQ.normalize_data_section(
            ciqgen.generate(cls.template, cls.workflow, groups=2, rows=2))

    def tables(self):
        return self.data["nodes"][0]["configSequences"][0]["tables"]

    def test_it_harvests_the_literals_the_workflow_compares_against(self):
        """REGRESSION: a workflow closes the placeholder before the operator
        (`${x.y} == "z"`), so a pattern that did not allow the `}` harvested
        nothing at all and every value was a made-up one."""
        self.assertIn("RSI", self.demand.literals("table"))
        self.assertIn("Call Barring", self.demand.literals("table"))

    def test_it_learns_the_record_columns_from_the_workflow(self):
        self.assertIn("Test IMSI", self.demand.columns)
        self.assertIn("Action", self.demand.columns)

    def test_the_result_normalises_like_a_real_ciq(self):
        self.assertEqual(len(self.data.get("nodes") or []), 2)
        self.assertTrue(self.data["nodes"][0].get("configSequences"))

    def test_the_records_carry_every_column_the_workflow_reads(self):
        record = self.tables()[0]["records"][0]
        for column in self.demand.columns:
            self.assertIn(column, record["data"], column)

    def test_a_table_name_is_a_name_the_workflow_tests_for(self):
        """A made-up table name falls through every
        `${table_row.table} == "..."` and documents nothing."""
        names = [t["table"] for t in self.tables()]
        self.assertTrue(set(names) & set(self.demand.literals("table")), names)

    def test_a_literal_in_the_mapping_stays_a_literal(self):
        """REGRESSION: `table: RSI` was read as a column reference and
        replaced with a synthesised value."""
        self.assertNotIn("RSI_1", [t["table"] for t in self.tables()])

    def test_a_value_is_chosen_for_the_whole_column_name(self):
        """REGRESSION: "Report Email" contains the letters PORT, and the email
        address came out as 5060."""
        self.assertIn("@", self.data["nodes"][0]["email"])

    def test_the_document_says_the_order_is_synthetic(self):
        self.assertIn("not a real CIQ", self.data["meta"]["generatedBy"])

    def test_a_field_that_repeats_its_own_name_is_a_column(self):
        """`INPUT_FILE: INPUT_FILE` is the mapping's idiom for "this column as
        it is", and emitting the literal "INPUT_FILE" put that string into the
        command instead of a file name."""
        mrf = os.path.join(MAPPING_DIR, "MRF_ANNOUNCEMENT_LOADING_json-output.yaml")
        if not os.path.isfile(mrf):
            self.skipTest("the MRF mapping is not in this checkout")
        data = ciqgen.generate(CIQ.load_output_template(mrf), None)
        entry = data["nodes"][0]["configData"][0]
        self.assertNotEqual(entry["INPUT_FILE"], "INPUT_FILE")

    def test_a_workflow_with_no_mapping_still_gets_a_node(self):
        data = CIQ.normalize_data_section(ciqgen.minimal(self.workflow, groups=2))
        self.assertEqual(len(data["nodes"]), 2)
        self.assertIn("no json-output mapping", data["meta"]["generatedBy"])


# --------------------------------------------------------------------------- #
#  every workflow in the repo
# --------------------------------------------------------------------------- #
@unittest.skipUnless(os.path.isdir(YAML_DIR),
                     "the CLICR workflows are not in this checkout")
class EveryWorkflowExpands(unittest.TestCase):
    """
    The generator has to cope with all of them, not just 1051. The families
    differ in shape - nodeGroups against a flat node list, configSequences
    against configData - and a change that suits one can silently empty
    another's document.
    """

    def workflows(self):
        return sorted(f for f in os.listdir(YAML_DIR) if f.endswith(".yaml"))

    def expand(self, name):
        stem = os.path.splitext(name)[0]
        workflow = CIQ.read_yaml(os.path.join(YAML_DIR, name))
        mapping = runall.find_mapping(stem, MAPPING_DIR)
        template = CIQ.load_output_template(mapping or "")
        data = (ciqgen.generate(template, workflow) if mapping
                else ciqgen.minimal(workflow))
        data = CIQ.normalize_data_section(data)
        self.assertTrue(data.get("nodes"), "%s: no node to document" % stem)
        return Expander(workflow, data, CIQ.load_params(), template).expand(
            data["nodes"][0])

    def test_there_are_workflows_to_check(self):
        self.assertTrue(self.workflows())

    def test_each_one_expands_to_a_document(self):
        empty = []
        for name in self.workflows():
            if not self.expand(name).steps:
                empty.append(name)
        self.assertEqual(empty, [], "these expanded to no steps at all")

    def test_the_mrf_activity_loops_over_the_nodes_own_data(self):
        """
        REGRESSION: `configData` hangs off the NODE, not off the root of the
        CIQ, so until the node's own fields were promoted into the context
        `for_each: "${configData}"` resolved to nothing, and the MRF documents
        came out with the health checks and no activity at all.
        """
        name = "MRF_ANNOUNCEMENT_LOADING.yaml"
        if not os.path.isfile(os.path.join(YAML_DIR, name)):
            self.skipTest("%s is not in this checkout" % name)
        phases = set(s.phase for s in self.expand(name).steps)
        self.assertIn("activity_configuration", phases,
                      "the activity phase is empty: ${configData} resolved to nothing")


@unittest.skipUnless(os.path.isdir(MAPPING_DIR),
                     "the CLICR mappings are not in this checkout")
class MappingPairing(unittest.TestCase):

    def test_a_variant_falls_back_to_the_base_activity(self):
        self.assertEqual(
            runall.base_names("MRF_24.7_ANNOUNCEMENT_LOADING_local")[-1],
            "MRF_24.7_ANNOUNCEMENT_LOADING")

    def test_an_activity_finds_its_own_mapping(self):
        found = runall.find_mapping("PGW_RDS_1051_SUBSCRIBER_PROFILE_CONFIGURATION",
                                    MAPPING_DIR)
        self.assertTrue(found, "no mapping found")
        self.assertTrue(found.endswith(
            "PGW_RDS_1051_SUBSCRIBER_PROFILE_CONFIGURATION_json-output.yaml"), found)

    def test_a_workflow_with_no_mapping_reports_none(self):
        self.assertIsNone(runall.find_mapping("NOT_AN_ACTIVITY_AT_ALL", MAPPING_DIR))


# --------------------------------------------------------------------------- #
#  the page, in a real DOM
# --------------------------------------------------------------------------- #
@unittest.skipUnless(os.path.isfile(MASTER) and os.path.isfile(SAMPLE_CIQ),
                     "the 1051 inputs are not in this checkout")
class PageDom(unittest.TestCase):
    """
    BrowserEngineParity proves the page's ENGINE matches engine.py. It says
    nothing about the page itself: whether a button is wired, whether a Failure
    on an `on_failure: stop` step marks the rest NOT EXECUTED, whether a reload
    resumes, whether Export produces parseable JSON. test_page_dom.js drives
    the real DOM through jsdom and answers those.
    """

    def test_page_dom_suite(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        script = os.path.join(HERE, "test_page_dom.js")
        self.assertTrue(os.path.isfile(script), script)

        tmp = tempfile.mkdtemp(prefix="execflow_dom_")
        try:
            gen = subprocess.Popen(
                [sys.executable, MOPGEN, "--yaml", MASTER, "--ciq", SAMPLE_CIQ,
                 "--json-template", MAPPING, "--out", tmp, "--quiet"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=HERE)
            out, _ = gen.communicate()
            self.assertEqual(gen.returncode, 0, out.decode("utf-8", "replace"))
            pages = sorted(f for f in os.listdir(tmp) if f.endswith(".html"))
            self.assertTrue(pages, "no page was generated")

            proc = subprocess.Popen([node, script, os.path.join(tmp, pages[0])],
                                    cwd=HERE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT)
            out, _ = proc.communicate()
            text = out.decode("utf-8", "replace")
            if "jsdom is not installed" in text:
                self.skipTest("jsdom is not installed (npm install)")
            self.assertEqual(proc.returncode, 0, text)
            self.assertIn("0 failed", text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)



# --------------------------------------------------------------------------- #
#  the batch runner, end to end
# --------------------------------------------------------------------------- #
@unittest.skipUnless(os.path.isdir(YAML_DIR) and os.path.isdir(MAPPING_DIR),
                     "the CLICR templates are not in this checkout")
class Batch(unittest.TestCase):
    """
    runall.py on a couple of activities, then the page suite on what it wrote.

    The two chosen are deliberately unalike: MRF has no mapping-level records
    and loops over the node's own configData, TEST_EMAIL has no order data at
    all and renders two checklist rows. Between them they cover the shapes that
    used to come out empty.
    """

    ACTIVITIES = ["MRF_ANNOUNCEMENT_LOADING", "TEST_EMAIL"]

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="execflow_batch_")
        argv = ["--out", cls.tmp, "--quiet"]
        for name in cls.ACTIVITIES:
            argv += ["--only", name]
        cls.rc = runall.main(argv)
        with io.open(os.path.join(cls.tmp, "index.json"), encoding="utf-8") as handle:
            cls.index = json.load(handle)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_selected_workflow_expanded(self):
        bad = [a["activity"] for a in self.index["activities"] if not a["ok"]]
        self.assertEqual(bad, [], "these produced no document")
        self.assertEqual(self.rc, 0)

    def test_it_writes_an_index(self):
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "index.html")))
        self.assertTrue(self.index["activities"])

    def test_a_synthesised_order_is_kept_and_labelled(self):
        synthetic = [a for a in self.index["activities"] if a["ciqSynthetic"]]
        self.assertTrue(synthetic, "nothing was synthesised, so nothing to check")
        for activity in synthetic:
            path = os.path.join(self.tmp, "_ciq", activity["activity"] + ".json")
            self.assertTrue(os.path.isfile(path), path)
            with io.open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertIn("not a real CIQ", data["meta"]["generatedBy"])

    def test_every_document_is_self_contained(self):
        for activity in self.index["activities"]:
            for node in activity["nodes"]:
                path = os.path.join(self.tmp, activity["activity"], node["html"])
                self.assertTrue(os.path.isfile(path), path)
                with io.open(path, encoding="utf-8") as handle:
                    html = handle.read()
                self.assertNotIn("{{", html)
                self.assertNotIn("<script src", html)

    def test_the_pages_work_in_a_dom(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        proc = subprocess.Popen(
            [node, os.path.join(HERE, "test_page_dom.js"), "--all", self.tmp],
            cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = proc.communicate()
        text = out.decode("utf-8", "replace")
        if "jsdom is not installed" in text:
            self.skipTest("jsdom is not installed (npm install)")
        self.assertEqual(proc.returncode, 0, text)
        self.assertIn("0 failed", text)



# --------------------------------------------------------------------------- #
#  the browser walk
# --------------------------------------------------------------------------- #
@unittest.skipUnless(os.path.isdir(YAML_DIR),
                     "the CLICR workflows are not in this checkout")
class ExpanderParity(unittest.TestCase):
    """
    The runner page expands a workflow in the browser; mopgen.py expands it in
    python. If the two disagree, the same order yields two different documents
    depending on which door you came in by. Every workflow in the repo is
    walked through both and compared step by step.
    """

    def test_every_workflow_expands_identically(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        import test_expander_parity                        # noqa: PLC0415
        self.assertEqual(test_expander_parity.run(yaml_dir=YAML_DIR,
                                                  mapping_dir=MAPPING_DIR), 0,
                         "python and the browser produced different documents")


@unittest.skipUnless(os.path.isfile(MASTER) and os.path.isfile(SAMPLE_CIQ),
                     "the 1051 inputs are not in this checkout")
class Runner(unittest.TestCase):
    """
    build_runner.py's page takes the three inputs itself - drop a workflow, a
    CIQ and the mapping, pick a node, walk it - so it is the only piece where
    an operator's files reach the expander through a file input rather than a
    command line.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="execflow_runner_")
        cls.page = os.path.join(cls.tmp, "clicr-runner.html")
        import build_runner                                # noqa: PLC0415
        build_runner.main(["--out", cls.page, "--quiet"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_page_is_one_self_contained_file(self):
        with io.open(self.page, encoding="utf-8") as handle:
            html = handle.read()
        self.assertNotIn("{{", html)
        self.assertNotIn("<script src", html)
        self.assertNotIn("<link ", html)
        self.assertIn("initDocument", html)
        self.assertIn("class Expander", html)

    def test_it_carries_the_same_engine_as_a_generated_mop(self):
        """One engine, or a document behaves differently depending on where it
        was made. The marker is a quirk no rewrite would reproduce twice."""
        with io.open(self.page, encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn("JS has no such syntax", html)
        self.assertIn("|| splits before &&", html)

    def test_dropping_the_real_files_in_produces_the_same_document(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        script = os.path.join(HERE, "test_runner_dom.js")
        proc = subprocess.Popen([node, script, self.page], cwd=HERE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = proc.communicate()
        text = out.decode("utf-8", "replace")
        if "jsdom is not installed" in text:
            self.skipTest("jsdom is not installed (npm install)")
        self.assertEqual(proc.returncode, 0, text)
        self.assertIn("0 failed", text)


class LenientYamlLoading(unittest.TestCase):
    """
    SnakeYAML and PyYAML forgive two things every JavaScript YAML parser
    rejects, and a page that refused a file the engine runs would be worse than
    useless. Both were found by running the repo's own workflows through it.
    """

    @unittest.skipUnless(os.path.isdir(YAML_DIR), "no workflows in this checkout")
    def test_pyyaml_accepts_what_the_page_has_to_accept(self):
        """The two files that prove the point still load in python, so the
        leniency the page needs is not hypothetical."""
        for name in ["DSR_10005_SAPC_CCPC_PREFERENCE_CHANGE_IN_DSR.yaml",
                     "SBC_96_FIXED_LINE_CONFIGURATION_IN_SBC.yaml"]:
            path = os.path.join(YAML_DIR, name)
            if not os.path.isfile(path):
                continue
            self.assertTrue(CIQ.read_yaml(path).get("phases"), name)

    def test_the_repair_is_reported_not_silent(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        script = (
            'const l=require("./template/yamlload.js");'
            'const t="a:\\n  send: \'printf x\\n\\" y\'\\n";'
            'const r=l.repairIndentation(t);'
            'process.stdout.write(JSON.stringify(r.notes.length));')
        proc = subprocess.Popen([node, "-e", script], cwd=HERE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = proc.communicate()
        text = out.decode("utf-8", "replace").strip()
        self.assertEqual(proc.returncode, 0, text)
        self.assertNotEqual(text, "0", "an under-indented continuation was not noticed")

    def test_duplicate_keys_are_reported(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        script = (
            'const l=require("./template/yamlload.js");'
            'const t="name: a\\nname: b\\n";'
            'process.stdout.write(String(l.duplicateKeys(t).length));')
        proc = subprocess.Popen([node, "-e", script], cwd=HERE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = proc.communicate()
        text = out.decode("utf-8", "replace").strip()
        self.assertEqual(proc.returncode, 0, text)
        self.assertEqual(text, "1")


if __name__ == "__main__":
    unittest.main()

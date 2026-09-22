/*
 * Parity tests for template/engine.js.
 *
 *     node test_engine_js.js
 *
 * The page re-resolves every later command whenever the operator supplies a
 * Custom output, so its engine must agree with engine.py - which agrees with
 * ExecutionContext.java. These are the same vectors the python suite asserts
 * (test_execution_flow.py: Interpolation, Conditions, ForEachAndRegisters), so
 * if one implementation is "tidied up" the mismatch shows here.
 */

const E = require("./template/engine.js");

let passed = 0;
const failures = [];

function eq(label, got, want) {
  if (JSON.stringify(got) === JSON.stringify(want)) { passed++; return; }
  failures.push(label + "\n      got  " + JSON.stringify(got) +
                        "\n      want " + JSON.stringify(want));
}
const T = (label, got) => eq(label, got, true);
const F = (label, got) => eq(label, got, false);

/* -- interpolation ------------------------------------------------------- */
{
  const vars = { A: "x", rec_row: { data: { "Test IMSI": "404960000000112" } },
                 hosts: ["10.0.0.1", "10.0.0.2"], X: "hi" };

  eq("unresolved becomes an empty string",
     E.interpolate("a=${nope}b", vars), "a=b");
  eq("dotted path with a space in the key",
     E.interpolate("${rec_row.data.Test IMSI}", vars), "404960000000112");
  eq("list index", E.interpolate("${hosts[1]}", vars), "10.0.0.2");
  eq("base64", E.interpolate("${base64:X}", vars), "aGk=");
  eq("nested placeholder cannot work",
     E.interpolate("${LDAPFIELDS${A}}", vars), "}");
  eq("no placeholder is returned untouched",
     E.interpolate("plain text", vars), "plain text");

  const missing = [];
  E.interpolate("${nope} ${alsoNope}", vars, missing);
  eq("missing tokens are reported", missing, ["nope", "alsoNope"]);
}

/* -- conditions ---------------------------------------------------------- */
{
  T("empty expression is true", E.evalCond("", {}));
  T("null expression is true", E.evalCond(null, {}));

  T("equality", E.evalCond('${A == "ENABLE"}', { A: "ENABLE" }));
  F("equality is case sensitive", E.evalCond('${A == "enable"}', { A: "ENABLE" }));

  T("unset compares as empty (stringValue(null) is \"\")",
    E.evalCond('${MISSING == ""}', {}));
  F("unset is not equal to a value", E.evalCond('${MISSING == "x"}', {}));
  T("!= against unset", E.evalCond('${MISSING != "x"}', {}));

  T("or splits before and",
    E.evalCond('${A == "1" && B == "1" || C == "1" && D == "1"}',
               { A: "1", B: "0", C: "1", D: "1" }));
  F("parentheses are not supported",
    E.evalCond('${(A == "1" || B == "9") && A == "1"}', { A: "1", B: "1" }));

  T("contains", E.evalCond('${LIST contains "113"}', { LIST: "112,113" }));
  T("notContains", E.evalCond('${LIST notContains "999"}', { LIST: "112,113" }));
  T("startsWith", E.evalCond('${N startsWith "niam"}', { N: "niam_rds_1" }));
  T("notStartsWith", E.evalCond('${N notStartsWith "x"}', { N: "niam_rds_1" }));

  T("numeric >=", E.evalCond("${N >= 8}", { N: "9" }));
  F("numeric >= fails", E.evalCond("${N >= 10}", { N: "9" }));
  T("variable to variable equality",
    E.evalCond("${A == B}", { A: "2", B: "2" }));
  T("gt inside quotes is a literal",
    E.evalCond('${V == "<unset>"}', { V: "<unset>" }));

  /* the real pass filter from the 1051 two-pass work */
  const FILTER = '${pass_row == "REMOVE" && rec_row.data.Action == "REMOVE"' +
                 ' || pass_row == "REMOVE" && rec_row.data.Action == "UN-BARRING"' +
                 ' || pass_row == "ENABLE" && rec_row.data.Action != "REMOVE"' +
                 ' && rec_row.data.Action != "UN-BARRING"}';
  const rec = a => ({ rec_row: { data: { Action: a } } });
  T("pass filter: REMOVE in the remove pass",
    E.evalCond(FILTER, Object.assign({ pass_row: "REMOVE" }, rec("REMOVE"))));
  F("pass filter: REMOVE not in the enable pass",
    E.evalCond(FILTER, Object.assign({ pass_row: "ENABLE" }, rec("REMOVE"))));
  T("pass filter: ENABLE in the enable pass",
    E.evalCond(FILTER, Object.assign({ pass_row: "ENABLE" }, rec("ENABLE"))));
  T("pass filter: an unknown verb lands in the enable pass",
    E.evalCond(FILTER, Object.assign({ pass_row: "ENABLE" }, rec("ENALBE"))));
}

/* -- registers ----------------------------------------------------------- */
{
  let vars = {};
  E.applyRegisters([{ regex: "(?<X>OK)" }], "value is OK here", vars);
  eq("java named group spelling", vars.X, "OK");

  vars = {};
  E.applyRegisters([{ regex: "(?P<X>OK)" }], "value is OK here", vars);
  eq("python named group spelling is rewritten", vars.X, "OK");

  vars = {};
  E.applyRegisters([{ regex: "(?m)^line \\d$", loop: true, count_var: "N" }],
                   "line 1\nline 2\nline 3", vars);
  eq("loop register counts", vars.N, "3");

  vars = { LOGIN: "no" };
  E.applyRegisters([{ name: "T", when: '${LOGIN == "yes"}', value: "x" }], "", vars);
  eq("register when gates the set", vars.T, undefined);

  vars = { LOGIN: "yes" };
  E.applyRegisters([{ name: "T", when: '${LOGIN == "yes"}', value: "niam_rds_1" }],
                   "", vars);
  eq("register when allows the set", vars.T, "niam_rds_1");

  vars = {};
  E.applyRegisters([{ name: "XMLEXISTS", value: "false" },
                    { regex: "(?m)^XML_EXISTS=(?<XMLEXISTS>true|false)" }],
                   "XML_EXISTS=true", vars);
  eq("default then capture", vars.XMLEXISTS, "true");

  vars = {};
  E.applyRegisters([{ name: "BLOCKED", value: "" },
                    { regex: "(?m)^BLOCKED=(?<BLOCKED>.+)" }],
                   "NEEDUPDATE=yes", vars);
  eq("no match keeps the declared default", vars.BLOCKED, "");
}

/* -- criteria ------------------------------------------------------------ */
{
  T("exit_code 0", E.evalCriteria({ exit_code: 0 }, {}, "", 0));
  F("exit_code 0 with a failure", E.evalCriteria({ exit_code: 0 }, {}, "", 1));
  T("expr", E.evalCriteria({ expr: '${A == "1"}' }, { A: "1" }, "", 0));
  T("regex against the output",
    E.evalCriteria({ regex: "(?m)^ok$" }, {}, "ok", 0));
  F("regex that does not match", E.evalCriteria({ regex: "^ok$" }, {}, "no", 0));
  T("all[]", E.evalCriteria({ all: [{ expr: "${A == B}" }, { exit_code: 0 }] },
                            { A: "1", B: "1" }, "", 0));
  F("all[] with one false",
    E.evalCriteria({ all: [{ expr: '${A == "2"}' }, { exit_code: 0 }] },
                   { A: "1" }, "", 0));
  T("any[]", E.evalCriteria({ any: [{ expr: '${A == "2"}' }, { exit_code: 0 }] },
                            { A: "1" }, "", 0));
  T("empty criteria passes", E.evalCriteria({}, {}, "", 0));
}

/* -- the s0010 / s0018 / s0039 shapes the synthesiser had to get right --- */
{
  let vars = {};
  const ss = [{ name: "REMOTE_NODE_COUNT", value: "0" },
              { regex: "(?m)^\\s+Local\\s+DSA\\s+\\d+\\s+NODE\\s+\\d+\\s+is\\s+\\S+",
                loop: true, count_var: "REMOTE_NODE_COUNT" },
              { name: "LINK_UP_COUNT", value: "0" },
              { regex: "(?m)^\\s+Local\\s+DSA\\s+\\d+\\s+NODE\\s+\\d+\\s+is\\s+\\S+.*Link\\s+UP",
                loop: true, count_var: "LINK_UP_COUNT" }];
  E.applyRegisters(ss, " Local DSA 7 NODE 7 is xLink UP", vars);
  T("one specific line satisfies both counters",
    E.evalCond("${LINK_UP_COUNT == REMOTE_NODE_COUNT && LINK_UP_COUNT > 0}", vars));

  vars = {};
  E.applyRegisters([{ name: "WRITEOK", value: "" },
                    { regex: "(?m)^(?<WRITEOK>WRITE_OK)" }],
                   "NOT_MATCHING_WRITE_OK", vars);
  F("a prefixed failure value cannot match an anchored pattern",
    E.evalCond('${WRITEOK == "WRITE_OK"}', vars));
}

/* -- report -------------------------------------------------------------- */
console.log("engine.js parity: " + passed + " passed, " + failures.length + " failed");
if (failures.length) {
  for (const f of failures) console.log("  FAIL  " + f);
  process.exit(1);
}

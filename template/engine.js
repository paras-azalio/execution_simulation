/* engine.js - the CLICR engine, in the browser.
 *
 * A mirror of execution_simulation/engine.py, which is itself a port of
 * cliautomation/exec/ExecutionContext.java. The page needs it because the
 * operator's Custom input changes variables, and every later command, gate and
 * criterion then has to be re-resolved without regenerating the document.
 *
 * Same quirks on purpose:
 *   - an unresolved ${...} becomes an EMPTY STRING
 *   - stringValue(null) is "", so ${MISSING == ""} is TRUE
 *   - || splits before &&, and there are NO parentheses
 *   - == is case sensitive
 *
 * test_engine_js.js runs the same vectors as the python tests through both
 * implementations, so the two cannot drift apart silently.
 */
const PH = /\$\{([^}]+)\}/g;

function stringify(v){
  if(v===null||v===undefined) return "";
  if(typeof v==="boolean") return v?"true":"false";
  if(typeof v==="object")  return pyDumps(v);
  return String(v);
}

/* A map or list interpolated into a command is rendered by json.dumps() on the
 * python side, and json.dumps puts a SPACE after every ',' and ':'.
 * JSON.stringify does not, so a command carrying a whole CIQ table came out
 * differently in the page than in the generated document. Same bytes or the
 * two are not the same document. */
function pyDumps(v){
  if(v===null||v===undefined) return "null";
  if(typeof v==="boolean") return v?"true":"false";
  if(typeof v==="number") return String(v);
  if(typeof v==="string") return JSON.stringify(v);
  if(Array.isArray(v)) return "[" + v.map(pyDumps).join(", ") + "]";
  if(typeof v==="object")
    return "{" + Object.keys(v).map(k=>JSON.stringify(k)+": "+pyDumps(v[k])).join(", ") + "}";
  return JSON.stringify(v);
}

function resolveName(name, vars){
  name = (name||"").trim();
  if(name.startsWith("base64:")){
    const inner = resolveName(name.slice(7), vars);
    if(inner===undefined||inner===null) return undefined;
    try { return btoa(unescape(encodeURIComponent(stringify(inner)))); }
    catch(e){ return undefined; }
  }
  if(name in vars) return vars[name];
  const parts = name.split(".");
  let cur, found=false, rest=[];
  for(let cut=parts.length; cut>0; cut--){
    const head = parts.slice(0,cut).join(".");
    if(head in vars){ cur=vars[head]; rest=parts.slice(cut); found=true; break; }
    /* the head itself may carry an index - "hosts[1]", "nodes[0].PGW" */
    const bm = /^(.*?)\[(\d+)\]$/.exec(head);
    if(bm && (bm[1] in vars)){
      const base = vars[bm[1]], idx = parseInt(bm[2],10);
      if(!Array.isArray(base) || idx>=base.length) return undefined;
      cur = base[idx]; rest = parts.slice(cut); found = true; break;
    }
  }
  if(!found) return undefined;
  for(let part of rest){
    let idx=null;
    const m = /^(.*?)\[(\d+)\]$/.exec(part);
    if(m){ part=m[1]; idx=parseInt(m[2],10); }
    if(part){
      if(cur && typeof cur==="object" && !Array.isArray(cur) && part in cur) cur=cur[part];
      else return undefined;
    }
    if(idx!==null){
      if(!Array.isArray(cur) || idx>=cur.length) return undefined;
      cur=cur[idx];
    }
  }
  return cur;
}

function interpolate(raw, vars, missing){
  if(typeof raw!=="string") return raw;
  if(raw.indexOf("${")<0) return raw;
  return raw.replace(PH, (_m, token)=>{
    const v = resolveName(token.trim(), vars);
    if(v===undefined||v===null){ if(missing) missing.push(token.trim()); return ""; }
    return stringify(v);
  });
}

function splitTop(expr, op){
  const out=[]; let i=0,last=0,sq=false,dq=false;
  while(i<expr.length){
    const c=expr[i];
    if(c==="'"&&!dq) sq=!sq; else if(c==='"'&&!sq) dq=!dq;
    if(!sq&&!dq&&expr.startsWith(op,i)){
      const piece=expr.slice(last,i).trim(); if(piece) out.push(piece);
      i+=op.length; last=i; continue;
    }
    i++;
  }
  const tail=expr.slice(last).trim(); if(tail) out.push(tail);
  return out;
}

function findOp(expr, op){
  let sq=false,dq=false;
  for(let i=0;i+op.length<=expr.length;i++){
    const c=expr[i];
    if(c==="'"&&!dq){sq=!sq;continue;}
    if(c==='"'&&!sq){dq=!dq;continue;}
    if(!sq&&!dq&&expr.startsWith(op,i)) return i;
  }
  return -1;
}

function operand(text, vars){
  text=(text||"").trim();
  if(text.length>=2 && text[0]===text[text.length-1] && (text[0]==="'"||text[0]==='"'))
    return text.slice(1,-1);
  const v=resolveName(text,vars);
  return v===undefined||v===null ? text : stringify(v);
}

function cmpVals(a,b){
  const na=parseFloat(a), nb=parseFloat(b);
  if(!isNaN(na)&&!isNaN(nb)&&String(na)===String(a).trim()&&String(nb)===String(b).trim())
    return na>nb?1:(na<nb?-1:0);
  a=stringify(a); b=stringify(b);
  return a>b?1:(a<b?-1:0);
}

const WORD_OPS=[" notStartsWith "," startsWith "," notContains "," contains "];
const CMP_OPS=["==","!=",">=","<=",">","<"];

function evalCond(expression, vars, trace){
  if(expression===null||expression===undefined||!String(expression).trim()) return true;
  let expr=String(expression).trim();
  if(expr.startsWith("${")&&expr.endsWith("}")) expr=expr.slice(2,-1).trim();
  return evalExpr(expr, vars, trace);
}

function evalExpr(expr, vars, trace){
  const ors=splitTop(expr,"||");
  if(ors.length>1){ for(const p of ors) if(evalExpr(p,vars,trace)) return true; return false; }
  const ands=splitTop(expr,"&&");
  if(ands.length>1){ for(const p of ands) if(!evalExpr(p,vars,trace)) return false; return true; }
  return evalLeaf(expr.trim(), vars, trace);
}

function evalLeaf(expr, vars, trace){
  /* stringValue() (ExecutionContext.java:642) makes an unset variable the
     EMPTY STRING before any comparison, so ${MISSING == ""} is true and no
     operator ever sees a null. `shown` keeps <unset> for the debug panel. */
  for(const op of WORD_OPS){
    const at=expr.indexOf(op);
    if(at>0){
      const lraw=expr.slice(0,at).trim();
      const lv=resolveName(lraw,vars);
      const unset=(lv===undefined||lv===null);
      const left=unset?"":stringify(lv);
      const right=operand(expr.slice(at+op.length),vars);
      const name=op.trim();
      let r;
      if(name==="notStartsWith") r = !left.startsWith(right);
      else if(name==="startsWith") r = left.startsWith(right);
      else if(name==="notContains") r = left.indexOf(right)<0;
      else r = left.indexOf(right)>=0;
      if(trace) trace.push({expr, left:lraw, leftVal:unset?null:left, right, result:r});
      return r;
    }
  }
  for(const op of CMP_OPS){
    const at=findOp(expr,op);
    if(at>0){
      const lraw=expr.slice(0,at).trim();
      const lv=resolveName(lraw,vars);
      const unset=(lv===undefined||lv===null);
      const left=unset?"":stringify(lv);
      const right=operand(expr.slice(at+op.length),vars);
      let r;
      if(op==="==") r = left===right;
      else if(op==="!=") r = left!==right;
      else { const c=cmpVals(left,right);
             r = op===">="?c>=0:op==="<="?c<=0:op===">"?c>0:c<0; }
      if(trace) trace.push({expr, left:lraw, leftVal:unset?null:left, right, result:r});
      return r;
    }
  }
  const raw=resolveName(expr,vars);
  const r = typeof raw==="boolean" ? raw
          : (raw!==undefined && raw!==null && stringify(raw).toLowerCase()!=="false");
  if(trace) trace.push({expr, left:expr, leftVal:raw===undefined?null:stringify(raw), right:"", result:r});
  return r;
}

/* Java and python accept inline flags - (?m), (?s), (?i) - INSIDE the pattern.
 * JS has no such syntax: new RegExp("(?m)^x$") throws. Every register and
 * criteria pattern in the CLICR workflows starts with (?m), so without this
 * translation they all failed to compile and every capture silently produced
 * nothing. The flags are lifted out of the source and passed to RegExp. */
function toJsRegex(p){
  let src = (p||"").replace(/\(\?P</g, "(?<");
  let flags = "";
  src = src.replace(/\(\?([imsx]+)\)/g, (_m, f)=>{
    for(const c of f) if("ims".indexOf(c)>=0 && flags.indexOf(c)<0) flags += c;
    return "";
  });
  return { source: src, flags: flags };
}

function compileRe(pattern, extra){
  const t = toJsRegex(pattern);
  let flags = t.flags + (extra||"");
  flags = flags.split("").filter((c,i,a)=>a.indexOf(c)===i).join("");
  try { return new RegExp(t.source, flags); } catch(e){ return null; }
}

function applyRegisters(register, output, vars, log){
  output = output||"";
  for(const entry of register||[]){
    if(!entry) continue;
    if(entry.when!==undefined && entry.when!==null && !evalCond(entry.when,vars)){
      if(log) log.push({name:entry.name||entry.regex, value:null, source:"skipped: when is false"});
      continue;
    }
    if(entry.regex){
      const re = compileRe(entry.regex, entry.loop?"g":"");
      if(!re){ if(log) log.push({name:entry.regex,value:null,source:"bad regex"}); continue; }
      if(entry.loop){
        /* ResultProcessor: a loop register names its captures NAME_1..NAME_n
           and counts them. engine.py sets those numbered variables too, and a
           workflow reads them (${XMLNAME_1}), so the mirror has to as well. */
        const hits = Array.from(output.matchAll(re));
        const base = (function(){
          const m = /\(\?P?<([A-Za-z][A-Za-z0-9]*)>/.exec(entry.regex||"");
          return m ? m[1] : "MATCH";
        })();
        hits.forEach((hit, i)=>{
          const text = hit.groups ? (Object.values(hit.groups)[0]||"")
                     : (hit[1]!==undefined ? hit[1] : hit[0]);
          vars[base+"_"+(i+1)] = text||"";
        });
        if(entry.count_var){
          vars[entry.count_var]=String(hits.length);
          if(log) log.push({name:entry.count_var,value:String(hits.length),source:"loop count"});
        }
        continue;
      }
      const m = re.exec(output);
      if(!m){ if(log) log.push({name:entry.regex,value:null,source:"no match in output"}); continue; }
      if(m.groups){
        for(const g in m.groups){
          vars[g]=m.groups[g]||"";
          if(log) log.push({name:g,value:vars[g],source:"captured"});
        }
      }
      continue;
    }
    if(entry.name){
      vars[entry.name]=interpolate(entry.value===undefined?"":entry.value, vars);
      if(log) log.push({name:entry.name,value:vars[entry.name],source:"set from value"});
    }
  }
}

/* criteria evaluation - SuccessCriteria: exit_code / http_status /
   transfer_status / regex / expr / all[] / any[] */
function evalCriteria(crit, vars, output, exitCode){
  if(!crit || typeof crit!=="object") return true;
  const checks=[];
  if("exit_code" in crit) checks.push(String(exitCode)===String(crit.exit_code));
  if("http_status" in crit) checks.push(true);
  if("transfer_status" in crit) checks.push(true);
  if(typeof crit.regex==="string"){
    const re = compileRe(crit.regex, "");
    checks.push(re ? re.test(output||"") : false);
  }
  if(typeof crit.expr==="string" && crit.expr.trim()) checks.push(evalCond(crit.expr,vars));
  if(Array.isArray(crit.all)) checks.push(crit.all.every(c=>evalCriteria(c,vars,output,exitCode)));
  if(Array.isArray(crit.any)) checks.push(crit.any.some(c=>evalCriteria(c,vars,output,exitCode)));
  if(!checks.length) return true;
  return checks.every(Boolean);
}


/* Criteria evaluation, with the reasoning kept.
 *
 * evalCriteria() answers pass or fail, which is all the document needed while
 * the output was synthesised from the criteria themselves. The moment an
 * operator pastes real output, "SUCCESS" without a reason is worthless - and
 * actively misleading where the only criterion is `exit_code: 0`, which any
 * pasted text satisfies. This returns the same verdict with every check that
 * produced it, so the page can show the operator what was actually tested. */
function explainCriteria(crit, vars, output, exitCode, out){
  out = out || [];
  if(!crit || typeof crit!=="object" || !Object.keys(crit).length){
    out.push({label:"no output criteria - this step is judged on its exit code alone",
              ok:String(exitCode)==="0", detail:"exit code "+exitCode});
    return out;
  }
  if("exit_code" in crit){
    out.push({label:"exit code is "+crit.exit_code,
              ok:String(exitCode)===String(crit.exit_code),
              detail:"you gave "+exitCode});
  }
  if("http_status" in crit) out.push({label:"HTTP status "+crit.http_status, ok:true,
                                      detail:"not checked against pasted output"});
  if("transfer_status" in crit) out.push({label:"transfer "+crit.transfer_status, ok:true,
                                          detail:"not checked against pasted output"});
  if(typeof crit.regex==="string"){
    const re = compileRe(crit.regex, "");
    out.push({label:"output matches "+crit.regex,
              ok: re ? re.test(output||"") : false,
              detail: re ? "" : "the pattern does not compile"});
  }
  if(typeof crit.expr==="string" && crit.expr.trim()){
    const trace=[];
    const ok = evalCond(crit.expr, vars, trace);
    out.push({label:crit.expr, ok:ok, trace:trace});
  }
  if(Array.isArray(crit.all)){
    for(const item of crit.all) explainCriteria(item, vars, output, exitCode, out);
  }
  if(Array.isArray(crit.any)){
    const inner=[];
    for(const item of crit.any) explainCriteria(item, vars, output, exitCode, inner);
    out.push({label:"any of these", ok:inner.some(c=>c.ok), children:inner});
  }
  return out;
}

/* ===================================================================== *
 *  What the walk needs on top of resolution
 * ===================================================================== */

/** Every ${...} token in a string, in order, deduplicated. */
function tokensIn(raw){
  if(typeof raw!=="string") return [];
  const seen=new Set(), out=[];
  let m; const re=new RegExp(PH.source, "g");
  while((m=re.exec(raw))){
    const t=m[1].trim();
    if(!seen.has(t)){ seen.add(t); out.push(t); }
  }
  return out;
}

/** Why a ${...} resolved to nothing - ExecutionContext has no such notion,
 *  but a document has to show the operator a reason, not a blank. */
function whyUnresolved(token, vars){
  if(token.startsWith("ENV.")) return "environment variable "+token.slice(4)+
    " is not set (a browser has no environment)";
  if(token.startsWith("SECRET.")) return "secret "+token.slice(7)+" was not supplied";
  if(token in vars) return token+" is empty";
  const root = token.split(".")[0].split("[")[0];
  if(root in vars) return root+" has no member "+token.slice(root.length+1);
  return token+" is not defined";
}

/** ExecutionContext.resolveForEachValue() + ExecutionOrchestrator.toIterable().
 *  A bare "${nodeGroups}" yields the LIST; a YAML list literal iterates;
 *  anything else becomes a one-element list (the "[1]" wrapper idiom). */
function resolveForEach(raw, vars){
  if(raw===null||raw===undefined) return [];
  let value;
  if(typeof raw==="string"){
    const trimmed=raw.trim();
    if(trimmed.startsWith("${") && trimmed.endsWith("}"))
      value = resolveName(trimmed.slice(2,-1).trim(), vars);
    else
      value = interpolate(raw, vars);
  } else {
    value = raw;
  }
  if(value===null||value===undefined) return [];
  return Array.isArray(value) ? value.slice() : [value];
}

/* node (parity tests) picks these up; the browser ignores the export. */
if (typeof module !== "undefined" && module.exports) {
  module.exports = { resolveName, interpolate, evalCond, applyRegisters,
                     compileRe, tokensIn, whyUnresolved, resolveForEach,
                     explainCriteria,
                     evalCriteria, splitTop, findOp, operand, cmpVals,
                     stringify, pyDumps, toJsRegex };
}

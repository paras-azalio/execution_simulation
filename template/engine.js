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
      if(cur && typeof cur==="object" && !Array.isArray(cur)){
        if(part in cur) cur=cur[part];
        else {
          /* resolvePath(): a missing key falls back to the ONE key ending in
             ".<part>"; two such keys and the lookup gives up. */
          const hits = Object.keys(cur).filter(k => k.endsWith("." + part));
          if(hits.length!==1) return undefined;
          cur = cur[hits[0]];
        }
      }
      else if(Array.isArray(cur) && /^\d+$/.test(part)){
        /* resolvePath() indexes a list with a DOTTED number: hosts.0 */
        const i = parseInt(part,10);
        if(i>=cur.length) return undefined;
        cur = cur[i];
      }
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

/* ResultProcessor.tryResolveGroupName(): the i-th CAPTURING group gets the
 * i-th NAME in the pattern text, so an unnamed group ahead of a named one
 * shifts the names - a real engine quirk, reproduced on purpose. */
const GROUP_NAME_RE = /\(\?P?<([a-zA-Z][a-zA-Z0-9_]*)>/g;
function groupNamesOf(pattern){
  const out=[]; let m; const re=new RegExp(GROUP_NAME_RE.source,"g");
  while((m=re.exec(String(pattern||"")))) out.push(m[1]);
  return out;
}

/* java.util.regex refuses a '{' that is not a {n,m} quantifier and a group
 * name with '_' in it; python and JS accept both, so the page has to say no
 * where the engine would. */
function javaRegexError(pattern){
  const text = String(pattern||"");
  for(const name of groupNamesOf(text)){
    if(name.indexOf("_")>=0) return "group name <"+name+"> contains '_', which java.util.regex rejects";
  }
  const stripped = text.replace(/\[(?:\\.|[^\]\\])*\]/g, "");
  if(/(^|[^\\])\{(?!\d+(?:,\d*)?\})/.test(stripped))
    return "a '{' that is not a {n,m} quantifier (Illegal repetition)";
  return null;
}

/* Pattern.compile(p, Pattern.MULTILINE) - how both captureVariables() and
 * regexMatches() compile. Returns null for anything java would reject. */
function compileJava(pattern, extra){
  if(javaRegexError(pattern)) return null;
  return compileRe(pattern, "m"+(extra||""));
}

/**
 * ResultProcessor.captureVariables(), including the parts that surprise:
 * the regex is interpolated first, compiled MULTILINE, the LAST match wins,
 * no match clears the named groups to "", every group of a loop register is
 * numbered, and a name entry's `when` gates only the name - never the regex.
 */
function applyRegisters(register, output, vars, log){
  output = (output===null||output===undefined) ? "" : String(output);
  for(const entry of register||[]){
    if(!entry || typeof entry!=="object") continue;
    if(entry.regex!==undefined && entry.regex!==null){
      const resolved = interpolate(String(entry.regex), vars);
      const names = groupNamesOf(resolved);
      const re = compileJava(resolved, "g");
      if(!re){
        if(log) log.push({name:entry.regex, value:null,
                          source:"bad regex: "+(javaRegexError(resolved)||"does not compile")});
      } else {
        const loop = !!entry.loop;
        let index = (typeof entry.start_index==="number") ? entry.start_index : 1;
        let count = 0, groups = 0;
        for(const m of output.matchAll(re)){
          count++;
          groups = m.length-1;
          for(let i=1;i<m.length;i++){
            const name = names[i-1];
            if(!name) continue;
            const key = loop ? name+"_"+index : name;
            if(m[i]===undefined) delete vars[key];           // putVariable(k, null)
            else vars[key] = m[i];
            if(log) log.push({name:key, value:m[i]===undefined?null:m[i],
                              source:"captured by "+entry.regex});
          }
          if(loop) index++;
        }
        if(count===0 && !loop){
          /* groupCount() of the pattern - a throwaway match against "" is not
             possible for every pattern, so count the capturing groups the way
             the source declares them */
          const total = new RegExp(re.source+"|", re.flags.replace("g","")).exec("").length-1;
          for(let i=1;i<=total;i++){
            const name = names[i-1];
            if(!name) continue;
            vars[name] = "";
            if(log) log.push({name:name, value:null, source:'no match in output - cleared to ""'});
          }
        }
        if(loop && entry.count_var){
          vars[entry.count_var]=String(count);
          if(log) log.push({name:entry.count_var,value:String(count),source:"loop count over "+entry.regex});
        }
      }
    }
    const name = entry.name;
    if(!name || !String(name).trim()) continue;
    if(entry.when!==undefined && entry.when!==null && !evalCond(entry.when,vars)){
      if(log) log.push({name:name, value:null, source:"skipped: when is false"});
      continue;
    }
    if(entry.value===undefined || entry.value===null) continue;
    vars[name] = interpolateObject(entry.value, vars);
    if(log) log.push({name:name, value:stringify(vars[name]), source:"set from value"});
  }
}

/** ExecutionContext.interpolateObject(): strings, maps and lists, recursively. */
function interpolateObject(value, vars){
  if(typeof value==="string") return interpolate(value, vars);
  if(Array.isArray(value)) return value.map(v=>interpolateObject(v, vars));
  if(value && typeof value==="object"){
    const out={}; for(const k of Object.keys(value)) out[k]=interpolateObject(value[k], vars);
    return out;
  }
  return value;
}

/* ===================================================================== *
 *  criteria - ResultProcessor.isSuccess()
 * ===================================================================== */
/** regexMatches(): Pattern.compile(p, MULTILINE).matcher(output).find(). */
function regexMatches(pattern, output){
  if(output===null||output===undefined) return false;
  const re = compileJava(String(pattern));
  return re ? re.test(output) : false;
}

/** safeEquals(String.valueOf(expected), String.valueOf(actual)). */
function attrText(v){
  if(v===null||v===undefined) return "null";
  if(typeof v==="boolean") return v?"true":"false";
  return String(v);
}

/* The protocol attributes the java reads. A bare number is the exit code -
 * the shape every caller used before REST steps carried a status. */
function asAttrs(a){
  if(a===null||a===undefined) return {};
  if(typeof a==="object") return a;
  return { exit_code: a };
}

/** matchesCondition(): an all[]/any[] item is NOT a nested criteria block. A
 *  `regex` is matched; every other key is compared to the RESULT ATTRIBUTE of
 *  that name - so an `expr:` inside all[] is always false. */
function matchesCondition(cond, output, attrs){
  if(!cond || typeof cond!=="object" || !Object.keys(cond).length) return true;
  if(cond.regex!==undefined && cond.regex!==null) return regexMatches(cond.regex, output);
  for(const k of Object.keys(cond)){
    if(attrText(cond[k])!==attrText(attrs[k])) return false;
  }
  return true;
}

/* criteria evaluation - SuccessCriteria: exit_code / regex / expr /
   http_status / transfer_status / all[] / any[] */
function evalCriteria(crit, vars, output, attrsIn){
  const attrs = asAttrs(attrsIn);
  if(crit===null||crit===undefined){
    return attrs.exit_code===undefined || attrs.exit_code===null || String(attrs.exit_code)==="0";
  }
  if(typeof crit!=="object") return true;
  if(crit.exit_code!==undefined && crit.exit_code!==null){
    if(attrs.exit_code===undefined||attrs.exit_code===null||String(attrs.exit_code)!==String(crit.exit_code)) return false;
  }
  if(crit.regex!==undefined && crit.regex!==null && !regexMatches(crit.regex, output)) return false;
  if(typeof crit.expr==="string" && crit.expr.trim() && !evalCond(crit.expr,vars)) return false;
  if(crit.http_status!==undefined && crit.http_status!==null){
    if(attrs.http_status===undefined||attrs.http_status===null||String(attrs.http_status)!==String(crit.http_status)) return false;
  }
  if(crit.transfer_status!==undefined && crit.transfer_status!==null){
    if(attrText(crit.transfer_status)!==attrText(attrs.transfer_status)) return false;
  }
  if(Array.isArray(crit.all) && crit.all.length && !crit.all.every(c=>matchesCondition(c,output,attrs))) return false;
  if(Array.isArray(crit.any) && crit.any.length && !crit.any.some(c=>matchesCondition(c,output,attrs))) return false;
  return true;
}

/* ===================================================================== *
 *  REST - RestProtocolPlugin
 * ===================================================================== */
/** The body as the plugin reads it: `\/` unescaped, then loaded. An empty body
 *  is not an error - every json_path then falls back to its default. */
function parseBody(body){
  const text = String(body===null||body===undefined ? "" : body).replace(/\\\//g, "/");
  if(!text.trim()) return { root: null, error: null };
  try { return { root: JSON.parse(text), error: null }; } catch(e){ /* try YAML */ }
  if(typeof jsyaml!=="undefined"){
    try { return { root: jsyaml.load(text), error: null }; } catch(e){ /* reported */ }
  }
  return { root: null, error: "failed to parse response body for inline response_template" };
}

function splitPathTokens(body){
  const out=[]; let buf="", depth=0;
  for(const c of body){
    if(c==="." && depth===0){ out.push(buf); buf=""; continue; }
    if(c==="[") depth++; else if(c==="]") depth--;
    buf+=c;
  }
  if(buf) out.push(buf);
  return out;
}

function navigateToken(cur, token){
  const at = token.indexOf("[");
  const key = at<0 ? token : token.slice(0,at);
  let pos = 0;
  if(key){
    if(!cur || typeof cur!=="object" || Array.isArray(cur)) return null;
    cur = (key in cur) ? cur[key] : null;
    pos = key.length;
  }
  while(pos<token.length){
    if(token[pos]!=="[") return null;
    const close = token.indexOf("]", pos);
    if(close<0) return null;
    const raw = token.slice(pos+1, close).trim();
    if(!/^-?\d+$/.test(raw) || !Array.isArray(cur)) return null;
    const i = parseInt(raw,10);
    if(i<0 || i>=cur.length) return null;
    cur = cur[i];
    pos = close+1;
  }
  return cur===undefined ? null : cur;
}

/** evaluateJsonPath(): $, $.a.b, $.a[0].b, $.list.length() - nothing more. */
function jsonPath(root, path){
  if(root===null||root===undefined||path===null||path===undefined) return null;
  path = String(path).trim();
  if(!path) return null;
  if(path==="$") return root;
  if(!path.startsWith("$")) return null;
  let body = path.slice(1);
  if(body.startsWith(".")) body = body.slice(1);
  let cur = root;
  for(const token of splitPathTokens(body)){
    if(!token) continue;
    if(token==="length()"){
      if(Array.isArray(cur) || typeof cur==="string") { cur = cur.length; continue; }
      if(cur && typeof cur==="object") { cur = Object.keys(cur).length; continue; }
      return null;
    }
    cur = navigateToken(cur, token);
    if(cur===null||cur===undefined) return null;
  }
  return cur;
}

/** applyInlineResponseTemplate(). Returns the error the plugin would throw -
 *  a missing `required` field, an unreadable body - or null. */
function applyResponseTemplate(rules, body, vars, log){
  if(!Array.isArray(rules)) return null;
  const parsed = parseBody(body);
  if(parsed.error) return parsed.error;
  for(const rule of rules){
    if(!rule || typeof rule!=="object") continue;
    const name = interpolate(String(rule.name===undefined||rule.name===null?"":rule.name), vars);
    if(!name.trim()) continue;
    const path = interpolate(String(rule.json_path===undefined||rule.json_path===null?"":rule.json_path), vars);
    let value = jsonPath(parsed.root, path);
    let source = "json_path "+path;
    if((value===null||value===undefined) && ("default" in rule)){
      value = interpolateObject(rule["default"], vars);
      source = "default (nothing at "+path+")";
    }
    if((value===null||value===undefined) && rule.required)
      return "required response_template field missing: "+name+" path="+path;
    if(value!==null && value!==undefined) vars[name] = value;
    if(log) log.push({name:name, value:(value===null||value===undefined)?null:stringify(value), source:source});
  }
  return null;
}


/* Criteria evaluation, with the reasoning kept.
 *
 * evalCriteria() answers pass or fail, which is all the document needed while
 * the output was synthesised from the criteria themselves. The moment an
 * operator pastes real output, "SUCCESS" without a reason is worthless - and
 * actively misleading where the only criterion is `exit_code: 0`, which any
 * pasted text satisfies. This returns the same verdict with every check that
 * produced it, so the page can show the operator what was actually tested. */
function explainCriteria(crit, vars, output, attrsIn, out){
  out = out || [];
  const attrs = asAttrs(attrsIn);
  const shown = v => (v===undefined||v===null) ? "none" : String(v);
  if(!crit || typeof crit!=="object" || !Object.keys(crit).length){
    out.push({label:"no output criteria - this step is judged on whether the command itself succeeded",
              ok:true, detail:attrs.http_status!==undefined ? "HTTP "+shown(attrs.http_status)
                                                            : "exit code "+shown(attrs.exit_code)});
    return out;
  }
  if(crit.exit_code!==undefined && crit.exit_code!==null){
    out.push({label:"exit code is "+crit.exit_code,
              ok:attrs.exit_code!==undefined && attrs.exit_code!==null &&
                 String(attrs.exit_code)===String(crit.exit_code),
              detail:"you gave "+shown(attrs.exit_code)});
  }
  if(crit.regex!==undefined && crit.regex!==null){
    const why = javaRegexError(String(crit.regex));
    out.push({label:"output matches "+crit.regex,
              ok: regexMatches(crit.regex, output),
              detail: why ? "java refuses this pattern: "+why : ""});
  }
  if(typeof crit.expr==="string" && crit.expr.trim()){
    const trace=[];
    const ok = evalCond(crit.expr, vars, trace);
    out.push({label:crit.expr, ok:ok, trace:trace});
  }
  if(crit.http_status!==undefined && crit.http_status!==null){
    out.push({label:"HTTP status is "+crit.http_status,
              ok:attrs.http_status!==undefined && attrs.http_status!==null &&
                 String(attrs.http_status)===String(crit.http_status),
              detail:"got "+shown(attrs.http_status)});
  }
  if(crit.transfer_status!==undefined && crit.transfer_status!==null){
    out.push({label:"transfer status is "+crit.transfer_status,
              ok:attrText(crit.transfer_status)===attrText(attrs.transfer_status),
              detail:"got "+shown(attrs.transfer_status)});
  }
  const itemLabel = c => (c && c.regex!==undefined) ? "output matches "+c.regex
    : Object.keys(c||{}).map(k=>k+" = "+attrText(c[k])+
        (k==="expr" ? "  (compared as a result attribute - the engine never evaluates it)" : "")).join(", ");
  if(Array.isArray(crit.all) && crit.all.length){
    const inner = crit.all.map(c=>({label:itemLabel(c), ok:matchesCondition(c,output,attrs)}));
    out.push({label:"all of these", ok:inner.every(c=>c.ok), children:inner});
  }
  if(Array.isArray(crit.any) && crit.any.length){
    const inner = crit.any.map(c=>({label:itemLabel(c), ok:matchesCondition(c,output,attrs)}));
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
                     compileRe, compileJava, javaRegexError, groupNamesOf,
                     tokensIn, whyUnresolved, resolveForEach,
                     explainCriteria, matchesCondition, regexMatches,
                     evalCriteria, splitTop, findOp, operand, cmpVals,
                     stringify, pyDumps, toJsRegex, interpolateObject,
                     parseBody, jsonPath, applyResponseTemplate };
}

import json, os, sys, importlib
sys.path.insert(0, "/opt/muse-bridge")
os.environ["MUSE_ADAPTER_LOG"]="/tmp/hint_adapter.log"; os.environ["MUSE_CALL_CACHE"]="/tmp/hint_cache.sqlite"
fails=[]
def chk(n,c):
    print(("PASS" if c else "FAIL"),n)
    if not c: fails.append(n)
def load(hint):
    os.environ["MUSE_AGENT_HINT"]=hint
    import adapter; return importlib.reload(adapter)
def body(instr="You are a coding agent. Terse.", extra=None):
    b={"model":"muse-spark-1.3-contributor","instructions":instr,"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}],"tools":[]}
    if extra: b.update(extra)
    return b
# find the entry function that returns (bytes, notes) after the include logic
a=load("0")
fn="transform_request_body"
F=lambda mod,b: getattr(mod,fn)(json.dumps(b).encode())
# 1 off: unchanged
raw,notes=F(a,body()); d=json.loads(raw)
chk("hint OFF: instructions unchanged", d["instructions"]=="You are a coding agent. Terse." and "agent-hint" not in notes)
# 2 on: appended once
a=load("1"); raw,notes=F(a,body()); d=json.loads(raw)
chk("hint ON: appended", "notify_on_complete=true" in d["instructions"] and d["instructions"].startswith("You are a coding agent. Terse."))
chk("hint ON: note logged", "agent-hint" in notes)
# 3 idempotent
b2=body(d["instructions"]); raw2,_=F(a,b2); d2=json.loads(raw2)
chk("idempotent (no double append)", d2["instructions"].count("[adapter-hint]")==1)
# 4 session key / prompt_cache_key identical with hint on vs off
a0=load("0"); r0,n0=F(a0,body()); k0=json.loads(r0).get("prompt_cache_key")
a1=load("1"); r1,n1=F(a1,body()); k1=json.loads(r1).get("prompt_cache_key")
chk("prompt_cache_key unchanged by hint (%s)"%k0, k0 is not None and k0==k1)
# 5 stateful chain key unchanged on a follow-up request that carries previous hint-free instructions
chk("same key on repeat", json.loads(F(a1,body())[0]).get("prompt_cache_key")==k1)
# 6 chained path (previous_response_id) also gets the hint
rc,_=F(a1,body(extra={"previous_response_id":"resp_x"})); dc=json.loads(rc)
chk("chained request also gets hint", "notify_on_complete=true" in dc["instructions"])
# 7 empty/missing instructions
b=body(); b.pop("instructions"); dd=json.loads(F(a1,b)[0])
chk("missing instructions -> hint only", dd["instructions"].startswith("[adapter-hint]"))
# 8 non-string instructions don't crash
b=body(); b["instructions"]=None; dd=json.loads(F(a1,b)[0]); chk("None instructions ok","instructions" in dd)
print("FAILED:",fails) ; sys.exit(1 if fails else 0)

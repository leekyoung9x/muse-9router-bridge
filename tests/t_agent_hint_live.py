import sys, json, time, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import *
TOOL = {"type":"function","name":"terminal","description":"Run a shell command. For long jobs set background=true.",
 "parameters":{"type":"object","properties":{"command":{"type":"string"},"background":{"type":"boolean"},
 "notify":{"description":"true = notify when the background process exits; [..] = patterns","anyOf":[{"type":"boolean"},{"type":"array","items":{"type":"string"}}]},
 "notify_on_complete":{"type":"boolean"},"watch_patterns":{"type":"array","items":{"type":"string"}}},"required":["command"]}}
PROMPT = "Build the WebGL game: run `cd /proj && ./build.sh webgl` and write its output to /tmp/build.log. It takes about 12 minutes, so run it in the background."
def trial(i):
    b={"model":MODEL,"max_output_tokens":2000,"store":True,"instructions":"You are a coding agent. Terse.",
       "tools":[TOOL],"input":[msg("user","[t%d-%d] %s"%(i,time.time(),PROMPT)),]}
    c,d=post(b)
    if c!=200: return ("http%s"%c,None)
    for it in d.get("output",[]):
        if it.get("type")=="function_call":
            try: a=json.loads(it.get("arguments") or "{}")
            except Exception: return ("badargs",None)
            n=a.get("notify"); noc=a.get("notify_on_complete")
            ok = (n is True) or (noc is True)
            kind = "armed" if ok else ("pattern-only" if (n or a.get("watch_patterns")) else "NONE")
            return (kind,a.get("background"))
    return ("no-tool-call",None)
N=int(sys.argv[1]) if len(sys.argv)>1 else 6
res=[trial(i) for i in range(N)]
from collections import Counter
print(os.environ.get("LABEL","?"), dict(Counter(r[0] for r in res)), "of", N)

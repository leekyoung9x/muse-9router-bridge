# or-harness-proxy

Header-setting passthrough to OpenRouter + a one-shot registrar that adds it to 9router as a custom
provider (`orh`). Fixes `403 ... only available on agentic harnesses` without patching 9router.

Full explanation, measurements, install/verify/caveats: **[../docs/openrouter-harness.md](../docs/openrouter-harness.md)**

    sudo ./install.sh        # then use model  orh/thinkingmachines/inkling-2-small:free

Files: `proxy.py` (127.0.0.1:20134, no key stored), `register-9router.py` (idempotent DB registration),
`install.sh`, `or-harness-proxy.service` (reference unit; install.sh writes its own).

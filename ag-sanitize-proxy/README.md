# ag-sanitize-proxy

Loopback proxy that moves a third-party harness's system prompt out of
`systemInstruction` before the request reaches Google's Antigravity endpoint,
which otherwise answers `429 RESOURCE_EXHAUSTED` (0 tokens) for such prompts
while the official `agy` client and raw `agy` calls work fine.

Full explanation, measurements, install/verify/caveats: **[../docs/antigravity-system-sanitize.md](../docs/antigravity-system-sanitize.md)**

    sudo ./install.sh        # then use model  ags/gemini-4-hydrogen-medium

Files: `proxy.py` (127.0.0.1:20135, no key stored), `register-9router.py` (idempotent DB
registration + optional combo repoint), `install.sh`, `ag-sanitize-proxy.service` (reference unit;
install.sh writes its own).

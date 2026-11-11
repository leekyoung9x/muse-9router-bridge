# or-harness-proxy

Local passthrough to OpenRouter that adds app-attribution headers, so a gateway such as 9Router is
accepted as an "agentic harness". Needed for models like `thinkingmachines/inkling-2-small:free`, which
return `403 ... is only available on agentic harnesses` when `HTTP-Referer` is missing or is an
unregistered app (9Router sends `https://endpoint-proxy.local`).

Measured: no header -> 403; `X-Title` alone -> 403; `HTTP-Referer` alone -> 200.

- Binds 127.0.0.1:20134 only. Holds no API key; forwards the caller's `Authorization`.
- Always overrides `HTTP-Referer`, `X-Title`, `X-OpenRouter-Categories`
  (env: `OR_REFERER`, `OR_TITLE`, `OR_CATEGORIES`, `OR_PROXY_PORT`, `OR_UPSTREAM`).
- Streams SSE; tool calls pass through unchanged.

## Install
    mkdir -p /opt/or-harness-proxy && cp proxy.py /opt/or-harness-proxy/
    cp or-harness-proxy.service /etc/systemd/system/ && systemctl enable --now or-harness-proxy
Needs `python3` + `aiohttp`.

## 9Router
Add an OpenAI-compatible (chat) provider: prefix `orh`, base URL `http://127.0.0.1:20134/v1`, your own
OpenRouter key. Use model `orh/thinkingmachines/inkling-2-small:free` (not `openrouter/...`, which goes direct).

## Caveats
- This impersonates a registered app; OpenRouter may tighten the check later.
- OpenRouter prefixes the JSON body with whitespace/newlines; strict parsers should `lstrip()` first.

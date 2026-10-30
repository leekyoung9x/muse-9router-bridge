# Hướng dẫn nhanh (tiếng Việt)

Bản đầy đủ bằng tiếng Anh: [../README.md](../README.md) ·
[how-it-works.md](how-it-works.md) · [install.md](install.md) · [troubleshooting.md](troubleshooting.md)

## Cái này để làm gì

Meta chặn key **Muse Code subscription** khi request không "giống" client Muse Code:
trả `403 client_attestation_denied`. Ngoài ra khi đi qua gateway chat→Responses, tool call
thường bị mất tham số (`arguments: "{}"`), làm client gọi `read_file` với path rỗng rồi tự
phá vòng lặp bằng guardrail.

Repo này là 2 service Python nhỏ (không cần cài thêm gì) đứng cạnh 9router để sửa đúng 2 lỗi đó,
**không patch code 9router** — nên `npm update -g 9router` không xoá mất.

## Kiến trúc

```
Client thường (Hermes, [CC], curl)      Muse Code CLI
              │                                │
              │                    muse-gateway :20133   ← chỉ cho CLI
              │                                │
              └──────────────┬─────────────────┘
                             ▼
                      9router :20127
                             ▼
                    muse-adapter :20132
                             ▼
                       api.meta.ai
```

- `muse-adapter` (:20132) — **mọi** request đều đi qua. Sửa cổng 403, sửa tham số, vá tool args.
- `muse-gateway` (:20133) — chỉ Muse Code CLI cần, vì CLI hardcode key riêng và gọi
  `GET /muse-code/models` (ngoài `/v1`) mà 9router không có.

## Cài trong 4 bước

```bash
# 1) copy code
sudo mkdir -p /opt/muse-bridge && sudo cp adapter.py muse_gateway.py /opt/muse-bridge/

# 2) tạo 2 unit systemd (nội dung đầy đủ trong docs/install.md §3), rồi:
sudo systemctl daemon-reload && sudo systemctl enable --now muse-adapter muse-gateway

# 3) trỏ 9router vào adapter — sửa baseUrl của connection Muse:
#    http://127.0.0.1:20132/v1      (KHÔNG phải https://api.meta.ai/v1)
#    apiType phải là "responses", không phải "chat"

# 4) ghi key 9router vào file (đừng copy qua terminal, key sẽ bị cắt còn 14 ký tự):
python3 - <<'PY'
import os, sqlite3
c = sqlite3.connect("/root/ai-services/data-9router/db/data.sqlite")
k = c.execute("SELECT key FROM apiKeys WHERE isActive=1 ORDER BY createdAt LIMIT 1").fetchone()[0]
open("/opt/muse-bridge/9router.key","w").write(k+"\n"); os.chmod("/opt/muse-bridge/9router.key",0o600)
print("ok", len(k))
PY
```

## Kiểm tra

```bash
bash scripts/selftest.sh
```

Script đi qua từng lớp: service → port → adapter tới được Meta → adapter phá được 403 →
connection đã trỏ đúng → chat qua 9router → gateway list model → cache tool call.
Lớp nào fail là biết ngay lỗi ở đâu.

## Dùng

**Client thường** (Hermes, Claude Code, curl…) — không cần đổi gì, gọi thẳng 9router:

```bash
KEY=$(cat /opt/muse-bridge/9router.key)
curl http://127.0.0.1:20127/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"thtung-muse","messages":[{"role":"user","content":"hi"}],"max_tokens":512}'
```

**Muse Code CLI** — phải đi qua gateway:

```bash
muse exec --base-url http://127.0.0.1:20133/v1 \
          --model thtung-muse --contributor-mode on --reasoning-effort low \
          --workspace /path/repo 'prompt của bạn'
```

- `--model` là **tên combo 9router**, không phải id gốc (`muse-spark-1.3-contributor` sẽ báo
  `model muse-spark-1.3 does not exist`)
- thiếu `--contributor-mode on` thì CLI tự cắt hậu tố `-contributor`

## 5 lỗi thường gặp

| Lỗi | Nguyên nhân | Sửa |
|---|---|---|
| `403 client_attestation_denied` | request không đi qua adapter, hoặc adapter không bọc `namespace` | kiểm tra `baseUrl` connection = `http://127.0.0.1:20132/v1` |
| tool call `arguments: "{}"`, guardrail dừng turn | history không replay được `reasoning` + `function_call` gốc của Meta | `grep -c replayed adapter.log`; nếu 0 → mở session mới |
| `still unauthorized after a token refresh` (CLI) | `--base-url` trỏ 9router thay vì gateway | dùng `:20133` |
| `Invalid API key (requireApiKey=true)` | gateway đang gửi key Muse thay vì key 9router | kiểm tra `9router.key` đủ 35 ký tự |
| HTTP 200 nhưng content rỗng | model đốt hết budget vào reasoning | `max_tokens >= 512` |

## Lưu ý an toàn

- `9router.key` là secret: mode 600, đừng commit
- cả 2 service bind `127.0.0.1`; **không** mở `:20132` ra ngoài (nó chuyển tiếp key subscription của bạn)
- `MUSE_DUMP_DIR` chỉ dùng khi debug — nó ghi toàn bộ body request xuống đĩa
- cách này hoạt động vì Meta kiểm tra **hình dạng payload**, không phải chữ ký client. Nếu Meta
  chuyển sang ký thật thì phải làm lại cách khác

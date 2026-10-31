claude-adapter - proxy local giữa 9router và api.anthropic.com
Bỏ dấu hiệu Claude Code (beta claude-code-20250219, x-app, system prompt "You are Claude Code...")
để API key không bị lỗi "credit balance is too low".

Chạy:   node adapter.js        (PORT mặc định 20129, đổi bằng env PORT / UPSTREAM)
9router: provider Anthropic Compatible -> Base URL = http://127.0.0.1:20129/v1
Tự chạy khi bật máy: sửa đường dẫn trong claude-adapter.vbs rồi copy vào
  %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup

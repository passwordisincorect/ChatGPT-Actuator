[English](README.md) | [Tiếng Việt](README.vi.md)

# ChatGPT-Actuator 1.2.0

ChatGPT-Actuator là một Windows MCP actuator có lớp phân quyền, cho phép ChatGPT thao tác với Windows thông qua các capability được kiểm soát rõ ràng.

Phiên bản 1.1.0 là baseline stable hiện tại của kiến trúc Windows control-plane.

## Kiến trúc chạy

- Actuator chạy trên Windows desktop
- MCP qua stdio phía sau OpenAI Secure MCP Tunnel
- 73 MCP tools trong baseline v1.1.0 hiện tại (bản phát hành v1.0.0 có 45 tool)
- Admin UI cục bộ tại `http://127.0.0.1:8765/`
- tunnel profile: `chatgpt-actuator`
- health endpoint mặc định: `http://127.0.0.1:8081/healthz`

## Nhóm chức năng

- Filesystem
- Thông tin hệ thống
- Quản lý process
- PowerShell
- Chụp màn hình
- Quản lý cửa sổ
- Chuột
- Bàn phím
- Clipboard
- Nhập văn bản có xác minh
- Windows UI Automation
- Browser DOM qua Playwright + CDP chỉ trên loopback
- EDA inspection cùng KiCad ERC/DRC và Gerber export có guard

## Mô hình bảo mật

ChatGPT-Actuator đặt mọi thao tác phía sau các cổng quyền rõ ràng. Admin UI cục bộ có thể bật/tắt capability ngay khi đang chạy mà không cần restart tunnel.

Các lớp bảo vệ chính gồm:

- giới hạn filesystem theo allowed roots
- bảo vệ không cho xóa/di chuyển root được cấp quyền
- chặn path traversal và thoát root qua symlink/junction
- bảo vệ chuỗi process của actuator/tunnel
- bảo vệ các process Windows quan trọng
- giới hạn working root của PowerShell
- xác minh đúng cửa sổ đích trước khi gửi phím
- UI Automation semantic action gắn element reference với đúng cửa sổ
- Browser DOM có allowlist theo host và CDP endpoint chỉ cho loopback
- không đọc/ghi generic value trên password controls
- audit log không ghi plaintext nội dung bàn phím/clipboard nhạy cảm
- Admin UI chỉ bind loopback và dùng token ngẫu nhiên theo process

Nếu tab Admin cũ bị stale sau khi process restart, trang có thể tự reload và khôi phục các lựa chọn quyền chưa lưu để bạn kiểm tra lại.

### Cập nhật v1.1.0: UIA background-first, Browser DOM, routing và EDA

Source local hiện tại bổ sung năm semantic action: uia_scroll, uia_scroll_into_view, uia_set_range_value, uia_text_select_all và uia_window_action. Các UIA mutation tool hiện mặc định auto_focus_window=false, ưu tiên thao tác nền qua UI Automation. Những UIA action này không giả lập chuột hoặc bàn phím vật lý. Nhóm mouse/keyboard vẫn được giữ lại như fallback cần được gọi rõ ràng.

Baseline v1.1.0 có 14 browser DOM tool dùng Playwright attach vào Microsoft Edge qua CDP: dom_list_tabs, dom_find, dom_details, dom_read_text, dom_click, dom_set_value, dom_select, dom_toggle, dom_scroll_into_view, dom_scroll, dom_wait, dom_open_tab, dom_navigate và dom_close_tab. CDP hỗ trợ tự phát hiện qua file DevToolsActivePort cục bộ của Edge (khi bật remote debugging ở edge://inspect) hoặc dùng endpoint loopback chỉ định; quyền DOM tiếp tục bị giới hạn bằng allowed-host list. Các DOM action không dùng chuột hoặc bàn phím vật lý của Windows.

## Cài đặt

Thiết lập:

```powershell
.\scripts\setup.ps1
```

Tạo lại virtual environment:

```powershell
.\scripts\setup.ps1 -RecreateVenv
```

Kiểm tra trạng thái triển khai:

```powershell
.\scripts\deployment-status.ps1
```

Quản lý tunnel:

```powershell
.\scripts\tunnel-manager.ps1 -Action Status
.\scripts\tunnel-manager.ps1 -Action Start
.\scripts\tunnel-manager.ps1 -Action Stop
.\scripts\tunnel-manager.ps1 -Action Restart
```

Mở Admin UI:

```powershell
.\scripts\open-admin.ps1
```

## Tự khởi động cùng Windows

Autostart dùng Windows Task Scheduler khi người dùng đăng nhập. Dự án không dùng Windows Service vì các chức năng chuột, bàn phím, cửa sổ và UI Automation cần chạy trong interactive desktop session.

Bảo vệ runtime API key bằng Windows DPAPI:

```powershell
.\scripts\save-runtime-key.ps1
```

Cài và kiểm tra autostart:

```powershell
.\scripts\install-autostart.ps1
.\scripts\autostart-status.ps1
```

Gỡ autostart:

```powershell
.\scripts\remove-autostart.ps1
```

Xóa riêng credential đã được DPAPI bảo vệ:

```powershell
.\scripts\remove-runtime-key.ps1
```

`save-runtime-key.ps1` không ghi plaintext API key xuống ổ đĩa.

## Gói release

Build:

```powershell
.\scripts\build-release.ps1
```

File ZIP bootstrap không phải ứng dụng standalone. Nó cần Windows và Python 3.12 x64. Kết nối OpenAI cần tunnel-client/profile; kết nối Tailscale cần Tailscale và phụ thuộc gateway tùy chọn.

Release builder:

- tạo ZIP deterministic
- build Python wheel
- tạo `SHA256SUMS.txt`
- tạo `release-manifest.json`
- kiểm tra không đóng gói credential/state runtime cục bộ
- dùng cấu hình safe-default trong artifact
- loại metadata build cục bộ như `*.egg-info`

### Cấu hình mặc định an toàn

Khi giải nén một package mới:

- filesystem ở chế độ read-only và được giới hạn vào thư mục project sau setup
- cho phép xem danh sách process nhưng tắt start/stop/force-kill
- PowerShell bị tắt
- screen capture bị tắt
- cho phép xem thông tin cửa sổ nhưng tắt thao tác thay đổi
- mouse, keyboard và clipboard bị tắt
- verified input bị tắt
- cho phép UI Automation discovery nhưng tắt semantic actions
- Browser DOM có mặt nhưng release artifact mặc định tắt quyền đọc/mutation/navigation, tắt auto-launch và để allowed hosts rỗng
- EDA chỉ cho phép inspection read-only trong root của project; edit/manufacturing bị tắt và đường dẫn công cụ ngoài được để trống
- Admin UI chỉ chạy trên loopback

Sau đó bạn có thể chủ động bật từng quyền cần thiết trong Admin UI.

## Kiểm tra release

Chạy:

```powershell
.\scripts\verify-release.ps1
```

Verifier kiểm tra:

1. cú pháp PowerShell
2. compile Python
3. tính nhất quán dependency với pip
4. toàn bộ regression tests, gồm OAuth/bảo mật và tích hợp gateway HTTP trên Windows với `ResourceWarning` được coi là lỗi
5. smoke test MCP/Admin runtime độc lập
6. khả năng build ZIP và wheel reproducible
7. build artifact release cuối

Số MCP tool kỳ vọng trong baseline v1.1.0 hiện tại: **73**.

Trước khi phát hành v1.0.0, release candidate cũng đã vượt qua test end-to-end thực tế theo đường ChatGPT → Secure MCP Tunnel → ChatGPT-Actuator → Windows, gồm chọn tab bằng UI Automation, verified text replacement/read-back, xử lý dialog và autostart sau reboot.

## Giấy phép

Dự án được phát hành theo [MIT License](LICENSE).

## Đóng góp và an toàn

Nên giữ quyền điều khiển Windows ở mức hạn chế mặc định. Không commit credential cục bộ, file DPAPI, log, deployment state hoặc tunnel secret vào repository.


## Profile kết nối và gateway Tailscale tùy chọn

Cùng một mã công cụ trong src/chatgpt_actuator phục vụ OpenAI Secure MCP Tunnel và Tailscale Funnel. Gateway OAuth tùy chọn nằm trong integrations/tailscale_gateway; không sao chép các công cụ hoặc thay đổi cấu hình OpenAI Tunnel hiện tại.

| Phương thức | Điểm kết nối ChatGPT | Thành phần cục bộ |
| --- | --- | --- |
| openai_tunnel | OpenAI Secure MCP Tunnel | Profile stdio hiện có |
| tailscale_funnel | https://HOST/full/mcp | Actuator HTTP 127.0.0.1:8090, gateway OAuth 127.0.0.1:8188, trang duyệt 127.0.0.1:8189, Actuator Admin 127.0.0.1:8766 |

Cài trên máy Windows khác:

1. Cài Python 3.12 x64 và Tailscale, đăng nhập Tailscale, rồi chạy `scripts/setup.ps1 -Tailscale`. Phương thức này không cần OpenAI Tunnel client, API key hay tunnel profile.
2. Sao chép config/connection-profiles.example.json thành config/connection-profiles.local.json. Thay issuer bằng hostname HTTPS do Tailscale Funnel cung cấp. Giữ backend và trang duyệt trên loopback. Các profile gateway chạy đồng thời cần cổng và thư mục trạng thái riêng.
3. Ở một cửa sổ PowerShell, chạy .\scripts\start-actuator-http.ps1 -Profile tailscale. Actuator hiện có sẽ chạy ở chế độ HTTP trên cổng 8090. Nếu đã có một Actuator HTTP trên cổng đó, dùng tiến trình đang chạy hoặc dừng nó trước.
4. Ở cửa sổ khác, chạy .\scripts\gateway-profile.ps1 -Action Validate -Profile tailscale, sau đó .\scripts\gateway-profile.ps1 -Action Run -Profile tailscale. Kiểm tra bằng -Action Status. Duyệt OAuth tại http://127.0.0.1:8189/ trên máy Windows.
5. Chủ động công bố gateway bằng .\scripts\gateway-profile.ps1 -Action EnableFunnel -Profile tailscale. Kết nối ChatGPT với tài nguyên /full/mcp và các scope mcp:read mcp:write mcp:execute. Chỉ cổng 8188 được công bố.

Profile OpenAI hiện có chạy bằng .\scripts\gateway-profile.ps1 -Action Run -Profile openai; lệnh này gọi tunnel-manager.ps1 sau khi bạn đã thiết lập OpenAI Tunnel.

OAuth luôn cần chủ máy duyệt kết nối. Profile mẫu còn yêu cầu duyệt cục bộ từng lời gọi không thuộc nhóm đọc. Sau khi đánh giá, bạn có thể đặt per_call_approval thành false trong profile riêng. Lệnh Actuator chạy với quyền tài khoản Windows; gateway không tạo sandbox. Số lượng công cụ do backend và cấu hình quyền đang chạy quyết định. Đường dẫn /orch/mcp cần một backend Orchestrator HTTP riêng và không được các script này tự khởi động. Profile riêng và dữ liệu OAuth không được đưa vào Git.

Chỉ chuyển gateway OAuth lên Funnel; không công bố backend HTTP hoặc trang duyệt. Cổng backend được lấy từ profile; `actuator_admin_port` mặc định 8766 để có thể chạy cùng OpenAI Tunnel. Các biến môi trường HTTP chỉ áp dụng cho tiến trình mới. Gateway không thay thế cấu hình quyền Actuator.

Dừng: dùng Ctrl+C trong hai cửa sổ chạy backend/gateway. Muốn tắt Funnel, dùng `tailscale funnel --https=443 off` (hoặc cổng HTTPS đã chọn); lệnh này ảnh hưởng mọi đường dẫn trên cổng đó. Không dùng `funnel reset` khi còn dịch vụ khác. Để quay lại bản cũ, checkout tag/commit trước đó; giữ profile và thư mục trạng thái riêng, không khôi phục token đã thu hồi.

CI chạy trên mỗi PR: kiểm thử bảo mật trên Linux, hồi quy và gateway HTTP thật trên Windows, rồi build/kiểm tra tính tái lập ZIP và wheel. Kiểm thử loopback không thay thế việc kết nối và duyệt OAuth từ ChatGPT qua Funnel trên máy người cài.

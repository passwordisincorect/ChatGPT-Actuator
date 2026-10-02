[English](README.md) | [Tiếng Việt](README.vi.md)

# ChatGPT-Actuator 1.0.0

ChatGPT-Actuator là một Windows MCP actuator có lớp phân quyền, cho phép ChatGPT thao tác với Windows thông qua các capability được kiểm soát rõ ràng.

Phiên bản 1.0.0 là bản stable đầu tiên của kiến trúc Windows control-plane hiện tại.

## Kiến trúc chạy

- Actuator chạy trên Windows desktop
- MCP qua stdio phía sau OpenAI Secure MCP Tunnel
- 45 MCP tools
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
- không đọc/ghi generic value trên password controls
- audit log không ghi plaintext nội dung bàn phím/clipboard nhạy cảm
- Admin UI chỉ bind loopback và dùng token ngẫu nhiên theo process

Nếu tab Admin cũ bị stale sau khi process restart, trang có thể tự reload và khôi phục các lựa chọn quyền chưa lưu để bạn kiểm tra lại.

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

File ZIP bootstrap không phải ứng dụng standalone. Nó cần Windows, Python 3.12 x64, OpenAI tunnel-client và tunnel profile đã được cấp quyền.

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
4. toàn bộ 39 regression tests với `ResourceWarning` được coi là lỗi
5. smoke test MCP/Admin runtime độc lập
6. khả năng build ZIP và wheel reproducible
7. build artifact release cuối

Số MCP tool kỳ vọng: **45**.

Trước khi phát hành v1.0.0, release candidate cũng đã vượt qua test end-to-end thực tế theo đường ChatGPT → Secure MCP Tunnel → ChatGPT-Actuator → Windows, gồm chọn tab bằng UI Automation, verified text replacement/read-back, xử lý dialog và autostart sau reboot.

## Giấy phép

Dự án được phát hành theo [MIT License](LICENSE).

## Đóng góp và an toàn

Nên giữ quyền điều khiển Windows ở mức hạn chế mặc định. Không commit credential cục bộ, file DPAPI, log, deployment state hoặc tunnel secret vào repository.

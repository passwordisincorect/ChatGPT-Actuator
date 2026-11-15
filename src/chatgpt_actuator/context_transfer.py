"""Local opt-in profile extraction from a verified ChatGPT Main/Worker account.

The Control Center starts one background job per explicit user click. Only the
existing DOM broker is used. No share links or OpenAI Export Data endpoints.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import re
import secrets
import threading
import time

PROMPT_MARKER = "AI_CONTEXT_TRANSFER_2026_V1"
END_MARKER = "HOAN_TAT_HO_SO_NGU_CANH"
PROMPT = """Hãy xây dựng bộ hồ sơ ngữ cảnh cá nhân để chuyển sang một tài khoản ChatGPT khác.

Chỉ sử dụng Memory, lịch sử trò chuyện liên quan, tài liệu và ngữ cảnh mà bạn thực sự có thể truy cập. Không khẳng định đã đọc toàn bộ lịch sử; không suy đoán các sự kiện, trạng thái test, phiên bản hoặc đường dẫn. Phân biệt thông tin đã được xác minh, thông tin chưa xác minh và dữ liệu có thể đã cũ.

Yêu cầu:
1. Hồ sơ cá nhân: lĩnh vực, công việc/học tập, mục tiêu, kiến thức và các ưu tiên.
2. Phong cách giao tiếp, cách giải quyết vấn đề và những quy tắc AI cần tuân thủ.
3. Memory/ngữ cảnh dài hạn đáng giữ; không nhầm lẫn với trạng thái dự án có thể thay đổi.
4. Các Projects quan trọng: mục tiêu, kiến trúc, tiến độ, quyết định, lỗi đã sửa, việc còn tồn đọng; ghi mốc thời gian và nguồn nếu biết.
5. Những cuộc trò chuyện quan trọng nên tiếp tục: chủ đề, quyết định, bước tiếp theo; không cần chép hết hội thoại.
6. Kỹ năng, phần kiến thức đã học và bằng chứng; không đoán trình độ.
7. Thông tin thiếu/mâu thuẫn/cần lấy lại từ cuộc trò chuyện hoặc file gốc.
8. Không xuất mật khẩu, cookie, token, khóa API, mã xác thực hoặc dữ liệu cá nhân nhạy cảm không cần thiết.

Kết quả BẰNG MARKDOWN, theo cấu trúc:
# Hồ sơ cá nhân
# Phong cách và hướng dẫn AI
# Memory và ngữ cảnh dài hạn
# Projects và trạng thái
# Kiến thức và kỹ năng
# Hội thoại cần tiếp tục
# Thông tin thiếu / chưa xác minh
# Hướng dẫn sử dụng trong tài khoản mới

Kết thúc bằng dòng: HOAN_TAT_HO_SO_NGU_CANH
Mã yêu cầu: AI_CONTEXT_TRANSFER_2026_V1
"""
COMPOSERS = ["#prompt-textarea", '[data-testid="composer-input"]',
             'div[contenteditable="true"]', "textarea"]
SEND_BUTTONS = ['[data-testid="send-button"]', 'button[aria-label="Send prompt"]',
                'button[aria-label="Send"]', 'button[aria-label="Gửi"]']
ASSISTANT_SELECTOR = '[data-message-author-role="assistant"]'
MAX_RESPONSE = 58_000


def validate_identity(report: dict) -> str:
    identity = report.get("identity_detection") or {}
    main = identity.get("main") or {}
    worker = identity.get("worker") or {}
    if identity.get("same_login_detected") is not True:
        raise RuntimeError("Main/Worker chưa được xác minh cùng một tài khoản. Chạy 'Kiểm tra hệ thống' trước.")
    if not main.get("authenticated") or not worker.get("authenticated"):
        raise RuntimeError("Chưa xác minh được đăng nhập cả Main và Worker. Không gửi prompt.")
    return str(worker.get("email") or worker.get("label")
               or report.get("active_account_id") or "verified-account")[:120]


def find_first(call, tab_ref: str, selectors: list[str]) -> str | None:
    for selector in selectors:
        result = call("find", {"tab_ref": tab_ref, "selector": selector, "max_items": 1})
        for item in result.get("matches") or []:
            if item.get("element_ref"):
                return str(item["element_ref"])
    return None


def read_assistant(call, tab_ref: str) -> str:
    found = call("find", {"tab_ref": tab_ref, "selector": ASSISTANT_SELECTOR,
                          "max_items": 10})
    matches = found.get("matches") or []
    if not matches:
        return ""
    element = matches[-1].get("element_ref")
    if not element:
        return ""
    read = call("read_text", {"element_ref": element, "max_chars": MAX_RESPONSE})
    return str(read.get("text") or "").strip()


class ContextTransferManager:
    def __init__(self, output_root: Path, *, sleeper=time.sleep, clock=time.monotonic):
        self.output_root = Path(output_root)
        self.sleep = sleeper
        self.clock = clock
        self._lock = threading.Lock()
        self._job = {"phase": "idle"}
        self._thread = None

    def status(self) -> dict:
        with self._lock:
            return {k: v for k, v in self._job.items() if k in {
                "run_id", "phase", "message", "account", "characters",
                "conversation_url", "created_at", "finished_at", "quality"}}

    def _set(self, run_id: str, **data) -> None:
        with self._lock:
            if self._job.get("run_id") == run_id:
                self._job.update(data)

    def start(self, *, call, detect_identity) -> dict:
        with self._lock:
            if self._job.get("phase") in {
                "checking_account", "preparing", "sending", "generating", "saving"}:
                raise RuntimeError("Đang chạy một tác vụ chuyển ngữ cảnh.")
            run_id = secrets.token_hex(10)
            self._job = {"run_id": run_id, "phase": "checking_account",
                         "created_at": datetime.now(timezone.utc).isoformat(),
                         "message": "Đang xác minh Main/Worker."}
            self._thread = threading.Thread(target=self._run, daemon=True,
                name="context-transfer-" + run_id,
                kwargs={"run_id": run_id, "call": call, "detect_identity": detect_identity})
            self._thread.start()
            return dict(self._job)

    def _run(self, *, run_id: str, call, detect_identity) -> None:
        sent = False
        try:
            account = validate_identity(detect_identity())
            self._set(run_id, phase="preparing", account=account,
                      message="Đã xác minh cùng tài khoản. Đang mở chat mới.")
            opened = call("open_tab", {"url": "https://chatgpt.com/", "timeout_ms": 30000})
            tab_ref = str(opened.get("tab_ref") or "")
            if not tab_ref:
                raise RuntimeError("DOM Broker không tạo được tab ChatGPT.")
            composer = None
            for _ in range(18):
                composer = find_first(call, tab_ref, COMPOSERS)
                if composer:
                    break
                self.sleep(0.6)
            if not composer:
                raise RuntimeError("Không thấy ô soạn thảo ChatGPT; chưa gửi prompt.")
            call("set_value", {"element_ref": composer, "value": PROMPT,
                               "timeout_ms": 30000})
            check = call("read_text", {"element_ref": composer,
                                       "max_chars": len(PROMPT) + 500})
            if PROMPT_MARKER not in str(check.get("text") or ""):
                raise RuntimeError("Nội dung trong ô soạn thảo không khớp; chưa gửi.")
            send = find_first(call, tab_ref, SEND_BUTTONS)
            if not send:
                raise RuntimeError("Không tìm thấy nút Gửi; chưa gửi prompt.")
            self._set(run_id, phase="sending", message="Đang gửi prompt.")
            # Do not retry or resend on an ambiguous failure after this point.
            sent = True
            call("click", {"element_ref": send, "timeout_ms": 30000})
            self._set(run_id, phase="generating", message="Đang kiểm tra câu trả lời AI.")
            start = self.clock()
            previous, stable, answer = "", 0, ""
            while self.clock() - start < 240:
                self.sleep(2)
                current = read_assistant(call, tab_ref)
                stable = stable + 1 if current and current == previous else 0
                previous = current
                if END_MARKER in current and stable >= 2 and len(current) >= 500:
                    answer = current
                    break
            if not answer:
                raise RuntimeError("Chưa xác minh câu trả lời đã hoàn tất; không xuất file. "
                                   "Kiểm tra tab ChatGPT vừa mở.")
            if len(answer) >= MAX_RESPONSE - 1000:
                raise RuntimeError("Câu trả lời vượt mức thu thập an toàn; không xuất file.")
            self._set(run_id, phase="saving", message="Đang tạo Markdown và JSON cục bộ.")
            folder = self.output_root / "private" / "context-transfer"
            folder.mkdir(parents=True, exist_ok=True)
            when = datetime.now(timezone.utc).isoformat()
            header = ("# Hồ sơ chuyển ngữ cảnh ChatGPT\n\n"
                      "> Đây là bản tổng hợp do AI tạo; không phải Memory hoặc lịch sử "
                      "đã được xác minh đầy đủ.\n\n"
                      f"- Tạo lúc (UTC): {when}\n"
                      f"- Mã: {run_id}\n\n---\n\n")
            markdown = header + answer + "\n"
            payload = {"schema": "context-transfer-v1", "run_id": run_id,
                       "created_at": when, "source": "chatgpt-live-response",
                       "quality": "ai-generated-unverified", "markdown": markdown}
            md = folder / ("AI-Context-Transfer-" + run_id + ".md")
            js = folder / ("AI-Context-Transfer-" + run_id + ".json")
            with md.open("x", encoding="utf-8") as handle:
                handle.write(markdown)
            with js.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            url = str(opened.get("url") or "https://chatgpt.com/")
            try:
                tabs = call("list_tabs", {})
                for item in tabs.get("tabs") or []:
                    if item.get("tab_ref") == tab_ref:
                        url = str(item.get("url") or url)
                        break
            except Exception:
                pass
            self._set(run_id, phase="complete", finished_at=when,
                      quality="ai-generated-unverified", characters=len(answer),
                      conversation_url=url,
                      message="Đã tạo Markdown/JSON. Hãy kiểm tra nội dung trước khi chuyển.")
        except Exception as exc:
            message = str(exc)[:400]
            if sent:
                message += " Prompt có thể đã gửi; hệ thống KHÔNG tự gửi lại."
            self._set(run_id, phase="error", message=message,
                      finished_at=datetime.now(timezone.utc).isoformat())

    def download(self, run_id: str, fmt: str) -> tuple[bytes, str, str]:
        if fmt not in ("md", "json") or not re.fullmatch(r"[a-f0-9]{20}", run_id or ""):
            raise ValueError("Mã hoặc định dạng không hợp lệ.")
        with self._lock:
            if self._job.get("run_id") != run_id or self._job.get("phase") != "complete":
                raise ValueError("Chưa có file cho tác vụ hoàn tất này.")
        path = (self.output_root / "private" / "context-transfer" /
                ("AI-Context-Transfer-" + run_id + "." + fmt))
        data = path.read_bytes()
        if not data or len(data) > 400_000:
            raise ValueError("File trống hoặc quá lớn.")
        return data, path.name, ("application/json" if fmt == "json" else "text/markdown")

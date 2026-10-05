from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import threading
from typing import Any
from urllib.parse import urlparse


_SESSION_JS = r"""
async () => {
  try {
    const response = await fetch('/api/auth/session', {
      credentials: 'include',
      cache: 'no-store'
    });
    let payload = null;
    try { payload = await response.json(); } catch (_e) {}
    const user = payload && payload.user ? payload.user : null;
    const name = user && user.name ? String(user.name) : null;
    const email = user && user.email ? String(user.email) : null;
    return {
      http_status: Number(response.status || 0),
      authenticated: Boolean(user && (email || name)),
      name,
      email
    };
  } catch (error) {
    return {
      http_status: 0,
      authenticated: false,
      name: null,
      email: null,
      error: String(error)
    };
  }
}
"""


class ChatGPTIdentityDetector:
    """Read the signed-in ChatGPT identity without reading browser cookies.

    Detection executes a tiny same-origin fetch inside an already-running Edge
    tab over CDP. Only name/email/authenticated status are returned to Python.
    Session cookies, access tokens, and the raw session payload are never copied.

    Main Edge is strictly read-only: no tab is opened, focused, navigated, or
    closed. Worker Edge may use one temporary background ChatGPT tab when no
    ChatGPT tab exists, because the worker browser is the dedicated automation
    browser and is kept minimized/offscreen.
    """

    def __init__(self, main_user_data_dir: Path | None = None) -> None:
        if main_user_data_dir is None:
            local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
            if local_app_data:
                main_user_data_dir = Path(local_app_data) / "Microsoft" / "Edge" / "User Data"
            else:
                main_user_data_dir = Path.home() / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data"
        self.main_user_data_dir = Path(main_user_data_dir).expanduser().resolve()
        self._lock = threading.RLock()
        self._cache: dict[str, dict[str, Any]] = {
            "main": self._unknown("not_checked"),
            "worker": self._unknown("not_checked"),
        }

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @classmethod
    def _unknown(cls, state: str, detail: str | None = None) -> dict[str, Any]:
        return {
            "state": state,
            "authenticated": False,
            "name": None,
            "email": None,
            "detected_at": None,
            "detail": detail,
        }

    @staticmethod
    def _safe_identity(value: Any, *, state: str, detected_at: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {
                "state": "error",
                "authenticated": False,
                "name": None,
                "email": None,
                "detected_at": detected_at,
                "detail": "ChatGPT session response was not an object.",
            }
        authenticated = bool(value.get("authenticated"))
        name = value.get("name")
        email = value.get("email")
        if name is not None:
            name = str(name).strip() or None
        if email is not None:
            email = str(email).strip() or None
        if authenticated:
            final_state = "signed_in"
        elif int(value.get("http_status") or 0) in {200, 204}:
            final_state = "signed_out"
        else:
            final_state = state
        return {
            "state": final_state,
            "authenticated": authenticated,
            "name": name,
            "email": email,
            "detected_at": detected_at,
            "detail": str(value.get("error") or "").strip() or None,
        }

    @staticmethod
    def _cdp_endpoint(user_data_dir: Path) -> str:
        active_port = user_data_dir / "DevToolsActivePort"
        if not active_port.is_file():
            raise FileNotFoundError(f"DevToolsActivePort not found: {active_port}")
        lines = active_port.read_text(encoding="utf-8").splitlines()
        if not lines:
            raise RuntimeError(f"Invalid DevToolsActivePort file: {active_port}")
        port = int(lines[0].strip())
        if not 1 <= port <= 65535:
            raise RuntimeError(f"Invalid DevToolsActivePort port: {port}")
        if len(lines) >= 2:
            path = lines[1].strip()
            if path.startswith("/devtools/browser/"):
                return f"ws://127.0.0.1:{port}{path}"
        return f"http://127.0.0.1:{port}"

    @staticmethod
    def _is_chatgpt_page(url: str) -> bool:
        try:
            host = (urlparse(url).hostname or "").casefold()
        except Exception:
            return False
        return host == "chatgpt.com" or host.endswith(".chatgpt.com")

    def _detect_via_cdp(
        self,
        user_data_dir: Path,
        *,
        allow_temporary_tab: bool,
        timeout_ms: int,
    ) -> dict[str, Any]:
        detected_at = self._now()
        try:
            endpoint = self._cdp_endpoint(user_data_dir)
        except FileNotFoundError as exc:
            return {
                **self._unknown("cdp_unavailable", str(exc)),
                "detected_at": detected_at,
            }
        except Exception as exc:
            return {
                **self._unknown("cdp_error", str(exc)),
                "detected_at": detected_at,
            }

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return {
                **self._unknown("playwright_unavailable", "Playwright is not installed."),
                "detected_at": detected_at,
            }

        playwright = None
        temporary_page = None
        try:
            playwright = sync_playwright().start()
            browser = playwright.chromium.connect_over_cdp(
                endpoint,
                timeout=max(1500, int(timeout_ms)),
            )
            pages = [
                page
                for context in browser.contexts
                for page in context.pages
                if self._is_chatgpt_page(page.url)
            ]
            page = pages[0] if pages else None

            if page is None and allow_temporary_tab:
                if not browser.contexts:
                    return {
                        **self._unknown("no_browser_context", "No Edge browser context is available."),
                        "detected_at": detected_at,
                    }
                temporary_page = browser.contexts[0].new_page()
                temporary_page.goto(
                    "https://chatgpt.com/",
                    wait_until="domcontentloaded",
                    timeout=max(5000, int(timeout_ms) * 2),
                )
                page = temporary_page

            if page is None:
                return {
                    **self._unknown(
                        "no_chatgpt_tab",
                        "No existing chatgpt.com tab is available in Main Edge.",
                    ),
                    "detected_at": detected_at,
                }

            result = page.evaluate(_SESSION_JS)
            return self._safe_identity(result, state="session_unavailable", detected_at=detected_at)
        except Exception as exc:
            message = str(exc)
            lowered = message.casefold()
            if any(token in lowered for token in ("403 forbidden", "connection rejected", "remote debugging")):
                state = "authorization_required"
                detail = (
                    "Edge rejected the CDP connection. If Edge shows 'Allow remote debugging?', "
                    "approve it manually and run detection again."
                )
            elif "timeout" in lowered:
                state = "authorization_required"
                detail = (
                    "CDP connection timed out. Main Edge may be waiting for the "
                    "'Allow remote debugging?' confirmation."
                )
            else:
                state = "cdp_error"
                detail = message[:500]
            return {
                **self._unknown(state, detail),
                "detected_at": detected_at,
            }
        finally:
            if temporary_page is not None:
                try:
                    temporary_page.close()
                except Exception:
                    pass
            # Stopping Playwright disconnects the client. Do not call browser.close()
            # because this detector must never close the user's Edge browser.
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    pass

    def detect_main(self) -> dict[str, Any]:
        with self._lock:
            result = self._detect_via_cdp(
                self.main_user_data_dir,
                allow_temporary_tab=False,
                timeout_ms=5000,
            )
            self._cache["main"] = result
            return dict(result)

    def detect_worker(self, user_data_dir: Path) -> dict[str, Any]:
        with self._lock:
            result = self._detect_via_cdp(
                Path(user_data_dir).expanduser().resolve(),
                allow_temporary_tab=True,
                timeout_ms=8000,
            )
            self._cache["worker"] = result
            return dict(result)

    def clear_worker(self) -> None:
        with self._lock:
            self._cache["worker"] = self._unknown("not_checked")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            main = dict(self._cache["main"])
            worker = dict(self._cache["worker"])
            same: bool | None = None
            main_email = str(main.get("email") or "").strip().casefold()
            worker_email = str(worker.get("email") or "").strip().casefold()
            if main.get("authenticated") and worker.get("authenticated"):
                if main_email and worker_email:
                    same = main_email == worker_email
            return {
                "main": main,
                "worker": worker,
                "same_login_detected": same,
            }

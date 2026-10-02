from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import tempfile
import tomllib
import urllib.request

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


def expected_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def payload(result) -> dict:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            value = json.loads(text)
        except Exception:
            continue
        if isinstance(value, dict):
            return value
    return {}


def http_json(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=2) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


async def wait_health(port: int, timeout: float = 5.0) -> tuple[int, dict]:
    deadline = asyncio.get_running_loop().time() + timeout
    last_error = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            return await asyncio.to_thread(
                http_json,
                f"http://127.0.0.1:{port}/healthz",
            )
        except Exception as exc:
            last_error = repr(exc)
            await asyncio.sleep(0.1)
    raise RuntimeError(f"Isolated Admin health did not become ready: {last_error}")


async def verify() -> dict:
    version = expected_version()
    port = free_port()

    with tempfile.TemporaryDirectory(prefix="chatgpt-actuator-rc-") as td:
        temp = Path(td)
        raw = json.loads((ROOT / "config" / "config.json").read_text(encoding="utf-8"))
        raw["server"]["version"] = version
        raw["admin"]["host"] = "127.0.0.1"
        raw["admin"]["port"] = port
        raw["logging"]["enabled"] = True
        raw["logging"]["file"] = str(temp / "audit.log")
        raw["filesystem"]["allowed_roots"] = [str(ROOT)]
        raw["powershell"]["default_cwd"] = str(ROOT)
        raw["powershell"]["allowed_working_roots"] = [str(ROOT)]

        config_path = temp / "config.json"
        config_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["CHATGPT_ACTUATOR_CONFIG"] = str(config_path)

        params = StdioServerParameters(
            command=str(PYTHON),
            args=["-m", "chatgpt_actuator.server"],
            cwd=str(ROOT),
            env=env,
        )

        result = {
            "ok": False,
            "expected_version": version,
            "admin_port": port,
        }

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                tools = await session.list_tools()
                result["server_version"] = init.server_info.version
                result["tool_count"] = len(tools.tools)
                result["tool_names"] = sorted(tool.name for tool in tools.tools)

                roots_result = await session.call_tool("filesystem_list_roots", {})
                sys_result = await session.call_tool("system_info", {})
                result["filesystem_roots"] = payload(roots_result).get("allowed_roots")
                result["system_hostname_present"] = bool(payload(sys_result).get("hostname"))

                status, health = await wait_health(port)
                result["admin_health_status"] = status
                result["admin_health"] = health

                result["ok"] = (
                    result["server_version"] == version
                    and result["tool_count"] == 45
                    and result["filesystem_roots"] == [str(ROOT)]
                    and result["system_hostname_present"] is True
                    and status == 200
                    and health.get("status") == "ok"
                    and health.get("version") == version
                )

        return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = asyncio.run(verify())
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Version:      {result.get('server_version')}")
        print(f"Tool count:   {result.get('tool_count')}")
        print(f"Admin health: {result.get('admin_health_status') == 200}")
        print(f"Result:       {'PASS' if result.get('ok') else 'FAIL'}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

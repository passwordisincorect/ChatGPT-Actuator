from __future__ import annotations

import asyncio
import inspect
from time import perf_counter
from typing import Any, Callable, Mapping, Sequence


MAX_BATCH_ACTIONS = 25
MAX_PARALLEL_ACTIONS = 8

# Deliberately conservative: only side-effect-free services that are safe to
# execute concurrently in worker threads are allowed in parallel mode.
PARALLEL_READ_ONLY_TOOLS = frozenset(
    {
        "filesystem_list_roots",
        "filesystem_list",
        "filesystem_read_text",
        "filesystem_stat",
        "filesystem_search",
        "system_info",
        "system_resource_snapshot",
        "process_list",
        "process_details",
        "powershell_info",
        "screen_info",
        "clipboard_read_text",
    }
)

Handler = Callable[..., Any]


def _normalize_actions(
    actions: Sequence[dict],
    handlers: Mapping[str, Handler],
) -> list[tuple[int, str, dict]]:
    if not isinstance(actions, (list, tuple)):
        raise TypeError("actions must be a list.")
    if not actions:
        raise ValueError("actions must contain at least one action.")
    if len(actions) > MAX_BATCH_ACTIONS:
        raise ValueError(f"actions cannot contain more than {MAX_BATCH_ACTIONS} items.")

    normalized: list[tuple[int, str, dict]] = []
    for index, raw in enumerate(actions):
        if not isinstance(raw, dict):
            raise TypeError(f"actions[{index}] must be an object.")

        tool = raw.get("tool")
        arguments = raw.get("arguments", {})
        if not isinstance(tool, str) or not tool.strip():
            raise ValueError(f"actions[{index}].tool must be a non-empty string.")
        tool = tool.strip()
        if tool == "batch_execute":
            raise ValueError("Nested batch_execute calls are not allowed.")
        if tool not in handlers:
            raise ValueError(f"Tool is not batch-enabled: {tool}")
        if not isinstance(arguments, dict):
            raise TypeError(f"actions[{index}].arguments must be an object.")

        normalized.append((index, tool, dict(arguments)))
    return normalized


def _contains_reference(value: Any) -> bool:
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            return True
        return any(_contains_reference(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_reference(item) for item in value)
    return False


def _resolve_reference(reference: str, prior_results: list[dict]) -> Any:
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError("$ref must be a non-empty string.")

    parts = reference.split(".")
    try:
        result_index = int(parts[0])
    except ValueError as exc:
        raise ValueError("$ref must start with a prior action index.") from exc

    if result_index < 0 or result_index >= len(prior_results):
        raise ValueError(f"$ref points to unavailable action index {result_index}.")

    value: Any = prior_results[result_index]
    if not value.get("ok", False):
        raise ValueError(f"$ref points to failed action index {result_index}.")

    for part in parts[1:]:
        if isinstance(value, dict):
            if part not in value:
                raise ValueError(f"$ref path component not found: {part}")
            value = value[part]
        elif isinstance(value, (list, tuple)):
            try:
                list_index = int(part)
                value = value[list_index]
            except (ValueError, IndexError) as exc:
                raise ValueError(f"Invalid $ref list index: {part}") from exc
        else:
            raise ValueError(f"$ref cannot traverse through {type(value).__name__}.")
    return value


def _resolve_value(value: Any, prior_results: list[dict]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            return _resolve_reference(value["$ref"], prior_results)
        return {key: _resolve_value(item, prior_results) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_value(item, prior_results) for item in value]
    if isinstance(value, tuple):
        return tuple(_resolve_value(item, prior_results) for item in value)
    return value


async def _invoke(handler: Handler, arguments: dict, *, use_thread: bool) -> Any:
    if inspect.iscoroutinefunction(handler):
        return await handler(**arguments)

    if use_thread:
        result = await asyncio.to_thread(handler, **arguments)
    else:
        result = handler(**arguments)

    if inspect.isawaitable(result):
        return await result
    return result


async def _run_one(
    index: int,
    tool: str,
    arguments: dict,
    handler: Handler,
    *,
    use_thread: bool,
) -> dict:
    started = perf_counter()
    try:
        value = await _invoke(handler, arguments, use_thread=use_thread)
        return {
            "index": index,
            "tool": tool,
            "ok": True,
            "duration_ms": round((perf_counter() - started) * 1000, 3),
            "result": value,
        }
    except Exception as exc:
        return {
            "index": index,
            "tool": tool,
            "ok": False,
            "duration_ms": round((perf_counter() - started) * 1000, 3),
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }


async def execute_batch(
    actions: Sequence[dict],
    handlers: Mapping[str, Handler],
    *,
    stop_on_error: bool = True,
    mode: str = "sequential",
) -> dict:
    normalized = _normalize_actions(actions, handlers)
    requested_mode = str(mode).strip().casefold()
    if requested_mode not in {"sequential", "parallel_readonly"}:
        raise ValueError("mode must be 'sequential' or 'parallel_readonly'.")

    batch_started = perf_counter()

    if requested_mode == "parallel_readonly":
        disallowed = sorted(
            {tool for _, tool, _ in normalized if tool not in PARALLEL_READ_ONLY_TOOLS}
        )
        if disallowed:
            raise PermissionError(
                "parallel_readonly accepts only conservative read-only tools; "
                "not allowed: " + ", ".join(disallowed)
            )
        if any(_contains_reference(arguments) for _, _, arguments in normalized):
            raise ValueError("$ref dependencies require sequential mode.")

        semaphore = asyncio.Semaphore(MAX_PARALLEL_ACTIONS)

        async def guarded(item: tuple[int, str, dict]) -> dict:
            index, tool, arguments = item
            async with semaphore:
                return await _run_one(
                    index,
                    tool,
                    arguments,
                    handlers[tool],
                    use_thread=True,
                )

        results = list(await asyncio.gather(*(guarded(item) for item in normalized)))
        results.sort(key=lambda item: item["index"])
    else:
        results: list[dict] = []
        for index, tool, arguments in normalized:
            started = perf_counter()
            try:
                resolved_arguments = _resolve_value(arguments, results)
            except Exception as exc:
                item = {
                    "index": index,
                    "tool": tool,
                    "ok": False,
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                }
            else:
                item = await _run_one(
                    index,
                    tool,
                    resolved_arguments,
                    handlers[tool],
                    use_thread=False,
                )

            results.append(item)
            if not item["ok"] and stop_on_error:
                break

    failed = sum(1 for item in results if not item["ok"])
    completed = len(results)
    stopped_early = requested_mode == "sequential" and completed < len(normalized)

    return {
        "mode": requested_mode,
        "requested": len(normalized),
        "completed": completed,
        "succeeded": completed - failed,
        "failed": failed,
        "stopped_early": stopped_early,
        "duration_ms": round((perf_counter() - batch_started) * 1000, 3),
        "results": results,
    }

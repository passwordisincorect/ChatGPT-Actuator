from __future__ import annotations

import asyncio
import time
import unittest

from chatgpt_actuator.batch import MAX_BATCH_ACTIONS, execute_batch


class BatchExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_sequential_preserves_order(self):
        seen = []

        def echo(value):
            seen.append(value)
            return {"value": value}

        result = await execute_batch(
            [
                {"tool": "echo", "arguments": {"value": 1}},
                {"tool": "echo", "arguments": {"value": 2}},
            ],
            {"echo": echo},
        )

        self.assertEqual(seen, [1, 2])
        self.assertEqual(result["requested"], 2)
        self.assertEqual(result["completed"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertFalse(result["stopped_early"])

    async def test_result_reference_feeds_later_action(self):
        def make():
            return {"nested": [{"value": "abc"}]}

        def consume(value):
            return {"received": value}

        result = await execute_batch(
            [
                {"tool": "make"},
                {
                    "tool": "consume",
                    "arguments": {"value": {"$ref": "0.result.nested.0.value"}},
                },
            ],
            {"make": make, "consume": consume},
        )

        self.assertEqual(result["results"][1]["result"]["received"], "abc")

    async def test_bad_reference_fails_safely(self):
        result = await execute_batch(
            [
                {"tool": "make"},
                {"tool": "consume", "arguments": {"value": {"$ref": "9.result"}}},
            ],
            {"make": lambda: {"x": 1}, "consume": lambda value: value},
        )

        self.assertEqual(result["failed"], 1)
        self.assertFalse(result["results"][1]["ok"])
        self.assertIn("unavailable action index", result["results"][1]["error"]["message"])

    async def test_sequential_stops_on_error(self):
        called = []

        def fail():
            called.append("fail")
            raise RuntimeError("boom")

        def later():
            called.append("later")
            return "unexpected"

        result = await execute_batch(
            [{"tool": "fail"}, {"tool": "later"}],
            {"fail": fail, "later": later},
            stop_on_error=True,
        )

        self.assertEqual(called, ["fail"])
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertTrue(result["stopped_early"])
        self.assertEqual(result["results"][0]["error"]["type"], "RuntimeError")

    async def test_sequential_can_continue_after_error(self):
        def fail():
            raise ValueError("bad")

        def ok():
            return {"ok": 1}

        result = await execute_batch(
            [{"tool": "fail"}, {"tool": "ok"}],
            {"fail": fail, "ok": ok},
            stop_on_error=False,
        )

        self.assertEqual(result["completed"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["succeeded"], 1)

    async def test_async_handler_is_supported(self):
        async def async_echo(value):
            await asyncio.sleep(0)
            return value

        result = await execute_batch(
            [{"tool": "async_echo", "arguments": {"value": "yes"}}],
            {"async_echo": async_echo},
        )

        self.assertTrue(result["results"][0]["ok"])
        self.assertEqual(result["results"][0]["result"], "yes")

    async def test_parallel_readonly_rejects_non_readonly_tool(self):
        with self.assertRaises(PermissionError):
            await execute_batch(
                [{"tool": "filesystem_write_text", "arguments": {"path": "x", "content": "y"}}],
                {"filesystem_write_text": lambda **_: None},
                mode="parallel_readonly",
            )

    async def test_parallel_readonly_rejects_dependencies(self):
        with self.assertRaises(ValueError):
            await execute_batch(
                [
                    {"tool": "system_info"},
                    {"tool": "system_info", "arguments": {"unused": {"$ref": "0.result"}}},
                ],
                {"system_info": lambda **_: {}},
                mode="parallel_readonly",
            )

    async def test_parallel_readonly_runs_allowed_actions_concurrently(self):
        def delayed_info():
            time.sleep(0.08)
            return {"ok": True}

        started = time.perf_counter()
        result = await execute_batch(
            [{"tool": "system_info"}, {"tool": "system_info"}],
            {"system_info": delayed_info},
            mode="parallel_readonly",
        )
        elapsed = time.perf_counter() - started

        self.assertEqual(result["failed"], 0)
        self.assertLess(elapsed, 0.15)

    async def test_validation_rejects_nested_unknown_and_oversized_batches(self):
        handlers = {"system_info": lambda: {}}

        with self.assertRaises(ValueError):
            await execute_batch([{"tool": "batch_execute"}], handlers)

        with self.assertRaises(ValueError):
            await execute_batch([{"tool": "unknown"}], handlers)

        with self.assertRaises(ValueError):
            await execute_batch(
                [{"tool": "system_info"}] * (MAX_BATCH_ACTIONS + 1),
                handlers,
            )


if __name__ == "__main__":
    unittest.main()

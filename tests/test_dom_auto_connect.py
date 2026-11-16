from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from chatgpt_actuator import dom_auto_connect as auto
from chatgpt_actuator.automation_errors import BrowserConnectionUnavailable


def binding(endpoint="ws://127.0.0.1:9222/devtools/browser/edge"):
    return {"owner": "alice", "pid": 101, "started": 10.0, "exe": "msedge.exe",
            "user_data_dir": "C:/Users/alice/AppData/Local/Microsoft/Edge/User Data",
            "profile_directory": "Default", "endpoint": endpoint,
            "allow_temporary_focus": False}


class Worker:
    created = []
    messages_value = [{"ready": True}, {"prompt_hwnd": 44, "request_correlated": True}]
    result_value = None

    def __init__(self, action, binding, limits, **extra):
        self.action, self.binding, self.extra = action, binding, extra
        self.process = SimpleNamespace(poll=lambda: None)
        self.closed = False
        self.created.append(self)

    def messages(self):
        return list(self.messages_value)

    def result(self):
        return self.result_value

    def close(self):
        self.closed = True


class DomAutoConnectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Worker.created = []
        Worker.messages_value = [{"ready": True}, {"prompt_hwnd": 44, "request_correlated": True}]
        Worker.result_value = None
        self.limits = {"max_child_processes": 4}

    async def test_prepare_bootstraps_missing_endpoint_then_refreshes(self):
        before = binding(None)
        after = binding()
        fake = Worker("enable", before, self.limits)
        with patch.object(auto.cdp_identity, "resolve", return_value=before), \
                patch.object(auto.cdp_identity, "refresh", return_value=after) as refresh, \
                patch.object(auto, "_UIWorker", return_value=fake) as make_worker, \
                patch.object(auto, "_wait", new=AsyncMock(return_value={"ok": True, "debugging_enabled_by_tool": True})):
            result = await auto.prepare({"enabled": True}, "alice", self.limits)
        make_worker.assert_called_once_with("enable", before, self.limits)
        refresh.assert_called_once_with(before)
        self.assertTrue(fake.closed)
        self.assertEqual(result["endpoint"], after["endpoint"])
        self.assertTrue(result["debugging_enabled_by_tool"])

    def test_validate_rejects_rotated_endpoint(self):
        changed = binding("ws://127.0.0.1:9333/devtools/browser/new")
        with patch.object(auto.cdp_identity, "resolve", return_value=changed):
            with self.assertRaisesRegex(BrowserConnectionUnavailable, "identity or CDP endpoint changed"):
                auto.validate(binding(), {"enabled": True}, "alice")

    def _driver(self, entered=None):
        async def connect_over_cdp(*args, **kwargs):
            if entered is not None:
                entered.set()
            await asyncio.Future()
        process = SimpleNamespace(pid=77)
        transport = SimpleNamespace(_proc=process)
        return SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect_over_cdp),
                               _impl_obj=SimpleNamespace(_connection=SimpleNamespace(_transport=transport)))

    async def test_cancelled_connect_closes_workers_and_cleans_correlated_prompt(self):
        entered = asyncio.Event()
        driver = self._driver(entered)
        with patch.object(auto.cdp_identity, "refresh", return_value=binding()), \
                patch.object(auto.psutil, "Process", return_value=SimpleNamespace(create_time=lambda: 3.0)), \
                patch.object(auto, "_UIWorker", side_effect=lambda *args, **kwargs: Worker(*args, **kwargs)), \
                patch.object(auto, "_wait", new=AsyncMock(return_value={"ok": True})):
            task = asyncio.create_task(auto.connect(driver, binding(), self.limits))
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual([item.action for item in Worker.created], ["approve", "cancel"])
        self.assertTrue(all(item.closed for item in Worker.created))
        self.assertEqual(Worker.created[1].extra, {"prompt_hwnd": 44, "request_correlated": True})

    async def test_worker_failure_cancels_transport_and_closes_resource(self):
        driver = self._driver()
        failure = {"ok": False, "error": "consent window rejected"}
        Worker.result_value = failure
        with patch.object(auto.cdp_identity, "refresh", return_value=binding()), \
                patch.object(auto.psutil, "Process", return_value=SimpleNamespace(create_time=lambda: 3.0)), \
                patch.object(auto, "_UIWorker", side_effect=lambda *args, **kwargs: Worker(*args, **kwargs)):
            with self.assertRaisesRegex(BrowserConnectionUnavailable, "consent window rejected"):
                await auto.connect(driver, binding(), self.limits)
        self.assertEqual([item.action for item in Worker.created], ["approve", "cancel"])
        self.assertTrue(all(item.closed for item in Worker.created))

    async def test_worker_failure_after_transport_completion_is_not_ignored(self):
        failure = {"ok": False, "error": "consent window rejected"}
        async def connect_over_cdp(*args, **kwargs):
            Worker.result_value = failure
            return SimpleNamespace()
        process = SimpleNamespace(pid=77)
        transport = SimpleNamespace(_proc=process)
        driver = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect_over_cdp),
                                 _impl_obj=SimpleNamespace(_connection=SimpleNamespace(_transport=transport)))
        with patch.object(auto.cdp_identity, "refresh", return_value=binding()), \
                patch.object(auto.psutil, "Process", return_value=SimpleNamespace(create_time=lambda: 3.0)), \
                patch.object(auto, "_UIWorker", side_effect=lambda *args, **kwargs: Worker(*args, **kwargs)):
            with self.assertRaisesRegex(BrowserConnectionUnavailable, "consent window rejected"):
                await auto.connect(driver, binding(), self.limits)
        self.assertEqual([item.action for item in Worker.created], ["approve", "cancel"])
        self.assertTrue(all(item.closed for item in Worker.created))

    async def test_message_overflow_still_closes_worker_without_masking_error(self):
        class OverflowWorker(Worker):
            def messages(self):
                raise BrowserConnectionUnavailable("CDP UI worker exceeded its output budget")

            def close(self):
                self.closed = True
                raise RuntimeError("simulated cleanup failure")

        driver = self._driver()
        with patch.object(auto.cdp_identity, "refresh", return_value=binding()), \
                patch.object(auto.psutil, "Process", return_value=SimpleNamespace(create_time=lambda: 3.0)), \
                patch.object(auto, "_UIWorker", side_effect=lambda *args, **kwargs: OverflowWorker(*args, **kwargs)):
            with self.assertRaisesRegex(BrowserConnectionUnavailable, "output budget"):
                await auto.connect(driver, binding(), self.limits)
        self.assertEqual(len(Worker.created), 1)
        self.assertTrue(Worker.created[0].closed)

    def test_partial_capture_construction_joins_and_closes_streams(self):
        class Pipe:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class Reader:
            def __init__(self, pipe, _limit):
                self.pipe = pipe
                self.thread = SimpleNamespace(join=lambda timeout: setattr(self, "joined", timeout),
                                              is_alive=lambda: False)

        class Process:
            def __init__(self, *args, **kwargs):
                self.stdout, self.stderr = Pipe(), Pipe()
                self.returncode = None

            def wait(self, timeout):
                self.returncode = 1

            def close(self):
                self.was_closed = True

        process = Process()
        job = SimpleNamespace(close=lambda: None)
        calls = 0

        def capture(pipe, limit):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("capture setup failed")
            return Reader(pipe, limit)

        with patch("chatgpt_actuator.background_process.Capture", side_effect=capture), \
                patch("chatgpt_actuator.windows_job.WindowsJob", return_value=job), \
                patch.dict("sys.modules", {"chatgpt_actuator.windows_process":
                    SimpleNamespace(WindowsProcess=lambda *args, **kwargs: process)}):
            with self.assertRaisesRegex(RuntimeError, "capture setup failed"):
                auto._UIWorker("enable", binding(), self.limits)
        self.assertEqual(process.stdout.closed, True)
        self.assertEqual(process.stderr.closed, True)
        self.assertTrue(process.was_closed)

    async def test_endpoint_rotation_blocks_connection_before_worker_creation(self):
        rotated = binding("ws://127.0.0.1:9333/devtools/browser/new")
        driver = self._driver()
        with patch.object(auto.cdp_identity, "refresh", return_value=rotated), \
                patch.object(auto, "_UIWorker") as make_worker:
            with self.assertRaisesRegex(BrowserConnectionUnavailable, "endpoint changed"):
                await auto.connect(driver, binding(), self.limits)
        make_worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()

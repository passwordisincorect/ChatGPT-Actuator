"""Owner-granted CDP bootstrap; existing Edge is never a child job."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import psutil
from . import cdp_identity
from .automation_errors import BrowserConnectionUnavailable


class _UIWorker:
    def __init__(self, action, binding, limits, **extra):
        from .background_process import Capture
        from .windows_job import WindowsJob
        from .windows_process import WindowsProcess
        self.closed = False
        env = {key: os.environ[key] for key in (
            'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'LOCALAPPDATA',
            'APPDATA', 'USERPROFILE', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432', 'PATH')
            if key in os.environ}
        env['PYTHONPATH'] = str(Path(__file__).resolve().parent.parent)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        request = json.dumps({'action': action, 'binding': binding, **extra}, ensure_ascii=True)
        self.process = WindowsProcess([sys.executable, '-B', '-m',
                                       'chatgpt_actuator.cdp_ui_worker', request],
                                      str(Path(__file__).parent), env, None)
        self.job = None
        self.captures = []
        try:
            self.job = WindowsJob(self.process, limits, allow_external_ui_reads=True)
            self.out = Capture(self.process.stdout, 65536)
            self.captures.append(self.out)
            self.err = Capture(self.process.stderr, 65536)
            self.captures.append(self.err)
            self.process.resume()
        except BaseException:
            try:
                if self.job is not None:
                    self.job.close()
                else:
                    self.process.terminate()
            except BaseException:
                pass
            try:
                self.process.wait(timeout=3)
            except BaseException:
                pass
            captured = {id(capture.pipe): capture for capture in self.captures}
            for stream in (self.process.stdout, self.process.stderr):
                capture = captured.get(id(stream))
                if capture is not None:
                    capture.thread.join(timeout=1)
                    if not capture.thread.is_alive():
                        capture.pipe.close()
                else:
                    stream.close()
            try:
                self.process.close()
            except BaseException:
                pass
            raise

    def messages(self):
        data, truncated = self.out.snapshot()
        if truncated:
            raise BrowserConnectionUnavailable('CDP UI worker exceeded its output budget')
        result = []
        for line in data.decode('utf-8').splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                result.append(value)
        return result

    def result(self):
        return next((row for row in reversed(self.messages()) if 'ok' in row), None)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.job.close()
        try:
            self.process.wait(timeout=3)
        finally:
            for capture in (self.out, self.err):
                capture.thread.join(timeout=1)
                if not capture.thread.is_alive():
                    capture.pipe.close()
            self.process.close()


async def _wait(worker, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = worker.result()
        if result is not None:
            if not result.get('ok'):
                raise BrowserConnectionUnavailable('Edge CDP automation blocked: ' + result.get('error', 'worker failed'))
            return result
        if worker.process.poll() is not None:
            raise BrowserConnectionUnavailable('Edge CDP UI worker exited without a result')
        await asyncio.sleep(.05)
    raise BrowserConnectionUnavailable('Edge CDP UI worker deadline exceeded')


async def prepare(policy, owner, resource_limits):
    binding = cdp_identity.resolve(policy, owner)
    enabled = False
    if binding['endpoint'] is None:
        worker = _UIWorker('enable', binding, resource_limits)
        try:
            result = await _wait(worker, 24)
            enabled = result.get('debugging_enabled_by_tool') is True
        finally:
            worker.close()
        binding = cdp_identity.refresh(binding)
        if binding['endpoint'] is None:
            raise BrowserConnectionUnavailable('Edge debugging was not exposed by the selected profile')
    return {**binding, 'debugging_enabled_by_tool': enabled}


def discover(policy, owner):
    """Preflight identity and current port without enabling or connecting anything."""
    return cdp_identity.resolve(policy, owner)


def validate(binding, policy, owner):
    current = cdp_identity.resolve(policy, owner)
    identity = ('owner', 'pid', 'started', 'exe', 'user_data_dir', 'profile_directory', 'endpoint')
    if any(current.get(key) != binding.get(key) for key in identity) or current['endpoint'] is None:
        raise BrowserConnectionUnavailable('The selected Edge identity or CDP endpoint changed')
    return current


async def connect(driver, binding, resource_limits):
    current = cdp_identity.refresh(binding)
    if current['endpoint'] != binding['endpoint'] or current['endpoint'] is None:
        raise BrowserConnectionUnavailable('Edge CDP endpoint changed before connection')
    if not binding['endpoint'].startswith('ws://'):
        browser = await driver.chromium.connect_over_cdp(binding['endpoint'], timeout=20000, no_defaults=True)
        return browser, False
    process = driver._impl_obj._connection._transport._proc
    requester = {'pid': process.pid, 'started': psutil.Process(process.pid).create_time()}
    worker = _UIWorker('approve', binding, resource_limits, driver=requester)
    connection = None
    failed = True
    try:
        ready_deadline = time.monotonic() + 5
        while not any(row.get('ready') is True for row in worker.messages()):
            if worker.result() is not None or worker.process.poll() is not None:
                await _wait(worker, .1)
                raise BrowserConnectionUnavailable('CDP consent watcher was not armed')
            if time.monotonic() >= ready_deadline:
                raise BrowserConnectionUnavailable('CDP consent watcher readiness deadline exceeded')
            await asyncio.sleep(.05)
        connection = asyncio.create_task(driver.chromium.connect_over_cdp(
            binding['endpoint'], timeout=20000, no_defaults=True))
        while not connection.done():
            result = worker.result()
            if result is not None and not result.get('ok'):
                await _wait(worker, .1)
            await asyncio.sleep(.05)
        browser = await connection
        # The native Invoke can settle just after the socket completes.
        result = worker.result()
        if result is None and any(row.get('request_correlated') for row in worker.messages()):
            result = await _wait(worker, 1)
        if result is not None and not result.get('ok'):
            await _wait(worker, .1)
        failed = False
        return browser, bool(result and result.get('approved'))
    finally:
        primary_error = sys.exc_info()[1]
        if connection is not None and not connection.done():
            connection.cancel()
            await asyncio.gather(connection, return_exceptions=True)
        observed = None
        try:
            observed = next((row for row in worker.messages() if row.get('request_correlated')), None)
        except BrowserConnectionUnavailable:
            # A truncated transcript is not trusted for prompt attribution.
            # Preserve the original failure/cancellation and still close the worker.
            pass
        finally:
            try:
                worker.close()
            except BaseException:
                if primary_error is None:
                    raise
        if failed and observed:
            cleanup = _UIWorker('cancel', binding, resource_limits,
                                prompt_hwnd=observed['prompt_hwnd'], request_correlated=True)
            try:
                await _wait(cleanup, 3)
            except Exception:
                pass  # An approved or closed dialog is never recreated or replayed.
            finally:
                cleanup.close()

"""Tool-disabled Codex CLI planner; saved authentication stays inside the CLI."""
import asyncio
import json
import logging
import os
import re
from pathlib import Path
import shutil
import tempfile
import time
from collections import Counter
from .retry import PlannerUnavailable, failure


DECISION_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {name: {'type': 'string'} for name in
                   ('kind', 'reason', 'tool', 'arguments_json', 'preconditions_json', 'postconditions_json')},
    'required': ['kind', 'reason', 'tool', 'arguments_json', 'preconditions_json', 'postconditions_json'],
}
DECISION_SCHEMA['properties']['kind']['enum'] = ['observe', 'step', 'blocked']
_log = logging.getLogger(__name__)


async def communicate_bounded(process, prompt, limit=1048576, diagnostics=None):
    total = 0
    counts = {'stdout_bytes': 0, 'stderr_bytes': 0}
    pending = bytearray()
    event_types = Counter()
    allowed_events = {'thread.started', 'turn.started', 'turn.completed', 'turn.failed',
                      'item.started', 'item.updated', 'item.completed', 'error'}
    stderr_tail = ''
    signal_flags, status_codes = set(), set()
    if diagnostics is not None:
        diagnostics.update(counts, event_types={}, completion_seen=False, signal_flags=[], status_codes=[])
    def observe(stream_name, chunk):
        nonlocal stderr_tail
        counts[stream_name + '_bytes'] += len(chunk)
        if diagnostics is not None:
            diagnostics[stream_name + '_bytes'] = counts[stream_name + '_bytes']
        if stream_name == 'stdout':
            pending.extend(chunk)
            while b'\n' in pending:
                line, _, rest = pending.partition(b'\n')
                pending[:] = rest
                try:
                    event = json.loads(line)
                    kind = event.get('type') if isinstance(event, dict) else None
                    if kind is not None:
                        event_types[kind if isinstance(kind, str) and kind in allowed_events else 'other'] += 1
                        if kind == 'turn.completed' and diagnostics is not None:
                            diagnostics['completion_seen'] = True
                    if isinstance(event, dict):
                        signal = event.get('signal')
                        code = event.get('status_code')
                        if type(signal) is str and signal in {'SIGTERM', 'SIGKILL', 'SIGINT', 'SIGABRT'}:
                            signal_flags.add(signal)
                        if type(code) is int and 100 <= code <= 599:
                            status_codes.add(code)
                except (ValueError, UnicodeDecodeError):
                    pass
                if diagnostics is not None:
                    diagnostics.update(event_types=dict(event_types), signal_flags=sorted(signal_flags), status_codes=sorted(status_codes))
        else:
            text = stderr_tail + chunk.decode('utf-8', errors='replace').lower()
            for pattern, flag in (('websocket', 'websocket'), ('falling back', 'fallback'),
                                  ('timed out', 'network_timeout'), ('connection', 'connection'),
                                  ('unauthorized', 'authentication'), ('rate limit', 'rate_limit')):
                if pattern in text:
                    signal_flags.add(flag)
            for code in re.findall(r'\b(?:http\s+|status(?:_code)?[=: ]+)([1-5]\d\d)\b', text):
                status_codes.add(int(code))
            stderr_tail = text[-128:]
            if diagnostics is not None:
                diagnostics.update(signal_flags=sorted(signal_flags), status_codes=sorted(status_codes))
    async def read(stream):
        nonlocal total
        chunks = []
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return b''.join(chunks)
            total += len(chunk)
            observe('stdout' if stream is process.stdout else 'stderr', chunk)
            if total > limit:
                raise ValueError('Planner output limit exceeded')
            chunks.append(chunk)
    readers = [asyncio.create_task(read(process.stdout)), asyncio.create_task(read(process.stderr))]
    try:
        process.stdin.write(prompt)
        await process.stdin.drain()
        process.stdin.close()
        result = await asyncio.gather(*readers)
        await process.wait()
        return result
    except BaseException:
        if diagnostics is not None:
            diagnostics.update(counts)
        if process.returncode is None:
            process.kill()
        for task in readers:
            task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        async def discard(stream):
            while await stream.read(65536):
                pass
        try:
            await asyncio.wait_for(asyncio.gather(discard(process.stdout), discard(process.stderr), process.wait()), 3)
        except (asyncio.TimeoutError, ConnectionError):
            pass
        raise
    finally:
        for task in readers:
            task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


def reject_action_events(stdout):
    for line in stdout.decode('utf-8').splitlines():
        event = json.loads(line)
        if event.get('type', '').startswith('item.') and event.get('item', {}).get('type') not in {
                'agent_message', 'reasoning', 'error'}:
            raise RuntimeError('Planner emitted a disallowed action event')


def validate_events(stdout):
    """Accept a completed planning response, never an action/tool event."""
    usage, completed = 0, False
    for line in stdout.decode('utf-8').splitlines():
        event = json.loads(line)
        if event.get('type') == 'turn.failed':
            raise RuntimeError('Planner turn failed')
        if event.get('type') == 'turn.completed':
            if completed:
                raise RuntimeError('Planner emitted multiple completed turns')
            completed = True
            used = event.get('usage', {})
            if not isinstance(used, dict) or any(type(used.get(key)) is not int or used[key] < 0
                    for key in ('input_tokens', 'output_tokens')):
                raise RuntimeError('Planner reported invalid usage')
            usage = used['input_tokens'] + used['output_tokens']
        if event.get('type', '').startswith('item.'):
            kind = event.get('item', {}).get('type')
            # Diagnostic items are not tool executions. Actual command/file/
            # MCP/browser events fail closed, including unfamiliar future types.
            if kind not in {'agent_message', 'reasoning', 'error'}:
                raise RuntimeError('Planner emitted a disallowed event')
    if not completed or usage < 0:
        raise RuntimeError('Planner did not complete a valid response')
    return usage


class CodexPlanner:
    def __init__(self, directory, executable=None, timeout=90):
        self.directory = Path(directory)
        self.executable = executable or shutil.which('codex')
        self.timeout = timeout

    async def decide(self, prompt):
        if not self.executable or not Path(self.executable).is_file():
            raise RuntimeError('Codex CLI is unavailable')
        if len(prompt.encode('utf-8')) > 65536:
            raise ValueError('Planner context limit exceeded')
        self.directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='plan-', dir=self.directory) as temp:
            directory = Path(temp)
            schema, answer = directory / 'schema.json', directory / 'answer.json'
            schema.write_text(json.dumps(DECISION_SCHEMA), encoding='utf-8')
            argv = [self.executable, 'exec', '--ignore-user-config', '--ephemeral', '--skip-git-repo-check',
                    '--sandbox', 'read-only', '--json', '--color', 'never']
            config = {'approval_policy': 'never', 'features.shell_tool': False, 'features.apps': False,
                      'features.plugins': False, 'features.multi_agent': False, 'features.hooks': False,
                      'features.daemon_auto_start': False, 'features.shell_snapshot': False,
                      'features.browser_use': False, 'features.browser_use_external': False,
                      'features.computer_use': False, 'features.image_generation': False,
                      'features.view_image': False, 'features.code_mode_host': False,
                      'features.goals': False, 'features.sleep_tool': False,
                      'features.workspace_dependencies': False,
                      'web_search': 'disabled', 'model_reasoning_effort': 'low',
                      'model_provider': 'actuator-https',
                      'model_providers.actuator-https.name': 'OpenAI',
                      'model_providers.actuator-https.requires_openai_auth': True,
                      'model_providers.actuator-https.supports_websockets': False}
            for key, value in config.items():
                argv += ['-c', key + '=' + json.dumps(value)]
            argv += ['--output-schema', str(schema), '--output-last-message', str(answer), '-']
            process = await asyncio.create_subprocess_exec(*argv, cwd=directory,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                creationflags=0x08000000 if os.name == 'nt' else 0)
            job = None
            try:
                if os.name == 'nt':
                    from ..windows_job import WindowsJob
                    job = WindowsJob.from_pid(process.pid)
                try:
                    started = time.monotonic()
                    diagnostics = {}
                    stdout, stderr = await asyncio.wait_for(communicate_bounded(process, prompt.encode('utf-8'), diagnostics=diagnostics), self.timeout)
                except asyncio.TimeoutError:
                    diagnostics['elapsed_seconds'] = round(time.monotonic() - started, 3)
                    if job:
                        try:
                            accounting = job.accounting()
                            if isinstance(accounting, dict):
                                diagnostics['windows_job'] = {key: value for key, value in accounting.items()
                                    if key in {'active_processes', 'total_processes', 'terminated_processes', 'peak_process_memory_mb', 'peak_job_memory_mb'} and type(value) in (int, float)}
                        except Exception:
                            diagnostics['windows_job_available'] = False
                    _log.warning('planner_timeout diagnostics=%s', diagnostics)
                    raise PlannerUnavailable('planner_timeout', transient=True) from None
                if len(stdout) + len(stderr) > 1048576 or not answer.exists() or answer.stat().st_size > 65536:
                    if process.returncode == 0 or len(stdout) + len(stderr) > 1048576:
                        raise ValueError('Planner output limit exceeded')
                reject_action_events(stdout)
                if process.returncode != 0:
                    raise failure(stdout, stderr)
                try:
                    usage = validate_events(stdout)
                except RuntimeError as exc:
                    if str(exc) == 'Planner turn failed':
                        raise failure(stdout, stderr) from None
                    raise
                decision = json.loads(answer.read_text(encoding='utf-8'))
                if set(decision) != set(DECISION_SCHEMA['required']) or any(not isinstance(v, str) for v in decision.values()):
                    raise ValueError('Invalid planner decision')
                return decision, usage
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                if job:
                    job.close()

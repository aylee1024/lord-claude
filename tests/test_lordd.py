"""HTTP and MCP contract tests against strict subprocess protocol peers."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import quote

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYTHON = os.environ.get('LORD_TEST_PYTHON', sys.executable)
DAEMON = ROOT / 'lordd.py'


@pytest.fixture(autouse=True)
def grok_home(tmp_path, monkeypatch):
    # The daemon writes grok sandbox profiles under GROK_HOME; tests never touch the user's ~/.grok.
    monkeypatch.setenv('GROK_HOME', str(tmp_path / 'grok-home'))


def test_daemon_exists():
    assert DAEMON.is_file(), 'The daemon required by CONTRACT.md must exist'


class Client:
    def __init__(self, home):
        self.home = home

    def connect(self, path, body=None):
        conn = socket.socket(socket.AF_UNIX)
        conn.settimeout(float(os.environ.get('LORD_TEST_TIMEOUT', '12')))
        conn.connect(str(self.home / 'lordd.sock'))
        payload = json.dumps(body).encode() if body is not None else b''
        conn.sendall((f'{"POST" if body is not None else "GET"} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n').encode() + payload)
        file = conn.makefile('rb')
        status = int(file.readline().split()[1])
        headers = {}
        while (line := file.readline()) != b'\r\n':
            key, value = line.decode().split(':', 1)
            headers[key.lower()] = value.strip()
        return conn, file, status, headers

    def request(self, path, body=None):
        conn, file, status, _ = self.connect(path, body)
        try:
            result = json.loads(file.read())
            assert status == 200, result
            return result
        finally:
            file.close()
            conn.close()

    def stream(self, path, body):
        conn, file, status, headers = self.connect(path, body)
        assert status == 200, file.read()
        try:
            assert headers['content-type'] == 'application/x-ndjson'
            assert headers['transfer-encoding'] == 'chunked'
            while (size := int(file.readline(), 16)):
                payload = file.read(size)
                assert file.read(2) == b'\r\n'
                for line in payload.splitlines():
                    yield json.loads(line)
            assert file.read(2) == b'\r\n'
        finally:
            file.close()
            conn.close()


class Rig(Client):
    def __init__(self, home, idle=60):
        super().__init__(home)
        self.data = home / 'vendor'
        self.env = dict(os.environ, LORD_HOME=str(home), GROK_HOME=str(home / 'grok'), PYTHONDONTWRITEBYTECODE='1',
                        OPENAI_API_KEY='must-not-reach-vendor', SOME_TOKEN='must-not-reach-vendor',
                        ANTHROPIC_BASE_URL='must-not-reach-vendor', CLAUDE_CODE_FLAG='must-not-reach-vendor',
                        RANDOM_SECRET='must-not-reach-vendor', CUSTOM_BEARER='Bearer must-not-reach-vendor',
                        S3_ACCESS_KEY='must-not-reach-vendor', DB_PASSWORD='must-not-reach-vendor', CLOUD_CREDENTIAL='must-not-reach-vendor')
        config = {family: {'model': model, 'effort': 'high', 'program': [PYTHON, str(ROOT / 'tests/fakes/vendor.py'), family, str(self.data)]}
                  for family, model in [('gpt', 'gpt-6.1-sol'), ('grok', 'grok-4.7'), ('gemini', 'gemini-3.8-flash')]}
        config['grok'].update(write_sandbox='lord-workspace', readonly_sandbox='test-readonly')
        config.update(retry_delay_s=.05, guard_interval_s=.2)
        config.update(idle_exit_s=idle, prune_days=14, vendor_home=str(self.data))
        self.config = home / 'config.json'
        self.config.write_text(json.dumps(config))
        self.env['LORD_CONFIG'] = str(self.config)
        self.proc = None
        self.start()

    def start(self):
        self.log = (self.home / 'test-process.log').open('ab')
        self.proc = subprocess.Popen([PYTHON, str(DAEMON)], env=self.env, stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert self.proc.poll() is None, (self.home / 'test-process.log').read_text()
            try:
                if self.request('/health')['ok']:
                    return
            except (OSError, ValueError):
                time.sleep(.025)
        pytest.fail('Daemon did not bind socket')

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill(); self.proc.wait()
                raise
        self.log.close()

    def open(self, family, agent='a', **options):
        return self.request('/open', {'agentId': agent, 'family': family, 'cwd': str(self.home), **options})

    def turn(self, text, agent='a', **options):
        return list(self.stream('/turn', {'agentId': agent, 'text': text, **options}))

    def audit(self):
        return [json.loads(line) for line in (self.data / 'audit.jsonl').read_text().splitlines()]


@pytest.fixture
def rig():
    base = Path.home() / '.claude/lord'
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='test-', dir=base) as folder:
        item = Rig(Path(folder))
        try:
            yield item
        finally:
            item.stop()


@pytest.mark.parametrize('family', ['gpt', 'grok', 'gemini'])
def test_open_stream_done_permissions_and_usage(rig, family):
    opened = rig.open(family, effort='low', readonly=family == 'gpt', net=False)
    assert opened['vendorSession'] and opened['model'] and opened['effort'] == 'low'
    assert rig.open(family) == opened
    assert (rig.home.stat().st_mode & 0o777) == 0o700
    assert ((rig.home / 'lordd.sock').stat().st_mode & 0o777) == 0o600
    events = rig.turn('PING')
    assert any(e['type'] == 'activity' for e in events)
    assert ''.join(e['text'] for e in events if e['type'] == 'text') == 'PONG'
    assert events[-1]['type'] == 'done' and events[-1]['text'] == 'PONG'
    assert events[-1]['usage']['input_tokens'] == 10
    assert events[-1]['usage']['cache_read_input_tokens'] == (0 if family == 'gpt' else 2)
    assert set(events[-1]['usage']) == {'input_tokens', 'output_tokens', 'cache_read_input_tokens'}
    assert rig.request('/agents')[0]['state'] == 'idle'
    records = [json.loads(line) for line in (rig.home / 'sessions.jsonl').read_text().splitlines()]
    assert records[-1]['vendorSession'] == opened['vendorSession']
    assert all(k in records[-1] for k in ['agentId', 'family', 'cwd', 'model', 'created'])
    assert all('activity' not in rec for rec in records)
    assert 'fake-bearer-value' not in (rig.home / 'lordd.log').read_text()


@pytest.mark.parametrize('family', ['gpt', 'grok'])
@pytest.mark.parametrize('prompt,name', [('TOOL', 'Agent'), ('MESSAGE', 'SendMessage'), ('ERROR_TOOL', 'Agent'), ('CLAUDE_TOOL', 'Agent')])
def test_mcp_tool_result_continue(rig, family, prompt, name):
    rig.open(family)
    events = rig.turn(prompt)
    event = events[-1]
    assert event['type'] == 'tool' and event['name'] == name
    assert not any(e['type'] == 'done' for e in events)
    assert rig.request('/agents')[0]['state'] == 'waiting_tool'
    if name == 'Agent':
        assert event['input']['prompt'] == ('child' if prompt == 'CLAUDE_TOOL' else '@model gpt-6-luna\nchild')
        assert event['input']['subagent_type'] == ('general-purpose' if prompt == 'CLAUDE_TOOL' else 'lord:gpt')
        assert event['input']['run_in_background'] is False
        if prompt == 'CLAUDE_TOOL':
            assert event['input']['model'] == 'haiku'
    else:
        assert event['input']['to'] == 'friend' and event['input']['message'] == 'hello'
    assert rig.request('/tool_result', {'agentId': 'a', 'callId': event['callId'], 'text': 'child-result', 'isError': prompt == 'ERROR_TOOL'}) == {'ok': True}
    completed = list(rig.stream('/continue', {'agentId': 'a'}))
    assert completed[-1]['type'] == 'done' and 'child-result' in completed[-1]['text']
    result = json.loads(completed[-1]['text'])
    assert result['isError'] is (prompt == 'ERROR_TOOL')


@pytest.mark.parametrize('family', ['gpt', 'grok', 'gemini'])
def test_list_agents_mcp_is_local_and_each_agent_has_its_own_process(rig, family):
    rig.open(family, agent='a')
    rig.open(family, agent='b')
    events = rig.turn('LIST')
    assert events[-1]['type'] == 'done'
    assert not any(e['type'] == 'tool' for e in events)
    result = json.loads(events[-1]['text'])
    listed = json.loads(result['content'][0]['text'])
    assert listed['you'] == 'a' and {a['agentId'] for a in listed['agents']} == {'a', 'b'}
    assert len([e for e in rig.audit() if e.get('launch')]) == 2


@pytest.mark.parametrize('family', ['gpt', 'grok', 'gemini'])
def test_interrupt_mid_turn_and_resume_memory(rig, family):
    rig.open(family)
    assert rig.turn('secret-word')[-1]['type'] == 'done'
    stream = rig.stream('/turn', {'agentId': 'a', 'text': 'SLOW'})
    assert next(stream)['type'] == 'activity'
    assert rig.request('/interrupt', {'agentId': 'a'}) == {'ok': True}
    assert list(stream)[-1]['type'] == 'error'
    assert rig.turn('REMEMBER')[-1]['text'] == 'secret-word'
    assert rig.request('/agents')[0]['state'] == 'idle'


def test_steer_and_effort_nearest_supported(rig):
    assert rig.open('gpt', model='gpt-6-luna', effort='max')['effort'] == 'high'
    assert rig.request('/steer', {'agentId': 'a', 'text': 'idle'}) == {'ok': True, 'steered': False}
    stream = rig.stream('/turn', {'agentId': 'a', 'text': 'SLOW'})
    assert next(stream)['type'] == 'activity'
    assert rig.request('/steer', {'agentId': 'a', 'text': 'steered'}) == {'ok': True, 'steered': True}
    assert list(stream)[-1]['text'] == 'steered'
    rig.turn('PING')
    assert rig.turn('PING')[-1]['usage']['input_tokens'] == 10


@pytest.mark.parametrize('family', ['gpt', 'grok', 'gemini'])
def test_vendor_crash_restart_resume(rig, family):
    session = rig.open(family)['vendorSession']
    rig.turn('persistent-word')
    failed = rig.turn('CRASH')[-1]
    assert failed['type'] == 'error' and failed['retryable'] is True
    assert rig.turn('REMEMBER')[-1]['text'] == 'persistent-word'
    assert rig.open(family)['vendorSession'] == session
    launches = [e for e in rig.audit() if e.get('launch')]
    assert len(launches) == 2
    if family != 'gemini':
        assert any(e.get('method') == ('thread/resume' if family == 'gpt' else 'session/load') for e in rig.audit())
    else:
        assert '--conversation' in launches[-1]['argv']


@pytest.mark.parametrize('family', ['gpt', 'grok', 'gemini'])
def test_daemon_restart_resumes_idempotent_session(rig, family):
    opened = rig.open(family)
    rig.turn('before-daemon-restart')
    rig.stop()
    rig.start()
    assert rig.open(family) == opened
    assert rig.turn('REMEMBER')[-1]['text'] == 'before-daemon-restart'


def test_usage_cached_env_scrub_and_close(rig):
    rig.open('gpt')
    expected = {'chatgpt': {'usedPercent': 47, 'resetsAt': 1234567890, 'credits': {'balance': '1'}}}
    assert rig.request('/usage') == expected == rig.request('/usage')
    assert len([e for e in rig.audit() if e.get('method') == 'account/rateLimits/read']) == 1
    assert all(e['env'] == {} for e in rig.audit() if e.get('launch'))
    assert rig.request('/close', {'agentId': 'a'}) == {'ok': True}
    assert rig.request('/agents')[0]['state'] == 'closed'


def test_idle_exit_does_not_expire_running_turn(rig):
    config = json.loads(rig.config.read_text())
    config['idle_exit_s'] = .3
    rig.config.write_text(json.dumps(config))
    rig.stop()
    rig.start()
    rig.open('gpt')
    stream = rig.stream('/turn', {'agentId': 'a', 'text': 'SLOW'})
    assert next(stream)['type'] == 'activity'
    time.sleep(.5)
    assert rig.proc.poll() is None
    rig.request('/interrupt', {'agentId': 'a'})
    list(stream)
    rig.proc.wait(timeout=4)
    assert rig.proc.returncode == 0 and not (rig.home / 'lordd.sock').exists()


def test_prune_only_owned_sessions(rig):
    sessions = {family: rig.open(family, agent=family)['vendorSession'] for family in ['gpt', 'grok', 'gemini']}
    rig.open('gpt', agent='recent')
    rig.turn('PING', agent='grok')
    rig.stop()
    path = rig.home / 'sessions.jsonl'
    records = [json.loads(line) for line in path.read_text().splitlines()]
    for row in records:
        if row['agentId'] != 'recent':
            row['created'] = row['used'] = '2000-01-01T00:00:00+00:00'
    path.write_text(''.join(json.dumps(row) + '\n' for row in records))
    grok_root = rig.data / '.grok/sessions' / quote(str(rig.home), safe='')
    stranger = grok_root / 'unowned'; stranger.mkdir(parents=True)
    (stranger / 'keep').write_text('keep')
    agy_root = rig.data / '.gemini/antigravity-cli/conversations'
    (agy_root / 'unowned.db').touch()
    result = subprocess.run([PYTHON, str(DAEMON), '--prune', '--days', '14'], env=rig.env, capture_output=True, text=True, timeout=12)
    assert result.returncode == 0, result.stderr
    # Vendor history is evidence: prune drops the journal record and archives codex threads, deleting nothing.
    assert (grok_root / sessions['grok']).exists() and (agy_root / (sessions['gemini'] + '.db')).exists()
    assert (stranger / 'keep').read_text() == 'keep' and (agy_root / 'unowned.db').exists()
    assert [e['archived'] for e in rig.audit() if 'archived' in e] == [sessions['gpt']]
    assert [json.loads(line)['agentId'] for line in path.read_text().splitlines()] == ['recent']


def test_daemon_fork_returns_silently_and_singleton(rig):
    first = rig.request('/health')['pid']
    result = subprocess.run([PYTHON, str(DAEMON), '--daemon'], env=rig.env, capture_output=True, timeout=2)
    assert result.returncode == 0 and result.stdout == b'' and result.stderr == b''
    assert rig.request('/health')['pid'] == first


def test_redactor_and_environment_scrub_without_credentials(rig):
    spec = importlib.util.spec_from_file_location('lordd', DAEMON)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    assert module.Redactor()('trace Bearer credential-value rest') == 'trace [REDACTED] rest'
    env = module.subscription_env({'ordinary': 'ok', 'HARNESS_API_KEY': 'bad', 'ANTHROPIC_BASE_URL': 'bad', 'SNEAKY': 'Bearer bad'})
    assert env['ordinary'] == 'ok' and 'HARNESS_API_KEY' not in env and 'ANTHROPIC_BASE_URL' not in env and 'SNEAKY' not in env


@pytest.mark.parametrize('family', ['gpt', 'grok'])
def test_parallel_mcp_calls_are_delivered_in_order(rig, family):
    rig.open(family)
    first = rig.turn('PARALLEL')[-1]
    assert first['type'] == 'tool'
    rig.request('/tool_result', {'agentId': 'a', 'callId': first['callId'], 'text': 'first', 'isError': False})
    second = list(rig.stream('/continue', {'agentId': 'a'}))[-1]
    assert second['type'] == 'tool' and first['callId'] != second['callId']
    assert rig.request('/agents')[0]['state'] == 'waiting_tool'
    rig.request('/tool_result', {'agentId': 'a', 'callId': second['callId'], 'text': 'second', 'isError': False})
    completed = list(rig.stream('/continue', {'agentId': 'a'}))[-1]
    assert completed['type'] == 'done' and 'first' in completed['text'] and 'second' in completed['text']


@pytest.mark.parametrize('family', ['gpt', 'grok'])
def test_interrupt_pending_mcp_keeps_other_turn_alive(rig, family):
    rig.open(family, agent='a'); rig.open(family, agent='b')
    other = rig.stream('/turn', {'agentId': 'b', 'text': 'SLOW'})
    assert next(other)['type'] == 'activity'
    assert rig.turn('TOOL')[-1]['type'] == 'tool'
    rig.request('/interrupt', {'agentId': 'a'})
    assert rig.request('/agents')[1]['state'] == 'running'
    if family == 'gpt':
        rig.request('/steer', {'agentId': 'b', 'text': 'still-alive'})
        assert list(other)[-1]['text'] == 'still-alive'
    else:
        rig.request('/interrupt', {'agentId': 'b'})
        assert list(other)[-1]['type'] == 'error'
        assert rig.turn('PING', agent='b')[-1]['text'] == 'PONG'
    assert len([e for e in rig.audit() if e.get('launch')]) == 2


def test_codex_lord_tools_are_trusted_and_required(rig):
    (rig.data / '.codex').mkdir(parents=True, exist_ok=True)
    (rig.data / '.codex/config.toml').write_text('[mcp_servers.playwright]\ncommand = "npx"\n[mcp_servers."odd name"]\ncommand = "x"\n')
    rig.open('gpt')
    params = next(e['params'] for e in rig.audit() if e.get('method') == 'thread/start')
    assert params['config']['mcp_servers']['playwright'] == {'enabled': False}
    assert params['config']['mcp_servers']['odd name'] == {'enabled': False}
    server = params['config']['mcp_servers']['lord']
    assert server['default_tools_approval_mode'] == 'approve'
    assert server['required'] is True and server['tool_timeout_sec'] >= 3600


def test_gemini_prune_closes_backend_before_reopening(rig):
    first = rig.open('gemini')['vendorSession']
    assert rig.request('/prune', {'days': 0})['pruned'] == ['a']
    second = rig.open('gemini')['vendorSession']
    assert second and second != first
    assert rig.turn('PING')[-1]['text'] == 'PONG'


def local_daemon(rig, monkeypatch):
    spec = importlib.util.spec_from_file_location('lordd_unit', DAEMON)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    module.HOME = rig.home / 'unit'
    module.SOCKET = module.HOME / 'lordd.sock'
    monkeypatch.setenv('LORD_CONFIG', str(rig.config))
    return module.Daemon()


def test_interrupt_during_codex_start_does_not_kill_shared_process(rig, monkeypatch):
    async def scenario():
        d = local_daemon(rig, monkeypatch)
        release = asyncio.Event()
        try:
            await d.open({'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home)})
            await d.open({'agentId': 'b', 'family': 'gpt', 'cwd': str(rig.home)})
            a, b = d.agents['a'], d.agents['b']
            await d.begin(b, {'text': 'SLOW'})
            await asyncio.wait_for(b.events.get(), 2)
            v = d.backend(a); original = v.rpc
            async def gated(method, params, **options):
                if method == 'turn/start' and params['threadId'] == a.vendorSession:
                    await release.wait()
                return await original(method, params, **options)
            v.rpc = gated
            await d.begin(a, {'text': 'PING'})
            await d.interrupt(a)
            assert v.process.returncode is None and b.state == 'running'
            release.set()
            await asyncio.wait_for(a.finished.wait(), 2)
            assert a.state == 'idle'
        finally:
            release.set()
            await d.close()
    asyncio.run(scenario())


def test_prune_reserves_agent_before_awaiting_archive(rig, monkeypatch):
    async def scenario():
        d = local_daemon(rig, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        prune = None
        try:
            await d.open({'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home)})
            a = d.agents['a']; v = d.service(); original = v.rpc
            async def gated(method, params, **options):
                if method == 'thread/archive':
                    entered.set(); await release.wait()
                return await original(method, params, **options)
            v.rpc = gated
            prune = asyncio.create_task(d.prune(0))
            await asyncio.wait_for(entered.wait(), 2)
            with pytest.raises(ValueError):
                await d.begin(a, {'text': 'PING'})
            release.set(); await prune
            assert 'a' not in d.agents
        finally:
            release.set()
            if prune:
                await asyncio.gather(prune, return_exceptions=True)
            await d.close()
    asyncio.run(scenario())


def test_detached_turn_keeps_running_and_leads_the_next_turn(rig):
    rig.open('gpt')
    stream = rig.stream('/turn', {'agentId': 'a', 'text': 'SLOW'})
    assert next(stream)['type'] == 'activity'
    stream.close()
    time.sleep(.3)
    assert rig.request('/agents')[0]['state'] == 'running'
    assert rig.request('/steer', {'agentId': 'a', 'text': 'detached-result'})['steered']
    done = rig.turn('PING')[-1]
    assert done['type'] == 'done'
    assert done['text'].startswith('Result of the earlier task') and 'detached-result' in done['text'] and done['text'].endswith('PONG')
    assert rig.turn('REMEMBER')[-1]['text'] == 'PING'


def test_interrupt_cancels_a_queued_followup(rig):
    rig.open('gpt')
    stream = rig.stream('/turn', {'agentId': 'a', 'text': 'SLOW'})
    assert next(stream)['type'] == 'activity'
    stream.close()
    time.sleep(.3)
    stopper = threading.Timer(.3, lambda: rig.request('/interrupt', {'agentId': 'a'}))
    stopper.start()
    assert list(rig.stream('/turn', {'agentId': 'a', 'text': 'PING'}))[-1]['type'] == 'error'
    stopper.join()
    assert rig.request('/agents')[0]['state'] == 'idle'


def test_readonly_is_rejected_without_an_enforcing_adapter(rig):
    conn, file, status, _ = rig.connect('/open', {'agentId': 'a', 'family': 'gemini', 'cwd': str(rig.home), 'readonly': True})
    try:
        assert status == 400 and 'readonly' in json.loads(file.read())['error']
        assert rig.request('/agents') == []
    finally:
        file.close(); conn.close()


def test_failed_start_is_retried_once_with_a_fresh_process(rig):
    rig.stop()
    config = json.loads(rig.config.read_text())
    config['gpt']['program'].append('--fail-init-once')
    rig.config.write_text(json.dumps(config))
    rig.start()
    assert rig.open('gpt')['vendorSession']
    assert len([e for e in rig.audit() if e.get('launch')]) == 2
    assert rig.turn('PING')[-1]['text'] == 'PONG'


def test_second_daemon_preserves_a_live_socket_without_its_lock(rig):
    home = rig.home / 'occupied'; home.mkdir()
    path = home / 'lordd.sock'
    listener = socket.socket(socket.AF_UNIX); listener.bind(str(path)); listener.listen()
    inode = path.stat().st_ino
    proc = subprocess.Popen([PYTHON, str(DAEMON)], env={**rig.env, 'LORD_HOME': str(home)}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        proc.wait(timeout=2)
        assert proc.returncode == 0 and path.stat().st_ino == inode
    finally:
        if proc.poll() is None:
            proc.terminate(); proc.wait(timeout=3)
        listener.close()


@pytest.mark.parametrize('family', ['gpt', 'grok', 'gemini'])
def test_environment_scrub_applies_to_every_vendor(rig, family):
    rig.open(family)
    assert [e['env'] for e in rig.audit() if e.get('launch')] == [{}]


@pytest.mark.parametrize('readonly', [False, True])
@pytest.mark.parametrize('net', [False, True])
def test_codex_permission_policy_applies_to_every_turn(rig, readonly, net):
    rig.open('gpt', readonly=readonly, net=net)
    rig.turn('PING')
    params = next(e['params'] for e in rig.audit() if e.get('method') == 'turn/start')
    assert params['sandboxPolicy']['type'] == ('readOnly' if readonly else 'workspaceWrite')
    assert params['sandboxPolicy']['networkAccess'] is net


def test_interrupt_before_grok_prompt_is_dispatched(rig, monkeypatch):
    async def scenario():
        d = local_daemon(rig, monkeypatch)
        try:
            await d.open({'agentId': 'a', 'family': 'grok', 'cwd': str(rig.home)})
            a = d.agents['a']; v = d.backend(a)
            await d.begin(a, {'text': 'PING'})
            await d.interrupt(a)
            await asyncio.wait_for(a.finished.wait(), 2)
            await asyncio.sleep(.1)
            assert v.process and v.process.returncode is None
            assert not any(e.get('method') == 'session/prompt' for e in rig.audit())
        finally:
            await d.close()
    asyncio.run(scenario())


def test_steer_reaches_codex_while_waiting_for_lord_tool(rig):
    rig.open('gpt')
    tool = rig.turn('TOOL')[-1]
    assert rig.request('/steer', {'agentId': 'a', 'text': 'additional context'}) == {'ok': True, 'steered': True}
    rig.request('/tool_result', {'agentId': 'a', 'callId': tool['callId'], 'text': 'result', 'isError': False})
    assert list(rig.stream('/continue', {'agentId': 'a'}))[-1]['type'] == 'done'


@pytest.mark.parametrize('waiting', ['tool', 'turn'])
def test_shutdown_releases_active_http_and_vendor_waiters(rig, waiting):
    rig.open('gpt')
    stream = None
    if waiting == 'tool':
        assert rig.turn('TOOL')[-1]['type'] == 'tool'
    else:
        stream = rig.stream('/turn', {'agentId': 'a', 'text': 'SLOW'})
        assert next(stream)['type'] == 'activity'
    try:
        rig.stop()
        assert rig.proc.returncode == 0 and not (rig.home / 'lordd.sock').exists()
    finally:
        if stream:
            stream.close()


@pytest.mark.parametrize('family', ['gpt', 'grok', 'gemini'])
def test_text_is_flushed_while_vendor_is_still_waiting(rig, family):
    rig.open(family)
    stream = rig.stream('/turn', {'agentId': 'a', 'text': 'TEXT_WAIT'})
    assert next(stream)['type'] == 'activity'
    assert next(stream) == {'type': 'text', 'text': 'partial'}
    assert rig.request('/agents')[0]['state'] == 'running'
    rig.request('/interrupt', {'agentId': 'a'})
    assert list(stream)[-1]['type'] == 'error'


def test_log_is_redacted_and_has_only_one_rotation(rig, monkeypatch):
    d = local_daemon(rig, monkeypatch)
    path = rig.home / 'unit/lordd.log'
    path.write_text('a' * (1024 * 1024))
    d.log('trace Bearer rotation-credential end')
    assert path.stat().st_size < 1024 * 1024 and 'rotation-credential' not in path.read_text()
    assert path.with_name('lordd.log.1').read_text() == 'a' * (1024 * 1024)
    path.write_text('b' * (1024 * 1024))
    d.log('another rotation')
    assert path.with_name('lordd.log.1').read_text() == 'b' * (1024 * 1024)
    assert not path.with_name('lordd.log.2').exists()


def test_grok_readonly_agents_run_under_the_sandbox_profile(rig):
    rig.open('grok', agent='w')
    rig.open('grok', agent='r1', readonly=True)
    rig.open('grok', agent='r2', readonly=True)
    launches = [a['argv'] for a in rig.audit() if a.get('launch')]
    assert launches.count(['--sandbox', 'lord-workspace', 'agent', '--always-approve', 'stdio']) == 1
    assert launches.count(['--sandbox', 'test-readonly', 'agent', '--always-approve', 'stdio']) == 2


def test_gemini_registers_the_env_keyed_lord_server_once(rig):
    rig.open('gemini', agent='g1')
    rig.open('gemini', agent='g2')
    added = [a['mcp_add'] for a in rig.audit() if 'mcp_add' in a]
    assert len(added) == 1 and added[0][:2] == ['lord', '--'] and added[0][2] == PYTHON and added[0][3:] == [str(DAEMON), '--mcp']


def test_mcp_server_without_an_agent_lists_no_tools():
    env = {k: v for k, v in os.environ.items() if k != 'LORD_AGENT_ID'}
    out = subprocess.run([PYTHON, str(DAEMON), '--mcp'], input=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}) + '\n',
                         capture_output=True, text=True, env=env, timeout=10).stdout
    assert json.loads(out)['result']['tools'] == []
    env['LORD_AGENT_ID'] = 'x'
    out = subprocess.run([PYTHON, str(DAEMON), '--mcp'], input=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}) + '\n',
                         capture_output=True, text=True, env=env, timeout=10).stdout
    assert [t['name'] for t in json.loads(out)['result']['tools']] == ['spawn_agent', 'send_message', 'list_agents']


def restart_with(rig, **changes):
    rig.stop()
    config = json.loads(rig.config.read_text())
    for key, value in changes.items():
        if '.' in key:
            family, name = key.split('.')
            config[family][name] = value
        else:
            config[key] = value
    rig.config.write_text(json.dumps(config))
    rig.start()


def test_memory_ceiling_stops_only_the_offending_agent(rig):
    restart_with(rig, memory_ceiling_kb=1)
    rig.open('gpt', agent='a')
    events = list(rig.stream('/turn', {'agentId': 'a', 'text': 'SLOW'}))
    assert events[-1]['type'] == 'error' and 'memory ceiling' in events[-1]['message'] and events[-1]['retryable'] is False
    restart_with(rig, memory_ceiling_kb=12 * 1024 * 1024)
    assert rig.turn('PING', agent='a')[-1]['text'] == 'PONG'


def test_a_silent_vendor_is_stopped_after_its_no_progress_limit(rig):
    restart_with(rig, **{'grok.no_progress_s': 1})
    rig.open('grok')
    started = time.monotonic()
    events = list(rig.stream('/turn', {'agentId': 'a', 'text': 'SLOW'}))
    assert events[-1]['type'] == 'error' and 'printed nothing for 1 s' in events[-1]['message']
    assert time.monotonic() - started < 5
    assert rig.turn('PING')[-1]['text'] == 'PONG'


def test_an_idle_vendor_process_is_closed_and_the_next_turn_resumes(rig):
    restart_with(rig, process_idle_s=.5)
    rig.open('gpt')
    rig.turn('secret-word')
    time.sleep(1.5)
    assert rig.turn('REMEMBER')[-1]['text'] == 'secret-word'
    assert len([e for e in rig.audit() if e.get('launch')]) == 2


def test_grok_runs_without_claude_or_cursor_imports(rig):
    rig.open('grok')
    env = next(e['grok_env'] for e in rig.audit() if e.get('launch'))
    assert {k for k, v in env.items() if v == 'false'} == {f'GROK_{a}_{b}_ENABLED' for a in ('CLAUDE', 'CURSOR') for b in ('MCPS', 'HOOKS', 'SKILLS')}


def test_dirs_become_codex_writable_roots_and_reattach(rig):
    extra = rig.home / 'extra'; extra.mkdir()
    rig.open('gpt')
    rig.open('gpt', dirs=[str(extra)])
    starts = [e['params'] for e in rig.audit() if e.get('method') in ('thread/start', 'thread/resume')]
    assert starts[-1]['config']['sandbox_workspace_write']['writable_roots'] == [str(extra.resolve())]
    rig.turn('PING')
    turn = [e['params'] for e in rig.audit() if e.get('method') == 'turn/start'][-1]
    assert turn['sandboxPolicy']['writableRoots'] == [str(extra.resolve())]


def test_missing_dir_is_refused(rig):
    conn, file, status, _ = rig.connect('/open', {'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home), 'dirs': ['/no/such/dir']})
    try: assert status == 400 and '@dir' in json.loads(file.read())['error']
    finally: file.close(); conn.close()


def test_effort_is_the_request_or_the_nearest_level_the_vendor_lists(rig):
    assert rig.open('gemini', agent='g', effort='low')['effort'] == 'low'
    assert rig.open('gemini', agent='h', effort='max')['effort'] == 'high'
    assert rig.open('grok', agent='k', effort='medium')['effort'] == 'medium'
    assert rig.open('grok', agent='m', effort='max')['effort'] == 'xhigh'
    assert rig.open('gpt', agent='n', model='gpt-6-luna', effort='max')['effort'] == 'high'
    assert rig.open('gpt', agent='u', effort='ultra')['effort'] == 'ultra'
    assert rig.open('grok', agent='v', effort='ultra')['effort'] == 'xhigh'


def test_per_agent_memory_and_hang_overrides(rig):
    rig.open('gpt', agent='m', memory_gb=0.000001)
    events = list(rig.stream('/turn', {'agentId': 'm', 'text': 'SLOW'}))
    assert 'memory ceiling' in events[-1]['message']
    rig.open('grok', agent='h', hang_s=1)
    events = list(rig.stream('/turn', {'agentId': 'h', 'text': 'SLOW'}))
    assert 'printed nothing for 1 s' in events[-1]['message']


def test_tools_keeps_the_users_vendor_setup(rig):
    (rig.data / '.codex').mkdir(parents=True, exist_ok=True)
    (rig.data / '.codex/config.toml').write_text('[mcp_servers.playwright]\ncommand = "npx"\n')
    rig.open('gpt', agent='g', tools=True)
    params = [e['params'] for e in rig.audit() if e.get('method') == 'thread/start'][-1]
    assert 'playwright' not in params['config']['mcp_servers']
    rig.open('grok', agent='k', tools=True)
    assert {k: v for k, v in [e['grok_env'] for e in rig.audit() if e.get('launch')][-1].items() if k != 'GROK_HOME'} == {}


def test_a_pending_tool_call_is_offered_again_to_the_next_client(rig):
    rig.open('gpt')
    events = rig.turn('TOOL')
    assert events[-1]['type'] == 'tool'
    call = events[-1]['callId']
    again = list(rig.stream('/turn', {'agentId': 'a', 'text': 'PING'}))
    assert again[0]['type'] == 'tool' and again[0]['callId'] == call
    rig.request('/tool_result', {'agentId': 'a', 'callId': call, 'text': 'child said hi', 'isError': False})
    done = list(rig.stream('/continue', {'agentId': 'a'}))[-1]
    assert done['type'] == 'done' and 'child said hi' in done['text'] and done['text'].endswith('PONG')


def test_an_interrupted_detached_turn_leaves_no_false_prefix(rig):
    rig.open('gpt')
    stream = rig.stream('/turn', {'agentId': 'a', 'text': 'SLOW'})
    assert next(stream)['type'] == 'activity'
    stream.close()
    time.sleep(.3)
    assert rig.request('/interrupt', {'agentId': 'a'}) == {'ok': True}
    assert rig.turn('PING')[-1]['text'] == 'PONG'


def test_the_memory_guard_spares_the_other_agents(rig):
    rig.open('gpt', agent='calm')
    rig.open('gpt', agent='hog', memory_gb=0.000001)
    calm = rig.stream('/turn', {'agentId': 'calm', 'text': 'SLOW'})
    assert next(calm)['type'] == 'activity'
    assert 'memory ceiling' in list(rig.stream('/turn', {'agentId': 'hog', 'text': 'SLOW'}))[-1]['message']
    assert rig.request('/steer', {'agentId': 'calm', 'text': 'still-here'})['steered']
    assert list(calm)[-1]['text'] == 'still-here'


def mcp_tools(env):
    out = subprocess.run([PYTHON, str(DAEMON), '--mcp', 'x'], input=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}) + '\n',
                         capture_output=True, text=True, env={**os.environ, **env}, timeout=10).stdout
    return [t['name'] for t in json.loads(out)['result']['tools']]


def test_read_only_agents_get_only_list_agents_and_calls_need_the_token(rig):
    assert mcp_tools({'LORD_READONLY': '1'}) == ['list_agents']
    rig.open('gpt', readonly=True)
    stream = rig.stream('/turn', {'agentId': 'a', 'text': 'SLOW'})
    assert next(stream)['type'] == 'activity'
    for body, word in [({'agentId': 'a', 'name': 'list_agents', 'token': 'forged'}, 'unknown process'),
                       ({'agentId': 'a', 'name': 'spawn_agent', 'arguments': {'type': 'general-purpose', 'prompt': 'x', 'description': 'x'}}, 'unknown process')]:
        conn, file, status, _ = rig.connect('/mcp', body)
        try: assert status == 400 and word in json.loads(file.read())['error']
        finally: file.close(); conn.close()
    rig.request('/interrupt', {'agentId': 'a'})


@pytest.mark.parametrize('family,body,word', [
    ('grok', {'dirs': ['/usr']}, '@dir is not available for grok'),
    ('gemini', {'dirs': ['/usr']}, '@dir is not needed for gemini'),
    ('grok', {'readonly': True, 'cwd': '/private/tmp'}, 'temp folders'),
    ('gpt', {'cwd': str(Path.home())}, 'home folder'),
    ('gpt', {'model': 'no-such-model'}, 'unknown gpt model'),
    ('gemini', {'model': 'no-such-model'}, 'unknown gemini model'),
])
def test_unsupported_options_are_refused_with_the_reason(rig, family, body, word):
    conn, file, status, _ = rig.connect('/open', {'agentId': 'a', 'family': family, 'cwd': str(rig.home), **body})
    try: assert status == 400 and word in json.loads(file.read())['error']
    finally: file.close(); conn.close()


def test_activity_is_logged_per_agent_and_listed(rig):
    rig.open('gpt')
    rig.turn('PING')
    entry = rig.request('/agents')[0]
    assert 'fake work' in Path(entry['log']).read_text() and entry['usage']['output_tokens'] >= 0


def test_a_later_readonly_reattaches_with_the_read_only_sandbox(rig):
    rig.open('gpt')
    rig.turn('PING')
    rig.open('gpt', readonly=True)
    rig.turn('PING')
    assert [e['params'] for e in rig.audit() if e.get('method') == 'turn/start'][-1]['sandboxPolicy']['type'] == 'readOnly'


def test_grok_answer_is_the_text_after_its_last_tool_call(rig):
    rig.open('grok')
    events = rig.turn('NARRATE')
    assert events[-1]['text'] == 'NARRATE'


def test_codex_agents_start_without_the_users_plugins_and_notifier(rig):
    (rig.data / '.codex').mkdir(parents=True, exist_ok=True)
    (rig.data / '.codex/config.toml').write_text('notify = ["x"]\n[plugins."example@openai-curated"]\nenabled = true\n')
    rig.open('gpt')
    config = [e['params'] for e in rig.audit() if e.get('method') == 'thread/start'][-1]['config']
    assert config['plugins'] == {'example@openai-curated': {'enabled': False}} and config['notify'] == []


def test_a_turn_past_its_wall_clock_limit_is_stopped(rig):
    rig.open('gpt', wall_s=1)
    started = time.monotonic()
    events = list(rig.stream('/turn', {'agentId': 'a', 'text': 'SLOW'}))
    assert 'wall-clock limit' in events[-1]['message'] and time.monotonic() - started < 5


def test_grok_runs_only_under_lord_custom_profiles_that_fail_closed(rig):
    sandbox = rig.home / 'grok/sandbox.toml'
    sandbox.parent.mkdir()
    sandbox.write_text('# user profile\n[profiles.mine]\nextends = "strict"\n')
    rig.open('grok', agent='w')
    import tomllib
    profiles = tomllib.loads(sandbox.read_text())['profiles']
    assert profiles['mine'] == {'extends': 'strict'}
    assert profiles['lord-workspace'] == {'extends': 'workspace'}
    assert profiles['lord-readonly']['extends'] == 'read-only' and profiles['lord-readonly']['restrict_network'] is False
    assert '/var/run/docker.sock' in profiles['lord-readonly']['deny']
    first = sandbox.read_text()
    rig.open('grok', agent='x')
    assert sandbox.read_text() == first


def test_user_config_is_merged_over_the_bundled_defaults(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('lordd_config', DAEMON)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    bundled = tmp_path / 'bundled.json'
    bundled.write_text(json.dumps({'grok': {'model': 'grok-4.7', 'effort': 'high'}, 'idle_exit_s': 600}))
    (tmp_path / 'config.json').write_text(json.dumps({'grok': {'model': 'grok-5'}, 'idle_exit_s': 60}))
    monkeypatch.setenv('LORD_CONFIG', str(bundled))
    monkeypatch.setattr(module, 'HOME', tmp_path)
    assert module.load_config() == {'grok': {'model': 'grok-5', 'effort': 'high'}, 'idle_exit_s': 60}


def test_a_missing_vendor_program_names_how_to_install_it(rig):
    config = json.loads(rig.config.read_text())
    config['gpt']['program'] = [str(rig.home / 'no-such-codex')]
    rig.config.write_text(json.dumps(config))
    rig.stop(); rig.start()
    conn, file, status, _ = rig.connect('/open', {'agentId': 'm', 'family': 'gpt', 'cwd': str(rig.home)})
    body = json.loads(file.read()); file.close(); conn.close()
    assert status != 200 and 'was not found' in body['error'] and 'codex login' in body['error']

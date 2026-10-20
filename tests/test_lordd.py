"""HTTP and MCP contract tests against strict subprocess protocol peers."""
import asyncio
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
from urllib.parse import quote

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYTHON = os.environ.get('LORD_TEST_PYTHON', sys.executable)
DAEMON = ROOT / 'lordd.py'


@pytest.fixture(autouse=True)
def grok_home(tmp_path, monkeypatch):
    # The daemon writes grok sandbox profiles under GROK_HOME and its own files under LORD_HOME; tests never touch
    # the user's ~/.grok or ~/.claude/lord.
    monkeypatch.setenv('GROK_HOME', str(tmp_path / 'grok-home'))
    monkeypatch.setenv('LORD_HOME', str(tmp_path / 'lord-home'))


def test_daemon_exists():
    assert DAEMON.is_file(), 'The daemon required by CONTRACT.md must exist'


class Client:
    def __init__(self, home):
        self.home = home

    def connect(self, path, body=None, key=True):
        conn = socket.socket(socket.AF_UNIX)
        conn.settimeout(float(os.environ.get('LORD_TEST_TIMEOUT', '12')))
        conn.connect(str(getattr(self, 'sock', None) or self.home / 'lordd.sock'))
        payload = json.dumps(body).encode() if body is not None else b''
        header = (self.home / 'control.header').read_text().strip() + '\r\n' if key and (self.home / 'control.header').exists() else ''
        conn.sendall((f'{"POST" if body is not None else "GET"} {path} HTTP/1.1\r\nHost: localhost\r\n{header}Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n').encode() + payload)
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
        self.env = dict(os.environ, LORD_HOME=str(home), LORD_DAEMON_SOCKET=str(home / 'lordd.sock'), GROK_HOME=str(home / 'grok'), PYTHONDONTWRITEBYTECODE='1',
                        OPENAI_API_KEY='must-not-reach-vendor', SOME_TOKEN='must-not-reach-vendor',
                        ANTHROPIC_BASE_URL='must-not-reach-vendor', CLAUDE_CODE_FLAG='must-not-reach-vendor',
                        RANDOM_SECRET='must-not-reach-vendor', CUSTOM_BEARER='Bearer must-not-reach-vendor',
                        S3_ACCESS_KEY='must-not-reach-vendor', GROK_API_KEY='must-not-reach-vendor', LORD_SECRET='must-not-reach-vendor', DB_PASSWORD='must-not-reach-vendor', CLOUD_CREDENTIAL='must-not-reach-vendor')
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
    # Not under a temp folder: read-only grok is refused there.
    base = Path.home() / '.cache/lord-tests'
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
    records = [json.loads(line) for line in (rig.home / 'journal.jsonl').read_text().splitlines()]
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



def test_a_stopped_gpt_turn_leaves_no_command_running(rig):
    rig.open('gpt')
    assert rig.turn('secret-word')[-1]['type'] == 'done'
    stream = rig.stream('/turn', {'agentId': 'a', 'text': 'COMMAND'})
    assert next(stream)['type'] == 'activity'
    assert rig.request('/interrupt', {'agentId': 'a'}) == {'ok': True}
    assert list(stream)[-1]['type'] == 'error'
    pid = [e['command_pid'] for e in rig.audit() if 'command_pid' in e][-1]
    deadline = time.monotonic() + 5
    while True:
        try: os.kill(pid, 0)
        except ProcessLookupError: break
        assert time.monotonic() < deadline, 'the stopped turn left its command running'
        time.sleep(.05)
    assert rig.turn('REMEMBER')[-1]['text'] == 'secret-word'

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


def test_a_failed_gpt_turn_gives_the_next_turn_a_new_codex_on_the_same_thread(rig):
    session = rig.open('gpt')['vendorSession']
    rig.turn('persistent-word')
    failed = rig.turn('FAIL')[-1]
    assert failed['type'] == 'error' and 'failed to load workspace requirements' in failed['message']
    assert 'the next turn starts a new codex' in failed['message']
    assert rig.turn('REMEMBER')[-1]['text'] == 'persistent-word'
    assert rig.open('gpt')['vendorSession'] == session
    assert len([e for e in rig.audit() if e.get('launch')]) == 2
    assert any(e.get('method') == 'thread/resume' for e in rig.audit())


@pytest.mark.parametrize('family', ['gpt', 'grok', 'gemini'])
def test_daemon_restart_resumes_idempotent_session(rig, family):
    opened = rig.open(family)
    rig.turn('before-daemon-restart')
    rig.stop()
    rig.start()
    assert rig.open(family) == opened
    assert rig.turn('REMEMBER')[-1]['text'] == 'before-daemon-restart'


def test_env_scrub_and_close(rig):
    rig.open('gpt')
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
    path = rig.home / 'journal.jsonl'
    records = [json.loads(line) for line in path.read_text().splitlines()]
    for row in records:
        if row['agentId'] != 'recent':
            row['created'] = row['used'] = '2000-01-01T00:00:00+00:00'
    path.write_text(''.join(json.dumps(row) + '\n' for row in records))
    grok_root = rig.home / 'grok/sessions' / quote(str(rig.home), safe='')
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


def test_redactor_and_environment_scrub_without_credentials(rig, monkeypatch):
    spec = importlib.util.spec_from_file_location('lordd', DAEMON)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    assert module.Redactor()('trace Bearer credential-value rest') == 'trace [REDACTED] rest'
    inherited = {'ordinary': 'ok', 'HARNESS_API_KEY': 'bad', 'ANTHROPIC_BASE_URL': 'bad', 'SNEAKY': 'Bearer bad',
                 'GROK_API_KEY': 'bad', 'LORD_SECRET': 'bad', 'GROK_HOME': '/g'}
    for key, value in inherited.items(): monkeypatch.setenv(key, value)
    env = module.subscription_env({'LORD_MCP_TOKEN': 'own'})
    assert env['ordinary'] == 'ok' and env['GROK_HOME'] == '/g' and env['LORD_MCP_TOKEN'] == 'own'
    assert not {'HARNESS_API_KEY', 'ANTHROPIC_BASE_URL', 'SNEAKY', 'GROK_API_KEY', 'LORD_SECRET'} & set(env)


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
    proc = subprocess.Popen([PYTHON, str(DAEMON)], env={**rig.env, 'LORD_HOME': str(home), 'LORD_DAEMON_SOCKET': str(path)}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
    if readonly and net:
        # With network on, codex can reach local services, so read-only would not hold.
        conn, file, status, _ = rig.connect('/open', {'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home), 'readonly': True, 'net': True})
        reply = json.loads(file.read()); file.close(); conn.close()
        assert status == 400 and 'not read-only' in reply['error']
        return
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


def test_gemini_registers_the_env_keyed_lord_server_at_each_agy_start(rig):
    # Re-added at every start: a daemon of another version may have removed the entry when it exited.
    rig.open('gemini', agent='g1')
    rig.open('gemini', agent='g2')
    added = [a['mcp_add'] for a in rig.audit() if 'mcp_add' in a]
    assert added == [['lord', '--', PYTHON, str(DAEMON), '--mcp']] * 2


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
    ('grok', {'nosandbox': True}, '@nosandbox is available for gpt only'),
    ('gemini', {'nosandbox': True}, '@nosandbox is not needed for gemini'),
    ('gpt', {'nosandbox': True, 'readonly': True}, 'contradict'),
])
def test_unsupported_options_are_refused_with_the_reason(rig, family, body, word):
    conn, file, status, _ = rig.connect('/open', {'agentId': 'a', 'family': family, 'cwd': str(rig.home), **body})
    try: assert status == 400 and word in json.loads(file.read())['error']
    finally: file.close(); conn.close()


def test_each_stand_in_reply_is_two_lines_so_a_progress_summary_copying_it_is_dropped():
    # Claude Code's progress summaries run the agent's own system prompt and drop a multi-line reply.
    for family in ('gpt', 'grok', 'gemini'):
        body = (ROOT / f'agents/{family}.md').read_text().split('\n---\n', 1)[1]
        lines = [line for line in body.splitlines()[1:] if line.strip()]
        assert lines[0].startswith('lord: error: ') and len(lines) == 2, family


def test_activity_is_logged_per_agent_and_listed(rig):
    rig.open('gpt')
    rig.turn('PING')
    entry = rig.request('/agents')[0]
    assert 'fake work' in Path(entry['log']).read_text() and entry['usage']['output_tokens'] >= 0
    # Each turn's log names the model and effort that ran.
    assert f"turn: gpt {entry['model']} effort {entry['effort']}" in Path(entry['log']).read_text()


def test_agy_tool_steps_are_logged_once_with_their_parameters(rig):
    rig.open('gemini')
    rig.turn('PING')
    log = Path(rig.request('/agents')[0]['log']).read_text()
    assert log.count('run_command {"CommandLine":"fake work"}') == 1


def test_each_turn_saves_its_final_answer_to_a_file(rig):
    rig.open('gpt', readonly=True)
    assert rig.request('/agents')[0]['answer'] is None
    rig.turn('PING')
    path = Path(rig.request('/agents')[0]['answer'])
    assert path.read_text() == 'PONG'
    rig.turn('second answer')
    assert path.read_text() == 'second answer'
    # A turn with an empty answer keeps the last one.
    rig.turn(' ')
    assert path.read_text() == 'second answer'


def test_a_later_readonly_reattaches_with_the_read_only_sandbox(rig):
    rig.open('gpt')
    rig.turn('PING')
    rig.open('gpt', readonly=True)
    rig.turn('PING')
    assert [e['params'] for e in rig.audit() if e.get('method') == 'turn/start'][-1]['sandboxPolicy']['type'] == 'readOnly'


def test_nosandbox_runs_codex_unconfined_and_a_later_readonly_is_refused(rig):
    rig.open('gpt', nosandbox=True)
    rig.turn('PING')
    assert [e['params'] for e in rig.audit() if e.get('method') == 'thread/start'][-1]['sandbox'] == 'danger-full-access'
    assert [e['params'] for e in rig.audit() if e.get('method') == 'turn/start'][-1]['sandboxPolicy'] == {'type': 'dangerFullAccess'}
    conn, file, status, _ = rig.connect('/open', {'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home), 'readonly': True})
    try: assert status == 400 and 'contradict' in json.loads(file.read())['error']
    finally: file.close(); conn.close()


def test_a_later_nosandbox_reattaches_unconfined(rig):
    rig.open('gpt')
    rig.turn('PING')
    assert [e['params'] for e in rig.audit() if e.get('method') == 'turn/start'][-1]['sandboxPolicy']['type'] == 'workspaceWrite'
    rig.open('gpt', nosandbox=True)
    rig.turn('PING')
    assert [e['params'] for e in rig.audit() if e.get('method') == 'thread/resume'][-1]['sandbox'] == 'danger-full-access'
    assert [e['params'] for e in rig.audit() if e.get('method') == 'turn/start'][-1]['sandboxPolicy'] == {'type': 'dangerFullAccess'}


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
    key = str((rig.home / 'control.header').resolve())
    assert profiles['lord-workspace'] == {'extends': 'workspace', 'deny': [key]}
    assert profiles['lord-readonly']['extends'] == 'read-only' and profiles['lord-readonly']['restrict_network'] is False
    assert '/var/run/docker.sock' in profiles['lord-readonly']['deny'] and key in profiles['lord-readonly']['deny']
    first = sandbox.read_text()
    rig.open('grok', agent='x')
    assert sandbox.read_text() == first
    # An end marker that lost its newline is still recognised: the block is replaced, never duplicated.
    sandbox.write_text(first.rstrip('\n'))
    rig.open('grok', agent='y')
    assert sandbox.read_text() == first


def test_a_sandbox_file_that_cannot_take_the_profiles_is_refused_unchanged(rig):
    sandbox = rig.home / 'grok/sandbox.toml'
    sandbox.parent.mkdir()
    sandbox.write_text('profiles = {mine = {extends = "strict"}}\n')
    conn, file, status, _ = rig.connect('/open', {'agentId': 'k', 'family': 'grok', 'cwd': str(rig.home)})
    body = json.loads(file.read()); file.close(); conn.close()
    assert status != 200 and "cannot take Lord's grok profiles" in body['error']
    assert sandbox.read_text() == 'profiles = {mine = {extends = "strict"}}\n'


def test_requests_without_the_control_key_are_refused_except_health_and_mcp(rig):
    for path, body in [('/agents', None), ('/open', {'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home)})]:
        conn, file, status, _ = rig.connect(path, body, key=False)
        reply = json.loads(file.read()); file.close(); conn.close()
        assert status == 403 and 'control key' in reply['error']
    conn, file, status, _ = rig.connect('/health', key=False)
    file.close(); conn.close()
    assert status == 200
    assert (rig.home / 'control.header').stat().st_mode & 0o777 == 0o600


def test_read_only_grok_is_refused_where_its_profile_leaves_writes(rig):
    var_tmp = Path(tempfile.mkdtemp(dir='/var/tmp'))
    grok_home = rig.home / 'grok'
    grok_home.mkdir()
    try:
        for cwd in (var_tmp, grok_home):
            conn, file, status, _ = rig.connect('/open', {'agentId': f'r{len(str(cwd))}', 'family': 'grok', 'cwd': str(cwd), 'readonly': True})
            reply = json.loads(file.read()); file.close(); conn.close()
            assert status == 400 and 'read-only profile' in reply['error']
        # An agent opened writable in a temp folder cannot become read-only there by naming another folder.
        rig.request('/open', {'agentId': 'w', 'family': 'grok', 'cwd': str(var_tmp)})
        conn, file, status, _ = rig.connect('/open', {'agentId': 'w', 'family': 'grok', 'cwd': str(rig.home), 'readonly': True})
        file.close(); conn.close()
        assert status == 400
    finally: var_tmp.rmdir()


def test_a_failed_first_open_leaves_no_agent_behind(rig):
    conn, file, status, _ = rig.connect('/open', {'agentId': 'm', 'family': 'gpt', 'cwd': str(rig.home), 'model': 'no-such-model'})
    file.close(); conn.close()
    assert status == 400
    assert rig.open('gpt', agent='m', model='gpt-6.1-sol')['model'] == 'gpt-6.1-sol'


def test_a_missing_agy_names_how_to_install_it(rig):
    config = json.loads(rig.config.read_text())
    config['gemini']['program'] = [str(rig.home / 'no-such-agy')]
    rig.config.write_text(json.dumps(config))
    rig.stop(); rig.start()
    conn, file, status, _ = rig.connect('/open', {'agentId': 'g', 'family': 'gemini', 'cwd': str(rig.home)})
    body = json.loads(file.read()); file.close(); conn.close()
    assert status != 200 and 'antigravity.google' in body['error']


def test_grok_and_gemini_usage_is_saved_when_the_turn_ends(rig):
    for family in ('grok', 'gemini'):
        rig.open(family, agent=family)
        assert rig.turn('PING', agent=family)[-1]['type'] == 'done'
    rig.stop(); rig.start()
    usage = {a['agentId']: a['usage'] for a in rig.request('/agents')}
    assert usage['grok'] is not None and usage['gemini'] is not None


def test_spawned_vendor_only_effort_goes_as_a_directive(monkeypatch):
    spec = importlib.util.spec_from_file_location('lordd_effort', DAEMON)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    async def main():
        a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '.', 'model': 'gpt-6.1-sol'}); a.state = 'running'
        d = object.__new__(m.Daemon); d.log = lambda text: None
        task = asyncio.create_task(d.mcp_call(a, 'spawn_agent', {'type': 'lord:gpt', 'prompt': 'work', 'description': 'd', 'effort': 'ultra'}, a.token))
        event = await asyncio.wait_for(a.events.get(), 2); task.cancel()
        return event['input']
    data = asyncio.run(main())
    assert data['prompt'] == '@effort ultra\nwork' and 'effort' not in data


def test_a_late_completion_of_an_earlier_codex_turn_is_ignored():
    spec = importlib.util.spec_from_file_location('lordd_late', DAEMON)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    async def main():
        a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '.', 'model': 'gpt-6.1-sol', 'vendorSession': 's'})
        a.state, a.turn_id = 'running', 'new'
        d = object.__new__(m.Daemon); d.agents = {'a': a}; d.redact = str
        m.Vendor(d, 'gpt', a).notification({'method': 'turn/completed', 'params': {'threadId': 's', 'turn': {'id': 'old', 'status': 'interrupted'}}})
        return a.state, a.events.empty()
    assert asyncio.run(main()) == ('running', True)


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


def load(name):
    spec = importlib.util.spec_from_file_location(name, DAEMON)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def test_a_refused_followup_ends_the_stream_with_the_earlier_result_and_the_reason(rig):
    rig.open('gpt')
    stream = rig.stream('/turn', {'agentId': 'a', 'text': 'SLOW'})
    assert next(stream)['type'] == 'activity'
    stream.close()
    time.sleep(.3)
    assert rig.request('/steer', {'agentId': 'a', 'text': 'detached-result'})['steered']
    last = rig.turn('PING', model='no-such-model')[-1]
    assert last['type'] == 'error' and 'detached-result' in last['message'] and 'no-such-model' in last['message']
    assert rig.turn('PING')[-1]['text'] == 'PONG'


def test_an_interrupt_before_dispatch_leaves_nothing_to_deliver():
    m = load('lordd_dispatch')
    async def main():
        a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '.', 'model': 'gpt-6.1-sol'})
        a.state = 'running'; a.finished.clear()
        d = object.__new__(m.Daemon); d.backend = lambda a: None; d.redact = str
        asyncio.create_task(d.run_turn(a, {'text': 'cancelled'}))
        await d.interrupt(a)
        await asyncio.sleep(.05)
        return a.undelivered()
    assert asyncio.run(main()) is False


def test_a_reconnect_offers_a_queued_tool_call_once():
    m = load('lordd_reconnect')
    async def main():
        a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '.', 'model': 'gpt-6.1-sol'})
        a.state = 'waiting_tool'; a.calls['c'] = asyncio.get_running_loop().create_future(); a.call_data['c'] = ('Agent', {'prompt': 'child'})
        a.events.put_nowait({'type': 'tool', 'callId': 'c', 'name': 'Agent', 'input': {'prompt': 'child'}})
        d = object.__new__(m.Daemon); d.agents = {'a': a}; d.connections = set(); d.requests = 0; d.redact = str; d.control = 'k'
        class Writer:
            data = b''
            def write(self, data): self.data += data
            async def drain(self): pass
            def close(self): pass
            async def wait_closed(self): pass
        reader, writer = asyncio.StreamReader(), Writer()
        payload = json.dumps({'agentId': 'a', 'text': 'next'}).encode()
        reader.feed_data(b'POST /turn HTTP/1.1\r\nX-Lord-Control: k\r\nContent-Length: ' + str(len(payload)).encode() + b'\r\n\r\n' + payload)
        await d.http(reader, writer)
        return writer.data.count(b'"callId":"c"'), a.events.empty(), a.carry
    assert asyncio.run(main()) == (1, True, [])


def test_waiting_for_a_lord_tool_does_not_count_as_vendor_silence():
    m = load('lordd_silence')
    async def main():
        a = m.Agent({'agentId': 'a', 'family': 'grok', 'cwd': '.', 'model': 'grok-4.7'})
        a.state = 'waiting_tool'; a.calls['c'] = asyncio.get_running_loop().create_future()
        d = object.__new__(m.Daemon); d.config = {'idle_exit_s': 600, 'grok': {'no_progress_s': 1}}; d.log = lambda t: None
        v = m.Vendor(d, 'grok', a); v.process = types.SimpleNamespace(pid=123456789, returncode=None); v.last = time.monotonic() - 10
        d.agents, d.vendors = {'a': a}, {'a': v}
        await d.route('POST', '/tool_result', {'agentId': 'a', 'callId': 'c', 'text': 'child finished'})
        return time.monotonic() - v.last < 1
    assert asyncio.run(main())


def test_agy_narration_before_a_tool_step_is_not_the_answer():
    m = load('lordd_agy')
    a = m.Agent({'agentId': 'g', 'family': 'gemini', 'cwd': '.', 'model': 'gemini-4-argon'}); a.state = 'running'
    d = object.__new__(m.Daemon); d.agents = {'g': a}; d.redact = str; d.save = lambda a: None
    v = m.Vendor(d, 'gemini', a)
    v.notification({'event': 'step_update', 'step_update': {'step_type': 'agent_response', 'text_delta': 'narration'}})
    v.notification({'event': 'step_update', 'step_update': {'step_type': 'run_command', 'command': 'ls'}})
    v.notification({'event': 'result', 'result': {'status': 'SUCCESS', 'response': '', 'usage': {}}})
    events = []
    while not a.events.empty(): events.append(a.events.get_nowait())
    assert events[-1] == {**events[-1], 'type': 'done', 'text': ''}


def test_a_refused_open_changes_nothing(rig):
    rig.open('gpt')
    conn, file, status, _ = rig.connect('/open', {'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home), 'net': True, 'dirs': [str(rig.home / 'missing')]})
    file.close(); conn.close()
    assert status == 400
    rig.turn('PING')
    params = [e['params'] for e in rig.audit() if e.get('method') == 'turn/start'][-1]
    assert params['sandboxPolicy']['networkAccess'] is False


def test_an_unknown_model_for_an_existing_agent_is_refused_at_open(rig):
    rig.open('gpt')
    conn, file, status, _ = rig.connect('/open', {'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home), 'model': 'no-such-model'})
    reply = json.loads(file.read()); file.close(); conn.close()
    assert status == 400 and 'unknown gpt model no-such-model' in reply['error']
    assert rig.turn('PING')[-1]['text'] == 'PONG'


def test_list_agents_shows_other_agents_without_their_commands_or_folders():
    m = load('lordd_list')
    async def main():
        a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '/secret/folder', 'model': 'gpt-6.1-sol'})
        a.activity = '$ curl -H "token: abc"'
        d = object.__new__(m.Daemon); d.agents = {'a': a}; d.log = lambda text: None
        return json.loads((await d.mcp_call(a, 'list_agents', {}, a.token))['content'][0]['text'])
    listed = asyncio.run(main())
    assert listed == {'you': 'a', 'agents': [{'agentId': 'a', 'family': 'gpt', 'model': 'gpt-6.1-sol', 'state': 'idle'}]}


def test_common_token_shapes_are_redacted():
    m = load('lordd_redact')
    text = m.Redactor()('ghp_' + 'a' * 36 + ' AKIA' + 'B' * 16 + ' x-api-key: k123 password=hunter2 keep')
    assert 'ghp_' not in text and 'AKIA' not in text and 'k123' not in text and 'hunter2' not in text and text.endswith('keep')


def test_a_user_config_value_of_the_wrong_type_is_refused(tmp_path, monkeypatch):
    m = load('lordd_types')
    (tmp_path / 'config.json').write_text(json.dumps({'grok': {'no_progress_s': '2400'}}))
    monkeypatch.setattr(m, 'HOME', tmp_path)
    with pytest.raises(ValueError, match='grok.no_progress_s'): m.load_config()


def test_a_reconnect_offers_every_unresolved_tool_call():
    m = load('lordd_calls')
    async def main():
        a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '.', 'model': 'gpt-6.1-sol'})
        a.state = 'waiting_tool'
        for call in ('c1', 'c2'):
            a.calls[call] = asyncio.get_running_loop().create_future(); a.call_data[call] = ('Agent', {'prompt': call})
        a.carry = [{'type': 'tool', 'callId': 'c2', 'name': 'Agent', 'input': {'prompt': 'c2'}}]
        d = object.__new__(m.Daemon); d.agents = {'a': a}; d.connections = set(); d.requests = 0; d.redact = str; d.control = 'k'
        class Writer:
            data = b''
            def write(self, data): self.data += data
            async def drain(self): pass
            def close(self): pass
            async def wait_closed(self): pass
        reader, writer = asyncio.StreamReader(), Writer()
        payload = json.dumps({'agentId': 'a', 'text': 'next'}).encode()
        reader.feed_data(b'POST /turn HTTP/1.1\r\nX-Lord-Control: k\r\nContent-Length: ' + str(len(payload)).encode() + b'\r\n\r\n' + payload)
        await d.http(reader, writer)
        return sorted(e['callId'] for e in a.carry) + [c for c in ('c1', 'c2') if f'"callId":"{c}"'.encode() in writer.data]
    assert sorted(asyncio.run(main())) == ['c1', 'c2']


def test_an_earlier_codex_turn_completing_before_the_new_turn_id_is_ignored():
    m = load('lordd_turn_ids')
    from unittest.mock import AsyncMock
    async def main():
        a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '.', 'model': 'gpt-6.1-sol', 'vendorSession': 's'})
        a.turn_id = 'old'
        d = object.__new__(m.Daemon); d.agents = {'a': a}; d.redact = str; d.save = lambda a: None
        d.effort, d.attach = AsyncMock(), AsyncMock()
        d.task = lambda coroutine: coroutine.close()
        await d.begin_locked(a, {'text': 'new'})
        m.Vendor(d, 'gpt', a).notification({'method': 'turn/completed', 'params': {'threadId': 's', 'turn': {'id': 'old', 'status': 'interrupted'}}})
        return a.state, a.events.empty()
    assert asyncio.run(main()) == ('running', True)


def test_path_refusals_ignore_letter_case_on_macos(rig):
    if sys.platform != 'darwin': pytest.skip('case-insensitive volumes are the macOS default')
    conn, file, status, _ = rig.connect('/open', {'agentId': 'h', 'family': 'gpt', 'cwd': str(Path.home()).upper()})
    reply = json.loads(file.read()); file.close(); conn.close()
    assert status == 400 and 'home folder' in reply['error']
    grok_home = rig.home / 'grok'
    grok_home.mkdir()
    conn, file, status, _ = rig.connect('/open', {'agentId': 'r', 'family': 'grok', 'cwd': str(grok_home).upper(), 'readonly': True})
    reply = json.loads(file.read()); file.close(); conn.close()
    assert status == 400 and 'read-only profile' in reply['error']


def test_a_failed_agy_listing_is_not_cached_and_ultra_stays_in_range():
    m = load('lordd_agy_list')
    async def main():
        d = object.__new__(m.Daemon); d.agy_levels, d.agy_models = {}, set(); d.config = {'gemini': {'program': 'agy'}}; d.log = lambda t: None
        calls = []
        async def quick(argv, timeout=20):
            calls.append(argv)
            return (1, 'down') if len(calls) == 1 else (0, 'gemini-4-argon-low\ngemini-4-argon-high\n')
        m.run_quick = quick
        first = await d.agy_efforts('gemini-4-argon')
        second = await d.agy_efforts('gemini-4-argon')
        a = m.Agent({'agentId': 'g', 'family': 'gemini', 'cwd': '.', 'model': 'gemini-4-argon'})
        d.agy_levels = {}
        m.run_quick = lambda argv, timeout=20: asyncio.sleep(0, (1, 'down'))
        await d.effort(a, 'ultra')
        return first, second, a.effort
    assert asyncio.run(main()) == (m.EFFORTS, ['low', 'high'], 'max')


def test_an_interrupt_after_a_detached_turn_finished_discards_its_result(rig):
    rig.open('gpt')
    stream = rig.stream('/turn', {'agentId': 'a', 'text': 'SLOW'})
    assert next(stream)['type'] == 'activity'
    stream.close()
    time.sleep(.3)
    assert rig.request('/steer', {'agentId': 'a', 'text': 'detached-result'})['steered']
    deadline = time.monotonic() + 5
    while rig.request('/agents')[0]['state'] != 'idle' and time.monotonic() < deadline: time.sleep(.05)
    rig.request('/interrupt', {'agentId': 'a'})
    assert rig.turn('PING')[-1]['text'] == 'PONG'


def test_a_refused_model_change_puts_the_agents_options_back(rig):
    rig.open('gpt')
    conn, file, status, _ = rig.connect('/open', {'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home), 'net': True, 'model': 'no-such-model'})
    file.close(); conn.close()
    assert status == 400
    rig.turn('PING')
    params = [e['params'] for e in rig.audit() if e.get('method') == 'turn/start'][-1]
    assert params['sandboxPolicy']['networkAccess'] is False


def test_an_opened_agent_is_not_pruned_before_its_turn(rig):
    rig.open('gpt')
    journal = rig.home / 'journal.jsonl'
    old = json.loads(journal.read_text().splitlines()[-1]); old['used'] = '2000-01-01T00:00:00+00:00'
    journal.write_text(json.dumps(old) + '\n')
    rig.stop(); rig.start()
    rig.open('gpt')
    assert rig.request('/prune', {'days': 1})['pruned'] == []
    assert rig.turn('PING')[-1]['text'] == 'PONG'


def test_folder_refusals_see_through_macos_data_volume_paths(rig):
    if sys.platform != 'darwin': pytest.skip('macOS data volume')
    for cwd in ('/System/Volumes/Data', '/System/Volumes/Data' + str(Path.home())):
        conn, file, status, _ = rig.connect('/open', {'agentId': 'd', 'family': 'gpt', 'cwd': cwd})
        reply = json.loads(file.read()); file.close(); conn.close()
        assert status == 400 and 'home folder' in reply['error']
    temp = Path(tempfile.mkdtemp(dir='/private/var/tmp'))
    try:
        conn, file, status, _ = rig.connect('/open', {'agentId': 'r', 'family': 'grok', 'cwd': '/System/Volumes/Data' + str(temp), 'readonly': True})
        reply = json.loads(file.read()); file.close(); conn.close()
        assert status == 400 and 'read-only profile' in reply['error']
    finally: temp.rmdir()


def test_relative_folders_are_refused_and_a_pinned_folder_is_compared_as_a_folder(rig):
    conn, file, status, _ = rig.connect('/open', {'agentId': 'a', 'family': 'gpt', 'cwd': 'relative/folder'})
    reply = json.loads(file.read()); file.close(); conn.close()
    assert status == 400 and 'absolute' in reply['error']
    rig.open('gpt')
    assert rig.open('gpt', pin_cwd=True, cwd=str(rig.home) + '/./')['vendorSession']
    other = rig.home / 'other'; other.mkdir()
    conn, file, status, _ = rig.connect('/open', {'agentId': 'a', 'family': 'gpt', 'cwd': str(other), 'pin_cwd': True})
    reply = json.loads(file.read()); file.close(); conn.close()
    assert status == 400 and 'fixed at the first turn' in reply['error']


def test_secrets_in_headers_and_json_are_redacted():
    m = load('lordd_redact_json')
    text = m.Redactor()('Authorization: Bearer opaque123 {"password": "pw-value", "api_key": "k-value"} token=t-value keep')
    assert not any(s in text for s in ('opaque123', 'pw-value', 'k-value', 't-value')) and text.endswith('keep')


def test_a_family_section_that_is_not_an_object_is_refused(tmp_path, monkeypatch):
    m = load('lordd_family')
    (tmp_path / 'config.json').write_text(json.dumps({'grok': 'grok-5'}))
    monkeypatch.setattr(m, 'HOME', tmp_path)
    with pytest.raises(ValueError, match='grok in config.json must be an object'): m.load_config()


def test_vendors_start_with_the_latest_session_path(rig):
    rig.request('/open', {'agentId': 'p', 'family': 'grok', 'cwd': str(rig.home), 'path': '/session/bin:' + os.environ['PATH']})
    launch = [e for e in rig.audit() if e.get('launch')][-1]
    assert launch['path'].startswith('/session/bin:')


def test_an_interrupt_while_a_turn_is_starting_stops_it_and_leaves_nothing():
    m = load('lordd_starting')
    from unittest.mock import AsyncMock
    async def main():
        a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '.', 'model': 'gpt-6.1-sol', 'vendorSession': 's'})
        d = object.__new__(m.Daemon); d.agents = {'a': a}; d.redact = str; d.save = lambda a: None
        async def slow_attach(a): await asyncio.sleep(.1)
        d.effort, d.attach = AsyncMock(), slow_attach
        d.backend = lambda a: None
        d.task = asyncio.create_task
        begin = asyncio.create_task(d.begin_locked(a, {'text': 'x'}))
        await asyncio.sleep(.02)
        await d.interrupt(a)
        await begin
        await asyncio.sleep(.05)
        return a.state, a.undelivered()
    assert asyncio.run(main()) == ('idle', False)


def test_kill_tree_survives_a_group_of_zombies(monkeypatch):
    m = load('lordd_eperm')
    def eperm(*args): raise PermissionError(1, 'Operation not permitted')
    monkeypatch.setattr(m.os, 'killpg', eperm)
    async def main():
        process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(30)', start_new_session=True)
        await m.kill_tree(process)
        return process.returncode
    assert asyncio.run(main()) is not None


def test_prefixed_secret_names_and_basic_credentials_are_redacted():
    r = load('lordd_redact_names').Redactor()
    for text in ('export GITHUB_TOKEN=val1abcdefgh', 'curl -d client_secret=val2abcdefgh', '{"access_token": "val3abcdefgh"}',
                 'DB_PASSWORD=val4abcdefgh psql', 'Authorization: Basic dmFsNWFiY2RlZmdo'):
        assert 'val' not in r(text) and 'dmFs' not in r(text), r(text)
    assert r('tokens used 9,032') == 'tokens used 9,032'


def test_a_crashed_vendors_descendants_in_another_group_are_stopped():
    m = load('lordd_orphans')
    async def main():
        code = ('import os, subprocess, sys, time\n'
                'c = subprocess.Popen([sys.executable, "-c", "import os, time; os.setsid(); time.sleep(60)"])\n'
                'print(c.pid, flush=True)\n')
        parent = await asyncio.create_subprocess_exec(sys.executable, '-c', code, stdout=-1, start_new_session=True)
        child = int((await parent.stdout.readline()).decode())
        await parent.wait()
        await asyncio.sleep(.3)
        await m.kill_tree(parent, {child})
        await asyncio.sleep(.3)
        try: os.kill(child, 0)
        except ProcessLookupError: return True
        os.kill(child, signal.SIGKILL)
        return False
    assert asyncio.run(main())


def test_grok_effort_is_clamped_before_grok_reports_its_levels():
    m = load('lordd_grok_levels')
    async def main():
        a = m.Agent({'agentId': 'k', 'family': 'grok', 'cwd': '.', 'model': 'grok-4.7'})
        d = object.__new__(m.Daemon); d.vendors = {}
        await d.effort(a, 'ultra')
        return a.effort
    assert asyncio.run(main()) == 'xhigh'


def test_an_aws_secret_key_assignment_is_redacted():
    r = load('lordd_redact_aws').Redactor()
    assert 'abc123' not in r('aws_secret_access_key=abc123def456 AWS_SECRET_ACCESS_KEY: abc123xyz')


def test_an_extra_folder_holding_the_home_folder_is_refused(rig):
    conn, file, status, _ = rig.connect('/open', {'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home), 'dirs': ['/']})
    reply = json.loads(file.read()); file.close(); conn.close()
    assert status == 400 and 'holding your home folder' in reply['error']


def test_a_default_model_the_vendor_does_not_offer_gives_way_to_the_vendors_default(rig):
    grok = [PYTHON, str(ROOT / 'tests/fakes/vendor.py'), 'grok', str(rig.data), '--list-models']
    restart_with(rig, **{'gpt.model': 'gpt-retired', 'grok.model': 'grok-retired', 'grok.program': grok, 'gemini.model': 'gemini-retired'})
    # codex marks its default, grok names its current model, and agy's first listed model stands for its default.
    models = {family: rig.open(family, agent=family)['model'] for family in ('gpt', 'grok', 'gemini')}
    assert models == {'gpt': 'gpt-6-luna', 'grok': 'grok-4.7', 'gemini': 'gemini-4-argon'}
    assert [rig.turn('PING', agent=family)[-1]['text'] for family in models] == ['PONG'] * 3
    audit = rig.audit()
    assert [e['params']['model'] for e in audit if e.get('method') == 'turn/start'] == ['gpt-6-luna']
    assert {'configId': 'model', 'value': 'grok-4.7'} in [{k: e['params'].get(k) for k in ('configId', 'value')} for e in audit if e.get('method') == 'session/set_config_option']
    assert any(e['argv'][e['argv'].index('--model') + 1] == 'gemini-4-argon' for e in audit if e.get('launch') and '--model' in e['argv'])
    assert {a['agentId']: a['model'] for a in rig.request('/agents')} == models
    assert 'codex does not offer gpt-retired; using gpt-6-luna' in (rig.home / 'lordd.log').read_text()


@pytest.mark.parametrize('family', ['gpt', 'grok', 'gemini'])
def test_a_named_model_the_vendor_does_not_offer_is_refused_for_a_new_agent(rig, family):
    restart_with(rig, **{'grok.program': [PYTHON, str(ROOT / 'tests/fakes/vendor.py'), 'grok', str(rig.data), '--list-models']})
    conn, file, status, _ = rig.connect('/open', {'agentId': 'n', 'family': family, 'cwd': str(rig.home), 'model': 'no-such-model'})
    reply = json.loads(file.read()); file.close(); conn.close()
    assert status == 400 and f'unknown {family} model no-such-model' in reply['error']
    assert rig.request('/agents') == []


def test_the_agy_entry_exists_only_while_the_daemon_runs(rig):
    rig.open('gemini', agent='g1')
    rig.open('gemini', agent='g2')
    assert [a['mcp_add'][0] for a in rig.audit() if 'mcp_add' in a] == ['lord', 'lord']
    assert not [a for a in rig.audit() if 'mcp_remove' in a]
    rig.stop()
    assert [a['mcp_remove'] for a in rig.audit() if 'mcp_remove' in a] == [['lord']]
    # A daemon that never started a gemini agent leaves agy's settings alone.
    rig.start()
    rig.open('gpt')
    rig.stop()
    assert len([a for a in rig.audit() if 'mcp_remove' in a]) == 1


def test_a_missing_grok_writes_no_sandbox_profiles(rig):
    restart_with(rig, **{'grok.program': [str(rig.home / 'no-such-grok')]})
    conn, file, status, _ = rig.connect('/open', {'agentId': 'g', 'family': 'grok', 'cwd': str(rig.home)})
    reply = json.loads(file.read()); file.close(); conn.close()
    assert status != 200 and 'was not found' in reply['error'] and 'x.ai/cli/install.sh' in reply['error']
    assert not (rig.home / 'grok' / 'sandbox.toml').exists()


def test_health_reports_the_plugin_version(rig):
    assert rig.request('/health')['version'] == json.loads((ROOT / '.claude-plugin/plugin.json').read_text())['version']


def test_a_newer_plugin_retires_the_daemon_once_its_work_is_done(rig):
    restart_with(rig, retire_idle_s=.3)
    rig.open('gpt', version='999.0.0')
    stream = rig.stream('/turn', {'agentId': 'a', 'text': 'SLOW'})
    assert next(stream)['type'] == 'activity'
    time.sleep(.8)
    assert rig.proc.poll() is None
    rig.request('/interrupt', {'agentId': 'a'})
    list(stream)
    rig.proc.wait(timeout=5)
    assert rig.proc.returncode == 0 and not (rig.home / 'lordd.sock').exists()


@pytest.mark.parametrize('version', [None, '0.1.0', 'same', 'not-a-version'])
def test_an_older_or_equal_plugin_does_not_retire_the_daemon(rig, version):
    restart_with(rig, retire_idle_s=.3)
    if version == 'same':
        version = rig.request('/health')['version']
    rig.open('gpt', **({} if version is None else {'version': version}))
    # Longer than one heartbeat after the grace period, in which a retiring daemon would have exited.
    time.sleep(1.6)
    assert rig.proc.poll() is None


def test_a_new_daemon_takes_over_from_one_that_is_shutting_down(rig):
    rig.stop()
    with (rig.home / 'lordd.lock').open('a') as held:
        # Stands for a daemon that has stopped answering and not yet released its lock.
        fcntl.flock(held, fcntl.LOCK_EX)
        rig.proc = subprocess.Popen([PYTHON, str(DAEMON)], env=rig.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(.5)
        assert rig.proc.poll() is None
    deadline = time.monotonic() + 5
    while True:
        try:
            assert rig.request('/health')['pid'] == rig.proc.pid
            break
        except OSError:
            assert time.monotonic() < deadline and rig.proc.poll() is None
            time.sleep(.05)


def test_a_vendor_that_is_not_signed_in_says_how_to_sign_in(rig):
    for family in ('gpt', 'grok'):
        restart_with(rig, **{f'{family}.program': [PYTHON, str(ROOT / 'tests/fakes/vendor.py'), family, str(rig.data), '--signed-out']})
    rig.open('gpt', agent='g')
    assert 'codex is not signed in; run `codex login`' in rig.turn('PING', agent='g')[-1]['message']
    conn, file, status, _ = rig.connect('/open', {'agentId': 'k', 'family': 'grok', 'cwd': str(rig.home)})
    reply = json.loads(file.read()); file.close(); conn.close()
    assert status != 200 and 'Authentication required' in reply['error'] and 'grok is not signed in; run `grok` once' in reply['error']
    # Other failures keep their own text.
    assert load('lordd_sign_in').sign_in_hint('gpt', 'gpt process closed stdout') == 'gpt process closed stdout'


def start_version(rig, version):
    # A copy of the daemon whose plugin.json names another version, on that version's own socket.
    root = rig.home / f'plugin-{version}'
    (root / '.claude-plugin').mkdir(parents=True)
    (root / '.claude-plugin/plugin.json').write_text(json.dumps({'version': version}))
    shutil.copy(DAEMON, root / 'lordd.py')
    env = {k: v for k, v in rig.env.items() if k != 'LORD_DAEMON_SOCKET'}
    proc = subprocess.Popen([PYTHON, str(root / 'lordd.py')], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = Client(rig.home)
    client.sock = rig.home / f'lordd-{version}.sock'
    deadline = time.monotonic() + 10
    while True:
        assert proc.poll() is None
        try:
            if client.request('/health')['version'] == version: return proc, client
        except OSError:
            assert time.monotonic() < deadline
            time.sleep(.025)


def stop_version(proc):
    proc.terminate()
    proc.wait(timeout=10)


def test_daemons_of_two_versions_serve_side_by_side_with_one_key(rig):
    key = (rig.home / 'control.header').read_text()
    rig.open('gpt', agent='old')
    proc, new = start_version(rig, '9.9.9')
    try:
        # The running daemon's plugins keep their key, and neither daemon waits for the other.
        assert (rig.home / 'control.header').read_text() == key
        new.request('/open', {'agentId': 'fresh', 'family': 'gpt', 'cwd': str(rig.home)})
        assert list(new.stream('/turn', {'agentId': 'fresh', 'text': 'PING'}))[-1]['text'] == 'PONG'
        assert rig.turn('PING', agent='old')[-1]['text'] == 'PONG'
        rig.stop()
        assert list(new.stream('/turn', {'agentId': 'fresh', 'text': 'PING'}))[-1]['text'] == 'PONG'
    finally: stop_version(proc)


@pytest.mark.parametrize('family', ['gpt', 'gemini'])
def test_a_vendor_reaches_the_daemon_that_started_it(rig, family):
    proc, new = start_version(rig, '9.9.9')
    try:
        new.request('/open', {'agentId': 'a', 'family': family, 'cwd': str(rig.home)})
        events = list(new.stream('/turn', {'agentId': 'a', 'text': 'LIST'}))
        assert events[-1]['type'] == 'done'
        assert json.loads(json.loads(events[-1]['text'])['content'][0]['text'])['you'] == 'a'
    finally: stop_version(proc)


def test_an_agent_moves_between_daemons_with_its_vendor_session(rig):
    proc, new = start_version(rig, '9.9.9')
    try:
        new.request('/open', {'agentId': 'y', 'family': 'gpt', 'cwd': str(rig.home)})
        # Started after the newer daemon read the journal.
        sid = rig.open('gpt', agent='x')['vendorSession']
        assert rig.turn('secret-word', agent='x')[-1]['type'] == 'done'
        assert new.request('/open', {'agentId': 'x', 'family': 'gpt', 'cwd': str(rig.home)})['vendorSession'] == sid
        assert list(new.stream('/turn', {'agentId': 'x', 'text': 'REMEMBER'}))[-1]['text'] == 'secret-word'
        assert 'y' in [a['agentId'] for a in rig.request('/agents')]
        # Each daemon's journal rewrite keeps the agents the other added.
        new.request('/prune', {'days': 14})
        rig.request('/prune', {'days': 14})
        assert {json.loads(line)['agentId'] for line in (rig.home / 'journal.jsonl').read_text().splitlines()} == {'x', 'y'}
    finally: stop_version(proc)


def test_a_first_journal_starts_from_the_earlier_daemons_records(rig):
    sid = rig.open('gpt', agent='x')['vendorSession']
    rig.stop()
    journal = rig.home / 'journal.jsonl'
    (rig.home / 'sessions.jsonl').write_text(journal.read_text())
    journal.unlink()
    rig.start()
    # The start's journal rewrite carries the earlier records over.
    deadline = time.monotonic() + 5
    while not journal.exists() or 'x' not in journal.read_text():
        assert time.monotonic() < deadline
        time.sleep(.05)
    assert rig.open('gpt', agent='x')['vendorSession'] == sid

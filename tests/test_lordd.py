"""HTTP and MCP contract tests against strict subprocess protocol peers."""
import asyncio
import contextlib
import fcntl
import hashlib
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
    # The user's codex folder in tests is the vendor data folder's .codex.
    monkeypatch.delenv('CODEX_HOME', raising=False); monkeypatch.delenv('CODEX_SQLITE_HOME', raising=False)
    for vendor in ('CLAUDE', 'CURSOR'):
        for cell in ('MCPS', 'HOOKS', 'SKILLS'): monkeypatch.delenv(f'GROK_{vendor}_{cell}_ENABLED', raising=False)


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


def test_gpt_codex_keeps_threads_in_lords_folder_linked_to_the_users(rig):
    source, own = rig.data / '.codex', rig.home / 'codex'
    (source / 'skills').mkdir(parents=True); (source / 'rules').mkdir()
    (source / 'auth.json').write_text('{}')
    # Trust for a project's hooks is keyed by the project's path, a tool server's cwd is not resolved when the
    # settings load, and a path under a linked name resolves the same in both folders: none stops the sharing.
    (source / 'agents').mkdir(); (source / 'agents/x.toml').write_text('')
    (source / 'config.toml').write_text('role_file = "agents/x.toml"\n[hooks.state]\n[mcp_servers.tool]\ncwd = "."\n')
    (own / 'rules').mkdir(parents=True)
    # A start before the user had skills left codex's own skills/.system, which gives way to the user's skills.
    (own / 'skills/.system').mkdir(parents=True)
    rig.stop(); rig.env['CODEX_SQLITE_HOME'] = str(rig.data / 'elsewhere'); rig.start()
    rig.open('gpt')
    assert rig.turn('PING')[-1]['type'] == 'done'
    launches = [e for e in rig.audit() if e.get('launch') and e['family'] == 'gpt']
    assert {e['codex_home'] for e in launches} == {str(own)} and {e['sqlite_home'] for e in launches} == {None}
    # The sign-in locks folder is made in the user's folder, so both folders lock the same files.
    for name in ('auth.json', 'config.toml', 'skills', 'mcp-oauth-locks'):
        assert (own / name).is_symlink() and os.readlink(own / name) == str(source / name)
    assert (source / 'mcp-oauth-locks').is_dir()
    # What is already in Lord's folder stays; what the user's folder lacks is not linked.
    assert not (own / 'rules').is_symlink() and not os.path.lexists(own / 'plugins')


def test_login_lords_folder_cannot_share_keeps_the_users_folder(rig):
    source = rig.data / '.codex'; source.mkdir(parents=True)
    rig.open('gpt', agent='no-login')
    # codex names a keyring login after its folder, so another folder would be signed out.
    (source / 'auth.json').write_text('{}'); (source / 'config.toml').write_text('cli_auth_credentials_store = "keyring"\n')
    rig.open('gpt', agent='keyring')
    # In auto mode codex moves the login into the keychain, under its own folder's name, at every save.
    (source / 'config.toml').write_text('cli_auth_credentials_store = "auto"\n')
    rig.open('gpt', agent='auto')
    # codex trusts a hook by the path it read it from, so the user's hooks would not run from Lord's folder.
    (source / 'config.toml').write_text('[[hooks.pre_tool_use]]\n')
    rig.open('gpt', agent='toml-hook')
    (source / 'config.toml').write_text(''); (source / 'hooks.json').write_text('{}')
    rig.open('gpt', agent='json-hook')
    # A database folder in the settings would put the threads back in the user's.
    (source / 'hooks.json').unlink(); (source / 'config.toml').write_text('sqlite_home = "/elsewhere"\n')
    rig.open('gpt', agent='sqlite-home')
    (source / 'config.toml').write_text('log_dir = "/elsewhere"\n')
    rig.open('gpt', agent='log-dir')
    # codex resolves a relative path in the settings against the folder it runs in.
    (source / 'base.md').write_text(''); (source / 'config.toml').write_text('model_instructions_file = "base.md"\n')
    rig.open('gpt', agent='relative')
    (source / 'config.toml').write_text('[features]\nsecret_auth_storage = true\n')
    rig.open('gpt', agent='secret-store')
    # codex writes tool sign-ins kept in a file only as a regular file, so a link could not share them.
    (source / 'config.toml').write_text(''); (source / '.credentials.json').write_text('{}')
    rig.open('gpt', agent='mcp-file')
    (source / '.credentials.json').unlink()
    rig.stop()
    config = json.loads(rig.config.read_text()); config['gpt']['own_home'] = False; rig.config.write_text(json.dumps(config))
    rig.start()
    rig.open('gpt', agent='off')
    assert [e['codex_home'] for e in rig.audit() if e.get('launch') and e['family'] == 'gpt'] == [None] * 11
    assert not (rig.home / 'codex').exists()


def test_setup_that_stops_being_shareable_refuses_the_agent_with_the_reason(rig):
    source = rig.data / '.codex'; source.mkdir(parents=True); (source / 'auth.json').write_text('{}')
    rig.open('gpt', agent='early')
    rig.stop()
    (source / 'hooks.json').write_text('{}')
    rig.start()
    with pytest.raises(AssertionError, match="can no longer share your codex setup: you have codex hooks.*undo that and it resumes"):
        rig.turn('PING', agent='early')
    assert [e['codex_home'] for e in rig.audit() if e.get('launch') and e['family'] == 'gpt'] == [str(rig.home / 'codex')]
    # Pruning cannot archive its thread meanwhile, so it keeps the record and archives once the hook is gone.
    rig.stop()
    records = [json.loads(line) for line in (rig.home / 'journal.jsonl').read_text().splitlines()]
    for row in records: row['created'] = row['used'] = '2000-01-01T00:00:00+00:00'
    (rig.home / 'journal.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in records))
    prune = [PYTHON, str(DAEMON), '--prune', '--days', '14']
    assert subprocess.run(prune, env=rig.env, capture_output=True, text=True, timeout=12).returncode == 0
    assert 'early' in (rig.home / 'journal.jsonl').read_text() and not [e for e in rig.audit() if 'archived' in e]
    (source / 'hooks.json').unlink()
    assert subprocess.run(prune, env=rig.env, capture_output=True, text=True, timeout=12).returncode == 0
    assert [e['archived'] for e in rig.audit() if 'archived' in e] == [records[-1]['vendorSession']]


def test_signed_out_user_is_told_to_sign_in(rig):
    source = rig.data / '.codex'; source.mkdir(parents=True); (source / 'auth.json').write_text('{}')
    rig.open('gpt', agent='early')
    rig.stop(); (source / 'auth.json').unlink(); rig.start()
    with pytest.raises(AssertionError, match="run `codex login` and it resumes"):
        rig.turn('PING', agent='early')
    (source / 'auth.json').write_text('{}')
    assert rig.turn('PING', agent='early')[-1]['type'] == 'done'


def test_idle_codex_of_any_folder_is_closed(rig, monkeypatch):
    async def scenario():
        d = local_daemon(rig, monkeypatch)
        (rig.data / '.codex').mkdir(parents=True); (rig.data / '.codex/auth.json').write_text('{}')
        try:
            services = [d.service(), d.service(str(rig.home / 'codex'))]
            for v in services:
                await v.ensure(); v.last = 0
            await d.guard(time.monotonic())
            assert all(v.process is None or v.process.returncode is not None for v in services)
        finally:
            await d.close()
    asyncio.run(scenario())


def test_agent_saved_without_codex_home_keeps_the_users_folder_and_prunes_in_its_own(rig):
    source = rig.data / '.codex'; source.mkdir(parents=True); (source / 'auth.json').write_text('{}')
    sessions = {agent: rig.open('gpt', agent=agent)['vendorSession'] for agent in ('old', 'new')}
    rig.stop()
    path = rig.home / 'journal.jsonl'
    records = [json.loads(line) for line in path.read_text().splitlines()]
    for row in records:
        # As a daemon before 2.1.2 saved it.
        if row['agentId'] == 'old': row.pop('codexHome')
    path.write_text(''.join(json.dumps(row) + '\n' for row in records))
    rig.start()
    assert rig.turn('PING', agent='old')[-1]['type'] == 'done'
    assert [e for e in rig.audit() if e.get('launch')][-1]['codex_home'] is None
    rig.stop()
    records = [json.loads(line) for line in path.read_text().splitlines()]
    for row in records: row['created'] = row['used'] = '2000-01-01T00:00:00+00:00'
    path.write_text(''.join(json.dumps(row) + '\n' for row in records))
    result = subprocess.run([PYTHON, str(DAEMON), '--prune', '--days', '14'], env=rig.env, capture_output=True, text=True, timeout=12)
    assert result.returncode == 0, result.stderr
    homes = {e['pid']: e['codex_home'] for e in rig.audit() if e.get('launch')}
    archived = {e['archived']: homes[e['pid']] for e in rig.audit() if 'archived' in e}
    assert archived == {sessions['old']: None, sessions['new']: str(rig.home / 'codex')}


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
            a = d.agents['a']; v = d.service(a.record.get('codexHome')); original = v.rpc
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
    # The earlier answer stays an answer; the refused follow-up's reason follows it.
    assert last['type'] == 'done' and 'detached-result' in last['text'] and 'The new message failed' in last['text'] and 'no-such-model' in last['text']
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



def test_a_new_turn_does_not_replay_carried_steps_or_narration_of_the_earlier_one():
    m = load('lordd_carry')
    async def main():
        a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '.', 'model': 'gpt-6.1-sol'})
        # Held over from a client that went away: a step and narration, then the earlier turn's result.
        a.carry = [{'type': 'row', 'rowId': 'r', 'shell': True, 'title': 'old step', 'text': 'old step'}, {'type': 'text', 'text': 'old narration'},
                   {'type': 'done', 'text': 'earlier answer', 'usage': {}, 'model': 'm'}]
        d = object.__new__(m.Daemon); d.agents = {'a': a}; d.connections = set(); d.requests = 0; d.redact = str; d.control = 'k'
        class Writer:
            data = b''
            def write(self, data): self.data += data
            async def drain(self): pass
            def close(self): pass
            async def wait_closed(self): pass
        reader, writer = asyncio.StreamReader(), Writer()
        payload = json.dumps({'agentId': 'a', 'text': 'next', 'rows': True}).encode()
        reader.feed_data(b'POST /turn HTTP/1.1\r\nX-Lord-Control: k\r\nContent-Length: ' + str(len(payload)).encode() + b'\r\n\r\n' + payload)
        await d.http(reader, writer)
        return writer.data
    data = asyncio.run(main())
    assert b'old step' not in data and b'old narration' not in data and b'earlier answer' in data

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
        d = object.__new__(m.Daemon); d.quota = {}; d.agents = {'a': a}; d.redact = str; d.save = lambda a: None; d.config = {'gpt': {}}
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
        d = object.__new__(m.Daemon); d.quota = {}; d.agents = {'a': a}; d.redact = str; d.save = lambda a: None; d.config = {'gpt': {}}
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


def row_turn(rig, text, agent='a'):
    # Reads a turn as the plugin does: each step's row is answered from /row before the stream continues.
    rows, seen, path, body = [], [], '/turn', {'agentId': agent, 'text': text, 'rows': True}
    while True:
        events = list(rig.stream(path, body))
        seen += events
        if events[-1]['type'] != 'row': return rows, seen
        rows.append((events[-1], rig.request('/row', {'agentId': agent, 'rowId': events[-1]['rowId']})))
        path, body = '/continue', {'agentId': agent, 'rows': True}


@pytest.mark.parametrize('family', ['gpt', 'grok', 'gemini'])
def test_each_vendor_step_is_a_row_with_its_redacted_capped_output(rig, family):
    rig.open(family)
    rows, events = row_turn(rig, 'STEPS')
    assert events[-1]['type'] == 'done' and events[-1]['text'] == 'STEPS'
    command = next(result for row, result in rows if row['shell'] and row['title'] == 'printf hi')
    assert 'sk-abcdefghijklmnopqrstuv' not in command['output'] and '[REDACTED]' in command['output']
    assert command['output'].startswith('[') and 'earlier characters not shown' in command['output'].splitlines()[0]
    assert len(command['output'].split('\n', 1)[1]) <= 2000 and command['output'].endswith('(exit code 2)')
    edit = next(result for row, result in rows if not row['shell'] and 'a.txt' in row['title'])
    assert not any('{' in row['title'] for row, _ in rows)
    assert 'done' in edit['output'] and command['stopped'] is False
    # The activity log keeps one line per step.
    assert sum('printf hi' in line for line in (rig.home / 'logs' / 'a.log').read_text().splitlines()) == 1


def test_a_client_without_rows_gets_each_step_as_one_activity_line(rig):
    rig.open('gpt')
    events = rig.turn('STEPS')
    assert [e['text'] for e in events if e['type'] == 'activity'] == ['$ fake work', '$ /bin/zsh -lc "printf hi"', 'edit a.txt']
    assert events[-1]['text'] == 'STEPS' and not any(e['type'] == 'row' for e in events)


def test_a_stop_answers_a_step_still_waiting(rig):
    rig.open('gpt')
    events = list(rig.stream('/turn', {'agentId': 'a', 'text': 'SLOW', 'rows': True}))
    assert events[-1]['type'] == 'row'
    answer = {}
    waiter = threading.Thread(target=lambda: answer.update(rig.request('/row', {'agentId': 'a', 'rowId': events[-1]['rowId']})))
    waiter.start()
    time.sleep(.3)
    assert waiter.is_alive()
    assert rig.request('/interrupt', {'agentId': 'a'}) == {'ok': True}
    waiter.join(5)
    assert answer == {'output': '', 'stopped': True}


def test_a_turn_finished_while_disconnected_replays_no_steps_or_narration(rig):
    rig.open('gpt')
    stream = rig.stream('/turn', {'agentId': 'a', 'text': 'PING', 'rows': True})
    assert next(stream)['type'] == 'row'
    stream.close()
    deadline = time.monotonic() + 5
    while rig.request('/agents')[0]['state'] != 'idle':
        assert time.monotonic() < deadline
        time.sleep(.05)
    rows, events = row_turn(rig, 'PONG-AGAIN')
    assert [row['title'] for row, _ in rows] == ['fake work']
    # Only the new turn's narration is shown: the earlier answer's pieces ('PO', 'NG') are not replayed.
    assert [e['text'] for e in events if e['type'] == 'text'] == ['PO', 'NG-AGAIN']
    assert events[-1]['text'].startswith('Result of the earlier task') and events[-1]['text'].endswith('PONG-AGAIN')


def test_a_spent_gemini_quota_is_an_error_with_its_reset_time_and_refuses_the_next_turn(rig):
    rig.open('gemini')
    failed = rig.turn('QUOTA')[-1]
    assert failed['type'] == 'error' and 'RESOURCE_EXHAUSTED (code 429)' in failed['message'] and 'Resets in 104h27m0s' in failed['message']
    log = (rig.home / 'logs' / 'a.log').read_text()
    assert 'error: RESOURCE_EXHAUSTED (code 429)' in log
    conn, file, status, _ = rig.connect('/turn', {'agentId': 'a', 'text': 'PING'})
    try:
        body = json.loads(file.read())
    finally:
        file.close(); conn.close()
    assert status == 400 and 'quota' in body['error'] and 'resets' in body['error']
    launches = [e for e in rig.audit() if e.get('launch')]
    assert len(launches) == 1
    # The plugin opens before each turn: a spent quota is refused there too, before agy starts, and logged.
    conn, file, status, _ = rig.connect('/open', {'agentId': 'a', 'family': 'gemini', 'cwd': str(rig.home)})
    try:
        body = json.loads(file.read())
    finally:
        file.close(); conn.close()
    assert status == 400 and 'quota' in body['error']
    assert len([e for e in rig.audit() if e.get('launch')]) == 1
    assert 'refused: gemini quota reached' in (rig.home / 'logs' / 'a.log').read_text()
    # A daemon started later still knows the reset.
    rig.stop(); rig.start()
    conn, file, status, _ = rig.connect('/turn', {'agentId': 'a', 'text': 'PING'})
    try:
        body = json.loads(file.read())
    finally:
        file.close(); conn.close()
    assert status == 400 and 'quota' in body['error']


def test_every_turn_error_is_in_the_agents_log(rig):
    rig.open('gpt')
    rig.turn('FAIL')
    assert 'error: failed to load workspace requirements' in (rig.home / 'logs' / 'a.log').read_text()


def test_each_log_record_is_one_line(rig):
    rig.open('gpt')
    rig.turn('HEREDOC')
    lines = (rig.home / 'logs' / 'a.log').read_text().splitlines()
    record = next(line for line in lines if 'line one' in line)
    assert all(part in record for part in ('line two', 'line three', 'line four', 'line five')) and all(line[:4].isdigit() for line in lines)


def test_agys_error_line_counts_when_it_arrives_after_its_output_closes(rig):
    rig.open('gemini')
    failed = rig.turn('QUOTA_LATE')[-1]
    assert failed['type'] == 'error' and 'RESOURCE_EXHAUSTED' in failed['message']


def test_an_earlier_agy_error_does_not_name_a_later_crash(rig):
    rig.open('gemini')
    first = rig.turn('AGYFAIL')[-1]
    assert first['type'] == 'error' and 'boom' in first['message'] and first['retryable'] is False
    later = rig.turn('CRASH')[-1]
    assert later['type'] == 'error' and 'boom' not in later['message']


def test_daemons_sharing_quota_json_keep_each_others_resets_and_a_new_agents_refusal_is_logged(rig):
    later = time.time() + 3600
    (rig.home / 'quota.json').write_text(json.dumps({'gemini gemini-4-argon': [later, 'written by another daemon']}))
    rig.open('gemini')
    rig.turn('QUOTA')
    saved = json.loads((rig.home / 'quota.json').read_text())
    assert set(saved) == {'gemini gemini-4-argon', 'gemini gemini-3.8-flash'}
    conn, file, status, _ = rig.connect('/open', {'agentId': 'b', 'family': 'gemini', 'cwd': str(rig.home), 'model': 'gemini-4-argon'})
    try:
        body = json.loads(file.read())
    finally:
        file.close(); conn.close()
    assert status == 400 and 'written by another daemon' in body['error']
    # A new agent has no log of its own yet: its refusal is in lordd.log, and no log nothing would remove is left behind.
    assert 'b: refused: gemini quota reached' in (rig.home / 'lordd.log').read_text() and not (rig.home / 'logs' / 'b.log').exists()
    # Another daemon's reset written after this one started is honoured, and kept when this one records its own.
    saved['grok grok-4.7'] = [time.time() + 3600, 'grok spent']
    (rig.home / 'quota.json').write_text(json.dumps(saved))
    conn, file, status, _ = rig.connect('/open', {'agentId': 'c', 'family': 'grok', 'cwd': str(rig.home)})
    file.close(); conn.close()
    assert status == 400
    # Deleting the file clears every refusal at once.
    (rig.home / 'quota.json').unlink()
    assert rig.open('grok', agent='c')['vendorSession']


def test_a_latin1_locale_does_not_break_the_logs(rig):
    rig.stop()
    rig.env.update(LC_ALL='en_US.ISO8859-1', LANG='en_US.ISO8859-1', PYTHONUTF8='0')
    rig.start()
    rig.open('gpt')
    assert rig.turn('HEREDOC')[-1]['type'] == 'done'
    rig.open('gemini', agent='g')
    failed = rig.turn('QUOTA_UNICODE', agent='g')[-1]
    assert failed['type'] == 'error' and 'RESOURCE_EXHAUSTED' in failed['message']
    assert '\u2713' in (rig.home / 'lordd.log').read_text(encoding='utf-8')


def test_a_quota_agy_reports_at_start_up_is_named_and_not_retried(rig):
    rig.data.mkdir(parents=True, exist_ok=True)
    (rig.data / 'start-quota').touch()
    conn, file, status, _ = rig.connect('/open', {'agentId': 'a', 'family': 'gemini', 'cwd': str(rig.home)})
    try:
        body = json.loads(file.read())
    finally:
        file.close(); conn.close()
    assert status != 200 and 'RESOURCE_EXHAUSTED' in body['error']
    assert len([e for e in rig.audit() if e.get('launch')]) == 1
    # A new agent refused at its first start has no log of its own, which nothing would remove.
    assert 'a: refused: gemini quota reached' in (rig.home / 'lordd.log').read_text() and not (rig.home / 'logs' / 'a.log').exists()


def test_a_held_answer_is_still_delivered_while_its_models_quota_is_spent(rig):
    rig.open('gemini')
    conn, file, status, _ = rig.connect('/turn', {'agentId': 'a', 'text': 'WAIT1'})
    file.close(); conn.close()
    deadline = time.monotonic() + 10
    while rig.request('/agents')[0]['state'] != 'idle' and time.monotonic() < deadline: time.sleep(.1)
    rig.open('gemini', agent='b')
    rig.turn('QUOTA', agent='b')
    assert rig.open('gemini')['vendorSession']
    events = rig.turn('PING')
    # The held answer arrives as an answer, whole, with the refused message noted after it.
    assert events[-1]['type'] == 'done' and events[-1]['text'].startswith('Result of the earlier task') and 'The new message failed: gemini quota reached' in events[-1]['text']


def test_a_held_answer_survives_a_closed_vendor_and_a_spent_quota(rig):
    rig.open('gemini')
    conn, file, status, _ = rig.connect('/turn', {'agentId': 'a', 'text': 'WAIT1'})
    file.close(); conn.close()
    deadline = time.monotonic() + 10
    while rig.request('/agents')[0]['state'] != 'idle' and time.monotonic() < deadline: time.sleep(.1)
    rig.open('gemini', agent='b')
    rig.turn('QUOTA', agent='b')
    # The idle vendor goes away, and any agy started now would fail on the quota.
    os.kill(next(e['pid'] for e in rig.audit() if e.get('launch')), signal.SIGKILL)
    (rig.data / 'start-quota').touch()
    time.sleep(.5)
    launches = len([e for e in rig.audit() if e.get('launch')])
    assert rig.open('gemini')['vendorSession']
    assert len([e for e in rig.audit() if e.get('launch')]) == launches
    assert rig.turn('PING')[-1]['text'].startswith('Result of the earlier task')


def test_a_malformed_quota_file_does_not_stop_the_daemon(rig):
    rig.stop()
    later = time.time() + 3600
    (rig.home / 'quota.json').write_text('{"gemini gemini-3.8-flash": ["soon", "reason"], "nospace": [1, "x"], "gemini x": "text", "gemini y": [1e309, "x"], "gemini z": [' + '9' * 401 + ', "x"]'
                                        f', "gemini gemini-4-argon": [{later}, 5], "grok grok-4.7": [true, "x"], "gpt gpt-6.1-sol": [NaN, "x"]}}')
    rig.start()
    rig.open('gemini')
    assert rig.turn('PING')[-1]['text'] == 'PONG'
    # An entry whose reason is not text is ignored too.
    assert rig.open('gemini', agent='b', model='gemini-4-argon')['vendorSession']


def test_a_quota_named_after_agy_closes_its_output_at_start_up_is_still_named(rig):
    rig.data.mkdir(parents=True, exist_ok=True)
    (rig.data / 'start-quota-late').touch()
    conn, file, status, _ = rig.connect('/open', {'agentId': 'a', 'family': 'gemini', 'cwd': str(rig.home)})
    try:
        body = json.loads(file.read())
    finally:
        file.close(); conn.close()
    assert status != 200 and 'RESOURCE_EXHAUSTED' in body['error']


def test_a_full_log_moves_aside_and_the_newest_records_are_still_written(rig):
    rig.open('gpt')
    log = rig.home / 'logs' / 'a.log'
    log.parent.mkdir(exist_ok=True)
    log.write_text('x' * (4 * 1024 * 1024 + 10) + '\n')
    rig.turn('FAIL')
    assert (rig.home / 'logs' / 'a.log.1').stat().st_size > 4 * 1024 * 1024
    text = log.read_text()
    assert 'turn: gpt' in text and 'error: ' in text


def test_a_recorded_quota_is_merged_with_one_another_daemon_wrote_after_this_one_started(rig):
    rig.open('gemini')
    (rig.home / 'quota.json').write_text(json.dumps({'grok grok-4.7': [time.time() + 3600, 'grok spent']}))
    rig.turn('QUOTA')
    assert set(json.loads((rig.home / 'quota.json').read_text())) == {'grok grok-4.7', 'gemini gemini-3.8-flash'}


def test_a_held_answer_is_delivered_even_when_its_vendor_will_not_start(tmp_path):
    m = load('lordd_held')
    async def main():
        d = object.__new__(m.Daemon)
        a = m.Agent({'agentId': 'a', 'family': 'gemini', 'cwd': str(tmp_path.resolve()), 'model': 'gemini-3.8-flash', 'effort': 'high'})
        a.events.put_nowait({'type': 'done', 'text': 'HELD'})
        d.agents, d.open_locks, d.retire, d.quota = {'a': a}, {}, False, {}
        d.config = {'gemini': {'model': 'gemini-3.8-flash', 'effort': 'high'}}
        d.refresh = lambda aid: None
        d.save = lambda a: None
        d.known_models = lambda a: []
        async def attach(agent): raise RuntimeError('vendor will not start')
        d.attach = attach
        await d.open({'agentId': 'a', 'family': 'gemini', 'cwd': str(tmp_path.resolve())})
        return a.undelivered()
    assert asyncio.run(main()) is True


def test_a_held_answer_is_delivered_when_the_follow_up_names_an_unknown_model(rig):
    rig.open('gemini')
    conn, file, status, _ = rig.connect('/turn', {'agentId': 'a', 'text': 'WAIT1'})
    file.close(); conn.close()
    deadline = time.monotonic() + 10
    while rig.request('/agents')[0]['state'] != 'idle' and time.monotonic() < deadline: time.sleep(.1)
    assert rig.open('gemini', model='no-such-model')['vendorSession']
    last = rig.turn('PING', model='no-such-model')[-1]
    assert last['type'] == 'done' and last['text'].startswith('Result of the earlier task') and 'no-such-model' in last['text']


def test_save_quota_keeps_a_reset_another_daemon_wrote_after_this_one_last_read(tmp_path, monkeypatch):
    monkeypatch.setenv('LORD_HOME', str(tmp_path))
    m = load('lordd_quota_merge')
    d = object.__new__(m.Daemon); d.log = lambda text: None
    d.quota = {}
    later = time.time() + 3600
    (tmp_path / 'quota.json').write_text(json.dumps({'grok grok-4.7': [later, 'written by another daemon']}))
    d.save_quota(('gemini', 'gemini-3.8-flash'), (later, 'mine'))
    assert set(json.loads((tmp_path / 'quota.json').read_text())) == {'grok grok-4.7', 'gemini gemini-3.8-flash'}


def test_prune_removes_the_earlier_log_generation_too(rig):
    rig.open('gpt')
    rig.turn('PING')
    (rig.home / 'logs' / 'a.log.1').write_text('older\n')
    assert rig.request('/prune', {'days': 0})['pruned'] == ['a']
    assert not (rig.home / 'logs' / 'a.log.1').exists()


def test_codex_without_tools_lists_no_skills_and_a_read_only_agent_starts_no_helpers(rig):
    rig.open('gpt', readonly=True)
    config = [e['params'] for e in rig.audit() if e.get('method') == 'thread/start'][-1]['config']
    assert config['skills'] == {'include_instructions': False, 'bundled': {'enabled': False}}
    # codex picks helpers by agents.enabled, then multi_agent_v2, then the model catalog, then multi_agent.
    assert config['features'] == {'multi_agent': False, 'multi_agent_v2': False} and config['agents'] == {'enabled': False}
    rig.open('gpt', agent='b', tools=True)
    config = [e['params'] for e in rig.audit() if e.get('method') == 'thread/start'][-1]['config']
    assert 'skills' not in config and 'features' not in config and 'agents' not in config


def test_helper_agent_steps_reach_the_parents_log_and_record_but_never_its_rows(rig):
    rig.open('gpt')
    # As the plugin asks: rows, each ending the stream until the client continues.
    events = rig.turn('HELPER', rows=True)
    while events[-1]['type'] == 'row': events += list(rig.stream('/continue', {'agentId': 'a', 'rows': True}))
    assert events[-1]['type'] == 'done'
    # A helper's steps are not rows or activity: nothing of them enters the Claude-side transcript.
    assert not any('notes.md' in str(e.get('title', '')) + str(e.get('text', '')) for e in events[:-1])
    log = (rig.home / 'logs/a.log').read_text()
    assert '[/root/check] $ /bin/zsh -lc "cat notes.md"' in log and '[/root/check] edit /w/out.txt' in log
    assert 'helper started /root/check gpt-6-luna low' in log
    reads = (rig.home / 'logs/a.reads.txt').read_text().splitlines()
    assert reads == ['read /w/notes.md (/root/check)', 'edit /w/out.txt (/root/check)']
    record = json.loads((rig.home / 'logs/a.json').read_text())
    assert record['helpers'][0]['path'] == '/root/check' and record['helpers'][0]['model'] == 'gpt-6-luna' and record['helpers'][0]['commands'] == 1
    assert record['turns'][-1]['helpers'] == [record['helpers'][0]['thread']]
    assert events[-1]['record']['helpers'] == ['gpt-6-luna']


def test_a_search_line_names_what_was_searched(rig):
    rig.open('gpt')
    events = rig.turn('SEARCH')
    assert [e['text'] for e in events if e['type'] == 'activity' and e['text'].startswith('search')] == ['search lord claude']
    assert 'search lord claude' in (rig.home / 'logs/a.log').read_text()


def test_a_vendor_refusal_is_reported_as_refused_and_resumable(rig):
    rig.open('gpt')
    failed = rig.turn('CYBER')[-1]
    assert failed['type'] == 'error' and failed['message'].startswith('refused by vendor (cyberPolicy): This content was flagged')
    assert 'send a reworded message to resume it' in failed['message'] and 'cat probe.py' in failed['message']
    assert failed['record']['ended_how'] == 'error (cyberPolicy)'
    # The session is intact: the next turn runs in the same codex process.
    assert rig.turn('PING')[-1]['text'] == 'PONG'
    assert len([e for e in rig.audit() if e.get('launch')]) == 1
    assert (rig.home / 'logs/a.reads.txt').read_text() == f'read {rig.home}/probe.py\n'


def test_at_capacity_before_any_edit_retries_the_same_session(rig):
    config = json.loads(rig.config.read_text())
    rig.stop(); rig.config.write_text(json.dumps({**config, 'capacity_retry_s': .1})); rig.start()
    rig.open('gpt')
    done = rig.turn('BUSY')[-1]
    assert done['type'] == 'done' and done['text'] == 'RECOVERED' and done['record']['retries'] == 1
    assert 'vendor at capacity (Selected model is at capacity); retry 1' in (rig.home / 'logs/a.log').read_text()
    starts = [e['params'] for e in rig.audit() if e.get('method') == 'turn/start']
    assert len({s['threadId'] for s in starts}) == 1 and starts[-1]['input'][0]['text'].startswith('Continue with the task above')
    # After an edit the turn is not repeated.
    failed = rig.turn('BUSY_EDIT')[-1]
    assert failed['type'] == 'error' and failed['record']['ended_how'] == 'error (serverOverloaded)' and failed['record']['retries'] == 0


def test_an_error_sent_before_the_turns_completion_keeps_its_kind_and_restarts_codex(rig):
    rig.open('gpt')
    failed = rig.turn('ERRFIRST')[-1]
    assert failed['type'] == 'error' and failed['message'] == 'stream ended' and failed['record']['ended_how'] == 'error (responseStreamDisconnected)'
    rig.turn('PING')
    assert len([e for e in rig.audit() if e.get('launch')]) == 2


def test_a_spent_codex_allowance_is_shown_and_refused_until_its_reset(rig):
    rig.open('gpt')
    failed = rig.turn('SPENT')[-1]
    assert failed['type'] == 'error' and failed['record']['ended_how'] == 'error (usageLimitExceeded)'
    allowance = rig.request('/allowance')
    reset = allowance['gpt']['codex']['primary']['resetsAt']
    assert allowance['gpt']['codex']['primary'] == {'usedPercent': 100, 'windowDurationMins': 10080, 'resetsAt': reset} and allowance['gpt']['codex']['seen']
    # Another quota bucket's report is kept apart.
    assert allowance['gpt']['reserve']['primary']['usedPercent'] == 50
    assert allowance['refused'][0]['family'] == 'gpt' and allowance['refused'][0]['until'] == time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(reset))
    conn, file, status, _ = rig.connect('/open', {'agentId': 'b', 'family': 'gpt', 'cwd': str(rig.home)})
    try: assert status == 400 and 'gpt quota reached' in json.loads(file.read())['error']
    finally: file.close(); conn.close()


def test_each_turn_has_a_record_a_kept_answer_and_a_footer_and_prune_removes_them(rig):
    opened = rig.open('gpt', session='caller-1')
    first = rig.turn('PING')[-1]
    record = first['record']
    assert record['ended_how'] == 'done' and record['model'] == opened['model'] and record['session'] == opened['vendorSession']
    assert record['started'] <= record['ended'] and record['commands'] == 0
    rig.turn('second answer')
    document = json.loads((rig.home / 'logs/a.json').read_text())
    assert document['lordVersion'] and document['session'] == 'caller-1' and document['sandbox'] == 'workspace-write'
    assert [t['turn'] for t in document['turns']] == [1, 2] and document['turns'][0]['usage']['input_tokens'] == 10
    assert (rig.home / 'logs/a.answers/1.md').read_text() == 'PONG' and (rig.home / 'logs/a.answers/2.md').read_text() == 'second answer'
    assert {p.name for p in (rig.home / 'logs').iterdir()} == {'a.log', 'a.answer.md', 'a.json', 'a.answers'}
    # A record replacement cut short leaves its scratch file, which pruning removes with the rest.
    (rig.home / 'logs/a.json.tmp').write_text('{}')
    rig.stop()
    rows = [{**json.loads(line), 'used': '2020-01-01T00:00:00+00:00'} for line in (rig.home / 'journal.jsonl').read_text().splitlines()]
    (rig.home / 'journal.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
    rig.start()
    # The daemon prunes as it starts; this prune finds nothing left.
    rig.request('/prune', {'days': 14})
    assert rig.request('/agents') == [] and list((rig.home / 'logs').iterdir()) == []


def test_open_and_every_turn_state_the_effective_settings_with_their_defaults(rig):
    opened = rig.open('gpt', dirs=[str(rig.home)], wall_s=900)
    # The niceness reported is the one the vendor runs at: the daemon's own, here the test's.
    assert opened['memory_gb'] == 12.0 and opened['wall_s'] == 900 and opened['hang_s'] is None and opened['nice'] == os.getpriority(os.PRIO_PROCESS, 0)
    assert opened['dirs'] == [str(rig.home.resolve())] and opened['sandbox'] == 'workspace-write' and opened['network'] is False
    assert opened['line'].startswith(f"cwd {rig.home.resolve()} · dirs {rig.home.resolve()} · sandbox workspace-write · network off · tools off · nice {opened['nice']} · memory 12.0 GB")
    rig.turn('PING')
    line = next(l for l in (rig.home / 'logs/a.log').read_text().splitlines() if ' turn: ' in l)
    assert line.endswith(f"turn: gpt {opened['model']} effort {opened['effort']} · {opened['line']}")


def test_nice_starts_the_vendor_at_that_niceness_and_a_change_restarts_it(rig):
    # The daemon has the test's niceness (zsh starts background jobs at 5); a level is absolute, never below it.
    base = os.getpriority(os.PRIO_PROCESS, 0)
    rig.open('gpt', nice=15)
    rig.turn('PING')
    launches = [e for e in rig.audit() if e.get('launch')]
    assert launches[-1]['nice'] == max(15, base)
    assert rig.open('gpt', nice=15) and len([e for e in rig.audit() if e.get('launch')]) == 1
    rig.open('gpt', nice=12)
    assert [e for e in rig.audit() if e.get('launch')][-1]['nice'] == max(12, base)
    conn, file, status, _ = rig.connect('/open', {'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home), 'nice': 20})
    try: assert status == 400 and '0 to 19' in json.loads(file.read())['error']
    finally: file.close(); conn.close()


def test_a_message_to_a_running_agent_is_logged_and_one_codex_cannot_take_is_queued_as_the_next_turn(rig):
    rig.open('grok')
    stream = rig.stream('/turn', {'agentId': 'a', 'text': 'SLOW'})
    assert next(stream)['type'] == 'activity'
    assert rig.request('/steer', {'agentId': 'a', 'text': 'too early'})['steered'] is False
    assert rig.request('/queue', {'agentId': 'a', 'text': 'PING'})['queued'] is True
    rig.request('/interrupt', {'agentId': 'a'})
    events = list(stream)
    assert events[-1]['type'] == 'error'
    log = (rig.home / 'logs/a.log').read_text()
    assert 'message queued for the next turn: PING' in log
    rig.open('gpt', agent='g')
    stream = rig.stream('/turn', {'agentId': 'g', 'text': 'SLOW'})
    assert next(stream)['type'] == 'activity'
    assert rig.request('/steer', {'agentId': 'g', 'text': 'steered words'})['steered'] is True
    assert list(stream)[-1]['text'] == 'steered words'
    assert 'message injected into the running turn: steered words' in (rig.home / 'logs/g.log').read_text()


def test_a_queued_message_runs_as_the_next_turn_and_the_answer_carries_both(rig):
    rig.open('grok')
    stream = rig.stream('/turn', {'agentId': 'a', 'text': 'WAIT1'})
    assert next(stream)['type'] == 'activity'
    assert rig.request('/queue', {'agentId': 'a', 'text': 'PING'})['queued'] is True
    done = list(stream)[-1]
    assert done['type'] == 'done' and done['record']['ended_how'] == 'done'
    assert done['text'] == 'Result of the task, before the message sent while it ran:\nfirst\n\nReply to the new message:\nPONG'
    assert [e['params']['prompt'][0]['text'] for e in rig.audit() if e.get('method') == 'session/prompt'] == ['WAIT1', 'PING']
    assert rig.request('/queue', {'agentId': 'a', 'text': 'late'})['queued'] is False


def test_a_refused_open_names_the_folder_and_is_logged(rig):
    conn, file, status, _ = rig.connect('/open', {'agentId': 'x', 'family': 'gpt', 'cwd': str(rig.home), 'dirs': [str(rig.home), '/no/such/dir']})
    try: assert status == 400 and json.loads(file.read())['error'] == '@dir /no/such/dir is not an existing directory'
    finally: file.close(); conn.close()
    assert 'x: open refused: @dir /no/such/dir is not an existing directory' in (rig.home / 'lordd.log').read_text()


def test_the_daemon_and_its_vendors_helpers_run_from_lords_own_folder(rig):
    out = subprocess.run(['/usr/sbin/lsof', '-a', '-p', str(rig.proc.pid), '-d', 'cwd', '-Fn'], capture_output=True, text=True).stdout
    assert f'n{rig.home.resolve()}' in out.splitlines() or f'n{rig.home}' in out.splitlines()



def test_a_helpers_edit_counts_as_the_turns_edit_so_at_capacity_is_not_retried(rig):
    rig.open('gpt')
    failed = rig.turn('HELPER_BUSY')[-1]
    assert failed['type'] == 'error' and failed['record']['retries'] == 0 and failed['record']['ended_how'] == 'error (serverOverloaded)'


def test_messages_queued_one_after_another_keep_every_result(rig):
    rig.open('grok')
    stream = rig.stream('/turn', {'agentId': 'a', 'text': 'WAIT1 alpha'})
    assert next(stream)['type'] == 'activity'
    assert rig.request('/queue', {'agentId': 'a', 'text': 'WAIT1 beta'})['queued'] is True
    # PING is queued once beta's turn has started, however loaded the machine is.
    deadline = time.monotonic() + 10
    while (rig.home / 'logs/a.log').read_text().count(' turn: grok ') < 2 and time.monotonic() < deadline: time.sleep(.05)
    assert rig.request('/queue', {'agentId': 'a', 'text': 'PING'})['queued'] is True
    done = list(stream)[-1]
    assert done['type'] == 'done' and all(word in done['text'] for word in ('alpha', 'beta', 'PONG'))
    assert done['text'].index('alpha') < done['text'].index('beta') < done['text'].index('PONG')


def test_answer_files_stay_within_the_last_100_turns_whatever_turns_are_empty(tmp_path):
    m = load('lordd_answers')
    a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '.', 'model': 'gpt-6.1-sol'})
    a.state = 'running'
    for n in range(300):
        a.turn_record('done', {'text': '' if 100 <= n < 200 else f'answer {n}'})
    kept = sorted(int(p.stem) for p in a.answers_path.iterdir())
    assert len(json.loads(a.record_path.read_text())['turns']) == 100 and kept == list(range(201, 301))


def test_an_interrupt_while_a_retry_is_being_sent_still_stops_codexs_turn():
    m = load('lordd_retry_interrupt')
    from unittest.mock import AsyncMock
    async def main():
        d = object.__new__(m.Daemon); d.redact = str; d.config = {'gpt': {}}
        a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '/w', 'model': 'gpt-6.1-sol', 'vendorSession': 's'})
        a.state, a.turn_body, a.note, a.turn_record = 'running', {'text': 'work'}, lambda text: None, lambda kind, value: {}
        requests, sent = [], asyncio.Event()
        async def rpc(method, params):
            requests.append(method)
            if method == 'turn/start':
                sent.set()
                await asyncio.sleep(.2)
                return {'turn': {'id': 't2'}}
            if method == 'turn/interrupt': a.emit('error', message='interrupted', retryable=False)
            return {}
        v = types.SimpleNamespace(last=0, exec_process=None, rpc=rpc, close=AsyncMock(), process=None)
        d.backend = lambda agent: v
        a.retry_task = asyncio.create_task(d.retry(a, 0))
        await sent.wait()
        await d.interrupt(a)
        await asyncio.gather(a.retry_task, return_exceptions=True)
        return requests, a.state
    assert asyncio.run(main()) == (['turn/start', 'turn/interrupt'], 'idle')



def test_helpers_of_codexs_older_helper_mode_are_logged_too(rig):
    rig.open('gpt')
    assert rig.turn('HELPER1')[-1]['type'] == 'done'
    log = (rig.home / 'logs/a.log').read_text()
    record = json.loads((rig.home / 'logs/a.json').read_text())
    thread = record['helpers'][0]['thread']
    assert f'[{thread}] $ /bin/zsh -lc "cat notes.md"' in log and record['helpers'][0]['model'] == 'gpt-6-luna'


def test_an_interrupted_grok_turn_is_recorded_as_interrupted(rig):
    rig.open('grok')
    stream = rig.stream('/turn', {'agentId': 'a', 'text': 'SLOW'})
    assert next(stream)['type'] == 'activity'
    rig.request('/interrupt', {'agentId': 'a'})
    list(stream)
    assert json.loads((rig.home / 'logs/a.json').read_text())['turns'][-1]['ended_how'] == 'interrupted'


def test_a_malformed_record_or_allowance_file_never_stops_a_turn(tmp_path):
    m = load('lordd_malformed')
    a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '.', 'model': 'gpt-6.1-sol'})
    a.state = 'running'
    a.record_path.parent.mkdir(parents=True, exist_ok=True)
    for text in ('{"turns": [1]}', '{"turns": 3}', '[]', '{"turns": [{"turn": "x"}]}'):
        a.record_path.write_text(text)
        assert a.turn_record('done', {'text': 'ok'})['ended_how'] == 'done'
    (m.HOME / 'allowance.json').write_text('[]')
    d = object.__new__(m.Daemon); d.log = lambda text: None
    d.allowance({'limitId': 'codex', 'primary': {'usedPercent': 1}})
    assert m.allowance_buckets()['codex']['primary'] == {'usedPercent': 1}


def test_the_read_list_stops_growing_at_its_limit(tmp_path):
    m = load('lordd_reads_limit')
    m.READS_LIMIT = 60
    a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '.', 'model': 'gpt-6.1-sol'})
    for n in range(10): a.read_paths([('read', f'/w/file{n}.py')])
    lines = a.reads_path.read_text().splitlines()
    assert lines[-1] == m.READS_FULL and len(lines) < 10
    a.read_paths([('read', '/w/later.py')])
    assert a.reads_path.read_text().splitlines() == lines


def test_the_mcp_helper_runs_from_lords_folder(tmp_path):
    home = tmp_path / 'lord-home'
    proc = subprocess.Popen([PYTHON, str(DAEMON), '--mcp', 'a'], cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env={**os.environ, 'LORD_HOME': str(home), 'LORD_DAEMON_SOCKET': str(home / 'x.sock')})
    try:
        proc.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'); proc.stdin.flush()
        assert json.loads(proc.stdout.readline())['id'] == 1
        out = subprocess.run(['/usr/sbin/lsof', '-a', '-p', str(proc.pid), '-d', 'cwd', '-Fn'], capture_output=True, text=True).stdout
        assert f'n{home.resolve()}' in out.splitlines()
    finally:
        proc.stdin.close(); proc.wait(timeout=10)


def test_the_niceness_reported_is_the_one_the_vendor_runs_at(monkeypatch):
    m = load('lordd_nice_reported')
    d = object.__new__(m.Daemon); d.config = {'gpt': {}}
    a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '/w', 'model': 'gpt-6.1-sol', 'nice': 3})
    monkeypatch.setattr(m.os, 'getpriority', lambda *args: 8)
    assert d.settings(a)['nice'] == 8
    a.record['nice'] = 12
    assert d.settings(a)['nice'] == 12



def test_a_message_asking_for_read_only_is_not_steered_into_a_turn_that_can_write(rig):
    rig.open('gpt')
    stream = rig.stream('/turn', {'agentId': 'a', 'text': 'SLOW'})
    assert next(stream)['type'] == 'activity'
    # The next turn is to be read-only, but this one still runs writable.
    rig.open('gpt', readonly=True)
    assert rig.request('/steer', {'agentId': 'a', 'text': 'review only', 'readonly': True})['steered'] is False
    assert rig.request('/steer', {'agentId': 'a', 'text': 'finish'})['steered'] is True
    assert list(stream)[-1]['text'] == 'finish'


@pytest.mark.parametrize('family', ['grok', 'gemini'])
def test_grok_and_gemini_turns_count_their_commands(rig, family):
    rig.open(family)
    # The fake agy also runs its opening `fake work` command.
    assert rig.turn('STEPS')[-1]['record']['commands'] == {'grok': 1, 'gemini': 2}[family]


def test_allowance_writes_take_the_journal_lock_and_leave_one_scratch_file_at_most(monkeypatch):
    m = load('lordd_allowance_lock')
    d = object.__new__(m.Daemon); d.log = lambda text: None
    m.HOME.mkdir(parents=True, exist_ok=True)
    held = []
    real = m.Daemon.journal_lock
    def lock(self):
        held.append(True)
        return real(self)
    monkeypatch.setattr(m.Daemon, 'journal_lock', lock)
    d.allowance({'limitId': 'codex', 'primary': {'usedPercent': 1}})
    assert held
    original = m.Path.replace
    monkeypatch.setattr(m.Path, 'replace', lambda self, target: (_ for _ in ()).throw(OSError('disk full')))
    d.allowance({'limitId': 'codex', 'primary': {'usedPercent': 2}})
    d.allowance({'limitId': 'codex', 'primary': {'usedPercent': 3}})
    monkeypatch.setattr(m.Path, 'replace', original)
    assert [p.name for p in m.HOME.iterdir() if p.name.startswith('allowance.json.')] in ([], ['allowance.json.tmp'])
    d.allowance({'limitId': 'codex', 'primary': {'usedPercent': 4}})
    assert [p.name for p in m.HOME.iterdir() if p.name.startswith('allowance.json')] == ['allowance.json']


def test_a_usage_limit_is_refused_until_the_reset_of_the_models_own_bucket():
    m = load('lordd_quota_bucket')
    now = time.time()
    m.HOME.mkdir(parents=True, exist_ok=True)
    (m.HOME / 'allowance.json').write_text(json.dumps({'gpt': {
        'codex': {'primary': {'usedPercent': 100, 'resetsAt': now + 3600}},
        # codex names a bucket in lower case with '_' for '-', and its rate-limit events carry no limitName.
        'gpt_reserve': {'limitId': 'gpt_reserve', 'normalModelSlug': 'gpt-6-luna', 'primary': {'usedPercent': 100, 'resetsAt': now + 604800}},
        # A bucket whose id is not the model's: only its limitName (from codex's response headers) names the model.
        'codex_bengalfox': {'limitId': 'codex_bengalfox', 'limitName': 'gpt-6-sol', 'primary': {'usedPercent': 100, 'resetsAt': now + 7200}}}}))
    d = object.__new__(m.Daemon); d.quota = {}; d.log = lambda text: None
    s = m.Agent({'agentId': 's', 'family': 'gpt', 'cwd': '.', 'model': 'gpt-6-sol'})
    d.gpt_quota(s, 'limit')
    assert abs(d.quota[('gpt', 'gpt-6-sol')][0] - (now + 7200)) < 5
    a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '.', 'model': 'gpt-6.1-sol'})
    d.gpt_quota(a, 'limit')
    assert abs(d.quota[('gpt', 'gpt-6.1-sol')][0] - (now + 3600)) < 5
    b = m.Agent({'agentId': 'b', 'family': 'gpt', 'cwd': '.', 'model': 'gpt-reserve'})
    d.gpt_quota(b, 'limit')
    assert abs(d.quota[('gpt', 'gpt-reserve')][0] - (now + 604800)) < 5
    # normalModelSlug only lends a display name to an alias quota; gpt-6-luna is metered by codex's own bucket.
    c = m.Agent({'agentId': 'c', 'family': 'gpt', 'cwd': '.', 'model': 'gpt-6-luna'})
    d.gpt_quota(c, 'limit')
    assert abs(d.quota[('gpt', 'gpt-6-luna')][0] - (now + 3600)) < 5


def test_one_large_batch_of_reads_stops_at_the_limit():
    m = load('lordd_reads_batch')
    m.READS_LIMIT = 100
    a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '.', 'model': 'gpt-6.1-sol'})
    a.read_paths([('read', f'/w/{"x" * 20}{n}.py') for n in range(10)])
    lines = a.reads_path.read_text().splitlines()
    assert lines[-1] == m.READS_FULL and a.reads_path.stat().st_size <= 100 + len(m.READS_FULL) + 1



def test_an_interrupt_right_after_at_capacity_cancels_the_retry_at_once(monkeypatch):
    m = load('lordd_retry_window')
    async def main():
        d = object.__new__(m.Daemon); d.redact = str; d.config = {'gpt': {}}; d.tasks = set(); d.log = lambda text: None
        a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '/w', 'model': 'gpt-6.1-sol', 'vendorSession': 's'})
        a.state, a.turn_body, a.note, a.turn_record = 'running', {'text': 'work'}, lambda text: None, lambda kind, value: {}
        a.finished.clear()
        killed = []
        async def kill_tree(process, known=()): killed.append(process)
        monkeypatch.setattr(m, 'kill_tree', kill_tree)
        async def rpc(method, params): return {}
        v = types.SimpleNamespace(last=0, exec_process=None, rpc=rpc, process=None, close=None)
        d.backend = lambda agent: v
        # codex's "at capacity" schedules the retry; the interrupt is handled before the retry task first runs.
        m.Vendor(d, 'gpt', a).failed(a, {'message': 'Selected model is at capacity', 'codexErrorInfo': 'serverOverloaded'}, 'failed', 't1')
        started = time.monotonic()
        await d.interrupt(a)
        took = time.monotonic() - started
        await asyncio.gather(a.retry_task, return_exceptions=True)
        return took < 1, a.retry_task.cancelled(), killed, a.state, a.retry_waiting
    assert asyncio.run(main()) == (True, True, [], 'idle', False)


def test_a_retry_keeps_the_turns_wall_clock_and_its_wait_is_not_silence(monkeypatch):
    m = load('lordd_retry_clock')
    async def main():
        d = object.__new__(m.Daemon); d.redact = str; d.config = {'gpt': {}, 'process_idle_s': 60, 'idle_exit_s': 600}
        a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '/w', 'model': 'gpt-6.1-sol', 'vendorSession': 's', 'hang_s': 5})
        a.state, a.turn_body, a.started = 'running', {'text': 'work'}, 100.0
        async def rpc(method, params): return {'turn': {'id': 't2'}}
        v = types.SimpleNamespace(last=0, exec_process=None, rpc=rpc, agent=a, process=types.SimpleNamespace(pid=999999, returncode=None), reason=None, descendants=set())
        d.backend, d.vendors = (lambda agent: v), {'a': v}
        await d.retry(a, 0)
        kept = a.started
        # While Lord waits to retry, codex is idle by Lord's choice: the silence guard does not count it.
        a.retry_waiting, v.last = True, time.monotonic() - 60
        async def table(): return {}, {}
        monkeypatch.setattr(m, 'process_table', table)
        await d.guard(time.monotonic())
        return kept, v.reason
    assert asyncio.run(main()) == (100.0, None)


def test_a_logged_message_is_redacted_before_it_is_cut(rig):
    rig.open('grok')
    stream = rig.stream('/turn', {'agentId': 'a', 'text': 'SLOW'})
    assert next(stream)['type'] == 'activity'
    rig.request('/queue', {'agentId': 'a', 'text': 'x' * 290 + ' sk-' + 'q' * 30})
    rig.request('/interrupt', {'agentId': 'a'})
    list(stream)
    assert 'sk-qqq' not in (rig.home / 'logs/a.log').read_text()


@pytest.mark.parametrize('key', ['capacity_retry_s', 'capacity_retries'])
def test_capacity_retry_settings_must_be_numbers(tmp_path, monkeypatch, key):
    m = load('lordd_capacity_config')
    bundled = tmp_path / 'bundled.json'
    bundled.write_text(json.dumps({'gpt': {'model': 'gpt-6.1-sol'}}))
    (tmp_path / 'config.json').write_text(json.dumps({key: '30'}))
    monkeypatch.setenv('LORD_CONFIG', str(bundled))
    monkeypatch.setattr(m, 'HOME', tmp_path)
    with pytest.raises(ValueError, match=key): m.load_config()


def test_a_turn_id_codex_already_ended_is_never_taken_back():
    m = load('lordd_ended_turn_id')
    async def main():
        d = object.__new__(m.Daemon); d.redact = str; d.config = {'gpt': {}}
        a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '/w', 'model': 'gpt-6.1-sol', 'vendorSession': 's'})
        a.state = 'running'
        # codex's start response arrives after the same turn already failed "at capacity" (failed() ended it).
        a.ended_turns.add('t1')
        async def rpc(method, params): return {'turn': {'id': 't1'}}
        d.backend = lambda agent: types.SimpleNamespace(last=0, exec_process=None, rpc=rpc)
        await d.run_turn(a, {'text': 'work'})
        return a.turn_id
    assert asyncio.run(main()) is None


def test_a_pending_retry_ends_with_its_turn_and_never_reaches_a_later_one(monkeypatch):
    m = load('lordd_retry_owner')
    async def main():
        d = object.__new__(m.Daemon); d.redact = str; d.config = {'gpt': {}, 'capacity_retry_s': .1}; d.tasks = set(); d.log = lambda text: None
        a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '/w', 'model': 'gpt-6.1-sol', 'vendorSession': 's'})
        a.state, a.turn_body, a.note, a.turn_record = 'running', {'text': 'work'}, lambda text: None, lambda kind, value: {}
        starts = []
        async def rpc(method, params):
            starts.append(params['input'][0]['text'])
            return {'turn': {'id': 'n'}}
        d.backend = lambda agent: types.SimpleNamespace(last=0, exec_process=None, rpc=rpc)
        m.Vendor(d, 'gpt', a).failed(a, {'message': 'at capacity', 'codexErrorInfo': 'serverOverloaded'}, 'failed', 't1')
        # The turn ends another way (the wall-clock guard) while the retry waits; a new turn then runs.
        a.emit('error', message='stopped: the turn ran past its 60 s wall-clock limit', retryable=True)
        waiting = a.retry_waiting
        a.state, a.turn_body = 'running', {'text': 'new task'}
        await asyncio.sleep(.3)
        return waiting, starts
    assert asyncio.run(main()) == (False, [])


def test_a_restarted_daemon_keeps_the_helpers_its_record_names(tmp_path):
    m = load('lordd_helpers_kept')
    a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '.', 'model': 'gpt-6.1-sol'})
    a.state, a.turn_started = 'running', '2026-01-01T00:00:00+00:00'
    m.helper_seen(a, {'type': 'subAgentActivity', 'agentThreadId': 'child', 'agentPath': '/root/x', 'model': 'gpt-6-luna'})
    a.turn_record('done', {'text': 'one'})
    b = m.Agent(dict(a.record))
    b.state = 'running'
    b.turn_record('done', {'text': 'two'})
    document = json.loads(b.record_path.read_text())
    assert [h['thread'] for h in document['helpers']] == ['child'] and document['turns'][0]['helpers'] == ['child']


def test_the_record_keeps_only_the_helpers_its_kept_turns_name():
    m = load('lordd_helpers_bounded')
    for n in range(102):
        a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '.', 'model': 'gpt-6.1-sol'})
        started = time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(1767225600 + 60 * n))
        a.state, a.turn_started = 'running', started
        m.helper_seen(a, {'type': 'subAgentActivity', 'agentThreadId': f'child-{n}', 'agentPath': f'/root/{n}', 'model': 'gpt-6-luna'})
        a.helpers[f'child-{n}']['started'] = started
        a.turn_record('done', {'text': 'ok'})
    document = json.loads(a.record_path.read_text())
    named = [t for turn in document['turns'] for t in turn['helpers']]
    assert len(document['turns']) == 100 and [h['thread'] for h in document['helpers']] == named == [f'child-{n}' for n in range(2, 102)]


def test_a_hand_edited_helper_in_the_record_never_breaks_a_later_step():
    m = load('lordd_helper_edited')
    a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '.', 'model': 'gpt-6.1-sol'})
    a.record_path.parent.mkdir(parents=True, exist_ok=True)
    a.record_path.write_text(json.dumps({'helpers': [{'thread': 'child', 'started': '2026-01-01T00:00:00+00:00', 'commands': 'many'}], 'turns': []}))
    a.state, a.turn_started = 'running', '2026-01-02T00:00:00+00:00'
    a.turn_record('done', {'text': 'one'})
    m.helper_seen(a, {'type': 'subAgentActivity', 'agentThreadId': 'child', 'agentPath': '/root/x', 'model': 'gpt-6-luna'})
    v = object.__new__(m.Vendor)
    v.helper_event(a, a.helpers['child'], 'item/completed', {'item': {'type': 'commandExecution', 'command': 'ls', 'commandActions': []}})
    assert a.helpers['child']['model'] == 'gpt-6-luna' and a.helpers['child']['commands'] == 1



def test_a_retry_whose_turn_was_replaced_while_it_waited_sends_nothing():
    m = load('lordd_retry_identity')
    async def main():
        d = object.__new__(m.Daemon); d.redact = str; d.config = {'gpt': {}}
        a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '/w', 'model': 'gpt-6.1-sol', 'vendorSession': 's'})
        a.state, a.turn_body = 'running', {'text': 'old task'}
        starts = []
        async def rpc(method, params): starts.append(params['input'][0]['text']); return {'turn': {'id': 'n'}}
        d.backend = lambda agent: types.SimpleNamespace(last=0, exec_process=None, rpc=rpc)
        waiting = asyncio.create_task(d.retry(a, .1))
        await asyncio.sleep(.02)
        a.turn_body = {'text': 'new task'}
        await waiting
        return starts
    assert asyncio.run(main()) == []


def test_a_quota_write_that_fails_leaves_one_fixed_scratch_name_at_most(monkeypatch):
    m = load('lordd_quota_scratch')
    m.HOME.mkdir(parents=True, exist_ok=True)
    d = object.__new__(m.Daemon); d.quota = {}; d.log = lambda text: None
    monkeypatch.setattr(m.Path, 'replace', lambda self, target: (_ for _ in ()).throw(OSError('disk full')))
    d.save_quota(('gpt', 'x'), (time.time() + 60, 'spent'))
    assert [p.name for p in m.HOME.iterdir() if p.name.startswith('quota.json')] in ([], ['quota.json.tmp'])


def test_prune_removes_files_of_agents_an_older_daemon_pruned(rig):
    logs = rig.home / 'logs'
    logs.mkdir(exist_ok=True)
    old = time.time() - 20 * 86400
    for name in ('gone.json', 'gone.reads.txt', 'gone.answers/1.md', 'fresh.json'):
        (logs / name).parent.mkdir(exist_ok=True)
        (logs / name).write_text('x')
    for path in (logs / 'gone.json', logs / 'gone.reads.txt', logs / 'gone.answers/1.md', logs / 'gone.answers'): os.utime(path, (old, old))
    rig.open('gpt', agent='kept'); rig.turn('PING', agent='kept')
    for path in logs.glob('kept*'): os.utime(path, (old, old))
    rig.request('/prune', {'days': 14})
    # No record and older than the prune period: removed. A young file, or one whose agent has a record, stays.
    assert sorted(p.name for p in logs.iterdir()) == sorted(['fresh.json', *[p.name for p in logs.glob('kept*')]]) and (logs / 'kept.log').exists()


def test_the_sweep_names_an_agents_files_exactly_and_works_under_the_journal_lock(monkeypatch):
    m = load('lordd_sweep_owner')
    logs = m.HOME / 'logs'
    logs.mkdir(parents=True)
    old = time.time() - 20 * 86400
    names = ['live.review.json', 'live.review.log', 'live.review.answers', 'live.json', 'gone.json', 'gone.answers', 'notes.txt', 'other.log.1']
    for name in names:
        (logs / name).mkdir() if name.endswith('.answers') else (logs / name).write_text('x')
        os.utime(logs / name, (old, old))
    d = object.__new__(m.Daemon); d.log = lambda text: None
    d.agents = {'live.review': None}
    d.journal, d.legacy_journal = m.HOME / 'journal.jsonl', m.HOME / 'sessions.jsonl'
    # An agent a daemon of another version holds: in the journal only, never in this daemon's memory.
    d.journal.write_text(json.dumps({'agentId': 'other', 'family': 'grok', 'cwd': '/w', 'model': 'x'}) + '\n')
    inside = []
    real = m.Daemon.journal_lock
    @contextlib.contextmanager
    def lock(self):
        with real(self):
            inside.append(True)
            try: yield
            finally: inside.pop()
    monkeypatch.setattr(m.Daemon, 'journal_lock', lock)
    removed = []
    real_unlink, real_rmtree = m.Path.unlink, m.shutil.rmtree
    monkeypatch.setattr(m.Path, 'unlink', lambda self, missing_ok=False: (removed.append((self.name, bool(inside))), real_unlink(self, missing_ok=missing_ok))[1])
    monkeypatch.setattr(m.shutil, 'rmtree', lambda path, **kw: (removed.append((Path(path).name, bool(inside))), real_rmtree(path, **kw))[1])
    d.sweep(14)
    # 'live' is not an agent: its file goes. Only an agent's own file names count, and nothing else in logs/ is touched.
    assert sorted(removed) == [('gone.answers', True), ('gone.json', True), ('live.json', True)]
    assert sorted(p.name for p in logs.iterdir()) == ['live.review.answers', 'live.review.json', 'live.review.log', 'notes.txt', 'other.log.1']


@pytest.fixture
def tests_backend(rig, monkeypatch):
    d = local_daemon(rig, monkeypatch)
    # local_daemon loads a fresh module without registering it in sys.modules.
    m = d.open.__func__.__globals__
    real = asyncio.create_subprocess_exec
    launched = []
    async def start(*argv, **kw):
        launched.append((argv, kw))
        if '/usr/bin/sandbox-exec' in argv:
            at = argv.index('/usr/bin/sandbox-exec')
            i = at + 3
            while argv[i].startswith('-D'): i += 1
            argv = (*argv[:at], *argv[i:])
        return await real(*argv, **kw)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', start)
    return d, m, launched


def test_tests_wiring_settings_environment_and_resume(tests_backend, rig):
    d, m, launched = tests_backend
    async def scenario():
        try:
            opened = await d.open({'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home), 'tests': True, 'nice': 12})
            assert opened['sandbox'] == 'tests' and 'sandbox tests' in opened['line'] and opened['network'] is False
            a = d.agents['a']; v = d.backend(a)
            await d.begin(a, {'text': 'PING'})
            await asyncio.wait_for(a.finished.wait(), 3)
            audit = rig.audit()
            methods = [e.get('method') for e in audit if e['pid'] == v.process.pid and e.get('method')]
            assert methods.index('initialize') < methods.index('environment/add') < methods.index('thread/start')
            env = next(e['params'] for e in audit if e.get('method') == 'environment/add')
            ex = next(e for e in audit if e.get('exec_launch'))
            argv = next(argv for argv, _ in launched if '/usr/bin/sandbox-exec' in argv)
            step = 12 - os.getpriority(os.PRIO_PROCESS, 0)
            assert argv[:4] == ('/usr/bin/nice', '-n', str(step), '/usr/bin/sandbox-exec') if step > 0 else argv[0] == '/usr/bin/sandbox-exec'
            assert ex['cwd'] == str(rig.home.resolve()) and ex['nice'] == max(12, os.getpriority(os.PRIO_PROCESS, 0))
            assert ex['mcp_env'] == {} and ex['env'] == {} and ex['codex_home'] == str(d.journal.parent / 'exec/a')
            assert (Path(ex['codex_home']).stat().st_mode & 0o777) == 0o700
            args = ex['argv']
            assert args[args.index('--listen') + 1] == 'ws://127.0.0.1:0'
            assert args[args.index('--ws-auth') + 1] == 'capability-token'
            assert args[args.index('--ws-token-sha256') + 1] == hashlib.sha256(env['authBearerToken'].encode()).hexdigest()
            assert env['authBearerToken'] not in args
            entries = [{'environmentId': env['environmentId'], 'cwd': a.cwd}]
            thread = next(e['params'] for e in audit if e.get('method') == 'thread/start')
            turn = next(e['params'] for e in audit if e.get('method') == 'turn/start')
            assert thread['environments'] == turn['environments'] == entries
            assert turn['sandboxPolicy'] == {'type': 'externalSandbox', 'networkAccess': 'restricted'}
            assert turn['input'] == [{'type': 'text', 'text': 'PING'}]
            assert 'sandbox tests' in a.log_path.read_text()
            await v.close()
            a.generation = 0
            await d.attach(a)
            resumed = [e['params'] for e in rig.audit() if e.get('method') == 'thread/resume'][-1]
            assert resumed['environments'] == [{'environmentId': v.environment_id, 'cwd': a.cwd}]
            assert len([e for e in rig.audit() if e.get('exec_launch')]) == 2
            assert not list(d.journal.parent.rglob('*.sbpl'))
        finally: await d.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('family,clash', [('grok', None), ('gemini', None), *[('gpt', c) for c in ('readonly', 'nosandbox', 'net')]])
def test_tests_rejects_families_and_clashes_before_start(rig, family, clash):
    body = {'agentId': 'a', 'family': family, 'cwd': str(rig.home), 'tests': True, **({clash: True} if clash else {})}
    conn, file, status, _ = rig.connect('/open', body)
    try:
        error = json.loads(file.read()).get('error', '')
        assert status == 400 and '@tests' in error and ('@' + clash if clash else 'gpt only') in error
    finally: file.close(); conn.close()
    assert not rig.data.exists(), 'a refused option started a vendor'


@pytest.mark.parametrize('clash', ['readonly', 'nosandbox', 'net'])
@pytest.mark.parametrize('first_tests', [False, True])
def test_tests_rejects_inherited_clashes(tests_backend, rig, clash, first_tests):
    d, _, _ = tests_backend
    async def scenario():
        try:
            body = {'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home)}
            await d.open({**body, **({'tests': True} if first_tests else {clash: True})})
            with pytest.raises(ValueError, match='@tests.*@' + clash):
                await d.open({**body, **({clash: True} if first_tests else {'tests': True})})
        finally: await d.close()
    asyncio.run(scenario())


def test_tests_later_enable_is_sticky_and_start_options_restart_both(tests_backend, rig):
    d, _, _ = tests_backend
    async def scenario():
        try:
            body = {'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home)}
            await d.open(body)
            v = d.backend(d.agents['a']); original = v.process
            await d.open({**body, 'tests': True})
            assert original.returncode is not None and v.exec_process is not None
            for change in ({'nice': 15}, {'dirs': [str(rig.data)]}):
                app, ex = v.process, v.exec_process
                await d.open({**body, **change})
                assert app.returncode is not None and ex.returncode is not None
                assert v.process is not app and v.exec_process is not ex
            app, ex = v.process, v.exec_process
            await d.open({**body, 'tests': False})
            assert v.process is app and v.exec_process is ex
            assert d.agents['a'].record['tests'] is True
            await d.open({**body, 'agentId': 'b', 'tests': True})
            assert {e['codex_home'] for e in rig.audit() if e.get('exec_launch')} == {str(d.journal.parent / 'exec' / aid) for aid in ('a', 'b')}
        finally: await d.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('stop', ['close', 'crash', 'memory', 'hang', 'wall', 'interrupt', 'shutdown', 'exec-crash', 'idle'])
def test_tests_exec_server_tree_dies_with_vendor(tests_backend, rig, stop):
    d, m, _ = tests_backend
    async def scenario():
        try:
            await d.open({'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home), 'tests': True})
            a = d.agents['a']; v = d.backend(a)
            app, ex = v.process, v.exec_process
            child = next(e['child'] for e in rig.audit() if e.get('exec_launch'))
            await d.guard(time.monotonic())
            if stop == 'crash': await d.begin(a, {'text': 'CRASH'})
            elif stop == 'close': await d.route('POST', '/close', {'agentId': 'a'})
            elif stop == 'shutdown': await d.close()
            elif stop == 'exec-crash':
                await d.begin(a, {'text': 'SLOW'})
                while not a.turn_id: await asyncio.sleep(.01)
                v.reason = 'stopped: test guard'
                ex.kill()
            elif stop == 'idle':
                v.last = 0
                await d.guard(time.monotonic())
            else:
                await d.begin(a, {'text': 'SLOW'})
                while not a.turn_id: await asyncio.sleep(.01)
                if stop == 'interrupt': await d.interrupt(a)
                else:
                    if stop == 'memory':
                        # Only the exec-server's RSS crosses the limit.
                        async def table(): return {ex.pid: [child]}, {ex.pid: 2000, child: 500}
                        m['process_table'] = table
                        a.record['memory_gb'] = .001
                    elif stop == 'hang': a.record['hang_s'] = .01; v.last = 0
                    else: a.record['wall_s'] = .01; a.started = 0
                    await d.guard(time.monotonic())
            await asyncio.wait_for(app.wait(), 4)
            await asyncio.wait_for(ex.wait(), 4)
            if stop == 'exec-crash':
                await asyncio.wait_for(a.finished.wait(), 4)
                events = []
                while not a.events.empty(): events.append(a.events.get_nowait())
                assert events[-1]['message'] == 'stopped: test guard'
            deadline = time.monotonic() + 4
            while True:
                try: os.kill(child, 0)
                except ProcessLookupError: break
                assert time.monotonic() < deadline, 'exec-server left a command running'
                await asyncio.sleep(.02)
        finally: await d.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['--exec-eof', '--exec-invalid', '--exec-silent', '--fail-environment'])
def test_tests_failed_start_is_bounded_and_reaps_both(tests_backend, rig, failure):
    d, m, _ = tests_backend
    m['EXEC_START_TIMEOUT'] = 1
    d.config['gpt']['program'].append(failure)
    async def scenario():
        try:
            started = time.monotonic()
            with pytest.raises(RuntimeError, match='exec-server|environment refused') as failed:
                await d.open({'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home), 'tests': True})
            expected = {'--exec-eof': 'closed stdout', '--exec-invalid': 'not a URL', '--exec-silent': 'within 1 s', '--fail-environment': 'environment refused'}
            assert expected[failure] in str(failed.value)
            assert time.monotonic() - started < 4
            assert not d.agents
            for event in rig.audit():
                if event.get('exec_launch') or event.get('launch'):
                    for pid in [event['pid'], *([event['child']] if event.get('exec_launch') else [])]:
                        with pytest.raises(ProcessLookupError): os.kill(pid, 0)
        finally: await d.close()
    asyncio.run(scenario())


def test_tests_profile_uses_parameters_and_protects_resolved_metadata(tmp_path, monkeypatch):
    m = load('lordd_tests_profile')
    work = tmp_path / 'work " (odd)'; work.mkdir()
    extra = tmp_path / 'extra'; extra.mkdir()
    target = extra / 'gitdata'; target.mkdir()
    (work / '.git').symlink_to(target, target_is_directory=True)
    key = tmp_path / 'actual-key'; key.write_text('decoy')
    m.HOME.mkdir(); (m.HOME / 'control.header').symlink_to(key)
    temp = tmp_path / 'temp'; temp.mkdir()
    profile, params = m.tests_profile(str(work), [str(extra)], {'TMPDIR': str(temp), 'CODEX_HOME': str(m.HOME / 'exec/a')})
    assert str(work) not in profile and str(extra) not in profile and str(key) not in profile
    assert all(p.startswith('-D') for p in params)
    assert {str(p.resolve()) for p in (work, extra, temp, key, target)} <= {p.split('=', 1)[1] for p in params}
    assert '(deny file-read*' in profile and '(deny file-read-data' in profile


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS Seatbelt profile')
@pytest.mark.parametrize('metadata_exists', [False, True])
def test_tests_real_seatbelt_permissions(tmp_path, metadata_exists):
    m = load('lordd_real_tests_profile')
    work = tmp_path / 'work'; work.mkdir()
    extra = tmp_path / 'extra'; extra.mkdir()
    temp = tmp_path / 'temp'; temp.mkdir()
    outside = tmp_path / 'outside'; outside.mkdir()
    m.HOME = outside
    (outside / 'control.header').write_text('decoy')
    gitdata = extra / 'gitdata'; gitdata.mkdir()
    (work / '.git').symlink_to(gitdata, target_is_directory=True)
    if metadata_exists:
        for root in (work, extra):
            for name in ('.git', '.codex', '.agents'):
                if not (root / name).exists(): (root / name).mkdir()
    listener = socket.socket(socket.AF_UNIX)
    # A short socket path under the suite's own basetemp.
    endpoint = str(tmp_path.parents[0] / ('s' + str(os.getpid())))
    listener.bind(endpoint); listener.listen()
    code = r'''
import json, os, pathlib, socket, subprocess, sys
work, extra, temp, outside, endpoint = map(pathlib.Path, sys.argv[1:])
s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()
c = socket.create_connection(s.getsockname(), timeout=2); a, _ = s.accept(); c.send(b'x'); assert a.recv(1) == b'x'
assert subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.boottime'], capture_output=True).returncode == 0
for root in (work, extra, temp): (root / 'ok').write_text('ok')
for path in (outside / 'no', outside / 'control.header', extra / 'gitdata/no', *[root / name / 'no' for root in (work, extra) for name in ('.git', '.codex', '.agents')]):
 try:
  if path.name == 'control.header': path.read_text()
  else: path.parent.mkdir(exist_ok=True); path.write_text('bad')
 except PermissionError: pass
 else: raise AssertionError(str(path) + ' allowed')
try: socket.socket(socket.AF_UNIX).connect(str(endpoint))
except PermissionError: pass
else: raise AssertionError('unix socket allowed')
try: assert subprocess.run(['/bin/ps', '-p', '1'], capture_output=True).returncode != 0
except PermissionError: pass
print('profile ok')
'''
    try:
        profile, params = m.tests_profile(str(work), [str(extra)], {'TMPDIR': str(temp), 'CODEX_HOME': str(m.HOME / 'exec/a')})
        result = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, *params, PYTHON, '-c', code, str(work), str(extra), str(temp), str(outside), endpoint], cwd=work, capture_output=True, text=True, timeout=8)
        assert result.returncode == 0, result.stdout + result.stderr
    finally: listener.close(); Path(endpoint).unlink(missing_ok=True)


def test_the_gpt_description_grows_no_longer_with_its_options():
    # The description is in every Claude Code session's context, so a new option replaces words rather than adding them.
    line = next(line for line in (ROOT / "agents/gpt.md").read_text().splitlines() if line.startswith("description: "))
    assert len(json.loads(line.removeprefix("description: "))) <= 975


def test_tests_exec_homes_are_per_agent(tests_backend, rig):
    d, _, _ = tests_backend
    async def scenario():
        try:
            for aid in ('a', 'b'):
                await d.open({'agentId': aid, 'family': 'gpt', 'cwd': str(rig.home), 'tests': True})
            homes = [Path(e['codex_home']) for e in rig.audit() if e.get('exec_launch')]
            assert homes == [d.journal.parent / 'exec' / aid for aid in ('a', 'b')]
            assert all(home.stat().st_mode & 0o777 == 0o700 for home in homes)
        finally: await d.close()
    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS Seatbelt profile')
def test_tests_exec_profiles_cannot_write_other_agents_aliases(tests_backend, rig):
    d, _, launched = tests_backend
    async def scenario():
        try:
            for aid in ('a', 'b'):
                cwd = rig.home / aid; cwd.mkdir()
                await d.open({'agentId': aid, 'family': 'gpt', 'cwd': str(cwd), 'tests': True})
            profiles, aliases = {}, {}
            for argv, kw in launched:
                if '/usr/bin/sandbox-exec' not in argv: continue
                at = argv.index('/usr/bin/sandbox-exec')
                end = at + 3
                while argv[end].startswith('-D'): end += 1
                aid = kw['env']['LORD_AGENT_ID']
                profiles[aid] = (argv[at:end], kw)
                aliases[aid] = Path(kw['env']['CODEX_HOME']) / 'tmp/arg0-macos' / f'codex-arg0-{aid}'
                aliases[aid].mkdir()
            code = '''import pathlib, sys
own, other = map(pathlib.Path, sys.argv[1:])
(own / 'apply_patch').write_text('own alias')
try: (other / 'apply_patch').write_text('planted alias')
except PermissionError: pass
else: raise AssertionError("another agent's alias folder is writable")
'''
            for aid, other in (('a', 'b'), ('b', 'a')):
                argv, kw = profiles[aid]
                result = subprocess.run([*argv, PYTHON, '-c', code, str(aliases[aid]), str(aliases[other])],
                    cwd=kw['cwd'], env=kw['env'], capture_output=True, text=True, timeout=8)
                assert result.returncode == 0, result.stdout + result.stderr
        finally: await d.close()
    asyncio.run(scenario())


def test_tests_prune_removes_the_agents_exec_home(tests_backend, rig):
    d, _, _ = tests_backend
    async def scenario():
        try:
            await d.open({'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home), 'tests': True})
            home = d.journal.parent / 'exec/a'
            home.mkdir(parents=True, exist_ok=True)
            (home / 'alias').write_text('stale')
            assert (await d.prune(0))['pruned'] == ['a']
            assert not home.exists()
        finally: await d.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('with_logs', [False, True])
def test_tests_sweep_removes_only_old_unrecorded_exec_homes_under_lock(tmp_path, monkeypatch, with_logs):
    m = load('lordd_exec_sweep')
    root = m.HOME / 'exec'; root.mkdir(parents=True)
    if with_logs: (m.HOME / 'logs').mkdir()
    for aid in ('gone', 'live', 'live.review', 'journal-only', 'legacy-only', 'recent'):
        home = root / aid; home.mkdir()
        (home / 'alias').write_text('kept until sweep')
        if aid != 'recent': os.utime(home, (0, 0))
    (root / 'notes.txt').write_text('not a home'); os.utime(root / 'notes.txt', (0, 0))
    d = object.__new__(m.Daemon)
    d.agents = {'live.review': object()}
    d.journal, d.legacy_journal = m.HOME / 'journal.jsonl', m.HOME / 'sessions.jsonl'
    d.journal.write_text(json.dumps({'agentId': 'journal-only'}) + '\n')
    d.legacy_journal.write_text(json.dumps({'agentId': 'legacy-only'}) + '\n')
    held, removed = [], []
    real_lock, real_rmtree = m.Daemon.journal_lock, m.shutil.rmtree
    @contextlib.contextmanager
    def lock(self):
        with real_lock(self):
            held.append(True)
            try: yield
            finally: held.pop()
    def remove(path, **kw):
        removed.append((Path(path).name, bool(held)))
        return real_rmtree(path, **kw)
    monkeypatch.setattr(m.Daemon, 'journal_lock', lock)
    monkeypatch.setattr(m.shutil, 'rmtree', remove)
    d.sweep(14)
    assert sorted(removed) == [('gone', True), ('live', True)]
    assert sorted(p.name for p in root.iterdir()) == ['journal-only', 'legacy-only', 'live.review', 'notes.txt', 'recent']


def test_nosandbox_reports_network_on_in_open_and_turn_log(rig):
    opened = rig.open('gpt', nosandbox=True, net=False)
    assert opened['sandbox'] == 'none' and opened['network'] is True
    assert 'sandbox none · network on' in opened['line']
    rig.turn('PING')
    assert 'sandbox none · network on' in (rig.home / 'logs/a.log').read_text()
    assert rig.open('gpt', net=False)['network'] is True

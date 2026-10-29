"""Remembered processes, turn ownership and message folder application."""
import asyncio
import json
import os
from pathlib import Path
import random
import signal
import string
import sys
from types import SimpleNamespace

import pytest

from test_lordd import ROOT, PYTHON, load, rig, tests_backend, grok_home
from test_round3 import scope
import test_round7


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS process births and hidden shell environments')
@pytest.mark.parametrize('server', ['process', 'exec_process'])
def test_r8_hidden_shell_is_stopped_after_vendor_exit(scope, server):
    _, a, m, work, *_ = scope
    async def run():
        v = m.Vendor(SimpleNamespace(log=lambda text: None), 'gpt', a)
        parent, child = None, None
        code = """import subprocess,time
p=subprocess.Popen(['/bin/zsh','-f','-c','while :; do /bin/sleep 1; done'],start_new_session=True,stdin=-3,stdout=-3,stderr=-3)
print(p.pid,flush=True)
time.sleep(300)
"""
        try:
            parent = await asyncio.create_subprocess_exec(PYTHON, '-B', '-c', code, cwd=work,
                env={'HOME': str(work), 'ZDOTDIR': str(work), 'PATH': '/usr/bin:/bin', 'LORD_AGENT_ID': a.agentId, 'LORD_DAEMON_SOCKET': str(m.SOCKET)},
                start_new_session=True, stdin=-3, stdout=-1, stderr=-3)
            setattr(v, server, parent)
            child = int(await asyncio.wait_for(parent.stdout.readline(), 5))
            await asyncio.sleep(.2)
            await v.update_jobs()
            assert any(job['pid'] == child for job in a.jobs)
            assert child not in m.marked_pids(await m.process_markers(), a.agentId)
            parent.terminate(); await asyncio.wait_for(parent.wait(), 5)
            await v.update_jobs()
            await v.close(); await v.close()
            assert not await m.job_details({child}), 'remembered shell survived cleanup'
        finally:
            if child:
                try: os.killpg(child, signal.SIGKILL)
                except ProcessLookupError: pass
            if parent:
                if parent.returncode is None: parent.kill()
                await parent.wait()
    asyncio.run(run())


@pytest.mark.parametrize('reused', [False, True])
@pytest.mark.parametrize('lookup_failure', [False, True])
def test_r8_remembered_process_birth_prevents_reuse(scope, monkeypatch, reused, lookup_failure):
    _, a, m, *_ = scope
    stage, signals = [0], []
    process = SimpleNamespace(pid=900001, returncode=None)
    async def wait(): pass
    process.wait = wait
    async def table():
        if stage[0] and lookup_failure: raise RuntimeError('synthetic lookup failure')
        return ({900001: [900002]} if not stage[0] else {}), {}
    async def markers(): return m.ProcessMarkers()
    async def jobs(pids): return [{'pid': pid, 'command': 'synthetic', 'age_s': 1} for pid in pids]
    def birth(pid):
        if pid == 900001: return (1, 1) if not stage[0] else None
        return (3 if reused and stage[0] else 2, 1)
    monkeypatch.setattr(m, 'process_table', table); monkeypatch.setattr(m, 'process_markers', markers)
    monkeypatch.setattr(m, 'job_details', jobs); monkeypatch.setattr(m, 'process_start', birth)
    monkeypatch.setattr(m, 'signal_all', lambda pids, sig: signals.extend((pid, sig) for pid in pids))
    async def run():
        v = m.Vendor(SimpleNamespace(log=lambda text: None), 'gpt', a); v.process = process
        await v.update_jobs()
        stage[0] = 1; process.returncode = 0
        if not lookup_failure: await v.update_jobs()
        await v.close()
        assert ((900002, signal.SIGTERM) in signals) is not reused
        assert ((900002, signal.SIGKILL) in signals) is not reused
    asyncio.run(run())


@pytest.mark.parametrize('family', ['gpt', 'grok', 'gemini'])
def test_r8_delayed_finish_does_not_end_a_later_turn(scope, family):
    d, a, m, work, *_ = scope
    d.config = {family: {}}; d.vendors, d.tasks = {}, set()
    a.family = a.record['family'] = family; a.state = 'idle'
    d.log = lambda text: None; d.save = lambda agent: None; d.quota_refusal = lambda *args: None
    async def noop(*args, **kwargs): pass
    async def dispatch(agent, body, retry=False): agent.turn_id = body['text']; agent.dispatched = True
    async def interrupted(*args): a.emit('error', message='interrupted', retryable=False); return {}
    d.effort = d.attach = noop; d.run_turn = dispatch
    d.settings = lambda agent: {'model': 'fake', 'effort': 'high', 'cwd': str(work), 'dirs': [], 'sandbox': 'workspace-write', 'network': False, 'tools': False, 'nice': 0, 'memory_gb': 24, 'hang_s': None, 'wall_s': None}
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def lookup(*args, **kwargs): entered.set(); await release.wait()
        d.refresh_jobs = lookup
        vendor = d.backend(a); vendor.close = noop; vendor.rpc = vendor.send = interrupted
        try:
            await d.begin(a, {'text': 'first'}); await a.turn_task
            old = d.task(d.finish(a, text='old answer', usage={}, model='fake'))
            await asyncio.wait_for(entered.wait(), 2)
            await d.interrupt(a, 'TaskStop')
            await d.begin(a, {'text': 'second'}); await a.turn_task
            release.set(); await asyncio.wait_for(old, 2)
            assert a.state == 'running' and a.turn_id == 'second'
            assert a.events.empty() and not a.answer_path.exists()
        finally:
            release.set()
            pending = list(d.tasks)
            for task in pending: task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize('option', ['cwd', 'dir'])
def test_r8_missing_message_folder_is_refused_at_send(scope, option):
    d, a, m, *_ = scope
    with pytest.raises(ValueError, match='does not exist'):
        d.confine_call(a, 'SendMessage', {'to': a.agentId, 'message': f'@{option} missing\nwork'}, m.delegation_access(a))
    assert d.message_tickets(a) == []


@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('swap', [False, True])
def test_r8_message_ticket_survives_age_prune_and_unrelated_start(tests_backend, rig, restart, swap):
    d, m, _ = tests_backend
    work = rig.home / 'work'; slot = work / 'slot'; slot.mkdir()
    outside = rig.home / 'outside'; outside.mkdir()
    body = {'agentId': 'receiver', 'family': 'gpt', 'cwd': str(work)}
    async def run():
        current = d
        try:
            await d.open(body); a = d.agents['receiver']
            d.confine_call(a, 'SendMessage', {'to': a.agentId, 'message': f'@dir {slot}\nwork'}, m['delegation_access'](a))
            [ticket] = d.message_tickets(a); path = m['HOME'] / 'launches' / f'{ticket}.json'
            entry = json.loads(path.read_text()); entry['expires'] = 0; path.write_text(json.dumps(entry))
            os.utime(path, (1, 1))
            if restart:
                await d.close(); current = m['Daemon'](); await current.open(body)
                a = current.agents['receiver']
            await current.prune_locked(14)
            assert path.exists() and current.message_tickets(a) == [ticket]
            if swap: slot.rename(work / 'saved'); slot.symlink_to(outside, target_is_directory=True)
            await current.backend(a).close(); a.generation = 0
            await current.begin(a, {'text': 'PING'}); await asyncio.wait_for(a.finished.wait(), 5)
            assert a.answer == 'PONG' and current.message_tickets(a) == [ticket]
            if swap:
                with pytest.raises(ValueError, match='changed after it was checked'):
                    await current.open({**body, 'dirs': [str(slot)]})
                slot.unlink(); (work / 'saved').rename(slot)
            await current.open({**body, 'dirs': [str(slot)]})
            assert not path.exists() and current.message_tickets(a) == []
        finally: await current.close()
    asyncio.run(run())


def test_r8_removed_unapplied_folder_does_not_block_receiver(tests_backend, rig):
    d, m, _ = tests_backend; work = rig.home / 'work'; slot = work / 'slot'; slot.mkdir()
    body = {'agentId': 'receiver', 'family': 'gpt', 'cwd': str(work)}
    async def run():
        try:
            await d.open(body); a = d.agents['receiver']
            d.confine_call(a, 'SendMessage', {'to': a.agentId, 'message': f'@dir {slot}\nwork'}, m['delegation_access'](a))
            slot.rmdir()
            await d.open(body)
            await d.backend(a).close(); a.generation = 0
            await d.begin(a, {'text': 'PING'}); await asyncio.wait_for(a.finished.wait(), 5)
            with pytest.raises(ValueError, match='does not exist'): await d.open({**body, 'dirs': [str(slot)]})
            await d.open(body)
            assert d.message_tickets(a)
        finally: await d.close()
    asyncio.run(run())


def test_r8_pruning_receiver_removes_its_pending_tickets(tests_backend, rig):
    d, m, _ = tests_backend; work = rig.home / 'work'
    async def noop(*args, **kwargs): pass
    async def run():
        try:
            await d.open({'agentId': 'receiver', 'family': 'gpt', 'cwd': str(work)})
            a = d.agents['receiver']
            d.confine_call(a, 'SendMessage', {'to': a.agentId, 'message': f'@dir {work}\nwork'}, m['delegation_access'](a))
            [ticket] = d.message_tickets(a); path = m['HOME'] / 'launches' / f'{ticket}.json'
            a.record['used'] = '2000-01-01T00:00:00+00:00'
            d.service = lambda *args: SimpleNamespace(ensure=noop, rpc=noop)
            assert 'receiver' in (await d.prune_locked(1))['pruned']
            assert not path.exists() and not (m['HOME'] / 'messages' / 'receiver.json').exists()
        finally: await d.close()
    asyncio.run(run())


def test_r8_message_ticket_limit_stays_bounded(scope):
    d, a, m, work, *_ = scope
    for _ in range(64):
        d.confine_call(a, 'SendMessage', {'to': a.agentId, 'message': f'@dir {work}\nwork'}, m.delegation_access(a))
    with pytest.raises(ValueError, match='too many pending'):
        d.confine_call(a, 'SendMessage', {'to': a.agentId, 'message': f'@dir {work}\nwork'}, m.delegation_access(a))
    assert len(d.message_tickets(a)) == 64


@pytest.mark.parametrize('lines', [160, 900, 1100])
def test_r8_long_exact_secret_uses_bounded_lookback(scope, monkeypatch, lines):
    _, a, m, *_ = scope; rng = random.Random(11)
    body = '\n'.join(''.join(rng.choice(string.ascii_letters + string.digits + '+/') for _ in range(64)) for _ in range(lines))
    secret = '-----BEGIN CERTIFICATE CHAIN KEY-----\n' + body + '\n-----END CERTIFICATE CHAIN KEY-----'
    original = m.Redactor(); original.secrets.add(secret); sizes = []
    def redact(value, **kwargs): sizes.append(len(value)); return original(value, **kwargs)
    redact.secrets = original.secrets; monkeypatch.setattr(m, 'REDACT', redact)
    output = 'build log\n' * 1000 + 'BUNDLE_PRIVATE_KEY=' + secret + '\n' + 'done: 42 files\n' * 40
    async def run():
        a.rows['r'] = asyncio.get_running_loop().create_future(); a.row_done('r', output)
        shown = (await a.rows['r'])['output']
        if len(secret) <= 65536: assert not set(body.splitlines()).intersection(shown.splitlines())
        assert shown.endswith('done: 42 files\n')
        assert m.ROW_OUTPUT + 8192 < sizes[-1] <= m.ROW_OUTPUT + 65536
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['readonly-gpt', 'readonly-grok', 'nosandbox', 'gemini', 'journal', 'closed', 'writable', 'incoming'])
@pytest.mark.parametrize('folder', ['cwd', 'dir'])
def test_r8_vendor_refusal_counts_only_writable_open_agents(scope, monkeypatch, mode, folder):
    d, _, m, work, extra, outside = scope
    monkeypatch.setenv('TMPDIR', str(outside / 'temp'))
    target = work if folder == 'cwd' else extra
    program = target / 'vendor'; program.write_text('#!/bin/sh\nexit 0\n'); program.chmod(0o755)
    a = m.Agent({'agentId': 'owner', 'family': 'gemini' if mode == 'gemini' else 'grok' if mode == 'readonly-grok' else 'gpt',
        'model': 'fake', 'cwd': str(work), 'dirs': [str(extra)], 'readonly': mode.startswith('readonly'), 'nosandbox': mode == 'nosandbox'})
    a.opened = mode not in ('journal', 'incoming')
    if mode == 'closed': a.state = 'closed'
    d.agents = {'owner': a}; d.config = {'gpt': {'program': str(program)}}
    if mode in ('writable', 'incoming'):
        with pytest.raises(RuntimeError, match='vendor program') as error: d.vendor_program('gpt', a if mode == 'incoming' else None)
        assert a.agentId in str(error.value) and str(target) in str(error.value)
    else: assert d.vendor_program('gpt') == [str(program)]


@pytest.mark.parametrize('protected', ['home', 'plugin'])
@pytest.mark.parametrize('folder', ['cwd', 'dir'])
@pytest.mark.parametrize('mode', ['workspace', 'net', 'readonly', 'tests', 'nosandbox'])
def test_r8_settings_name_protected_read_only_folders(scope, monkeypatch, protected, folder, mode):
    d, a, m, work, _, outside = scope
    plugin = outside / 'plugin'; plugin.mkdir(); monkeypatch.setattr(m, 'HERE', plugin / 'lordd.py')
    root = m.HOME if protected == 'home' else plugin
    path = root / 'child'; path.mkdir()
    a.cwd = str(path if folder == 'cwd' else work)
    a.record['dirs'] = [str(path)] if folder == 'dir' else []
    if mode != 'workspace': a.record[mode] = True
    d.config = {'gpt': {}, 'vendor_home': str(outside)}
    line = m.settings_line(d.settings(a))
    if mode == 'nosandbox': assert 'read-only folders' not in line
    else: assert f'read-only folders {path}' in line


def test_r8_docs_describe_messages_parameters_and_environment():
    readme = (ROOT / 'README.md').read_text()
    assert 'only the options wait' in readme
    for name in ('LORD_HOME', 'LORD_CONFIG', 'GROK_HOME', 'TMPDIR', 'LORD_AGENT_ID', 'LORD_MCP_TOKEN', 'LORD_READONLY'):
        assert '`' + name + '`' in readme
    contract = (ROOT / 'CONTRACT.md').read_text()
    assert 'socket and plugin' in contract and '65,536' in contract
    for name in ('README.md', 'PRIVACY.md', 'CONTRACT.md'):
        assert 'Message tickets do not expire' in (ROOT / name).read_text()


def test_r8_native_test_skips_when_codex_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(test_round7.shutil, 'which', lambda *args, **kwargs: None)
    def unexpected(*args, **kwargs): raise AssertionError('missing codex reached native execution')
    monkeypatch.setattr(test_round7.subprocess, 'run', unexpected)
    with pytest.raises(pytest.skip.Exception, match='codex'):
        test_round7.test_r7_native_profiles_protect_both_plugin_spellings(tmp_path, monkeypatch, 'workspace')


def test_r8_native_test_uses_resolved_production_paths(tmp_path, monkeypatch):
    original = test_round7.load
    class Checked(Exception): pass
    def capture(name):
        m = original(name)
        def profile(a):
            assert m.HERE == m.HERE.resolve()
            assert all(path == str(Path(path).resolve()) for path in a.record['dirs'])
            raise Checked()
        m.gpt_permissions = profile
        return m
    monkeypatch.setattr(test_round7, 'load', capture)
    monkeypatch.setattr(test_round7.shutil, 'which', lambda *args, **kwargs: '/tmp/synthetic-codex')
    with pytest.raises(Checked):
        test_round7.test_r7_native_profiles_protect_both_plugin_spellings(tmp_path, monkeypatch, 'workspace')

"""Turn cancellation, detached jobs and bounded folder-ticket cleanup."""
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import time
from types import SimpleNamespace

import pytest

from test_lordd import ROOT, PYTHON, rig, tests_backend, grok_home
from test_round3 import scope


@pytest.fixture
def turn_case(scope):
    d, a, m, work, *_ = scope
    d.config = {'gpt': {}, 'grok': {}, 'gemini': {}}
    d.vendors, d.tasks, d.stopping = {}, set(), False
    d.log = lambda text: None
    d.save = d.quota_refusal = lambda *args: None
    d.settings = lambda agent: {'model': 'fake', 'effort': 'high', 'cwd': str(work), 'dirs': [],
        'sandbox': 'workspace-write', 'network': False, 'tools': False, 'nice': 0,
        'memory_gb': 24, 'hang_s': None, 'wall_s': None}
    async def noop(*args, **kwargs): pass
    async def dispatch(agent, body, retry=False): agent.turn_id = body['text']; agent.dispatched = True
    d.attach = d.effort = noop
    d.run_turn = dispatch
    a.state = 'idle'
    return d, a, m


@pytest.mark.parametrize('mode', ['error', 'timeout', 'ok'])
def test_r9_interrupt_always_reaches_wait_and_close(turn_case, mode):
    d, a, _ = turn_case
    closed = []
    async def rpc(*args):
        if mode == 'error': raise RuntimeError('no active turn')
        if mode == 'timeout': raise TimeoutError('no reply')
        return {}
    async def close(): closed.append(True)
    async def run():
        v = d.backend(a); v.rpc, v.close = rpc, close
        await d.begin(a, {'text': 'first'}); await a.turn_task
        await asyncio.wait_for(d.interrupt(a, 'TaskStop'), 8)
        assert closed and a.state == 'idle' and a.finished.is_set()
        assert json.loads(a.record_path.read_text())['turns'][-1]['ended_how'] == 'interrupted'
        assert a.events.empty()
        await d.begin(a, {'text': 'second'}); await a.turn_task
        assert a.state == 'running' and a.turn_id == 'second'
    asyncio.run(run())


@pytest.mark.parametrize('family', ['gpt', 'grok', 'gemini'])
def test_r9_cancelled_finish_ends_only_its_own_running_turn(turn_case, family):
    d, a, _ = turn_case
    a.family = a.record['family'] = family
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def lookup(*args, **kwargs): entered.set(); await release.wait()
        d.refresh_jobs = lookup
        await d.begin(a, {'text': 'first'}); await a.turn_task
        old = asyncio.create_task(d.finish(a, text='old answer', usage={}, model='fake'))
        try:
            await entered.wait(); a.cancel_requested = True
            release.set(); await old
            event = a.events.get_nowait()
            assert event['type'] == 'error' and event['message'] == 'interrupted'
            assert a.state == 'idle' and a.finished.is_set() and not a.answer_path.exists()
            assert event['record']['ended_how'] == 'interrupted'
            await d.begin(a, {'text': 'second'}); await a.turn_task
            entered.clear(); release.clear()
            stale = asyncio.create_task(d.finish(a, text='second answer', usage={}, model='fake'))
            await entered.wait()
            a.emit('error', message='interrupted', retryable=False); a.events.get_nowait()
            await d.begin(a, {'text': 'third'}); await a.turn_task
            a.cancel_requested = True
            release.set(); await stale
            assert a.state == 'running' and a.events.empty() and not a.finished.is_set()
        finally:
            release.set()
            await old
    asyncio.run(run())


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS process births and hidden shell environments')
@pytest.mark.parametrize('family,server', [('gpt', 'process'), ('gpt', 'exec_process'), ('grok', 'process'), ('gemini', 'process')])
def test_r9_detached_hidden_shell_stays_a_job_past_idle_minute(scope, family, server):
    d, a, m, work, *_ = scope
    d.config = {'idle_exit_s': 600, 'process_idle_s': 60, family: {}}
    d.vendors, d.stopping = {}, False
    d.log = lambda text: None
    d.refresh = lambda *args: None
    a.family = a.record['family'] = family; a.state = 'idle'
    launcher = """import subprocess,sys
p=subprocess.Popen(['/bin/zsh','-f','-c','while :; do /bin/sleep 1; done'],start_new_session=True,stdin=-3,stdout=-3,stderr=-3)
print(p.pid,flush=True)
sys.stdin.readline()
"""
    vendor = ("import subprocess,sys,time\n"
        f"p=subprocess.Popen([sys.executable,'-B','-c',{launcher!r}],stdin=-1,stdout=-1,stderr=-3)\n"
        "print(int(p.stdout.readline()),flush=True)\n"
        "sys.stdin.readline()\n"
        "p.stdin.write(b'detach\\n');p.stdin.flush();p.wait(timeout=5)\n"
        "print('detached',flush=True)\n"
        "time.sleep(300)\n")
    async def run():
        parent = child = birth = None
        v = d.backend(a)
        try:
            parent = await asyncio.create_subprocess_exec(PYTHON, '-B', '-c', vendor, cwd=work,
                env={'HOME': str(work), 'ZDOTDIR': str(work), 'PATH': '/usr/bin:/bin',
                     'LORD_AGENT_ID': a.agentId, 'LORD_DAEMON_SOCKET': str(m.SOCKET)},
                start_new_session=True, stdin=-1, stdout=-1, stderr=-3)
            setattr(v, server, parent)
            child = int(await asyncio.wait_for(parent.stdout.readline(), 5)); birth = m.process_start(child)
            assert birth is not None
            await v.update_jobs()
            assert child in {job['pid'] for job in a.jobs} and a.process_births[child] == birth
            parent.stdin.write(b'detach\n'); await parent.stdin.drain()
            assert await asyncio.wait_for(parent.stdout.readline(), 5) == b'detached\n'
            assert child not in m.marked_pids(await m.process_markers(), a.agentId)
            await v.update_jobs()
            assert child in {job['pid'] for job in a.jobs}
            v.last = time.monotonic() - 61
            await asyncio.wait_for(d.guard(time.monotonic()), 10)
            assert parent.returncode is None and m.process_start(child) == birth
            assert child in {job['pid'] for job in d.listed()[0]['jobs']}
            await d.route('POST', '/close', {'agentId': a.agentId})
            assert not await m.job_details({child}) and parent.returncode is not None
            await v.close()
        finally:
            if child is not None and birth is not None and m.process_start(child) == birth:
                try: os.killpg(child, signal.SIGKILL)
                except ProcessLookupError: pass
            if parent:
                await v.close()
                if parent.returncode is None: parent.kill()
                await parent.wait()
    asyncio.run(run())


@pytest.mark.parametrize('backend', [False, True])
@pytest.mark.parametrize('status', ['alive', 'gone', 'reborn'])
def test_r9_refresh_lists_remembered_tree_and_drops_old_births(scope, monkeypatch, backend, status):
    d, a, m, *_ = scope
    d.jobs_lock, d.vendors = asyncio.Lock(), {}
    birth = (1, 2); a.process_births = {900001: birth}
    async def table(): return {900001: [900002]}, {}
    async def markers(): return m.ProcessMarkers()
    async def details(pids): return [{'pid': pid, 'command': 'synthetic', 'age_s': 1} for pid in pids]
    monkeypatch.setattr(m, 'process_table', table); monkeypatch.setattr(m, 'process_markers', markers)
    monkeypatch.setattr(m, 'job_details', details)
    monkeypatch.setattr(m, 'process_start', lambda pid: (None if status == 'gone' else (3, 4) if status == 'reborn' else birth) if pid == 900001 else (5, 6))
    async def run():
        if backend: d.backend(a)
        await d.refresh_jobs()
        assert {job['pid'] for job in a.jobs} == ({900001, 900002} if status == 'alive' else set())
        assert (900001 in a.process_births) is (status == 'alive')
        if status == 'alive': assert a.process_births[900002] == (5, 6)
    asyncio.run(run())


@pytest.mark.parametrize('family', ['gpt', 'grok'])
@pytest.mark.parametrize('folder', ['cwd', 'dir'])
def test_r9_sending_held_folders_creates_no_ticket(scope, family, folder):
    d, a, m, work, extra, _ = scope
    a.family = a.record['family'] = family
    target = work if folder == 'cwd' else extra
    for _ in range(65):
        data = {'to': a.agentId, 'message': f'@{folder} {target}\nnext item'}
        if family == 'grok' and folder == 'dir':
            with pytest.raises(ValueError, match='not available for grok'): d.confine_call(a, 'SendMessage', data, m.delegation_access(a))
        else: d.confine_call(a, 'SendMessage', data, m.delegation_access(a))
        assert d.message_tickets(a) == []
    assert not list((m.HOME / 'launches').glob('*.json'))


@pytest.mark.parametrize('held_result', [False, True])
def test_r9_successful_open_consumes_held_snapshot_without_ticket_pin_check(tests_backend, rig, monkeypatch, held_result):
    d, m, _ = tests_backend; work = rig.home / 'work'; slot = work / 'slot'; slot.mkdir()
    body = {'agentId': 'receiver', 'family': 'gpt', 'cwd': str(work), 'dirs': [str(slot)]}
    async def run():
        try:
            await d.open(body); a = d.agents['receiver']
            pending = d.launch_folders(folders=[str(slot)], agent_id=a.agentId, message=True)
            d.message_tickets(a, add=pending)
            if held_result: a.carry.append({'type': 'done', 'text': 'held'})
            original = m['pinned_folder']
            def pin(label, path, pins, checked=()):
                assert str(slot) not in checked, 'held ticket was checked as a new grant'
                return original(label, path, pins, checked)
            monkeypatch.setitem(m, 'pinned_folder', pin)
            with pytest.raises(ValueError, match='@nice'): await d.open({**body, 'nice': -1})
            assert d.message_tickets(a) == [pending]
            await d.open(body)
            assert d.message_tickets(a) == []
            assert not (m['HOME'] / 'launches' / f'{pending}.json').exists()
        finally: await d.close()
    asyncio.run(run())


@pytest.mark.parametrize('bad', ['truncated', 'oversized', 'wrong-type', 'unreadable'])
@pytest.mark.parametrize('retire', [False, True])
def test_r9_prune_removes_bad_manifest_and_keeps_pruning(tests_backend, rig, monkeypatch, bad, retire):
    d, m, _ = tests_backend; work = rig.home / 'work'
    async def noop(*args, **kwargs): pass
    async def run():
        try:
            for aid in ('receiver', 'later'):
                await d.open({'agentId': aid, 'family': 'gpt', 'cwd': str(work)})
                if retire: d.agents[aid].record['used'] = '2000-01-01T00:00:00+00:00'
            receiver = d.agents['receiver']
            orphan = d.launch_folders(folders=[str(work)])
            named = d.launch_folders(folders=[str(work)], agent_id='other', message=True)
            for ticket in (orphan, named): os.utime(m['HOME'] / 'launches' / f'{ticket}.json', (1, 1))
            manifests = m['HOME'] / 'messages'; manifests.mkdir(exist_ok=True)
            (manifests / 'other.json').write_text(json.dumps([named]))
            manifest = manifests / 'receiver.json'
            manifest.write_text({'truncated': '["123', 'oversized': ' ' * 4097, 'wrong-type': '{}', 'unreadable': '[]'}[bad])
            original = Path.read_text
            def read(path, *args, **kwargs):
                if path == manifest and bad == 'unreadable': raise PermissionError('synthetic unreadable manifest')
                return original(path, *args, **kwargs)
            monkeypatch.setattr(Path, 'read_text', read)
            d.service = lambda *args: SimpleNamespace(ensure=noop, rpc=noop)
            removed = (await d.prune_locked(1))['pruned']
            assert set(removed) == ({'receiver', 'later'} if retire else set())
            assert not manifest.exists()
            assert not (m['HOME'] / 'launches' / f'{orphan}.json').exists()
            assert (m['HOME'] / 'launches' / f'{named}.json').exists()
            assert 'receiver.json' in (m['HOME'] / 'lordd.log').read_text()
            assert receiver.agentId not in d.agents if retire else receiver.agentId in d.agents
        finally: await d.close()
    asyncio.run(run())


@pytest.mark.parametrize('missing', ['manifest', 'ticket'])
def test_r9_unapplied_broken_metadata_does_not_block_ordinary_open(tests_backend, rig, missing):
    d, m, _ = tests_backend; work = rig.home / 'work'; slot = work / 'slot'; slot.mkdir()
    body = {'agentId': 'receiver', 'family': 'gpt', 'cwd': str(work)}
    async def run():
        try:
            await d.open(body); a = d.agents['receiver']
            d.confine_call(a, 'SendMessage', {'to': a.agentId, 'message': f'@dir {slot}\nnext item'}, m['delegation_access'](a))
            [ticket] = d.message_tickets(a)
            if missing == 'manifest': (m['HOME'] / 'messages' / 'receiver.json').write_text('[')
            else: (m['HOME'] / 'launches' / f'{ticket}.json').unlink()
            await d.open(body)
        finally: await d.close()
    asyncio.run(run())


def test_r9_prune_continues_if_manifest_breaks_during_close(tests_backend, rig):
    d, m, _ = tests_backend; work = rig.home / 'work'
    async def noop(*args, **kwargs): pass
    async def run():
        try:
            for aid in ('receiver', 'later'):
                await d.open({'agentId': aid, 'family': 'gpt', 'cwd': str(work)})
                d.agents[aid].record['used'] = '2000-01-01T00:00:00+00:00'
            manifest = m['HOME'] / 'messages' / 'receiver.json'; manifest.parent.mkdir(exist_ok=True)
            vendor = d.backend(d.agents['receiver']); close = vendor.close
            async def corrupt():
                await close(); manifest.write_text('[')
            vendor.close = corrupt
            d.service = lambda *args: SimpleNamespace(ensure=noop, rpc=noop)
            assert set((await d.prune_locked(1))['pruned']) == {'receiver', 'later'}
            assert not manifest.exists()
        finally: await d.close()
    asyncio.run(run())


@pytest.mark.parametrize('protected', ['home', 'plugin'])
@pytest.mark.parametrize('readonly', [False, True])
def test_r9_grok_settings_name_read_only_folders(scope, monkeypatch, protected, readonly):
    d, a, m, _, _, outside = scope
    plugin = outside / 'plugin'; plugin.mkdir(); monkeypatch.setattr(m, 'HERE', plugin / 'lordd.py')
    root = m.HOME if protected == 'home' else plugin
    path = root / 'child'; path.mkdir()
    a.family = a.record['family'] = 'grok'; a.cwd = str(path); a.record['dirs'] = []
    a.record['readonly'] = readonly
    d.config = {'grok': {}, 'vendor_home': str(outside)}
    assert f'read-only folders {path}' in m.settings_line(d.settings(a))


def test_r9_contract_limits_birth_observation_and_describes_ticket_cleanup():
    contract = (ROOT / 'CONTRACT.md').read_text()
    assert 'before it can leave the tree' not in contract
    assert 'when a refresh observes a process' in contract
    assert 'parent exits before any refresh' in contract
    assert 'remembered pids are never adopted by job listing' not in contract
    for name in ('README.md', 'PRIVACY.md', 'CONTRACT.md'):
        text = (ROOT / name).read_text()
        assert 'already holds every named folder' in text
        assert 'consume held-folder tickets without checking' in text
    assert 'gpt or grok working' in (ROOT / 'README.md').read_text()

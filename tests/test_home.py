"""Home identity, persistence and shutdown cleanup."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
import shutil
import signal
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_lordd import DAEMON, PYTHON, grok_home, rig, load, tests_backend


@pytest.mark.parametrize('failure', ['dangling', 'emit', 'logger'])
def test_shutdown_closes_all_vendors_despite_persistence_errors(tmp_path, monkeypatch, failure):
    m = load('home_cleanup')
    target = tmp_path / 'target'; target.mkdir()
    m.HOME = tmp_path / 'home'; m.HOME.symlink_to(target, target_is_directory=True)
    d = m.Daemon()
    a = m.Agent({'agentId': 'a', 'family': 'gpt', 'model': 'fake', 'cwd': str(tmp_path)})
    a.state = 'running'
    vendor = SimpleNamespace(close=AsyncMock())
    d.agents, d.vendors = {'a': a}, {'a': vendor}
    monkeypatch.setattr(m, 'others_answering', AsyncMock(return_value=True))
    if failure == 'dangling': shutil.rmtree(target)
    else:
        def broken(*args, **kwargs): raise OSError('unavailable persistence')
        monkeypatch.setattr(a, 'emit', broken)
        if failure == 'logger': monkeypatch.setattr(d, 'log', broken)
    asyncio.run(d.close())
    assert vendor.close.await_count == 1
    if failure == 'dangling': assert not target.exists()


@pytest.mark.parametrize('writer', ['note', 'reads', 'record', 'turn', 'done', 'journal', 'quota', 'allowance'])
@pytest.mark.parametrize('replacement', [False, True])
def test_persistence_does_not_recreate_or_write_a_replaced_home(tmp_path, writer, replacement):
    m = load('home_writes'); m.HOME = tmp_path / 'home'
    d = m.Daemon()
    a = m.Agent({'agentId': 'a', 'family': 'gpt', 'model': 'fake', 'cwd': str(tmp_path)})
    a.settings = {'model': 'fake', 'effort': 'high', 'cwd': str(tmp_path)}
    a.state = 'running'
    shutil.rmtree(m.HOME)
    if replacement:
        m.HOME.mkdir(); (m.HOME / 'keep').write_text('untouched')
    try:
        if writer == 'note': a.note('step')
        elif writer == 'reads': a.read_paths([('read', '/repo/file')])
        elif writer == 'record': a.write_record(a.settings)
        elif writer == 'turn': a.turn_record('done', {'text': 'PONG'})
        elif writer == 'done': a.emit('done', text='PONG')
        elif writer == 'journal': d.save(a)
        elif writer == 'quota': d.save_quota(('gpt', 'fake'), (time.time() + 10, 'quota'))
        else: d.allowance({'limitId': 'synthetic'})
    except OSError: pass
    if replacement:
        assert sorted(p.name for p in m.HOME.iterdir()) == ['keep']
        assert (m.HOME / 'keep').read_text() == 'untouched'
    else: assert not m.HOME.exists()


def test_heartbeat_detects_a_replaced_directory(tmp_path):
    m = load('home_replacement'); m.HOME = tmp_path / 'home'
    d = m.Daemon(); d.config['idle_exit_s'] = .05; d.requests = 1
    m.HOME.rename(tmp_path / 'old'); m.HOME.mkdir()
    async def check():
        try: await asyncio.wait_for(d.heartbeat(), .5)
        except TimeoutError: pytest.fail('replacement home kept the daemon alive')
        assert d.stop_event.is_set()
    asyncio.run(check())


@pytest.mark.parametrize('loss', ['deleted', 'replaced', 'dangling'])
@pytest.mark.parametrize('stepping', [False, True])
def test_home_loss_stops_vendor_and_jobs_without_recreating_home(rig, loss, stepping):
    rig.open('gpt'); rig.turn('BACKGROUND')
    audit = rig.audit()
    vendor = next(e['pid'] for e in audit if e.get('launch') and e['family'] == 'gpt')
    jobs = [e['command_pid'] for e in audit if 'command_pid' in e]
    m = load('home_process_identity')
    births = {pid: m.process_start(pid) for pid in [vendor, *jobs]}
    pool = ThreadPoolExecutor(max_workers=1)
    def drain():
        try:
            for _ in rig.stream('/turn', {'agentId': 'a', 'text': 'STEP_WAIT'}): pass
        except (OSError, ValueError): pass
    try:
        if stepping:
            pool.submit(drain)
            deadline = time.monotonic() + 3
            while not any(e.get('stepping') for e in rig.audit()):
                assert time.monotonic() < deadline
                time.sleep(.01)
        aside = rig.home.with_name(rig.home.name + '-removed')
        rig.home.rename(aside); shutil.rmtree(aside)
        if loss == 'replaced':
            rig.home.mkdir(); (rig.home / 'keep').write_text('untouched')
            (rig.home / 'lordd.sock').write_text('replacement socket')
        elif loss == 'dangling': rig.home.symlink_to(aside, target_is_directory=True)
        try: assert rig.proc.wait(timeout=4) == 0
        except subprocess.TimeoutExpired: pytest.fail('daemon stayed alive after losing its original home')
        for pid in births:
            with pytest.raises(ProcessLookupError): os.kill(pid, 0)
        if loss == 'replaced':
            assert sorted(p.name for p in rig.home.iterdir()) == ['keep', 'lordd.sock']
            assert (rig.home / 'lordd.sock').read_text() == 'replacement socket'
        else: assert not rig.home.exists()
    finally:
        if rig.home.is_symlink(): rig.home.unlink()
        rig.stop()
        for pid, birth in births.items():
            if birth is not None and m.process_start(pid) == birth:
                try: os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError: pass
                try: os.kill(pid, signal.SIGKILL)
                except ProcessLookupError: pass
        pool.shutdown(wait=True)


@pytest.mark.parametrize('state', ['live', 'gone', 'replaced'])
def test_codex_home_uses_the_real_location_of_a_linked_home(tmp_path, state):
    m = load('home_codex_alias')
    physical = tmp_path / 'physical'; physical.mkdir()
    m.HOME = tmp_path / 'linked'; m.HOME.symlink_to(physical, target_is_directory=True)
    identity = m.home_identity()
    saved = physical / 'codex'
    source = tmp_path / 'setup'; source.mkdir()
    if state != 'live': physical.rename(tmp_path / 'retired')
    if state == 'replaced':
        physical.mkdir(); (physical / 'keep').write_text('untouched')
    if state == 'live':
        m.codex_home_ready(saved, source, identity)
        assert saved.is_dir() and (saved / 'mcp-oauth-locks').is_symlink()
    else:
        with pytest.raises(FileNotFoundError, match='disappeared or was replaced'):
            m.codex_home_ready(saved, source, identity)
        assert not (tmp_path / 'retired/codex').exists()
        if state == 'gone': assert not physical.exists()
        else: assert sorted(p.name for p in physical.iterdir()) == ['keep']


def test_mcp_helper_with_a_missing_home_creates_nothing(tmp_path):
    user = tmp_path / 'user'; user.mkdir()
    home = user / 'missing/lord'
    reply = subprocess.run([PYTHON, str(DAEMON), '--mcp', 'a'],
        input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n', capture_output=True, text=True, timeout=5,
        env={**os.environ, 'HOME': str(user), 'LORD_HOME': str(home), 'LORD_DAEMON_SOCKET': str(tmp_path / 'absent.sock'),
             'LORD_SANDBOXED': '1', 'LORD_READONLY': '0'})
    assert reply.returncode == 0, reply.stderr
    assert {tool['name'] for tool in json.loads(reply.stdout)['result']['tools']} == {'list_agents'}
    assert list(user.iterdir()) == []


def test_gpt_vendor_does_not_recreate_its_deleted_codex_home(tests_backend, rig):
    d, m, launched = tests_backend
    source = d.user_codex_home; source.mkdir(parents=True)
    (source / 'auth.json').write_text('{}')
    a = m['Agent']({'agentId': 'a', 'family': 'gpt', 'model': 'fake', 'cwd': str(rig.home / 'work'),
                    'codexHome': str(m['HOME'] / 'codex')}, d.home_id)
    v = m['Vendor'](d, 'gpt', a)
    d.agents, d.vendors = {'a': a}, {'a': v}
    shutil.rmtree(m['HOME'])
    async def check():
        try:
            with pytest.raises(FileNotFoundError, match='disappeared or was replaced'): await v.ensure()
            assert not m['HOME'].exists()
            assert not launched
        finally: await d.close()
    asyncio.run(check())


@pytest.mark.parametrize('stepping', [False, True])
def test_dangling_home_cleanup_removes_only_its_own_link(rig, tmp_path, stepping):
    other = tmp_path / 'other'; other.symlink_to(tmp_path / 'absent', target_is_directory=True)
    try:
        test_home_loss_stops_vendor_and_jobs_without_recreating_home(rig, 'dangling', stepping)
        assert not rig.home.is_symlink()
        assert other.is_symlink()
    finally:
        if rig.home.is_symlink(): rig.home.unlink()

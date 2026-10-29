"""Round 2 regressions for process ownership, cleanup and persisted agents."""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import types
from unittest.mock import AsyncMock

import pytest

from test_lordd import PYTHON, ROOT, grok_home, load, restart_with, rig, tests_backend


@pytest.mark.parametrize('family', ['gpt', 'grok', 'gemini'])
def test_r2_lord_helper_is_never_a_job(rig, family):
    restart_with(rig, process_idle_s=.1, idle_exit_s=.3)
    rig.open(family)
    ended = rig.turn('HELPER_UP')[-1]
    pid = next(e['helper_pid'] for e in rig.audit() if 'helper_pid' in e)
    try:
        assert pid not in {j['pid'] for j in ended['record']['jobs']}
        rig.proc.wait(timeout=4)
        with pytest.raises(ProcessLookupError): os.kill(pid, 0)
    finally:
        try: os.kill(pid, signal.SIGKILL)
        except ProcessLookupError: pass


def test_r2_startup_shell_tool_and_subtree_do_not_keep_vendor_open(rig):
    rig.stop()
    config = json.loads(rig.config.read_text())
    config['gpt']['program'].append('--startup-shell-tool')
    config.update(process_idle_s=.1, idle_exit_s=.3)
    rig.config.write_text(json.dumps(config)); rig.start()
    rig.open('gpt')
    pid = next(e['startup_tool'] for e in rig.audit() if 'startup_tool' in e)
    ended = rig.turn('PING')[-1]
    try:
        assert ended['record']['jobs'] == []
        rig.proc.wait(timeout=4)
        with pytest.raises(ProcessLookupError): os.kill(pid, 0)
    finally:
        try: os.kill(pid, signal.SIGKILL)
        except ProcessLookupError: pass


@pytest.mark.parametrize('suffix', [b'', b' OTHER=value', b'changed'])
def test_r2_marker_arguments_are_not_environment(monkeypatch, suffix):
    m = load('r2_marker_suffix')
    m.SOCKET = Path('/tmp/x/lord.sock')
    plain = b'10001 /bin/sleep LORD_AGENT_ID=a LORD_DAEMON_SOCKET=/tmp/x/lord.sock'
    extended = (b'10001 /bin/sleep changed' if suffix == b'changed' else plain + suffix)
    async def spawn(*args, **kw):
        reader = asyncio.StreamReader(); reader.feed_data((extended if '-Eww' in args else plain) + b'\n'); reader.feed_eof()
        return types.SimpleNamespace(stdout=reader, returncode=0, wait=AsyncMock(return_value=0))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    markers = asyncio.run(m.process_markers())
    assert m.marked_pids(markers, 'a') == set()
    assert not markers and not markers.sockets
    assert markers.visible == ({10001} if suffix == b' OTHER=value' else set())


def test_r2_marker_suffix_preserves_socket_spaces_and_last_values(monkeypatch):
    m = load('r2_marker_values')
    m.SOCKET = Path('/tmp/x/home space/lord.sock')
    plain = b'10001 python script LORD_AGENT_ID=decoy LORD_DAEMON_SOCKET=/tmp/x/decoy.sock'
    suffix = b' LORD_AGENT_ID=wrong LORD_AGENT_ID=a LORD_DAEMON_SOCKET=/tmp/x/home space/lord.sock OTHER=discard'
    async def spawn(*args, **kw):
        reader = asyncio.StreamReader(); reader.feed_data(plain + (suffix if '-Eww' in args else b'') + b'\n'); reader.feed_eof()
        return types.SimpleNamespace(stdout=reader, returncode=0, wait=AsyncMock(return_value=0))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    markers = asyncio.run(m.process_markers())
    assert dict(markers) == {10001: 'a'} and markers.sockets == {10001: str(m.SOCKET)}
    assert m.marked_pids(markers, 'a') == {10001}


def test_r2_marker_diagnostics_hide_values():
    m = load('r2_marker_repr')
    markers = m.ProcessMarkers(); markers[10001] = 'scope-probe-unique'
    markers.sockets[10001] = '/tmp/x/diagnostic.sock'
    assert repr(markers) == 'ProcessMarkers(1 pids)'


@pytest.mark.parametrize('failure', ['table', 'markers', 'details'])
def test_r2_lookup_failures_still_signal_groups_known_and_marked(monkeypatch, failure):
    m = load('r2_kill_lookup')
    markers = m.ProcessMarkers(); markers[10003] = 'a'; markers.sockets[10003] = str(m.SOCKET)
    async def table():
        if failure == 'table': raise OSError('process table unavailable')
        return {10001: [10004]}, {}
    async def lookup():
        if failure == 'markers': raise OSError('marker lookup unavailable')
        return markers
    async def details(pids):
        if failure == 'details': raise OSError('job details unavailable')
        return []
    monkeypatch.setattr(m, 'process_table', table); monkeypatch.setattr(m, 'process_markers', lookup)
    monkeypatch.setattr(m, 'job_details', details)
    groups, signals = [], []
    monkeypatch.setattr(m.os, 'killpg', lambda pid, sig: groups.append((pid, sig)))
    monkeypatch.setattr(m, 'signal_all', lambda pids, sig: signals.append((set(pids), sig)))
    process = types.SimpleNamespace(pid=10001, returncode=None, wait=AsyncMock())
    asyncio.run(m.kill_tree(process, known={10002}, agent_id='a', on_jobs=lambda jobs: None))
    assert groups == [(10001, signal.SIGTERM), (10001, signal.SIGKILL)]
    for pids, sig in signals:
        assert {10001, 10002} <= pids
        if failure != 'markers': assert 10003 in pids
        if failure != 'table': assert 10004 in pids


@pytest.mark.parametrize('lookup', ['process_table', 'job_details'])
def test_r2_failed_ps_status_is_lookup_failure(monkeypatch, lookup):
    m = load('r2_ps_status')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(return_value=types.SimpleNamespace(
        returncode=1, communicate=AsyncMock(return_value=(b'', b'')))))
    args = ({10001},) if lookup == 'job_details' else ()
    with pytest.raises(RuntimeError, match='lookup failed'): asyncio.run(getattr(m, lookup)(*args))


@pytest.mark.parametrize('failure', ['close', 'table'])
def test_r2_crash_always_finishes_turn_when_close_fails(tests_backend, rig, failure):
    d, m, _ = tests_backend
    async def scenario():
        try:
            await d.open({'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home / 'work')})
            a = d.agents['a']; v = d.backend(a)
            await d.begin(a, {'text': 'SLOW'})
            async with asyncio.timeout(5):
                while not a.turn_id: await asyncio.sleep(.01)
            close = v.close
            table = m['process_table']
            async def failed_close(*args, **kw):
                await close(*args, **kw)
                raise OSError('cleanup unavailable')
            async def failed_table(): raise OSError('process table unavailable')
            if failure == 'close': v.close = failed_close
            else: m['process_table'] = failed_table
            os.kill(v.process.pid, signal.SIGKILL)
            try:
                await asyncio.wait_for(a.finished.wait(), 2)
                assert a.state == 'idle'
                assert json.loads(a.record_path.read_text())['turns'][-1]['ended_how'].startswith('error')
            finally:
                v.close = close
                m['process_table'] = table
        finally: await d.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('family', ['gpt', 'grok', 'gemini'])
def test_r2_first_open_writes_restorable_record_without_turns(rig, family):
    opened = rig.open(family)
    path = rig.home / 'logs/a.json'
    assert path.exists()
    saved = json.loads(path.read_text())
    assert saved['turns'] == [] and saved['vendorSession'] == opened['vendorSession']
    rig.stop(); (rig.home / 'journal.jsonl').unlink(); rig.start()
    restored = rig.request('/restore', {'agentId': 'a'})
    assert restored['found'] and restored['agent']['family'] == family
    assert rig.open(family, resume=True)['vendorSession'] == opened['vendorSession']
    assert rig.turn('PING')[-1]['record']['commands'] >= 0
    assert len(json.loads(path.read_text())['turns']) == 1


def test_r2_quit_closes_known_ids_across_session_changes(tests_backend, rig):
    d, _, _ = tests_backend
    async def scenario():
        try:
            for aid, session in [('idle', 'old'), ('active', 'old'), ('other', 'unrelated')]:
                await d.open({'agentId': aid, 'family': 'gpt', 'cwd': str(rig.home / 'work'), 'session': session})
            await d.begin(d.agents['active'], {'text': 'SLOW'})
            await d.route('POST', '/session_end', {'session': 'new', 'agentIds': ['idle', 'active'], 'cause': 'session end: quit'})
            assert d.agents['idle'].state == 'closed'
            assert d.agents['active'].state == 'running' and d.agents['active'].session_ended
            assert d.agents['other'].state == 'idle' and not d.agents['other'].session_ended
        finally: await d.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('shape', ['eyJ-', '-', 'token-'])
def test_r2_redaction_128kb_finishes_under_half_second(shape):
    code = ('import importlib.util,time; '
            's=importlib.util.spec_from_file_location("probe","lordd.py"); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); '
            f'text=({shape!r}*131072)[:131072]; start=time.perf_counter(); m.Redactor()(text); print(time.perf_counter()-start)')
    try: result = subprocess.run([PYTHON, '-c', code], cwd=ROOT, capture_output=True, text=True, timeout=2)
    except subprocess.TimeoutExpired: pytest.fail('128 KB redaction exceeded two seconds')
    assert result.returncode == 0, result.stderr
    assert float(result.stdout) < .5


def test_r2_redact_then_trim_sites_bound_input(tmp_path, monkeypatch):
    m = load('r2_trim_margin'); m.HOME = tmp_path
    sizes = []
    original = m.REDACT
    def redact(text, **kwargs): sizes.append(len(str(text))); return original(text, **kwargs)
    monkeypatch.setattr(m, 'REDACT', redact)
    async def scenario():
        a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '/tmp/x', 'model': 'fake'})
        a.state = 'running'; a.note('x' * 131072)
        a.read_paths([('read', '/' + 'x' * 131072)])
        a.row('r', 'command', 'x' * 131072)
        a.row_done('r', 'x' * 131072 + '\nTAIL')
        assert (await a.rows['r'])['output'].endswith('TAIL')
        d = object.__new__(m.Daemon); d.redact = redact; d.log('x' * 131072)
    asyncio.run(scenario())
    assert max(sizes) <= 16384, sizes


@pytest.mark.parametrize('root', ['cwd', 'dir'])
def test_r2_exec_restart_rechecks_pinned_roots(tests_backend, rig, root):
    d, _, launched = tests_backend
    work = rig.home / 'work'; extra = rig.home / 'extra'; extra.mkdir()
    outside = rig.home / 'outside'; outside.mkdir()
    async def scenario():
        try:
            await d.open({'agentId': 'a', 'family': 'gpt', 'cwd': str(work), 'dirs': [str(extra)], 'tests': True})
            a = d.agents['a']
            await d.begin(a, {'text': 'FAIL'}); await asyncio.wait_for(a.finished.wait(), 5)
            path = work if root == 'cwd' else extra
            path.rename(path.with_name(path.name + '.moved')); path.symlink_to(outside, target_is_directory=True)
            while not a.events.empty(): a.events.get_nowait()
            before = len(launched)
            with pytest.raises(ValueError, match='now points to'):
                await d.begin_locked(a, {'text': 'PING'})
            assert not any('/usr/bin/sandbox-exec' in argv for argv, _ in launched[before:])
        finally: await d.close()
    asyncio.run(scenario())


def test_r2_taskstop_after_daemon_restart_finds_marked_jobs(rig):
    rig.open('gpt'); rig.turn('BACKGROUND')
    event = next(e for e in rig.audit() if 'command_pid' in e)
    pids = [event['pid'], event['command_pid']]
    try:
        rig.proc.kill(); rig.proc.wait(); rig.log.close(); rig.start()
        closed = rig.request('/close', {'agentId': 'a', 'cause': 'TaskStop', 'idle_only': True})
        assert closed['stopped_jobs'] >= 1
        assert event['command_pid'] in {j['pid'] for j in closed['jobs']}
        for pid in pids:
            with pytest.raises(ProcessLookupError): os.kill(pid, 0)
    finally:
        for pid in pids:
            try: os.kill(pid, signal.SIGKILL)
            except ProcessLookupError: pass


@pytest.mark.parametrize('event', ['stop', 'quit'])
def test_r2_between_turn_kills_do_not_carry_to_next_turn(rig, event):
    rig.open('gpt', session='old'); rig.turn('BACKGROUND')
    pid = next(e['command_pid'] for e in rig.audit() if 'command_pid' in e)
    if event == 'stop':
        closed = rig.request('/close', {'agentId': 'a', 'cause': 'TaskStop', 'idle_only': True})
        assert closed['stopped_jobs'] == 1
    else: rig.request('/session_end', {'session': 'old'})
    rig.open('gpt', session='new')
    ended = rig.turn('PING')[-1]
    assert ended['record']['killed_jobs'] == []
    assert json.loads((rig.home / 'logs/a.json').read_text())['turns'][-1]['killed_jobs'] == []
    assert f'killing job {pid}:' in (rig.home / 'logs/a.log').read_text()


def test_r2_record_oserror_is_transient_and_retry_succeeds(tests_backend, rig, monkeypatch):
    d, m, _ = tests_backend
    async def scenario():
        try:
            await d.open({'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home / 'work')})
            a = d.agents['a']
            a.turn_record('done', {'text': 'saved'})
            await d.backend(a).close(); d.agents.clear(); d.journal.unlink()
            original = Path.read_text
            def read(path, *args, **kw):
                if path == a.record_path: raise PermissionError('temporary record access failure')
                return original(path, *args, **kw)
            with monkeypatch.context() as patch:
                patch.setattr(Path, 'read_text', read)
                with pytest.raises(RuntimeError, match='retry later'): d.restore('a')
            assert len(json.loads(a.record_path.read_text())['turns']) == 1
            assert d.restore('a')['found'] and d.agents['a'].vendorSession == a.vendorSession
        finally: await d.close()
    asyncio.run(scenario())


def test_r2_codex_filter_keeps_ownership_markers_on_start_and_resume(rig):
    home = rig.data / '.codex'; home.mkdir(parents=True, exist_ok=True)
    (home / 'config.toml').write_text('[shell_environment_policy]\ninherit="none"\ninclude_only=[]\n[shell_environment_policy.set]\nKEEP="yes"\nLORD_AGENT_ID="foreign"\nLORD_DAEMON_SOCKET="/tmp/x/foreign.sock"\n')
    for turn in range(2):
        rig.open('gpt')
        ended = rig.turn('FILTERED_BACKGROUND')[-1]
        pid = [e['command_pid'] for e in rig.audit() if 'command_pid' in e][-1]
        assert pid in {j['pid'] for j in ended['record']['jobs']}
        params = [e['params'] for e in rig.audit() if e.get('method') in ('thread/start', 'thread/resume')][-1]
        assert params['config']['shell_environment_policy']['set'] == {
            'LORD_AGENT_ID': 'a', 'LORD_DAEMON_SOCKET': str(rig.home / 'lordd.sock')}
        assert next(e['kept'] for e in rig.audit() if e.get('command_pid') == pid) == 'yes'
        rig.request('/close', {'agentId': 'a', 'idle_only': True})


def test_r2_record_keeps_all_jobs(tmp_path):
    m = load('r2_record_jobs'); m.HOME = tmp_path
    a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '/tmp/x', 'model': 'fake'})
    a.jobs = [{'pid': 10000 + i, 'command': f'worker {i}', 'age_s': i} for i in range(8)]
    a.turn_record('done', {'text': 'answer'})
    assert len(json.loads(a.record_path.read_text())['turns'][-1]['jobs']) == 8


def test_r2_startup_snapshot_resets_on_restart_and_keeps_turn_jobs(rig):
    rig.stop()
    config = json.loads(rig.config.read_text()); config['gpt']['program'].append('--startup-shell-tool')
    rig.config.write_text(json.dumps(config)); rig.start()
    rig.open('gpt')
    ended = rig.turn('SHELL_BACKGROUND')[-1]
    pid = next(e['shell_pid'] for e in rig.audit() if 'shell_pid' in e)
    assert {j['pid'] for j in ended['record']['jobs']} >= {pid}
    rig.open('gpt', nosearch=True)
    assert rig.turn('PING')[-1]['record']['jobs'] == []
    assert len([e for e in rig.audit() if 'startup_tool' in e]) == 2
    with pytest.raises(ProcessLookupError): os.kill(pid, 0)


def test_r2_explicit_tool_server_subtree_stays_excluded_when_marked():
    m = load('r2_explicit_tool')
    markers = m.ProcessMarkers(); markers.update({10002: 'a', 10003: 'a', 10004: 'a'})
    markers.sockets.update({pid: str(m.SOCKET) for pid in markers}); markers.hosts.add(10002)
    assert m.job_pids({10001: [10002], 10002: [10003]}, {10001, 10002, 10003}, {10001}, markers, 'a') == {10004}

"""Jobs behaviour."""
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import time
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from test_lordd import PYTHON, grok_home, load, restart_with, rig, tests_backend
from delegation_helpers import scope


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


@pytest.mark.parametrize('operation', ['list', 'restore', 'idle', 'vendor'])
def test_r4_restored_jobs_are_found_and_taskstop_kills_only_owned_job(tests_backend, rig, operation):
    d, m, _ = tests_backend
    async def scenario():
        owned = foreign = heartbeat = None
        try:
            await d.open({'agentId': 'restored', 'family': 'gpt', 'cwd': str(rig.home / 'work')})
            await d.backend(d.agents['restored']).close()
            d.agents.clear(); d.vendors.clear()
            for mine in (True, False):
                process = await asyncio.create_subprocess_exec(PYTHON, '-c', 'import time; time.sleep(120)',
                    start_new_session=True, env={**os.environ, 'LORD_AGENT_ID': 'restored',
                    'LORD_DAEMON_SOCKET': str(m['SOCKET']) if mine else str(rig.home / 'foreign.sock')})
                if mine: owned = process
                else: foreign = process
            if operation == 'restore': await d.route('POST', '/restore', {'agentId': 'restored'})
            else:
                d.refresh()
                if operation == 'list': await d.route('GET', '/agents', {})
                elif operation == 'vendor': await d.backend(d.agents['restored']).update_jobs()
                else:
                    d.config.update(idle_exit_s=.1, guard_interval_s=3600)
                    d.last_request = time.monotonic() - 1
                    heartbeat = asyncio.create_task(d.heartbeat())
                    async def discovered():
                        while owned.pid not in {j['pid'] for j in d.agents['restored'].jobs} and not d.stop_event.is_set():
                            if heartbeat.done(): await heartbeat; break
                            await asyncio.sleep(.01)
                    await asyncio.wait_for(discovered(), 3)
                    assert not d.stop_event.is_set(), 'idle exit ignored the restored marked job'
            a = d.agents['restored']
            assert owned.pid in {job['pid'] for job in a.jobs}
            assert foreign.pid not in {job['pid'] for job in a.jobs}
            closed = await d.route('POST', '/close', {'agentId': 'restored', 'idle_only': True, 'cause': 'TaskStop'})
            assert closed['closed'] and owned.pid in {j['pid'] for j in closed['jobs']}
            await asyncio.wait_for(owned.wait(), 3)
            assert foreign.returncode is None
            if heartbeat: await asyncio.wait_for(heartbeat, 3)
        finally:
            if heartbeat:
                heartbeat.cancel(); await asyncio.gather(heartbeat, return_exceptions=True)
            for process in (owned, foreign):
                if process and process.returncode is None: process.kill(); await process.wait()
            await d.close()
    asyncio.run(scenario())


def test_r4_startup_prune_keeps_restored_live_jobs(tests_backend, rig):
    d, m, _ = tests_backend
    async def scenario():
        process = restored = None
        try:
            await d.open({'agentId': 'orphan', 'family': 'gpt', 'cwd': str(rig.home / 'work')})
            a = d.agents['orphan']
            await d.backend(a).close()
            a.record['used'] = '2000-01-01T00:00:00+00:00'
            d.journal.write_text(json.dumps(a.record) + '\n')
            process = await asyncio.create_subprocess_exec(PYTHON, '-c', 'import time; time.sleep(120)',
                start_new_session=True, env={**os.environ, 'LORD_AGENT_ID': 'orphan', 'LORD_DAEMON_SOCKET': str(m['SOCKET'])})
            restored = m['Daemon']()
            result = await restored.prune(14)
            assert result['pruned'] == []
            assert process.pid in {j['pid'] for j in restored.agents['orphan'].jobs}
        finally:
            if process and process.returncode is None: process.kill(); await process.wait()
            if restored: await restored.close()
            await d.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('source', ['tree', 'marker'])
@pytest.mark.parametrize('failed_lookup', [False, True])
def test_r5_kill_reconfirms_pids_but_keeps_lookup_failure_fallback(scope, monkeypatch, source, failed_lookup):
    _, _, m, *_ = scope
    calls, count = [], [0]
    class Process:
        pid, returncode = 900001, None
        async def wait(self): pass
    async def table():
        count[0] += 1
        if failed_lookup and count[0] == 2: raise RuntimeError('synthetic lookup failure')
        return ({900001: [900002]} if count[0] == 1 and source == 'tree' else {}), {}
    async def markers():
        found = m.ProcessMarkers()
        if count[0] == 1 and source == 'marker': found[900002] = 'caller'; found.sockets[900002] = str(m.SOCKET)
        return found
    monkeypatch.setattr(m, 'process_table', table); monkeypatch.setattr(m, 'process_markers', markers)
    monkeypatch.setattr(m.os, 'kill', lambda pid, sig: calls.append((pid, sig)))
    monkeypatch.setattr(m.os, 'killpg', lambda *args: None)
    asyncio.run(m.kill_tree(Process() if source == 'tree' else None, agent_id='caller'))
    assert (900002, signal.SIGTERM) in calls
    assert ((900002, signal.SIGKILL) in calls) is failed_lookup


def test_r5_listing_and_finish_reuse_lookup_without_empty_vendors(tests_backend, monkeypatch):
    d, m, _ = tests_backend
    counts = [0, 0]
    async def table(): counts[0] += 1; return {}, {}
    async def markers(): counts[1] += 1; return m['ProcessMarkers']()
    monkeypatch.setitem(m, 'process_table', table); monkeypatch.setitem(m, 'process_markers', markers)
    a = m['Agent']({'agentId': 'restored', 'family': 'gpt', 'cwd': '/tmp/x', 'model': 'fake'})
    d.agents[a.agentId] = a; a.emit = lambda *args, **kwargs: None
    async def run():
        await d.route('GET', '/agents', {})
        await d.route('GET', '/agents', {})
        await d.finish(a, text='done')
        assert counts == [1, 1] and not d.vendors
        a.jobs_checked -= 5.1
        await d.route('GET', '/agents', {})
        assert counts == [2, 2] and not d.vendors
    asyncio.run(run())


def test_r5_new_command_invalidates_the_job_cache(scope):
    _, a, *_ = scope
    async def run():
        a.jobs_checked = time.monotonic()
        a.row('command', 'command', 'synthetic')
        assert a.jobs_checked == 0
    asyncio.run(run())


def test_r5_finish_does_not_reuse_a_lookup_during_an_unfinished_command(tests_backend, monkeypatch):
    d, m, _ = tests_backend
    scans = []
    async def table(): scans.append(1); return {}, {}
    async def markers(): return m['ProcessMarkers']()
    monkeypatch.setitem(m, 'process_table', table); monkeypatch.setitem(m, 'process_markers', markers)
    a = m['Agent']({'agentId': 'active', 'family': 'gpt', 'cwd': '/tmp/x', 'model': 'fake'})
    d.agents[a.agentId] = a; a.state = 'running'; a.emit = lambda *args, **kw: None
    async def run():
        a.row('command', 'command', 'synthetic')
        await d.route('GET', '/agents', {})
        await d.finish(a, text='done')
        assert len(scans) == 2
    asyncio.run(run())


def test_r5_lookup_before_command_completion_cannot_validate_the_cache(tests_backend, monkeypatch):
    d, m, _ = tests_backend
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        scans = []
        async def table():
            scans.append(1)
            if len(scans) == 1: entered.set(); await release.wait()
            return {}, {}
        async def markers(): return m['ProcessMarkers']()
        monkeypatch.setitem(m, 'process_table', table); monkeypatch.setitem(m, 'process_markers', markers)
        a = m['Agent']({'agentId': 'active', 'family': 'gpt', 'cwd': '/tmp/x', 'model': 'fake'})
        a.state = 'running'; d.agents[a.agentId] = a; a.emit = lambda *args, **kw: None
        a.row('command', 'command', 'synthetic')
        lookup = asyncio.create_task(d.refresh_jobs())
        await entered.wait(); a.row_done('command', 'finished'); release.set(); await lookup
        await d.finish(a, text='done')
        assert len(scans) == 2
    asyncio.run(run())


def test_r5_restored_jobs_keep_the_memory_guard_without_empty_vendors(tests_backend, monkeypatch):
    d, m, _ = tests_backend
    killed = []
    a = m['Agent']({'agentId': 'restored', 'family': 'gpt', 'cwd': '/tmp/x', 'model': 'fake', 'memory_gb': .000001})
    d.agents[a.agentId] = a
    async def table(): return {}, {900002: 100}
    async def markers():
        found = m['ProcessMarkers'](); found[900002] = a.agentId; found.sockets[900002] = str(m['SOCKET']); return found
    async def details(pids): return [{'pid': pid, 'command': 'synthetic', 'age_s': 1} for pid in pids]
    async def kill(vendor): killed.append(vendor.agent.agentId)
    monkeypatch.setitem(m, 'process_table', table); monkeypatch.setitem(m, 'process_markers', markers)
    monkeypatch.setitem(m, 'job_details', details); monkeypatch.setattr(m['Vendor'], 'kill', kill)
    async def run():
        await d.route('GET', '/agents', {})
        await d.guard(time.monotonic())
        assert killed == [a.agentId]
    asyncio.run(run())


def test_r5_shutdown_finds_restored_jobs_without_empty_vendor_fanout(tests_backend, monkeypatch):
    d, m, _ = tests_backend
    closed, scans = [], []
    for index in range(40):
        a = m['Agent']({'agentId': f'restored{index}', 'family': 'gpt', 'cwd': '/tmp/x', 'model': 'fake'})
        d.agents[a.agentId] = a
    async def table(): scans.append(1); return {}, {}
    async def markers():
        found = m['ProcessMarkers'](); found[900002] = 'restored0'; found.sockets[900002] = str(m['SOCKET']); return found
    async def details(pids): return [{'pid': pid, 'command': 'synthetic', 'age_s': 1} for pid in pids]
    async def close(vendor): closed.append(vendor.agent.agentId)
    async def others(): return True
    monkeypatch.setitem(m, 'process_table', table); monkeypatch.setitem(m, 'process_markers', markers)
    monkeypatch.setitem(m, 'job_details', details); monkeypatch.setitem(m, 'others_answering', others)
    monkeypatch.setattr(m['Vendor'], 'close', close)
    async def run():
        await d.route('GET', '/agents', {})
        assert not d.vendors
        await d.close()
        assert closed == ['restored0'] and len(scans) == 2
    asyncio.run(run())


def test_r5_detached_jobs_refresh_no_more_than_every_five_seconds(tests_backend, monkeypatch):
    d, m, _ = tests_backend
    counts = [0]
    a = m['Agent']({'agentId': 'restored', 'family': 'gpt', 'cwd': '/tmp/x', 'model': 'fake'})
    d.agents[a.agentId] = a
    async def table(): counts[0] += 1; return {}, {}
    async def markers():
        found = m['ProcessMarkers'](); found[900002] = a.agentId; found.sockets[900002] = str(m['SOCKET']); return found
    async def details(pids): return [{'pid': pid, 'command': 'synthetic', 'age_s': 1} for pid in pids]
    monkeypatch.setitem(m, 'process_table', table); monkeypatch.setitem(m, 'process_markers', markers); monkeypatch.setitem(m, 'job_details', details)
    d.config.update(idle_exit_s=.1, guard_interval_s=3600); d.last_request -= 1
    async def run():
        task = asyncio.create_task(d.heartbeat())
        try:
            await asyncio.sleep(.7)
            assert counts == [1] and not d.stop_event.is_set() and not d.vendors
            a.jobs_checked -= 5.1
            await asyncio.sleep(.15)
            assert counts == [2]
        finally: task.cancel(); await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize('same_start', [True, False])
def test_r6_kill_tracks_term_process_birth_without_reusing_a_pid(scope, monkeypatch, same_start):
    _, _, m, *_ = scope
    count, calls = [0], []
    class Process:
        pid, returncode = 900001, None
        async def wait(self): pass
    process = Process()
    async def table():
        count[0] += 1
        if count[0] == 2: process.returncode = 0
        return ({900001: [900002]} if count[0] == 1 else {}), {}
    async def markers(): return m.ProcessMarkers()
    def start(pid): return (100, 123) if count[0] == 1 or same_start else (100, 124)
    monkeypatch.setattr(m, 'process_table', table)
    monkeypatch.setattr(m, 'process_markers', markers)
    monkeypatch.setattr(m, 'process_start', start, raising=False)
    monkeypatch.setattr(m.os, 'kill', lambda pid, sig: calls.append((pid, sig)))
    monkeypatch.setattr(m.os, 'killpg', lambda *args: None)
    asyncio.run(m.kill_tree(process, agent_id='caller'))
    assert (900002, signal.SIGTERM) in calls
    assert ((900002, signal.SIGKILL) in calls) is same_start


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS process environment hiding')
def test_r6_term_ignoring_apple_child_dies_after_its_parent_exits(scope, tmp_path):
    _, _, m, *_ = scope
    async def run():
        marker = tmp_path / 'child'
        root = await asyncio.create_subprocess_exec('/opt/homebrew/bin/python3.12', '-c',
            "import subprocess,sys,time; p=subprocess.Popen(['/bin/sh','-c',\"trap '' TERM; exec /bin/sleep 300\"]); open(sys.argv[1],'w').write(str(p.pid)); time.sleep(300)",
            str(marker), start_new_session=True, env={'PATH': '/usr/bin:/bin'})
        child = None
        def alive(pid):
            try: os.kill(pid, 0); return True
            except ProcessLookupError: return False
        try:
            for _ in range(100):
                if marker.exists() and marker.read_text(): break
                await asyncio.sleep(.02)
            child = int(marker.read_text()); await asyncio.sleep(.1)
            await m.kill_tree(root, [child], agent_id='caller')
            for _ in range(50):
                if not alive(child): break
                await asyncio.sleep(.02)
            assert not alive(child), 'TERM-ignoring orphan survived KILL'
        finally:
            for pid in (child, root.pid):
                if pid:
                    try: os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError: pass
            await root.wait()
    asyncio.run(run())


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

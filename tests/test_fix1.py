"""Regression checks for marker ownership, lifecycle and bounded parsing."""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import types
from unittest.mock import AsyncMock

import pytest

from test_lordd import ROOT, PYTHON, DAEMON, load, rig, grok_home, tests_backend, restart_with


def agent(m, family='gpt', **record):
    return m.Agent({'agentId': 'a', 'family': family, 'cwd': '/tmp/x', 'model': 'gpt-6.1-sol', **record})


def test_fix1_detached_marked_job_is_found_and_closed(tmp_path):
    m = load('fix1_detached')
    async def scenario():
        d = types.SimpleNamespace(log=lambda text: None)
        a = agent(m); v = m.Vendor(d, 'gpt', a)
        detached = None
        try:
            env = {**m.subscription_env(), 'LORD_AGENT_ID': 'a', 'LORD_DAEMON_SOCKET': str(m.SOCKET)}
            v.process = await asyncio.create_subprocess_exec('/bin/sleep', '30', env=env, start_new_session=True)
            launcher = await asyncio.create_subprocess_exec(PYTHON, '-c', 'import subprocess,sys; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print(p.pid)', env=env, stdout=-1)
            detached = int(await launcher.stdout.readline()); await launcher.wait(); await asyncio.sleep(.1)
            measured = await v.update_jobs()
            assert detached in measured and detached in {j['pid'] for j in a.jobs}
            await v.close()
            with pytest.raises(ProcessLookupError): os.kill(detached, 0)
        finally:
            if detached:
                try: os.kill(detached, signal.SIGKILL)
                except ProcessLookupError: pass
            await v.close()
    asyncio.run(scenario())


def test_fix1_remembered_orphan_pid_is_not_adopted(monkeypatch):
    m = load('fix1_reused_pid')
    async def scenario():
        a = agent(m); v = m.Vendor(types.SimpleNamespace(), 'gpt', a)
        v.process = types.SimpleNamespace(pid=10001, returncode=None)
        v.descendants = {10002}
        async def table(): return {1: [10002]}, {10001: 1, 10002: 999}
        async def markers(*args): return {}
        async def details(pids): return [{'pid': pid, 'command': 'decoy', 'age_s': 1} for pid in pids]
        monkeypatch.setattr(m, 'process_table', table)
        monkeypatch.setattr(m, 'process_markers', markers, raising=False)
        monkeypatch.setattr(m, 'job_details', details)
        measured = await v.update_jobs()
        assert 10002 not in measured and not a.jobs
    asyncio.run(scenario())


def test_fix1_unmarked_tool_server_is_memory_only(monkeypatch):
    m = load('fix1_tool_server')
    async def scenario():
        a = agent(m); v = m.Vendor(types.SimpleNamespace(), 'gpt', a)
        v.process = types.SimpleNamespace(pid=10001, returncode=None)
        async def table(): return {10001: [10002]}, {10001: 1, 10002: 2}
        async def markers(*args):
            found = m.ProcessMarkers()
            found[10001] = 'a'
            found.sockets[10001] = str(m.SOCKET)
            found.visible.add(10002)
            return found
        async def details(pids): return [{'pid': pid, 'command': 'tool-server', 'age_s': 1} for pid in pids]
        monkeypatch.setattr(m, 'process_table', table)
        monkeypatch.setattr(m, 'process_markers', markers, raising=False)
        monkeypatch.setattr(m, 'job_details', details)
        measured = await v.update_jobs()
        assert 10002 in measured and not a.jobs
    asyncio.run(scenario())


def test_fix1_kill_uses_fresh_markers_and_ignores_old_pids(monkeypatch):
    m = load('fix1_fresh_kill')
    async def scenario():
        sent, logged = [], []
        process = types.SimpleNamespace(pid=10001, returncode=None, wait=AsyncMock())
        monkeypatch.setattr(m, 'process_table', AsyncMock(return_value=({}, {})))
        def markers(values):
            found = m.ProcessMarkers(); found.update(values)
            found.sockets.update({pid: str(m.SOCKET) for pid in values})
            return found
        monkeypatch.setattr(m, 'process_markers', AsyncMock(side_effect=[markers({10002: 'a', 10004: 'another'}), markers({10003: 'a', 10004: 'another'})]))
        monkeypatch.setattr(m, 'job_details', AsyncMock(side_effect=lambda pids: [{'pid': p, 'command': 'synthetic', 'age_s': 1} for p in pids]))
        monkeypatch.setattr(m.os, 'killpg', lambda *args: None)
        monkeypatch.setattr(m.os, 'kill', lambda pid, sig: sent.append((pid, sig)))
        await m.kill_tree(process, {10004}, agent_id='a', on_jobs=logged.extend)
        assert (10002, signal.SIGTERM) in sent and (10003, signal.SIGKILL) in sent
        assert not any(pid == 10004 for pid, sig in sent)
        assert {j['pid'] for j in logged} == {10002, 10003}
    asyncio.run(scenario())


def test_fix1_marker_parser_retains_only_pid_and_marker(monkeypatch):
    m = load('fix1_marker_parser')
    async def scenario():
        output = [b'10001 /tmp/vendor OTHER_VALUE=discard-this LORD_AGENT_ID=a LORD_DAEMON_SOCKET=/tmp/x/first socket.sock OTHER_VALUE=discard-after\n', b'10002 /tmp/worker LORD_AGENT_ID=another OTHER_VALUE=discard-too LORD_DAEMON_SOCKET=/tmp/x/second.sock\n', b'']
        plain = [b'10001 /tmp/vendor\n', b'10002 /tmp/worker\n', b'10003 /bin/sleep 300\n', b'']
        output.insert(-1, b'10003 /bin/sleep 300\n')
        peers = [types.SimpleNamespace(stdout=types.SimpleNamespace(readline=AsyncMock(side_effect=rows)), returncode=0, wait=AsyncMock()) for rows in (plain, output)]
        spawn = AsyncMock(side_effect=peers); monkeypatch.setattr(m.asyncio, 'create_subprocess_exec', spawn)
        found = await m.process_markers()
        assert found == {10001: 'a', 10002: 'another'}
        assert found.sockets == {10001: '/tmp/x/first socket.sock', 10002: '/tmp/x/second.sock'}
        assert found.visible == {10001, 10002}
        assert spawn.call_args_list[0].args == ('/bin/ps', '-ww', '-A', '-o', 'pid=,command=')
        assert spawn.call_args.args == ('/bin/ps', '-Eww', '-A', '-o', 'pid=,command=')
    asyncio.run(scenario())


def test_fix1_no_marker_listing_without_vendor_or_jobs(monkeypatch):
    m = load('fix1_no_marker_listing')
    async def scenario():
        lookup = AsyncMock(side_effect=AssertionError('unexpected environment listing'))
        monkeypatch.setattr(m, 'process_markers', lookup)
        a = agent(m); v = m.Vendor(types.SimpleNamespace(), 'gpt', a)
        v.descendants = {10002}
        assert await v.update_jobs() == set()
        await v.close()
        lookup.assert_not_called()
    asyncio.run(scenario())


@pytest.mark.parametrize('family', ['grok', 'gemini'])
def test_fix1_fake_tool_commands_inherit_marker_and_close(rig, family):
    rig.open(family)
    ended = rig.turn('BACKGROUND')[-1]
    pid = next(e['command_pid'] for e in rig.audit() if 'command_pid' in e)
    m = load('fix1_tool_marker')
    markers = asyncio.run(m.process_markers())
    assert markers.get(pid) == 'a' and markers.sockets.get(pid) == str(rig.home / 'lordd.sock')
    assert pid in {j['pid'] for j in ended['record']['jobs']}
    assert rig.request('/close', {'agentId': 'a', 'idle_only': True})['stopped_jobs'] >= 1
    with pytest.raises(ProcessLookupError): os.kill(pid, 0)


@pytest.mark.parametrize('family', ['grok', 'gemini'])
def test_fix1_memory_warning_does_not_queue_or_replace_answer(tmp_path, monkeypatch, family):
    m = load('fix1_memory_warning')
    async def scenario():
        d = object.__new__(m.Daemon); d.config = {'idle_exit_s': 600, family: {}}; d.tasks = set(); logged = []
        d.log = logged.append; d.redact = str
        a = agent(m, family, memory_gb=1); a.state = 'running'; a.turn_started = '2026-01-01T00:00:00+00:00'
        d.agents = {'a': a}; v = m.Vendor(d, family, a); d.vendors = {'a': v}
        v.process = types.SimpleNamespace(pid=10001, returncode=None)
        async def table(): return {}, {10001: int(.9 * 1048576)}
        async def markers(*args): return {10001: 'a'}
        monkeypatch.setattr(m, 'process_table', table)
        monkeypatch.setattr(m, 'process_markers', markers, raising=False)
        await d.guard(time.monotonic()); await asyncio.sleep(0)
        assert a.followup is None
        assert any('Memory:' in line for line in logged)
        assert 'Memory:' in a.log_path.read_text()
        a.emit('done', text='{"result":"ok"}', usage={})
        event = a.events.get_nowait()
        assert event['text'] == '{"result":"ok"}' and 'Memory:' in event['record']['memory_warning']
    asyncio.run(scenario())


@pytest.mark.parametrize('options', [{}, {'tests': True}, {'readonly': True}, {'net': True}, {'nosandbox': True}])
def test_fix1_dir_link_is_pinned_across_open(tests_backend, rig, options):
    d, _, _ = tests_backend
    first = rig.home / 'first'; first.mkdir()
    second = rig.home / 'second'; second.mkdir()
    link = rig.home / 'link'; link.symlink_to(first, target_is_directory=True)
    async def scenario():
        try:
            body = {'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home / 'work'), 'dirs': [str(link)], **options}
            await d.open(body)
            before = dict(d.agents['a'].record)
            link.unlink(); link.symlink_to(second, target_is_directory=True)
            with pytest.raises(ValueError, match='now points to.*not.*as when it was added'):
                await d.open(body)
            assert d.agents['a'].record == before
        finally: await d.close()
    asyncio.run(scenario())


def test_fix1_cwd_link_is_pinned_even_without_explicit_pin(rig):
    first = rig.home / 'first'; first.mkdir()
    second = rig.home / 'second'; second.mkdir()
    link = rig.home / 'link'; link.symlink_to(first, target_is_directory=True)
    rig.open('gpt', cwd=str(link))
    link.unlink(); link.symlink_to(second, target_is_directory=True)
    conn, file, status, _ = rig.connect('/open', {'agentId': 'a', 'family': 'gpt', 'cwd': str(link)})
    message = json.loads(file.read()); file.close(); conn.close()
    assert status == 400 and 'now points to' in message['error']


@pytest.mark.parametrize('record_only', [False, True])
def test_fix1_dir_pin_survives_daemon_restart(rig, record_only):
    first = rig.home / 'first'; first.mkdir()
    second = rig.home / 'second'; second.mkdir()
    link = rig.home / 'link'; link.symlink_to(first, target_is_directory=True)
    rig.open('gpt', dirs=[str(link)]); rig.turn('PING'); rig.stop()
    if record_only: (rig.home / 'journal.jsonl').unlink()
    link.unlink(); link.symlink_to(second, target_is_directory=True)
    rig.start()
    if record_only: assert rig.request('/restore', {'agentId': 'a'})['found']
    conn, file, status, _ = rig.connect('/open', {'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home), 'dirs': [str(link)]})
    response = json.loads(file.read()); file.close(); conn.close()
    assert status == 400 and 'as when it was added' in response['error']
    opened = rig.request('/open', {'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home), 'dirs': [str(second)]})
    assert {str(first), str(second)} <= set(opened['dirs'])


def test_fix1_quit_keeps_running_turn_then_closes_its_jobs(tests_backend, rig):
    d, m, _ = tests_backend
    async def scenario():
        try:
            await d.open({'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home / 'work'), 'session': 'caller'})
            a = d.agents['a']; v = d.backend(a)
            await d.begin(a, {'text': 'SLOW'})
            while not a.turn_id: await asyncio.sleep(.01)
            await d.route('POST', '/session_end', {'session': 'caller', 'cause': 'quit'})
            assert a.state == 'running' and v.process.returncode is None
            await d.route('POST', '/steer', {'agentId': 'a', 'text': 'finished'})
            await asyncio.wait_for(a.finished.wait(), 3)
            child = await asyncio.create_subprocess_exec(PYTHON, '-c', 'import time; time.sleep(30)', env={**os.environ, 'LORD_AGENT_ID': 'a', 'LORD_DAEMON_SOCKET': str(m['SOCKET'])}, start_new_session=True)
            try:
                await v.update_jobs(); assert child.pid in {j['pid'] for j in a.jobs}
                v.last = 0; await d.guard(time.monotonic())
                assert v.process is None
                await asyncio.wait_for(child.wait(), 2)
            finally:
                if child.returncode is None: child.kill(); await child.wait()
        finally: await d.close()
    asyncio.run(scenario())


def test_fix1_journal_only_resume_keeps_first_conversation(rig):
    sid = rig.open('gpt')['vendorSession']
    stream = rig.stream('/turn', {'agentId': 'a', 'text': 'SLOW'})
    next(stream); stream.close(); rig.stop()
    (rig.home / 'logs/a.json').unlink(missing_ok=True)
    rig.start()
    assert rig.request('/open', {'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home), 'resume': True})['vendorSession'] == sid
    assert rig.turn('PING')[-1]['text'] == 'PONG'


@pytest.mark.parametrize('tool', ['renice', 'taskpolicy'])
def test_fix1_priority_cannot_change_outside_process(tmp_path, tool):
    m = load('fix1_priority')
    m.HOME = tmp_path / 'lord'
    profile, params = m.tests_profile(str(tmp_path), [], {'TMPDIR': str(tmp_path / 'temp')})
    decoy = subprocess.Popen([PYTHON, '-c', 'import time; time.sleep(30)'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        before = os.getpriority(os.PRIO_PROCESS, decoy.pid)
        argv = ['/usr/bin/renice', '-n', '15', '-p'] if tool == 'renice' else ['/usr/sbin/taskpolicy', '-b', '-p']
        result = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, *params, *argv, str(decoy.pid)], capture_output=True, text=True, timeout=5)
        assert result.returncode != 0 and os.getpriority(os.PRIO_PROCESS, decoy.pid) == before
    finally: decoy.terminate(); decoy.wait(timeout=5)


@pytest.mark.parametrize('text,expected', [
    ('cat <<EOF\ncat invented\nEOF\ncat actual', ['/tmp/x/actual']),
    ('cat <<EOF\nunmatched "\nEOF\ncat actual', ['/tmp/x/actual']),
    ("/bin/zsh -lc 'cat <<EOF\ncat invented\nEOF\ncat actual'", ['/tmp/x/actual']),
    ('cd sub; cat a; cd ../other && head b', ['/tmp/x/sub/a', '/tmp/x/other/b']),
    ('cd "$MISSING"; cd sub; cat relative; cat /tmp/absolute; cd /tmp/known; cat restored', ['/tmp/absolute', '/tmp/known/restored']),
    ('rg --files folder; sed -ne "1p" file', ['/tmp/x/folder', '/tmp/x/file']),
    ('cat *.py a?b [abc] "$FILE" literal', ['/tmp/x/literal']),
])
def test_fix1_shell_reads_best_effort(text, expected):
    assert load('fix1_shell').shell_reads(text, '/tmp/x') == expected


def test_fix1_large_shell_command_is_skipped_before_shlex(monkeypatch):
    m = load('fix1_shell_limit')
    monkeypatch.setattr(m.shlex, 'split', lambda *args, **kwargs: pytest.fail('oversized command reached shlex'))
    assert m.shell_reads('cat x\n' + 'a' * 65536, '/tmp/x') == []


def test_fix1_redaction_is_bounded(tmp_path):
    code = 'import importlib.util,sys,time; s=importlib.util.spec_from_file_location("m",sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);t=time.monotonic();m.Redactor()("-"*65536);assert time.monotonic()-t<1'
    try: result = subprocess.run([PYTHON, '-c', code, str(DAEMON)], capture_output=True, text=True, timeout=2)
    except subprocess.TimeoutExpired: pytest.fail('64 KB redaction exceeded two seconds')
    assert result.returncode == 0


def test_fix1_job_command_is_cut_before_redaction(monkeypatch):
    m = load('fix1_job_redaction')
    output = b'10001 S 00:01 ' + b'-' * 65536 + b'\n'
    monkeypatch.setattr(m.asyncio, 'create_subprocess_exec', AsyncMock(return_value=types.SimpleNamespace(communicate=AsyncMock(return_value=(output, b'')))))
    sizes = []
    def redact(text): sizes.append(len(text)); return text
    monkeypatch.setattr(m, 'REDACT', redact)
    result = asyncio.run(m.job_details({10001}))
    assert max(sizes) <= 300 and len(result[0]['command']) <= 300


def test_fix1_finish_ends_when_job_lookup_fails():
    m = load('fix1_finish')
    async def scenario():
        d = object.__new__(m.Daemon); logged = []; d.log = logged.append
        a = agent(m); a.state = 'running'; a.finished.clear()
        d.backend = lambda _: types.SimpleNamespace(update_jobs=AsyncMock(side_effect=OSError('ps unavailable')))
        try: await d.finish(a, text='answer', usage={})
        except OSError: pass
        assert a.finished.is_set() and a.events.get_nowait()['text'] == 'answer'
        assert any('ps unavailable' in line for line in logged)
    asyncio.run(scenario())


def test_fix1_exactly_one_policy_continue(rig):
    rig.open('gpt')
    assert rig.turn('CYBER')[-1]['type'] == 'error'
    inputs = [e['params']['input'][0]['text'] for e in rig.audit() if e.get('method') == 'turn/start']
    assert len(inputs) == 2 and sum(text.startswith('Your last step was refused') for text in inputs) == 1


@pytest.mark.parametrize('flag,sandbox', [('readonly', 'read-only'), ('tests', 'tests')])
def test_fix1_record_restore_keeps_sandbox_flags(tests_backend, rig, flag, sandbox):
    d, m, _ = tests_backend
    async def scenario():
        new = None
        try:
            body = {'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home / 'work'), flag: True}
            await d.open(body); a = d.agents['a']
            await d.begin(a, {'text': 'PING'}); await asyncio.wait_for(a.finished.wait(), 3)
            await d.close(); d.journal.unlink()
            new = m['Daemon']()
            result = new.restore('a')
            assert result['found'] and new.agents['a'].record[flag] is True
            assert (await new.open({**body, flag: False}))['sandbox'] == sandbox
        finally:
            if new: await new.close()
            await d.close()
    asyncio.run(scenario())


def test_fix1_vendor_limit_is_256mb():
    m = load('fix1_vendor_limit')
    assert m.VENDOR_LIMIT == 256 * 1024 * 1024 and m.LIMIT == 8 * 1024 * 1024


@pytest.mark.parametrize('message', ['Authentication required', 'session token is missing', 'no session credentials', '@tests exec-server could not start', 'temporary initialize failure'])
def test_fix1_transient_attach_errors_are_not_fresh_agent_advice(tests_backend, rig, message):
    d, _, _ = tests_backend
    async def scenario():
        try:
            body = {'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home / 'work')}
            await d.open(body); a = d.agents['a']
            async def attach(_): raise RuntimeError(message)
            d.attach = attach
            with pytest.raises(RuntimeError) as error: await d.open(body)
            assert str(error.value) == message
        finally: await d.close()
    asyncio.run(scenario())


def test_fix1_dir_file_has_folder_diagnostic(rig):
    path = rig.home / 'file'; path.touch()
    conn, file, status, _ = rig.connect('/open', {'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home), 'dirs': [str(path)]})
    response = json.loads(file.read()); file.close(); conn.close()
    assert status == 400 and response['error'] == f'@dir {path} is not a folder'


@pytest.mark.parametrize('restart', ['option', 'failure'])
def test_fix1_restart_killed_jobs_are_in_record(tests_backend, rig, restart):
    d, _, _ = tests_backend
    async def scenario():
        try:
            body = {'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home / 'work')}
            await d.open(body); a = d.agents['a']
            await d.begin(a, {'text': 'BACKGROUND'}); await asyncio.wait_for(a.finished.wait(), 3)
            pid = next(e['command_pid'] for e in rig.audit() if 'command_pid' in e)
            while not a.events.empty(): a.events.get_nowait()
            if restart == 'option': await d.open({**body, 'nosearch': True})
            else:
                await d.begin(a, {'text': 'FAIL'}); await asyncio.wait_for(a.finished.wait(), 3)
                while not a.events.empty(): a.events.get_nowait()
            await d.begin(a, {'text': 'PING'}); await asyncio.wait_for(a.finished.wait(), 3)
            events = []
            while not a.events.empty(): events.append(a.events.get_nowait())
            ended = events[-1]
            assert any(j['pid'] == pid for j in ended['record']['killed_jobs'])
            assert any(j['pid'] == pid for j in json.loads(a.record_path.read_text())['turns'][-1]['killed_jobs'])
        finally: await d.close()
    asyncio.run(scenario())


def test_fix1_docs_state_pgrep_scope_and_shell_limits():
    for name in ('README.md', 'PRIVACY.md'):
        text = (ROOT / name).read_text()
        assert "root's included" in text and 'with its arguments' in text
        assert 'best effort' in text and '64 KB' in text and 'heredoc' in text

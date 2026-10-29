"""Delegation identity, restored jobs and bounded parser regressions."""
import asyncio
import json
import os
import subprocess
import time

import pytest

from test_lordd import PYTHON, ROOT, grok_home, load, rig, tests_backend
from test_round3 import scope, invoke, denied


@pytest.mark.parametrize('option', ['cwd', 'dir'])
def test_r4_missing_delegated_launch_folder_is_refused(scope, option):
    assert 'does not exist' in denied(scope, text=f'@{option} later\nwork')


@pytest.mark.parametrize('option', ['cwd', 'dir'])
@pytest.mark.parametrize('restart', [False, True])
def test_r4_delegated_folder_swap_is_refused_at_child_open(tests_backend, rig, option, restart):
    d, m, _ = tests_backend
    work, outside = rig.home / 'work', rig.home / 'outside'
    outside.mkdir()
    slot = work / 'slot'; slot.mkdir()
    async def scenario():
        current = d
        try:
            await d.open({'agentId': 'sender', 'family': 'gpt', 'cwd': str(work)})
            a = d.agents['sender']; a.state = 'running'
            data = {'subagent_type': 'lord:gpt', 'prompt': f'@{option} {slot}\nwork'}
            d.confine_call(a, 'Agent', data, m['delegation_access'](a, current=True))
            assert f'@{option} {slot}' in data['prompt']
            if restart:
                await d.close()
                current = m['Daemon']()
            slot.rmdir(); slot.symlink_to(outside, target_is_directory=True)
            body = {'agentId': 'child', 'family': 'gpt', 'cwd': str(slot if option == 'cwd' else work)}
            if option == 'dir': body['dirs'] = [str(slot)]
            with pytest.raises(ValueError, match=f'@{option} .*changed after it was checked; launch again'):
                await current.open(body)
            assert 'child' not in current.agents
        finally: await current.close()
    asyncio.run(scenario())


def test_r4_already_running_daemon_reads_new_delegated_pins(tests_backend, rig):
    d, m, _ = tests_backend
    slot = rig.home / 'work/slot'; slot.mkdir()
    outside = rig.home / 'outside'; outside.mkdir()
    async def scenario():
        peer = None
        try:
            await d.open({'agentId': 'sender', 'family': 'gpt', 'cwd': str(rig.home / 'work')})
            peer = m['Daemon']()
            a = d.agents['sender']
            d.confine_call(a, 'Agent', {'subagent_type': 'lord:gpt', 'prompt': f'@dir {slot}\nwork'}, m['delegation_access'](a))
            slot.rmdir(); slot.symlink_to(outside, target_is_directory=True)
            with pytest.raises(ValueError, match='changed after it was checked'):
                await peer.open({'agentId': 'child', 'family': 'gpt', 'cwd': str(rig.home / 'work'), 'dirs': [str(slot)]})
        finally:
            if peer: await peer.close()
            await d.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('text', ['work', '@tests\nwork', '@nosearch\nwork'])
def test_r4_tests_launch_inherits_tests(scope, text):
    d, a, m, *_ = scope; a.record['tests'] = True
    _, event = invoke(d, a, text=text)
    assert event and m.parse_options(event['input']['prompt']).get('tests') is True


@pytest.mark.parametrize('line', ['@readonly', '@nosandbox', '@net', '@tools'])
def test_r4_tests_launch_refuses_incompatible_options(scope, line):
    d, a, *_ = scope; a.record['tests'] = True
    assert line in denied(scope, text=line + '\nwork')


@pytest.mark.parametrize('readonly', [False, True])
def test_r4_tests_sender_cannot_message_plain_or_readonly_target(scope, readonly):
    d, a, m, *_ = scope
    d.agents['peer'] = m.Agent({**a.record, 'agentId': 'peer', 'readonly': readonly})
    a.record['tests'] = True
    assert 'peer' in denied(scope, tool='send_message', target='peer')


def test_r4_tests_sender_can_message_tests_target(scope):
    d, a, m, *_ = scope; a.record['tests'] = True
    d.agents['peer'] = m.Agent({**a.record, 'agentId': 'peer'})
    assert invoke(d, a, tool='send_message', target='peer')[1]


@pytest.mark.parametrize('side', ['python', 'plugin'])
@pytest.mark.parametrize('shape', ['openers', 'fields'])
def test_r4_one_megabyte_of_notification_openers_parses_under_half_second(side, shape):
    text = '<task-notification>' * 60000 if shape == 'openers' else '<task-notification>' + '<result>' * 140000 + '</task-notification>'
    if side == 'python':
        command = [PYTHON, '-c', 'import lordd,time,sys; text=sys.stdin.read(); start=time.perf_counter(); lordd.parse_options(text); print(time.perf_counter()-start)']
    else:
        source = (ROOT / 'hooks/register.tsx').read_text()
        js = source[source.index('function task('):source.index('function testsError(')]
        for old, new in [('function task(text: string): Task', 'function task(text)'), ('const result: Task', 'const result'), ('new Set<string>()', 'new Set()'), ('(name: string)', '(name)')]:
            js = js.replace(old, new)
        command = ['/opt/homebrew/bin/node', '-e', js + '\nconst text=require("fs").readFileSync(0,"utf8"); const start=performance.now(); task(text); console.log((performance.now()-start)/1000)']
    try: result = subprocess.run(command, input=text, cwd=ROOT, capture_output=True, text=True, timeout=2)
    except subprocess.TimeoutExpired: pytest.fail(f'{side} parser exceeded two seconds on 1 MB of unmatched openers')
    assert result.returncode == 0, result.stderr
    elapsed = float(result.stdout)
    assert elapsed < .5, f'{side} parser took {elapsed:.3f} s'


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


@pytest.mark.parametrize('site', ['row', 'stderr'])
def test_r4_cut_drops_long_secret_word_before_redacting(scope, site):
    d, a, m, *_ = scope
    text = 'Bearer ' + 'Q' * 14000 + '\nvisible result'
    async def scenario():
        if site == 'row':
            a.rows['row'] = asyncio.get_running_loop().create_future()
            a.row_done('row', text)
            return (await a.rows['row'])['output']
        vendor = m.Vendor(d, 'gpt', a)
        reader = asyncio.StreamReader(); reader.feed_data((text + '\n').encode()); reader.feed_eof()
        process = type('Process', (), {'stderr': reader})()
        vendor.exec_process = process
        await vendor.stderr(process)
        return vendor.exec_error
    shown = asyncio.run(scenario())
    assert 'Q' * 20 not in shown and 'visible result' in shown


@pytest.mark.parametrize('tool', ['spawn_agent', 'send_message'])
def test_r4_readonly_refusal_is_returned_once(scope, tool):
    d, a, m, *_ = scope; a.record['readonly'] = True
    result, event = invoke(d, a, tool=tool)
    assert event is None and result['isError'] is True
    assert a.log_path.read_text().count('read-only agent cannot') == 1
    assert (m.HOME / 'lordd.log').read_text().count('read-only agent cannot') == 1


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

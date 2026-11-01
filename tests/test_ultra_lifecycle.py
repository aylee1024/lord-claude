"""Helper activity, live thread configuration, and permission changes."""
import asyncio
import json
import os
import time

import pytest
from test_lordd import grok_home, load, restart_with, rig, tests_backend


def threads(rig):
    return [e for e in rig.audit() if e.get('method') in ('thread/start', 'thread/resume')]


def launches(rig):
    return len([e for e in rig.audit() if e.get('launch')])


async def delivered_turn(d, a, text, **options):
    await d.begin(a, {'text': text, **options})
    await asyncio.wait_for(a.finished.wait(), 5)
    events = []
    while not a.events.empty(): events.append(a.events.get_nowait())
    assert events[-1]['type'] == 'done'
    return events[-1]


@pytest.mark.parametrize('activity', ['started', 'interacted', 'interrupted', 'completed', 'spawnAgent',
                                    'sendInput', 'resumeAgent', 'followupTask', 'sendMessage',
                                    'step-started', 'step-completed', 'tokens'])
def test_reused_helper_counts_only_when_active_in_this_turn(activity):
    m = load('helper_activity')
    m.HOME.mkdir(parents=True, exist_ok=True)
    a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '/work', 'model': 'gpt-6.1-sol', 'effort': 'ultra'})
    a.record_path.parent.mkdir(parents=True, exist_ok=True)
    a.state, a.vendorSession = 'running', 'parent'
    m.helper_seen(a, {'type': 'subAgentActivity', 'kind': 'started', 'agentThreadId': 'child',
                      'agentPath': '/root/check', 'model': 'gpt-6-luna', 'reasoningEffort': 'low'})
    a.turn_record('done', {'text': 'one'})
    a.turn_helpers.clear()
    if activity in ('started', 'interacted', 'interrupted', 'completed'):
        m.helper_seen(a, {'type': 'subAgentActivity', 'kind': activity, 'agentThreadId': 'child', 'model': None})
    elif activity in ('step-started', 'step-completed', 'tokens'):
        v = object.__new__(m.Vendor)
        method = 'thread/tokenUsage/updated' if activity == 'tokens' else 'item/' + activity.split('-')[1]
        v.helper_event(a, a.helpers['child'], method, {'item': {'type': 'commandExecution', 'command': 'cat notes.md'}})
    else:
        m.helper_seen(a, {'type': 'collabAgentToolCall', 'tool': activity, 'receiverThreadIds': ['child'], 'model': None})
    second = a.turn_record('done', {'text': 'two'})
    assert second['helpers'] == ['gpt-6-luna'] and 'helper_note' not in second
    saved = json.loads(a.record_path.read_text())['turns']
    assert saved[-1]['helpers'] == saved[0]['helpers'] == ['child']
    a.turn_helpers.clear()
    assert a.turn_record('done', {'text': 'three'})['helper_note'] == 'ultra: no helpers ran'


def test_restored_helper_interaction_uses_its_saved_model():
    m = load('restored_helper_activity')
    m.HOME.mkdir(parents=True, exist_ok=True)
    a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '/work', 'model': 'gpt-6.1-sol', 'effort': 'ultra'})
    a.record_path.parent.mkdir(parents=True, exist_ok=True)
    a.state = 'running'
    m.helper_seen(a, {'type': 'subAgentActivity', 'agentThreadId': 'child', 'model': 'gpt-6-luna'})
    a.turn_record('done', {'text': 'one'})
    restored = m.Agent(dict(a.record)); restored.state = 'running'
    m.helper_seen(restored, {'type': 'subAgentActivity', 'kind': 'interacted', 'agentThreadId': 'child', 'model': None})
    assert restored.turn_record('done', {'text': 'two'})['helpers'] == ['gpt-6-luna']


@pytest.mark.parametrize('first,second,earlier_status', [
    ('HELPER_WAIT', 'PING', '1 helper (gpt-6-luna)'),
    ('WAIT1 first', 'HELPER', 'ultra: no helpers ran'),
])
def test_joined_ultra_answers_keep_the_earlier_turns_helper_status(rig, first, second, earlier_status):
    rig.open('gpt', effort='ultra')
    stream = rig.stream('/turn', {'agentId': 'a', 'text': first})
    try:
        assert next(stream)['type'] == 'activity'
        assert rig.request('/queue', {'agentId': 'a', 'text': second})['queued']
        (rig.data / 'helper-finish').write_text('ready')
        done = list(stream)[-1]
        assert done['type'] == 'done'
        earlier, later = done['text'].split('Reply to the new message:', 1)
        assert earlier_status in earlier and later.strip() == ('PONG' if second == 'PING' else 'HELPER')
        saved = json.loads((rig.home / 'logs/a.json').read_text())['turns']
        assert len(saved) == 2 and bool(saved[0]['helpers']) != bool(saved[1]['helpers'])
    finally:
        stream.close()


@pytest.mark.parametrize('failure', ['save', 'process_lookup', 'rpc_reply'])
@pytest.mark.parametrize('reopen', [False, True])
def test_failed_attach_after_accepted_resume_restarts_at_rolled_back_effort(tests_backend, rig, monkeypatch, failure, reopen):
    d, m, _ = tests_backend
    async def scenario():
        try:
            opened = await d.open({'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home / 'work'), 'readonly': True, 'effort': 'max'})
            a = d.agents['a']; vendor = d.backend(a)
            original_rpc, original_save, original_lookup = vendor.rpc, d.save, m['process_table']
            armed = used = False
            def fail_once():
                nonlocal used
                if armed and not used:
                    used = True
                    raise RuntimeError('synthetic attach failure after accepted resume')
            async def rpc(method, params, **kw):
                nonlocal armed
                result = await original_rpc(method, params, **kw)
                if method == 'thread/resume':
                    armed = True
                    if failure == 'rpc_reply': fail_once()
                return result
            def save(agent):
                if failure == 'save': fail_once()
                return original_save(agent)
            async def lookup(*args, **kwargs):
                if failure == 'process_lookup': fail_once()
                return await original_lookup(*args, **kwargs)
            monkeypatch.setattr(vendor, 'rpc', rpc)
            monkeypatch.setattr(d, 'save', save)
            monkeypatch.setitem(m, 'process_table', lookup)
            with pytest.raises(RuntimeError, match='after accepted resume'):
                await d.begin_starting(a, {'text': 'PING', 'effort': 'ultra'})
            assert used and a.effort == 'max'
            before = launches(rig)
            if reopen:
                await d.open({'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home / 'work')})
            await d.begin(a, {'text': 'PING', 'effort': 'max'})
            await asyncio.wait_for(a.finished.wait(), 5)
            assert launches(rig) == before + 1
            resumed = threads(rig)[-1]
            assert resumed['method'] == 'thread/resume' and resumed['params']['threadId'] == opened['vendorSession']
            assert resumed['params']['config']['agents']['enabled'] is False
            assert a.codex_helpers is False
        finally: await d.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('initial,effort,model,effective', [('max', 'ultra', 'gpt-6.1-sol', 'ultra'),
    ('ultra', 'xhigh', 'gpt-6.1-sol', 'xhigh'), ('ultra', 'ultra', 'gpt-6-luna', 'high')])
@pytest.mark.parametrize('readonly', [False, True])
def test_idle_closed_open_and_turn_start_codex_only_once(rig, initial, effort, model, effective, readonly):
    restart_with(rig, process_idle_s=.5)
    opened = rig.open('gpt', readonly=readonly, effort=initial)
    assert rig.turn('persistent-word')[-1]['type'] == 'done'
    pid = next(e['pid'] for e in rig.audit() if e.get('launch'))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try: os.kill(pid, 0)
        except ProcessLookupError: break
        time.sleep(.05)
    else: pytest.fail('idle vendor did not close')
    before = launches(rig)
    rig.open('gpt', readonly=readonly, effort=effort, model=model, resume=True)
    done = rig.turn('REMEMBER', effort=effort, model=model)[-1]
    assert done['text'] == 'persistent-word' and done['record']['effort'] == effective
    assert launches(rig) == before + 1
    assert threads(rig)[-1]['method'] == 'thread/resume'
    assert threads(rig)[-1]['params']['threadId'] == opened['vendorSession']
    config = threads(rig)[-1]['params']['config']
    if readonly or effective == 'ultra':
        assert config['agents']['enabled'] is (effective == 'ultra')
    else:
        assert 'agents' not in config and 'features' not in config


@pytest.mark.parametrize('options', [{}, {'tools': True}, {'net': True}, {'nosandbox': True}, {'tests': True}])
def test_writable_ultra_config_and_effort_changes_preserve_live_jobs(tests_backend, rig, options):
    d, _, _ = tests_backend
    async def scenario():
        try:
            opened = await d.open({'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home / 'work'), 'effort': 'ultra', **options})
            config = threads(rig)[-1]['params']['config']
            assert config.get('agents') == {'enabled': True}
            assert config.get('features') == {'multi_agent': True, 'multi_agent_v2': True}
            a = d.agents['a']; vendor = d.backend(a); pid = vendor.process.pid
            await delivered_turn(d, a, 'BACKGROUND')
            job = next(e['command_pid'] for e in rig.audit() if 'command_pid' in e)
            before = launches(rig)
            await delivered_turn(d, a, 'PING', effort='max')
            assert vendor.process.pid == pid and launches(rig) == before
            os.kill(job, 0)
            await vendor.close(); a.generation = 0
            await d.attach(a)
            resumed = threads(rig)[-1]
            assert resumed['method'] == 'thread/resume' and resumed['params']['threadId'] == opened['vendorSession']
            assert 'features' not in resumed['params']['config'] and 'agents' not in resumed['params']['config']
        finally: await d.close()
    asyncio.run(scenario())


def test_added_dir_restarts_with_new_permissions_and_records_killed_jobs(tests_backend, rig):
    d, _, _ = tests_backend
    extra = rig.home / 'extra'; extra.mkdir()
    async def scenario():
        try:
            body = {'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home / 'work')}
            opened = await d.open(body)
            a = d.agents['a']; vendor = d.backend(a); pid = vendor.process.pid
            await delivered_turn(d, a, 'BACKGROUND')
            job = next(e['command_pid'] for e in rig.audit() if 'command_pid' in e)
            before = launches(rig)
            await d.open({**body, 'dirs': [str(extra)]})
            assert launches(rig) == before + 1 and vendor.process.pid != pid
            resumed = threads(rig)[-1]
            assert resumed['method'] == 'thread/resume' and resumed['params']['threadId'] == opened['vendorSession']
            config = resumed['params']['config']; fs = config['permissions'][config['default_permissions']]['filesystem']
            assert fs[str(extra.resolve())] == 'write'
            await delivered_turn(d, a, 'PING')
            saved = json.loads(a.record_path.read_text())['turns'][-1]
            assert job in {entry['pid'] for entry in saved['killed_jobs']}
            with pytest.raises(ProcessLookupError): os.kill(job, 0)
            before = launches(rig)
            await d.open({**body, 'dirs': [str(extra)]})
            assert launches(rig) == before
        finally: await d.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('option', ['readonly', 'net', 'nosearch', 'tools', 'nosandbox', 'tests', 'nice', 'memory_gb', 'hang_s', 'wall_s'])
def test_gpt_message_option_restart_audit(tests_backend, rig, option):
    d, _, _ = tests_backend
    async def scenario():
        try:
            body = {'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home / 'work')}
            opened = await d.open(body)
            values = {'nice': 19, 'memory_gb': 32, 'hang_s': 0, 'wall_s': 0}
            value = values.get(option, True)
            before = launches(rig)
            await d.open({**body, option: value})
            restart = option not in ('memory_gb', 'hang_s', 'wall_s')
            assert launches(rig) == before + int(restart)
            if restart:
                assert threads(rig)[-1]['method'] == 'thread/resume'
                assert threads(rig)[-1]['params']['threadId'] == opened['vendorSession']
            before = launches(rig)
            await d.open({**body, option: value})
            assert launches(rig) == before
        finally: await d.close()
    asyncio.run(scenario())


def test_grok_permission_change_already_restarts_its_process(rig):
    opened = rig.open('grok')
    rig.turn('persistent-word')
    before = launches(rig)
    rig.open('grok', readonly=True)
    assert launches(rig) == before + 1
    loaded = [e for e in rig.audit() if e.get('method') == 'session/load'][-1]
    assert loaded['params']['sessionId'] == opened['vendorSession']
    profiles = rig.sandbox_audit()
    grant = '(allow file-write* (subpath (param "AGENT")))'
    assert grant in profiles[0]['profile'] and grant not in profiles[-1]['profile']
    assert rig.turn('REMEMBER')[-1]['text'] == 'persistent-word'

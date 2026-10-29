"""Control-key confinement, launch pins, cleanup ownership and bounded lookups."""
import asyncio
import json
import signal
import threading
import time

import pytest

from test_lordd import grok_home, rig, tests_backend
from test_round3 import scope, invoke, denied


def test_r5_nosandbox_retains_legacy_policy_on_start_resume_and_turn(rig):
    rig.open('gpt', nosandbox=True)
    rig.turn('PING')
    rig.turn('CRASH')
    rig.turn('PING')
    threads = [e['params'] for e in rig.audit() if e.get('method') in ('thread/start', 'thread/resume')]
    assert {e.get('method') for e in rig.audit()} >= {'thread/start', 'thread/resume'}
    for params in threads:
        assert params.get('sandbox') == 'danger-full-access'
        assert 'default_permissions' not in params['config'] and 'permissions' not in params['config']
    turns = [e['params'] for e in rig.audit() if e.get('method') == 'turn/start']
    assert turns and all(p.get('sandboxPolicy') == {'type': 'dangerFullAccess'} for p in turns)


@pytest.mark.parametrize('options', [{}, {'net': True}, {'readonly': True}])
def test_r5_gpt_thread_keeps_key_denial_on_start_and_resume(rig, options):
    rig.open('gpt', **options)
    rig.turn('PING')
    rig.turn('CRASH')
    rig.turn('PING')
    threads = [e['params'] for e in rig.audit() if e.get('method') in ('thread/start', 'thread/resume')]
    for params in threads:
        config = params['config']
        profile = config['permissions'][config['default_permissions']]
        assert profile['filesystem'][str((rig.home / 'control.header').resolve())] == 'deny'
        assert profile['filesystem']['/**/control.header'] == 'deny'
        assert 'sandbox' not in params
        assert profile['network']['enabled'] is bool(options.get('net') or options.get('nosandbox'))
    assert all('sandboxPolicy' not in e['params'] for e in rig.audit() if e.get('method') == 'turn/start')


@pytest.mark.parametrize('family,option', [('gpt', 'cwd'), ('grok', 'cwd'), ('gemini', 'cwd'), ('gpt', 'dir')])
def test_r5_vendor_restart_rechecks_all_folder_pins(tests_backend, rig, family, option):
    d, m, _ = tests_backend
    slot, outside = rig.home / 'work/slot', rig.home / 'outside'
    slot.mkdir(); outside.mkdir()
    async def run():
        try:
            body = {'agentId': 'child', 'family': family, 'cwd': str(slot if option == 'cwd' else rig.home / 'work')}
            if option == 'dir': body['dirs'] = [str(slot)]
            await d.open(body)
            a = d.agents['child']; await d.backend(a).close(); a.generation = 0
            slot.rmdir(); slot.symlink_to(outside, target_is_directory=True)
            with pytest.raises(ValueError, match=f'@{option} .*now points to'):
                await d.begin(a, {'text': 'PING'})
        finally: await d.close()
    asyncio.run(run())


def test_r5_grok_queued_followup_refuses_swapped_folder(rig):
    slot, outside = rig.home / 'work/slot', rig.home / 'outside'
    slot.mkdir(parents=True); outside.mkdir()
    rig.request('/open', {'agentId': 'child', 'family': 'grok', 'cwd': str(slot), 'wall_s': 1})
    events = []
    reader = threading.Thread(target=lambda: events.extend(rig.stream('/turn', {'agentId': 'child', 'text': 'SLOW'})))
    reader.start()
    time.sleep(.3)
    assert rig.request('/queue', {'agentId': 'child', 'text': 'PING'})['queued']
    slot.rmdir(); slot.symlink_to(outside, target_is_directory=True)
    reader.join(15)
    assert not reader.is_alive()
    assert 'now points to' in events[-1].get('message', '')
    assert len(rig.sandbox_audit()) == 1


@pytest.mark.parametrize('tool', ['spawn_agent', 'send_message'])
@pytest.mark.parametrize('shape', ['lines', 'bytes', 'utf8', 'envelope', 'padding', 'tail_padding', 'crlf'])
def test_r5_delegated_option_limit_precedes_any_path_resolution(scope, monkeypatch, tool, shape):
    d, a, m, *_ = scope
    text = '@dir child\n' * 65 + 'work'
    if shape == 'bytes': text = '@dir ' + 'x' * 16384 + '\nwork'
    if shape == 'utf8': text = '@dir ' + '\u754c' * 6000 + '\nwork'
    if shape == 'padding': text = ' ' * 16384 + '@dir child\nwork'
    if shape == 'tail_padding': text = '@dir child' + ' ' * 16384
    if shape == 'crlf': text = '@model ' + 'x' * 16376 + '\r\nwork'
    if shape == 'envelope': text = 'The coordinator sent a message while you were working:\n' + text + '\n\nAddress this before completing your current task.'
    def resolution(*args): pytest.fail('path resolution ran before the option bound')
    monkeypatch.setattr(m, 'canon', resolution)
    assert 'too many option lines' in denied(scope, tool=tool, text=text)


def test_r5_exactly_64_delegated_options_are_allowed(scope):
    d, a, *_ = scope
    assert invoke(d, a, text='@dir child\n' * 64 + 'work')[1]


@pytest.mark.parametrize('ending', ['\nwork', ''])
def test_r5_exactly_16kb_of_options_is_allowed(scope, ending):
    d, a, *_ = scope
    assert invoke(d, a, text='@model ' + 'x' * (16376 if ending else 16377) + ending)[1]


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


PREFIXES = ['Bearer ', 'Authorization: Bearer ', 'api_token: ', '"access_token": "',
            'client_secret=', 'sk-', 'eyJ', *['gh' + c + '_' for c in 'pousr'], 'github_pat_', 'AKIA', 'AIza', 'xoxb-', 'xai-']


@pytest.mark.parametrize('site', ['row', 'stderr'])
@pytest.mark.parametrize('prefix', PREFIXES)
def test_r5_every_credential_prefix_cut_is_redacted(scope, monkeypatch, site, prefix):
    d, a, m, *_ = scope
    limit = m.ROW_OUTPUT
    secret = 'Q' * (limit - 1200)
    if prefix == 'AKIA': secret = 'Q' * 16
    if prefix == 'eyJ':
        payload = min(8192, limit - 1300)
        secret = 'Q' * 30 + '.' + 'Q' * payload + '.' + 'Q' * (limit - 1232 - payload)
    line = prefix + secret
    original, redacted = m.REDACT, []
    assert 'Q' * 16 not in original(line)
    def redact(value, **kwargs):
        result = original(value, **kwargs); redacted.append(result[0] if isinstance(result, tuple) else result); return result
    monkeypatch.setattr(m, 'REDACT', redact); d.redact = redact; d.log = lambda text: None
    async def run():
        for case in range(2 * len(prefix)):
            nearby, offset = divmod(case, len(prefix))
            redacted.clear()
            trailer = ' ' * (limit - (len(line) - offset))
            full = ('earlier output\n' if nearby else 'earlier output ') * 30 + line + trailer
            assert full[-limit:].startswith(line[offset:])
            if site == 'row':
                a.rows['row'] = asyncio.get_running_loop().create_future(); a.row_done('row', full)
                shown = (await a.rows['row'])['output']
            else:
                vendor = m.Vendor(d, 'gpt', a)
                reader = asyncio.StreamReader(); reader.feed_data(full.encode()); reader.feed_eof()
                process = type('Process', (), {'stderr': reader})(); vendor.exec_process = process
                await vendor.stderr(process); shown = vendor.exec_error
            assert all('Q' * 16 not in value for value in [shown, *redacted]), f'{site}: prefix {prefix!r}, offset {offset}'
    asyncio.run(run())


def test_r5_cut_keeps_redaction_and_discards_only_the_partial_run(scope):
    _, _, m, *_ = scope
    assert m.redact_tail('old\nBearer decoy\nend', 18, m.REDACT).endswith('[REDACTED]\nend')
    assert m.redact_tail('old\n' + 'x' * 300 + ' Bearer decoy\nend', 18, m.REDACT).endswith('[REDACTED]\nend')


@pytest.mark.parametrize('restart', [False, True])
def test_r5_launch_pin_is_scoped_consumed_and_expires(tests_backend, rig, restart):
    d, m, _ = tests_backend
    slot, outside = rig.home / 'work/slot', rig.home / 'outside'
    slot.mkdir(); outside.mkdir()
    async def run():
        current = d
        try:
            await d.open({'agentId': 'sender', 'family': 'gpt', 'cwd': str(rig.home / 'work')})
            a = d.agents['sender']
            launch = d.confine_call(a, 'Agent', {'subagent_type': 'lord:gpt', 'prompt': f'@dir {slot}\nwork'}, m['delegation_access'](a))
            assert isinstance(launch, str)
            if restart: await d.close(); current = m['Daemon']()
            slot.rmdir(); slot.symlink_to(outside, target_is_directory=True)
            await current.open({'agentId': 'user', 'family': 'gpt', 'cwd': str(slot)})
            body = {'agentId': 'child', 'family': 'gpt', 'cwd': str(rig.home / 'work'), 'dirs': [str(slot)], 'launch': launch}
            with pytest.raises(ValueError, match='changed after it was checked; launch again'): await current.open(body)
            with pytest.raises(ValueError, match='launch again'): await current.open(body)
            await current.open({**body, 'agentId': 'unrelated', 'launch': None})
            expired = d.confine_call(a, 'Agent', {'subagent_type': 'lord:gpt', 'prompt': 'work'}, m['delegation_access'](a))
            path = m['HOME'] / 'launches' / f'{expired}.json'
            pending = json.loads(path.read_text()); pending['expires'] = time.time() - 1
            path.write_text(json.dumps(pending))
            with pytest.raises(ValueError, match='expired.*launch again'):
                await current.open({'agentId': 'late', 'family': 'gpt', 'cwd': str(rig.home / 'work'), 'launch': expired})
        finally: await current.close()
    asyncio.run(run())


def test_r5_agent_own_pins_take_over_after_first_open(tests_backend, rig):
    d, m, _ = tests_backend
    async def run():
        try:
            await d.open({'agentId': 'sender', 'family': 'gpt', 'cwd': str(rig.home / 'work')})
            a = d.agents['sender']
            launch = d.confine_call(a, 'Agent', {'subagent_type': 'lord:gpt', 'prompt': 'work'}, m['delegation_access'](a))
            body = {'agentId': 'child', 'family': 'gpt', 'cwd': a.cwd}
            await d.open({**body, 'launch': launch})
            assert not (m['HOME'] / 'launches' / f'{launch}.json').exists()
            await d.open(body)
        finally: await d.close()
    asyncio.run(run())


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

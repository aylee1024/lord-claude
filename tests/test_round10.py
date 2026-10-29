"""Bounded interrupt writes, applicable folder messages and atomic manifests."""
import asyncio
import json
from pathlib import Path
import time

import pytest

from test_lordd import PYTHON, rig, tests_backend, grok_home
from test_round3 import scope


@pytest.mark.parametrize('family,blocked', [('gpt', 'drain'), ('grok', 'lock')])
def test_r10_full_vendor_pipe_cannot_block_taskstop(scope, family, blocked):
    d, a, m, work, *_ = scope
    d.vendors, d.stopping = {}, False
    logs = []; d.log = logs.append
    a.family = a.record['family'] = family
    a.vendorSession, a.turn_id, a.dispatched = 'synthetic-session', 'synthetic-turn', True
    a.finished.clear()
    async def run():
        v = d.backend(a); prior = stop = None
        process = await asyncio.create_subprocess_exec(PYTHON, '-I', '-S', '-B', '-c', 'import time; time.sleep(90)',
            cwd=work, env={'HOME': str(work), 'PATH': '/usr/bin:/bin'}, start_new_session=True, stdin=-1, stdout=-3, stderr=-3)
        v.process = process
        try:
            process.stdin.write(b'x' * 1048576)
            assert process.stdin.transport.get_write_buffer_size() > process.stdin.transport.get_write_buffer_limits()[1]
            if blocked == 'lock':
                prior = asyncio.create_task(v.send({'method': 'synthetic-prior-send'}))
                await asyncio.sleep(0)
                assert v.write_lock.locked() and not prior.done()
            started = time.monotonic()
            stop = asyncio.create_task(d.route('POST', '/close', {'agentId': a.agentId, 'cause': 'TaskStop'}))
            done, _ = await asyncio.wait({stop}, timeout=20)
            assert stop in done, 'TaskStop stayed blocked by the full vendor pipe'
            assert await stop == {'ok': True}
            assert 14 <= time.monotonic() - started < 20
            assert a.state == 'closed' and a.finished.is_set() and process.returncode is not None
            assert any('interrupt' in str(line) for line in logs)
            assert not v.pending and not v.write_lock.locked()
            assert json.loads(a.record_path.read_text())['turns'][-1]['ended_how'] == 'interrupted'
        finally:
            if stop and not stop.done(): stop.cancel()
            if stop: await asyncio.gather(stop, return_exceptions=True)
            await v.close()
            if prior: await asyncio.gather(prior, return_exceptions=True)
            if process.returncode is None: process.kill()
            await process.wait()
    asyncio.run(run())


@pytest.mark.parametrize('family', ['gpt', 'grok'])
def test_r10_interrupt_send_error_reaches_fallback(scope, family):
    d, a, m, *_ = scope
    d.vendors = {}; logs, closed = [], []
    d.log = logs.append
    a.family = a.record['family'] = family
    a.turn_id, a.dispatched = 'synthetic-turn', True
    a.finished.clear()
    async def failed(*args, **kwargs): raise BrokenPipeError('synthetic closed stdin')
    async def close(): closed.append(True)
    async def run():
        v = d.backend(a); v.rpc = v.send = failed; v.close = close
        await d.interrupt(a)
        assert a.state == 'idle' and a.finished.is_set() and closed
        assert logs and 'interrupt' in str(logs[0])
    asyncio.run(run())


@pytest.mark.parametrize('family,option', [('gpt', 'cwd'), ('grok', 'cwd'), ('grok', 'dir')])
def test_r10_impossible_folder_message_is_refused_before_ticket(tests_backend, rig, family, option):
    d, m, _ = tests_backend
    work = rig.home / 'work'; other = work / 'other'; other.mkdir()
    body = {'agentId': 'receiver', 'family': family, 'cwd': str(work)}
    async def run():
        try:
            await d.open(body); a = d.agents['receiver']
            await d.begin(a, {'text': 'PING'}); await asyncio.wait_for(a.finished.wait(), 5)
            while not a.events.empty(): a.events.get_nowait()
            message = f'@{option} {other}\nnext item'
            for _ in range(65):
                with pytest.raises(ValueError, match='fixed at the first turn' if option == 'cwd' else 'not available for grok'):
                    d.confine_call(a, 'SendMessage', {'to': a.agentId, 'message': message}, m['delegation_access'](a))
            assert d.message_tickets(a) == []
            assert not list((m['HOME'] / 'launches').glob('*.json'))
            await d.open(body)
            valid = f'@dir {other}' if family == 'gpt' else f'@cwd {work}'
            d.confine_call(a, 'SendMessage', {'to': a.agentId, 'message': valid + '\nnext item'}, m['delegation_access'](a))
            if family == 'gpt':
                await d.open({**body, 'dirs': [str(other)]})
            assert d.message_tickets(a) == []
        finally: await d.close()
    asyncio.run(run())


def test_r10_grok_dir_is_refused_even_when_already_held(scope):
    d, a, m, work, *_ = scope
    a.family = a.record['family'] = 'grok'
    with pytest.raises(ValueError, match='not available for grok'):
        d.confine_call(a, 'SendMessage', {'to': a.agentId, 'message': f'@dir {work}\nnext item'}, m.delegation_access(a))
    assert not d.message_tickets(a)


@pytest.mark.parametrize('family', ['gpt', 'grok'])
def test_r10_same_cwd_alias_still_passes_without_ticket(tests_backend, rig, family):
    d, m, _ = tests_backend; work = rig.home / 'work'
    alias = work / 'alias'; alias.symlink_to(work, target_is_directory=True)
    async def run():
        try:
            await d.open({'agentId': 'receiver', 'family': family, 'cwd': str(work)})
            a = d.agents['receiver']
            d.confine_call(a, 'SendMessage', {'to': a.agentId, 'message': f'@cwd {alias}\nnext item'}, m['delegation_access'](a))
            assert not d.message_tickets(a)
        finally: await d.close()
    asyncio.run(run())


@pytest.mark.parametrize('update', ['first', 'append', 'consume'])
@pytest.mark.parametrize('failure', ['write', 'rename'])
def test_r10_failed_manifest_update_preserves_last_good_file(scope, monkeypatch, update, failure):
    d, a, m, *_ = scope
    first, second, third = '1' * 32, '2' * 32, '3' * 32
    before = [] if update == 'first' else [first] if update == 'append' else [first, second]
    for ticket in before: d.message_tickets(a, add=ticket)
    manifest = m.HOME / 'messages' / f'{a.agentId}.json'
    original_write, original_replace = Path.write_text, Path.replace
    def cut_short(path, text, *args, **kwargs):
        if path.parent == manifest.parent:
            original_write(path, '["')
            raise OSError('synthetic interrupted write')
        return original_write(path, text, *args, **kwargs)
    def refuse_rename(path, target):
        if Path(target) == manifest: raise OSError('synthetic rename failure')
        return original_replace(path, target)
    monkeypatch.setattr(Path, 'write_text' if failure == 'write' else 'replace', cut_short if failure == 'write' else refuse_rename)
    request = {'consume': [first]} if update == 'consume' else {'add': third}
    with pytest.raises(OSError, match='synthetic'):
        d.message_tickets(a, **request)
    if before: assert json.loads(manifest.read_text()) == before
    else: assert not manifest.exists()
    assert d.message_tickets(a) == before
    monkeypatch.setattr(Path, 'write_text', original_write); monkeypatch.setattr(Path, 'replace', original_replace)
    after = [second] if update == 'consume' else [*before, third]
    assert d.message_tickets(a, **request) == after
    assert json.loads(manifest.read_text()) == after
    assert list(manifest.parent.iterdir()) == [manifest]

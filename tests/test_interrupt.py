"""Interrupt behaviour."""
import asyncio
import json
import time
import pytest
from test_lordd import PYTHON, grok_home
from delegation_helpers import scope


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

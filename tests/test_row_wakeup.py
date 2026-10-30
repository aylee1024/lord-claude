"""Concurrent row waits each observe a published call."""
import asyncio
from test_lordd import grok_home, load


def test_both_row_waiters_observe_the_same_pending_call(tmp_path):
    m = load('row_notifications'); m.HOME = tmp_path
    async def check():
        a = m.Agent({'agentId': 'a', 'family': 'gemini', 'model': 'fake', 'cwd': str(tmp_path)})
        a.state = 'running'
        loop = asyncio.get_running_loop()
        a.rows = {name: loop.create_future() for name in ['first', 'second']}
        d = object.__new__(m.Daemon); d.agents = {'a': a}
        first = asyncio.create_task(d.route('POST', '/row', {'agentId': 'a', 'rowId': 'first'}))
        publication = asyncio.create_task(d.mcp_call(a, 'send_message', {'to': 'peer', 'text': 'ping'}, a.token))
        second = asyncio.create_task(d.route('POST', '/row', {'agentId': 'a', 'rowId': 'second'}))
        try:
            offered = await asyncio.wait_for(second, .5)
            assert offered['tool']['name'] == 'SendMessage'
            try: assert await asyncio.wait_for(asyncio.shield(first), .2) == offered
            except TimeoutError: assert False, 'another row erased the first row wakeup'
            assert not publication.done() and all(not f.done() for f in a.rows.values())
        finally:
            for task in (first, publication, second): task.cancel()
            await asyncio.gather(first, publication, second, return_exceptions=True)
    asyncio.run(check())


def test_row_wait_blocks_after_a_call_has_settled(tmp_path):
    m = load('row_settled'); m.HOME = tmp_path
    class Counting(dict):
        scans = 0
        def items(self):
            self.scans += 1
            return super().items()
    async def check():
        a = m.Agent({'agentId': 'a', 'family': 'gemini', 'model': 'fake', 'cwd': str(tmp_path)})
        a.state, a.calls = 'running', Counting()
        d = object.__new__(m.Daemon); d.agents = {'a': a}
        call = asyncio.create_task(d.mcp_call(a, 'send_message', {'to': 'peer', 'text': 'ping'}, a.token))
        row = None
        try:
            await asyncio.sleep(0)
            (pending,) = a.calls.values()
            pending.set_result({'content': [], 'isError': False})
            await call
            assert not a.calls
            a.state = 'running'; a.rows = {'r': asyncio.get_running_loop().create_future()}
            a.calls.scans = 0
            row = asyncio.create_task(d.route('POST', '/row', {'agentId': 'a', 'rowId': 'r'}))
            await asyncio.sleep(.1)
            assert not row.done()
            assert 1 <= a.calls.scans <= 3
            result = {'output': 'finished', 'stopped': False}
            a.rows['r'].set_result(result)
            assert await asyncio.wait_for(row, .5) == result
        finally:
            for task in [call, *([row] if row else [])]: task.cancel()
            await asyncio.gather(call, *([row] if row else []), return_exceptions=True)
    asyncio.run(check())

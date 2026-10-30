"""Synthetic agent fixtures and Lord tool event helpers."""
import asyncio
import pytest
from test_lordd import load


@pytest.fixture
def scope(tmp_path):
    m = load('r3_scope'); m.HOME = tmp_path / 'lord'; m.HOME.mkdir()
    work, extra, outside = [tmp_path / name for name in ('work', 'extra', 'outside')]
    for path in (work, extra, outside): path.mkdir()
    (work / 'child').mkdir()
    a = m.Agent({'agentId': 'caller', 'family': 'gpt', 'model': 'fake', 'cwd': str(work), 'dirs': [str(extra)]})
    a.state = 'running'
    d = object.__new__(m.Daemon); d.agents = {'caller': a}; d.redact = m.Redactor()
    d.journal, d.touched = m.HOME / 'journal.jsonl', set()
    return d, a, m, work, extra, outside


def invoke(d, a, tool='spawn_agent', text='work', target=None, **args):
    async def run():
        values = {'type': target or 'lord:gpt', 'prompt': text, 'description': 'probe', **args} if tool == 'spawn_agent' else {'to': target or a.agentId, 'text': text}
        pending = asyncio.create_task(d.mcp_call(a, tool, values, a.token))
        await asyncio.sleep(0)
        if pending.done(): return pending.result(), None
        try:
            event = await asyncio.wait_for(a.events.get(), .5)
            assert event['type'] == 'tool'
            a.calls[event['callId']].set_result({'content': [{'type': 'text', 'text': 'accepted'}], 'isError': False})
            return await pending, event
        finally:
            if not pending.done(): pending.cancel(); await asyncio.gather(pending, return_exceptions=True)
    return asyncio.run(run())


def denied(scope, **request):
    d, a, m, *_ = scope
    result, event = invoke(d, a, **request)
    assert event is None and result['isError'] is True
    message = result['content'][0]['text']
    assert message.startswith('lord: ') and '\n' not in message
    assert message in a.log_path.read_text() and message in (m.HOME / 'lordd.log').read_text()
    assert not a.calls and not a.call_data and a.state == 'running'
    return message

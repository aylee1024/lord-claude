"""Forwarded Lord calls pass open vendor rows without finishing them."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as ThreadTimeout
import json
import time

import pytest

from test_lordd import grok_home, rig


def wait_for(check):
    deadline = time.monotonic() + 3
    while not (value := check()):
        assert time.monotonic() < deadline, 'fake vendor did not reach the expected state'
        time.sleep(.01)
    return value


@pytest.mark.parametrize('family', ['gpt', 'gemini'])
@pytest.mark.parametrize('tool', ['spawn_agent', 'send_message'])
@pytest.mark.parametrize('phase,other', [('waiting', False), ('queued', False), ('waiting', True), ('queued', True)])
def test_forwarded_call_passes_an_unfinished_vendor_row(rig, family, tool, phase, other):
    rig.open(family, **({'nosandbox': True} if family == 'gpt' else {}))
    text = 'FORWARD_ROW:' + json.dumps({'tool': tool, 'other': other})
    first = list(rig.stream('/turn', {'agentId': 'a', 'text': text, 'rows': True}))[-1]
    assert first['type'] == 'row'
    wait_for(lambda: any(e.get('forward_rows_ready') for e in rig.audit()))
    pool = ThreadPoolExecutor(max_workers=1)
    def row(): return rig.request('/row', {'agentId': 'a', 'rowId': first['rowId']})
    try:
        if phase == 'queued':
            (rig.data / 'forward-release').touch()
            wait_for(lambda: rig.request('/agents')[0]['state'] == 'waiting_tool')
        pending = pool.submit(row)
        if phase == 'waiting':
            with pytest.raises(ThreadTimeout): pending.result(timeout=.1)
            (rig.data / 'forward-release').touch()
        try: offered = pending.result(timeout=2)
        except ThreadTimeout: pytest.fail('forwarded Lord call stayed behind an unfinished row')
        call = offered['tool']
        assert call['name'] == ('Agent' if tool == 'spawn_agent' else 'SendMessage')
        assert not any('forward_result' in e for e in rig.audit())
        assert rig.request('/tool_result', {'agentId': 'a', 'callId': call['callId'], 'text': 'PONG', 'isError': False}) == {'ok': True}
        result = wait_for(lambda: next((e['forward_result'] for e in rig.audit() if 'forward_result' in e), None))
        assert result == {'content': [{'type': 'text', 'text': 'PONG'}], 'isError': False}
        pending = pool.submit(row)
        with pytest.raises(ThreadTimeout): pending.result(timeout=.1)
        (rig.data / 'forward-finish').touch()
        output = pending.result(timeout=2)
        assert output['stopped'] is False
        assert 'ordinary output' in output['output'] if other else 'PONG' in output['output']
        events = []
        while True:
            batch = list(rig.stream('/continue', {'agentId': 'a', 'rows': True}))
            events += batch
            assert not any(e['type'] == 'tool' for e in batch), 'already executed Lord call was delivered twice'
            last = batch[-1]
            if last['type'] != 'row': break
            completed = rig.request('/row', {'agentId': 'a', 'rowId': last['rowId']})
            assert completed['stopped'] is False and 'PONG' in completed['output']
        assert events[-1]['type'] == 'done'
        assert json.loads(events[-1]['text']) == result
    finally:
        rig.stop()
        pool.shutdown(wait=True, cancel_futures=True)

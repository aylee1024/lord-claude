"""Deferred thread attachment preserves validation and resume diagnostics."""
import json

import pytest
from test_lordd import grok_home, rig


def thread_calls(rig):
    return [e for e in rig.audit() if e.get('method') in ('thread/start', 'thread/resume')]


def launches(rig):
    return len([e for e in rig.audit() if e.get('launch')])


def cold_agent(rig, state, readonly):
    opened = rig.open('gpt', readonly=readonly)
    assert rig.turn('persistent-word')[-1]['type'] == 'done'
    if state == 'new-daemon':
        rig.stop(); rig.start()
    else:
        assert rig.request('/close', {'agentId': 'a', 'idle_only': True})['closed']
    return opened


@pytest.mark.parametrize('state', ['closed-cached-catalog', 'new-daemon'])
@pytest.mark.parametrize('readonly', [False, True])
def test_unknown_model_is_refused_at_deferred_open_before_thread_resume(rig, state, readonly):
    opened = cold_agent(rig, state, readonly)
    before, calls = launches(rig), len(thread_calls(rig))
    conn, file, status, _ = rig.connect('/open', {'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home),
                                               'resume': True, 'model': 'no-such-model', 'effort': 'ultra'})
    try: reply = json.loads(file.read())
    finally: file.close(); conn.close()
    assert status == 400 and 'unknown gpt model no-such-model' in reply['error']
    assert len(thread_calls(rig)) == calls
    assert rig.request('/agents')[0]['model'] == opened['model']
    rig.open('gpt', resume=True, model=opened['model'], effort='ultra')
    assert len(thread_calls(rig)) == calls
    done = rig.turn('REMEMBER', model=opened['model'], effort='ultra')[-1]
    assert done['text'] == 'persistent-word' and done['record']['session'] == opened['vendorSession']
    assert launches(rig) == before + 1
    assert len(thread_calls(rig)) == calls + 1
    assert thread_calls(rig)[-1]['method'] == 'thread/resume'
    assert thread_calls(rig)[-1]['params']['threadId'] == opened['vendorSession']


@pytest.mark.parametrize('state', ['closed-cached-catalog', 'new-daemon'])
@pytest.mark.parametrize('readonly', [False, True])
def test_missing_deferred_thread_keeps_the_fresh_agent_diagnostic(rig, state, readonly):
    opened = cold_agent(rig, state, readonly)
    (rig.data / (opened['vendorSession'] + '.json')).unlink()
    before, calls = launches(rig), len(thread_calls(rig))
    rig.open('gpt', resume=True, model=opened['model'], effort='ultra')
    assert len(thread_calls(rig)) == calls
    conn, file, status, _ = rig.connect('/turn', {'agentId': 'a', 'text': 'PING', 'effort': 'ultra'})
    try: reply = json.loads(file.read())
    finally: file.close(); conn.close()
    expected = 'agent a cannot be resumed ({"message":"conversation not found"}); launch a fresh agent'
    assert status == 503 and reply['error'] == expected
    assert launches(rig) == before + 1
    assert len(thread_calls(rig)) == calls + 1
    assert thread_calls(rig)[-1]['method'] == 'thread/resume'
    assert thread_calls(rig)[-1]['params']['threadId'] == opened['vendorSession']
    saved = json.loads((rig.home / 'logs/a.json').read_text())
    assert saved['vendorSession'] == opened['vendorSession']
    assert saved['turns'][-1]['ended_how'] == 'refused' and saved['turns'][-1]['error'] == expected

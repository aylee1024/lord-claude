"""Administrative helper calls do not claim delegation without helper activity."""
import json

import pytest
from test_lordd import grok_home, load, rig


@pytest.mark.parametrize('tool', ['closeAgent', 'wait', 'interruptAgent', 'listAgents'])
@pytest.mark.parametrize('late_work', [False, True])
@pytest.mark.parametrize('restored', [False, True])
def test_only_administrative_calls_do_not_count_but_later_helper_work_does(tool, late_work, restored):
    m = load('helper_work')
    m.HOME.mkdir(parents=True, exist_ok=True)
    a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '/work', 'model': 'gpt-6.1-sol', 'effort': 'ultra'})
    a.state, a.vendorSession = 'running', 'parent'
    m.helper_seen(a, {'type': 'subAgentActivity', 'kind': 'started', 'agentThreadId': 'child',
                      'agentPath': '/root/check', 'model': 'gpt-6-luna'})
    a.turn_record('done', {'text': 'first'})
    a.turn_helpers.clear()
    if restored:
        a = m.Agent(dict(a.record)); a.state, a.vendorSession = 'running', 'parent'
    m.helper_seen(a, {'type': 'collabAgentToolCall', 'tool': tool, 'receiverThreadIds': ['child'], 'model': None})
    if late_work:
        vendor = object.__new__(m.Vendor)
        vendor.helper_event(a, a.helpers['child'], 'item/completed',
                            {'item': {'type': 'commandExecution', 'command': 'cat notes.md', 'commandActions': []}})
    terminal = a.turn_record('done', {'text': 'second'})
    saved = json.loads(a.record_path.read_text())['turns'][-1]
    if late_work:
        assert terminal['helpers'] == ['gpt-6-luna'] and 'helper_note' not in terminal
        assert saved['helpers'] == ['child'] and 'helper_note' not in saved
    else:
        assert terminal['helpers'] == [] and saved['helpers'] == []
        assert terminal['helper_note'] == saved['helper_note'] == 'ultra: no helpers ran'


@pytest.mark.parametrize('prompt', ['HELPER_CLOSE', 'HELPER_WAIT_ONLY'])
@pytest.mark.parametrize('readonly', [False, True])
def test_ultra_closing_or_waiting_on_an_inactive_helper_reports_no_helper_work(rig, prompt, readonly):
    rig.open('gpt', effort='ultra', readonly=readonly)
    first = rig.turn('HELPER')[-1]
    assert first['record']['helpers'] == ['gpt-6-luna']
    second = rig.turn(prompt)[-1]
    assert second['type'] == 'done' and second['record']['helpers'] == []
    assert second['record']['helper_note'] == 'ultra: no helpers ran'
    saved = json.loads((rig.home / 'logs/a.json').read_text())
    assert saved['turns'][0]['helpers'] and saved['turns'][1]['helpers'] == []
    assert saved['turns'][1]['helper_note'] == 'ultra: no helpers ran'

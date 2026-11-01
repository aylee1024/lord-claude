"""Only helper work earns turn credit, and restored helpers keep their names."""
import json
from types import SimpleNamespace

import pytest
from test_lordd import grok_home, load


def previous_helper(m):
    m.HOME.mkdir(parents=True, exist_ok=True)
    a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '/work', 'model': 'gpt-6.1-sol', 'effort': 'ultra'})
    a.state, a.vendorSession = 'running', 'parent'
    m.helper_seen(a, {'type': 'subAgentActivity', 'agentThreadId': 'child', 'agentPath': '/root/check',
                      'model': 'gpt-6-luna', 'reasoningEffort': 'low'})
    a.turn_record('done', {'text': 'first'})
    a.turn_helpers.clear()
    return a


def vendor_for(m, a):
    vendor = object.__new__(m.Vendor)
    vendor.d, vendor.family, vendor.agent, vendor.stopping = SimpleNamespace(agents={'a': a}), 'gpt', a, False
    return vendor


@pytest.mark.parametrize('method,params,work', [
    ('thread/closed', {}, False),
    *[('thread/status/changed', {'status': {'type': status}}, False)
      for status in ('notLoaded', 'idle', 'systemError', 'active')],
    ('thread/readState/changed', {'readState': 'read'}, False),
    ('thread/settings/updated', {}, False),
    ('thread/archived', {}, False),
    ('thread/name/updated', {'name': 'check'}, False),
    ('item/started', {'item': {'type': 'commandExecution', 'command': 'cat notes.md'}}, True),
    ('item/completed', {'item': {'type': 'commandExecution', 'command': 'cat notes.md'}}, True),
    ('item/agentMessage/delta', {'delta': 'answer'}, True),
    ('turn/started', {'turn': {'id': 'helper-turn'}}, True),
    ('turn/completed', {'turn': {'id': 'helper-turn'}}, True),
    ('thread/tokenUsage/updated', {'tokenUsage': {'total': {'totalTokens': 1}}}, True),
])
def test_helper_thread_notices_count_only_work(method, params, work):
    m = load('helper_notices')
    a = previous_helper(m)
    vendor = vendor_for(m, a)
    m.helper_seen(a, {'type': 'collabAgentToolCall', 'tool': 'closeAgent', 'receiverThreadIds': ['child']})
    vendor.notification({'method': method, 'params': {'threadId': 'child', **params}})
    terminal = a.turn_record('done', {'text': 'second'})
    saved = json.loads(a.record_path.read_text())['turns'][-1]
    assert terminal['helpers'] == (['gpt-6-luna'] if work else [])
    assert saved['helpers'] == (['child'] if work else [])
    assert terminal.get('helper_note') == saved.get('helper_note') == (None if work else 'ultra: no helpers ran')


@pytest.mark.parametrize('tool', ['sendInput', 'followupTask', 'wait', 'closeAgent'])
def test_restored_helper_name_is_available_before_first_logged_event(tool):
    m = load('helper_names')
    a = previous_helper(m)
    restored = m.Agent(dict(a.record))
    restored.state, restored.vendorSession = 'running', 'parent'
    vendor = vendor_for(m, restored)
    m.helper_seen(restored, {'type': 'collabAgentToolCall', 'tool': tool, 'receiverThreadIds': ['child'], 'model': None})
    vendor.notification({'method': 'item/completed', 'params': {'threadId': 'child', 'item': {
        'type': 'commandExecution', 'command': 'cat notes.md', 'commandActions': []}}})
    terminal = restored.turn_record('done', {'text': 'second'})
    assert '[/root/check] $ cat notes.md' in restored.log_path.read_text()
    helper = json.loads(restored.record_path.read_text())['helpers'][0]
    assert helper['path'] == '/root/check' and helper['commands'] == 1
    assert helper['effort'] == 'low' and terminal['helpers'] == ['gpt-6-luna']


def test_helper_name_replaces_a_thread_id_when_the_name_arrives_later():
    m = load('helper_name_arrival')
    a = previous_helper(m)
    m.helper_seen(a, {'type': 'collabAgentToolCall', 'tool': 'spawnAgent', 'receiverThreadIds': ['new-child']})
    m.helper_seen(a, {'type': 'subAgentActivity', 'agentThreadId': 'new-child', 'agentPath': '/root/named'})
    assert a.helpers['new-child']['path'] == '/root/named'

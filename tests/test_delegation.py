"""Lord tool permissions follow the caller's current sandbox."""
import asyncio
import json

import pytest

from test_lordd import grok_home
from delegation_helpers import scope, invoke, denied


REFUSAL = 'lord: a sandboxed agent cannot launch or message agents; ask Claude to, or launch this agent with @nosandbox'


@pytest.mark.parametrize('tool', ['spawn_agent', 'send_message'])
@pytest.mark.parametrize('mode', ['gpt', 'net', 'tests', 'grok', 'future-nosandbox', 'unknown'])
def test_sandboxed_tools_refuse_once_before_emitting(scope, tool, mode):
    d, a, m, work, *_ = scope
    if mode == 'grok': a.family = a.record['family'] = 'grok'
    if mode in ('net', 'tests'): a.record[mode] = True
    if mode in ('future-nosandbox', 'unknown'):
        a.settings = {'cwd': str(work), 'dirs': [], 'sandbox': 'workspace-write' if mode == 'future-nosandbox' else 'other',
                      'network': False, 'tools': False}
        a.record['nosandbox'] = True
    result, event = invoke(d, a, tool=tool)
    assert result == {'content': [{'type': 'text', 'text': REFUSAL}], 'isError': True}
    assert event is None and a.events.empty() and not a.calls and not a.call_data
    assert a.state == 'running'
    assert a.log_path.read_text().count(REFUSAL) == 1
    assert (m.HOME / 'lordd.log').read_text().count(REFUSAL) == 1
    assert not (m.HOME / 'launches').exists() and not (m.HOME / 'messages').exists()


@pytest.mark.parametrize('tool', ['spawn_agent', 'send_message'])
def test_refusal_does_not_parse_sandboxed_arguments(scope, tool):
    d, a, *_ = scope
    result = asyncio.run(d.mcp_call(a, tool, {}, a.token))
    assert result['isError'] is True and result['content'][0]['text'] == REFUSAL
    assert a.events.empty()


@pytest.mark.parametrize('mode', ['gpt', 'tests', 'grok', 'readonly', 'nosandbox', 'gemini'])
def test_list_agents_remains_available_without_forwarding(scope, mode):
    d, a, *_ = scope
    if mode in ('grok', 'gemini'): a.family = a.record['family'] = mode
    elif mode != 'gpt': a.record[mode] = True
    result = asyncio.run(d.mcp_call(a, 'list_agents', {}, a.token))
    assert result['isError'] is False
    assert json.loads(result['content'][0]['text'])['you'] == a.agentId
    assert a.events.empty()


@pytest.mark.parametrize('tool', ['spawn_agent', 'send_message'])
def test_running_unconfined_settings_authorize_without_ticket(scope, tool):
    d, a, m, work, *_ = scope
    a.settings = {'cwd': str(work), 'dirs': [], 'sandbox': 'none', 'network': True, 'tools': True}
    result, event = invoke(d, a, tool=tool, target='general-purpose', text='@dir /outside\nwork')
    assert event and result['isError'] is False
    assert event['input']['prompt' if tool == 'spawn_agent' else 'message'] == '@dir /outside\nwork'
    assert not (m.HOME / 'launches').exists() and not (m.HOME / 'messages').exists()


@pytest.mark.parametrize('family', ['gpt', 'gemini'])
@pytest.mark.parametrize('tool', ['spawn_agent', 'send_message'])
def test_r3_unconfined_callers_are_unchanged(scope, family, tool):
    d, a, *_ = scope
    a.family = a.record['family'] = family; a.record['nosandbox'] = True
    text = '@nosandbox\n@dir /outside\nwork'
    result, event = invoke(d, a, tool=tool, text=text, target='general-purpose')
    assert event and event['input']['prompt' if tool == 'spawn_agent' else 'message'] == text


@pytest.mark.parametrize('tool', ['spawn_agent', 'send_message'])
def test_r3_readonly_callers_remain_blocked(scope, tool):
    d, a, *_ = scope; a.record['readonly'] = True
    assert 'read-only' in denied(scope, tool=tool)
    assert a.events.empty()


def test_r3_current_readonly_turn_cannot_use_future_write_settings(scope):
    d, a, _, work, *_ = scope
    a.settings = {'cwd': str(work), 'dirs': [], 'sandbox': 'read-only', 'network': False, 'tools': False}
    assert 'read-only' in denied(scope)
    assert a.events.empty() and 'read-only' in a.log_path.read_text()


@pytest.mark.parametrize('tool', ['spawn_agent', 'send_message'])
def test_r4_readonly_refusal_is_returned_once(scope, tool):
    d, a, m, *_ = scope; a.record['readonly'] = True
    result, event = invoke(d, a, tool=tool)
    assert event is None and result['isError'] is True
    assert a.log_path.read_text().count('read-only agent cannot') == 1
    assert (m.HOME / 'lordd.log').read_text().count('read-only agent cannot') == 1

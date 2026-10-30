"""Catalog visibility and refusal through the actual MCP peer and stream."""
import json
import os
import subprocess

import pytest

from test_lordd import grok_home, rig
from delegation_helpers import scope

REFUSAL = 'lord: a sandboxed agent cannot launch or message agents; ask Claude to, or launch this agent with @nosandbox'


@pytest.mark.parametrize('family,options', [('gpt', {}), ('gpt', {'net': True}), ('grok', {}), ('gpt', {'nosandbox': True}), ('gemini', {})])
@pytest.mark.parametrize('prompt', ['TOOL', 'MESSAGE'])
def test_catalog_and_delegation_refusal_through_vendor_mcp(rig, family, options, prompt):
    rig.open(family, **options)
    if prompt == 'MESSAGE': rig.open('gpt', agent='friend')
    events = rig.turn(prompt)
    unconfined = family == 'gemini' or options.get('nosandbox')
    if unconfined:
        call = events[-1]
        assert call['type'] == 'tool'
        assert rig.request('/tool_result', {'agentId': 'a', 'callId': call['callId'], 'text': 'PONG', 'isError': False}) == {'ok': True}
        assert json.loads(list(rig.stream('/continue', {'agentId': 'a'}))[-1]['text'])['content'][0]['text'] == 'PONG'
    else:
        assert not any(e['type'] == 'tool' for e in events)
        assert events[-1]['type'] == 'done'
        result = json.loads(events[-1]['text'])
        assert result == {'content': [{'type': 'text', 'text': REFUSAL}], 'isError': True}
        assert (rig.home / 'logs/a.log').read_text().count(REFUSAL) == 1
    tools = json.loads(rig.turn('CATALOG')[-1]['text'])['tools']
    assert {t['name'] for t in tools} == ({'spawn_agent', 'send_message', 'list_agents'} if unconfined else {'list_agents'})


@pytest.mark.parametrize('family,changed', [('gpt', {'nosandbox': True}), ('gpt', {'readonly': True}), ('grok', {'readonly': True})])
def test_catalog_follows_applied_sandbox_changes(rig, family, changed):
    vendors = []
    for mode in [{}, changed]:
        rig.open(family, **mode)
        tools = json.loads(rig.turn('CATALOG')[-1]['text'])['tools']
        assert {t['name'] for t in tools} == ({'spawn_agent', 'send_message', 'list_agents'} if mode.get('nosandbox') else {'list_agents'})
        vendors.append([e['pid'] for e in rig.audit() if e.get('launch') and e['family'] == family][-1])
    assert len(set(vendors)) == 2


@pytest.mark.parametrize('mode', ['gpt', 'net', 'tests', 'grok', 'readonly', 'nosandbox', 'gemini'])
def test_tool_server_catalog_matches_its_applied_flags(scope, mode):
    d, a, *_ = scope
    if mode in ['grok', 'gemini']: a.family = a.record['family'] = mode
    elif mode != 'gpt': a.record[mode] = True
    server = d.server(a)
    out = subprocess.run([server['command'], *server['args']], input=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}) + '\n',
                         capture_output=True, text=True, env={**os.environ, **server['env']}, timeout=3)
    assert out.returncode == 0
    assert {t['name'] for t in json.loads(out.stdout)['result']['tools']} == ({'spawn_agent', 'send_message', 'list_agents'} if mode in ['nosandbox', 'gemini'] else {'list_agents'})

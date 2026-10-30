"""Docs behaviour."""
import pytest
from test_lordd import ROOT, grok_home


def test_r3_docs_state_delegation_boundary_and_keep_description_cap():
    description = next(line for line in (ROOT / 'agents/gpt.md').read_text().splitlines() if line.startswith('description:'))
    assert len(description.split(': ', 1)[1].strip('"')) <= 975


@pytest.mark.parametrize('name', ['README.md', 'PRIVACY.md'])
def test_r6_docs_state_same_user_file_and_environment_access(name):
    text = (ROOT / name).read_text().lower()
    assert 'as your user' in text and 'environments' in text and 'read your files' in text


@pytest.mark.parametrize('name', ['README.md', 'PRIVACY.md'])
def test_r7_docs_describe_hardening_and_remaining_routes(name):
    text = (ROOT / name).read_text().lower()
    for phrase in ('not a security boundary', 'as your user', 'read your files', "processes' environments", 'shell start-up files', 'project tool folders', "claude code's settings", 'option lines', 'launch types', 'folder', 'control key', 'tool identity', 'records', 'plugin'):
        assert phrase in text


def test_r7_contract_matches_output_tickets_and_vendor_paths():
    text = (ROOT / 'CONTRACT.md').read_text()
    assert '8,192 characters of lookback' in text
    assert 'absolute paths before running them' in text


def test_r8_docs_describe_messages_parameters_and_environment():
    readme = (ROOT / 'README.md').read_text()
    assert 'only the options wait' in readme
    for name in ('LORD_HOME', 'LORD_CONFIG', 'GROK_HOME', 'TMPDIR', 'LORD_AGENT_ID', 'LORD_MCP_TOKEN', 'LORD_READONLY'):
        assert '`' + name + '`' in readme
    contract = (ROOT / 'CONTRACT.md').read_text()
    assert 'socket and plugin' in contract and '65,536' in contract


def test_r9_contract_limits_birth_observation_and_describes_ticket_cleanup():
    contract = (ROOT / 'CONTRACT.md').read_text()
    assert 'before it can leave the tree' not in contract
    assert 'when a refresh observes a process' in contract
    assert 'parent exits before any refresh' in contract
    assert 'remembered pids are never adopted by job listing' not in contract
    assert 'gpt or grok working' in (ROOT / 'README.md').read_text()

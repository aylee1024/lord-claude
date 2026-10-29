"""Marked processes belong to one daemon socket as well as one agent."""
import asyncio
import os
from pathlib import Path
import types
from unittest.mock import AsyncMock

import pytest

from test_lordd import Rig, load, grok_home, rig, tests_backend


def test_fix3_same_id_daemons_list_and_close_only_their_jobs(tmp_path):
    peers, jobs = [], []
    try:
        for name in ('left', 'right'):
            folder = tmp_path / name; folder.mkdir()
            peer = Rig(folder); peers.append(peer)
            peer.open('gpt', agent='scope-check')
            peer.turn('BACKGROUND', agent='scope-check')
            jobs.append(next(e['command_pid'] for e in peer.audit() if 'command_pid' in e))
        listed = [{j['pid'] for j in peer.request('/agents')[0]['jobs']} for peer in peers]
        peers[0].request('/close', {'agentId': 'scope-check', 'idle_only': True})
        try: os.kill(jobs[1], 0); other_alive = True
        except ProcessLookupError: other_alive = False
        assert listed == [{jobs[0]}, {jobs[1]}] and other_alive, {'listed': listed, 'other_alive_after_close': other_alive}
        with pytest.raises(ProcessLookupError): os.kill(jobs[0], 0)
        peers[1].request('/close', {'agentId': 'scope-check', 'idle_only': True})
        with pytest.raises(ProcessLookupError): os.kill(jobs[1], 0)
    finally:
        for peer in peers: peer.stop()


def test_fix3_socket_mismatch_is_unmarked_even_inside_tree(monkeypatch):
    m = load('fix3_socket_scope')
    m.SOCKET = Path('/tmp/x/current.sock')
    children = {10001: [10002], 10002: [10003]}
    markers = m.ProcessMarkers()
    markers.update({10001: 'a', 10002: 'a', 10004: 'a', 10005: 'a', 10006: 'a'})
    markers.sockets = {10001: str(m.SOCKET), 10002: '/tmp/x/other.sock', 10004: '/tmp/x/other.sock', 10005: str(m.SOCKET)}
    markers.visible.update(markers)
    selected = m.job_pids(children, set(m.tree(children, 10001)), {10001}, markers, 'a')
    async def scenario():
        a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '/tmp/x', 'model': 'gpt-6.1-sol'})
        v = m.Vendor(types.SimpleNamespace(), 'gpt', a)
        v.process = types.SimpleNamespace(pid=10001, returncode=None)
        monkeypatch.setattr(m, 'process_table', AsyncMock(return_value=({}, {})))
        monkeypatch.setattr(m, 'process_markers', AsyncMock(return_value=markers))
        monkeypatch.setattr(m, 'job_details', AsyncMock(return_value=[]))
        assert await v.update_jobs() == {10001, 10005}
    asyncio.run(scenario())
    assert selected == {10005}


def test_fix3_exec_server_keeps_socket_identity_without_mcp_token(tests_backend, rig):
    d, m, _ = tests_backend
    async def scenario():
        try:
            await d.open({'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home / 'work'), 'tests': True})
            event = next(e for e in rig.audit() if e.get('exec_launch'))
            assert event['mcp_env'] == {'LORD_DAEMON_SOCKET': str(m['SOCKET'])}
            markers = await m['process_markers']()
            for pid in (d.backend(d.agents['a']).exec_process.pid, event['child']):
                assert markers.get(pid) == 'a' and markers.sockets.get(pid) == str(m['SOCKET'])
        finally: await d.close()
    asyncio.run(scenario())

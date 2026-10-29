"""Tree job ownership with hidden process environments on macOS."""
import asyncio
import os
import signal
import sys
import time

import pytest

from test_lordd import load, rig, grok_home, restart_with


pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS process environments')


@pytest.mark.parametrize('family', ['gpt', 'grok', 'gemini'])
def test_fix2_shell_loop_survives_idle_and_dies_at_close(rig, family):
    restart_with(rig, process_idle_s=.1, idle_exit_s=.3)
    rig.open(family)
    ended = rig.turn('SHELL_BACKGROUND')[-1]
    pid = next(e['shell_pid'] for e in rig.audit() if 'shell_pid' in e)
    try:
        assert pid in {j['pid'] for j in ended['record']['jobs']}
        time.sleep(1.3)
        assert rig.proc.poll() is None
        os.kill(pid, 0)
        assert pid in {j['pid'] for j in rig.request('/agents')[0]['jobs']}
        closed = rig.request('/close', {'agentId': 'a', 'idle_only': True})
        assert closed['closed'] and closed['stopped_jobs'] >= 1
        with pytest.raises(ProcessLookupError): os.kill(pid, 0)
        assert f'killing job {pid}:' in (rig.home / 'logs/a.log').read_text()
    finally:
        try: os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError: pass


def test_fix2_visible_unmarked_tool_subtree_is_not_jobs(rig):
    rig.open('gpt')
    ended = rig.turn('TOOL_BACKGROUND')[-1]
    event = next(e for e in rig.audit() if 'tool_pid' in e)
    pids = {event['tool_pid'], event['tool_child']}
    try:
        assert not (pids & {j['pid'] for j in ended['record']['jobs']})
        assert not (pids & {j['pid'] for j in rig.request('/agents')[0]['jobs']})
        for pid in pids: os.kill(pid, 0)
        closed = rig.request('/close', {'agentId': 'a', 'idle_only': True})
        assert closed['stopped_jobs'] == 0
        for pid in pids:
            with pytest.raises(ProcessLookupError): os.kill(pid, 0)
    finally:
        for pid in pids:
            try: os.kill(pid, signal.SIGKILL)
            except ProcessLookupError: pass


def test_fix2_visibility_distinguishes_shell_and_tool_server(rig):
    rig.open('gpt')
    rig.turn('SHELL_BACKGROUND'); rig.turn('TOOL_BACKGROUND')
    shell = next(e['shell_pid'] for e in rig.audit() if 'shell_pid' in e)
    tool = next(e for e in rig.audit() if 'tool_pid' in e)
    markers = asyncio.run(load('fix2_visibility').process_markers())
    visible = getattr(markers, 'visible', set())
    assert tool['tool_pid'] in visible and shell not in visible and tool['tool_child'] not in visible
    assert not any(markers.get(pid) for pid in (shell, tool['tool_pid'], tool['tool_child']))


def test_fix2_code_host_and_tool_subtrees_leave_marked_jobs():
    m = load('fix2_subtrees')
    children = {10001: [10002, 10004, 10006], 10002: [10003, 10008], 10004: [10005], 10006: [10007]}
    markers = m.ProcessMarkers()
    markers.update({10001: 'a', 10002: 'other', 10004: 'a', 10008: 'a', 10010: 'a'})
    markers.sockets.update({pid: str(m.SOCKET) for pid in markers})
    markers.visible.update({10001, 10002, 10004, 10008, 10010})
    markers.hosts.add(10004)
    pids = set(m.tree(children, 10001))
    assert m.job_pids(children, pids, {10001}, markers, 'a') == {10006, 10007, 10008, 10010}

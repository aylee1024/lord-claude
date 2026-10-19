"""Edge cases of /open, interrupts and folder refusals, run against the daemon with fake vendors."""
import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest

from test_lordd import Rig, PYTHON, ROOT, grok_home, rig  # noqa: F401  (fixtures)


def refused(rig, body):
    conn, file, status, _ = rig.connect('/open', body)
    reply = file.read(); file.close(); conn.close()
    return status, reply


def test_refused_open_options_do_not_return_after_a_daemon_restart(rig):
    # open() restores the record in memory only; attach() already journalled the refused options.
    rig.open('gpt')
    extra = rig.home / 'extra'; extra.mkdir()
    status, reply = refused(rig, {'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home), 'net': True,
                                  'dirs': [str(extra)], 'model': 'no-such-model'})
    assert status == 400, reply
    rig.stop(); rig.start()          # e.g. the 600 s idle exit before the agent's next turn
    rig.open('gpt')
    rig.turn('PING')
    policy = [e['params'] for e in rig.audit() if e.get('method') == 'turn/start'][-1]['sandboxPolicy']
    assert policy['networkAccess'] is False and policy['writableRoots'] == [], policy


def _grok_lists_models(rig):
    # Real grok answers session/new with a `model` config option (attach() reads it into grok_models).
    fake = rig.home / 'grok_fake.py'
    text = (ROOT / 'tests/fakes/vendor.py').read_text()
    text = text.replace("result['configOptions'] = [", "result['configOptions'] = [{'id': 'model', 'options': [{'value': 'grok-4.7'}]}, ", 1)
    fake.write_text(text)
    config = json.loads(rig.config.read_text())
    config['grok']['program'] = [PYTHON, str(fake), 'grok', str(rig.data)]
    rig.config.write_text(json.dumps(config))
    rig.stop(); rig.start()


def _last_prompt_launch(rig):
    audit = rig.audit()
    pid = [e for e in audit if e.get('method') == 'session/prompt'][-1]['pid']
    return [e for e in audit if e.get('launch') and e['pid'] == pid][0]


def test_refused_open_with_tools_does_not_leave_grok_running_with_the_users_tools(rig):
    # the restore sets generation 0 (reattach) but not restart, so the process started for the
    # refused options keeps running.
    _grok_lists_models(rig)
    rig.open('grok')
    status, reply = refused(rig, {'agentId': 'a', 'family': 'grok', 'cwd': str(rig.home), 'tools': True, 'model': 'no-such-model'})
    assert status == 400, reply
    rig.open('grok')
    assert rig.turn('PING')[-1]['text'] == 'PONG'
    launch = _last_prompt_launch(rig)
    assert launch['grok_env'].get('GROK_CLAUDE_MCPS_ENABLED') == 'false', launch['grok_env']


def test_refused_open_with_readonly_does_not_leave_a_writable_grok_read_only(rig):
    _grok_lists_models(rig)
    rig.open('grok')
    status, reply = refused(rig, {'agentId': 'a', 'family': 'grok', 'cwd': str(rig.home), 'readonly': True, 'model': 'no-such-model'})
    assert status == 400, reply
    rig.open('grok')
    assert rig.turn('PING')[-1]['text'] == 'PONG'
    assert 'lord-workspace' in _last_prompt_launch(rig)['argv'], _last_prompt_launch(rig)['argv']


def test_an_interrupt_during_a_start_longer_than_five_seconds_is_not_lost(rig):
    # interrupt() waits at most 5 s for `starting`, then sees state 'idle' and does nothing.
    slow = rig.home / 'slow_agy.py'
    marker = rig.home / 'slow'
    slow.write_text(
        'import os, sys, time\n'
        f'if os.path.exists({str(marker)!r}) and sys.argv[3:4] not in (["models"], ["mcp"]): time.sleep(7)\n'
        f'os.execv({PYTHON!r}, [{PYTHON!r}, {str(ROOT / "tests/fakes/vendor.py")!r}, *sys.argv[1:]])\n')
    config = json.loads(rig.config.read_text())
    config['gemini']['program'] = [PYTHON, str(slow), 'gemini', str(rig.data)]
    rig.config.write_text(json.dumps(config))
    rig.stop(); rig.start()
    rig.open('gemini')
    marker.touch()
    # The mod's real sequence for a follow-up with "@model gemini-4-argon": /open (model checked), then /turn.
    rig.open('gemini', model='gemini-4-argon')
    events = []
    worker = threading.Thread(target=lambda: events.extend(rig.turn('PING', model='gemini-4-argon')))
    worker.start()
    time.sleep(.7)
    began = time.monotonic()
    assert rig.request('/interrupt', {'agentId': 'a'})['ok']
    waited = time.monotonic() - began
    worker.join(20)
    assert events, 'turn stream produced nothing'
    assert events[-1]['type'] == 'error' and events[-1]['message'] == 'interrupted', (round(waited, 1), events[-1])


def test_a_relative_dir_is_not_resolved_against_the_daemons_folder(rig, monkeypatch):
    # the mod passes @dir text unchanged; open_agent() resolves it against the daemon's own cwd.
    elsewhere = rig.home / 'other-project'
    (elsewhere / 'shared').mkdir(parents=True)
    project = rig.home / 'project'; project.mkdir()
    monkeypatch.chdir(elsewhere)      # the folder of the session that happened to start the daemon
    rig.stop(); rig.start()
    status, reply = refused(rig, {'agentId': 'a', 'family': 'gpt', 'cwd': str(project), 'dirs': ['shared']})
    if status == 200:
        rig.turn('PING')
        roots = [e['params'] for e in rig.audit() if e.get('method') == 'turn/start'][-1]['sandboxPolicy']['writableRoots']
        assert not any(r.startswith(str(elsewhere)) for r in roots), roots


def test_folders_that_hold_the_home_folder_through_the_data_volume_are_refused(rig):
    # /System and /System/Volumes hold /System/Volumes/Data/Users/<you>, the home folder.
    if sys.platform != 'darwin': pytest.skip('macOS data volume')
    for cwd in ('/System/Volumes', '/System'):
        status, reply = refused(rig, {'agentId': 'v' + str(len(cwd)), 'family': 'gpt', 'cwd': cwd})
        assert status == 400, (cwd, reply)

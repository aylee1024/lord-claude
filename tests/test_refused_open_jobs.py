"""Invalid models are refused before restart options can disturb a live agent."""
import asyncio
import copy
import os

import pytest
from test_lordd import grok_home, rig, tests_backend
from test_ultra_lifecycle import delivered_turn, launches, threads


@pytest.mark.parametrize('option', ['dirs', 'readonly', 'nice', 'tools'])
@pytest.mark.parametrize('deferred', [False, True])
def test_unknown_model_with_restart_options_preserves_jobs_and_agent(tests_backend, rig, option, deferred):
    d, _, _ = tests_backend
    extra = rig.home / 'extra'; extra.mkdir()

    async def scenario():
        try:
            body = {'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home / 'work')}
            await d.open(body)
            a = d.agents['a']; vendor = d.backend(a)
            await delivered_turn(d, a, 'BACKGROUND')
            job = next(e['command_pid'] for e in rig.audit() if 'command_pid' in e)
            os.kill(job, 0)
            record = copy.deepcopy(a.record)
            state = a.state, a.restart, a.generation, a.vendorSession, a.model, a.effort, a.codex_helpers
            pid, starts, calls = vendor.process.pid, launches(rig), len(threads(rig))
            value = [str(extra)] if option == 'dirs' else 5 if option == 'nice' else True
            request = {**body, 'resume': True, option: value, 'model': 'no-such-model'}
            if deferred: request['effort'] = 'high'
            with pytest.raises(ValueError, match='unknown gpt model no-such-model'):
                await d.open(request)
            os.kill(job, 0)
            assert a.record == record
            assert (a.state, a.restart, a.generation, a.vendorSession, a.model, a.effort, a.codex_helpers) == state
            assert vendor.process.pid == pid and launches(rig) == starts and len(threads(rig)) == calls
            assert (await delivered_turn(d, a, 'PING'))['text'] == 'PONG'
            os.kill(job, 0)
            assert launches(rig) == starts
        finally:
            await d.close()

    asyncio.run(scenario())

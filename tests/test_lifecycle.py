"""Daemon shutdown, legacy cleanup and interrupt diagnostics."""
import asyncio
import os
import shutil
import signal
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_lordd import grok_home, rig, tests_backend
from delegation_helpers import scope


@pytest.mark.parametrize('family', ['gpt', 'grok'])
@pytest.mark.parametrize('error', [TimeoutError(), BrokenPipeError('closed input')], ids=['timeout', 'pipe'])
def test_interrupt_failure_names_the_exception(scope, family, error):
    d, a, *_ = scope
    a.family = a.record['family'] = family
    a.turn_id, a.dispatched = 'synthetic-turn', True
    a.finished.set()
    logs = []; d.log = logs.append
    vendor = SimpleNamespace(rpc=AsyncMock(side_effect=error), send=AsyncMock(side_effect=error), close=AsyncMock())
    d.vendors = {a.agentId: vendor}
    asyncio.run(d.interrupt(a))
    assert logs == [f'{a.agentId}: turn interrupt failed: {error!r}']
    assert vendor.close.await_count == (1 if family == 'gpt' else 0)


@pytest.mark.parametrize('linked', [False, True])
def test_prune_removes_legacy_ticket_folders(tests_backend, rig, linked):
    d, m, _ = tests_backend
    outside = rig.home / 'work/kept'; outside.mkdir()
    sentinel = outside / 'keep'; sentinel.write_text('keep')
    for name in ('launches', 'messages'):
        folder = m['HOME'] / name
        if linked: folder.symlink_to(outside, target_is_directory=True)
        else:
            folder.mkdir(); (folder / 'current.json').write_text('{}')
            (folder / 'partial.tmp').write_text('partial')
    async def run():
        try:
            assert await d.prune(14) == {'ok': True, 'pruned': []}
            assert all(not os.path.lexists(m['HOME'] / name) for name in ('launches', 'messages'))
            assert sentinel.read_text() == 'keep'
        finally: await d.close()
    asyncio.run(run())


@pytest.mark.parametrize('active', [False, True])
def test_daemon_exits_after_its_home_is_deleted(rig, active):
    pids = []
    if active:
        rig.open('gpt'); rig.turn('BACKGROUND')
        pids = [e['command_pid'] for e in rig.audit() if 'command_pid' in e]
    try:
        shutil.rmtree(rig.home)
        try: assert rig.proc.wait(timeout=8) == 0
        except subprocess.TimeoutExpired: pytest.fail('daemon remained alive after its home was deleted')
        for pid in pids:
            with pytest.raises(ProcessLookupError): os.kill(pid, 0)
    finally:
        rig.stop()
        for pid in pids:
            try: os.kill(pid, signal.SIGKILL)
            except ProcessLookupError: pass

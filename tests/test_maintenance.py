"""Legacy cleanup shares ownership and does not block journal maintenance."""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from unittest.mock import AsyncMock

import pytest

from test_lordd import grok_home, rig, load, tests_backend


@pytest.mark.parametrize('another', [False, True])
def test_legacy_folders_wait_for_other_daemons(tests_backend, monkeypatch, another):
    d, m, _ = tests_backend
    monkeypatch.setitem(m, 'others_answering', AsyncMock(return_value=another))
    for name in ('launches', 'messages'):
        folder = m['HOME'] / name; folder.mkdir(); (folder / 'ticket').write_text('keep')
    async def check():
        try:
            assert await d.prune(14) == {'ok': True, 'pruned': []}
            for name in ('launches', 'messages'):
                assert (m['HOME'] / name).exists() is another
                if another: assert (m['HOME'] / name / 'ticket').read_text() == 'keep'
        finally: await d.close()
    asyncio.run(check())


@pytest.mark.parametrize('another', [False, True])
def test_prune_excludes_its_socket_through_a_home_alias(tmp_path, another):
    m = load('maintenance_alias')
    physical = tmp_path / 'physical'; physical.mkdir()
    m.HOME = tmp_path / 'linked'; m.HOME.symlink_to(physical, target_is_directory=True)
    m.SOCKET = physical / 'lordd-current.sock'
    for name in ('launches', 'messages'): (m.HOME / name).mkdir()
    async def check():
        async def accept(reader, writer):
            writer.close(); await writer.wait_closed()
        servers = [await asyncio.start_unix_server(accept, str(m.SOCKET))]
        try:
            if another: servers.append(await asyncio.start_unix_server(accept, str(m.HOME / 'lordd-other.sock')))
            d = object.__new__(m.Daemon); d.home_id = m.home_identity()
            d.refresh_jobs = AsyncMock(); d.agents, d.open_locks, d.pruned = {}, {}, set()
            d.compact, d.sweep = lambda *args: None, lambda *args: None
            assert await m.others_answering() is another
            assert await d.prune_locked(14) == {'ok': True, 'pruned': []}
            for name in ('launches', 'messages'): assert (m.HOME / name).exists() is another
        finally:
            for server in servers: server.close()
            for server in servers: await server.wait_closed()
    asyncio.run(check())


@pytest.mark.parametrize('failure', ['plain', 'permission'])
def test_legacy_removal_failure_keeps_compaction_and_sweep_running(tests_backend, monkeypatch, failure):
    d, m, _ = tests_backend
    monkeypatch.setitem(m, 'others_answering', AsyncMock(return_value=False))
    path = m['HOME'] / 'launches'
    if failure == 'plain': path.write_text('not a folder')
    else:
        path.mkdir()
        real = m['shutil'].rmtree
        def denied(folder, *args, **kwargs):
            if folder == path: raise PermissionError('synthetic denial')
            return real(folder, *args, **kwargs)
        monkeypatch.setattr(m['shutil'], 'rmtree', denied)
    (m['HOME'] / 'messages').mkdir()
    logs = []; monkeypatch.setattr(d, 'log', logs.append)
    folder = m['HOME'] / 'logs'; folder.mkdir()
    stale = folder / 'gone.log'; stale.write_text('old')
    stamp = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp(); os.utime(stale, (stamp, stamp))
    async def check():
        try:
            assert await d.prune(14) == {'ok': True, 'pruned': []}
            assert not stale.exists() and d.journal.exists()
            assert not (m['HOME'] / 'messages').exists()
            assert any('launches' in line for line in logs)
        finally: await d.close()
    asyncio.run(check())

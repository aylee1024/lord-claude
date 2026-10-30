"""Legacy cleanup shares ownership and does not block journal maintenance."""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from unittest.mock import AsyncMock

import pytest

from test_lordd import grok_home, rig, tests_backend


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

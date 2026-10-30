"""Resume behaviour."""
import asyncio
import json
from pathlib import Path
import pytest
from test_lordd import grok_home, rig, tests_backend


@pytest.mark.parametrize('family', ['gpt', 'grok', 'gemini'])
def test_r2_first_open_writes_restorable_record_without_turns(rig, family):
    opened = rig.open(family)
    path = rig.home / 'logs/a.json'
    assert path.exists()
    saved = json.loads(path.read_text())
    assert saved['turns'] == [] and saved['vendorSession'] == opened['vendorSession']
    rig.stop(); (rig.home / 'journal.jsonl').unlink(); rig.start()
    restored = rig.request('/restore', {'agentId': 'a'})
    assert restored['found'] and restored['agent']['family'] == family
    assert rig.open(family, resume=True)['vendorSession'] == opened['vendorSession']
    assert rig.turn('PING')[-1]['record']['commands'] >= 0
    assert len(json.loads(path.read_text())['turns']) == 1


def test_r2_quit_closes_known_ids_across_session_changes(tests_backend, rig):
    d, _, _ = tests_backend
    async def scenario():
        try:
            for aid, session in [('idle', 'old'), ('active', 'old'), ('other', 'unrelated')]:
                await d.open({'agentId': aid, 'family': 'gpt', 'cwd': str(rig.home / 'work'), 'session': session})
            await d.begin(d.agents['active'], {'text': 'SLOW'})
            await d.route('POST', '/session_end', {'session': 'new', 'agentIds': ['idle', 'active'], 'cause': 'session end: quit'})
            assert d.agents['idle'].state == 'closed'
            assert d.agents['active'].state == 'running' and d.agents['active'].session_ended
            assert d.agents['other'].state == 'idle' and not d.agents['other'].session_ended
        finally: await d.close()
    asyncio.run(scenario())


def test_r2_record_oserror_is_transient_and_retry_succeeds(tests_backend, rig, monkeypatch):
    d, m, _ = tests_backend
    async def scenario():
        try:
            await d.open({'agentId': 'a', 'family': 'gpt', 'cwd': str(rig.home / 'work')})
            a = d.agents['a']
            a.turn_record('done', {'text': 'saved'})
            await d.backend(a).close(); d.agents.clear(); d.journal.unlink()
            original = Path.read_text
            def read(path, *args, **kw):
                if path == a.record_path: raise PermissionError('temporary record access failure')
                return original(path, *args, **kw)
            with monkeypatch.context() as patch:
                patch.setattr(Path, 'read_text', read)
                with pytest.raises(RuntimeError, match='retry later'): d.restore('a')
            assert len(json.loads(a.record_path.read_text())['turns']) == 1
            assert d.restore('a')['found'] and d.agents['a'].vendorSession == a.vendorSession
        finally: await d.close()
    asyncio.run(scenario())

"""Codex helper configuration and per-turn delegation evidence."""
import asyncio
import json
import shutil
import subprocess

import pytest
from test_lordd import PYTHON, grok_home, load, rig


def thread_params(rig):
    return [e['params'] for e in rig.audit() if e.get('method') in ('thread/start', 'thread/resume')]


def assert_helpers(params, enabled):
    assert params['config']['agents']['enabled'] is enabled
    assert params['config']['features'] == {'multi_agent': enabled, 'multi_agent_v2': enabled}
    profile = params['config']['permissions'][params['config']['default_permissions']]
    assert set(profile['filesystem'].values()) <= {'read', 'deny'}
    assert profile['network'] == {'enabled': False}
    assert 'sandbox' not in params


@pytest.mark.parametrize('options', [{}, {'tools': True}, {'nosearch': True}])
def test_readonly_ultra_enables_helpers_on_start_resume_and_crash(rig, options):
    opened = rig.open('gpt', readonly=True, effort='ultra', dirs=[str(rig.home)], **options)
    assert_helpers(thread_params(rig)[-1], True)
    assert rig.turn('PING')[-1]['type'] == 'done'
    assert rig.turn('CRASH')[-1]['type'] == 'error'
    assert rig.turn('PING')[-1]['type'] == 'done'
    assert thread_params(rig)[-1]['threadId'] == opened['vendorSession']
    assert_helpers(thread_params(rig)[-1], True)
    turns = [e['params'] for e in rig.audit() if e.get('method') == 'turn/start']
    assert all(p['effort'] == 'ultra' and 'sandboxPolicy' not in p for p in turns)


@pytest.mark.parametrize('readonly', [False, True])
def test_helper_configuration_follows_each_effective_turn_effort(rig, readonly):
    opened = rig.open('gpt', readonly=readonly)
    session = opened['vendorSession']
    assert rig.turn('persistent-word')[-1]['type'] == 'done'
    previous_effort = opened['effort']
    for effort, model, effective in [('ultra', 'gpt-6.1-sol', 'ultra'), ('ultra', 'gpt-6.1-sol', 'ultra'),
                                     ('max', 'gpt-6.1-sol', 'max'), ('xhigh', 'gpt-6.1-sol', 'xhigh'),
                                     ('ultra', 'gpt-6.1-sol', 'ultra'), ('ultra', 'gpt-6-luna', 'high'),
                                     ('high', 'gpt-6.1-sol', 'high')]:
        before = len([e for e in rig.audit() if e.get('launch')])
        threads_before = len(thread_params(rig))
        previous = thread_params(rig)[-1]
        result = rig.turn('REMEMBER', effort=effort, model=model)[-1]
        assert result['type'] == 'done' and result['record']['effort'] == effective
        assert result['text'] == 'persistent-word' and result['record']['session'] == session
        params = thread_params(rig)[-1]
        relaunched = readonly and (previous_effort == 'ultra') != (effective == 'ultra')
        assert len([e for e in rig.audit() if e.get('launch')]) == before + int(relaunched)
        assert len(thread_params(rig)) == threads_before + int(relaunched)
        if readonly:
            assert_helpers(params, effective == 'ultra')
            if relaunched:
                assert [e['method'] for e in rig.audit() if e.get('method') in ('thread/start', 'thread/resume')][-1] == 'thread/resume'
                assert params['threadId'] == session
            else:
                assert params == previous
        else:
            assert params == previous
            assert 'features' not in params['config'] and 'agents' not in params['config']
            assert len([e for e in rig.audit() if e.get('launch')]) == before
        previous_effort = effective


@pytest.mark.parametrize('outcome', ['PING', 'FAIL'])
def test_ultra_no_helpers_is_in_the_terminal_event_and_saved_turn(rig, outcome):
    rig.open('gpt', effort='ultra')
    event = rig.turn(outcome)[-1]
    assert event['record']['helper_note'] == 'ultra: no helpers ran'
    saved = json.loads((rig.home / 'logs/a.json').read_text())['turns'][-1]
    assert saved['effort'] == 'ultra' and saved['model'] == 'gpt-6.1-sol'
    assert saved['helpers'] == [] and saved['helper_note'] == event['record']['helper_note']


def test_helpers_from_an_earlier_turn_in_the_same_second_do_not_hide_missing_delegation():
    m = load('ultra_helper_turns')
    async def run():
        a = m.Agent({'agentId': 'a', 'family': 'gpt', 'cwd': '/work', 'model': 'gpt-6.1-sol', 'effort': 'ultra'})
        a.record_path.parent.mkdir(parents=True, exist_ok=True)
        a.turn_started = '2026-01-01T00:00:00+00:00'
        m.helper_seen(a, {'type': 'subAgentActivity', 'agentThreadId': 'child', 'model': 'gpt-6-luna'})
        a.helpers['child']['started'] = a.turn_started
        first = a.turn_record('done', {'text': 'one'})
        assert first['helpers'] == ['gpt-6-luna'] and 'helper_note' not in first
        d = object.__new__(m.Daemon)
        d.quota_refusal = lambda *args: None
        async def unchanged(*args): pass
        d.effort = d.attach = unchanged
        d.config = {'gpt': {}, 'vendor_home': str(m.HOME / 'vendor')}
        d.save = lambda *args: None
        d.task = lambda coroutine: asyncio.create_task(coroutine)
        d.run_turn = unchanged
        # Matching timestamps exercise turns that start before the clock advances a second.
        class Clock:
            @staticmethod
            def now(*args):
                return m.datetime.fromisoformat(a.turn_started)
        original = m.datetime
        Clock.now = staticmethod(lambda *args: original.fromisoformat(a.turn_started))
        m.datetime = Clock
        await d.begin_starting(a, {'text': 'two'})
        await a.turn_task
        second = a.turn_record('done', {'text': 'two'})
        assert second['helpers'] == [] and second['helper_note'] == 'ultra: no helpers ran'
    asyncio.run(run())


def test_parent_readonly_profile_denies_native_writes_with_helper_switches_present(rig, tmp_path):
    codex = shutil.which('codex')
    if not codex: pytest.skip('native codex is not installed')
    rig.open('gpt', readonly=True, effort='ultra', tools=True)
    params = thread_params(rig)[-1]
    assert_helpers(params, True)
    cfg = params['config']; name = cfg['default_permissions']; profile = cfg['permissions'][name]
    cli = tmp_path / 'cli'; cli.mkdir()
    lines = [f'default_permissions={json.dumps(name)}', '[agents]', 'enabled=true',
             '[features]', 'multi_agent=true', 'multi_agent_v2=true', f'[permissions.{name}.filesystem]']
    lines += [json.dumps(k) + '=' + json.dumps(v) for k, v in profile['filesystem'].items()]
    lines += [f'[permissions.{name}.network]', 'enabled=false']
    (cli / 'config.toml').write_text('\n'.join(lines) + '\n')
    target = rig.home / 'write-probe'
    code = "import pathlib,sys; p=pathlib.Path(sys.argv[1]); print(p.parent.is_dir()); p.write_text('synthetic')"
    env = {'HOME': str(cli), 'CODEX_HOME': str(cli), 'PATH': '/opt/homebrew/bin:/usr/bin:/bin'}
    result = subprocess.run([codex, 'sandbox', '-P', name, '--', PYTHON, '-I', '-c', code, str(target)],
                            env=env, cwd=rig.home, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and result.stdout.strip() == 'True'
    assert 'PermissionError' in result.stderr and not target.exists()

"""Profiles behaviour."""
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
import pytest
from test_lordd import PYTHON, ROOT, grok_home, load, rig, tests_backend
from delegation_helpers import scope

import test_profiles as test_round7


@pytest.mark.parametrize('root', ['cwd', 'dir'])
def test_r2_exec_restart_rechecks_pinned_roots(tests_backend, rig, root):
    d, _, launched = tests_backend
    work = rig.home / 'work'; extra = rig.home / 'extra'; extra.mkdir()
    outside = rig.home / 'outside'; outside.mkdir()
    async def scenario():
        try:
            await d.open({'agentId': 'a', 'family': 'gpt', 'cwd': str(work), 'dirs': [str(extra)], 'tests': True})
            a = d.agents['a']
            await d.begin(a, {'text': 'FAIL'}); await asyncio.wait_for(a.finished.wait(), 5)
            path = work if root == 'cwd' else extra
            path.rename(path.with_name(path.name + '.moved')); path.symlink_to(outside, target_is_directory=True)
            while not a.events.empty(): a.events.get_nowait()
            before = len(launched)
            with pytest.raises(ValueError, match='now points to'):
                await d.begin_locked(a, {'text': 'PING'})
            assert not any('/usr/bin/sandbox-exec' in argv for argv, _ in launched[before:])
        finally: await d.close()
    asyncio.run(scenario())


def test_r5_nosandbox_retains_legacy_policy_on_start_resume_and_turn(rig):
    rig.open('gpt', nosandbox=True)
    rig.turn('PING')
    rig.turn('CRASH')
    rig.turn('PING')
    threads = [e['params'] for e in rig.audit() if e.get('method') in ('thread/start', 'thread/resume')]
    assert {e.get('method') for e in rig.audit()} >= {'thread/start', 'thread/resume'}
    for params in threads:
        assert params.get('sandbox') == 'danger-full-access'
        assert 'default_permissions' not in params['config'] and 'permissions' not in params['config']
    turns = [e['params'] for e in rig.audit() if e.get('method') == 'turn/start']
    assert turns and all(p.get('sandboxPolicy') == {'type': 'dangerFullAccess'} for p in turns)


@pytest.mark.parametrize('options', [{}, {'net': True}, {'readonly': True}])
def test_r5_gpt_thread_keeps_key_denial_on_start_and_resume(rig, options):
    rig.open('gpt', **options)
    rig.turn('PING')
    rig.turn('CRASH')
    rig.turn('PING')
    threads = [e['params'] for e in rig.audit() if e.get('method') in ('thread/start', 'thread/resume')]
    for params in threads:
        config = params['config']
        profile = config['permissions'][config['default_permissions']]
        assert profile['filesystem'][str((rig.home / 'control.header').resolve())] == 'deny'
        assert profile['filesystem']['/**/control.header'] == 'deny'
        assert 'sandbox' not in params
        assert profile['network']['enabled'] is bool(options.get('net') or options.get('nosandbox'))
    assert all('sandboxPolicy' not in e['params'] for e in rig.audit() if e.get('method') == 'turn/start')


@pytest.mark.parametrize('family,option', [('gpt', 'cwd'), ('grok', 'cwd'), ('gemini', 'cwd'), ('gpt', 'dir')])
def test_r5_vendor_restart_rechecks_all_folder_pins(tests_backend, rig, family, option):
    d, m, _ = tests_backend
    slot, outside = rig.home / 'work/slot', rig.home / 'outside'
    slot.mkdir(); outside.mkdir()
    async def run():
        try:
            body = {'agentId': 'child', 'family': family, 'cwd': str(slot if option == 'cwd' else rig.home / 'work')}
            if option == 'dir': body['dirs'] = [str(slot)]
            await d.open(body)
            a = d.agents['child']; await d.backend(a).close(); a.generation = 0
            slot.rmdir(); slot.symlink_to(outside, target_is_directory=True)
            with pytest.raises(ValueError, match=f'@{option} .*now points to'):
                await d.begin(a, {'text': 'PING'})
        finally: await d.close()
    asyncio.run(run())


def test_r5_grok_queued_followup_refuses_swapped_folder(rig):
    slot, outside = rig.home / 'work/slot', rig.home / 'outside'
    slot.mkdir(parents=True); outside.mkdir()
    rig.request('/open', {'agentId': 'child', 'family': 'grok', 'cwd': str(slot), 'wall_s': 1})
    events = []
    reader = threading.Thread(target=lambda: events.extend(rig.stream('/turn', {'agentId': 'child', 'text': 'SLOW'})))
    reader.start()
    time.sleep(.3)
    assert rig.request('/queue', {'agentId': 'child', 'text': 'PING'})['queued']
    slot.rmdir(); slot.symlink_to(outside, target_is_directory=True)
    reader.join(15)
    assert not reader.is_alive()
    assert 'now points to' in events[-1].get('message', '')
    assert len(rig.sandbox_audit()) == 1


@pytest.mark.parametrize('options', [{}, {'net': True}, {'readonly': True}])
@pytest.mark.parametrize('layout', ['cwd-parent', 'dir-parent', 'dir-home', 'dir-child', 'cwd-child'])
def test_r6_gpt_lord_home_stays_read_only(scope, options, layout):
    _, a, m, work, *_ = scope
    home = m.HOME
    (home / 'logs').mkdir()
    a.record.update(options)
    a.record['dirs'] = []
    if layout == 'cwd-parent': a.cwd = str(home.parent)
    elif layout == 'cwd-child': a.cwd = str(home / 'logs')
    else:
        a.cwd = str(work)
        a.record['dirs'] = [str(home.parent if layout == 'dir-parent' else home / 'logs' if layout == 'dir-child' else home)]
    config = m.gpt_permissions(a)
    fs = config['permissions'][config['default_permissions']]['filesystem']
    assert fs.get(str(home)) == 'read'
    assert fs.get(m.canon(home)) == 'read'
    assert all(not m.inside(path, [m.canon(home)]) for path, access in fs.items() if access == 'write')
    assert fs[m.canon(home / 'control.header')] == 'deny'


@pytest.mark.parametrize('options', [{}, {'net': True}, {'readonly': True}])
def test_r6_temporary_control_key_is_denied_by_gpt(scope, options):
    _, a, m, *_ = scope; a.record.update(options)
    cfg = m.gpt_permissions(a); fs = cfg['permissions'][cfg['default_permissions']]['filesystem']
    assert fs.get('/**/.control.header') == 'deny'
    assert fs.get(m.canon(m.HOME / '.control.header')) == 'deny'


@pytest.mark.parametrize('family', ['grok', 'tests'])
@pytest.mark.parametrize('check', ['records', 'temporary-key'])
def test_r6_native_grok_and_tests_protect_lord_home(scope, monkeypatch, family, check):
    _, _, m, work, _, outside = scope
    monkeypatch.setenv('GROK_HOME', str(outside / 'grok'))
    (m.HOME / 'control.header').write_text('synthetic key')
    (m.HOME / '.control.header').write_text('synthetic temporary key')
    env = {'TMPDIR': str(outside / 'temp')}; Path(env['TMPDIR']).mkdir()
    profile, params = m.grok_profile(str(work), False, env) if family == 'grok' else m.tests_profile(str(work), [], env)
    code = """import json,sys
from pathlib import Path
home,work=map(Path,sys.argv[1:])
out={}
for name,path in [('record',home/'journal.jsonl'),('neighbor',work/'probe')]:
    try: path.write_text('synthetic');out[name]=True
    except OSError: out[name]=False
for name in ('control.header','.control.header'):
    try: (home/name).read_text();out[name]=True
    except OSError: out[name]=False
print(json.dumps(out))
"""
    result = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, *params, '/opt/homebrew/bin/python3.12', '-I', '-c', code, str(m.HOME), str(work)],
        capture_output=True, text=True, timeout=10, env={'PATH': '/usr/bin:/bin', 'TMPDIR': env['TMPDIR']})
    assert result.returncode == 0, 'native profile did not run'
    measured = json.loads(result.stdout)
    assert measured['neighbor'] is True and measured['control.header'] is False
    assert measured['record' if check == 'records' else '.control.header'] is False


@pytest.mark.parametrize('first', [False, True])
@pytest.mark.parametrize('folder', ['cwd', 'dir'])
def test_r7_startup_swap_never_grants_the_new_target(tests_backend, rig, monkeypatch, first, folder):
    d, m, _ = tests_backend
    work = rig.home / 'work'; slot, outside = work / 'slot', rig.home / 'outside'
    slot.mkdir(); outside.mkdir()
    body = {'agentId': 'child', 'family': 'gpt', 'cwd': str(slot if folder == 'cwd' else work), **({'dirs': [str(slot)]} if folder == 'dir' else {})}
    original = m['Vendor'].rpc
    async def racing(self, method, params, **kw):
        if method == 'initialize' and self.agent and self.agent.agentId == 'child' and not slot.is_symlink():
            slot.rename(work / 'moved'); slot.symlink_to(outside, target_is_directory=True)
        return await original(self, method, params, **kw)
    async def run():
        try:
            if not first:
                await d.open(body)
                await d.backend(d.agents['child']).close(); d.agents['child'].generation = 0
            monkeypatch.setattr(m['Vendor'], 'rpc', racing)
            await d.open(body)
            params = [e['params'] for e in rig.audit() if e.get('method') in ('thread/start', 'thread/resume')][-1]
            config = params['config']; fs = config['permissions'][config['default_permissions']]['filesystem']
            assert fs.get(str(slot)) == 'write'
            assert m['canon'](outside) not in fs
        finally: await d.close()
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['workspace', 'net', 'readonly', 'grok', 'tests'])
def test_r7_native_profiles_protect_both_plugin_spellings(tmp_path, monkeypatch, mode):
    codex = shutil.which('codex') if mode not in ('grok', 'tests') else None
    if mode not in ('grok', 'tests') and not codex: pytest.skip('native codex is not installed')
    m = load('r7_plugin'); work = tmp_path / 'work'; work.mkdir()
    plugin = work / 'cache' / 'plugin'; plugin.mkdir(parents=True)
    shutil.copytree(ROOT / 'sandbox', plugin / 'sandbox')
    (plugin / 'lordd.py').write_text('# synthetic plugin\n')
    alias = work / 'alias'; alias.symlink_to(plugin, target_is_directory=True)
    monkeypatch.setattr(m, 'HERE', (alias / 'lordd.py').resolve())
    m.HOME = tmp_path / 'lord'; m.HOME.mkdir()
    cli, temp = tmp_path / 'cli', tmp_path / 'temp'; cli.mkdir(); temp.mkdir()
    env = {'HOME': str(cli), 'CODEX_HOME': str(cli), 'TMPDIR': str(temp), 'PATH': '/opt/homebrew/bin:/usr/bin:/bin'}
    targets = [root / name for root in (plugin, alias) for name in ('lordd.py', 'sandbox/grok.sbpl', 'sandbox/tests.sbpl')]
    targets.append(work / 'neighbor')
    code = """import json,sys
out=[]
for name in sys.argv[1:]:
    try:
        with open(name,'a') as file:file.write('')
        out.append(True)
    except OSError:out.append(False)
print(json.dumps(out))
"""
    if mode in ('grok', 'tests'):
        profile, params = m.grok_profile(str(work), False, env) if mode == 'grok' else m.tests_profile(str(work), [], env)
        command = ['/usr/bin/sandbox-exec', '-p', profile, *params]
    else:
        a = m.Agent({'agentId': 'probe', 'family': 'gpt', 'model': 'fake', 'cwd': str(work), 'dirs': [str((alias / 'sandbox').resolve())], 'net': mode == 'net', 'readonly': mode == 'readonly'})
        config = m.gpt_permissions(a); name = config['default_permissions']; profile = config['permissions'][name]
        lines = ['default_permissions=' + json.dumps(name), f'[permissions.{name}.filesystem]']
        lines += [json.dumps(k) + '=' + json.dumps(v) for k, v in profile['filesystem'].items()]
        lines += [f'[permissions.{name}.network]', 'enabled=' + str(profile['network']['enabled']).lower()]
        (cli / 'config.toml').write_text('\n'.join(lines) + '\n')
        command = [codex, 'sandbox', '-P', name, '--']
    result = subprocess.run([*command, '/opt/homebrew/bin/python3.12', '-I', '-c', code, *map(str, targets)], env=env, cwd=work, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-500:]
    measured = json.loads(result.stdout)
    assert measured[:6] == [False] * 6
    assert measured[-1] is (mode != 'readonly')


@pytest.mark.parametrize('family', ['gpt', 'grok', 'gemini'])
@pytest.mark.parametrize('root', ['cwd', 'dir', 'temp'])
@pytest.mark.parametrize('linked', [False, True])
def test_r7_vendor_in_any_agents_folders_is_refused(scope, monkeypatch, family, root, linked):
    d, a, m, work, extra, outside = scope
    temp = outside / 'temp'; temp.mkdir(); monkeypatch.setenv('TMPDIR', str(temp))
    folder = {'cwd': work, 'dir': extra, 'temp': temp}[root]
    path = folder / 'bin' / 'vendor'; path.parent.mkdir(); path.write_text('#!/bin/sh\nexit 0\n'); path.chmod(0o755)
    if linked:
        alias = outside / 'vendor'; alias.symlink_to(path); program = str(alias)
    else: program = str(path)
    d.config = {family: {'program': [program]}}; d.stopping = False
    victim = m.Agent({'agentId': 'victim', 'family': family, 'model': 'fake', 'cwd': str(outside)})
    async def unexpected(*args, **kwargs): raise AssertionError('untrusted program reached execution')
    monkeypatch.setattr(m.asyncio, 'create_subprocess_exec', unexpected)
    async def run():
        with pytest.raises(RuntimeError, match='vendor program') as error:
            await m.Vendor(d, family, victim).ensure()
        assert program in str(error.value)
    asyncio.run(run())


def test_r7_session_path_vendor_is_absolute_when_started(tests_backend, rig, monkeypatch):
    d, m, launched = tests_backend
    safe = rig.home / 'bin'; safe.mkdir(); program = safe / 'vendor'; program.symlink_to(PYTHON)
    d.config['gpt']['program'][0] = 'vendor'
    monkeypatch.setitem(m, 'SESSION_PATH', str(safe) + ':/usr/bin:/bin')
    async def run():
        try:
            await d.open({'agentId': 'receiver', 'family': 'gpt', 'cwd': str(rig.home / 'work')})
            argv = next(argv for argv, _ in launched if 'app-server' in argv)
            assert argv[0] == str(program)
        finally: await d.close()
    asyncio.run(run())


def test_r7_planted_session_vendor_is_refused_before_it_runs(tests_backend, rig, monkeypatch):
    d, m, _ = tests_backend
    project = rig.home / 'project'; bindir = project / 'bin'; bindir.mkdir(parents=True)
    escaped = rig.home / 'escaped'
    program = bindir / 'codex'
    program.write_text(f'#!{PYTHON}\nimport os\nfrom pathlib import Path\nPath({str(escaped)!r}).write_text("escaped")\nos.execv({PYTHON!r}, {[PYTHON, str(ROOT / "tests/fakes/vendor.py"), "gpt", str(rig.data)]!r} + ["app-server"])\n')
    program.chmod(0o755)
    d.agents['sender'] = m['Agent']({'agentId': 'sender', 'family': 'gpt', 'model': 'fake', 'cwd': str(project)})
    d.agents['sender'].opened = True
    d.config['gpt'].pop('program')
    monkeypatch.setitem(m, 'SESSION_PATH', str(bindir) + ':/usr/bin:/bin')
    async def run():
        try:
            with pytest.raises(RuntimeError, match='vendor program') as error:
                await d.open({'agentId': 'receiver', 'family': 'gpt', 'cwd': str(rig.home / 'work')})
            assert str(program) in str(error.value) and not escaped.exists()
        finally: await d.close()
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['readonly-gpt', 'readonly-grok', 'nosandbox', 'gemini', 'journal', 'closed', 'writable', 'incoming'])
@pytest.mark.parametrize('folder', ['cwd', 'dir'])
def test_r8_vendor_refusal_counts_only_writable_open_agents(scope, monkeypatch, mode, folder):
    d, _, m, work, extra, outside = scope
    monkeypatch.setenv('TMPDIR', str(outside / 'temp'))
    target = work if folder == 'cwd' else extra
    program = target / 'vendor'; program.write_text('#!/bin/sh\nexit 0\n'); program.chmod(0o755)
    a = m.Agent({'agentId': 'owner', 'family': 'gemini' if mode == 'gemini' else 'grok' if mode == 'readonly-grok' else 'gpt',
        'model': 'fake', 'cwd': str(work), 'dirs': [str(extra)], 'readonly': mode.startswith('readonly'), 'nosandbox': mode == 'nosandbox'})
    a.opened = mode not in ('journal', 'incoming')
    if mode == 'closed': a.state = 'closed'
    d.agents = {'owner': a}; d.config = {'gpt': {'program': str(program)}}
    if mode in ('writable', 'incoming'):
        with pytest.raises(RuntimeError, match='vendor program') as error: d.vendor_program('gpt', a if mode == 'incoming' else None)
        assert a.agentId in str(error.value) and str(target) in str(error.value)
    else: assert d.vendor_program('gpt') == [str(program)]


@pytest.mark.parametrize('protected', ['home', 'plugin'])
@pytest.mark.parametrize('folder', ['cwd', 'dir'])
@pytest.mark.parametrize('mode', ['workspace', 'net', 'readonly', 'tests', 'nosandbox'])
def test_r8_settings_name_protected_read_only_folders(scope, monkeypatch, protected, folder, mode):
    d, a, m, work, _, outside = scope
    plugin = outside / 'plugin'; plugin.mkdir(); monkeypatch.setattr(m, 'HERE', plugin / 'lordd.py')
    root = m.HOME if protected == 'home' else plugin
    path = root / 'child'; path.mkdir()
    a.cwd = str(path if folder == 'cwd' else work)
    a.record['dirs'] = [str(path)] if folder == 'dir' else []
    if mode != 'workspace': a.record[mode] = True
    d.config = {'gpt': {}, 'vendor_home': str(outside)}
    line = m.settings_line(d.settings(a))
    if mode == 'nosandbox': assert 'read-only folders' not in line
    else: assert f'read-only folders {path}' in line


def test_r8_native_test_skips_when_codex_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(test_round7.shutil, 'which', lambda *args, **kwargs: None)
    def unexpected(*args, **kwargs): raise AssertionError('missing codex reached native execution')
    monkeypatch.setattr(test_round7.subprocess, 'run', unexpected)
    with pytest.raises(pytest.skip.Exception, match='codex'):
        test_round7.test_r7_native_profiles_protect_both_plugin_spellings(tmp_path, monkeypatch, 'workspace')


def test_r8_native_test_uses_resolved_production_paths(tmp_path, monkeypatch):
    original = test_round7.load
    class Checked(Exception): pass
    def capture(name):
        m = original(name)
        def profile(a):
            assert m.HERE == m.HERE.resolve()
            assert all(path == str(Path(path).resolve()) for path in a.record['dirs'])
            raise Checked()
        m.gpt_permissions = profile
        return m
    monkeypatch.setattr(test_round7, 'load', capture)
    monkeypatch.setattr(test_round7.shutil, 'which', lambda *args, **kwargs: '/tmp/synthetic-codex')
    with pytest.raises(Checked):
        test_round7.test_r7_native_profiles_protect_both_plugin_spellings(tmp_path, monkeypatch, 'workspace')


@pytest.mark.parametrize('protected', ['home', 'plugin'])
@pytest.mark.parametrize('readonly', [False, True])
def test_r9_grok_settings_name_read_only_folders(scope, monkeypatch, protected, readonly):
    d, a, m, _, _, outside = scope
    plugin = outside / 'plugin'; plugin.mkdir(); monkeypatch.setattr(m, 'HERE', plugin / 'lordd.py')
    root = m.HOME if protected == 'home' else plugin
    path = root / 'child'; path.mkdir()
    a.family = a.record['family'] = 'grok'; a.cwd = str(path); a.record['dirs'] = []
    a.record['readonly'] = readonly
    d.config = {'grok': {}, 'vendor_home': str(outside)}
    assert f'read-only folders {path}' in m.settings_line(d.settings(a))

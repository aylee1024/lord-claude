"""Output redaction, folder tickets and vendor startup paths."""
import asyncio
import json
from pathlib import Path
import random
import shutil
import string
import subprocess

import pytest

from test_lordd import ROOT, PYTHON, load, rig, tests_backend, grok_home
from test_round3 import scope


def test_r7_multiline_environment_secret_stays_out_of_rows(scope, monkeypatch):
    _, a, m, *_ = scope
    rng = random.Random(7)
    body = '\n'.join(''.join(rng.choice(string.ascii_letters + string.digits + '+/') for _ in range(64)) for _ in range(26))
    secret = '-----BEGIN PRIVATE KEY-----\n' + body + '\n-----END PRIVATE KEY-----'
    monkeypatch.setenv('DEPLOY_PRIVATE_KEY', secret)
    monkeypatch.setattr(m, 'REDACT', m.Redactor())
    output = 'build log\n' * 50 + 'DEPLOY_PRIVATE_KEY=' + secret + '\n' + 'done: 42 files\n' * 40
    async def run():
        a.rows['r'] = asyncio.get_running_loop().create_future()
        a.row_done('r', output)
        shown = (await a.rows['r'])['output']
        assert not set(body.splitlines()).intersection(shown.splitlines())
        assert shown.endswith('done: 42 files\n')
    asyncio.run(run())


@pytest.mark.parametrize('held', [False, True])
@pytest.mark.parametrize('folder', ['cwd', 'dir'])
def test_r7_existing_message_folders_are_not_newly_applied(tests_backend, rig, held, folder):
    d, m, _ = tests_backend
    work = rig.home / 'work'; slot = work / 'slot'; slot.mkdir()
    async def run():
        try:
            await d.open({'agentId': 'receiver', 'family': 'gpt', 'cwd': str(work), 'dirs': [str(slot)]})
            a = d.agents['receiver']; vendor = d.backend(a); generation = vendor.generation
            if held: a.carry.append({'type': 'done', 'text': 'held'})
            path = work if folder == 'cwd' else slot
            d.confine_call(a, 'SendMessage', {'to': a.agentId, 'message': f'@{folder} {path}\nwork'}, m['delegation_access'](a))
            assert d.message_tickets(a)
            await d.open({'agentId': a.agentId, 'family': 'gpt', 'cwd': str(work), 'dirs': [str(slot)], 'pin_cwd': True})
            assert vendor.generation == generation
            assert len(d.message_tickets(a)) == 1
            assert len(list((m['HOME'] / 'launches').glob('*.json'))) == 1
        finally: await d.close()
    asyncio.run(run())


@pytest.mark.parametrize('apply_folder', [False, True])
@pytest.mark.parametrize('old_expiry', [False, True])
@pytest.mark.parametrize('option', ['cwd', 'dir'])
def test_r7_message_check_waits_for_new_folder_application(tests_backend, rig, apply_folder, old_expiry, option):
    d, m, _ = tests_backend
    work = rig.home / 'work'; slot = work / 'slot'; slot.mkdir()
    async def run():
        try:
            await d.open({'agentId': 'receiver', 'family': 'gpt', 'cwd': str(work)})
            a = d.agents['receiver']
            folder = work if option == 'cwd' else slot
            ticket = d.launch_folders(folders=[str(folder)], agent_id=a.agentId)
            d.message_tickets(a, add=ticket)
            path = m['HOME'] / 'launches' / f'{ticket}.json'
            if old_expiry:
                entry = json.loads(path.read_text()); entry['expires'] = 0; path.write_text(json.dumps(entry))
            applied = {'dirs': [str(slot)]} if option == 'dir' else {'pin_cwd': True}
            body = {'agentId': a.agentId, 'family': 'gpt', 'cwd': str(work), **(applied if apply_folder else {})}
            await d.open(body)
            consumed = apply_folder and option == 'dir'
            assert bool(d.message_tickets(a)) is not consumed
            assert path.exists() is not consumed
            for _ in range(2): await d.open(body)
            await d.backend(a).close(); a.generation = 0
            await d.begin(a, {'text': 'PING'})
            await asyncio.wait_for(a.finished.wait(), 5)
            assert a.state == 'idle' and a.answer == 'PONG'
        finally: await d.close()
    asyncio.run(run())


def test_r7_only_pinned_tickets_in_the_open_snapshot_are_consumed(tests_backend, rig, monkeypatch):
    d, m, _ = tests_backend
    work = rig.home / 'work'; first, later = work / 'first', work / 'later'
    first.mkdir(); later.mkdir()
    async def run():
        try:
            await d.open({'agentId': 'receiver', 'family': 'gpt', 'cwd': str(work)})
            a = d.agents['receiver']; vendor = d.backend(a)
            pending = d.launch_folders(folders=[str(first), str(later)], agent_id=a.agentId)
            d.message_tickets(a, add=pending)
            await d.open({'agentId': a.agentId, 'family': 'gpt', 'cwd': str(work), 'dirs': [str(first)]})
            assert d.message_tickets(a) == [pending]
            original = vendor.ensure; arriving = []
            async def ensure():
                ticket = d.launch_folders(folders=[str(work)], agent_id=a.agentId)
                d.message_tickets(a, add=ticket); arriving.append(ticket)
                await original()
            monkeypatch.setattr(vendor, 'ensure', ensure)
            await d.open({'agentId': a.agentId, 'family': 'gpt', 'cwd': str(work), 'dirs': [str(later)]})
            assert d.message_tickets(a) == arriving
        finally: await d.close()
    asyncio.run(run())


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


@pytest.mark.parametrize('name', ['README.md', 'PRIVACY.md'])
def test_r7_docs_describe_hardening_and_remaining_routes(name):
    text = (ROOT / name).read_text().lower()
    for phrase in ('not a security boundary', 'as your user', 'read your files', "processes' environments", 'shell start-up files', 'project tool folders', "claude code's settings", 'option lines', 'launch types', 'folder', 'control key', 'tool identity', 'records', 'plugin'):
        assert phrase in text


def test_r7_contract_matches_output_tickets_and_vendor_paths():
    text = (ROOT / 'CONTRACT.md').read_text()
    assert '8,192 characters of lookback' in text
    assert 'a warm vendor or a held result' in text
    assert 'absolute paths before running them' in text

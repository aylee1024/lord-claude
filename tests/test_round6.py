"""Process identity, delegated-folder lifetimes and bounded output tails."""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from test_lordd import ROOT, load, grok_home, rig, tests_backend
from test_round3 import scope
from test_round5 import PREFIXES


@pytest.mark.parametrize('failure', ['outside', 'lookup'])
def test_r6_mcp_requires_peer_tree_even_with_the_right_token(tests_backend, rig, monkeypatch, failure):
    d, m, _ = tests_backend
    async def run():
        server = None
        try:
            await d.open({'agentId': 'victim', 'family': 'gpt', 'cwd': str(rig.home / 'work'), 'nosandbox': True})
            if failure == 'lookup':
                async def broken(): raise RuntimeError('synthetic lookup failure')
                monkeypatch.setitem(m, 'process_table', broken)
            path = rig.home / 'peer.sock'
            server = await asyncio.start_unix_server(d.http, str(path))
            reader, writer = await asyncio.open_unix_connection(str(path))
            body = json.dumps({'agentId': 'victim', 'token': d.agents['victim'].token, 'name': 'list_agents'}).encode()
            writer.write(b'POST /mcp HTTP/1.1\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
            await writer.drain()
            response = await reader.read()
            writer.close(); await writer.wait_closed()
            assert not response.startswith(b'HTTP/1.1 200'), 'outside caller adopted the victim identity'
            assert b'unknown process' in response
        finally:
            if server: server.close(); await server.wait_closed()
            await d.close()
    asyncio.run(run())


@pytest.mark.parametrize('target', ['sender', 'receiver'])
@pytest.mark.parametrize('restart', [False, True])
def test_r6_message_folder_is_checked_again_at_open(tests_backend, rig, target, restart):
    d, m, _ = tests_backend
    work = rig.home / 'work'
    slot, outside = work / 'slot', rig.home / 'outside'
    slot.mkdir(); outside.mkdir()
    async def run():
        current = d
        try:
            for name in ('sender', 'receiver'):
                await d.open({'agentId': name, 'family': 'gpt', 'cwd': str(work)})
            sender = d.agents['sender']
            data = {'to': target, 'message': f'@dir {slot}\nwork'}
            d.confine_call(sender, 'SendMessage', data, m['delegation_access'](sender))
            forwarded = data['message'].split('\n')[0].split(' ', 1)[1]
            if restart: await d.close(); current = m['Daemon']()
            slot.rmdir(); slot.symlink_to(outside, target_is_directory=True)
            with pytest.raises(ValueError, match='changed after it was checked'):
                await current.open({'agentId': target, 'family': 'gpt', 'cwd': str(work), 'dirs': [forwarded]})
        finally: await current.close()
    asyncio.run(run())


def test_r6_pending_message_ticket_is_checked_only_at_applying_open(tests_backend, rig):
    d, m, _ = tests_backend
    work = rig.home / 'work'
    slot, outside = work / 'slot', rig.home / 'outside'
    slot.mkdir(); outside.mkdir()
    async def run():
        try:
            await d.open({'agentId': 'sender', 'family': 'gpt', 'cwd': str(work)})
            a = d.agents['sender']
            data = {'to': 'sender', 'message': f'@dir {slot}\nwork'}
            d.confine_call(a, 'SendMessage', data, m['delegation_access'](a))
            await d.backend(a).close(); a.generation = 0
            slot.rmdir(); slot.symlink_to(outside, target_is_directory=True)
            await d.begin(a, {'text': 'PING'})
            await asyncio.wait_for(a.finished.wait(), 5)
            assert a.answer == 'PONG' and d.message_tickets(a)
            with pytest.raises(ValueError, match='changed after it was checked'):
                await d.open({'agentId': a.agentId, 'family': 'gpt', 'cwd': str(work), 'dirs': [str(slot)]})
        finally: await d.close()
    asyncio.run(run())


def test_r6_message_folder_check_reaches_an_already_running_peer_daemon(tests_backend, rig):
    d, m, _ = tests_backend
    work = rig.home / 'work'; slot = work / 'slot'; slot.mkdir()
    outside = rig.home / 'outside'; outside.mkdir()
    async def run():
        peer = None
        try:
            for name in ('sender', 'receiver'):
                await d.open({'agentId': name, 'family': 'gpt', 'cwd': str(work)})
            peer = m['Daemon']()
            await peer.open({'agentId': 'receiver', 'family': 'gpt', 'cwd': str(work)})
            a = d.agents['sender']
            d.confine_call(a, 'SendMessage', {'to': 'receiver', 'message': f'@dir {slot}\nwork'}, m['delegation_access'](a))
            slot.rmdir(); slot.symlink_to(outside, target_is_directory=True)
            with pytest.raises(ValueError, match='changed after it was checked'):
                await peer.open({'agentId': 'receiver', 'family': 'gpt', 'cwd': str(work), 'dirs': [str(slot)]})
        finally:
            if peer: await peer.close()
            await d.close()
    asyncio.run(run())


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


@pytest.mark.parametrize('same_start', [True, False])
def test_r6_kill_tracks_term_process_birth_without_reusing_a_pid(scope, monkeypatch, same_start):
    _, _, m, *_ = scope
    count, calls = [0], []
    class Process:
        pid, returncode = 900001, None
        async def wait(self): pass
    process = Process()
    async def table():
        count[0] += 1
        if count[0] == 2: process.returncode = 0
        return ({900001: [900002]} if count[0] == 1 else {}), {}
    async def markers(): return m.ProcessMarkers()
    def start(pid): return (100, 123) if count[0] == 1 or same_start else (100, 124)
    monkeypatch.setattr(m, 'process_table', table)
    monkeypatch.setattr(m, 'process_markers', markers)
    monkeypatch.setattr(m, 'process_start', start, raising=False)
    monkeypatch.setattr(m.os, 'kill', lambda pid, sig: calls.append((pid, sig)))
    monkeypatch.setattr(m.os, 'killpg', lambda *args: None)
    asyncio.run(m.kill_tree(process, agent_id='caller'))
    assert (900002, signal.SIGTERM) in calls
    assert ((900002, signal.SIGKILL) in calls) is same_start


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS process environment hiding')
def test_r6_term_ignoring_apple_child_dies_after_its_parent_exits(scope, tmp_path):
    _, _, m, *_ = scope
    async def run():
        marker = tmp_path / 'child'
        root = await asyncio.create_subprocess_exec('/opt/homebrew/bin/python3.12', '-c',
            "import subprocess,sys,time; p=subprocess.Popen(['/bin/sh','-c',\"trap '' TERM; exec /bin/sleep 300\"]); open(sys.argv[1],'w').write(str(p.pid)); time.sleep(300)",
            str(marker), start_new_session=True, env={'PATH': '/usr/bin:/bin'})
        child = None
        def alive(pid):
            try: os.kill(pid, 0); return True
            except ProcessLookupError: return False
        try:
            for _ in range(100):
                if marker.exists() and marker.read_text(): break
                await asyncio.sleep(.02)
            child = int(marker.read_text()); await asyncio.sleep(.1)
            await m.kill_tree(root, [child], agent_id='caller')
            for _ in range(50):
                if not alive(child): break
                await asyncio.sleep(.02)
            assert not alive(child), 'TERM-ignoring orphan survived KILL'
        finally:
            for pid in (child, root.pid):
                if pid:
                    try: os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError: pass
            await root.wait()
    asyncio.run(run())


@pytest.mark.parametrize('shape', ['json', 'progress'])
@pytest.mark.parametrize('site', ['row', 'stderr'])
def test_r6_long_line_keeps_its_tail_and_counts_the_omission(scope, shape, site):
    d, a, m, *_ = scope
    text = '{"result":"' + 'v' * 30000 + '","status":"FAILED: disk full"}' if shape == 'json' else '#' * 30000 + '\r99% still running'
    async def run():
        if site == 'row':
            a.rows['row'] = asyncio.get_running_loop().create_future()
            a.row_done('row', text)
            shown = (await a.rows['row'])['output']
            assert ('FAILED: disk full' if shape == 'json' else '99% still running') in shown
            note, tail = shown.split('\n', 1)
            omitted = int(note.split()[0][1:])
            assert omitted + len(tail) == len(text)
        else:
            vendor = m.Vendor(d, 'gpt', a); d.log = lambda text: None
            reader = asyncio.StreamReader(); reader.feed_data(text.encode()); reader.feed_eof()
            process = type('Process', (), {'stderr': reader})(); vendor.exec_process = process
            await vendor.stderr(process)
            assert ('FAILED: disk full' if shape == 'json' else '99% still running') in vendor.exec_error
    asyncio.run(run())


def test_r6_failed_launch_open_keeps_its_ticket_and_first_folders(tests_backend, rig):
    d, m, _ = tests_backend
    async def run():
        try:
            await d.open({'agentId': 'sender', 'family': 'gpt', 'cwd': str(rig.home / 'work')})
            a = d.agents['sender']
            ticket = d.confine_call(a, 'Agent', {'subagent_type': 'lord:gpt', 'prompt': 'work'}, m['delegation_access'](a))
            body = {'agentId': 'child', 'family': 'gpt', 'cwd': a.cwd, 'launch': ticket}
            with pytest.raises(ValueError, match='unknown gpt model'):
                await d.open({**body, 'model': 'not-a-model'})
            assert (m['HOME'] / 'launches' / f'{ticket}.json').exists()
            with pytest.raises(ValueError, match='another agent'):
                await d.open({**body, 'agentId': 'unrelated'})
            await d.open(body)
            assert not (m['HOME'] / 'launches' / f'{ticket}.json').exists()
        finally: await d.close()
    asyncio.run(run())


@pytest.mark.parametrize('options', [{}, {'net': True}, {'readonly': True}])
def test_r6_temporary_control_key_is_denied_by_gpt(scope, options):
    _, a, m, *_ = scope; a.record.update(options)
    cfg = m.gpt_permissions(a); fs = cfg['permissions'][cfg['default_permissions']]['filesystem']
    assert fs.get('/**/.control.header') == 'deny'
    assert fs.get(m.canon(m.HOME / '.control.header')) == 'deny'


@pytest.mark.parametrize('name', ['README.md', 'PRIVACY.md'])
def test_r6_docs_state_same_user_file_and_environment_access(name):
    text = (ROOT / name).read_text().lower()
    assert 'as your user' in text and 'environments' in text and 'read your files' in text


@pytest.mark.parametrize('failure', ['none', 'peer', 'lookup'])
@pytest.mark.parametrize('root', ['process', 'exec_process'])
def test_r6_valid_peer_and_each_lookup_failure(tests_backend, rig, monkeypatch, failure, root):
    d, m, _ = tests_backend
    async def run():
        server, vendor = None, None
        try:
            a = m['Agent']({'agentId': 'peer', 'family': 'gpt', 'cwd': str(rig.home / 'work'), 'model': 'fake'})
            d.agents[a.agentId] = a
            vendor = d.backend(a)
            setattr(vendor, root, type('Process', (), {'pid': os.getpid(), 'returncode': None})())
            if failure == 'peer': monkeypatch.setitem(m, 'peer_pid', lambda writer: None)
            if failure == 'lookup':
                async def broken(): raise RuntimeError('synthetic lookup failure')
                monkeypatch.setitem(m, 'process_table', broken)
            path = rig.home / 'valid.sock'
            server = await asyncio.start_unix_server(d.http, str(path))
            reader, writer = await asyncio.open_unix_connection(str(path))
            body = json.dumps({'agentId': a.agentId, 'token': a.token, 'name': 'list_agents'}).encode()
            writer.write(b'POST /mcp HTTP/1.1\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
            await writer.drain(); response = await reader.read()
            writer.close(); await writer.wait_closed()
            assert response.startswith(b'HTTP/1.1 200') is (failure == 'none')
        finally:
            if vendor: vendor.process = vendor.exec_process = None
            if server: server.close(); await server.wait_closed()
            await d.close()
    asyncio.run(run())


def test_r6_marked_detached_process_cannot_use_its_agents_token(tests_backend, rig):
    d, m, _ = tests_backend
    async def run():
        server = None
        try:
            await d.open({'agentId': 'victim', 'family': 'gpt', 'cwd': str(rig.home / 'work'), 'nosandbox': True})
            path = rig.home / 'detached.sock'
            server = await asyncio.start_unix_server(d.http, str(path))
            code = """import socket,sys
body=sys.stdin.buffer.read()
s=socket.socket(socket.AF_UNIX);s.connect(sys.argv[1])
s.sendall(b'POST /mcp HTTP/1.1\\r\\nContent-Length: '+str(len(body)).encode()+b'\\r\\n\\r\\n'+body)
out=b''
while part:=s.recv(4096):out+=part
print(int(out.startswith(b'HTTP/1.1 200')))
"""
            process = await asyncio.create_subprocess_exec('/opt/homebrew/bin/python3.12', '-c', code, str(path),
                stdin=-1, stdout=-1, stderr=-1, start_new_session=True,
                env={'PATH': '/usr/bin:/bin', 'LORD_AGENT_ID': 'victim', 'LORD_DAEMON_SOCKET': str(m['SOCKET'])})
            body = json.dumps({'agentId': 'victim', 'token': d.agents['victim'].token, 'name': 'list_agents'}).encode()
            output, _ = await asyncio.wait_for(process.communicate(body), 10)
            assert process.returncode == 0 and output.strip() == b'0', 'marked detached caller was accepted'
        finally:
            if server: server.close(); await server.wait_closed()
            await d.close()
    asyncio.run(run())


def test_r6_message_tickets_survive_failed_start_then_yield_to_own_pins(tests_backend, rig, monkeypatch):
    d, m, _ = tests_backend
    work = rig.home / 'work'; slot = work / 'slot'; slot.mkdir()
    async def run():
        try:
            await d.open({'agentId': 'sender', 'family': 'gpt', 'cwd': str(work)})
            a = d.agents['sender']
            d.confine_call(a, 'SendMessage', {'to': a.agentId, 'message': f'@dir {slot}\nwork'}, m['delegation_access'](a))
            tickets = list((m['HOME'] / 'launches').glob('*.json'))
            assert tickets
            vendor = d.backend(a); original = vendor.ensure
            async def broken(): raise RuntimeError('synthetic start failure')
            monkeypatch.setattr(vendor, 'ensure', broken)
            with pytest.raises(RuntimeError, match='synthetic start failure'):
                await d.open({'agentId': a.agentId, 'family': 'gpt', 'cwd': str(work), 'dirs': [str(slot)]})
            assert all(path.exists() for path in tickets)
            monkeypatch.setattr(vendor, 'ensure', original)
            await d.open({'agentId': a.agentId, 'family': 'gpt', 'cwd': str(work), 'dirs': [str(slot)]})
            assert not any(path.exists() for path in tickets)
            outside = rig.home / 'outside'; outside.mkdir()
            slot.rmdir(); slot.symlink_to(outside, target_is_directory=True)
            with pytest.raises(ValueError, match='now points to'):
                await d.open({'agentId': a.agentId, 'family': 'gpt', 'cwd': str(work)})
        finally: await d.close()
    asyncio.run(run())


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


@pytest.mark.parametrize('site', ['row', 'stderr'])
@pytest.mark.parametrize('prefix', PREFIXES)
def test_r6_prefix_and_value_cuts_keep_a_safe_tail(scope, site, prefix):
    d, a, m, *_ = scope
    secret = 'Q' * 800
    if prefix == 'AKIA': secret = 'Q' * 16
    if prefix == 'eyJ': secret = 'Q' * 30 + '.' + 'Q' * 600 + '.' + 'Q' * 100
    line = prefix + secret
    assert 'Q' * 16 not in m.REDACT(line)
    async def run():
        for offset in [*range(len(prefix)), len(prefix) + 1, len(prefix) + 63, len(prefix) + 128]:
            if offset >= len(line): continue
            trailer = ' ' * (2000 - (len(line) - offset) - 10) + ' tail kept'
            text = 'earlier ' * 1000 + line + trailer
            if site == 'row':
                a.rows['r'] = asyncio.get_running_loop().create_future(); a.row_done('r', text)
                shown = (await a.rows['r'])['output']
            else:
                vendor = m.Vendor(d, 'gpt', a); d.log = lambda text: None
                reader = asyncio.StreamReader(); reader.feed_data(text.encode()); reader.feed_eof()
                process = type('Process', (), {'stderr': reader})(); vendor.exec_process = process
                await vendor.stderr(process); shown = vendor.exec_error
            assert 'Q' * 16 not in shown and shown.endswith('tail kept'), (prefix, offset)
    asyncio.run(run())

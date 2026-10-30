"""Identity behaviour."""
import asyncio
import json
import os
import pytest
from test_lordd import grok_home, rig, tests_backend


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

#!/opt/homebrew/bin/python3.12
"""Deterministic, strict peers for app-server, ACP, and stream-json."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from urllib.parse import quote

FAMILY, DATA = sys.argv[1], Path(sys.argv[2])
DATA.mkdir(parents=True, exist_ok=True)
SESSIONS, TURNS = {}, {}


def audit(**value):
    with (DATA / 'audit.jsonl').open('a') as file:
        file.write(json.dumps({'family': FAMILY, 'pid': os.getpid(), **value}) + '\n')


if sys.argv[3:4] == ['models']:
    for model in ('gemini-4-argon', 'gemini-3.8-flash'):
        for level in ('medium', 'high', 'low'):
            print(f'{model}-{level}\t{model} ({level})')
    sys.exit(0)

if sys.argv[3:5] == ['mcp', 'add']:
    audit(mcp_add=sys.argv[5:])
    sys.exit(0)

if sys.argv[3:5] == ['mcp', 'remove']:
    audit(mcp_remove=sys.argv[5:])
    sys.exit(0)


def send(value):
    print(json.dumps(value), flush=True)


def save(sid):
    (DATA / (sid + '.json')).write_text(json.dumps(SESSIONS[sid]))


def load(sid):
    SESSIONS[sid] = json.loads((DATA / (sid + '.json')).read_text())
    return SESSIONS[sid]


def update(sid, kind, **value):
    send({'method': 'session/update', 'params': {'sessionId': sid,
          'update': {'sessionUpdate': kind, **value}}})


async def mcp(server, name, arguments):
    # codex starts a tool server with a fixed list of its own variables plus the server's env, nothing else.
    env = {k: v for k, v in os.environ.items() if k in ('HOME', 'LOGNAME', 'PATH', 'SHELL', 'USER', 'LANG', 'LC_ALL', 'TERM', 'TMPDIR', 'TZ')} if FAMILY == 'gpt' else os.environ.copy()
    extra = server.get('env', {})
    env.update(extra if isinstance(extra, dict) else {v['name']: v['value'] for v in extra})
    proc = await asyncio.create_subprocess_exec(server['command'], *server['args'],
        stdin=-1, stdout=-1, stderr=-3, env=env)
    async def rpc(i, method, params):
        proc.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': i, 'method': method, 'params': params}) + '\n').encode())
        await proc.stdin.drain()
        while line := await proc.stdout.readline():
            value = json.loads(line)
            if value.get('id') == i:
                assert 'error' not in value, value
                return value['result']
        raise RuntimeError('MCP closed without a result')
    try:
        await rpc(1, 'initialize', {'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'fake', 'version': '1'}})
        tools = await rpc(2, 'tools/list', {})
        assert {t['name'] for t in tools['tools']} == {'spawn_agent', 'send_message', 'list_agents'}
        result = await rpc(3, 'tools/call', {'name': name, 'arguments': arguments})
        return result
    finally:
        proc.stdin.close()
        await proc.wait()


async def turn(sid, text, request_id=None):
    session = SESSIONS[sid]
    tid = str(uuid.uuid4())
    TURNS[sid] = {'id': tid, 'wake': asyncio.Event(), 'cancel': False, 'steer': None}
    live = TURNS[sid]
    narration = ''
    if FAMILY == 'gpt':
        send({'method': 'turn/started', 'params': {'threadId': sid, 'turn': {'id': tid}}})
        send({'id': request_id, 'result': {'turn': {'id': tid}}})
        send({'method': 'item/started', 'params': {'threadId': sid, 'item': {'type': 'commandExecution', 'command': 'fake work'}}})
    elif FAMILY == 'grok':
        update(sid, 'tool_call', toolCallId='native', title='fake work', status='in_progress')
    else:
        # agy reports each tool step twice: when it starts and when it ends.
        for state in ('ACTIVE', 'DONE'):
            send({'event': 'step_update', 'step_update': {'step_index': 2, 'state': state, 'step_type': 'tool', 'tool_name': 'run_command',
                  'tool_info': {'name': 'run_command', 'parameters': {'CommandLine': 'fake work'}}}})
    if '--signed-out' in sys.argv and FAMILY == 'gpt':
        send({'method': 'error', 'params': {'threadId': sid, 'turnId': tid, 'willRetry': False,
              'error': {'message': 'unexpected status 401 Unauthorized: Missing bearer or basic authentication in header'}}})
        send({'method': 'turn/completed', 'params': {'threadId': sid, 'turn': {'id': tid, 'status': 'failed'}}})
        return
    if text == 'FAIL' and FAMILY == 'gpt':
        # codex fails every later turn of a process whose settings reload failed.
        send({'method': 'turn/completed', 'params': {'threadId': sid, 'turn': {'id': tid, 'status': 'failed',
              'error': {'message': 'failed to load workspace requirements', 'codexErrorInfo': 'other'}}}})
        return
    if text == 'NARRATE' and FAMILY == 'grok':
        update(sid, 'agent_message_chunk', content={'type': 'text', 'text': 'Let me look first. '})
        update(sid, 'tool_call', toolCallId='native2', title='read notes', status='in_progress')
    if text == 'CRASH' and not session.get('crashed'):
        session['crashed'] = True
        save(sid)
        os._exit(9)
    if text == 'STEPS':
        # One command (long output ending in a token, exit code 2) and one file edit, shaped as each vendor reports them.
        output = 'x' * 3000 + ' sk-abcdefghijklmnopqrstuv'
        if FAMILY == 'gpt':
            command = {'type': 'commandExecution', 'id': 'c1', 'command': '/bin/zsh -lc "printf hi"', 'commandActions': [{'type': 'unknown', 'command': 'printf hi'}]}
            edit = {'type': 'fileChange', 'id': 'f1', 'changes': [{'path': 'a.txt', 'kind': 'add', 'diff': '+done'}]}
            for item, done in ((command, {'aggregatedOutput': output, 'exitCode': 2}), (edit, {})):
                send({'method': 'item/started', 'params': {'threadId': sid, 'item': item}})
                send({'method': 'item/completed', 'params': {'threadId': sid, 'item': {**item, **done}}})
        elif FAMILY == 'grok':
            update(sid, 'tool_call', toolCallId='g1', title='run_terminal_command', rawInput={'command': 'printf hi'},
                   _meta={'x.ai/tool': {'name': 'run_terminal_command', 'kind': 'execute', 'label': 'Run Command'}})
            update(sid, 'tool_call', toolCallId='g2', title='search_replace', rawInput={'file_path': 'a.txt', 'old_string': '', 'new_string': 'done'},
                   _meta={'x.ai/tool': {'name': 'search_replace', 'kind': 'edit', 'label': 'Edit'}})
            update(sid, 'tool_call_update', toolCallId='g1', status='completed', content=[{'type': 'content', 'content': {'type': 'text', 'text': output}}], rawOutput={'exit_code': 2})
            update(sid, 'tool_call_update', toolCallId='g2', status='completed', content=[{'type': 'diff', 'path': 'a.txt', 'oldText': '', 'newText': 'done'}])
        else:
            # agy narrates before its steps and joins every message into its final response.
            narration = 'Starting. '
            send({'event': 'step_update', 'step_update': {'step_type': 'agent_response', 'text_delta': narration}})
            for index, info, out in ((5, {'name': 'run_command', 'parameters': {'CommandLine': 'printf hi'}}, output + '\n(exit code 2)'),
                                     (6, {'name': 'write_file', 'parameters': {'Path': 'a.txt'}}, 'a.txt +done')):
                for state in ('ACTIVE', 'DONE'):
                    send({'event': 'step_update', 'step_update': {'step_index': index, 'state': state, 'step_type': 'tool', 'tool_name': info['name'],
                          'tool_info': {**info, **({'output': out} if state == 'DONE' else {})}}})
        answer = 'STEPS'
    elif text == 'COMMAND':
        # codex ends an interrupted turn but leaves the command it was running alive, in a session of its own.
        child = subprocess.Popen(['sleep', '300'], start_new_session=True)
        audit(command_pid=child.pid)
        await live['wake'].wait()
        answer = ''
    elif text in ('SLOW', 'TEXT_WAIT'):
        if text == 'TEXT_WAIT':
            if FAMILY == 'gpt':
                send({'method': 'item/agentMessage/delta', 'params': {'threadId': sid, 'itemId': tid, 'delta': 'partial'}})
            elif FAMILY == 'grok':
                update(sid, 'agent_message_chunk', content={'type': 'text', 'text': 'partial'})
            else:
                send({'event': 'step_update', 'step_update': {'step_type': 'agent_response', 'text_delta': 'partial'}})
        await live['wake'].wait()
        answer = live['steer'] or ''
    elif text in ('TOOL', 'ERROR_TOOL', 'LIST', 'MESSAGE', 'CLAUDE_TOOL', 'PARALLEL'):
        if text == 'LIST':
            name, args = 'list_agents', {}
        elif text == 'MESSAGE':
            name, args = 'send_message', {'to': 'friend', 'text': 'hello'}
        else:
            name, args = 'spawn_agent', {'type': 'lord:gpt', 'prompt': 'child', 'description': 'probe', 'model': 'gpt-6-luna', 'effort': 'low'}
            if text == 'CLAUDE_TOOL':
                args.update(type='general-purpose', model='haiku')
        if text == 'PARALLEL':
            answer = json.dumps(await asyncio.gather(mcp(session['server'], name, args), mcp(session['server'], name, args)))
        else:
            answer = json.dumps(await mcp(session['server'], name, args))
    elif text == 'REMEMBER':
        answer = session.get('memory', 'missing')
    else:
        answer = 'PONG' if text == 'PING' else text
        session['memory'] = text
    if not live['cancel']:
        if FAMILY == 'gpt':
            for part in (answer[:2], answer[2:]):
                send({'method': 'item/agentMessage/delta', 'params': {'threadId': sid, 'itemId': tid, 'delta': part}})
                await asyncio.sleep(.025)
            send({'method': 'item/completed', 'params': {'threadId': sid, 'item': {'id': tid, 'type': 'agentMessage', 'text': answer, 'phase': 'final_answer'}}})
            session['tokens'] = session.get('tokens', 0) + 10
            send({'method': 'thread/tokenUsage/updated', 'params': {'threadId': sid, 'tokenUsage': {'total': {'inputTokens': session['tokens'], 'outputTokens': session['tokens'], 'cachedInputTokens': 0}, 'last': {'inputTokens': 10, 'outputTokens': 10, 'cachedInputTokens': 0}}}})
        elif FAMILY == 'grok':
            update(sid, 'agent_message_chunk', content={'type': 'text', 'text': answer})
        else:
            send({'event': 'step_update', 'step_update': {'step_type': 'agent_response', 'text_delta': answer[:2]}})
            await asyncio.sleep(.025)
            send({'event': 'step_update', 'step_update': {'step_type': 'agent_response', 'text_delta': answer[2:]}})
    save(sid)
    if FAMILY == 'gpt':
        send({'method': 'turn/completed', 'params': {'threadId': sid, 'turn': {'id': tid, 'status': 'interrupted' if live['cancel'] else 'completed'}}})
    elif FAMILY == 'grok':
        row = {'inputTokens': 10, 'outputTokens': 5, 'cachedReadTokens': 2}
        folder = (Path(os.environ['GROK_HOME']) if os.environ.get('GROK_HOME') else DATA / '.grok') / 'sessions' / quote(session['cwd'], safe='') / sid
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'usage.json').write_text(json.dumps({'session': row, 'turns': [row]}))
        send({'id': request_id, 'result': {'stopReason': 'cancelled' if live['cancel'] else 'end_turn'}})
    else:
        send({'event': 'result', 'result': {'status': 'SUCCESS', 'response': narration + answer, 'usage': {'input_tokens': 10, 'output_tokens': 5, 'cache_read_tokens': 2}}})


async def handle(value):
    method, p, rid = value.get('method'), value.get('params', {}), value.get('id')
    audit(method=method or value.get('event'), params=p)
    result = {}
    if FAMILY == 'gemini':
        assert value['event'] == 'user' and isinstance(value['message']['content'], str)
        await turn(AGY_SESSION, value['message']['content'])
        return
    if method != 'initialize' and not SESSIONS.get('initialized'):
        send({'id': rid, 'error': {'code': -32000, 'message': 'initialize required'}})
        return
    if method == 'initialize':
        if '--fail-init-once' in sys.argv and not (DATA / 'init-failed').exists():
            (DATA / 'init-failed').touch()
            send({'id': rid, 'error': {'code': -32603, 'message': 'temporary initialize failure'}})
            return
        SESSIONS['initialized'] = True
        result = {'protocolVersion': 1, 'agentCapabilities': {'loadSession': True}}
    elif method in ('initialized', 'session/cancel'):
        if method == 'session/cancel':
            live = TURNS[p['sessionId']]
            live['cancel'] = True
            live['wake'].set()
        return
    elif method == 'model/list':
        if not SESSIONS.get('initialized'):
            send({'id': rid, 'error': {'code': -32000, 'message': 'initialize required'}})
            return
        result = {'data': [{'id': model, 'isDefault': model == 'gpt-6-luna', 'supportedReasoningEfforts': [{'reasoningEffort': effort} for effort in efforts]}
                    for model, efforts in [('gpt-6.1-sol', ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']), ('gpt-6-luna', ['low', 'medium', 'high'])]]}
    elif method in ('thread/start', 'session/new'):
        assert 'dynamicTools' not in p and '_meta' not in p
        if '--signed-out' in sys.argv and FAMILY == 'grok':
            send({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32000, 'message': 'Authentication required', 'data': 'no auth method id provided'}})
            return
        sid = str(uuid.uuid4())
        if FAMILY == 'gpt':
            assert p['approvalPolicy'] == 'never' and p['sandbox'] in ('workspace-write', 'read-only', 'danger-full-access')
            server = p['config']['mcp_servers']['lord']
        else:
            server = p['mcpServers'][0]
        SESSIONS[sid] = {'memory': '', 'server': server, 'cwd': p['cwd']}
        save(sid)
        result = {'thread': {'id': sid}} if FAMILY == 'gpt' else {'sessionId': sid}
        if FAMILY == 'grok':
            result['configOptions'] = [{'id': 'reasoning_effort', 'currentValue': 'xhigh',
                                        'options': [{'value': v} for v in ('xhigh', 'high', 'medium', 'low')]}]
            if '--list-models' in sys.argv:
                result['configOptions'].append({'id': 'model', 'currentValue': 'grok-4.7', 'options': [{'value': v} for v in ('grok-4.5', 'grok-4.7')]})
    elif method in ('thread/resume', 'session/load'):
        sid = p.get('threadId') or p['sessionId']
        load(sid)
        result = {'thread': {'id': sid}} if FAMILY == 'gpt' else {'sessionId': sid}
        if FAMILY == 'grok':
            update(sid, 'agent_message_chunk', content={'type': 'text', 'text': 'historical replay'})
    elif method == 'session/set_config_option':
        assert p['configId'] in ('model', 'reasoning_effort')
        if p['configId'] == 'reasoning_effort':
            assert p['value'] in ('low', 'medium', 'high', 'xhigh')
    elif method in ('turn/start', 'session/prompt'):
        sid = p.get('threadId') or p['sessionId']
        if FAMILY == 'gpt':
            assert p['model'] and p['effort'] in ('low', 'medium', 'high', 'xhigh', 'max')
        await turn(sid, (p.get('input') or p['prompt'])[0]['text'], rid)
        return
    elif method == 'turn/interrupt':
        live = TURNS[p['threadId']]
        assert live['id'] == p['turnId']
        live['cancel'] = True
        live['wake'].set()
    elif method == 'turn/steer':
        live = TURNS[p['threadId']]
        assert live['id'] == p['expectedTurnId']
        live['steer'] = p['input'][0]['text']
        live['wake'].set()
    elif method == 'thread/archive':
        audit(archived=p['threadId'])
    else:
        raise AssertionError('Unexpected method: ' + str(method))
    if rid is not None:
        send({'jsonrpc': '2.0', 'id': rid, 'result': result})


async def main():
    global AGY_SESSION
    audit(launch=True, argv=sys.argv[3:], path=os.environ.get('PATH', ''), grok_env={k: v for k, v in os.environ.items() if k.startswith('GROK_')}, env={k: v for k, v in os.environ.items() if 'must-not-reach' in v})
    print('fixture Bearer fake-bearer-value', file=sys.stderr, flush=True)
    if FAMILY == 'gemini':
        args = sys.argv[3:]
        assert '--effort' in args and '--model' in args
        AGY_SESSION = args[args.index('--conversation') + 1] if '--conversation' in args else str(uuid.uuid4())
        if '--conversation' in args:
            load(AGY_SESSION)
        else:
            # agy's lord tools come from the user-level entry; its server reads the agent id and token from this env.
            SESSIONS[AGY_SESSION] = {'memory': '', 'cwd': os.getcwd(),
                                     'server': {'command': sys.executable, 'args': [str(Path(__file__).resolve().parents[2] / 'lordd.py'), '--mcp'], 'env': {}}}
            save(AGY_SESSION)
        folder = DATA / '.gemini/antigravity-cli/conversations'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (AGY_SESSION + '.db')).touch()
        send({'event': 'init', 'conversation_id': AGY_SESSION, 'init': {'model': args[args.index('--model') + 1]}})
    tasks = set()
    reader = asyncio.StreamReader()
    await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    while line := await reader.readline():
        task = asyncio.create_task(handle(json.loads(line)))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        task.add_done_callback(lambda t: os._exit(31) if t.exception() else None)


asyncio.run(main())

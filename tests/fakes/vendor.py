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
    if text == 'HELPER_BUSY' and FAMILY == 'gpt':
        # A helper edits a file, then the parent's model is at capacity.
        child = 'child-' + sid[:8]
        send({'method': 'item/started', 'params': {'threadId': sid, 'item': {'type': 'subAgentActivity', 'id': 'h1', 'kind': 'started', 'agentThreadId': child, 'agentPath': '/root/fix', 'model': 'gpt-6-luna'}}})
        send({'method': 'item/completed', 'params': {'threadId': child, 'item': {'type': 'fileChange', 'id': 'k2', 'changes': [{'path': '/w/out.txt', 'kind': 'add', 'diff': '+y'}]}}})
        send({'method': 'turn/completed', 'params': {'threadId': sid, 'turn': {'id': tid, 'status': 'failed', 'error': {'message': 'Selected model is at capacity', 'codexErrorInfo': 'serverOverloaded'}}}})
        return
    if text in ('CYBER', 'BUSY', 'BUSY_EDIT', 'ERRFIRST', 'SPENT') and FAMILY == 'gpt':
        # A failed turn as codex classifies it (codexErrorInfo); BUSY and BUSY_EDIT fail as "at capacity", BUSY_EDIT
        # after a file edit; ERRFIRST sends the error before the turn's completion, which then carries none.
        if text in ('CYBER', 'BUSY_EDIT'):
            item = {'type': 'fileChange', 'id': 'e1', 'changes': [{'path': 'a.txt', 'kind': 'add', 'diff': '+x'}]} if text == 'BUSY_EDIT' else \
                   {'type': 'commandExecution', 'id': 'c1', 'command': '/bin/zsh -lc "cat probe.py"', 'commandActions': [{'type': 'read', 'command': 'cat probe.py', 'name': 'probe.py', 'path': 'probe.py'}], 'cwd': session['cwd']}
            send({'method': 'item/completed', 'params': {'threadId': sid, 'item': {**item, 'aggregatedOutput': '', 'exitCode': 0}}})
        if text == 'SPENT':
            send({'method': 'account/rateLimits/updated', 'params': {'rateLimits': {'limitId': 'codex', 'primary': {'usedPercent': 100, 'windowDurationMins': 10080, 'resetsAt': int(__import__('time').time()) + 86400}, 'secondary': None}}})
            send({'method': 'account/rateLimits/updated', 'params': {'rateLimits': {'limitId': 'reserve', 'primary': {'usedPercent': 50, 'windowDurationMins': 10080, 'resetsAt': int(__import__('time').time()) + 3600}}}})
        kind = {'CYBER': 'cyberPolicy', 'BUSY': 'serverOverloaded', 'BUSY_EDIT': 'serverOverloaded', 'ERRFIRST': 'usageLimitExceeded', 'SPENT': 'usageLimitExceeded'}[text]
        message = {'cyberPolicy': 'This content was flagged for possible cybersecurity risk', 'serverOverloaded': 'Selected model is at capacity'}.get(kind, 'You have hit your usage limit')
        error = {'message': message, 'codexErrorInfo': kind}
        if text == 'ERRFIRST':
            error = {'message': 'stream ended', 'codexErrorInfo': {'responseStreamDisconnected': {'httpStatusCode': None}}}
            send({'method': 'error', 'params': {'threadId': sid, 'turnId': tid, 'willRetry': False, 'error': error}})
        send({'method': 'turn/completed', 'params': {'threadId': sid, 'turn': {'id': tid, 'status': 'failed', **({} if text == 'ERRFIRST' else {'error': error})}}})
        return
    if text.startswith('Continue with the task above') and FAMILY == 'gpt':
        text = 'RECOVERED'
    if text == 'FAIL' and FAMILY == 'gpt':
        # codex fails every later turn of a process whose settings reload failed.
        send({'method': 'turn/completed', 'params': {'threadId': sid, 'turn': {'id': tid, 'status': 'failed',
              'error': {'message': 'failed to load workspace requirements', 'codexErrorInfo': 'other'}}}})
        return
    if text == 'NARRATE' and FAMILY == 'grok':
        update(sid, 'agent_message_chunk', content={'type': 'text', 'text': 'Let me look first. '})
        update(sid, 'tool_call', toolCallId='native2', title='read notes', status='in_progress')
    if text == 'QUOTA' and FAMILY == 'gemini':
        # agy reports a spent quota on stderr, as one line of text and one AGY_ERROR line, then exits.
        short = 'RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 104h27m0s.'
        print('error: Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 104h27m0s.', file=sys.stderr, flush=True)
        print('AGY_ERROR: ' + json.dumps({'short_error': short, 'status': 'RESOURCE_EXHAUSTED', 'error_code': 429, 'retryable': True}), file=sys.stderr, flush=True)
        await asyncio.sleep(.1)
        os._exit(1)
    if text == 'QUOTA_UNICODE' and FAMILY == 'gemini':
        # An error line holding a character a Latin-1 log cannot encode.
        # agy writes UTF-8 whatever the locale.
        sys.stderr.buffer.write(('AGY_ERROR: ' + json.dumps({'short_error': 'RESOURCE_EXHAUSTED (code 429): quota \u2713 reached. Resets in 1h0m0s.', 'error_code': 429}, ensure_ascii=False) + '\n').encode('utf-8'))
        sys.stderr.buffer.flush()
        await asyncio.sleep(.1)
        os._exit(1)
    if text in ('QUOTA_LATE', 'AGYFAIL') and FAMILY == 'gemini':
        # QUOTA_LATE: agy's stdout closes before its stderr line arrives. AGYFAIL: a failure that is not a quota.
        short = ('RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 1h0m0s.' if text == 'QUOTA_LATE'
                 else 'INTERNAL (code 500): boom')
        if text == 'QUOTA_LATE':
            os.close(1)
            await asyncio.sleep(.3)
        print('AGY_ERROR: ' + json.dumps({'short_error': short, 'error_code': 429 if text == 'QUOTA_LATE' else 500}), file=sys.stderr, flush=True)
        await asyncio.sleep(.1)
        os._exit(1)
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
    elif text.startswith('ROWS ') and FAMILY == 'gpt':
        # ROWS <count> <size>: that many commands, each printing <size> characters, as codex reports them.
        count, size = map(int, text.split()[1:3])
        for i in range(count):
            item = {'type': 'commandExecution', 'id': f'r{i}', 'command': f'/bin/zsh -lc "step {i}"', 'commandActions': [{'type': 'unknown', 'command': f'step {i}'}]}
            send({'method': 'item/started', 'params': {'threadId': sid, 'item': item}})
            send({'method': 'item/completed', 'params': {'threadId': sid, 'item': {**item, 'aggregatedOutput': (f'{i} ' + 'y' * size)[:size], 'exitCode': 0}}})
            await asyncio.sleep(0)
        answer = f'ROWS {count}'
    elif text == 'HEREDOC' and FAMILY == 'gpt':
        item = {'type': 'commandExecution', 'id': 'h1', 'command': "/bin/zsh -lc 'cat <<EOF\nline one\nline two\rline three\u2028line four\fline five\nEOF'", 'commandActions': [{'type': 'unknown', 'command': 'cat <<EOF\nline one\nline two\rline three\nEOF'}]}
        send({'method': 'item/started', 'params': {'threadId': sid, 'item': item}})
        send({'method': 'item/completed', 'params': {'threadId': sid, 'item': {**item, 'aggregatedOutput': 'line one\nline two\n', 'exitCode': 0}}})
        answer = 'HEREDOC'
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
    elif text == 'HELPER1' and FAMILY == 'gpt':
        # codex's older helper mode announces a helper only by its spawnAgent call.
        child = 'v1child-' + sid[:8]
        call = {'type': 'collabAgentToolCall', 'id': 'c1', 'tool': 'spawnAgent', 'status': 'completed', 'senderThreadId': sid, 'receiverThreadIds': [child], 'prompt': 'look', 'model': 'gpt-6-luna', 'reasoningEffort': 'low', 'agentsStates': {}}
        send({'method': 'item/completed', 'params': {'threadId': sid, 'item': call}})
        item = {'type': 'commandExecution', 'id': 'k1', 'command': '/bin/zsh -lc "cat notes.md"', 'cwd': '/w', 'commandActions': [], 'aggregatedOutput': 'x', 'exitCode': 0}
        send({'method': 'item/completed', 'params': {'threadId': child, 'item': item}})
        answer = text
    elif text in ('HELPER', 'SEARCH') and FAMILY == 'gpt':
        if text == 'SEARCH':
            # codex names a web search's query only when it is done.
            send({'method': 'item/started', 'params': {'threadId': sid, 'item': {'type': 'webSearch', 'id': 'w1', 'query': ''}}})
            send({'method': 'item/completed', 'params': {'threadId': sid, 'item': {'type': 'webSearch', 'id': 'w1', 'query': '', 'action': {'type': 'search', 'query': 'lord claude'}}}})
        else:
            # A codex helper agent: its parent reports it starting, then its own steps arrive under its own thread.
            child = 'child-' + sid[:8]
            started = {'type': 'subAgentActivity', 'id': 'h1', 'kind': 'started', 'agentThreadId': child, 'agentPath': '/root/check', 'model': 'gpt-6-luna', 'reasoningEffort': 'low'}
            send({'method': 'item/started', 'params': {'threadId': sid, 'item': started}})
            send({'method': 'item/completed', 'params': {'threadId': sid, 'item': started}})
            for item in ({'type': 'commandExecution', 'id': 'k1', 'command': '/bin/zsh -lc "cat notes.md"', 'cwd': '/w', 'commandActions': [{'type': 'read', 'command': 'cat notes.md', 'name': 'notes.md', 'path': 'notes.md'}], 'aggregatedOutput': 'x', 'exitCode': 0},
                         {'type': 'fileChange', 'id': 'k2', 'changes': [{'path': '/w/out.txt', 'kind': 'add', 'diff': '+y'}]}):
                send({'method': 'item/started', 'params': {'threadId': child, 'item': item}})
                send({'method': 'item/completed', 'params': {'threadId': child, 'item': item}})
            send({'method': 'turn/completed', 'params': {'threadId': child, 'turn': {'id': 'ct', 'status': 'completed'}}})
        answer = text
    elif text.startswith('WAIT1'):
        await asyncio.sleep(1)
        answer = text[6:] or 'first'
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
    elif method == 'environment/add':
        assert FAMILY == 'gpt' and p['environmentId'] and p['execServerUrl'].startswith('ws://127.0.0.1:')
        assert len(p['authBearerToken']) >= 32
        SESSIONS['environment'] = p['environmentId']
        if '--fail-environment' in sys.argv:
            send({'id': rid, 'error': {'code': -32000, 'message': 'environment refused'}})
            return
    elif method in ('thread/start', 'session/new'):
        assert 'dynamicTools' not in p and '_meta' not in p
        if '--signed-out' in sys.argv and FAMILY == 'grok':
            send({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32000, 'message': 'Authentication required', 'data': 'no auth method id provided'}})
            return
        sid = str(uuid.uuid4())
        if FAMILY == 'gpt':
            assert p['approvalPolicy'] == 'never'
            if p.get('environments'):
                assert p['environments'] == [{'environmentId': SESSIONS['environment'], 'cwd': p['cwd']}]
                assert 'sandbox' not in p
            else: assert p['sandbox'] in ('workspace-write', 'read-only', 'danger-full-access')
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
        if p.get('environments'): assert p['environments'] == [{'environmentId': SESSIONS['environment'], 'cwd': p['cwd']}]
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
    if 'exec-server' in sys.argv:
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'], start_new_session='--exec-eof' not in sys.argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        audit(exec_launch=True, child=child.pid, argv=sys.argv[3:], cwd=os.getcwd(), nice=os.getpriority(os.PRIO_PROCESS, 0),
              codex_home=os.environ.get('CODEX_HOME'), mcp_env={k: os.environ[k] for k in ('LORD_MCP_TOKEN', 'LORD_DAEMON_SOCKET') if k in os.environ},
              env={k: v for k, v in os.environ.items() if 'must-not-reach' in v})
        if '--exec-eof' in sys.argv: return
        if '--exec-invalid' in sys.argv: print('not a URL', flush=True)
        elif '--exec-silent' not in sys.argv: print('ws://127.0.0.1:43210', flush=True)
        await asyncio.Event().wait()
        return
    audit(launch=True, argv=sys.argv[3:], path=os.environ.get('PATH', ''), grok_env={k: v for k, v in os.environ.items() if k.startswith('GROK_')}, env={k: v for k, v in os.environ.items() if 'must-not-reach' in v}, codex_home=os.environ.get('CODEX_HOME'), sqlite_home=os.environ.get('CODEX_SQLITE_HOME'), nice=os.getpriority(os.PRIO_PROCESS, 0), cwd=os.getcwd())
    print('fixture Bearer fake-bearer-value', file=sys.stderr, flush=True)
    if FAMILY == 'gemini' and (DATA / 'start-quota-late').exists():
        # The same, with agy's stdout closed before its stderr line arrives.
        os.close(1)
        await asyncio.sleep(.3)
        print('AGY_ERROR: ' + json.dumps({'short_error': 'RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 1h0m0s.', 'error_code': 429}), file=sys.stderr, flush=True)
        await asyncio.sleep(.1)
        os._exit(1)
    if FAMILY == 'gemini' and (DATA / 'start-quota').exists():
        # agy failing at start-up, before its init event, on a spent quota.
        print('AGY_ERROR: ' + json.dumps({'short_error': 'RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 1h0m0s.', 'error_code': 429}), file=sys.stderr, flush=True)
        await asyncio.sleep(.1)
        os._exit(1)
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

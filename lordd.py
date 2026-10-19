#!/usr/bin/env python3
"""Lord's Unix HTTP broker and subscription CLI adapters (Python 3.11 or later)."""
import argparse
import asyncio
from datetime import datetime, timezone, timedelta
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import sys
import time

if sys.version_info < (3, 11):
    print('lordd needs Python 3.11 or later', file=sys.stderr)
    sys.exit(3)
from urllib.parse import quote
import uuid

VERSION = '1.0'
HERE = Path(__file__).resolve()
HOME = Path(os.environ.get('LORD_HOME', Path.home() / '.claude/lord')).expanduser()
SOCKET = HOME / 'lordd.sock'
LIMIT = 8 * 1024 * 1024
EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max']
RANKS = ['minimal', *EFFORTS, 'ultra']
INSTALL = {'gpt': "install OpenAI's codex CLI (brew install --cask codex, or npm install -g @openai/codex) and sign in with `codex login`",
           'grok': "install xAI's grok CLI (curl -fsSL https://x.ai/cli/install.sh | bash) and sign in by running `grok` once",
           'gemini': "install Google Antigravity's agy CLI (https://antigravity.google) and sign in by running `agy` once"}

# grok imports Claude Code's tool servers, hooks and skills by default; a lord agent keeps grok's own setup only.
GROK_NO_CLAUDE_IMPORTS = {f'GROK_{vendor}_{cell}_ENABLED': 'false' for vendor in ('CLAUDE', 'CURSOR') for cell in ('MCPS', 'HOOKS', 'SKILLS')}

def codex_config(folder):
    try:
        import tomllib
        return tomllib.loads((folder / 'config.toml').read_text())
    except (OSError, ValueError): return {}

def load_config():
    # The bundled defaults, with ~/.claude/lord/config.json merged over them one family at a time.
    config = json.loads(Path(os.environ.get('LORD_CONFIG', HERE.with_name('config.json'))).read_text())
    try: user = json.loads((HOME / 'config.json').read_text())
    except FileNotFoundError: return config
    except ValueError as error: raise ValueError(f'{HOME / "config.json"} is not valid JSON: {error}') from None
    if not isinstance(user, dict): raise ValueError(f'{HOME / "config.json"} must hold a JSON object')
    for key, value in user.items():
        config[key] = {**config[key], **value} if isinstance(value, dict) and isinstance(config.get(key), dict) else value
    for family in ('gpt', 'grok', 'gemini'):
        if family in config and not isinstance(config[family], dict): raise ValueError(f'{family} in config.json must be an object')
    numbers = [(k, config[k]) for k in ('memory_ceiling_kb', 'idle_exit_s', 'prune_days') if k in config]
    numbers += [(f'{f}.no_progress_s', config[f]['no_progress_s']) for f in ('gpt', 'grok', 'gemini') if 'no_progress_s' in config.get(f, {})]
    for key, value in numbers:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0: raise ValueError(f'{key} must be a number of 0 or more')
    return config

# Container sockets grok's built-in read-only profile denies; each symlink hop is denied too, because Seatbelt
# matches the resolved path.
SOCKETS = ('/var/run/docker.sock', '/run/podman/podman.sock', '/var/run/podman/podman.sock',
           '/run/containerd/containerd.sock', '/var/run/containerd/containerd.sock')

def link_chain(path):
    seen = [path]
    while os.path.islink(path) and len(seen) < 10:
        path = os.path.join(os.path.dirname(path), os.readlink(path))
        seen.append(os.path.normpath(path))
    seen.append(os.path.realpath(seen[0]))
    return seen

def grok_home():
    return Path(os.environ.get('GROK_HOME', Path.home() / '.grok'))

LORD_BLOCK = re.compile(r'^# lord: begin[^\n]*\n.*?^# lord: end[^\n]*(?:\n|\Z)', re.M | re.S)

def grok_profiles():
    # grok runs unconfined, with only a warning, when a built-in profile fails to apply, but refuses to start
    # when a custom profile fails. Lord therefore runs grok only under its own custom profiles. lord-readonly
    # drops restrict_network, whose socket resolver refuses symlinked endpoints (Docker Desktop's
    # /var/run/docker.sock), and denies the sockets explicitly instead. Neither profile can read Lord's control
    # key, without which a grok agent cannot ask the daemon for a less confined agent.
    import tomllib
    link = grok_home() / 'sandbox.toml'
    path = Path(os.path.realpath(link))
    if link.is_symlink() and not path.exists(): raise RuntimeError(f'{link} is a link to {path}, which does not exist')
    key = os.path.realpath(HOME / 'control.header')
    sockets = sorted({p for s in SOCKETS for p in link_chain(s)})
    listed = lambda paths: '[' + ', '.join(json.dumps(p) for p in paths) + ']'
    block = ('# lord: begin (written by the lord plugin; edits between these lines are replaced)\n'
             f'[profiles.lord-workspace]\nextends = "workspace"\ndeny = {listed([key])}\n\n'
             f'[profiles.lord-readonly]\nextends = "read-only"\nrestrict_network = false\ndeny = {listed([*sockets, key])}\n# lord: end\n')
    original = path.read_text() if path.exists() else ''
    kept = LORD_BLOCK.sub('', original).rstrip('\n')
    text = (kept + '\n\n' if kept else '') + block
    if text == original: return
    try: tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise RuntimeError(f"{path} cannot take Lord's grok profiles ({error}); fix that file and try again") from None
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.lord')
    temporary.write_text(text)
    if path.exists(): temporary.chmod(path.stat().st_mode & 0o777)
    temporary.replace(path)

TEMP_ROOTS = ('/tmp', '/private/tmp', '/var/tmp', '/private/var/tmp', '/var/folders', '/private/var/folders')

def fold(path):
    # macOS volumes are case-insensitive by default, so /Users/Name/.GROK is ~/.grok there.
    return path.lower() if sys.platform == 'darwin' else path

def canon(path):
    # The real path, with macOS's data-volume alias removed: /System/Volumes/Data/private/tmp is /private/tmp.
    path = os.path.realpath(path)
    if sys.platform == 'darwin' and fold(path + '/').startswith('/system/volumes/data/'): path = path[len('/System/Volumes/Data'):] or '/'
    return path

def inside(path, roots):
    return any(fold(path) == fold(root) or fold(path).startswith(fold(root).rstrip('/') + '/') for root in roots)

# The PATH of the latest Claude Code session to open an agent: a vendor CLI installed after the daemon started is found.
SESSION_PATH = None

async def run_quick(argv, timeout=20):
    # Helper CLI calls are bounded: a wedged one must not hold the daemon.
    try:
        process = await asyncio.create_subprocess_exec(*argv, env=subscription_env(), start_new_session=True,
            stdin=asyncio.subprocess.DEVNULL, stdout=-1, stderr=-1)
    except FileNotFoundError: return 127, f'{argv[0]} was not found'
    try: out, err = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        await kill_tree(process)
        return None, f'{argv[0]} did not answer within {timeout} s'
    return process.returncode, (out + err).decode('utf8', 'replace')

def codex_activity(item):
    # One line per action that touches the machine; messages and reasoning are not activity.
    kind = item.get('type')
    if kind == 'commandExecution': return '$ ' + str(item.get('command'))
    if kind == 'fileChange': return 'edit ' + ', '.join(str(c.get('path')) for c in item.get('changes', []))
    if kind == 'mcpToolCall': return f"tool {item.get('server')}.{item.get('tool')} {dumps(item.get('arguments'))[:300]}"
    if kind == 'webSearch': return 'search ' + str(item.get('query'))
    if kind in ('userMessage', 'agentMessage', 'reasoning', None): return None
    return str(kind)

def dumps(value):
    return json.dumps(value, separators=(',', ':'), ensure_ascii=True)

SECRET_NAMES = ('KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL')

def subscription_env(extra=None):
    # Every secret-like name in the inherited environment is dropped; the daemon's own settings (extra) are added after.
    return {**{k: v for k, v in os.environ.items()
               if not k.upper().startswith(('CLAUDECODE', 'CLAUDE_CODE_', 'ANTHROPIC_'))
               and not any(s in k.upper() for s in SECRET_NAMES)
               and k.upper() not in ('OPENAI_BASE_URL', 'RUST_LOG') and not v.lower().startswith('bearer ')},
            **({'PATH': SESSION_PATH} if SESSION_PATH else {}), **(extra or {})}

class Redactor:
    def __init__(self):
        self.secrets = {v for k, v in os.environ.items() if len(v) >= 8 and
                        any(s in k.upper() for s in SECRET_NAMES)}

    def __call__(self, value):
        value = str(value)
        for secret in sorted(self.secrets, key=len, reverse=True): value = value.replace(secret, '[REDACTED]')
        value = re.sub(r'(?i)\bBearer[ \t]+[^\s\"\x27]+|\bsk-[\w-]{16,}|\beyJ[\w-]+\.[\w-]+\.[\w-]+|\bgh[pousr]_\w{20,}|\bgithub_pat_\w{20,}'
                       r'|\bAKIA[0-9A-Z]{16}\b|\bAIza[\w-]{30,}|\bxox[abprs]-[\w-]{10,}|\bxai-\w{20,}', '[REDACTED]', value)
        # key=value, key: value and "key": "value", for the usual secret names.
        return re.sub(r'(?i)(["\x27]?\b(?:x-api-key|api[-_]?key|authorization|password|passwd|secret|token)["\x27]?\s*[:=]\s*["\x27]?)(?!\[REDACTED\])[^\s\"\x27,;}]+',
                      r'\1[REDACTED]', value)

async def process_table():
    ps = await asyncio.create_subprocess_exec('/bin/ps', '-A', '-o', 'pid=,ppid=,rss=', stdout=-1, stderr=-3)
    children, rss = {}, {}
    for row in (await ps.communicate())[0].decode().splitlines():
        if len(fields := row.split()) == 3:
            pid, ppid, kb = map(int, fields)
            children.setdefault(ppid, []).append(pid)
            rss[pid] = kb
    return children, rss

def tree(children, root):
    found, stack = [], [root]
    while stack:
        found.append(pid := stack.pop())
        stack += children.get(pid, [])
    return found

def signal_all(pids, sig):
    for pid in pids:
        try: os.kill(pid, sig)
        except (ProcessLookupError, PermissionError): pass

async def kill_tree(process):
    # Tool commands can leave the vendor's process group, so every descendant is signalled by pid as well.
    # macOS answers killpg with EPERM when the group holds only zombies; the pid signals below still run.
    if process is None: return
    pids = tree((await process_table())[0], process.pid)
    try: os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError): pass
    signal_all(pids, signal.SIGTERM)
    await asyncio.sleep(.2)
    try: os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError): pass
    signal_all(pids, signal.SIGKILL)
    await process.wait()

def usage(value):
    return {key: int(next((value[k] for k in aliases if isinstance(value.get(k), (int, float))), 0))
            for key, aliases in {
                'input_tokens': ('input_tokens', 'inputTokens', 'input'),
                'output_tokens': ('output_tokens', 'outputTokens', 'output'),
                'cache_read_input_tokens': ('cache_read_input_tokens', 'cachedInputTokens', 'cachedReadTokens', 'cache_read_tokens', 'cached_tokens')}.items()}

REDACT = Redactor()

class Agent:
    def __init__(self, record):
        self.record = record
        self.__dict__.update(record)
        self.vendorSession = record.get('vendorSession')
        self.state, self.activity, self.generation = 'idle', '', 0
        self.lock, self.events, self.stream_lock = asyncio.Lock(), asyncio.Queue(), asyncio.Lock()
        self.finished = asyncio.Event()
        self.finished.set()
        self.calls, self.items, self.total = {}, {}, record.get('total', {})
        self.effort = record.get('effort', 'high')
        self.turn_id, self.answer, self.tokens = None, '', usage({})
        self.cancel_requested, self.turn_task = False, None
        self.followup, self.earlier, self.carry, self.call_data, self.ended_turns = None, None, [], {}, set()
        self.final_text, self.restart, self.token = '', False, uuid.uuid4().hex
        self.log_path = HOME / 'logs' / f"{record['agentId']}.log"

    def undelivered(self):
        return bool(self.carry) or not self.events.empty()

    def emit(self, kind, **value):
        if self.state not in ('running', 'waiting_tool'): return
        if kind == 'text': self.answer += value['text']
        if kind == 'activity':
            self.activity = value['text'][:300]
            # The agent's activity log: what the vendor ran and read, for review provenance.
            self.log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not self.log_path.exists() or self.log_path.stat().st_size < 4 * 1024 * 1024:
                with self.log_path.open('a') as file: file.write(f'{datetime.now(timezone.utc).isoformat()} {REDACT(value["text"])[:2000]}\n')
        if kind in ('done', 'error'):
            self.state, self.activity = 'idle', ''
            self.finished.set()
            for future in self.calls.values():
                if not future.done(): future.set_result({'content': [{'type': 'text', 'text': 'Turn ended'}], 'isError': True})
        self.events.put_nowait({'type': kind, **value})

class Vendor:
    def __init__(self, daemon, family, agent=None):
        self.d, self.family, self.agent = daemon, family, agent
        self.process, self.serial, self.generation = None, 0, 0
        self.last, self.reason, self.efforts = time.monotonic(), None, []
        self.pending, self.models = {}, []
        self.lock, self.write_lock = asyncio.Lock(), asyncio.Lock()
        self.stopping, self.ready = False, False
        self.reaped = asyncio.Event()
        self.reaped.set()

    async def ensure(self):
        async with self.lock:
            await self.reaped.wait()
            if self.ready and self.process and self.process.returncode is None: return
            if self.process: await self.close()
            self.stopping = False
            self.readonly = bool(self.agent and self.agent.record.get('readonly'))
            config = self.d.config[self.family]
            command = config.get('program', {'gpt': 'codex', 'grok': 'grok', 'gemini': 'agy'}[self.family])
            command = list(command) if isinstance(command, list) else [command]
            if self.family == 'gpt': command += ['app-server']
            elif self.family == 'grok':
                # Seatbelt applies to the whole grok process, so read-only sessions get their own process.
                command += ['--sandbox', config['readonly_sandbox'] if self.readonly else config['write_sandbox']]
                command += ['agent', '--always-approve', 'stdio']
            else:
                await self.d.agy_mcp(command)
                a = self.agent
                command += ['--model', a.model, '--effort', a.effort, '--input-format', 'stream-json',
                            '--output-format', 'stream-json', '--dangerously-skip-permissions']
                if a.vendorSession: command += ['--conversation', a.vendorSession]
                self.initialized = asyncio.get_running_loop().create_future()
            self.last, self.reason = time.monotonic(), None
            if self.family == 'grok': grok_profiles()
            try:
                self.process = await asyncio.create_subprocess_exec(*command,
                    cwd=self.agent.cwd if self.agent else HOME, start_new_session=True,
                    env=subscription_env({**({} if self.agent and self.agent.record.get('tools') else GROK_NO_CLAUDE_IMPORTS),
                                          **({'LORD_AGENT_ID': self.agent.agentId, 'LORD_HOME': str(HOME), 'LORD_MCP_TOKEN': self.agent.token} if self.agent else {})}),
                    stdin=-1, stdout=-1, stderr=-1, limit=LIMIT)
            except FileNotFoundError: raise RuntimeError(f'{command[0]} was not found; {INSTALL[self.family]}') from None
            self.generation += 1
            self.d.task(self.read(self.process))
            self.d.task(self.stderr(self.process))
            try:
                if self.family == 'gpt':
                    await self.rpc('initialize', {'clientInfo': {'name': 'lord', 'version': VERSION},
                                                 'capabilities': {'experimentalApi': True}})
                    await self.send({'method': 'initialized', 'params': {}})
                    self.models = (await self.rpc('model/list', {'includeHidden': True}))['data']
                elif self.family == 'grok': await self.rpc('initialize', {'protocolVersion': 1, 'clientCapabilities': {}})
                else: await asyncio.wait_for(asyncio.shield(self.initialized), 45)
                self.ready = True
            except BaseException:
                await self.close()
                raise

    async def send(self, value):
        async with self.write_lock:
            self.process.stdin.write((dumps(value) + '\n').encode())
            await self.process.stdin.drain()

    async def rpc(self, method, params, timeout=45):
        self.serial += 1
        rid = f'lord-{self.serial}'
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        try:
            await self.send({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params})
            return await asyncio.wait_for(future, timeout)
        finally: self.pending.pop(rid, None)

    async def stderr(self, process):
        while line := await process.stderr.readline():
            self.d.log(line.decode('utf8', 'replace'))

    async def read(self, process):
        try:
            while line := await process.stdout.readline():
                if self.process is not process: return
                self.last = time.monotonic()
                value = json.loads(line)
                if 'id' in value and 'method' not in value:
                    future = self.pending.get(value['id'])
                    if future and not future.done():
                        if 'error' in value: future.set_exception(RuntimeError(self.d.redact(dumps(value['error']))))
                        else: future.set_result(value.get('result', {}))
                elif 'id' in value: self.d.task(self.request(value))
                else: self.notification(value)
            raise RuntimeError(f'{self.family} process closed stdout')
        except asyncio.CancelledError: raise
        except Exception as error:
            if self.stopping or self.d.stopping or self.process is not process: return
            self.ready = False
            self.reaped.clear()
            for future in list(self.pending.values()):
                if not future.done(): future.set_exception(RuntimeError(str(error)))
            if self.family == 'gemini' and not self.initialized.done(): self.initialized.set_exception(RuntimeError(str(error)))
            reason, self.reason = self.reason, None
            for a in self.d.agents.values():
                if self.d.backend(a) is self:
                    a.generation = 0
                    a.emit('error', message=self.d.redact(reason or error), retryable=not reason)
            self.d.log(error)
            try: await kill_tree(process)
            finally: self.reaped.set()

    async def request(self, value):
        method, p = value['method'], value.get('params', {})
        if method == 'item/tool/requestUserInput': result = {'answers': {}}
        elif method == 'session/request_permission':
            option = next((v for v in p.get('options', []) if v['kind'] in ('allow_once', 'allow_always')), None)
            result = {'outcome': {'outcome': 'selected', 'optionId': option['optionId']}} if option else {'outcome': {'outcome': 'cancelled'}}
        else:
            await self.send({'jsonrpc': '2.0', 'id': value['id'], 'error': {'code': -32601, 'message': 'Unsupported client request'}})
            return
        await self.send({'jsonrpc': '2.0', 'id': value['id'], 'result': result})

    def notification(self, value):
        if self.stopping: return
        p, method = value.get('params', {}), value.get('method')
        if self.family == 'gemini':
            a, event = self.agent, value.get('event', value.get('type'))
            payload = value.get(event) if isinstance(value.get(event), dict) else value
            if event == 'init':
                a.vendorSession = value.get('conversation_id') or payload.get('conversation_id')
                if not a.vendorSession: raise ValueError('agy init lacks a conversation id')
                self.d.save(a)
                if not self.initialized.done(): self.initialized.set_result(True)
            elif event == 'step_update':
                if payload.get('step_type') == 'agent_response' and payload.get('text_delta'): a.emit('text', text=payload['text_delta'])
                elif payload.get('step_type') != 'agent_response':
                    # Text before a tool step is narration; the answer is what agy says after its last one.
                    a.answer = ''
                    a.emit('activity', text=str(payload.get('command') or payload.get('step_type', 'Working')))
            elif event == 'result' and a.state in ('running', 'waiting_tool'):
                if payload.get('status') != 'SUCCESS' or payload.get('error'):
                    a.emit('error', message=self.d.redact(payload.get('error') or payload.get('status')), retryable=True)
                else:
                    self.d.record_usage(a, usage(payload.get('usage', {})))
                    a.emit('done', text=payload['response'] if isinstance(payload.get('response'), str) else a.answer, usage=usage(payload.get('usage', {})), model=a.model)
            return
        sid = p.get('threadId') or p.get('sessionId')
        a = next((a for a in self.d.agents.values() if a.family == self.family and a.vendorSession == sid), None)
        if not a or a.state not in ('running', 'waiting_tool'): return
        if self.family == 'grok':
            update = p.get('update', {})
            kind = update.get('sessionUpdate')
            if kind == 'agent_message_chunk' and update.get('content', {}).get('type') == 'text':
                a.final_text += update['content']['text']
                a.emit('text', text=update['content']['text'])
            elif kind in ('tool_call', 'tool_call_update'):
                # Text before a tool call is narration; the answer is what grok says after its last one.
                a.final_text = ''
                if kind == 'tool_call' or update.get('title'): a.emit('activity', text=update.get('title') or update.get('kind') or 'tool')
        elif method == 'turn/started' and p['turn']['id'] not in a.ended_turns: a.turn_id = p['turn']['id']
        elif method == 'item/agentMessage/delta':
            a.items[p['itemId']] = a.items.get(p['itemId'], '') + p['delta']
            a.emit('text', text=p['delta'])
        elif method == 'item/completed' and p['item'].get('type') == 'agentMessage':
            item = p['item']
            seen = a.items.get(item['id'], '')
            if item['text'].startswith(seen) and len(item['text']) > len(seen): a.emit('text', text=item['text'][len(seen):])
            a.items[item['id']] = item['text']
            a.final_text = item['text']
        elif method == 'item/started' and (line := codex_activity(p['item'])):
            # A message before a tool action is narration; the answer is the agent message after the last action.
            a.final_text = ''
            a.emit('activity', text=line)
        elif method == 'thread/tokenUsage/updated':
            a.total = p['tokenUsage']['total']
            a.tokens = usage({k: max(0, v - a.base.get(k, 0)) for k, v in a.total.items() if isinstance(v, int)})
        elif method == 'turn/completed':
            turn = p['turn']
            # A late completion of an earlier turn must not end the current one.
            if turn.get('id') in a.ended_turns or (a.turn_id and turn.get('id') != a.turn_id): return
            if turn['status'] == 'completed' and not a.cancel_requested:
                tokens = {**a.tokens, 'input_tokens': max(0, a.tokens['input_tokens'] - a.tokens['cache_read_input_tokens'])}
                self.d.record_usage(a, tokens)
                a.emit('done', text=a.final_text, usage=tokens, model=a.model)
            else:
                a.emit('error', message=self.d.redact('interrupted' if a.cancel_requested else turn.get('error') or turn['status']), retryable=not a.cancel_requested and turn['status'] != 'interrupted')
        elif method == 'error' and not p.get('willRetry', False): a.emit('error', message=self.d.redact(p.get('error', p)), retryable=True)
        elif method == 'model/rerouted': a.model = p['toModel']

    async def close(self):
        self.stopping, self.ready = True, False
        for future in [*self.pending.values(), getattr(self, 'initialized', None)]:
            if future and not future.done(): future.set_exception(RuntimeError('Vendor closed'))
        process = self.process
        await kill_tree(process)
        if self.process is process: self.process = None

class Daemon:
    def __init__(self):
        HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
        HOME.chmod(0o700)
        self.config = load_config()
        self.agents, self.vendors, self.tasks, self.connections = {}, {}, set(), set()
        self.redact, self.stopping, self.last_request = Redactor(), False, time.monotonic()
        self.requests, self.control = 0, None
        # One lock per agent: a slow vendor start delays only its own agent's /open.
        self.stop_event, self.open_locks, self.prune_lock = asyncio.Event(), {}, asyncio.Lock()
        self.usage_cache, self.agy_registered, self.agy_levels, self.agy_models = (0, {}), False, {}, set()
        self.journal = HOME / 'sessions.jsonl'
        if self.journal.exists():
            for line in self.journal.read_text().splitlines():
                try:
                    record = json.loads(line)
                    self.agents[record['agentId']] = Agent(record)
                except (ValueError, KeyError): self.log('Invalid journal line ignored')

    def log(self, text):
        path = HOME / 'lordd.log'
        line = f'{datetime.now(timezone.utc).isoformat()} {self.redact(text).strip()[:8192]}\n'
        if path.exists() and path.stat().st_size + len(line.encode()) > 1024 * 1024: path.replace(HOME / 'lordd.log.1')
        with path.open('a') as file: file.write(line)

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        def completed(task):
            self.tasks.discard(task)
            if not task.cancelled() and task.exception(): self.log(task.exception())
        task.add_done_callback(completed)
        return task

    def backend(self, a):
        # One vendor process per agent: a memory or hang guard can then stop one agent without touching the others.
        if a.agentId not in self.vendors: self.vendors[a.agentId] = Vendor(self, a.family, a)
        return self.vendors[a.agentId]

    def service(self):
        # A codex process owned by no agent, for account usage and archiving pruned threads.
        return self.vendors.setdefault(None, Vendor(self, 'gpt'))

    async def agy_mcp(self, program):
        # agy has no per-run MCP option. One user-level entry serves every agy process; the server reads its
        # agent id from LORD_AGENT_ID and lists no tools in agy sessions the daemon did not start.
        if self.agy_registered: return
        code, out = await run_quick([*program, 'mcp', 'add', 'lord', '--', sys.executable, str(HERE), '--mcp'])
        if code == 0: self.agy_registered = True
        else: self.log(f'agy mcp add failed; gemini agents lack lord tools until it succeeds: {out}')

    def record_usage(self, a, tokens):
        a.record['usage'] = tokens
        self.save(a)

    def save(self, a):
        a.record.update({k: getattr(a, k) for k in ('vendorSession', 'model', 'effort', 'total')}, used=datetime.now(timezone.utc).isoformat())
        with self.journal.open('a') as file: file.write(dumps(a.record) + '\n')

    def server(self, a):
        env = {'LORD_HOME': str(HOME), 'LORD_MCP_TOKEN': a.token, **({'LORD_READONLY': '1'} if a.record.get('readonly') else {})}
        return {'name': 'lord', 'command': sys.executable, 'args': [str(HERE), '--mcp', a.agentId], 'env': env}

    async def attach(self, a):
        async with a.lock:
            vendor = self.backend(a)
            try: await vendor.ensure()
            except Exception as error:
                self.log(f'{a.agentId}: {a.family} failed to start ({self.redact(error)}); retrying once')
                await asyncio.sleep(self.config.get('retry_delay_s', 3))
                await vendor.ensure()
            if a.generation == vendor.generation: return
            if a.family == 'gpt':
                await self.effort(a)
                server = self.server(a)
                server.pop('name')
                server.update(default_tools_approval_mode='approve', required=True, tool_timeout_sec=86400)
                # The user's codex tool servers would start in every agent's process (about 0.5 GB each);
                # agents get codex's own tools plus lord's, as the watchdog's --ignore-user-config runs did.
                user = {} if a.record.get('tools') else codex_config(Path(os.environ['CODEX_HOME']) if os.environ.get('CODEX_HOME') else self.vendor_home / '.codex')
                lean = {'mcp_servers': {name: {'enabled': False} for name in user.get('mcp_servers', {}) if name != 'lord'},
                        'plugins': {name: {'enabled': False} for name in user.get('plugins', {})}}
                if user.get('notify'): lean['notify'] = []
                params = {'cwd': a.cwd, 'model': a.model, 'approvalPolicy': 'never',
                          'sandbox': 'read-only' if a.record.get('readonly') else 'workspace-write',
                          'config': {**lean, 'mcp_servers': {**lean['mcp_servers'], 'lord': server},
                                     'sandbox_workspace_write': {'network_access': bool(a.record.get('net')), 'writable_roots': a.record.get('dirs', [])}}}
                if a.vendorSession: params['threadId'] = a.vendorSession
                result = await vendor.rpc('thread/resume' if a.vendorSession else 'thread/start', params)
                a.vendorSession = result['thread']['id']
            elif a.family == 'grok':
                server = self.server(a)
                server['env'] = [{'name': k, 'value': v} for k, v in server['env'].items()]
                params = {'cwd': a.cwd, 'mcpServers': [server]}
                if a.vendorSession: params['sessionId'] = a.vendorSession
                result = await vendor.rpc('session/load' if a.vendorSession else 'session/new', params)
                a.vendorSession = result.get('sessionId', a.vendorSession)
                options = {o.get('id'): [x['value'] for x in o.get('options', [])] for o in result.get('configOptions', [])}
                if options.get('reasoning_effort'): vendor.efforts = options['reasoning_effort']
                if options.get('model'): vendor.grok_models = options['model']
                if options.get('model') and a.model not in options['model']:
                    raise ValueError(f'unknown grok model {a.model}; grok lists {", ".join(options["model"])}')
                await self.effort(a)
                for key, value in [('model', a.model), ('reasoning_effort', a.effort)]:
                    await vendor.rpc('session/set_config_option', {'sessionId': a.vendorSession, 'configId': key, 'value': value})
            a.generation = vendor.generation
            self.save(a)

    async def effort(self, a, requested=None):
        # The requested level, or the nearest one the vendor lists for this model.
        value = requested or a.effort
        if value not in RANKS: raise ValueError('Invalid effort')
        if a.family == 'gpt':
            models = self.backend(a).models
            model = next((m for m in models if m['id'] == a.model), None)
            if models and model is None: raise ValueError(f'unknown gpt model {a.model}; codex lists {", ".join(m["id"] for m in models)}')
            allowed = [v['reasoningEffort'] for v in (model or {}).get('supportedReasoningEfforts', [])]
        elif a.family == 'grok': allowed = self.backend(a).efforts
        else: allowed = await self.agy_efforts(a.model)
        if allowed and value not in allowed:
            value = min((v for v in allowed if v in RANKS), key=lambda v: abs(RANKS.index(v) - RANKS.index(value)), default=value)
        a.effort = value

    async def agy_efforts(self, model):
        # agy lists each model once per effort level, as <model>-<level>.
        if model not in self.agy_levels:
            config = self.config['gemini']
            program = config.get('program', 'agy')
            program = list(program) if isinstance(program, list) else [program]
            code, out = await run_quick([*program, 'models'])
            ids = {line.split('\t')[0].strip() for line in out.splitlines()} if code == 0 else set()
            self.agy_models.update(i.rsplit('-', 1)[0] for i in ids if i.rsplit('-', 1)[-1] in EFFORTS)
            if code != 0:
                # Not cached: the next open asks agy again. Until then agy's own --effort range applies.
                self.log(f'agy models failed: {out}')
                return EFFORTS
            self.agy_levels[model] = [v for v in EFFORTS if f'{model}-{v}' in ids]
        if self.agy_models and model not in self.agy_models:
            raise ValueError(f'unknown gemini model {model}; agy lists {", ".join(sorted(self.agy_models))}')
        return self.agy_levels[model]

    async def open(self, body):
        aid, family = body['agentId'], body['family']
        if not isinstance(aid, str) or not aid or family not in ('gpt', 'grok', 'gemini'): raise ValueError('Invalid agent or family')
        async with self.open_locks.setdefault(aid, asyncio.Lock()):
            if body.get('readonly') and family == 'gemini': raise ValueError('readonly is not enforceable for agy; use lord:gpt or lord:grok')
            if body.get('dirs') and family != 'gpt':
                raise ValueError({'grok': "@dir is not available for grok: its sandbox confines writes to the working folder; use @cwd",
                                  'gemini': '@dir is not needed for gemini: agy has no sandbox and can write outside its folder'}[family])
            if not Path(body['cwd']).expanduser().is_absolute(): raise ValueError('the working folder must be an absolute path')
            cwd = str(Path(body['cwd']).expanduser().resolve(strict=True))
            if inside(canon(str(Path.home())), (canon(cwd),)):
                raise ValueError('refusing / or a folder holding your home folder as a working folder: start Claude Code in a project folder, or put "@cwd <folder>" on the first line of the task')
            a = self.agents.get(aid)
            if a and body.get('pin_cwd') and not os.path.samefile(cwd, a.cwd): raise ValueError(f'@cwd is fixed at the first turn ({a.cwd})')
            if isinstance(body.get('path'), str) and body['path']:
                global SESSION_PATH
                SESSION_PATH = body['path']
            readonly = body.get('readonly') or bool(a and a.record.get('readonly'))
            if family == 'gpt' and readonly and (body.get('net') or bool(a and a.record.get('net'))):
                raise ValueError("@readonly with @net is not read-only for gpt: with network on, codex can reach local services, Lord's included; use lord:grok for a read-only agent with network")
            # grok's read-only base profile leaves the temp folders and grok's own folder writable.
            if body.get('readonly') and family == 'grok' and inside(canon(a.cwd if a else cwd), (*TEMP_ROOTS, canon(str(grok_home())))):
                raise ValueError("grok's read-only profile leaves temp folders and ~/.grok writable; pin a read-only grok outside /tmp, /var/tmp, /var/folders and ~/.grok")
            new = a is None
            saved = None if new else (json.loads(dumps(a.record)), a.restart)
            try: return await self.open_agent(aid, family, cwd, body, a)
            except BaseException:
                # A first /open that fails leaves no agent behind, so a corrected retry starts clean; a refused
                # later /open puts the agent's options back and reattaches with them on the next turn.
                if new and self.agents.pop(aid, None) is not None:
                    vendor = self.vendors.pop(aid, None)
                    if vendor: await vendor.close()
                    self.compact()
                elif saved and a.record != saved[0]:
                    a.record.clear(); a.record.update(saved[0])
                    a.restart, a.generation = saved[1], 0
                raise

    async def open_agent(self, aid, family, cwd, body, a):
        if a is None:
            if not Path(cwd).is_dir(): raise ValueError('cwd must be a directory')
            config = self.config[family]
            a = Agent({**{k: body[k] for k in ('readonly', 'net') if k in body}, 'agentId': aid,
                'family': family, 'cwd': cwd, 'model': body.get('model') or config['model'],
                'effort': body.get('effort') or config['effort'], 'created': datetime.now(timezone.utc).isoformat()})
            await self.effort(a)
            self.agents[aid] = a
        elif a.family != family: raise ValueError('agentId already belongs to another family')
        # Everything is checked before anything changes, so a refused /open leaves the agent as it was.
        for key in ('memory_gb', 'hang_s', 'wall_s'):
            if body.get(key) is not None and (not isinstance(body[key], (int, float)) or body[key] < 0 or (key == 'memory_gb' and body[key] == 0)):
                raise ValueError(f'invalid {key}')
        dirs = [str(Path(x).expanduser().resolve()) for x in body.get('dirs') or []]
        if any(not Path(x).is_dir() for x in dirs): raise ValueError('every @dir must be an existing directory')
        for key in ('readonly', 'net'):
            if body.get(key) and not a.record.get(key):
                # A sandbox change needs a new process (grok) or a reattached thread (codex).
                a.record[key] = True
                a.restart = True
        # Per-agent overrides of the guards and of the lean vendor setup; any later /open may change them.
        for key in ('memory_gb', 'hang_s', 'wall_s'):
            if body.get(key) is not None: a.record[key] = body[key]
        if body.get('tools') and not a.record.get('tools'):
            # The user's tool servers load at process start, so the agent gets a new process and resumes.
            a.record['tools'] = True
            a.restart = True
        if a.restart and a.state not in ('running', 'waiting_tool'):
            a.restart = False
            await self.backend(a).close()
            a.generation = 0
        if set(dirs) - set(a.record.get('dirs', [])):
            # Extra writable folders change the sandbox, so the session reattaches with them.
            a.record['dirs'] = sorted(set(a.record.get('dirs', [])) | set(dirs))
            a.generation = 0
        await self.attach(a)
        if a.family == 'gemini' and body.get('model'): await self.agy_efforts(body['model'])
        # A model change applies at the next turn; refusing it here keeps the mod from adopting it.
        if body.get('model') and (models := self.known_models(a)) and body['model'] not in models:
            raise ValueError(f'unknown {a.family} model {body["model"]}; {a.family} lists {", ".join(models)}')
        if a.state == 'closed': a.state = 'idle'
        # An opened agent counts as used, so pruning cannot take it before its turn starts.
        self.save(a)
        return {'vendorSession': a.vendorSession, 'model': a.model, 'effort': a.effort}

    def known_models(self, a):
        vendor = self.backend(a)
        if a.family == 'gpt': return [m['id'] for m in vendor.models]
        if a.family == 'grok': return getattr(vendor, 'grok_models', [])
        return sorted(self.agy_models)

    async def begin(self, a, body):
        async with a.stream_lock: await self.begin_locked(a, body)

    async def begin_locked(self, a, body):
        if a.state != 'idle' or a.undelivered(): raise ValueError('Agent has a turn awaiting completion')
        a.cancel_requested, a.starting = False, True
        try: await self.begin_starting(a, body)
        finally: a.starting = False

    async def begin_starting(self, a, body):
        if a.turn_task: await asyncio.gather(a.turn_task, return_exceptions=True)
        previous = (a.model, a.effort)
        a.model = body.get('model') or a.model
        try:
            await self.effort(a, body.get('effort'))
            if a.restart:
                a.restart = False
                await self.backend(a).close()
                a.generation = 0
            if a.family in ('grok', 'gemini') and previous != (a.model, a.effort):
                if a.family == 'gemini': await self.backend(a).close()
                a.generation = 0
            await self.attach(a)
        except BaseException:
            if (a.model, a.effort) != previous:
                a.model, a.effort = previous
                a.generation = 0
            raise
        if a.turn_id: a.ended_turns.add(a.turn_id)
        a.state, a.answer, a.final_text, a.items = 'running', '', '', {}
        a.base, a.tokens, a.turn_id = dict(a.total), usage({}), None
        a.finished.clear()
        a.dispatched = False
        self.save(a)
        a.turn_task = self.task(self.run_turn(a, body))

    async def run_turn(self, a, body):
        v = self.backend(a)
        try:
            if a.cancel_requested:
                a.emit('error', message='interrupted', retryable=False)
                return
            a.dispatched, v.last = True, time.monotonic()
            a.started = v.last
            if a.family == 'gpt':
                params = {'threadId': a.vendorSession, 'input': [{'type': 'text', 'text': body['text']}],
                          'model': a.model, 'effort': a.effort,
                          'sandboxPolicy': {'type': 'readOnly', 'networkAccess': bool(a.record.get('net'))} if a.record.get('readonly')
                                           else {'type': 'workspaceWrite', 'networkAccess': bool(a.record.get('net')), 'writableRoots': a.record.get('dirs', [])}}
                if body.get('schema'): params['outputSchema'] = body['schema']
                result = await v.rpc('turn/start', params)
                a.turn_id = result['turn']['id']
                if a.cancel_requested and a.state == 'running': await self.interrupt(a)
            elif a.family == 'grok':
                text = body['text']
                if body.get('schema'): text += '\nReturn JSON matching this schema: ' + dumps(body['schema'])
                result = await v.rpc('session/prompt', {'sessionId': a.vendorSession,
                    'prompt': [{'type': 'text', 'text': text}]}, timeout=None)
                if result.get('stopReason') != 'end_turn' or a.cancel_requested:
                    a.emit('error', message=str(result.get('stopReason')), retryable=False)
                else:
                    path = (Path(os.environ['GROK_HOME']) if os.environ.get('GROK_HOME') else self.vendor_home / '.grok') / 'sessions' / quote(a.cwd, safe='') / a.vendorSession / 'usage.json'
                    tokens = json.loads(path.read_text()) if path.exists() else result.get('usage', {})
                    if tokens.get('turns'): tokens = tokens['turns'][-1]
                    self.record_usage(a, usage(tokens))
                    a.emit('done', text=a.final_text, usage=usage(tokens), model=a.model)
            else:
                text = body['text']
                if body.get('schema'): text += '\nReturn JSON matching this schema: ' + dumps(body['schema'])
                await v.send({'event': 'user', 'message': {'content': text}})
        except Exception as error: a.emit('error', message=self.redact(error), retryable=True)

    @property
    def vendor_home(self):
        return Path(self.config.get('vendor_home', Path.home()))

    async def interrupt(self, a):
        a.followup = a.earlier = None
        # A turn still starting (begin's awaits) is interrupted once it runs, through the path below.
        deadline = time.monotonic() + 5
        while getattr(a, 'starting', False) and time.monotonic() < deadline: await asyncio.sleep(.02)
        if a.state in ('running', 'waiting_tool'):
            v = self.backend(a)
            a.cancel_requested = True
            for future in list(a.calls.values()):
                if not future.done(): future.set_result({'content': [{'type': 'text', 'text': 'interrupted'}], 'isError': True})
            if a.family == 'gpt':
                # Before dispatch run_turn sees cancel_requested and ends the turn itself.
                if a.turn_id: await v.rpc('turn/interrupt', {'threadId': a.vendorSession, 'turnId': a.turn_id})
            elif a.family == 'grok':
                if a.dispatched: await v.send({'method': 'session/cancel', 'params': {'sessionId': a.vendorSession}})
            else:
                await v.close()
                a.generation = 0
                a.emit('error', message='interrupted', retryable=False)
            try: await asyncio.wait_for(a.finished.wait(), 5)
            except TimeoutError:
                await kill_tree(v.process)
                a.emit('error', message='interrupted', retryable=False)
        # An interrupted turn leaves nothing to deliver, also when it finished just before the interrupt arrived.
        if not a.stream_lock.locked():
            a.carry.clear()
            while not a.events.empty(): a.events.get_nowait()

    def listed(self):
        return [{**{k: getattr(a, k) for k in ('agentId', 'family', 'model', 'effort', 'state', 'activity', 'cwd')},
                 'log': str(a.log_path), 'usage': a.record.get('usage')} for a in self.agents.values()]

    async def mcp_call(self, a, name, args, token):
        if token != a.token: raise ValueError('lord tool call from an unknown process')
        if name == 'list_agents':
            self.log(f'MCP list_agents agent={a.agentId}')
            # Another agent's command lines and folders stay out of a vendor's view.
            agents = [{k: getattr(b, k) for k in ('agentId', 'family', 'model', 'state')} for b in self.agents.values()]
            return {'content': [{'type': 'text', 'text': dumps({'you': a.agentId, 'agents': agents})}], 'isError': False}
        if a.state not in ('running', 'waiting_tool') or a.cancel_requested: raise ValueError('No running turn for lord tool')
        if a.record.get('readonly'): raise ValueError('a read-only agent cannot spawn agents or send messages')
        if name == 'spawn_agent':
            data = {'subagent_type': args['type'], 'prompt': args['prompt'], 'description': args['description'],
                    'run_in_background': bool(args.get('background', False))}
            if args.get('effort') in EFFORTS: data['effort'] = args['effort']
            elif args.get('effort'): data['prompt'] = '@effort ' + args['effort'] + '\n' + data['prompt']
            if args.get('model'):
                model = args['model']
                if model in ('haiku', 'sonnet', 'opus', 'inherit') or model.startswith('claude-'): data['model'] = model
                else: data['prompt'] = '@model ' + model + '\n' + data['prompt']
            tool = 'Agent'
        elif name == 'send_message':
            data = {'to': args['to'], 'message': args['text'], 'summary': args['text'][:100]}
            tool = 'SendMessage'
        else: raise ValueError('Unknown lord tool')
        call = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        a.calls[call], a.call_data[call] = future, (tool, data)
        a.state = 'waiting_tool'
        a.emit('tool', callId=call, name=tool, input=data)
        try: return await future
        finally: a.calls.pop(call, None); a.call_data.pop(call, None)

    async def route(self, method, path, body):
        if method == 'GET':
            if path == '/health': return {'ok': True, 'pid': os.getpid(), 'version': VERSION}
            if path == '/agents': return self.listed()
            if path == '/usage':
                if time.monotonic() - self.usage_cache[0] >= 60:
                    v = self.service()
                    await v.ensure()
                    r = (await v.rpc('account/rateLimits/read', {}))['rateLimits']
                    window = r.get('primary') or r.get('secondary') or {}
                    self.usage_cache = time.monotonic(), {'chatgpt': {'usedPercent': window.get('usedPercent', 0),
                        'resetsAt': window.get('resetsAt'), 'credits': r.get('credits')}}
                return self.usage_cache[1]
        elif method == 'POST':
            if path == '/open': return await self.open(body)
            if path == '/prune': return await self.prune(body.get('days', self.config['prune_days']))
            a = self.agents[body['agentId']]
            if path == '/tool_result':
                future = a.calls.get(body['callId'])
                if future is None or future.done(): raise ValueError('No matching pending tool call')
                a.state = 'waiting_tool' if any(not f.done() for k, f in a.calls.items() if k != body['callId']) else 'running'
                # Time spent waiting for the tool is not vendor silence.
                if a.agentId in self.vendors: self.vendors[a.agentId].last = time.monotonic()
                future.set_result({'content': [{'type': 'text', 'text': body['text']}], 'isError': bool(body.get('isError'))})
            elif path == '/mcp': return await self.mcp_call(a, body['name'], body.get('arguments', {}), body.get('token'))
            elif path == '/steer':
                steered = a.family == 'gpt' and a.state in ('running', 'waiting_tool') and bool(a.turn_id)
                if steered:
                    await self.backend(a).rpc('turn/steer', {'threadId': a.vendorSession, 'expectedTurnId': a.turn_id,
                        'input': [{'type': 'text', 'text': body['text']}]})
                return {'ok': True, 'steered': steered}
            elif path in ('/interrupt', '/close'):
                await self.interrupt(a)
                if path == '/close':
                    a.state = 'closed'
                    await self.backend(a).close()
            else: raise ValueError('Unknown endpoint')
            return {'ok': True}
        raise ValueError('Unknown endpoint')

    async def http(self, reader, writer):
        streaming = False
        self.connections.add(writer)
        self.last_request = time.monotonic()
        self.requests += 1
        try:
            header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 30)
            first, *lines = header.decode('ascii').split('\r\n')
            method, path, protocol = first.split()
            headers = dict(line.lower().split(':', 1) for line in lines if ':' in line)
            length = int(headers.get('content-length', '0'))
            if protocol != 'HTTP/1.1' or not 0 <= length <= LIMIT or 'transfer-encoding' in headers: raise ValueError('Invalid HTTP request framing')
            if (method, path) not in (('GET', '/health'), ('POST', '/mcp')) and (not self.control or headers.get('x-lord-control', '').strip() != self.control):
                raise PermissionError("this request needs Lord's control key")
            body = json.loads(await asyncio.wait_for(reader.readexactly(length), 30)) if length else {}
            if method == 'POST' and path in ('/turn', '/continue'):
                a = self.agents[body['agentId']]
                if a.stream_lock.locked(): raise ValueError('A stream is already attached')
                if path == '/turn':
                    if not isinstance(body['text'], str): raise ValueError('text must be a string')
                    # A turn whose client went away keeps running; its result is delivered first and this
                    # text becomes the next turn in the same stream.
                    if a.state in ('running', 'waiting_tool') or a.undelivered():
                        a.followup = body
                        # A tool call whose client went away is offered again, so the new client can run it.
                        if a.state == 'waiting_tool':
                            queued = []
                            while not a.events.empty(): queued.append(a.events.get_nowait())
                            offered = {e['callId'] for e in [*queued, *a.carry] if e['type'] == 'tool'}
                            a.carry += [{'type': 'tool', 'callId': call, 'name': name, 'input': data}
                                        for call, (name, data) in a.call_data.items() if not a.calls[call].done() and call not in offered]
                            for e in queued: a.events.put_nowait(e)
                    else: await self.begin(a, body)
                elif not a.undelivered() and a.state not in ('running', 'waiting_tool'): raise ValueError('No turn to continue')
                async with a.stream_lock:
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n')
                    await writer.drain()
                    streaming = True
                    disconnected, pending = asyncio.create_task(reader.read(1)), None
                    try:
                        while True:
                            if a.carry: event = a.carry.pop(0)
                            else:
                                pending = asyncio.create_task(a.events.get())
                                ready, _ = await asyncio.wait((pending, disconnected), return_when=asyncio.FIRST_COMPLETED)
                                if disconnected in ready:
                                    # An event taken off the queue in the same instant is kept for the next client.
                                    if pending.done() and not pending.cancelled(): a.carry.insert(0, pending.result())
                                    break
                                event, pending = pending.result(), None
                            if event['type'] in ('done', 'error') and a.followup:
                                body, a.followup = a.followup, None
                                a.earlier = event['text'] if event['type'] == 'done' else 'lord: error: ' + event['message']
                                try:
                                    await self.begin_locked(a, body)
                                    continue
                                except Exception as error: event = {'type': 'error', 'message': self.redact(error), 'retryable': False}
                            if event['type'] in ('done', 'error') and a.earlier:
                                earlier = f'Result of the earlier task, which finished while Claude Code was disconnected:\n{a.earlier}\n\n'
                                event = ({**event, 'text': f'{earlier}Reply to the new message:\n{event["text"]}'} if event['type'] == 'done'
                                         else {**event, 'message': f'{earlier}The new message failed: {event["message"]}'})
                                a.earlier = None
                            line = (dumps(event) + '\n').encode()
                            try:
                                writer.write(f'{len(line):x}\r\n'.encode() + line + b'\r\n')
                                await writer.drain()
                            except (BrokenPipeError, ConnectionResetError):
                                a.carry.insert(0, event)
                                raise
                            if event['type'] in ('tool', 'done', 'error'): break
                        writer.write(b'0\r\n\r\n')
                    finally:
                        for task in (disconnected, pending):
                            if task: task.cancel()
                        await asyncio.gather(disconnected, *([pending] if pending else []), return_exceptions=True)
            else:
                result = await self.route(method, path, body)
                await self.response(writer, 200, result)
        except (BrokenPipeError, ConnectionResetError): pass
        except Exception as error:
            if not streaming: await self.response(writer, 400 if isinstance(error, (ValueError, KeyError)) else 403 if isinstance(error, PermissionError) else 503, {'error': self.redact(error)})
        finally:
            self.connections.discard(writer)
            self.requests -= 1
            self.last_request = time.monotonic()
            writer.close()
            try: await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError): pass

    async def response(self, writer, code, value):
        payload = dumps(value).encode()
        writer.write(f'HTTP/1.1 {code} {"OK" if code == 200 else "Error"}\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n'.encode() + payload)
        await writer.drain()

    async def heartbeat(self):
        last = guarded = 0
        pruned = time.monotonic()
        while not self.stopping:
            await asyncio.sleep(min(1, max(.05, self.config['idle_exit_s'] / 4)))
            now = time.monotonic()
            if now - guarded >= self.config.get('guard_interval_s', 5):
                guarded = now
                try: await self.guard(now)
                except Exception as error: self.log(f'guard failed: {error}')
            if now - last >= 60:
                self.log(f'heartbeat agents={len(self.agents)}')
                last = now
            if now - pruned >= 3600:
                pruned = now
                self.task(self.prune(self.config['prune_days']))
            busy = any(a.state in ('running', 'waiting_tool') or (a.undelivered() and now - self.last_request < self.config.get('keep_undelivered_s', 86400))
                       for a in self.agents.values())
            if not self.requests and now - self.last_request >= self.config['idle_exit_s'] and not busy:
                self.stop_event.set()
                return

    async def guard(self, now):
        idle_s = self.config.get('process_idle_s', self.config['idle_exit_s'])
        service = self.vendors.get(None)
        if service and service.process and not service.pending and now - service.last >= idle_s: await service.close()
        live = [v for v in self.vendors.values() if v.agent and v.process and v.process.returncode is None]
        for v in live:
            a = v.agent
            # A warm process with no turn for the idle period is closed; the next turn resumes its session.
            if a.state in ('idle', 'closed') and not a.lock.locked() and not a.stream_lock.locked() and now - v.last >= idle_s:
                await v.close()
        live = [v for v in live if v.process and v.process.returncode is None]
        if not live: return
        children, rss = await process_table()
        for v in live:
            a, pids = v.agent, tree(children, v.process.pid)
            ceiling = a.record['memory_gb'] * 1048576 if a.record.get('memory_gb') else self.config.get('memory_ceiling_kb', 12 * 1024 * 1024)
            used = sum(rss.get(pid, 0) for pid in pids)
            silent = a.record['hang_s'] if a.record.get('hang_s') is not None else self.config[a.family].get('no_progress_s', 0)
            if used > ceiling: v.reason = f'stopped: the {a.family} process tree used {used / 1048576:.1f} GB, over the {ceiling / 1048576:.1f} GB memory ceiling'
            elif silent and a.state == 'running' and now - v.last > silent: v.reason = f'stopped: {a.family} printed nothing for {silent} s while working'
            elif (wall := a.record.get('wall_s')) and a.state in ('running', 'waiting_tool') and now - getattr(a, 'started', now) > wall:
                v.reason = f'stopped: the turn ran past its {wall} s wall-clock limit'
            else: continue
            self.log(f'{a.agentId}: {v.reason}')
            # The vendor's process group and every descendant by pid, since tool commands may leave the group.
            try: os.killpg(v.process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError): pass
            signal_all(pids, signal.SIGKILL)

    async def prune(self, days):
        async with self.prune_lock: return await self.prune_locked(days)

    async def prune_locked(self, days):
        if not isinstance(days, (int, float)) or days < 0: raise ValueError('days must be nonnegative')
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        removed = []
        for aid, a in list(self.agents.items()):
            lock = self.open_locks.setdefault(aid, asyncio.Lock())
            if lock.locked(): continue
            async with lock:
                if self.agents.get(aid) is not a: continue
                if a.stream_lock.locked() or a.state in ('running', 'waiting_tool') or datetime.fromisoformat(a.record.get('used', a.created)) >= cutoff: continue
                sid = a.vendorSession
                if not sid or not re.fullmatch(r'[A-Za-z0-9_-]+', sid): continue
                previous, a.state = a.state, 'closed'
                try:
                    backend = self.vendors.pop(aid, None)
                    a.generation = 0
                    if backend: await backend.close()
                    # Vendor history is kept (it is review and build evidence); codex's archive only moves it aside.
                    if a.family == 'gpt':
                        v = self.service()
                        try:
                            await v.ensure()
                            await v.rpc('thread/archive', {'threadId': sid})
                        except Exception as error: self.log(f'codex thread {sid} not archived: {error}')
                except BaseException:
                    a.state = previous
                    raise
                removed.append(aid)
                a.log_path.unlink(missing_ok=True)
                del self.agents[aid]
                self.open_locks.pop(aid, None)
        self.compact()
        return {'ok': True, 'pruned': removed}

    def compact(self):
        # The journal rewritten with one line per agent the daemon still has.
        temporary = self.journal.with_suffix('.tmp')
        temporary.write_text(''.join(dumps(a.record) + '\n' for a in self.agents.values()))
        temporary.replace(self.journal)

    async def close(self):
        self.stopping = True
        for a in self.agents.values(): a.emit('error', message='Daemon shutting down', retryable=True)
        await asyncio.gather(*(v.close() for v in self.vendors.values()), return_exceptions=True)
        tasks = list(self.tasks)
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for writer in list(self.connections): writer.close()

    async def serve(self):
        lock = (HOME / 'lordd.lock').open('a')
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            return
        server, bound = None, False
        try:
            try: _, writer = await asyncio.open_unix_connection(str(SOCKET))
            except (FileNotFoundError, ConnectionRefusedError): pass
            else:
                writer.close()
                await writer.wait_closed()
                return
            # Every request but /health and /mcp (which carries a per-agent token) needs this key. Vendor sandboxes
            # cannot read the file (grok's profiles deny it; codex's sandbox cannot reach the socket without @net).
            self.control = uuid.uuid4().hex
            temporary = HOME / '.control.header'
            temporary.write_text(f'X-Lord-Control: {self.control}\n')
            temporary.chmod(0o600)
            temporary.replace(HOME / 'control.header')
            SOCKET.unlink(missing_ok=True)
            server = await asyncio.start_unix_server(self.http, str(SOCKET), limit=LIMIT)
            bound = True
            SOCKET.chmod(0o600)
            for sig in (signal.SIGTERM, signal.SIGINT): asyncio.get_running_loop().add_signal_handler(sig, self.stop_event.set)
            self.task(self.heartbeat())
            self.task(self.prune(self.config['prune_days']))
            await self.stop_event.wait()
        finally:
            if server: server.close()
            await self.close()
            if server: await server.wait_closed()
            if bound: SOCKET.unlink(missing_ok=True)
            lock.close()

async def relay(path, body=None):
    reader, writer = await asyncio.open_unix_connection(str(SOCKET), limit=LIMIT)
    payload = dumps(body).encode() if body is not None else b''
    try: key = (HOME / 'control.header').read_text().strip() + '\r\n'
    except OSError: key = ''
    writer.write(f'{"POST" if body is not None else "GET"} {path} HTTP/1.1\r\nHost: localhost\r\n{key}Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n'.encode() + payload)
    try:
        await writer.drain()
        header = await reader.readuntil(b'\r\n\r\n')
        value = json.loads(await reader.read())
        if header.split()[1] != b'200': raise RuntimeError(value.get('error', 'Daemon request failed'))
        return value
    finally:
        writer.close()
        await writer.wait_closed()

TOOLS = [
    {'name': 'spawn_agent', 'description': 'Run a Claude Code agent or a lord vendor agent.',
     'inputSchema': {'type': 'object', 'properties': {**{k: {'type': 'string'} for k in ('type', 'prompt', 'description', 'effort', 'model')},
                     'background': {'type': 'boolean'}}, 'required': ['type', 'prompt', 'description']}},
    {'name': 'send_message', 'description': 'Send a message to an agent.', 'inputSchema': {'type': 'object',
     'properties': {k: {'type': 'string'} for k in ('to', 'text')}, 'required': ['to', 'text']}},
    {'name': 'list_agents', 'description': 'Your own agent id (you) and every lord agent with its current activity.', 'inputSchema': {'type': 'object', 'properties': {}}}]

async def mcp(agent_id):
    agent_id = agent_id or os.environ.get('LORD_AGENT_ID')
    token, readonly = os.environ.get('LORD_MCP_TOKEN'), os.environ.get('LORD_READONLY') == '1'
    redact = Redactor()
    async def respond(value):
        if 'id' not in value: return
        try:
            method, params = value['method'], value.get('params', {})
            if method == 'initialize':
                result = {'protocolVersion': params.get('protocolVersion', '2024-11-05'),
                          'capabilities': {'tools': {}}, 'serverInfo': {'name': 'lord', 'version': VERSION}}
            elif method == 'ping': result = {}
            elif method == 'tools/list':
                result = {'tools': [] if not agent_id else [t for t in TOOLS if t['name'] == 'list_agents'] if readonly else TOOLS}
            elif method == 'tools/call':
                result = await relay('/mcp', {'agentId': agent_id, 'name': params['name'], 'arguments': params.get('arguments', {}), 'token': token})
            else: raise ValueError('Unknown MCP method')
            reply = {'jsonrpc': '2.0', 'id': value['id'], 'result': result}
        except Exception as error: reply = {'jsonrpc': '2.0', 'id': value['id'], 'error': {'code': -32603, 'message': redact(error)}}
        print(dumps(reply), flush=True)
    tasks = set()
    reader = asyncio.StreamReader(limit=LIMIT)
    await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    while line := await reader.readline():
        task = asyncio.create_task(respond(json.loads(line)))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    for task in tasks: task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--daemon', action='store_true')
    parser.add_argument('--mcp', nargs='?', const='')
    parser.add_argument('--prune', action='store_true')
    parser.add_argument('--days', type=float)
    args = parser.parse_args()
    os.umask(0o077)
    if args.mcp is not None:
        asyncio.run(mcp(args.mcp))
        return
    if args.daemon:
        # A broken settings file is reported to the starting process; after the fork nothing can see it.
        try: load_config()
        except ValueError as error:
            print(error, file=sys.stderr)
            sys.exit(2)
        if os.fork(): return
        os.setsid()
        with open(os.devnull, 'r+b', buffering=0) as null:
            for fd in (0, 1, 2): os.dup2(null.fileno(), fd)
    async def run():
        d = Daemon()
        if args.prune:
            try: result = await relay('/prune', {'days': args.days if args.days is not None else d.config['prune_days']})
            except (ConnectionRefusedError, FileNotFoundError):
                lock = (HOME / 'lordd.lock').open('a')
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try: result = await d.prune(args.days if args.days is not None else d.config['prune_days'])
                finally:
                    await d.close()
                    lock.close()
            print(dumps(result))
        else: await d.serve()
    asyncio.run(run())

if __name__ == '__main__': main()

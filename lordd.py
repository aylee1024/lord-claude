#!/usr/bin/env python3
"""Lord's Unix HTTP broker and subscription CLI adapters (Python 3.11 or later)."""
import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import time

if sys.version_info < (3, 11):
    print('lordd needs Python 3.11 or later', file=sys.stderr)
    sys.exit(3)
from urllib.parse import quote
import uuid

HERE = Path(__file__).resolve()

def plugin_version(root):
    try: return str(json.loads((root / '.claude-plugin/plugin.json').read_text())['version'])
    except (OSError, ValueError, KeyError, TypeError): return '0'

def newer(version, than):
    # Dotted numbers only; a version that is missing or not numeric is never newer.
    try: return tuple(map(int, str(version).split('.'))) > tuple(map(int, str(than).split('.')))
    except ValueError: return False

VERSION = plugin_version(HERE.parent)
HOME = Path(os.environ.get('LORD_HOME', Path.home() / '.claude/lord')).expanduser()
# Each plugin version runs its own daemon on its own socket, so an update never waits for an older daemon's agents
# to finish; the older daemon exits once its sessions have moved on. Daemons before 2.0.18 all used lordd.sock.
# LORD_DAEMON_SOCKET overrides it, and is how a vendor process's Lord tools reach the daemon that started it.
LEGACY_SOCKET = HOME / 'lordd.sock'
SOCKET = Path(os.environ['LORD_DAEMON_SOCKET']) if os.environ.get('LORD_DAEMON_SOCKET') else HOME / f'lordd-{VERSION}.sock'
LIMIT = 8 * 1024 * 1024
# A row's output as Claude Code keeps it: the end of what the step printed, where errors land.
ROW_OUTPUT = 2000
EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max']
RANKS = ['minimal', *EFFORTS, 'ultra']
INSTALL = {'gpt': "install OpenAI's codex CLI (brew install --cask codex, or npm install -g @openai/codex) and sign in with `codex login`",
           'grok': "install xAI's grok CLI (curl -fsSL https://x.ai/cli/install.sh | bash) and sign in by running `grok` once",
           'gemini': "install Google Antigravity's agy CLI (curl -fsSL https://antigravity.google/cli/install.sh | bash) and sign in by running `agy` once"}
SIGN_IN = {'gpt': 'codex is not signed in; run `codex login`', 'grok': 'grok is not signed in; run `grok` once',
           'gemini': 'agy is not signed in; run `agy` once'}

def sign_in_hint(family, text):
    # Without a login codex answers a turn with "401 Unauthorized" and grok a new session with "Authentication required";
    # neither says how to sign in.
    text = str(text)
    if family == 'gpt' and 'failed to load workspace requirements' in text:
        # A running codex fails every later turn once a reload of its settings fails, as after an edit to its config.
        return f'{text} (codex could not reload its settings, as after an edit to ~/.codex/config.toml; the next turn starts a new codex on the same conversation)'
    return f'{text} ({SIGN_IN[family]})' if re.search(r'\b401\b|Unauthorized|Authentication required', text) else text

# grok imports Claude Code's tool servers, hooks and skills by default; a lord agent keeps grok's own setup only.
GROK_NO_CLAUDE_IMPORTS = {f'GROK_{vendor}_{cell}_ENABLED': 'false' for vendor in ('CLAUDE', 'CURSOR') for cell in ('MCPS', 'HOOKS', 'SKILLS')}

def codex_config(folder):
    try:
        import tomllib
        return tomllib.loads((folder / 'config.toml').read_text())
    except (OSError, ValueError): return {}

# What Lord's codex folder shares with the user's, as links: codex writes its login file in place and its settings
# writer follows links, so both stay the user's own files. Threads and their database stay in Lord's folder, which
# the Codex app, reading only the user's folder, does not list.
# mcp-oauth-locks holds the locks codex takes to update or refresh tool servers' sign-ins, which are shared through
# the keychain; it is made in the user's folder first, so both folders take the same locks.
CODEX_SHARED = ('auth.json', 'config.toml', 'AGENTS.md', 'AGENTS.override.md', '.env', 'agents', 'skills', 'plugins', 'rules',
                'mcp-oauth-locks')

def codex_home_ready(home, source):
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    (source / 'mcp-oauth-locks').mkdir(exist_ok=True)
    for name in CODEX_SHARED:
        # codex makes skills/.system at start, so a start before the user had skills leaves a folder of codex's own.
        if name == 'skills' and (source / name).is_dir() and not (home / name).is_symlink() and (home / name).is_dir() \
                and {p.name for p in (home / name).iterdir()} <= {'.system'}:
            shutil.rmtree(home / name)
        if os.path.lexists(source / name) and not os.path.lexists(home / name):
            # A link that cannot be made stops the start: codex without it would run without the user's setup.
            try: (home / name).symlink_to(source / name)
            except FileExistsError: pass

def codex_relative_paths(value, source, key=()):
    # Settings naming a file relative to the codex folder, which codex resolves against the folder it runs in. Tool
    # server entries are not resolved when the settings load.
    if isinstance(value, dict):
        for k, v in value.items():
            if key or k != 'mcp_servers': yield from codex_relative_paths(v, source, key + (k,))
    elif isinstance(value, list):
        for v in value: yield from codex_relative_paths(v, source, key)
    elif isinstance(value, str) and value and '\n' not in value and len(value) < 1024 and not value.startswith(('/', '~')):
        try:
            if (source / value).exists() and (Path(value).parts or ('.',))[0] not in CODEX_SHARED: yield '.'.join(key)
        except (OSError, ValueError): pass

def codex_home_unshareable(source):
    # Why Lord's folder cannot run the user's codex as before, or None. Each reason is something tied to the folder's
    # path: codex names a keychain login after its folder (in auto mode too, at every save of the login), trusts a
    # hook by the path it read it from, resolves relative paths in the settings against its folder, writes tool
    # sign-ins in .credentials.json only as a regular file and removes it with the last sign-in, and a sqlite_home or
    # log_dir would put the threads back where the user's codex lists them. hooks.state holds trust for project hooks,
    # which are found by the project's path.
    config = codex_config(source)
    hooks = config.get('hooks', {})
    if not (source / 'auth.json').exists(): return f'{source / "auth.json"} is missing (a keychain login cannot be shared)'
    if config.get('cli_auth_credentials_store', 'file') != 'file': return 'cli_auth_credentials_store is not file'
    if (source / 'hooks.json').exists() or not isinstance(hooks, dict) or set(hooks) - {'state'}: return 'you have codex hooks of your own'
    if (source / '.credentials.json').exists(): return 'tool sign-ins are kept in .credentials.json'
    # This store encrypts sign-ins under a key named after the codex folder.
    features = config.get('features', {})
    if isinstance(features, dict) and features.get('secret_auth_storage'): return 'features.secret_auth_storage is on'
    if found := [k for k in ('sqlite_home', 'log_dir') if k in config]: return f'{found[0]} is set'
    if found := list(codex_relative_paths(config, source)): return f'{found[0]} names a file relative to {source}'
    return None

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
    numbers = [(k, config[k]) for k in ('memory_ceiling_kb', 'idle_exit_s', 'process_idle_s', 'prune_days', 'capacity_retry_s', 'capacity_retries') if k in config]
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

def searched(item):
    # What a web search looked for: codex fills `query` only once the search is done, and names a page it opened or
    # searched in `action`.
    action = item.get('action') if isinstance(item.get('action'), dict) else {}
    return item.get('query') or action.get('query') or ' '.join(map(str, action.get('queries') or [])) or \
        ' '.join(str(action[k]) for k in ('url', 'pattern') if action.get(k))

def codex_activity(item):
    # One line per action that touches the machine; messages and reasoning are not activity.
    kind = item.get('type')
    if kind == 'commandExecution': return '$ ' + str(item.get('command'))
    if kind == 'fileChange': return 'edit ' + ', '.join(str(c.get('path')) for c in item.get('changes', []))
    if kind == 'mcpToolCall': return f"tool {item.get('server')}.{item.get('tool')} {dumps(item.get('arguments'))[:300]}"
    if kind == 'webSearch': return 'search ' + searched(item)
    if kind == 'subAgentActivity':
        return f"helper {item.get('kind')} {item.get('agentPath')} {item.get('model') or ''} {item.get('reasoningEffort') or ''}".rstrip()
    if kind == 'collabAgentToolCall': return f"helpers {item.get('tool')} {' '.join(item.get('receiverThreadIds') or [])}".rstrip()
    if kind in ('userMessage', 'agentMessage', 'reasoning', None): return None
    return str(kind)

def codex_reads(item):
    # (kind, path) of each file a codex step read, listed, searched or edited, as codex itself parsed the command.
    if item.get('type') == 'fileChange': return [('edit', str(c.get('path'))) for c in item.get('changes', []) if isinstance(c, dict) and c.get('path')]
    if item.get('type') != 'commandExecution': return []
    found = []
    for action in item.get('commandActions') or []:
        if isinstance(action, dict) and action.get('type') in ('read', 'listFiles', 'search') and action.get('path'):
            path = str(action['path'])
            if not path.startswith('/') and item.get('cwd'): path = os.path.normpath(os.path.join(str(item['cwd']), path))
            found.append(({'listFiles': 'list'}.get(action['type'], action['type']), path))
    return found

def error_kind(error):
    # codex's classification of a failed turn (codexErrorInfo): a word, or an object keyed by one.
    info = error.get('codexErrorInfo') if isinstance(error, dict) else None
    return info if isinstance(info, str) else next(iter(info), None) if isinstance(info, dict) and info else None

def transcript_path(family, session, cwd, folder):
    # Where the vendor keeps this session's own transcript, when it is there; folder is the vendor's own folder.
    if not session or not folder or not re.fullmatch(r'[A-Za-z0-9_.-]+', session): return None
    folder = Path(folder)
    if family == 'gpt':
        found = sorted(folder.glob(f'sessions/*/*/*/rollout-*-{session}.jsonl'))
        return str(found[-1]) if found else None
    path = folder / 'sessions' / quote(cwd, safe='') / session if family == 'grok' else folder / 'antigravity-cli/conversations' / f'{session}.db'
    return str(path) if path.exists() else None

def allowance_buckets():
    # codex's reported allowance, by quota bucket; a file edited by hand or cut short is read as empty.
    try: found = json.loads((HOME / 'allowance.json').read_text(encoding='utf-8'))
    except (OSError, ValueError): return {}
    gpt = found.get('gpt') if isinstance(found, dict) else None
    return {k: v for k, v in gpt.items() if isinstance(v, dict)} if isinstance(gpt, dict) else {}

def settings_line(s):
    # The effective settings as one log line and one line of the plugin's launch notice.
    return ' · '.join([f"cwd {s['cwd']}", f"dirs {', '.join(s['dirs']) or 'none'}", f"sandbox {s['sandbox']}",
                       f"network {'on' if s['network'] else 'off'}", f"tools {'on' if s['tools'] else 'off'}", f"nice {s['nice']}",
                       f"memory {s['memory_gb']} GB", f"hang {s['hang_s'] or 'none'}{' s' if s['hang_s'] else ''}",
                       f"wall {s['wall_s'] or 'none'}{' s' if s['wall_s'] else ''}"])

def helper_seen(a, item):
    # A codex helper agent starting, from its parent's (or a helper's) item: subAgentActivity in codex's current helper
    # mode, a spawnAgent call (its new thread in receiverThreadIds) in the older one. Its later events arrive under its
    # own thread id.
    if item.get('type') == 'subAgentActivity': threads, path = [item.get('agentThreadId')], item.get('agentPath')
    elif item.get('type') == 'collabAgentToolCall' and item.get('tool') == 'spawnAgent': threads, path = item.get('receiverThreadIds') or [], None
    else: return
    for thread in threads:
        if not isinstance(thread, str) or not thread or thread == a.vendorSession: continue
        known = a.helpers.setdefault(thread, {'thread': thread, 'path': str(path or thread), 'model': None, 'effort': None,
                                              'started': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'commands': 0})
        known['model'] = known['model'] or item.get('model')
        known['effort'] = known['effort'] or item.get('reasoningEffort')

# codex's error kinds where the vendor declined the content: the session is intact and a reworded message resumes it.
REFUSALS = ('cyberPolicy', 'misalignmentPolicyViolation')

def codex_step(item):
    # (kind, title, log line) of a codex tool item. A command's title is the command as the model wrote it, without
    # codex's `/bin/zsh -lc` wrapper, when codex reports it as one action.
    line = codex_activity(item)
    if item.get('type') == 'commandExecution':
        actions = item.get('commandActions') or []
        command = actions[0].get('command') if len(actions) == 1 and isinstance(actions[0], dict) else None
        return 'command', str(command or item.get('command')), line
    return 'step', line, line

def codex_output(item):
    kind = item.get('type')
    if kind == 'commandExecution':
        code = item.get('exitCode')
        return str(item.get('aggregatedOutput') or '') + (f'\n(exit code {code})' if code not in (0, None) else '')
    if kind == 'fileChange':
        return '\n'.join(str(c.get('diff') or f"{c.get('kind', 'changed')} {c.get('path')}") for c in item.get('changes', []) if isinstance(c, dict))
    value = item.get('result') if item.get('result') is not None else item.get('error')
    return value if isinstance(value, str) else dumps(value) if value is not None else ''

def grok_step(update):
    # (kind, title, log line) of a grok tool call, from its first report: a command, or the tool's label and target.
    meta = (update.get('_meta') or {}).get('x.ai/tool') or {}
    raw = update.get('rawInput') or {}
    if (meta.get('kind') or update.get('kind')) == 'execute' and raw.get('command'):
        return 'command', str(raw['command']), '$ ' + str(raw['command'])
    target = next((raw[k] for k in ('target_file', 'file_path', 'path', 'query', 'pattern', 'url') if raw.get(k)), '')
    title = ' '.join(str(v) for v in (meta.get('label') or update.get('title') or meta.get('name') or 'tool', target) if v)
    return 'step', title, title

def grok_output(update):
    parts = []
    for item in update.get('content') or []:
        if item.get('type') == 'diff': parts.append(f"{item.get('path')}\n--- before\n{item.get('oldText') or ''}\n+++ after\n{item.get('newText') or ''}")
        elif isinstance(item.get('content'), dict) and item['content'].get('type') == 'text': parts.append(item['content'].get('text') or '')
    code = (update.get('rawOutput') or {}).get('exit_code')
    return '\n'.join(parts) + (f'\n(exit code {code})' if code not in (0, None) else '') + ('\n(failed)' if update.get('status') == 'failed' else '')

def agy_activity(step):
    # A tool step as agy names it, with its parameters: the file, folder, search or command it used.
    info = step.get('tool_info') or {}
    name = info.get('name') or step.get('tool_name')
    if not name: return str(step.get('command') or step.get('step_type', 'Working'))
    return f"{name} {dumps(info['parameters'])[:300]}" if info.get('parameters') else str(name)

def agy_title(step):
    # A row's title for an agy tool step: the tool's name and its parameters' values, as a reader would write them.
    info = step.get('tool_info') or {}
    values = ' '.join(str(v) for v in (info.get('parameters') or {}).values())
    return f"{info.get('name') or step.get('tool_name')} {values}".strip()[:2000]

def said(value):
    # A vendor's error as its message, not as the object that carries it.
    return value['message'] if isinstance(value, dict) and isinstance(value.get('message'), str) and value['message'] else value

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
        # Names may carry a prefix (GITHUB_TOKEN, client_secret, "access_token"); a scheme word (Basic, Token) goes with
        # its value.
        return re.sub(r'(?i)(["\x27]?(?<![A-Za-z0-9])[\w-]*(?:api[-_]?key|authorization|password|passwd|secret|token)[\w-]*["\x27]?\s*[:=]\s*["\x27]?)'
                      r'(?!\[REDACTED\])(?:(?:basic|token|digest|bearer)\s+)?[^\s\"\x27,;}]+', r'\1[REDACTED]', value)

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

async def kill_tree(process, known=()):
    # Tool commands can leave the vendor's process group, so every descendant is signalled by pid as well.
    # macOS answers killpg with EPERM when the group holds only zombies; the pid signals below still run.
    # A vendor that already exited leaves its descendants with launchd as parent: those recorded by the guard
    # (`known`) are signalled when they are still orphans, and never a pid that has since gone to another parent.
    if process is None: return
    children = (await process_table())[0]
    parent = {child: ppid for ppid, kids in children.items() for child in kids}
    pids = tree(children, process.pid)
    orphans = {pid for pid in known if parent.get(pid) == 1 or parent.get(pid) in known}
    pids = [*pids, *(orphans - set(pids))]
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

# The read list stops at the activity log's size, with a last line saying so.
READS_LIMIT = 4 * 1024 * 1024
READS_FULL = '[the read list stopped at 4 MB; later reads are in the activity log]'

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
        self.earlier_done = False
        # This turn's commands, retries after "at capacity", and whether it edited a file; and codex's error for it.
        self.commands, self.retries, self.edited, self.last_command, self.turn_error, self.retry_task = 0, 0, False, None, None, None
        self.retry_waiting = False
        self.final_text, self.restart, self.token = '', False, uuid.uuid4().hex
        self.default_model, self.agy_step = False, None
        # Each vendor tool step's result, by step id, for the plugin's row of it (/row).
        self.rows = {}
        self.log_path = HOME / 'logs' / f"{record['agentId']}.log"
        self.answer_path = self.log_path.with_suffix('.answer.md')
        # Beside the log, and removed with it by pruning: the turn record, each turn's answer, the files it read.
        self.record_path, self.answers_path = self.log_path.with_suffix('.json'), self.log_path.with_suffix('.answers')
        self.reads_path, self.reads = self.log_path.with_suffix('.reads.txt'), None
        # codex helper threads this agent started: thread id -> {path, model, effort}.
        self.helpers = {}

    def files(self):
        return [self.log_path, self.log_path.with_suffix('.log.1'), self.answer_path, self.record_path, self.reads_path,
                self.record_path.with_name(self.record_path.name + '.tmp')]

    def read_paths(self, found, helper=None):
        # Each path once, in the order first seen, so a reviewer can show what the agent and its helpers read.
        if not found: return
        if self.reads is None:
            try: self.reads = set(self.reads_path.read_text(encoding='utf-8').splitlines())
            except OSError: self.reads = set()
        lines = [line for line in (f'{kind} {path}' + (f' ({helper})' if helper else '') for kind, path in found) if line not in self.reads]
        if not lines or READS_FULL in self.reads: return
        self.reads.update(lines)
        try: size = self.reads_path.stat().st_size
        except OSError: size = 0
        written = []
        for line in (REDACT(line)[:2000] + '\n' for line in lines):
            if size + len(line.encode()) > READS_LIMIT:
                written.append(READS_FULL + '\n')
                self.reads.add(READS_FULL)
                break
            written.append(line)
            size += len(line.encode())
        self.reads_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.reads_path.open('a', encoding='utf-8') as file: file.write(''.join(written))

    def note(self, text):
        # The agent's activity log: what the vendor ran and read, for review provenance.
        self.log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # At 4 MB the log moves to <agentId>.log.1 (one earlier generation), so its newest records are always written.
        if self.log_path.exists() and self.log_path.stat().st_size >= 4 * 1024 * 1024: self.log_path.replace(self.log_path.with_suffix('.log.1'))
        # One line per record, so every line starts with its time: a multi-line command reads as one line.
        line = re.sub(r'\r\n|[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]', ' ⏎ ', REDACT(text)[:2000])
        with self.log_path.open('a', encoding='utf-8') as file: file.write(f'{datetime.now(timezone.utc).isoformat()} {line}\n')

    def row(self, rid, kind, title, line=None):
        # A vendor tool step starting: a row in Claude Code's view of the agent. kind 'command' is a shell command
        # (a Bash row); 'step' is any other tool step. The activity log keeps its usual line.
        if self.state not in ('running', 'waiting_tool') or rid in self.rows: return
        self.rows[rid] = asyncio.get_running_loop().create_future()
        self.emit('row', rowId=rid, shell=kind == 'command', title=REDACT(title)[:2000], text=line or title)

    def row_done(self, rid, output, stopped=False):
        future = self.rows.get(rid)
        if future is None or future.done(): return
        text = REDACT(output)
        if len(text) > ROW_OUTPUT: text = f'[{len(text) - ROW_OUTPUT} earlier characters not shown]\n{text[-ROW_OUTPUT:]}'
        future.set_result({'output': text, 'stopped': stopped})

    def turn_record(self, kind, value):
        # The turn as a program reads it: logs/<agentId>.json (the settings and every turn, the last 100 with their
        # answers in <agentId>.answers/), and the plugin's one-line footer on the answer.
        ended = datetime.now(timezone.utc).isoformat(timespec='seconds')
        how = 'done' if kind == 'done' else 'interrupted' if value.get('message') == 'interrupted' or self.cancel_requested else f"error ({value.get('cause') or 'vendor'})"
        settings = getattr(self, 'settings', None) or {}
        transcript = transcript_path(self.family, self.vendorSession, self.cwd, settings.get('codex_folder'))
        try: saved = json.loads(self.record_path.read_text(encoding='utf-8'))
        except (OSError, ValueError): saved = {}
        saved = saved if isinstance(saved, dict) else {}
        turns = saved.get('turns', [])
        # Helpers the record named before a daemon restart stay, so earlier turns' helper ids keep their models.
        for helper in saved.get('helpers') if isinstance(saved.get('helpers'), list) else []:
            if isinstance(helper, dict) and isinstance(helper.get('thread'), str) and isinstance(helper.get('started'), str):
                self.helpers.setdefault(helper['thread'], helper)
        # A record edited by hand or cut short is read for what it still holds, never a reason the turn cannot end.
        turns = [turn for turn in turns if isinstance(turn, dict)] if isinstance(turns, list) else []
        last = turns[-1].get('turn') if turns else 0
        number = (last if isinstance(last, int) and not isinstance(last, bool) else len(turns)) + 1
        answer = self.answers_path / f'{number}.md'
        text = value.get('text') if kind == 'done' else 'lord: error: ' + str(value.get('message'))
        started = getattr(self, 'turn_started', None) or ended
        mine = [h for h in self.helpers.values() if h['started'] >= started]
        turn = {'turn': number, 'started': started, 'ended': ended, 'ended_how': how,
                'commands': getattr(self, 'commands', 0), 'retries': getattr(self, 'retries', 0),
                'helpers': [h['thread'] for h in mine],
                **({'usage': value.get('usage')} if kind == 'done' else {'error': str(value.get('message'))}),
                **({'answer': str(answer)} if text and text.strip() else {})}
        document = {'agentId': self.agentId, 'family': self.family, 'lordVersion': VERSION, 'session': self.record.get('session'),
                    **{k: v for k, v in settings.items() if k != 'codex_folder'}, 'vendorSession': self.vendorSession,
                    'transcript': transcript, 'helpers': list(self.helpers.values()), 'turns': [*turns, turn][-100:]}
        try:
            if text and text.strip():
                self.answers_path.mkdir(parents=True, exist_ok=True, mode=0o700)
                answer.write_text(REDACT(text), encoding='utf-8')
            # Every turn advances the number, so removing the answer 100 turns back keeps only the recorded turns'.
            (self.answers_path / f'{number - 100}.md').unlink(missing_ok=True)
            scratch = self.record_path.with_name(self.record_path.name + '.tmp')
            scratch.write_text(dumps(document) + '\n', encoding='utf-8')
            scratch.replace(self.record_path)
        except OSError: pass
        helpers = [h.get('model') or '?' for h in mine]
        return {'ended_how': how, 'model': self.model, 'effort': self.effort, 'started': turn['started'], 'ended': ended,
                'commands': turn['commands'], 'retries': turn['retries'], 'helpers': helpers, 'session': self.vendorSession}

    def undelivered(self):
        return bool(self.carry) or not self.events.empty()

    def emit(self, kind, **value):
        if self.state not in ('running', 'waiting_tool'): return
        if kind == 'text': self.answer += value['text']
        if kind in ('activity', 'row'):
            self.activity = value['text'][:300]
            self.note(value['text'])
        if kind == 'error':
            value['message'] = sign_in_hint(self.family, value['message'])
            self.note('error: ' + str(value['message']))
        if kind in ('done', 'error'):
            # A turn that ends while its "at capacity" retry waits (a guard, a crash) takes the retry with it.
            if self.retry_waiting and self.retry_task and not self.retry_task.done(): self.retry_task.cancel()
            self.retry_waiting = False
            value['record'] = self.turn_record(kind, value)
            self.state, self.activity = 'idle', ''
            self.finished.set()
            for future in self.calls.values():
                if not future.done(): future.set_result({'content': [{'type': 'text', 'text': 'Turn ended'}], 'isError': True})
            # A step still open when the turn ends has no result to wait for.
            for future in self.rows.values():
                if not future.done(): future.set_result({'output': '', 'stopped': kind == 'error'})
        self.events.put_nowait({'type': kind, **value})
        if kind == 'done' and value['text'].strip():
            # The latest turn's final answer, kept beside the activity log, so a read-only agent's findings
            # still land in a file.
            self.answer_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.answer_path.write_text(REDACT(value['text']), encoding='utf-8')

class Vendor:
    def __init__(self, daemon, family, agent=None, codex_home=None):
        self.d, self.family, self.agent = daemon, family, agent
        # An agent-less codex's folder; an agent's codex uses the folder in its record. None is the user's own.
        self.codex_home = codex_home
        self.process, self.serial, self.generation = None, 0, 0
        self.last, self.reason, self.efforts, self.descendants = time.monotonic(), None, [], set()
        self.pending, self.models = {}, []
        self.lock, self.write_lock = asyncio.Lock(), asyncio.Lock()
        self.stopping, self.ready = False, False
        # The vendor's own account of a failure, from its stderr (agy's AGY_ERROR line), and the task reading stderr.
        self.error, self.stderr_task = None, None
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
            # A missing program leaves nothing behind: no grok profiles, no agy entry.
            if not shutil.which(command[0], path=subscription_env().get('PATH')):
                raise RuntimeError(f'{command[0]} was not found; {INSTALL[self.family]}')
            # @nice: the vendor starts at that niceness and every command it runs inherits it; a sandboxed command
            # cannot lower its own priority. nice -n adds to the daemon's own niceness, which no one may lower, so a
            # daemon already at or past the level starts the vendor at its own.
            if self.agent and (step := self.agent.record.get('nice', 0) - os.getpriority(os.PRIO_PROCESS, 0)) > 0:
                command = ['/usr/bin/nice', '-n', str(step), *command]
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
            self.last, self.reason, self.error = time.monotonic(), None, None
            if self.family == 'grok': grok_profiles()
            env = subscription_env({**({} if self.agent and self.agent.record.get('tools') else GROK_NO_CLAUDE_IMPORTS),
                                    **({'LORD_AGENT_ID': self.agent.agentId, 'LORD_HOME': str(HOME), 'LORD_DAEMON_SOCKET': str(SOCKET), 'LORD_MCP_TOKEN': self.agent.token} if self.agent else {})})
            home = self.agent.record.get('codexHome') if self.agent else self.codex_home
            if self.family == 'gpt' and home:
                # The thread lives in Lord's folder and cannot move, so a setup that can no longer be shared stops
                # the start, saying why; the agent resumes once that is undone.
                if reason := codex_home_unshareable(self.d.user_codex_home):
                    fix = 'run `codex login`' if reason.endswith('cannot be shared)') else 'undo that'
                    raise RuntimeError(f"Lord's codex folder can no longer share your codex setup: {reason}. This agent's conversation is in {home}; {fix} and it resumes, or start a new agent to keep the change")
                codex_home_ready(Path(home), self.d.user_codex_home)
                env['CODEX_HOME'] = home
                # codex would keep its thread database in this folder instead, which may be the user's.
                env.pop('CODEX_SQLITE_HOME', None)
            try:
                self.process = await asyncio.create_subprocess_exec(*command,
                    cwd=self.agent.cwd if self.agent else HOME, start_new_session=True, env=env,
                    stdin=-1, stdout=-1, stderr=-1, limit=LIMIT)
            except FileNotFoundError: raise RuntimeError(f'{command[0]} was not found; {INSTALL[self.family]}') from None
            self.generation += 1
            self.d.task(self.read(self.process))
            self.stderr_task = self.d.task(self.stderr(self.process))
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
            text = line.decode('utf8', 'replace')
            self.d.log(text)
            if self.family == 'gemini' and text.startswith('AGY_ERROR: '): self.agy_error(text[len('AGY_ERROR: '):])

    def agy_error(self, text):
        # agy states a failure only on stderr, then exits: the short error replaces "closed stdout" as the turn's error.
        # A spent quota (429) names when it resets; until then the daemon refuses that model's turns (begin_starting).
        try: payload = json.loads(text)
        except ValueError: return
        short = payload.get('short_error') if isinstance(payload, dict) else None
        if not isinstance(short, str) or not short: return
        self.error = self.d.redact(short)
        reset = re.search(r'Resets in (?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?', short)
        if payload.get('error_code') == 429 and reset and any(reset.groups()) and self.agent:
            hours, minutes, seconds = (int(x or 0) for x in reset.groups())
            self.d.save_quota((self.family, self.agent.model), (time.time() + hours * 3600 + minutes * 60 + seconds, self.error))

    async def read(self, process):
        try:
            while line := await process.stdout.readline():
                if self.process is not process: return
                self.last = time.monotonic()
                value = json.loads(line)
                if 'id' in value and 'method' not in value:
                    future = self.pending.get(value['id'])
                    if future and not future.done():
                        if 'error' in value: future.set_exception(RuntimeError(sign_in_hint(self.family, self.d.redact(dumps(value['error'])))))
                        else: future.set_result(value.get('result', {}))
                elif 'id' in value: self.d.task(self.request(value))
                else: self.notification(value)
            raise RuntimeError(f'{self.family} process closed stdout')
        except asyncio.CancelledError: raise
        except Exception as error:
            if self.stopping or self.d.stopping or self.process is not process: return
            self.ready = False
            self.reaped.clear()
            # agy writes its reason to stderr just before it exits, at start-up too; let that reader finish first.
            if self.stderr_task and not self.stderr_task.done(): await asyncio.wait([self.stderr_task], timeout=1)
            for future in list(self.pending.values()):
                if not future.done(): future.set_exception(RuntimeError(self.error or str(error)))
            if self.family == 'gemini' and not self.initialized.done(): self.initialized.set_exception(RuntimeError(self.error or str(error)))
            reason, self.reason = self.reason, None
            for a in self.d.agents.values():
                if self.d.backend(a) is self:
                    a.generation = 0
                    a.emit('error', message=self.d.redact(reason or self.error or error), retryable=not (reason or self.error))
            self.d.log(error)
            try: await kill_tree(process, self.descendants)
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
                    info = payload.get('tool_info') or {}
                    if (info.get('name') or payload.get('tool_name')) and payload.get('step_index') is not None:
                        # agy reports a tool step when it starts (ACTIVE) and when it ends (DONE), with its output.
                        rid, name = f"agy-{payload['step_index']}", info.get('name') or payload.get('tool_name')
                        command = name == 'run_command' and (info.get('parameters') or {}).get('CommandLine')
                        a.row(rid, 'command' if command else 'step', command or agy_title(payload), agy_activity(payload))
                        if payload.get('state') == 'DONE':
                            a.row_done(rid, str(info.get('output') or ''))
                            if command: a.commands += 1
                    elif payload.get('step_index') is None or payload.get('step_index') != a.agy_step:
                        a.agy_step = payload.get('step_index')
                        a.emit('activity', text=agy_activity(payload))
            elif event == 'result' and a.state in ('running', 'waiting_tool'):
                if payload.get('status') != 'SUCCESS' or payload.get('error'):
                    a.emit('error', message=self.d.redact(payload.get('error') or payload.get('status')), retryable=True)
                else:
                    self.d.record_usage(a, usage(payload.get('usage', {})))
                    # agy's response joins every message of the turn; the answer is what it said after its last tool step, as
                    # for the other vendors, and the whole response only when it said nothing after one.
                    response = payload.get('response') if isinstance(payload.get('response'), str) else ''
                    a.emit('done', text=a.answer if a.answer.strip() else response or a.answer, usage=usage(payload.get('usage', {})), model=a.model)
            return
        if method == 'account/rateLimits/updated':
            self.d.allowance(p.get('rateLimits'))
            return
        sid = p.get('threadId') or p.get('sessionId')
        a = next((a for a in self.d.agents.values() if a.family == self.family and a.vendorSession == sid), None)
        if a is None and self.agent and sid in self.agent.helpers:
            self.helper_event(self.agent, self.agent.helpers[sid], method, p)
            return
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
                rid = update.get('toolCallId') or uuid.uuid4().hex
                if kind == 'tool_call':
                    step = grok_step(update)
                    # grok names a command only when it starts; its completion carries no command.
                    if step[0] == 'command' and rid not in a.rows: a.commands += 1
                    a.row(rid, *step)
                elif update.get('status') in ('completed', 'failed'):
                    a.row(rid, *grok_step(update))
                    a.row_done(rid, grok_output(update))
        elif method == 'turn/started' and p['turn']['id'] not in a.ended_turns: a.turn_id = p['turn']['id']
        elif method == 'item/agentMessage/delta':
            a.items[p['itemId']] = a.items.get(p['itemId'], '') + p['delta']
            a.emit('text', text=p['delta'])
        elif method == 'item/completed' and codex_activity(p['item']):
            item = p['item']
            rid = item.get('id') or uuid.uuid4().hex
            helper_seen(a, item)
            a.row(rid, *codex_step(item))
            a.row_done(rid, codex_output(item))
            a.read_paths(codex_reads(item))
            if item.get('type') == 'commandExecution': a.commands, a.last_command = a.commands + 1, str(item.get('command'))[:300]
            if item.get('type') == 'fileChange': a.edited = True
        elif method == 'item/completed' and p['item'].get('type') == 'agentMessage':
            item = p['item']
            seen = a.items.get(item['id'], '')
            if item['text'].startswith(seen) and len(item['text']) > len(seen): a.emit('text', text=item['text'][len(seen):])
            a.items[item['id']] = item['text']
            a.final_text = item['text']
        elif method == 'item/started' and codex_activity(p['item']):
            # A message before a tool action is narration; the answer is the agent message after the last action.
            a.final_text = ''
            helper_seen(a, p['item'])
            # A search names what it looks for only when done; its row then starts and ends at once.
            if p['item'].get('type') != 'webSearch' or searched(p['item']): a.row(p['item'].get('id') or uuid.uuid4().hex, *codex_step(p['item']))
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
            elif a.cancel_requested or turn['status'] == 'interrupted':
                a.emit('error', message='interrupted' if a.cancel_requested else str(said(turn.get('error')) or 'interrupted'), retryable=False)
            else: self.failed(a, turn.get('error') or a.turn_error or {}, turn['status'], turn.get('id'))
        elif method == 'error' and not p.get('willRetry', False):
            if p.get('turnId') and p['turnId'] == a.turn_id:
                # codex follows a turn's error with that turn's completion, which ends the turn; if none comes, the
                # error ends it.
                a.turn_error = p.get('error') or {}
                self.d.task(self.unfinished(a, p['turnId']))
            else: a.emit('error', message=self.d.redact(said(p.get('error', p))), retryable=True, cause=error_kind(p.get('error')))
        elif method == 'model/rerouted': a.model = p['toModel']

    def failed(self, a, error, status, turn_id):
        # A codex turn that failed, by codex's own kind for it (codexErrorInfo).
        kind, message = error_kind(error), str(said(error) or status)
        if kind == 'serverOverloaded' and not a.edited and a.retries < self.d.config.get('capacity_retries', 2):
            # The model was at capacity before the turn changed anything: the same session tries again after a wait.
            a.retries += 1
            delay = self.d.config.get('capacity_retry_s', 30)
            a.ended_turns.add(turn_id)
            a.turn_id, a.turn_error = None, None
            a.note(f'vendor at capacity ({message}); retry {a.retries} in {delay} s on the same session')
            # Waiting from now, so an interrupt handled before the retry task first runs still cancels it.
            a.retry_waiting = True
            a.retry_task = self.d.task(self.d.retry(a, delay))
            return
        if kind in REFUSALS:
            message = f"refused by vendor ({kind}): {message}. The session is intact: send a reworded message to resume it" + \
                      (f'. Last completed command: {a.last_command}' if a.last_command else '')
        elif kind == 'usageLimitExceeded': self.d.gpt_quota(a, message)
        # A codex process can fail every later turn (a settings reload that failed), so after any other failure the
        # next turn gets a new process that resumes the thread.
        elif kind not in ('rateLimitExceeded', 'serverOverloaded'): a.restart = True
        a.emit('error', message=self.d.redact(message), retryable=True, cause=kind)

    async def unfinished(self, a, turn_id):
        await asyncio.sleep(5)
        if a.state in ('running', 'waiting_tool') and a.turn_id == turn_id and turn_id not in a.ended_turns: self.failed(a, a.turn_error or {}, 'failed', turn_id)

    def helper_event(self, a, helper, method, p):
        # A codex helper's steps go to its parent's log and read list only: no row, so nothing reaches a model.
        item = p.get('item') or {}
        if method == 'item/started': helper_seen(a, item)
        if method != 'item/completed' or not (line := codex_activity(item)): return
        helper_seen(a, item)
        a.note(f"[{helper['path']}] {line}")
        a.read_paths(codex_reads(item), helper['path'])
        if item.get('type') == 'commandExecution': helper['commands'] = helper.get('commands', 0) + 1
        if item.get('type') == 'fileChange': a.edited = True

    async def close(self):
        self.stopping, self.ready = True, False
        for future in [*self.pending.values(), getattr(self, 'initialized', None)]:
            if future and not future.done(): future.set_exception(RuntimeError('Vendor closed'))
        process = self.process
        await kill_tree(process, self.descendants)
        if self.process is process: self.process = None

class Daemon:
    def __init__(self):
        HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
        HOME.chmod(0o700)
        self.config = load_config()
        self.agents, self.vendors, self.tasks, self.connections = {}, {}, set(), set()
        self.redact, self.stopping, self.last_request = Redactor(), False, time.monotonic()
        self.requests, self.control, self.retire = 0, None, False
        # One lock per agent: a slow vendor start delays only its own agent's /open.
        self.stop_event, self.open_locks, self.prune_lock = asyncio.Event(), {}, asyncio.Lock()
        # agy_models keeps agy's listing order: its first model is the fallback for a default agy does not offer.
        self.agy_registered, self.agy_levels, self.agy_models = False, {}, {}
        # (family, model) -> (when a spent quota resets, the vendor's error), from the vendor's own report. Kept in
        # quota.json, so a daemon started before the reset still refuses.
        self.quota = {}
        self.load_quota()
        # Daemons of every version from 2.0.18 append to journal.jsonl and merge when they rewrite it. sessions.jsonl
        # belongs to earlier daemons, which rewrite it with only their own agents; it is read, never written.
        self.journal, self.legacy_journal = HOME / 'journal.jsonl', HOME / 'sessions.jsonl'
        self.touched, self.pruned = set(), set()
        found = self.records(self.journal if self.journal.exists() else self.legacy_journal)
        # A first journal starts with every earlier record, so the next rewrite keeps them.
        if not self.journal.exists(): self.touched = set(found)
        for aid, record in found.items(): self.agents[aid] = Agent(record)

    def records(self, *paths):
        # The newest record of each agent: later lines, and later files, replace earlier ones.
        found = {}
        for path in paths:
            try: lines = path.read_text().splitlines()
            except OSError: continue
            for line in lines:
                try:
                    record = json.loads(line)
                    found[record['agentId']] = record
                except (ValueError, KeyError, TypeError): self.log('Invalid journal line ignored')
        return found

    def refresh(self, aid=None):
        # A daemon of another version may have run an agent since this one read the journal. A record this daemon
        # holds gives way to a newer one there only while this daemon runs nothing for it; aid None adds unknown agents.
        found = self.records(self.legacy_journal, self.journal)
        for key in [aid] if aid else list(found):
            record, a = found.get(key), self.agents.get(key)
            if record is None or key in self.pruned: continue
            if a is None: self.agents[key] = Agent(record)
            elif aid and record.get('used', '') > a.record.get('used', '') and a.state in ('idle', 'closed') and not a.stream_lock.locked():
                v = self.vendors.get(key)
                if not (v and v.process and v.process.returncode is None): self.agents[key] = Agent(record)

    @contextmanager
    def journal_lock(self):
        with (HOME / 'journal.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def log(self, text):
        path = HOME / 'lordd.log'
        line = f'{datetime.now(timezone.utc).isoformat()} {self.redact(text).strip()[:8192]}\n'
        if path.exists() and path.stat().st_size + len(line.encode()) > 1024 * 1024: path.replace(HOME / 'lordd.log.1')
        with path.open('a', encoding='utf-8') as file: file.write(line)

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

    def service(self, home=None):
        # A codex process owned by no agent, for archiving pruned threads: one per codex folder.
        return self.vendors.setdefault(None if home is None else ('service', home), Vendor(self, 'gpt', codex_home=home))

    async def agy_mcp(self, program):
        # agy has no per-run MCP option. One user-level entry serves every agy process; the server reads its
        # agent id from LORD_AGENT_ID and lists no tools in agy sessions the daemon did not start. It is added (or
        # updated) at every agy start, because a daemon of another version may have removed it on exit.
        code, out = await run_quick([*program, 'mcp', 'add', 'lord', '--', sys.executable, str(HERE), '--mcp'])
        # The program that added the entry removes it when the daemon exits (close); a copy, because the caller goes
        # on to append agy's run options to its list.
        if code == 0: self.agy_registered = list(program)
        else: self.log(f'agy mcp add failed; gemini agents lack lord tools until it succeeds: {out}')

    def load_quota(self):
        # quota.json is the one record for daemons of every version: what it holds is what is refused, so deleting the
        # file, or an entry, clears a refusal at once. A malformed entry is ignored, never a reason not to start.
        try: found = json.loads((HOME / 'quota.json').read_text(encoding='utf-8'))
        except (OSError, ValueError): found = {}
        quota = {}
        for key, value in (found.items() if isinstance(found, dict) else []):
            parts = key.split(' ', 1)
            if len(parts) == 2 and isinstance(value, list) and len(value) == 2 and isinstance(value[0], (int, float)) \
                    and not isinstance(value[0], bool) and time.time() < value[0] < time.time() + 366 * 86400 and isinstance(value[1], str):
                quota[(parts[0], parts[1])] = (float(value[0]), value[1])
        self.quota = quota

    def save_quota(self, key, value):
        # Under the journal lock, so a reset another daemon wrote meanwhile is kept.
        try:
            with self.journal_lock():
                self.load_quota()
                self.quota[key] = value
                # One scratch name under the lock, replaced by the next write, so a failed one leaves nothing new.
                scratch = HOME / 'quota.json.tmp'
                scratch.write_text(dumps({f'{family} {model}': list(entry) for (family, model), entry in self.quota.items()}), encoding='utf-8')
                scratch.replace(HOME / 'quota.json')
        except OSError as error: self.log(f'quota.json not written: {error}')

    def record_usage(self, a, tokens):
        a.record['usage'] = tokens
        self.save(a)

    def save(self, a):
        a.record.update({k: getattr(a, k) for k in ('vendorSession', 'model', 'effort', 'total')}, used=datetime.now(timezone.utc).isoformat())
        with self.journal_lock(), self.journal.open('a') as file: file.write(dumps(a.record) + '\n')
        self.touched.add(a.agentId)

    def server(self, a):
        env = {'LORD_HOME': str(HOME), 'LORD_DAEMON_SOCKET': str(SOCKET), 'LORD_MCP_TOKEN': a.token, **({'LORD_READONLY': '1'} if a.record.get('readonly') else {})}
        return {'name': 'lord', 'command': sys.executable, 'args': [str(HERE), '--mcp', a.agentId], 'env': env}

    async def attach(self, a):
        async with a.lock:
            vendor = self.backend(a)
            try: await vendor.ensure()
            except Exception as error:
                # A start that failed on a spent quota is not retried. An agent never saved yet has no log pruning would
                # remove, so its refusal goes to lordd.log.
                self.quota_refusal(a.family, a.model, a if a.record.get('used') else None, a.agentId)
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
                user = {} if a.record.get('tools') else codex_config(Path(a.record['codexHome']) if a.record.get('codexHome') else self.user_codex_home)
                lean = {'mcp_servers': {name: {'enabled': False} for name in user.get('mcp_servers', {}) if name != 'lord'},
                        'plugins': {name: {'enabled': False} for name in user.get('plugins', {})}}
                if user.get('notify'): lean['notify'] = []
                # Skills come from more places than the plugins listed in config.toml (a remote plugin cache, the
                # skills folder); with these off, codex lists none and each request is about 3,700 tokens shorter.
                if not a.record.get('tools'): lean['skills'] = {'include_instructions': False, 'bundled': {'enabled': False}}
                # A read-only agent starts no codex helper agents. codex picks its helper mode from agents.enabled,
                # then multi_agent_v2, then the model catalog (which turns helpers on for current models), and only
                # then from the multi_agent feature, so all three are set.
                if a.record.get('readonly'):
                    lean['features'] = {'multi_agent': False, 'multi_agent_v2': False}
                    lean['agents'] = {'enabled': False}
                params = {'cwd': a.cwd, 'model': a.model, 'approvalPolicy': 'never',
                          # @nosandbox: codex runs commands unconfined, as Claude Code's own agents do without its sandbox.
                          'sandbox': 'read-only' if a.record.get('readonly') else 'danger-full-access' if a.record.get('nosandbox') else 'workspace-write',
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
                    current = next((o.get('currentValue') for o in result.get('configOptions', []) if o.get('id') == 'model'), None)
                    self.unlisted(a, 'grok', options['model'], current if current in options['model'] else options['model'][0])
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
            if models and model is None:
                model = next((m for m in models if m.get('isDefault')), models[0])
                self.unlisted(a, 'codex', [m['id'] for m in models], model['id'])
            allowed = [v['reasoningEffort'] for v in (model or {}).get('supportedReasoningEfforts', [])]
        # Before grok reports its levels (a resumed session may not), its documented range applies.
        elif a.family == 'grok': allowed = self.backend(a).efforts or ['low', 'medium', 'high', 'xhigh']
        else:
            try: allowed = await self.agy_efforts(a.model)
            except ValueError:
                # agy listed its models, and this one is not among them.
                self.unlisted(a, 'agy', sorted(self.agy_models), next(iter(self.agy_models)))
                allowed = await self.agy_efforts(a.model)
        if allowed and value not in allowed:
            value = min((v for v in allowed if v in RANKS), key=lambda v: abs(RANKS.index(v) - RANKS.index(value)), default=value)
        a.effort = value

    def unlisted(self, a, tool, listed, fallback):
        # A model someone named is refused. A new agent's configured default gives way to the vendor's own default, so
        # an account that lacks the default, or a vendor that retired it, still gets an agent; /lord shows the model.
        if not a.default_model: raise ValueError(f'unknown {a.family} model {a.model}; {tool} lists {", ".join(listed)}')
        self.log(f'{a.agentId}: {tool} does not offer {a.model}; using {fallback}')
        a.model = fallback

    async def agy_efforts(self, model):
        # agy lists each model once per effort level, as <model>-<level>.
        if model not in self.agy_levels:
            config = self.config['gemini']
            program = config.get('program', 'agy')
            program = list(program) if isinstance(program, list) else [program]
            code, out = await run_quick([*program, 'models'])
            listed = [line.split('\t')[0].strip() for line in out.splitlines()] if code == 0 else []
            ids = set(listed)
            self.agy_models.update(dict.fromkeys(i.rsplit('-', 1)[0] for i in listed if i.rsplit('-', 1)[-1] in EFFORTS))
            if code != 0:
                # Not cached: the next open asks agy again. Until then agy's own --effort range applies.
                self.log(f'agy models failed: {out}')
                return EFFORTS
            self.agy_levels[model] = [v for v in EFFORTS if f'{model}-{v}' in ids]
        if self.agy_models and model not in self.agy_models:
            raise ValueError(f'unknown gemini model {model}; agy lists {", ".join(sorted(self.agy_models))}')
        return self.agy_levels[model]

    async def open(self, body):
        if not self.retire and newer(body.get('version'), VERSION):
            # A newer plugin's own daemon takes over once this one has finished its work (heartbeat).
            self.retire = True
            self.log(f'plugin {body["version"]} is newer than this daemon ({VERSION}); exiting once idle')
        aid, family = body['agentId'], body['family']
        if not isinstance(aid, str) or not aid or family not in ('gpt', 'grok', 'gemini'): raise ValueError('Invalid agent or family')
        async with self.open_locks.setdefault(aid, asyncio.Lock()):
            self.refresh(aid)
            if body.get('nosandbox') and family != 'gpt':
                raise ValueError({'grok': '@nosandbox is available for gpt only; grok keeps its sandbox',
                                  'gemini': '@nosandbox is not needed for gemini: agy has no sandbox'}[family])
            if body.get('readonly') and family == 'gemini': raise ValueError('readonly is not enforceable for agy; use lord:gpt or lord:grok')
            if body.get('dirs') and family != 'gpt':
                raise ValueError({'grok': "@dir is not available for grok: its sandbox confines writes to the working folder; use @cwd",
                                  'gemini': '@dir is not needed for gemini: agy has no sandbox and can write outside its folder'}[family])
            if not Path(body['cwd']).expanduser().is_absolute(): raise ValueError('the working folder must be an absolute path')
            cwd = str(Path(body['cwd']).expanduser().resolve(strict=True))
            # A folder that holds the home folder, by its usual path or through macOS's data volume (/System/Volumes).
            if inside(canon(str(Path.home())), (canon(cwd),)) or (sys.platform == 'darwin' and inside('/System/Volumes/Data', (cwd,))):
                raise ValueError('refusing / or a folder holding your home folder as a working folder: start Claude Code in a project folder, or put "@cwd <folder>" on the first line of the task')
            a = self.agents.get(aid)
            if a and body.get('pin_cwd') and not os.path.samefile(cwd, a.cwd): raise ValueError(f'@cwd is fixed at the first turn ({a.cwd})')
            if isinstance(body.get('path'), str) and body['path']:
                global SESSION_PATH
                SESSION_PATH = body['path']
            readonly = body.get('readonly') or bool(a and a.record.get('readonly'))
            if readonly and (body.get('nosandbox') or bool(a and a.record.get('nosandbox'))):
                raise ValueError('@readonly and @nosandbox contradict each other; an agent is one or the other')
            if family == 'gpt' and readonly and (body.get('net') or bool(a and a.record.get('net'))):
                raise ValueError("@readonly with @net is not read-only for gpt: with network on, codex can reach local services, Lord's included; use lord:grok for a read-only agent with network")
            # grok's read-only base profile leaves the temp folders and grok's own folder writable.
            if body.get('readonly') and family == 'grok' and inside(canon(a.cwd if a else cwd), (*TEMP_ROOTS, canon(str(grok_home())))):
                raise ValueError("grok's read-only profile leaves temp folders and ~/.grok writable; pin a read-only grok outside /tmp, /var/tmp, /var/folders and ~/.grok")
            # An agent with a turn running or a result not yet handed over still opens: /turn delivers that result first,
            # and the turn's start then refuses the new message.
            if not (a and (a.state in ('running', 'waiting_tool') or a.undelivered())):
                self.quota_refusal(family, body.get('model') or (a.model if a else self.config[family]['model']), a, aid)
            new = a is None
            saved = None if new else (json.loads(dumps(a.record)), a.restart)
            try: return await self.open_agent(aid, family, cwd, body, a)
            except BaseException:
                # A first /open that fails leaves no agent behind, so a corrected retry starts clean; a refused
                # later /open puts the agent's options back and reattaches with them on the next turn.
                if new and self.agents.pop(aid, None) is not None:
                    vendor = self.vendors.pop(aid, None)
                    if vendor: await vendor.close()
                    self.compact((aid,))
                elif saved and a.record != saved[0]:
                    # Saved, so a daemon restart does not bring the refused options back; a new process drops any
                    # sandbox or imports started for them.
                    a.record.clear(); a.record.update(saved[0])
                    a.restart, a.generation = True, 0
                    self.save(a)
                raise

    async def open_agent(self, aid, family, cwd, body, a):
        if a is None:
            if not Path(cwd).is_dir(): raise ValueError('cwd must be a directory')
            config = self.config[family]
            # A gpt agent's codex keeps its threads in Lord's own codex folder; an agent saved without one (by an
            # older daemon, with own_home off, or for a codex setup the folder cannot share) resumes in the user's
            # folder, where its thread is.
            own = family == 'gpt' and config.get('own_home', True) and not codex_home_unshareable(self.user_codex_home)
            a = Agent({**{k: body[k] for k in ('readonly', 'net', 'nosandbox') if k in body}, 'agentId': aid,
                **({'codexHome': str(HOME / 'codex')} if own else {}),
                'family': family, 'cwd': cwd, 'model': body.get('model') or config['model'],
                'effort': body.get('effort') or config['effort'], 'created': datetime.now(timezone.utc).isoformat()})
            # Until this first /open succeeds, a model nobody named may give way to the vendor's own default.
            a.default_model = not body.get('model')
            await self.effort(a)
            self.agents[aid] = a
        elif a.family != family: raise ValueError('agentId already belongs to another family')
        # Everything is checked before anything changes, so a refused /open leaves the agent as it was.
        for key in ('memory_gb', 'hang_s', 'wall_s'):
            if body.get(key) is not None and (not isinstance(body[key], (int, float)) or body[key] < 0 or (key == 'memory_gb' and body[key] == 0)):
                raise ValueError(f'invalid {key}')
        if body.get('nice') is not None and (isinstance(body['nice'], bool) or not isinstance(body['nice'], int) or not 0 <= body['nice'] <= 19):
            raise ValueError('@nice must be a whole number from 0 to 19')
        if relative := [x for x in body.get('dirs') or [] if not Path(x).expanduser().is_absolute()]: raise ValueError(f'@dir {relative[0]} is not an absolute path')
        dirs = [str(Path(x).expanduser().resolve()) for x in body.get('dirs') or []]
        if any(inside(canon(str(Path.home())), (canon(x),)) or (sys.platform == 'darwin' and inside('/System/Volumes/Data', (x,))) for x in dirs):
            raise ValueError('refusing / or a folder holding your home folder as an @dir')
        if missing := [x for x in dirs if not Path(x).is_dir()]: raise ValueError(f'@dir {missing[0]} is not an existing directory')
        for key in ('readonly', 'net', 'nosandbox'):
            if body.get(key) and not a.record.get(key):
                # A sandbox change needs a new process (grok) or a reattached thread (codex).
                a.record[key] = True
                a.restart = True
        # Per-agent overrides of the guards and of the lean vendor setup; any later /open may change them.
        for key in ('memory_gb', 'hang_s', 'wall_s'):
            if body.get(key) is not None: a.record[key] = body[key]
        if body.get('nice') is not None and body['nice'] != a.record.get('nice', 0):
            # Niceness is set when the vendor starts, so a change needs a new process; the session resumes in it.
            a.record['nice'] = body['nice']
            a.restart = True
        if isinstance(body.get('session'), str) and body['session']: a.record['session'] = body['session']
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
        # A result held for this agent is handed over by /turn without the vendor, and a vendor that failed to start here
        # would strand it; the next turn's start attaches the vendor instead.
        if not a.undelivered(): await self.attach(a)
        # A model change applies at the next turn; refusing it here keeps the mod from adopting it. With a result held,
        # the turn's start checks it instead, after that result is handed over.
        if a.family == 'gemini' and body.get('model') and not a.undelivered(): await self.agy_efforts(body['model'])
        if body.get('model') and not a.undelivered() and (models := self.known_models(a)) and body['model'] not in models:
            raise ValueError(f'unknown {a.family} model {body["model"]}; {a.family} lists {", ".join(models)}')
        if a.state == 'closed': a.state = 'idle'
        a.default_model = False
        # An opened agent counts as used, so pruning cannot take it before its turn starts.
        self.save(a)
        settings = self.settings(a)
        return {'vendorSession': a.vendorSession, **{k: v for k, v in settings.items() if k != 'codex_folder'}, 'line': settings_line(settings)}

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

    def quota_spent(self, family, model):
        self.load_quota()
        quota = self.quota.get((family, model))
        return quota if quota and quota[0] > time.time() else None

    def quota_refusal(self, family, model, a=None, agent_id=None):
        # Refused at /open, before a vendor process starts, and again at a turn's start; the refusal is in the agent's log.
        quota = self.quota_spent(family, model)
        if not quota: return
        until = datetime.fromtimestamp(quota[0]).astimezone().strftime('%Y-%m-%d %H:%M %Z')
        message = f'{family} quota reached ({quota[1]}); Lord refuses new {model} turns until it resets, about {until}'
        # A new agent refused at its first /open has no record, and so no log that pruning would remove: lordd.log has it.
        if a: a.note('refused: ' + message)
        elif agent_id: self.log(f'{agent_id}: refused: {message}')
        raise ValueError(message)

    async def begin_starting(self, a, body):
        self.quota_refusal(a.family, body.get('model') or a.model, a)
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
        a.dispatched, a.agy_step, a.rows = False, None, {}
        a.commands, a.retries, a.edited, a.last_command, a.turn_error, a.turn_body, a.retry_waiting = 0, 0, False, None, None, body, False
        a.turn_started = datetime.now(timezone.utc).isoformat(timespec='seconds')
        a.settings = self.settings(a)
        a.note(f'turn: {a.family} {a.model} effort {a.effort} · {settings_line(a.settings)}')
        self.save(a)
        a.turn_task = self.task(self.run_turn(a, body))

    def settings(self, a):
        # What the agent runs with this turn, the defaults included: the limits a guard stops it at, and its sandbox.
        r, family = a.record, a.family
        ceiling = r['memory_gb'] * 1048576 if r.get('memory_gb') else self.config.get('memory_ceiling_kb', 12 * 1024 * 1024)
        hang = r['hang_s'] if r.get('hang_s') is not None else self.config[family].get('no_progress_s', 0)
        folder = (Path(r['codexHome']) if r.get('codexHome') else self.user_codex_home) if family == 'gpt' else \
            grok_home() if family == 'grok' else self.vendor_home / '.gemini'
        return {'model': a.model, 'effort': a.effort, 'cwd': a.cwd, 'dirs': r.get('dirs', []),
                'sandbox': 'read-only' if r.get('readonly') else 'none' if r.get('nosandbox') or family == 'gemini' else 'workspace-write',
                'network': bool(r.get('net')) if family == 'gpt' else True, 'tools': bool(r.get('tools')),
                'nice': max(r.get('nice', 0), os.getpriority(os.PRIO_PROCESS, 0)),
                'memory_gb': round(ceiling / 1048576, 1), 'hang_s': hang or None, 'wall_s': r.get('wall_s') or None,
                **({'codexHome': str(folder)} if family == 'gpt' else {}), 'codex_folder': str(folder)}

    async def retry(self, a, delay):
        # A turn that met "at capacity" before changing anything, sent again on the same session after a wait. The
        # session already holds the task, so the retry asks it to go on rather than sending the task twice.
        owner = a.turn_body
        try: await asyncio.sleep(delay)
        finally: a.retry_waiting = False
        # Only the turn that met "at capacity": a later turn started meanwhile is not continued.
        if a.cancel_requested or a.state not in ('running', 'waiting_tool') or a.turn_body is not owner: return
        self.backend(a).last = time.monotonic()
        await self.run_turn(a, {**a.turn_body, 'text': 'Continue with the task above: the last attempt stopped because the model was at capacity.'}, retry=True)

    def allowance(self, snapshot):
        # codex's account limits as it last reported them (it sends them unasked during turns), merged as codex says:
        # an update carries only what changed. Kept in allowance.json, one file every daemon replaces.
        # Each quota bucket (limitId) is kept apart: a model metered under another limit reports its own windows.
        # Under the journal lock, so daemons of other versions neither lose each other's buckets nor leave scratch files
        # (one scratch name, replaced by the next write).
        if not isinstance(snapshot, dict): return
        try:
            with self.journal_lock():
                buckets = allowance_buckets()
                key = str(snapshot.get('limitId') or 'codex')
                buckets[key] = {**buckets.get(key, {}), **{k: v for k, v in snapshot.items() if v is not None}, 'seen': datetime.now(timezone.utc).isoformat(timespec='seconds')}
                scratch = HOME / 'allowance.json.tmp'
                scratch.write_text(dumps({'gpt': buckets}), encoding='utf-8')
                scratch.replace(HOME / 'allowance.json')
        except OSError as error: self.log(f'allowance.json not written: {error}')

    def gpt_quota(self, a, message):
        # A spent codex allowance is refused until the window that ran out resets, as codex last reported it.
        # The bucket that meters this model: the one named after it (a limit such as gpt-reserve), else codex's own.
        # normalModelSlug is only the model lending an alias quota its display name.
        buckets = allowance_buckets()
        own = [b for k, b in buckets.items() if a.model in (k, b.get('limitName'))] or [b for k, b in buckets.items() if k == 'codex']
        resets = [w.get('resetsAt') for bucket in own for w in (bucket.get('primary'), bucket.get('secondary'))
                  if isinstance(w, dict) and isinstance(w.get('resetsAt'), (int, float)) and (w.get('usedPercent') or 0) >= 100]
        if not resets: return a.note('usage limit reached; codex named no reset time, so new turns are not refused')
        reset = max(resets)
        self.save_quota(('gpt', a.model), (reset / 1000 if reset > 1e12 else reset, message))

    async def run_turn(self, a, body, retry=False):
        v = self.backend(a)
        try:
            if a.cancel_requested:
                a.emit('error', message='interrupted', retryable=False)
                # With no client attached, an interrupted turn leaves nothing to deliver.
                if not a.stream_lock.locked():
                    while not a.events.empty(): a.events.get_nowait()
                return
            a.dispatched, v.last = True, time.monotonic()
            # A retry continues the same turn, whose wall-clock limit runs from its first dispatch.
            if not retry: a.started = v.last
            if a.family == 'gpt':
                params = {'threadId': a.vendorSession, 'input': [{'type': 'text', 'text': body['text']}],
                          'model': a.model, 'effort': a.effort,
                          'sandboxPolicy': {'type': 'readOnly', 'networkAccess': bool(a.record.get('net'))} if a.record.get('readonly')
                                           else {'type': 'dangerFullAccess'} if a.record.get('nosandbox')
                                           else {'type': 'workspaceWrite', 'networkAccess': bool(a.record.get('net')), 'writableRoots': a.record.get('dirs', [])}}
                if body.get('schema'): params['outputSchema'] = body['schema']
                result = await v.rpc('turn/start', params)
                # The response can arrive after codex already reported this turn ended (an "at capacity" failure read
                # from the same batch), which a retry has replaced.
                if result['turn']['id'] not in a.ended_turns: a.turn_id = result['turn']['id']
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

    @property
    def user_codex_home(self):
        return Path(os.environ['CODEX_HOME']) if os.environ.get('CODEX_HOME') else self.vendor_home / '.codex'

    async def interrupt(self, a):
        a.followup = a.earlier = None
        # A turn still starting (begin's awaits) is interrupted once it runs, through the path below; a start-up
        # longer than the wait ends as interrupted when it finishes.
        if getattr(a, 'starting', False): a.cancel_requested = True
        deadline = time.monotonic() + 5
        while getattr(a, 'starting', False) and time.monotonic() < deadline: await asyncio.sleep(.02)
        if a.state in ('running', 'waiting_tool'):
            v = self.backend(a)
            a.cancel_requested = True
            for future in list(a.calls.values()):
                if not future.done(): future.set_result({'content': [{'type': 'text', 'text': 'interrupted'}], 'isError': True})
            started = a.family == 'gpt' and a.turn_id
            if a.retry_task and getattr(a, 'retry_waiting', False):
                # Waiting to retry after "at capacity": no vendor turn is running. Once the retry is being sent, it is
                # stopped as any turn is (run_turn interrupts it when codex names the turn).
                a.retry_task.cancel()
                a.emit('error', message='interrupted', retryable=False)
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
            if started:
                # codex ends an interrupted turn but leaves the command it was running alive; closing the process
                # stops that command, and the next turn resumes the thread in a new one.
                await v.close()
                a.generation = 0
        # An interrupted turn leaves nothing to deliver, also when it finished just before the interrupt arrived.
        if not a.stream_lock.locked():
            a.carry.clear()
            while not a.events.empty(): a.events.get_nowait()

    def listed(self):
        self.refresh()
        now = time.monotonic()
        def quiet(a):
            # Seconds since the vendor last reported anything, while its process runs.
            v = self.vendors.get(a.agentId)
            return round(now - v.last) if v and v.process and v.process.returncode is None and a.state in ('running', 'waiting_tool') else None
        return [{**{k: getattr(a, k) for k in ('agentId', 'family', 'model', 'effort', 'state', 'activity', 'cwd')},
                 'log': str(a.log_path), 'answer': str(a.answer_path) if a.answer_path.exists() else None,
                 'usage': a.record.get('usage'), 'quiet_s': quiet(a)} for a in self.agents.values()]

    def allowances(self):
        # Each vendor's allowance as last reported: codex's windows, and the quota resets Lord refuses turns until.
        self.load_quota()
        return {'gpt': allowance_buckets(), 'refused': [{'family': f, 'model': m, 'until': datetime.fromtimestamp(q[0], timezone.utc).isoformat(timespec='seconds'), 'reason': q[1]}
                                        for (f, m), q in self.quota.items() if q[0] > time.time()]}

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
            if path == '/allowance': return self.allowances()
        elif method == 'POST':
            if path == '/open':
                # A refused launch leaves no agent log, so its reason is kept in lordd.log.
                try: return await self.open(body)
                except ValueError as error:
                    self.log(f"{body.get('agentId')}: open refused: {error}")
                    raise
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
            elif path == '/row':
                # Answers once the vendor reports the step finished, or the turn ends.
                future = a.rows.get(body.get('rowId'))
                if future is None: raise ValueError('No such step')
                return await asyncio.shield(future)
            elif path == '/steer':
                steered = a.family == 'gpt' and a.state in ('running', 'waiting_tool') and bool(a.turn_id)
                # A message asking for @readonly goes into the running turn only if that turn already runs read-only;
                # the agent's record may name the next turn's sandbox.
                if body.get('readonly') and (getattr(a, 'settings', None) or {}).get('sandbox') != 'read-only': steered = False
                if steered:
                    await self.backend(a).rpc('turn/steer', {'threadId': a.vendorSession, 'expectedTurnId': a.turn_id,
                        'input': [{'type': 'text', 'text': body['text']}]})
                    a.note('message injected into the running turn: ' + self.redact(body['text'])[:300])
                return {'ok': True, 'steered': steered}
            elif path == '/queue':
                # A message for a running agent whose vendor takes none mid-turn: it becomes the next turn, started as
                # soon as this one ends, and the answer delivered carries both results.
                if a.state not in ('running', 'waiting_tool'): return {'ok': True, 'queued': False}
                text = body['text'] if not a.followup else a.followup['text'] + '\n\n' + body['text']
                a.followup = {**(a.followup or {'agentId': a.agentId}), 'text': text, 'queued': True}
                a.note('message queued for the next turn: ' + self.redact(body['text'])[:300])
                return {'ok': True, 'queued': True}
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
                # A client that draws rows gets each tool step as a row event that ends its stream; any other gets the
                # step as one activity line.
                rows = bool(body.get('rows'))
                if path == '/turn':
                    if not isinstance(body['text'], str): raise ValueError('text must be a string')
                    # A turn whose client went away keeps running; its result is delivered first and this
                    # text becomes the next turn in the same stream.
                    if a.state in ('running', 'waiting_tool') or a.undelivered():
                        # A message queued while the turn ran goes first; this one follows it in the same turn.
                        a.followup = {**body, 'text': a.followup['text'] + '\n\n' + body['text'], 'queued': True} if a.followup and a.followup.get('queued') else body
                        # The earlier turn's steps and narration stay unshown: only its result and any tool call it
                        # is waiting on carry over into the new turn.
                        a.carry[:] = [e for e in a.carry if e['type'] not in ('row', 'text', 'activity')]
                        queued = []
                        while not a.events.empty(): queued.append(a.events.get_nowait())
                        for e in queued:
                            if e['type'] not in ('row', 'text', 'activity'): a.events.put_nowait(e)
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
                                result = event['text'] if event['type'] == 'done' else 'It failed: ' + event['message']
                                # Each queued message's result joins those held before it, for the one answer at the end;
                                # any answer among them keeps the delivery an answer.
                                a.earlier_done = event['type'] == 'done' or (bool(a.earlier) and a.earlier_done)
                                a.earlier = f'{a.earlier}\n\nThen:\n{result}' if a.earlier else result
                                a.earlier_queued = bool(body.get('queued'))
                                try:
                                    await self.begin_locked(a, body)
                                    continue
                                except Exception as error: event = {'type': 'error', 'message': self.redact(error), 'retryable': False}
                            if event['type'] in ('done', 'error') and a.earlier:
                                earlier = (f'Result of the task, before the message sent while it ran:\n{a.earlier}\n\n' if getattr(a, 'earlier_queued', False)
                                           else f'Result of the earlier task, which finished while Claude Code was disconnected:\n{a.earlier}\n\n')
                                # An earlier answer stays an answer, whole, when only the new message failed.
                                event = ({**event, 'text': f'{earlier}Reply to the new message:\n{event["text"]}'} if event['type'] == 'done'
                                         else {'type': 'done', 'text': f'{earlier}The new message failed: {event["message"]}', 'model': a.model, 'failed': True, 'record': event.get('record')} if a.earlier_done
                                         else {**event, 'message': f'{earlier}The new message failed: {event["message"]}'})
                                a.earlier = None
                            if event['type'] == 'row' and not rows: event = {'type': 'activity', 'text': event['text']}
                            line = (dumps(event) + '\n').encode()
                            try:
                                writer.write(f'{len(line):x}\r\n'.encode() + line + b'\r\n')
                                await writer.drain()
                            except (BrokenPipeError, ConnectionResetError):
                                a.carry.insert(0, event)
                                raise
                            if event['type'] in ('tool', 'row', 'done', 'error'): break
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
            idle_s = min(self.config['idle_exit_s'], self.config.get('retire_idle_s', 30)) if self.retire else self.config['idle_exit_s']
            if not self.requests and now - self.last_request >= idle_s and not busy:
                self.stop_event.set()
                return

    async def guard(self, now):
        idle_s = self.config.get('process_idle_s', self.config['idle_exit_s'])
        for service in [v for v in self.vendors.values() if v.agent is None]:
            if service.process and not service.pending and now - service.last >= idle_s: await service.close()
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
            v.descendants = set(pids) - {v.process.pid}
            ceiling = a.record['memory_gb'] * 1048576 if a.record.get('memory_gb') else self.config.get('memory_ceiling_kb', 12 * 1024 * 1024)
            used = sum(rss.get(pid, 0) for pid in pids)
            silent = a.record['hang_s'] if a.record.get('hang_s') is not None else self.config[a.family].get('no_progress_s', 0)
            if used > ceiling: v.reason = f'stopped: the {a.family} process tree used {used / 1048576:.1f} GB, over the {ceiling / 1048576:.1f} GB memory ceiling'
            elif silent and a.state == 'running' and now - v.last > silent and not getattr(a, 'retry_waiting', False): v.reason = f'stopped: {a.family} printed nothing for {silent} s while working'
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
                # Its codex cannot start to archive the thread, so the record stays for a later prune.
                if a.record.get('codexHome') and codex_home_unshareable(self.user_codex_home): continue
                previous, a.state = a.state, 'closed'
                try:
                    backend = self.vendors.pop(aid, None)
                    a.generation = 0
                    if backend: await backend.close()
                    # Vendor history is kept (it is review and build evidence); codex's archive only moves it aside.
                    if a.family == 'gpt':
                        v = self.service(a.record.get('codexHome'))
                        try:
                            await v.ensure()
                            await v.rpc('thread/archive', {'threadId': sid})
                        except Exception as error: self.log(f'codex thread {sid} not archived: {error}')
                except BaseException:
                    a.state = previous
                    raise
                removed.append(aid)
                for path in a.files(): path.unlink(missing_ok=True)
                shutil.rmtree(a.answers_path, ignore_errors=True)
                del self.agents[aid]
                self.open_locks.pop(aid, None)
        self.pruned.update(removed)
        self.compact(removed)
        self.sweep(days)
        return {'ok': True, 'pruned': removed}

    def sweep(self, days):
        # Files of an agent no journal holds, older than the prune period: a daemon of an earlier version pruned the
        # agent and removed only the files it knew.
        known = set(self.agents) | set(self.records(self.legacy_journal, self.journal))
        cutoff = time.time() - days * 86400
        try: paths = list((HOME / 'logs').iterdir())
        except OSError: return
        for path in paths:
            try:
                if path.name.split('.', 1)[0] in known or path.lstat().st_mtime >= cutoff: continue
                shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()
            except OSError as error: self.log(f'{path} not removed: {error}')

    def compact(self, removed=()):
        # The journal rewritten with one line per agent: the newest record of each, from the file (which daemons of
        # other versions append to) or from this daemon's own saves, less the agents this daemon removed.
        with self.journal_lock():
            found = self.records(self.journal)
            for aid in self.touched:
                a = self.agents.get(aid)
                if a and a.record.get('used', '') >= found.get(aid, {}).get('used', ''): found[aid] = a.record
            for aid in removed: found.pop(aid, None)
            temporary = self.journal.with_suffix('.tmp')
            temporary.write_text(''.join(dumps(record) + '\n' for record in found.values()))
            temporary.replace(self.journal)

    async def close(self):
        self.stopping = True
        for a in self.agents.values(): a.emit('error', message='Daemon shutting down', retryable=True)
        await asyncio.gather(*(v.close() for v in self.vendors.values()), return_exceptions=True)
        if self.agy_registered and not await others_answering():
            # The entry names this plugin copy's file, which an update or an uninstall later deletes; while no daemon
            # runs, no agy session (the user's own included) is left trying to start it. A daemon of another version
            # still running keeps it, and re-adds its own at its next agy start.
            code, out = await run_quick([*self.agy_registered, 'mcp', 'remove', 'lord'])
            if code != 0: self.log(f'agy mcp remove failed: {out}')
        tasks = list(self.tasks)
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for writer in list(self.connections): writer.close()

    async def serve(self):
        lock = SOCKET.with_suffix('.lock').open('a')
        # A daemon that is shutting down holds the lock for a moment after it stops answering; this one waits for it
        # and takes over, so a turn that starts during that moment still finds a daemon.
        deadline = time.monotonic() + 10
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline or await answering():
                    lock.close()
                    return
                await asyncio.sleep(.1)
        server, bound = None, False
        try:
            if await answering(): return
            # Every request but /health and /mcp (which carries a per-agent token) needs this key. Vendor sandboxes
            # cannot read the file (grok's profiles deny it; codex's sandbox cannot reach the socket without @net).
            # Daemons of different versions share it: a new key would lock out the plugins talking to a daemon still
            # running, and grok's profiles name this one file. It is replaced only when no other daemon runs.
            try: self.control = re.fullmatch(r'X-Lord-Control: ([0-9a-f]{32})\n', (HOME / 'control.header').read_text())[1] if await others_answering() else None
            except (OSError, TypeError): self.control = None
            if not self.control:
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

async def answering(path=None):
    # Whether a daemon is serving on the socket.
    try: _, writer = await asyncio.open_unix_connection(str(path or SOCKET))
    except (FileNotFoundError, ConnectionRefusedError): return False
    writer.close()
    await writer.wait_closed()
    return True

async def others_answering():
    # Whether a daemon of another plugin version is serving from this folder.
    for path in HOME.glob('lordd*.sock'):
        if path != SOCKET and await answering(path): return True
    return False

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
    global SOCKET
    # A vendor started by a daemon from before 2.0.18 carries no socket path; that daemon is on the old socket.
    if not os.environ.get('LORD_DAEMON_SOCKET'): SOCKET = LEGACY_SOCKET
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
    # A neutral working folder: never the agent's tree, which a worktree's removal must find unused.
    HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chdir(HOME)
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
                lock = SOCKET.with_suffix('.lock').open('a')
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try: result = await d.prune(args.days if args.days is not None else d.config['prune_days'])
                finally:
                    await d.close()
                    lock.close()
            print(dumps(result))
        else: await d.serve()
    asyncio.run(run())

if __name__ == '__main__': main()

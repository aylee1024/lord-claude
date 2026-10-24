import type { EngineInterface, Register, SessionMessage, TurnStepInput, TurnStepResult } from 'claude-code'
import type { LordAgent as Agent, LordTask as Task } from '../types'

// shown: the text of the latest text block, which the engine takes as the agent's result unless another follows.
// failed: the turn's new message failed though an earlier answer was delivered; model: the model the daemon ran.
// record: the daemon's account of the finished turn, for the answer's footer.
type Stream = { next: number; live: number; answer: string; shown: string; failed?: boolean; model?: string; record?: TurnRecord }
type TurnRecord = { ended_how: string; model: string; effort: string; started: string; ended: string; commands: number; retries: number; helpers: string[]; session?: string }
type Outcome = { tool?: { callId: string; name: string; input: Record<string, unknown> }; row?: { rowId: string; shell: boolean; title: string } }
// A vendor's tool steps are drawn as rows: a shell command as a Bash row, any other step under the plugin's own tool for
// that vendor, which Claude Code labels "lord - <family>". Neither runs anything; the plugin answers each with the
// step's output as the vendor reported it.
const stepTool = (family: string) => `mcp__lord__${family}`
const stepTools = ['gpt', 'grok', 'gemini'].map(stepTool)
const agents = new Map<string, Agent>()
const starts = new Map<string, Task>()
const directories = new Map<string, string>()
const pending = new Map<string, string>()
const others = new Set<string>()
const isolated = new Set<string>()
const callEfforts = new Map<string, string>()
// Quitting aborts the session's agent turns; the daemon keeps those turns running for the next session.
let ending = false
let mainTurns = 0
// A reload of this code (an update) starts the module variables over, even mid-turn; the session's state keeps
// what the next step of a turn in flight needs: each Lord agent's record, each agent turn's task, each start folder.
const agentState = { plugin: 'lord', key: 'agent' } as const
const startState = { plugin: 'lord', key: 'start' } as const
const dirState = { plugin: 'lord', key: 'dir' } as const

// The version of each record this copy of the code last wrote or read: a newer one was written by the other copy (the
// one before a reload, finishing its step) and replaces this copy's.
const versions = new Map<string, number>()

async function keep($: EngineInterface, agentId: string): Promise<void> {
  const agent = agents.get(agentId)
  if (agent) versions.set(agentId, (await $.state.set({ ...agentState, id: agentId }, agent)).version)
}

function task(text: string): Task {
  const coordinator = /^The coordinator sent a message while you were working:\r?\n([\s\S]*)\r?\n\r?\nAddress this before completing your current task\.$/.exec(text)
  if (coordinator) text = coordinator[1]
  if (text.startsWith('[Workflow harness — computed task]')) {
    const marker = 'The computed task text follows:'
    const end = text.indexOf(marker)
    if (end >= 0) text = text.slice(end + marker.length).replace(/^\r?\n/, '').replace(/^\r?\n/, '').replace(/^  /gm, '')
  }
  text = text.replace(/<task-notification>[\s\S]*?<\/task-notification>/g, block => {
    const field = (name: string) => (block.match(new RegExp(`<${name}>([\\s\\S]*?)<\\/${name}>`))?.[1] ?? '').trim()
    const line = (name: string) => field(name).replace(/\s+/g, ' ')
    return `Agent ${line('task-id')} finished (${line('status')}): ${field('result') || line('summary')}`
  })
  const result: Task = { text: '' }
  const lines = text.trim().split(/\r?\n/)
  const seen = new Set<string>()
  while (lines.length) {
    const line = lines[0].trim()
    // An option line is @word with at most one value, or a known option with any value; a longer line starting
    // with @ (a mention) is task text.
    const known = /^@([a-z]+)(?:\s|$)/.exec(line)?.[1]
    const word = known && KNOWN.has(known) ? known : /^@([a-z]+)(?:\s+\S+)?\s*$/.exec(line)?.[1]
    if (word && word !== 'dir' && seen.has(word)) { result.error = `option line repeated: ${line} (only @dir may be given more than once)`; break }
    const model = /^@model\s+(\S+)\s*$/.exec(line)
    if (model) result.model = model[1]
    else if (/^@effort\s+(minimal|low|medium|high|xhigh|max|ultra)\s*$/.test(line)) result.effort = line.split(/\s+/)[1]
    else if (line === '@readonly') result.readonly = true
    else if (line === '@net') result.net = true
    else if (line === '@nosandbox') result.nosandbox = true
    else if (line === '@tests') result.tests = true
    else if (/^@memory\s+\d+(\.\d+)?\s*$/.test(line)) result.limits = { ...result.limits, memory_gb: Number(line.split(/\s+/)[1]) }
    else if (/^@hang\s+\d+\s*$/.test(line)) result.limits = { ...result.limits, hang_s: Number(line.split(/\s+/)[1]) }
    else if (/^@wall\s+\d+\s*$/.test(line)) result.limits = { ...result.limits, wall_s: Number(line.split(/\s+/)[1]) }
    else if (line === '@tools') result.limits = { ...result.limits, tools: true }
    else if (/^@nice\s+\d+\s*$/.test(line)) result.limits = { ...result.limits, nice: Number(line.split(/\s+/)[1]) }
    else if (/^@cwd\s+\S/.test(line)) result.cwd = line.replace(/^@cwd\s+/, '')
    else if (/^@dir\s+\S/.test(line)) result.dirs = [...(result.dirs ?? []), line.replace(/^@dir\s+/, '')]
    // A known option written wrongly would end the options here and leave the rest (an @readonly, say) as task text.
    else if (/^@(model|effort|readonly|net|nosandbox|tests|memory|hang|wall|tools|cwd|dir|nice)\b/.test(line)) { result.error = `option line not understood: ${line}`; break }
    // A misspelt option would otherwise run the task at a default (@efort max at high effort).
    else if (word) { result.error = `unknown option line: ${line} (options: ${OPTIONS})`; break }
    else break
    if (word) seen.add(word)
    lines.shift()
  }
  result.text = lines.join('\n')
  return result
}

const OPTIONS = '@model, @effort, @cwd, @dir, @readonly, @net, @nosandbox, @tests, @tools, @memory, @hang, @wall, @nice'
const KNOWN = new Set(OPTIONS.split(', ').map(option => option.slice(1)))

function testsError(family: string, options: Partial<Task>): string | undefined {
  if (!options.tests) return undefined
  if (family !== 'gpt') return '@tests is available for gpt only'
  for (const clash of ['readonly', 'nosandbox', 'net'] as const)
    if (options[clash]) return `@tests cannot be used with @${clash}`
  if (options.limits?.tools) return '@tests cannot be used with @tools'
  return undefined
}

function merge(base: Task, more: Partial<Task>): Task {
  return { ...base, ...more, dirs: more.dirs ? [...new Set([...(base.dirs ?? []), ...more.dirs])] : base.dirs,
    limits: more.limits ? { ...base.limits, ...more.limits } : base.limits }
}

// One line naming how the turn ended and what ran, as the daemon recorded it (logs/<agentId>.json holds the rest). An
// answer that is one JSON value gets none, so a caller parsing the whole answer still can.
function signed(text: string, record: TurnRecord | undefined): string {
  if (!record || typeof record !== 'object') return text
  try { JSON.parse(text.trim()); return text } catch {}
  const time = (iso: unknown) => typeof iso === 'string' ? iso.slice(11, 19) + 'Z' : '?'
  const helpers = record.helpers?.length ? ` · ${record.helpers.length} helper${record.helpers.length > 1 ? 's' : ''} (${[...new Set(record.helpers)].join(', ')})` : ''
  return `${text}\n\n[lord: ${record.ended_how} · ${record.model} ${record.effort} · ${time(record.started)} to ${time(record.ended)} · ${record.commands} command${record.commands === 1 ? '' : 's'}${helpers}${record.retries ? ` · ${record.retries} retr${record.retries > 1 ? 'ies' : 'y'} at capacity` : ''}${record.session ? ` · session ${record.session}` : ''}]`
}

function errorLine(error: unknown): string {
  return 'lord: error: ' + String(error instanceof Error ? error.message : error).replace(/\s+/g, ' ').trim()
}

const HANDBACK = 'SubagentHandback'

// In auto permission mode Claude Code delivers a subagent's report only through its SubagentHandback tool: a reminder
// says so, plain text at the end is withheld, and each turn end without the call adds a "[handback-send-enforce]"
// reminder. A run that loses the tool gets a reminder withdrawing it. As in the engine's own check, the newest of these
// decides. Reminders are left out of the transcript's rows (the API form holds them) and a compaction may drop them, so
// the last answer found is kept in the agent's record and stands when none is left.
async function handbackActive($: EngineInterface, agentId: string, agent: Agent): Promise<boolean> {
  // A run whose delivery the engine refused once gets plain text from then on, never the call again.
  if (agent.refused) return false
  const messages = await $.session.messages({ agentId, as: 'api' }).catch(() => undefined)
  for (const message of Array.isArray(messages) ? [...messages].reverse() : []) {
    if (message.role !== 'user') continue
    const texts = (typeof message.content === 'string' ? [{ type: 'text', text: message.content }] : message.content)
      .filter((block: any) => block?.type === 'text').map((block: any) => String(block.text).trimStart())
    if (texts.some(text => text.startsWith(`<system-reminder>\n${HANDBACK} is not available in this run`))) { agent.auto = undefined; return false }
    if (texts.some(text => text.startsWith(`<system-reminder>\nYour final report is delivered through ${HANDBACK}`) || text.startsWith('[handback-send-enforce]'))) {
      agent.auto = true
      return true
    }
  }
  return !!agent.auto
}

// Claude Code compacts an agent's transcript as it nears the window it assumes for the agent's model, 180,000 tokens
// for a vendor model it does not know, and fails the agent ("Prompt is too long") when compaction is refused. A Lord
// agent's vendor keeps its own context and Lord answers every step, so the compaction is answered here, without a
// model: the task and the newest steps up to KEEP_CHARS stay, older steps go (the activity log names every step, and the
// vendor's own session keeps each step whole).
const KEEP_CHARS = 150_000

function size(message: SessionMessage): number {
  return message.text.length + JSON.stringify(message.toolUses).length + JSON.stringify(message.toolResults ?? []).length
}

// A message past a quarter of KEEP_CHARS (a long final answer, a child agent's result, a large tool input) is kept as a
// copy holding the end of each text, at a size halved until the copy fits; a message whose texts are too many to fit
// even then keeps none of them. A copy has no handle: the engine builds it from its role, text and tool blocks.
function shortened(message: SessionMessage): SessionMessage {
  if (size(message) <= KEEP_CHARS / 4) return message
  const { handle: _, ...copy } = message
  const dropped = (what: string, length: number) => `[${what} of ${length} characters dropped from this transcript; the vendor's own session keeps it]`
  for (let room = KEEP_CHARS / 8; room >= 64; room = Math.floor(room / 2)) {
    const cut = (text: string) => text.length > room ? `[${text.length - room} earlier characters dropped from this transcript; the vendor's own session keeps them]\n${text.slice(-room)}` : text
    // Every string, at any depth: a tool's input may nest its long text inside objects and lists.
    const deep = (value: unknown): unknown => typeof value === 'string' ? cut(value) : Array.isArray(value) ? value.map(deep)
      : value && typeof value === 'object' ? Object.fromEntries(Object.entries(value).map(([key, field]) => [key, deep(field)])) : value
    const result: SessionMessage = { ...copy, text: cut(message.text), toolUses: message.toolUses.map(use => ({ ...use, input: deep(use.input) as Record<string, unknown>, result: undefined, ...(use.text ? { text: cut(use.text) } : {}) })),
      ...(message.toolResults ? { toolResults: message.toolResults.map(result => ({ ...result, result: undefined, text: cut(String(result.text ?? '')) })) } : {}) }
    if (size(result) <= KEEP_CHARS / 4) return result
  }
  return { ...copy, text: message.text.length > 64 ? dropped('text', message.text.length) : message.text,
    toolUses: message.toolUses.map((use, i) => ({ ...use, input: { dropped: i ? '[dropped]' : dropped('input', JSON.stringify(use.input).length) }, result: undefined, text: undefined })),
    ...(message.toolResults ? { toolResults: message.toolResults.map(result => ({ ...result, result: undefined, text: '[dropped]' })) } : {}) }
}

function compacted(messages: readonly SessionMessage[]): SessionMessage[] | undefined {
  if (messages.length < 2) return undefined
  const kept = messages.map(shortened)
  let start = kept.length, total = 0
  while (start > 1 && total + size(kept[start - 1]!) <= KEEP_CHARS) total += size(kept[--start]!)
  // The step in flight stays, and the kept steps open on an assistant message, so no result loses its call.
  start = Math.max(1, Math.min(start, kept.length - 2))
  while (start > 1 && kept[start]!.role !== 'assistant') start--
  const result = [kept[0]!, ...kept.slice(start)]
  return start > 1 || kept.some((message, i) => message !== messages[i]) ? result : undefined
}

function* deliver(e: TurnStepInput, index: number, message: string): Generator<any, TurnStepResult> {
  // The engine runs the call itself; it hands the report to the caller and ends the agent's run.
  yield { kind: 'tool', index, id: `lord_handback_${e.turnId}_${e.index}`, name: HANDBACK }
  yield { kind: 'input', index, json: JSON.stringify({ message }) }
  yield { kind: 'stop', stopReason: 'tool_use', usage: null }
  return { turnId: e.turnId, index: e.index, answer: '', toolUses: [{ name: HANDBACK, input: { message } }], stopReason: 'tool_use', usage: null }
}

function result(e: TurnStepInput, answer: string): TurnStepResult {
  // Usage stays in the daemon (/lord): the engine reads a step's usage as this agent's context size and would
  // auto-compact the transcript with a Claude model, while the vendor manages its own context.
  return { turnId: e.turnId, index: e.index, answer, toolUses: [], stopReason: 'end_turn', usage: null }
}

async function defaultSocket($: EngineInterface): Promise<string> {
  // Each plugin version has its own daemon, so an update never waits for agents an older daemon is running.
  return `${await $.env.get('HOME')}/.claude/lord/lordd-${await version($) ?? '0'}.sock`
}

async function socket($: EngineInterface): Promise<string> {
  return await $.env.get('LORD_DAEMON_SOCKET') || await defaultSocket($)
}

async function daemonOf($: EngineInterface, agent: LordAgent): Promise<string> {
  // The daemon running the agent's turn; a turn opened before 2.0.18 ran on the one socket every version shared.
  return agent.daemon ?? (await $.env.get('LORD_DAEMON_SOCKET') || `${await $.env.get('HOME')}/.claude/lord/lordd.sock`)
}

async function keyFile($: EngineInterface): Promise<string> {
  // The daemon writes its control key next to its socket; vendor sandboxes cannot read it.
  return (await socket($)).replace(/[^/]*$/, 'control.header')
}

let pluginVersion: Promise<string | undefined> | undefined
function version($: EngineInterface): Promise<string | undefined> {
  // Names this version's daemon socket, and is sent with /open: a daemon older than a plugin on its socket exits once idle.
  pluginVersion ??= $.fs.read(`${$.plugin.root}/.claude-plugin/plugin.json`).then(text => JSON.parse(String(text)).version, () => undefined)
  return pluginVersion
}

function newer(version: string, than: string): boolean {
  // Dotted numbers only; anything else is never newer.
  const a = version.split('.').map(Number), b = than.split('.').map(Number)
  if ([...a, ...b].some(Number.isNaN)) return false
  for (let i = 0; i < Math.max(a.length, b.length); i++) if ((a[i] ?? 0) !== (b[i] ?? 0)) return (a[i] ?? 0) > (b[i] ?? 0)
  return false
}

const RANKS = ['minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']
// The highest level each vendor runs, as the agent descriptions state; a higher request runs at this one.
const CEILINGS: Record<string, string> = { gpt: 'ultra', grok: 'xhigh', gemini: 'high' }

function capped(family: string, effort: string): string {
  const level = RANKS.indexOf(effort), ceiling = RANKS.indexOf(CEILINGS[family] ?? 'ultra')
  return level < 0 ? effort : RANKS[Math.min(level, ceiling)]!
}

function shortName(model: string): string {
  // gpt-6-astra -> Astra, gpt-6.1-sol -> Sol 6.1, gemini-4-argon -> Argon, grok-4.7 -> Grok 4.7.
  const capital = (word: string) => word[0]!.toUpperCase() + word.slice(1)
  const named = /^[a-z]+-(\d+(?:\.\d+)*)-([a-z][a-z0-9]*)$/i.exec(model)
  if (named) return capital(named[2]!) + (named[1]!.includes('.') ? ` ${named[1]}` : '')
  const bare = /^([a-z]+)-(\d+(?:\.\d+)*)$/i.exec(model)
  return bare ? `${capital(bare[1]!)} ${bare[2]}` : model
}

async function defaultModel($: EngineInterface, family: string): Promise<string | undefined> {
  // The daemon's own rule: the bundled config.json, with ~/.claude/lord/config.json over it one family at a time.
  const read = (path: string) => $.fs.read(path).then(text => JSON.parse(String(text))?.[family]?.model, () => undefined)
  return await read(`${await $.env.get('HOME')}/.claude/lord/config.json`) ?? await read(`${$.plugin.root}/config.json`)
}

async function working($: EngineInterface): Promise<boolean> {
  // A reload is queued only while no turn runs, the main loop's or any agent's, Lord's or Claude's.
  if (mainTurns || [...agents.values()].some(agent => agent.running)) return true
  const roster = await $.agent.list().catch(() => undefined)
  return !roster || roster.some(agent => agent.status === 'pending' || agent.status === 'running')
}

let reloading: string | undefined
async function reloadIfUpdated($: EngineInterface): Promise<void> {
  // Claude Code keeps the plugin version a session started with; a newer installed copy loads through /reload-plugins.
  const loaded = await version($)
  const base = $.plugin.root.replace(/\/[^/]+$/, '')
  const installed = await $.fs.read(`${await $.env.get('HOME')}/.claude/plugins/installed_plugins.json`).then(text =>
    Object.values(JSON.parse(String(text)).plugins ?? {}).flat().find((entry: any) => String(entry?.installPath ?? '').replace(/\/[^/]+$/, '') === base) as any, () => undefined)
  if (!installed?.version || !loaded || !newer(String(installed.version), loaded) || reloading === installed.version) return
  if (await working($)) return
  reloading = String(installed.version)
  // Run from inside a hook the turn waits on, the command would be refused; a second later the turn has ended. A turn
  // started meanwhile (a queued message) leaves it for the next check, since a queued command would wait for that
  // turn and could follow agents it starts.
  void $.clock.sleep(1000).then(async () => {
    if (await working($)) { reloading = undefined; return }
    $.ui.toast(`Lord Claude ${reloading} installed; reloading plugins`)
    await $.command.run({ command: 'reload-plugins', args: '--force' })
  }).catch(() => $.ui.toast(`Lord Claude ${reloading} is installed: run /reload-plugins --force to load it`))
}

// `at` is the socket of the daemon running an agent's turn: a reload onto a newer version mid-turn must still reach it.
async function request($: EngineInterface, path: string, body?: unknown, at?: string): Promise<string> {
  const key = await $.fs.read(await keyFile($)).then(text => /^X-Lord-Control:\s*(\S+)/.exec(String(text))?.[1], () => undefined)
  const response = await $.http.fetch('http://lord' + path, {
    socketPath: at ?? await socket($), method: body === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json', ...(key ? { 'X-Lord-Control': key } : {}) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  if (!response.ok) {
    let reason = `HTTP ${response.status}`
    try { reason = JSON.parse(response.text).error ?? reason } catch {}
    throw new Error(`${path.slice(1)}: ${reason}`)
  }
  return response.text
}

function json(text: string): Record<string, unknown> | undefined {
  const fenced = /```(?:json)?\s*([\s\S]*?)```/.exec(text)?.[1]
  for (const candidate of [text.trim(), fenced?.trim(), text.slice(text.indexOf('{'), text.lastIndexOf('}') + 1)]) {
    if (!candidate) continue
    try {
      const value = JSON.parse(candidate)
      if (value && typeof value === 'object' && !Array.isArray(value)) return value
    } catch {}
  }
  return undefined
}

async function* events($: EngineInterface, path: string, body: unknown, at?: string): AsyncGenerator<any, void> {
  // The engine's HTTP API returns a complete body; curl exposes NDJSON as it arrives.
  const child = $.process.spawn({
    argv: ['/usr/bin/curl', '--disable', '--silent', '--show-error', '--no-buffer', '--fail-with-body', '--noproxy', '*',
      '--connect-timeout', '5', '--unix-socket', at ?? await socket($), '--request', 'POST',
      '--header', 'Content-Type: application/json', '--header', '@' + await keyFile($), '--data-binary', '@-', 'http://lord' + path],
    input: JSON.stringify(body),
  })
  let buffer = ''
  let stderr = ''
  try {
    while (true) {
      const piece = await child.next()
      if (piece.done) {
        if (piece.value.code !== 0) {
          try { const body = JSON.parse(buffer); if (typeof body.error === 'string') throw new Error(`${path.slice(1)}: ${body.error}`) } catch (error) {
            if (error instanceof Error && error.message.startsWith(path.slice(1))) throw error
          }
          throw new Error(`daemon ${path}: ${stderr.trim() || 'stream transport failed'}`)
        }
        break
      }
      if (piece.value.stream === 'stderr') { stderr = (stderr + piece.value.text).slice(-1000); continue }
      buffer += piece.value.text
      let newline: number
      while ((newline = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, newline)
        buffer = buffer.slice(newline + 1)
        if (line.trim()) yield JSON.parse(line)
      }
    }
    // A refused stream request answers with the daemon's {"error"} body.
    if (buffer.trim()) {
      const last = JSON.parse(buffer)
      if (!last.type && typeof last.error === 'string') throw new Error(`${path.slice(1)}: ${last.error}`)
      yield last
    }
  } finally { await child.return({ code: null, signal: null }) }
}

async function connect($: EngineInterface, signal: AbortSignal): Promise<void> {
  try { await request($, '/health'); return } catch (error) {
    // Only the default socket belongs to the daemon this plugin starts; a test socket is never auto-started.
    if (signal.aborted || await socket($) !== await defaultSocket($)) throw error
  }
  // The daemon forks and exits 0 at once. A missing interpreter, or exit 3 (older than 3.11), tries the next one;
  // any other failure (a broken ~/.claude/lord/config.json) is reported with the daemon's message.
  // Homebrew's and python.org's, then each python3 on the session PATH (pyenv, conda, uv), then the system's.
  const path = String(await $.env.get('PATH') ?? '').split(':').filter(dir => dir.startsWith('/')).map(dir => dir.replace(/\/+$/, '') + '/python3')
  const pythons = [...new Set([await $.env.get('LORD_PYTHON'), '/opt/homebrew/bin/python3', '/usr/local/bin/python3', ...path, '/usr/bin/python3'].filter(Boolean) as string[])]
  let started = false
  for (const python of pythons) {
    let code: number | null = null, stderr = ''
    try {
      const child = $.process.spawn({ argv: [python, `${$.plugin.root}/lordd.py`, '--daemon'] })
      let piece
      while (!(piece = await child.next()).done) if (piece.value.stream === 'stderr') stderr += piece.value.text
      code = piece.value.code
    } catch { continue }
    if (code === 0) { started = true; break }
    if (code !== 3) throw new Error(stderr.trim() || `the daemon exited with code ${code}`)
  }
  if (!started) throw new Error('no Python 3.11 or later found (set LORD_PYTHON to its path)')
  const deadline = await $.clock.now() + 5000
  while (true) {
    try { await request($, '/health'); return } catch (error) {
      if (signal.aborted || await $.clock.now() >= deadline) throw error
    }
    await $.clock.sleep(100, { signal })
  }
}

async function steer($: EngineInterface, agentId: string, text: string): Promise<boolean> {
  const agent = await lordAgent($, agentId)
  if (!agent?.running) return false
  const parsed = task(text)
  // Directives alter the next turn's session options rather than an in-flight request. A message asking for
  // @readonly is never put into a turn that can write: it waits for a turn that starts read-only.
  if (parsed.error || parsed.tests || testsError(agent.family, { ...agent, ...parsed }) || (parsed.readonly && !agent.readonly)) return false
  const { text: _, ...options } = parsed
  agent.pending = merge({ text: '', ...agent.pending }, options)
  await keep($, agentId)
  try {
    // The daemon knows the running turn's sandbox: a @readonly message goes in only if that turn is read-only.
    const response = JSON.parse(await request($, '/steer', { agentId, text: parsed.text, ...(parsed.readonly ? { readonly: true } : {}) }, await daemonOf($, agent)))
    return response.steered === true
  } catch { return false }
}

// A SendMessage to a running Lord agent, answered here so the sender learns when it is read: codex takes it into the
// running turn; grok, agy, and codex between turns take none mid-turn, so the daemon runs it as the next turn as soon as
// this one ends, and the answer then carries both results. Undefined: not a running Lord agent (the engine delivers).
async function message($: EngineInterface, to: string, text: string): Promise<string | undefined> {
  const agent = await lordAgent($, to)
  if (!agent?.running) return undefined
  if (await steer($, to, text)) return `Injected into ${to}'s running turn: ${agent.family} reads it at its next step.`
  const parsed = task(text)
  // A queued message starts its turn in the daemon, past the /open that applies option lines; one carrying any is
  // left to Claude Code, whose next turn of the agent applies them.
  if (parsed.error || Object.keys(parsed).some(key => key !== 'text')) return undefined
  try {
    if (JSON.parse(await request($, '/queue', { agentId: to, text: parsed.text }, await daemonOf($, agent))).queued)
      return `Queued for ${to}: ${agent.family === 'gpt' ? 'gpt has no running step to take it into yet' : `${agent.family} takes no message mid-turn`}, so it reads this as soon as its current turn ends and works on it before reporting; its answer will carry both results.`
  } catch {}
  return undefined
}

// The settings a launch asks for, defaults included, as one line the caller reads with the launch result; the daemon
// states the effective ones in the agent's log at each turn and the answer's footer names the model that ran.
async function launchLine($: EngineInterface, family: string, options: Task, effort: string): Promise<string> {
  const read = (path: string) => $.fs.read(path).then(text => JSON.parse(String(text)), () => undefined)
  const configs = [await read(`${await $.env.get('HOME')}/.claude/lord/config.json`), await read(`${$.plugin.root}/config.json`)]
  const setting = (pick: (c: any) => unknown) => configs.map(c => c && pick(c)).find(v => v !== undefined && v !== null)
  const model = options.model ?? setting(c => c[family]?.model) ?? 'default'
  const memory = options.limits?.memory_gb ?? Math.round(Number(setting(c => c.memory_ceiling_kb) ?? 12 * 1048576) / 104857.6) / 10
  const hang = options.limits?.hang_s ?? setting(c => c[family]?.no_progress_s)
  const sandbox = options.tests ? 'tests' : options.readonly ? 'read-only' : options.nosandbox || family === 'gemini' ? 'none' : 'workspace-write'
  return `lord: requested ${model} ${effort} · cwd ${options.cwd ?? 'the agent\'s start folder'} · dirs ${options.dirs?.join(', ') || 'none'} · sandbox ${sandbox}` +
    ` · network ${family !== 'gpt' || options.net || options.nosandbox ? 'on' : 'off'} · tools ${options.limits?.tools ? 'on' : 'off'} · nice ${options.limits?.nice ?? 0}` +
    ` · memory ${memory} GB · hang ${hang ? hang + ' s' : 'none'} · wall ${options.limits?.wall_s ? options.limits.wall_s + ' s' : 'none'}`
}

async function lordAgent($: EngineInterface, agentId: string): Promise<Agent | undefined> {
  // Module state starts empty after a reload; the engine's roster still names each agent's type.
  if (others.has(agentId)) return undefined
  const kept = await $.state.get({ ...agentState, id: agentId })
  if (kept.value && kept.version !== versions.get(agentId)) {
    agents.set(agentId, { ...kept.value })
    versions.set(agentId, kept.version)
  }
  if (agents.has(agentId)) return agents.get(agentId)
  const directory = (await $.state.get({ ...dirState, id: agentId })).value
  if (directory && !directories.has(agentId)) directories.set(agentId, directory)
  const listed = await $.agent.list().then(list => list.find(agent => agent.id === agentId), () => undefined)
  if (listed && !listed.type.startsWith('lord:')) { others.add(agentId); return undefined }
  let family = listed?.type.slice(5), cwd = directories.get(agentId)
  // The daemon keeps an opened agent's settings, such as an @effort level beyond the Agent call's.
  const known = await request($, '/agents').then(text => JSON.parse(text).find((a: any) => a.agentId === agentId), () => undefined)
  if (!family) {
    // An id the roster does not name may still be a lord agent the daemon opened before a reload.
    if (!known) { others.add(agentId); return undefined }
    family = known.family
  }
  agents.set(agentId, { family: family!, cwd: known?.cwd ?? cwd, opened: !!known, effort: known?.effort, model: known?.model })
  return agents.get(agentId)
}

async function* vendorTurn($: EngineInterface, path: string, body: unknown, stream: Stream, at?: string): AsyncGenerator<any, Outcome> {
  for await (const event of events($, path, body, at)) {
    if (typeof event !== 'object' || event === null) throw new Error('invalid daemon event')
    if (event.type === 'activity' && typeof event.text === 'string') {
      stream.live = -1
      yield { kind: 'thinking', index: stream.next++, text: event.text }
    } else if (event.type === 'text' && typeof event.text === 'string') {
      // Recorded as the vendor writes it. The engine keeps an agent's last text block as its result, so a final answer
      // that differs from the text shown is added as one more block at done.
      if (stream.live < 0) { stream.live = stream.next++; stream.shown = '' }
      stream.answer += event.text
      stream.shown += event.text
      yield { kind: 'text', index: stream.live, text: event.text }
    } else if (event.type === 'row') {
      if (typeof event.rowId !== 'string' || typeof event.title !== 'string') throw new Error('invalid daemon step')
      return { row: { rowId: event.rowId, shell: !!event.shell, title: event.title } }
    } else if (event.type === 'tool') {
      if (!['Agent', 'SendMessage'].includes(event.name) || typeof event.callId !== 'string' || !event.input || typeof event.input !== 'object') throw new Error('invalid daemon tool call')
      return { tool: { callId: event.callId, name: event.name, input: event.input } }
    } else if (event.type === 'done' && typeof event.text === 'string') {
      // The vendor's final answer alone; narration streamed before it is not the result.
      stream.answer = event.text
      stream.record = event.record
      stream.failed = event.failed === true
      if (typeof event.model === 'string' && event.model) stream.model = event.model
      return {}
    } else if (event.type === 'error') {
      stream.record = event.record
      throw new Error(event.message || 'daemon turn failed')
    }
    else throw new Error('invalid daemon event')
  }
  throw new Error('daemon stream ended without done or tool')
}

// codex's allowance windows as it last reported them, one line per quota bucket, and every reset Lord refuses turns until.
function allowanceLines(allowance: any): string[] {
  const lines: string[] = []
  const when = (at: unknown) => typeof at === 'number' ? new Date(at > 1e12 ? at : at * 1000).toISOString().slice(0, 16).replace('T', ' ') + 'Z' : '?'
  for (const [id, gpt] of Object.entries<any>(allowance?.gpt && typeof allowance.gpt === 'object' ? allowance.gpt : {})) {
    const windows = [gpt?.primary, gpt?.secondary].filter((w: any) => w && typeof w === 'object')
    if (windows.length) lines.push(`allowance: codex${id === 'codex' ? '' : ` (${gpt.limitName ?? id})`} ${windows.map((w: any) => `${w.windowDurationMins >= 1440 ? Math.round(w.windowDurationMins / 1440) + '-day' : Math.round(w.windowDurationMins / 60) + '-hour'} window ${w.usedPercent}% used, resets ${when(w.resetsAt)}`).join('; ')}${gpt.credits?.balance ? `; credits ${gpt.credits.balance}` : ''} (as of ${String(gpt.seen ?? '?').slice(0, 16).replace('T', ' ')}Z)`)
  }
  for (const r of allowance?.refused ?? []) lines.push(`refused until ${String(r.until).slice(0, 16).replace('T', ' ')}Z: ${r.family} ${r.model} (${r.reason})`)
  return lines
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    ending = false
    const r = await next(e)
    await $.command.register({ name: 'lord', description: 'List Lord agents or select their next model.', argumentHint: '[model <agentId> <model>]', immediate: true })
    // Behind ToolSearch, so the main conversation's prompt does not carry them; Lord's agent files list them.
    for (const family of ['gpt', 'grok', 'gemini'])
      await $.tool.register({ name: family, description: `One step a Lord ${family} agent took, as its vendor reported it. Display only: calling it does nothing.`,
        inputSchema: { type: 'object', properties: { step: { type: 'string' } }, required: ['step'] }, isDeferred: true }).catch(() => {})
    void reloadIfUpdated($).catch(() => {})
    return r
  })

  on('agent.spawn', async ($, e, next) => {
    let input = e
    // A resumed run's content is fixed by Claude Code, so only a new agent is labelled.
    if (e.subagentType.startsWith('lord:') && !e.resume) {
      // The agent list shows the description, so it leads with the vendor model and effort Lord will request; the
      // agent's model becomes the vendor's, so Claude Code's views name it rather than the stand-in's Claude model.
      const family = e.subagentType.slice(5), options = task(e.prompt)
      const model = options.model ?? await defaultModel($, family)
      const effort = callEfforts.get(e.tool_use_id) ?? capped(family, options.effort ?? 'high')
      const label = model ? `${shortName(model)}, ${effort}` : effort
      // A caller that copied a Lord label into its description gets this one in its place, not a second one.
      const description = (e.description ?? '').replace(new RegExp(`^(?:(?:[^·,]+, )?(?:${RANKS.join('|')}) · )+`), '')
      // A description that already names the model, by id or short name, is kept as written.
      const named = !!model && [model, shortName(model)].some(name => new RegExp(`(?<![a-z0-9])${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?![a-z0-9])`, 'i').test(description))
      input = { ...e, description: named ? description : description ? `${label} · ${description}` : label, ...(model ? { model } : {}) }
    }
    callEfforts.delete(e.tool_use_id)
    const r = await next(input)
    if (e.subagentType.startsWith('lord:') && r.agentId) {
      const previous = agents.get(r.agentId)
      // A resumed run arrives with the session's folder; an agent that already opened keeps its own.
      agents.set(r.agentId, { ...previous, family: e.subagentType.slice(5), cwd: previous?.opened ? previous.cwd : directories.get(r.agentId) ?? e.cwd ?? previous?.cwd,
        worktree: previous?.worktree || isolated.has(e.tool_use_id) })
      isolated.delete(e.tool_use_id)
      await keep($, r.agentId)
    }
    return r
  })

  on('classic.SubagentStart', async ($, e, next) => {
    // Only the start folder is recorded: an opened agent's folder is the daemon's, which lordAgent restores.
    if (e.agent_type.startsWith('lord:')) {
      directories.set(e.agent_id, e.cwd)
      await $.state.set({ ...dirState, id: e.agent_id }, e.cwd)
    }
    return next(e)
  })

  on('turn.start', async ($, e, next) => {
    // A new agent's start can precede the spawn result that identifies its type.
    if (e.agentId) {
      // A run woken only by a background task's notification, as Claude Code words it.
      const notice = /^\[SYSTEM NOTIFICATION[^\]]*\]/.test(e.text) && e.text.includes('<task-notification>')
      starts.set(e.turnId, { ...task(e.text), ...(notice ? { notice } : {}) })
      await $.state.set({ ...startState, id: e.turnId }, starts.get(e.turnId)!)
    } else mainTurns++
    return next(e)
  })

  on('turn.step', async function* ($, e, next) {
    const agent = e.agentId ? await lordAgent($, e.agentId) : undefined
    if (!agent) return yield* next(e)
    const agentId = e.agentId!
    const stream: Stream = { next: 0, live: -1, answer: '', shown: '' }
    // A refused delivery holds for its own run only; a new run may deliver again.
    if (e.index === 0) agent.refused = undefined
    try {
      if (agent.error) { const error = agent.error; delete agent.error; throw new Error(error.replace(/^lord: error: /, '')) }
      let outcome: Outcome | undefined
      agent.running = true
      await keep($, agentId)
      if (e.index === 0) {
        const started = starts.get(e.turnId) ?? (await $.state.get({ ...startState, id: e.turnId })).value ?? undefined
        if (!started) throw new Error('missing turn.start task')
        if (started.error) throw new Error(started.error)
        // Directives from this task and from messages steered into the last turn apply only once /open accepts them.
        const input = merge(agent.pending ?? { text: '' }, started)
        input.text = started.text
        delete agent.pending
        agent.notice = started.notice
        // Seen now, before a long turn's compaction can drop the reminder.
        await handbackActive($, agentId, agent)
        agent.structured = undefined
        agent.corrected = false
        await connect($, next.signal)
        // An agent restored while the daemon was down learns the daemon's settings once it is up.
        if (!agent.opened) {
          const known = await request($, '/agents').then(text => JSON.parse(text).find((a: any) => a.agentId === agentId), () => undefined)
          if (known) Object.assign(agent, { opened: true, cwd: known.cwd, effort: agent.effort ?? known.effort, model: agent.model ?? known.model })
        }
        const chosen = merge({ text: '', model: agent.model, effort: agent.effort, readonly: agent.readonly, net: agent.net, nosandbox: agent.nosandbox, tests: agent.tests, dirs: agent.dirs, limits: agent.limits }, input)
        const error = testsError(agent.family, chosen)
        if (error) throw new Error(error)
        // Only an absolute @cwd replaces the worktree; a relative one would be under the main tree.
        if (agent.worktree && !(input.cwd && /^[/~]/.test(input.cwd)) && !directories.get(agentId)) throw new Error('worktree isolation was requested but Claude Code reported no worktree path; refusing to run in the main tree')
        // A task can name the vendor's working folder (fixed at the first turn; the daemon compares folders, not
        // spellings). A relative one is under the agent's start folder, not the daemon's.
        const base = input.cwd && /^[/~]/.test(input.cwd) ? input.cwd : `${directories.get(agentId) ?? agent.cwd ?? await $.session.cwd()}${input.cwd ? '/' + input.cwd : ''}`
        if (input.cwd) input.cwd = base
        // A relative @dir is under the agent's working folder too.
        if (input.dirs) input.dirs = input.dirs.map(dir => /^[/~]/.test(dir) ? dir : `${input.cwd ?? (agent.opened ? agent.cwd : undefined) ?? base}/${dir}`)
        const cwd = input.cwd ?? (agent.opened ? agent.cwd : undefined) ?? directories.get(agentId) ?? agent.cwd ?? await $.session.cwd()
        await request($, '/open', { agentId, family: agent.family, cwd, pin_cwd: !!input.cwd, path: await $.env.get('PATH'), version: await version($), session: await $.session.id().catch(() => undefined), model: chosen.model, effort: chosen.effort ?? e.effort, readonly: chosen.readonly, net: chosen.net, nosandbox: chosen.nosandbox, tests: chosen.tests, dirs: chosen.dirs, ...chosen.limits })
        Object.assign(agent, { model: chosen.model, effort: chosen.effort, readonly: chosen.readonly, net: chosen.net, nosandbox: chosen.nosandbox, tests: chosen.tests, dirs: chosen.dirs, limits: chosen.limits })
        agent.opened = true
        agent.cwd = cwd
        agent.daemon = await socket($)
        outcome = yield* vendorTurn($, '/turn', { agentId, text: input.text, model: agent.model, effort: agent.effort ?? e.effort, rows: true }, stream)
      } else if (agent.continuing) {
        outcome = yield* vendorTurn($, '/continue', { agentId, rows: true }, stream, await daemonOf($, agent))
      } else if (agent.handback) {
        // The step after the report's delivery: nothing more to say. A refused delivery (the tool inactive) falls
        // back to the report as plain text, which is then what reaches the caller.
        const verdict = agent.handback, report = agent.report ?? ''
        delete agent.handback
        if (typeof verdict === 'object' && verdict.isError) agent.refused = true
        agent.running = false
        await keep($, agentId)
        // 'sent': the call's outcome never came back, so the report may not have reached the caller.
        if (verdict === 'done' || !report) {
          yield { kind: 'stop', stopReason: 'end_turn', usage: null }
          return result(e, '')
        }
        yield { kind: 'text', index: 0, text: report }
        yield { kind: 'stop', stopReason: 'end_turn', usage: null }
        return result(e, report)
      } else if (agent.refused) {
        // The engine refused this run's delivery and keeps asking: the report was already shown as text; end quietly.
        agent.running = false
        await keep($, agentId)
        yield { kind: 'stop', stopReason: 'end_turn', usage: null }
        return result(e, '')
      } else if (await handbackActive($, agentId, agent)) {
        // A reminder that the report was never delivered; never a Workflow schema step, which handback excludes.
        const report = agent.report ?? agent.lastAnswer ?? 'lord: error: the agent recorded no answer'
        agent.handback = 'sent'
        agent.running = false
        await keep($, agentId)
        return yield* deliver(e, 0, report)
      } else {
        // A later step with no lord tool behind it is the engine asking for the StructuredOutput call that a
        // Workflow schema requires. The schema itself reaches only the system prompt, which vendors never see,
        // so the engine's validation error is what tells the vendor how to correct its JSON.
        const verdict = agent.structured
        if (verdict === 'sent' || (verdict && !verdict.isError)) {
          agent.structured = undefined
          agent.running = false
          await keep($, agentId)
          yield { kind: 'stop', stopReason: 'end_turn', usage: null }
          return result(e, '')
        }
        if (verdict && agent.corrected) throw new Error(`the vendor's JSON was rejected again: ${verdict.text}`)
        let value = verdict ? undefined : json(agent.lastAnswer ?? '')
        if (!value) {
          // One correction per turn: an answer after it that still holds no JSON ends the turn.
          if (agent.corrected) throw new Error('the vendor\'s corrected answer held no JSON object')
          agent.corrected = true
          const text = verdict ? `That JSON was rejected: ${verdict.text}\nReply with only the corrected JSON object.`
            : 'Reply with only a JSON object holding your answer, with no other text.'
          outcome = yield* vendorTurn($, '/turn', { agentId, text, model: agent.model, effort: agent.effort ?? e.effort, rows: true }, stream, await daemonOf($, agent))
          if (!outcome.tool && !outcome.row) {
            agent.lastAnswer = stream.answer
            value = json(stream.answer)
            if (!value) throw new Error('the vendor returned no JSON object for StructuredOutput')
          }
        }
        if (value) {
          agent.structured = 'sent'
          await keep($, agentId)
          const id = `lord_structured_${e.turnId}_${e.index}`
          yield { kind: 'tool', index: stream.next, id, name: 'StructuredOutput' }
          yield { kind: 'input', index: stream.next, json: JSON.stringify(value) }
          yield { kind: 'stop', stopReason: 'tool_use', usage: null }
          return { turnId: e.turnId, index: e.index, answer: '', toolUses: [{ name: 'StructuredOutput', input: value }], stopReason: 'tool_use', usage: null }
        }
      }
      agent.continuing = !!(outcome!.tool || outcome!.row)
      if (outcome!.row) {
        const { rowId, shell, title } = outcome!.row
        const name = shell ? 'Bash' : stepTool(agent.family), input = shell ? { command: title, description: title.slice(0, 80) } : { step: title }
        agent.row = rowId
        await keep($, agentId)
        yield { kind: 'tool', index: stream.next, id: rowId, name }
        yield { kind: 'input', index: stream.next, json: JSON.stringify(input) }
        yield { kind: 'stop', stopReason: 'tool_use', usage: null }
        return { turnId: e.turnId, index: e.index, answer: '', toolUses: [{ name, input }], stopReason: 'tool_use', usage: null }
      }
      if (outcome!.tool) {
        const { callId, name, input } = outcome!.tool
        pending.set(callId, agentId)
        agent.call = callId
        await keep($, agentId)
        yield { kind: 'tool', index: stream.next, id: callId, name }
        yield { kind: 'input', index: stream.next, json: JSON.stringify(input) }
        yield { kind: 'stop', stopReason: 'tool_use', usage: null }
        return { turnId: e.turnId, index: e.index, answer: '', toolUses: [{ name, input }], stopReason: 'tool_use', usage: null }
      }
      // A vendor with nothing to add to a notification says nothing; the agent's answer stays its last one.
      if (!stream.answer.trim() && agent.notice && agent.lastAnswer) stream.answer = agent.lastAnswer
      if (!stream.answer.trim()) throw new Error('the vendor finished with an empty answer')
      // A correction that needed a lord tool ends here; its answer is the next StructuredOutput candidate.
      if (agent.structured && agent.structured !== 'sent') agent.structured = undefined
      // A turn whose new message failed leaves no answer a Workflow schema step could take for its own.
      agent.lastAnswer = stream.failed ? undefined : stream.answer
      // A refused follow-up leaves the daemon on its earlier model; the next turns ask for that one, not the refused one.
      if (stream.failed && stream.model) agent.model = stream.model
      agent.running = false
      await keep($, agentId)
      stream.answer = signed(stream.answer, stream.record)
      // The latest block must read as the final answer: the rest of it is added to the block on screen when that block
      // is its beginning, and otherwise the answer is a block of its own.
      if (stream.answer !== stream.shown) {
        if (stream.live >= 0 && stream.shown && stream.answer.startsWith(stream.shown)) yield { kind: 'text', index: stream.live, text: stream.answer.slice(stream.shown.length) }
        else yield { kind: 'text', index: stream.next++, text: stream.answer }
      }
      agent.report = stream.answer
      if (await handbackActive($, agentId, agent)) {
        agent.handback = 'sent'
        await keep($, agentId)
        return yield* deliver(e, stream.next, stream.answer)
      }
      await keep($, agentId)
      yield { kind: 'stop', stopReason: 'end_turn', usage: null }
      return result(e, stream.answer)
    } catch (error) {
      agent.running = false
      agent.continuing = false
      await keep($, agentId).catch(() => {})
      if (next.signal.aborted) {
        if (!ending) try { await request($, '/interrupt', { agentId }, await daemonOf($, agent)) } catch {}
        throw error
      }
      // The result is the error alone, so a partial answer is never mistaken for a finished one; the
      // partial text was already shown live.
      const text = signed(errorLine(error), stream.record)
      yield { kind: 'text', index: stream.next, text }
      agent.report = text
      agent.lastAnswer = undefined
      if (await handbackActive($, agentId, agent)) {
        agent.handback = 'sent'
        await keep($, agentId).catch(() => {})
        return yield* deliver(e, stream.next + 1, text)
      }
      await keep($, agentId).catch(() => {})
      yield { kind: 'stop', stopReason: 'end_turn', usage: null }
      return result(e, text)
    }
  }).catch(async function* ($, e, next) {
    // A lord agent's turn never falls back to the stand-in model; anything else passes through.
    if (!e.agentId || !agents.has(e.agentId)) return yield* next(e)
    if (next.signal.aborted) {
      if (!ending) try { await request($, '/interrupt', { agentId: e.agentId }, await daemonOf($, agents.get(e.agentId)!)) } catch {}
      throw new Error('Lord turn interrupted')
    }
    const line = 'lord: error: turn hook failed'
    yield { kind: 'text', index: 0, text: line }
    yield { kind: 'stop', stopReason: 'end_turn', usage: null }
    return result(e, line)
  })

  on('tool.call', { tool: ['Agent', 'SendMessage', 'StructuredOutput', HANDBACK] }, async ($, e, next) => {
    // A named Agent call starts an agent-team teammate in its own Claude process, whose turns no Lord hook answers.
    if (e.tool === 'Agent' && String((e as any).subagent_type ?? '').startsWith('lord:') && (e as any).name)
      return { deny: `lord agents cannot be named: a named Agent call starts a separate Claude process instead of ${String((e as any).subagent_type).slice(5)}. Launch it without name and message it by the agent id the launch returns.` }
    if (e.tool === 'Agent' && String((e as any).subagent_type ?? '').startsWith('lord:') && (e as any).isolation === 'worktree') isolated.add(e.tool_use_id)
    let call = e, launched: string | undefined
    if (e.tool === 'Agent' && String((e as any).subagent_type ?? '').startsWith('lord:')) {
      // The level the vendor will run: an @effort line, else the Agent call's, else the agent files' high, capped per
      // vendor. Claude Code shows the call's effort beside the agent's model, so the call carries that level when it
      // is one Claude Code accepts (ultra is codex-only and stays an @effort line).
      const input = e as any, family = String(input.subagent_type).slice(5), options = task(String(input.prompt ?? ''))
      // A launch whose option lines Lord cannot read is refused before an agent starts.
      if (options.error) return { deny: `lord: ${options.error}` }
      const error = testsError(family, options)
      if (error) return { deny: `lord: ${error}` }
      const effort = capped(family, options.effort ?? input.effort ?? 'high')
      callEfforts.set(e.tool_use_id, effort)
      if (effort !== input.effort && ['low', 'medium', 'high', 'xhigh', 'max'].includes(effort)) call = { ...e, effort } as typeof e
      if (!input.resume) launched = await launchLine($, family, options, effort).catch(() => undefined)
    }
    const input = e as any
    const sent = e.tool === 'SendMessage' && typeof input.to === 'string' && typeof input.message === 'string' ? await message($, input.to, input.message) : undefined
    // A message a Lord agent's own vendor sent still reports its result to that vendor below.
    const r: any = sent ? { result: { success: true, message: sent } } : await next(call)
    const agent = e.agentId ? await lordAgent($, e.agentId) : undefined
    if (e.tool === HANDBACK) {
      if (agent?.handback === 'sent') {
        agent.handback = r.isError || r.deny ? { isError: true, text: String(r.text ?? r.deny ?? '') } : 'done'
        await keep($, e.agentId!)
      }
    } else if (e.tool === 'StructuredOutput') {
      if (agent?.structured === 'sent') {
        agent.structured = { isError: !!(r.isError || r.deny), text: String(r.text ?? r.deny ?? '') }
        await keep($, e.agentId!)
      }
    } else if (agent && (pending.get(e.tool_use_id) === e.agentId || agent.call === e.tool_use_id)) {
      pending.delete(e.tool_use_id)
      delete agent.call
      try { await request($, '/tool_result', { agentId: e.agentId, callId: e.tool_use_id, text: r.text ?? r.deny ?? JSON.stringify(r.result), isError: !!(r.isError || r.deny) }, await daemonOf($, agent)) }
      catch (error) { agent.error = errorLine(error) }
      await keep($, e.agentId!)
    }
    if (launched && !r.deny && !r.isError) return { ...r, context: [...(r.context ?? []), launched] }
    return r
  })

  on('tool.call', { tool: ['Bash', ...stepTools] }, async ($, e, next) => {
    const agent = e.agentId ? await lordAgent($, e.agentId) : undefined
    if (agent && agent.row === e.tool_use_id) {
      // The vendor ran this step itself; its row waits for the vendor's report that the step finished.
      let row: { output?: string; stopped?: boolean } | undefined
      try { for await (const value of events($, '/row', { agentId: e.agentId, rowId: e.tool_use_id }, await daemonOf($, agent))) row = value }
      catch (error) { row = { output: errorLine(error), stopped: true } }
      delete agent.row
      await keep($, e.agentId!)
      const output = String(row?.output ?? '')
      return (e.tool === 'Bash' ? { result: { stdout: output, stderr: '', interrupted: !!row?.stopped } } : { result: output }) as any
    }
    // A Lord agent's Bash and Lord's step tools only draw what a vendor did.
    if (agent || e.tool !== 'Bash') return { deny: 'Lord draws its agents\' vendor steps with this tool; it runs nothing.' }
    return next(e)
  })

  on('session.compact', async ($, e, next) => {
    // A Lord agent's transcript is never sent to a model: its vendor keeps its own context, and Lord answers every
    // step, so summarizing it would spend Claude tokens on nothing. It is shortened in place instead (compacted).
    if (!e.agentId || !(agents.has(e.agentId) || await lordAgent($, e.agentId))) return next(e)
    const messages = e.trigger === 'precompute' ? undefined : compacted(e.messages)
    return messages ? { messages } : { skip: "a Lord agent's vendor keeps its own context" }
  })

  on('session.send', async ($, e, next) => {
    let agentId = agents.has(e.to) || await lordAgent($, e.to) ? e.to : undefined
    if (!agentId && [...agents.values()].some(agent => agent.running)) {
      agentId = (await $.agent.list()).find(agent => agent.name === e.to || agent.teammateId === e.to)?.id
    }
    if (agentId && await steer($, agentId, e.text)) return { isDelivered: true }
    return next(e)
  })

  on('session.receive', async ($, e, next) => {
    if (e.agentId && await steer($, e.agentId, e.text)) return { consumed: 'steered active Lord turn' }
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    starts.delete(e.turnId)
    if (e.agentId) await $.state.set({ ...startState, id: e.turnId }, null)
    else {
      mainTurns = Math.max(0, mainTurns - 1)
      void reloadIfUpdated($).catch(() => {})
    }
    const agent = e.agentId ? await lordAgent($, e.agentId) : undefined
    if (agent) {
      // Aborting a dispatch also aborts its HTTP calls; completion has a fresh dispatch. A run Claude Code ended on its
      // own error leaves the vendor to finish its turn (never cut mid-edit); the daemon keeps that result.
      if ((e.isAborted || (agent.running && e.reason !== 'error')) && !ending) {
        try { await request($, '/interrupt', { agentId: e.agentId }, await daemonOf($, agent)) } catch {}
        agent.running = false
        await keep($, e.agentId)
      } else if (agent.running) {
        agent.running = false
        agent.continuing = false
        await keep($, e.agentId)
        const r = await next(e)
        return { ...r, text: [r.text, `lord: ${agent.family} is still working on this turn; send this agent a message to get its result once it finishes (TaskStop then stops that run and the vendor).`].filter(Boolean).join('\n\n') }
      }
    }
    return next(e)
  })

  on('session.end', ($, e, next) => {
    // /clear and an in-process resume end the conversation but not the process; their agents stay stoppable.
    if (!['clear', 'resume'].includes(e.reason)) ending = true
    return next(e)
  })

  on('command.run', { command: 'lord' }, async ($, e) => {
    try {
      const args = e.args.trim().split(/\s+/)
      const listing = await request($, '/agents').catch(() => undefined)
      // Nothing is listening before the first Lord turn starts the daemon.
      if (listing === undefined) return { text: args[0] ? 'lord: no agents are running' : 'lord: no agents' }
      const list = JSON.parse(listing)
      if (args[0] === 'model' && args.length === 3) {
        const known = list.find((agent: any) => agent.agentId === args[1])
        if (!known) return { text: `lord: unknown agent ${args[1]}` }
        const agent = agents.get(args[1]) ?? { family: known.family, cwd: known.cwd }
        // Staged like an @model line: it applies only once /open accepts it.
        agent.pending = merge({ text: '', ...agent.pending }, { model: args[2] })
        agents.set(args[1], agent)
        await keep($, args[1])
        return { text: `lord: ${args[1]} will use ${args[2]} next turn.` }
      }
      if (args[0]) return { text: 'Usage: /lord [model <agentId> <model>]' }
      const allowance = JSON.parse(await request($, '/allowance').catch(() => '{}'))
      return { text: [list.map((agent: any) => {
        const next = agents.get(agent.agentId)?.model
        const tokens = agent.usage ? ` ${agent.usage.input_tokens}+${agent.usage.cache_read_input_tokens} in ${agent.usage.output_tokens} out` : ''
        const quiet = typeof agent.quiet_s === 'number' ? ` (last vendor action ${agent.quiet_s} s ago)` : ''
        return `${agent.agentId} ${agent.family} ${agent.model}${next && next !== agent.model ? ` (next: ${next})` : ''} ${agent.effort ?? ''} ${agent.state}${quiet}${tokens} ${agent.cwd ?? ''}${agent.activity ? ' — ' + agent.activity : ''}\n  log: ${agent.log}${agent.answer ? `\n  answer: ${agent.answer}` : ''}`
      }).join('\n') || 'lord: no agents', ...allowanceLines(allowance)].join('\n') }
    } catch (error) { return { text: errorLine(error) } }
  })
}

import type { EngineInterface, Register, TurnStepInput, TurnStepResult } from 'claude-code'

type Verdict = 'sent' | { isError: boolean; text: string }
type Agent = { family: string; cwd?: string; running?: boolean; model?: string; effort?: string; readonly?: boolean; net?: boolean; dirs?: string[]; limits?: Limits; opened?: boolean; error?: string
  continuing?: boolean; lastAnswer?: string; structured?: Verdict; corrected?: boolean; worktree?: boolean; pending?: Task }
type Stream = { next: number; live: number; answer: string }
type Outcome = { tool?: { callId: string; name: string; input: Record<string, unknown> } }
type Limits = { memory_gb?: number; hang_s?: number; wall_s?: number; tools?: boolean }
type Task = { text: string; error?: string; model?: string; effort?: string; readonly?: boolean; net?: boolean; dirs?: string[]; cwd?: string; limits?: Limits }
const agents = new Map<string, Agent>()
const starts = new Map<string, Task>()
const directories = new Map<string, string>()
const pending = new Map<string, string>()
const others = new Set<string>()
const isolated = new Set<string>()
// Quitting aborts the session's agent turns; the daemon keeps those turns running for the next session.
let ending = false

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
  while (lines.length) {
    const line = lines[0].trim()
    const model = /^@model\s+(\S+)\s*$/.exec(line)
    if (model) result.model = model[1]
    else if (/^@effort\s+(minimal|low|medium|high|xhigh|max|ultra)\s*$/.test(line)) result.effort = line.split(/\s+/)[1]
    else if (line === '@readonly') result.readonly = true
    else if (line === '@net') result.net = true
    else if (/^@memory\s+\d+(\.\d+)?\s*$/.test(line)) result.limits = { ...result.limits, memory_gb: Number(line.split(/\s+/)[1]) }
    else if (/^@hang\s+\d+\s*$/.test(line)) result.limits = { ...result.limits, hang_s: Number(line.split(/\s+/)[1]) }
    else if (/^@wall\s+\d+\s*$/.test(line)) result.limits = { ...result.limits, wall_s: Number(line.split(/\s+/)[1]) }
    else if (line === '@tools') result.limits = { ...result.limits, tools: true }
    else if (/^@cwd\s+\S/.test(line)) result.cwd = line.replace(/^@cwd\s+/, '')
    else if (/^@dir\s+\S/.test(line)) result.dirs = [...(result.dirs ?? []), line.replace(/^@dir\s+/, '')]
    // A known option written wrongly would end the options here and leave the rest (an @readonly, say) as task text.
    else if (/^@(model|effort|readonly|net|memory|hang|wall|tools|cwd|dir)\b/.test(line)) { result.error = `option line not understood: ${line}`; break }
    else break
    lines.shift()
  }
  result.text = lines.join('\n')
  return result
}

function merge(base: Task, more: Partial<Task>): Task {
  return { ...base, ...more, dirs: more.dirs ? [...new Set([...(base.dirs ?? []), ...more.dirs])] : base.dirs,
    limits: more.limits ? { ...base.limits, ...more.limits } : base.limits }
}

function errorLine(error: unknown): string {
  return 'lord: error: ' + String(error instanceof Error ? error.message : error).replace(/\s+/g, ' ').trim()
}

function result(e: TurnStepInput, answer: string): TurnStepResult {
  // Usage stays in the daemon (/lord): the engine reads a step's usage as this agent's context size and would
  // auto-compact the transcript with a Claude model, while the vendor manages its own context.
  return { turnId: e.turnId, index: e.index, answer, toolUses: [], stopReason: 'end_turn', usage: null }
}

async function socket($: EngineInterface): Promise<string> {
  return await $.env.get('LORD_DAEMON_SOCKET') || `${await $.env.get('HOME')}/.claude/lord/lordd.sock`
}

async function keyFile($: EngineInterface): Promise<string> {
  // The daemon writes its control key next to its socket; vendor sandboxes cannot read it.
  return (await socket($)).replace(/[^/]*$/, 'control.header')
}

let pluginVersion: Promise<string | undefined> | undefined
function version($: EngineInterface): Promise<string | undefined> {
  // Sent with /open: a daemon older than this plugin exits once idle, and the next turn starts this version's.
  pluginVersion ??= $.fs.read(`${$.plugin.root}/.claude-plugin/plugin.json`).then(text => JSON.parse(String(text)).version, () => undefined)
  return pluginVersion
}

async function request($: EngineInterface, path: string, body?: unknown): Promise<string> {
  const key = await $.fs.read(await keyFile($)).then(text => /^X-Lord-Control:\s*(\S+)/.exec(String(text))?.[1], () => undefined)
  const response = await $.http.fetch('http://lord' + path, {
    socketPath: await socket($), method: body === undefined ? 'GET' : 'POST',
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

async function* events($: EngineInterface, path: string, body: unknown): AsyncGenerator<any, void> {
  // The engine's HTTP API returns a complete body; curl exposes NDJSON as it arrives.
  const child = $.process.spawn({
    argv: ['/usr/bin/curl', '--disable', '--silent', '--show-error', '--no-buffer', '--fail-with-body', '--noproxy', '*',
      '--connect-timeout', '5', '--unix-socket', await socket($), '--request', 'POST',
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
    if (signal.aborted || await socket($) !== `${await $.env.get('HOME')}/.claude/lord/lordd.sock`) throw error
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

async function refresh($: EngineInterface): Promise<void> {
  let list: any[]
  try { list = JSON.parse(await request($, '/agents')) } catch { $.ui.status(undefined); return }
  const counts = new Map<string, number>()
  for (const agent of list) if (['running', 'waiting_tool'].includes(agent.state)) counts.set(agent.family, (counts.get(agent.family) ?? 0) + 1)
  const labels = ['gpt', 'grok', 'gemini'].filter(family => counts.has(family)).map(family => `${counts.get(family)} ${family}`)
  if (counts.has('gpt')) {
    try {
      const usage = JSON.parse(await request($, '/usage'))
      if (typeof usage.chatgpt?.usedPercent === 'number') labels.push(`ChatGPT ${usage.chatgpt.usedPercent}%`)
    } catch {}
  }
  $.ui.status(labels.length ? 'lord: ' + labels.join(' · ') : undefined)
}

async function steer($: EngineInterface, agentId: string, text: string): Promise<boolean> {
  const agent = agents.get(agentId)
  if (!agent?.running) return false
  const parsed = task(text)
  // Directives alter the next turn's session options rather than an in-flight request.
  if (parsed.error) return false
  const { text: _, ...options } = parsed
  agent.pending = merge({ text: '', ...agent.pending }, options)
  try {
    const response = JSON.parse(await request($, '/steer', { agentId, text: parsed.text }))
    return response.steered === true
  } catch { return false }
}

async function lordAgent($: EngineInterface, agentId: string): Promise<Agent | undefined> {
  // Module state starts empty after a reload; the engine's roster still names each agent's type.
  if (agents.has(agentId) || others.has(agentId)) return agents.get(agentId)
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

async function* vendorTurn($: EngineInterface, path: string, body: unknown, stream: Stream): AsyncGenerator<any, Outcome> {
  for await (const event of events($, path, body)) {
    if (typeof event !== 'object' || event === null) throw new Error('invalid daemon event')
    if (event.type === 'activity' && typeof event.text === 'string') {
      stream.live = -1
      yield { kind: 'thinking', index: stream.next++, text: event.text }
    } else if (event.type === 'text' && typeof event.text === 'string') {
      // The engine keeps an agent's last text block as its result, so streamed vendor text is shown as
      // thinking (not recorded) and the final answer is recorded once, whole, at done.
      if (stream.live < 0) stream.live = stream.next++
      stream.answer += event.text
      yield { kind: 'thinking', index: stream.live, text: event.text }
    } else if (event.type === 'tool') {
      if (!['Agent', 'SendMessage'].includes(event.name) || typeof event.callId !== 'string' || !event.input || typeof event.input !== 'object') throw new Error('invalid daemon tool call')
      return { tool: { callId: event.callId, name: event.name, input: event.input } }
    } else if (event.type === 'done' && typeof event.text === 'string') {
      // The vendor's final answer alone; narration streamed before it is not the result.
      stream.answer = event.text
      return {}
    } else if (event.type === 'error') throw new Error(event.message || 'daemon turn failed')
    else throw new Error('invalid daemon event')
  }
  throw new Error('daemon stream ended without done or tool')
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    ending = false
    const r = await next(e)
    await $.command.register({ name: 'lord', description: 'List Lord agents or select their next model.', argumentHint: '[model <agentId> <model>]', immediate: true })
    await refresh($)
    return r
  })

  on('agent.spawn', async ($, e, next) => {
    const r = await next(e)
    if (e.subagentType.startsWith('lord:') && r.agentId) {
      const previous = agents.get(r.agentId)
      agents.set(r.agentId, { ...previous, family: e.subagentType.slice(5), cwd: directories.get(r.agentId) ?? e.cwd ?? previous?.cwd,
        worktree: previous?.worktree || isolated.has(e.tool_use_id) })
      isolated.delete(e.tool_use_id)
    }
    return r
  })

  on('classic.SubagentStart', async ($, e, next) => {
    // Only the start folder is recorded: an opened agent's folder is the daemon's, which lordAgent restores.
    if (e.agent_type.startsWith('lord:')) directories.set(e.agent_id, e.cwd)
    return next(e)
  })

  on('turn.start', ($, e, next) => {
    // A new agent's start can precede the spawn result that identifies its type.
    if (e.agentId) starts.set(e.turnId, task(e.text))
    return next(e)
  })

  on('turn.step', async function* ($, e, next) {
    const agent = e.agentId ? await lordAgent($, e.agentId) : undefined
    if (!agent) return yield* next(e)
    const agentId = e.agentId!
    const stream: Stream = { next: 0, live: -1, answer: '' }
    try {
      if (agent.error) { const error = agent.error; delete agent.error; throw new Error(error.replace(/^lord: error: /, '')) }
      let outcome: Outcome | undefined
      agent.running = true
      if (e.index === 0) {
        const started = starts.get(e.turnId)
        if (!started) throw new Error('missing turn.start task')
        if (started.error) throw new Error(started.error)
        // Directives from this task and from messages steered into the last turn apply only once /open accepts them.
        const input = merge(agent.pending ?? { text: '' }, started)
        input.text = started.text
        delete agent.pending
        agent.structured = undefined
        agent.corrected = false
        await connect($, next.signal)
        // An agent restored while the daemon was down learns the daemon's settings once it is up.
        if (!agent.opened) {
          const known = await request($, '/agents').then(text => JSON.parse(text).find((a: any) => a.agentId === agentId), () => undefined)
          if (known) Object.assign(agent, { opened: true, cwd: known.cwd, effort: agent.effort ?? known.effort, model: agent.model ?? known.model })
        }
        const chosen = merge({ text: '', model: agent.model, effort: agent.effort, readonly: agent.readonly, net: agent.net, dirs: agent.dirs, limits: agent.limits }, input)
        // Only an absolute @cwd replaces the worktree; a relative one would be under the main tree.
        if (agent.worktree && !(input.cwd && /^[/~]/.test(input.cwd)) && !directories.get(agentId)) throw new Error('worktree isolation was requested but Claude Code reported no worktree path; refusing to run in the main tree')
        // A task can name the vendor's working folder (fixed at the first turn; the daemon compares folders, not
        // spellings). A relative one is under the agent's start folder, not the daemon's.
        const base = input.cwd && /^[/~]/.test(input.cwd) ? input.cwd : `${directories.get(agentId) ?? agent.cwd ?? await $.session.cwd()}${input.cwd ? '/' + input.cwd : ''}`
        if (input.cwd) input.cwd = base
        // A relative @dir is under the agent's working folder too.
        if (input.dirs) input.dirs = input.dirs.map(dir => /^[/~]/.test(dir) ? dir : `${input.cwd ?? (agent.opened ? agent.cwd : undefined) ?? base}/${dir}`)
        const cwd = input.cwd ?? (agent.opened ? agent.cwd : undefined) ?? directories.get(agentId) ?? agent.cwd ?? await $.session.cwd()
        await request($, '/open', { agentId, family: agent.family, cwd, pin_cwd: !!input.cwd, path: await $.env.get('PATH'), version: await version($), model: chosen.model, effort: chosen.effort ?? e.effort, readonly: chosen.readonly, net: chosen.net, dirs: chosen.dirs, ...chosen.limits })
        Object.assign(agent, { model: chosen.model, effort: chosen.effort, readonly: chosen.readonly, net: chosen.net, dirs: chosen.dirs, limits: chosen.limits })
        agent.opened = true
        agent.cwd = cwd
        void refresh($)
        outcome = yield* vendorTurn($, '/turn', { agentId, text: input.text, model: agent.model, effort: agent.effort ?? e.effort }, stream)
      } else if (agent.continuing) {
        outcome = yield* vendorTurn($, '/continue', { agentId }, stream)
      } else {
        // A later step with no lord tool behind it is the engine asking for the StructuredOutput call that a
        // Workflow schema requires. The schema itself reaches only the system prompt, which vendors never see,
        // so the engine's validation error is what tells the vendor how to correct its JSON.
        const verdict = agent.structured
        if (verdict === 'sent' || (verdict && !verdict.isError)) {
          agent.structured = undefined
          agent.running = false
          void refresh($)
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
          outcome = yield* vendorTurn($, '/turn', { agentId, text, model: agent.model, effort: agent.effort ?? e.effort }, stream)
          if (!outcome.tool) {
            agent.lastAnswer = stream.answer
            value = json(stream.answer)
            if (!value) throw new Error('the vendor returned no JSON object for StructuredOutput')
          }
        }
        if (value) {
          agent.structured = 'sent'
          const id = `lord_structured_${e.turnId}_${e.index}`
          yield { kind: 'tool', index: stream.next, id, name: 'StructuredOutput' }
          yield { kind: 'input', index: stream.next, json: JSON.stringify(value) }
          yield { kind: 'stop', stopReason: 'tool_use', usage: null }
          return { turnId: e.turnId, index: e.index, answer: '', toolUses: [{ name: 'StructuredOutput', input: value }], stopReason: 'tool_use', usage: null }
        }
      }
      agent.continuing = !!outcome!.tool
      if (outcome!.tool) {
        const { callId, name, input } = outcome!.tool
        pending.set(callId, agentId)
        yield { kind: 'tool', index: stream.next, id: callId, name }
        yield { kind: 'input', index: stream.next, json: JSON.stringify(input) }
        yield { kind: 'stop', stopReason: 'tool_use', usage: null }
        return { turnId: e.turnId, index: e.index, answer: '', toolUses: [{ name, input }], stopReason: 'tool_use', usage: null }
      }
      if (!stream.answer.trim()) throw new Error('the vendor finished with an empty answer')
      // A correction that needed a lord tool ends here; its answer is the next StructuredOutput candidate.
      if (agent.structured && agent.structured !== 'sent') agent.structured = undefined
      agent.lastAnswer = stream.answer
      agent.running = false
      void refresh($)
      yield { kind: 'text', index: stream.next++, text: stream.answer }
      yield { kind: 'stop', stopReason: 'end_turn', usage: null }
      return result(e, stream.answer)
    } catch (error) {
      agent.running = false
      agent.continuing = false
      if (next.signal.aborted) {
        if (!ending) try { await request($, '/interrupt', { agentId }) } catch {}
        throw error
      }
      void refresh($)
      // The result is the error alone, so a partial answer is never mistaken for a finished one; the
      // partial text was already shown live.
      const text = errorLine(error)
      yield { kind: 'text', index: stream.next, text }
      yield { kind: 'stop', stopReason: 'end_turn', usage: null }
      return result(e, text)
    }
  }).catch(async function* ($, e, next) {
    // A lord agent's turn never falls back to the stand-in model; anything else passes through.
    if (!e.agentId || !agents.has(e.agentId)) return yield* next(e)
    if (next.signal.aborted) {
      if (!ending) try { await request($, '/interrupt', { agentId: e.agentId }) } catch {}
      throw new Error('Lord turn interrupted')
    }
    const line = 'lord: error: turn hook failed'
    yield { kind: 'text', index: 0, text: line }
    yield { kind: 'stop', stopReason: 'end_turn', usage: null }
    return result(e, line)
  })

  on('tool.call', { tool: ['Agent', 'SendMessage', 'StructuredOutput'] }, async ($, e, next) => {
    if (e.tool === 'Agent' && String((e as any).subagent_type ?? '').startsWith('lord:') && (e as any).isolation === 'worktree') isolated.add(e.tool_use_id)
    const r = await next(e)
    const agent = e.agentId ? agents.get(e.agentId) : undefined
    if (e.tool === 'StructuredOutput') {
      if (agent?.structured === 'sent') agent.structured = { isError: !!(r.isError || r.deny), text: String(r.text ?? r.deny ?? '') }
    } else if (e.agentId && pending.get(e.tool_use_id) === e.agentId) {
      pending.delete(e.tool_use_id)
      try { await request($, '/tool_result', { agentId: e.agentId, callId: e.tool_use_id, text: r.text ?? r.deny ?? JSON.stringify(r.result), isError: !!(r.isError || r.deny) }) }
      catch (error) { agents.get(e.agentId)!.error = errorLine(error) }
      void refresh($)
    }
    return r
  })

  on('session.send', async ($, e, next) => {
    let agentId = agents.has(e.to) ? e.to : undefined
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
    if (e.agentId && agents.has(e.agentId)) {
      const agent = agents.get(e.agentId)!
      // Aborting a dispatch also aborts its HTTP calls; completion has a fresh dispatch.
      if ((e.isAborted || agent.running) && !ending) {
        try { await request($, '/interrupt', { agentId: e.agentId }) } catch {}
        agent.running = false
        void refresh($)
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
        return { text: `lord: ${args[1]} will use ${args[2]} next turn.` }
      }
      if (args[0]) return { text: 'Usage: /lord [model <agentId> <model>]' }
      return { text: list.map((agent: any) => {
        const next = agents.get(agent.agentId)?.model
        const tokens = agent.usage ? ` ${agent.usage.input_tokens}+${agent.usage.cache_read_input_tokens} in ${agent.usage.output_tokens} out` : ''
        return `${agent.agentId} ${agent.family} ${agent.model}${next && next !== agent.model ? ` (next: ${next})` : ''} ${agent.effort ?? ''} ${agent.state}${tokens} ${agent.cwd ?? ''}${agent.activity ? ' — ' + agent.activity : ''}\n  log: ${agent.log}`
      }).join('\n') || 'lord: no agents' }
    } catch (error) { return { text: errorLine(error) } }
  })
}

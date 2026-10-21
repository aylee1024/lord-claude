import { test, expect, mock } from 'claude-code/testing'
import type { On } from 'claude-code'

function world(on: On, events: unknown[] = [{ type: 'done', text: 'fake answer', model: 'fake-gpt', usage: { input_tokens: 3, output_tokens: 2, cache_read_input_tokens: 1 } }], failures = 0, socket: string | null = '/test/lord.sock', pythonExits: Record<string, number> = {}, env: Record<string, string> = {}) {
  const requests: { path: string; body: any; socket?: string }[] = []
  const statuses: string[] = []
  const processes: string[][] = []
  const deliveries: string[] = []
  let modelCalls = 0
  let opened: string | undefined
  let streamFinished = false
  // Each /turn or /continue stream takes the next queued event list, else `events`; /row answers `replies.row`.
  const sequence: unknown[][] = []
  const replies: { row: unknown } = { row: { output: 'row output', stopped: false } }
  mock.env(on, { HOME: '/test', ...(socket ? { LORD_DAEMON_SOCKET: socket } : {}), ...env })
  let spawned = 'lord:gpt'
  const clock = mock.clock(on)
  on('process.spawn', async function* (_, e) {
    if (String(e.argv[1]).endsWith('/lordd.py')) {
      processes.push([...e.argv])
      const code = pythonExits[e.argv[0]]
      if (code === 2) yield { stream: 'stderr', text: '/test/.claude/lord/config.json is not valid JSON: line 1\n' }
      if (code !== undefined) return { value: { code, signal: null } }
    } else {
      const path = e.argv[e.argv.length - 1].replace('http://lord', '')
      requests.push({ path, body: JSON.parse(e.input ?? '{}'), socket: e.argv[e.argv.indexOf('--unix-socket') + 1] })
      const raw = path === '/row' ? JSON.stringify(replies.row) : (sequence.length ? sequence.shift()! : events).map(x => JSON.stringify(x)).join('\n') + '\n'
      for (let i = 0; i < raw.length; i += 11) yield { stream: 'stdout', text: raw.slice(i, i + 11) }
      streamFinished = true
    }
    return { value: { code: 0, signal: null } }
  })
  // Like the real daemon, an agent spawned in this session is listed only after its /open; without a spawn the
  // test stands for a module reload and the daemon already holds the agent's settings.
  let fresh = false
  const spawnInputs: any[] = []
  on('agent.spawn', (_, e) => { spawned = e.subagentType; spawnInputs.push(e); fresh = true; return { agentId: 'a1', model: 'haiku' } })
  const commands: any[] = [], toasts: string[] = []
  on('command.run', (_, e) => { commands.push(e); return { text: '' } })
  on('ui.toast', (_, e) => { toasts.push(e.text); return { value: {} } })
  on('classic.SubagentStart', () => ({}))
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_, e) => ({ text: e.answer }))
  on('session.cwd', () => ({ value: '/repo' }))
  on('ui.status', (_, e) => { statuses.push(e.text ?? ''); return { value: {} } })
  on('command.register', () => ({ value: { name: 'lord' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.send', (_, e) => { deliveries.push(e.text); return { isDelivered: true } })
  on('session.receive', (_, e) => ({ text: e.text }))
  const roster: any[] = []
  on('agent.list', () => ({ value: [{ id: 'a1', type: spawned }, ...roster] as any }))
  const tools = { answer: { result: {}, text: 'child result', ref: 9 } as any }
  const toolCalls: any[] = []
  on('tool.call', (_, e) => { toolCalls.push(e); return tools.answer })
  on('http.fetch', (_, e) => {
    const path = e.url.replace('http://lord', '')
    requests.push({ path, body: e.init?.body ? JSON.parse(e.init.body) : undefined, socket: e.init?.socketPath })
    if (socket === '/down.sock') return { deny: 'socket unavailable' }
    // The daemon compares a pinned @cwd with the folder the agent first opened in.
    if (path === '/open') {
      const body = JSON.parse(e.init!.body!)
      if (body.model === '/bad-model') return { value: { ok: false, status: 400, headers: {}, text: JSON.stringify({ error: 'unknown gpt model /bad-model' }) } }
      if (body.pin_cwd && opened && body.cwd !== opened) return { value: { ok: false, status: 400, headers: {}, text: JSON.stringify({ error: `@cwd is fixed at the first turn (${opened})` }) } }
      if (!body.dirs?.includes('/bad')) opened = opened ?? body.cwd
    }
    if (path === '/open' && JSON.parse(e.init!.body!).dirs?.includes('/bad')) return { value: { ok: false, status: 400, headers: {}, text: JSON.stringify({ error: 'every @dir must be an existing directory' }) } }
    if (path === '/health' && failures-- > 0) return { deny: 'socket unavailable' }
    if (path === '/agents' && failures > 0) return { deny: 'socket unavailable' }
    const result = path === '/agents' ? (!fresh ? [{ agentId: 'a1', family: 'gpt', state: 'running', effort: 'ultra', model: 'fake-gpt', cwd: '/pinned' }]
        : requests.some(x => x.path === '/open') ? [{ agentId: 'a1', family: 'gpt', state: 'running' }] : [])
      : path === '/health' ? { ok: true }
      : path === '/open' ? { vendorSession: 'v1', model: 'fake-gpt', effort: 'high' }
      : path === '/steer' ? { ok: true, steered: true }
      : { ok: true }
    return { value: { ok: true, status: 200, headers: {}, text: path === '/turn' || path === '/continue' ? events.map(x => JSON.stringify(x)).join('\n') + '\n' : JSON.stringify(result) } }
  })
  on('turn.step', async function* (_, e) {
    modelCalls++
    return { turnId: e.turnId, index: e.index, answer: 'MODEL CALLED', toolUses: [], stopReason: 'end_turn', usage: null }
  })
  return { sequence, replies, events, tools, requests, statuses, processes, deliveries, clock, spawnInputs, toolCalls, commands, toasts, roster, modelCalls: () => modelCalls, streamFinished: () => streamFinished }
}

// The session's state as the host keeps it, which a test writes as the copy of the code before a reload would.
function sessionState(on: On) {
  const values = new Map<string, { value: unknown; version: number }>()
  const name = (e: any) => `${e.key}/${e.id ?? ''}`
  const write = (e: any) => { const version = (values.get(name(e))?.version ?? 0) + 1; values.set(name(e), { value: e.value, version }); return version }
  on('state.get', (_, e: any) => ({ value: values.get(name(e)) ?? { value: undefined, version: 0 } }) as any)
  on('state.set', (_, e: any) => ({ value: { isSet: true, version: write(e) } }) as any)
  return { put: (key: string, id: string, value: unknown) => write({ key, id, value }), get: (key: string, id: string) => values.get(`${key}/${id}`)?.value as any }
}

async function spawn($: any, type = 'lord:gpt') {
  await $.agent.spawn({ tool_use_id: 'spawn1', prompt: 'task', description: 'test', subagentType: type, provider: { plugin: 'lord', tier: 'user' }, parentModel: 'haiku', background: true, fork: false })
}

async function step($: any, text: string, index = 0) {
  await $.turn.start({ agentId: 'a1', turnId: 't1', text })
  const stream = $.turn.step({ agentId: 'a1', turnId: 't1', index, model: 'haiku', effort: 'xhigh', messageCount: 1 })
  const chunks = []
  while (true) {
    const item = await stream.next()
    if (item.done) return { chunks, result: item.value }
    chunks.push(item.value)
  }
}

test('lord turns use the daemon and return explicit results without Claude', async ($, on) => {
  const w = world(on)
  await spawn($)
  const { result, chunks } = await step($, 'hello')
  expect(result.answer).toBe('fake answer')
  expect(result.usage).toBeNull()
  expect(chunks).toContainEqual({ kind: 'text', index: 0, text: 'fake answer' })
  expect(w.modelCalls()).toBe(0)
  expect(w.requests.find(x => x.path === '/turn')?.body).toEqual({ agentId: 'a1', text: 'hello', effort: 'xhigh', rows: true })
})

test('activity is yielded before the daemon finishes sending its response', async ($, on) => {
  const w = world(on, [{ type: 'activity', text: 'live activity' }, { type: 'done', text: 'final answer' }])
  await spawn($)
  await $.turn.start({ agentId: 'a1', turnId: 't1', text: 'hello' })
  const stream = $.turn.step({ agentId: 'a1', turnId: 't1', index: 0, model: 'haiku', messageCount: 1 })
  expect((await stream.next()).value).toEqual({ kind: 'thinking', index: 0, text: 'live activity' })
  expect(w.streamFinished()).toBe(false)
  while (!(await stream.next()).done) {}
})

test('turn.start can arrive before the agent spawn has resolved', async ($, on) => {
  const w = world(on)
  await $.turn.start({ agentId: 'a1', turnId: 'early', text: 'early task' })
  await spawn($)
  const stream = $.turn.step({ agentId: 'a1', turnId: 'early', index: 0, model: 'haiku', messageCount: 1 })
  let result: any
  while (true) { const item = await stream.next(); if (item.done) { result = item.value; break } }
  expect(result.answer).toBe('fake answer')
  expect(w.requests.find(x => x.path === '/turn')?.body.text).toBe('early task')
  expect(w.modelCalls()).toBe(0)
})

test('Workflow wrappers and leading directives are removed and worktree cwd wins', async ($, on) => {
  const w = world(on)
  await spawn($, 'lord:grok')
  await $.classic.SubagentStart({ agent_id: 'a1', agent_type: 'lord:grok', cwd: '/repo/.claude/worktrees/agent-a1' })
  await step($, '[Workflow harness — computed task]\nextra header\nThe computed task text follows:\n\n  @model grok-model\n  @readonly\n  @net\n  do work\n    keep indent')
  expect(w.requests.find(x => x.path === '/open')?.body).toEqual({ agentId: 'a1', family: 'grok', cwd: '/repo/.claude/worktrees/agent-a1', pin_cwd: false, model: 'grok-model', readonly: true, net: true, effort: 'xhigh' })
  expect(w.requests.find(x => x.path === '/turn')?.body.text).toBe('do work\n  keep indent')
})

test('child notifications become a compact line', async ($, on) => {
  const w = world(on)
  await spawn($)
  await step($, '<task-notification>\n<task-id>child1</task-id>\n<status>completed</status>\n<summary>ignored</summary>\n<result>first\n  second</result>\n</task-notification>')
  expect(w.requests.find(x => x.path === '/turn')?.body.text).toBe('Agent child1 finished (completed): first\n  second')
})

test('the done event supplies the remaining final answer without repeating streamed text', async ($, on) => {
  const w = world(on, [{ type: 'text', text: 'hello ' }, { type: 'done', text: 'hello world' }])
  await spawn($)
  const { result, chunks } = await step($, 'hello')
  expect(result.answer).toBe('hello world')
  expect(chunks.filter(x => x.kind === 'text').map(x => x.text).join('')).toBe('hello world')
  expect(w.modelCalls()).toBe(0)
})

test('streamed text is recorded as it arrives, and a final answer that differs is the last block', async ($, on) => {
  world(on, [{ type: 'text', text: 'hel' }, { type: 'text', text: 'lo ' }, { type: 'activity', text: 'cmd' },
    { type: 'text', text: 'wor' }, { type: 'done', text: 'hello world' }])
  await spawn($)
  const { result, chunks } = await step($, 'hello')
  expect(chunks.filter(x => x.kind === 'text').map(x => [x.index, x.text])).toEqual([[0, 'hel'], [0, 'lo '], [2, 'wor'], [3, 'hello world']])
  expect(chunks.filter(x => x.kind === 'thinking').map(x => [x.index, x.text])).toEqual([[1, 'cmd']])
  expect(result.answer).toBe('hello world')
})

test('a final answer already shown whole is not shown again', async ($, on) => {
  world(on, [{ type: 'text', text: 'all ' }, { type: 'text', text: 'done' }, { type: 'done', text: 'all done' }])
  await spawn($)
  const { result, chunks } = await step($, 'hello')
  expect(chunks.filter(x => x.kind === 'text').map(x => [x.index, x.text])).toEqual([[0, 'all '], [0, 'done']])
  expect(result.answer).toBe('all done')
})

test('an error after partial output appears on its own line', async ($, on) => {
  world(on, [{ type: 'text', text: 'partial answer' }, { type: 'error', message: 'failed\nnow' }])
  await spawn($)
  const { result } = await step($, 'hello')
  expect(result.answer).toBe('lord: error: failed now')
})

test('a vendor command is drawn as a Bash row answered with its output, and the turn continues after it', async ($, on) => {
  const w = world(on)
  w.sequence.push([{ type: 'text', text: 'Looking. ' }, { type: 'row', rowId: 'r1', shell: true, title: 'ls -la' }], [{ type: 'done', text: 'Two files.' }])
  await spawn($)
  const first = await step($, 'hello')
  expect(first.chunks.map(x => x.kind)).toEqual(['text', 'tool', 'input', 'stop'])
  expect(first.chunks[1]).toMatchObject({ kind: 'tool', id: 'r1', name: 'Bash' })
  expect(JSON.parse(first.chunks[2].json)).toMatchObject({ command: 'ls -la' })
  expect(first.result.stopReason).toBe('tool_use')
  const r: any = await $.tool.call({ tool: 'Bash', tool_use_id: 'r1', agentId: 'a1', command: 'ls -la' } as any)
  expect(r.result).toEqual({ stdout: 'row output', stderr: '', interrupted: false })
  expect(w.requests.find(x => x.path === '/row')?.body).toEqual({ agentId: 'a1', rowId: 'r1' })
  expect(w.toolCalls).toEqual([])
  const second = await step($, 'hello', 1)
  expect(w.requests.find(x => x.path === '/continue')?.body).toEqual({ agentId: 'a1', rows: true })
  expect(second.result.answer).toBe('Two files.')
})

test('any other vendor step is drawn under the vendor\'s own step tool', async ($, on) => {
  const w = world(on)
  w.sequence.push([{ type: 'row', rowId: 'r2', shell: false, title: 'Read notes.txt' }])
  await spawn($)
  const { chunks } = await step($, 'hello')
  expect(chunks[0]).toMatchObject({ kind: 'tool', id: 'r2', name: 'mcp__lord__gpt' })
  expect(JSON.parse(chunks[1].json)).toEqual({ step: 'Read notes.txt' })
  const r: any = await $.tool.call({ tool: 'mcp__lord__gpt', tool_use_id: 'r2', agentId: 'a1', step: 'Read notes.txt' } as any)
  expect(r.result).toBe('row output')
})

test('a step the vendor stopped reads as interrupted', async ($, on) => {
  const w = world(on)
  w.sequence.push([{ type: 'row', rowId: 'r3', shell: true, title: 'sleep 300' }])
  w.replies.row = { output: '', stopped: true }
  await spawn($)
  await step($, 'hello')
  const r: any = await $.tool.call({ tool: 'Bash', tool_use_id: 'r3', agentId: 'a1', command: 'sleep 300' } as any)
  expect(r.result.interrupted).toBe(true)
})

test('only a vendor step may use a Lord agent\'s Bash or the step tools; other Bash calls pass', async ($, on) => {
  const w = world(on)
  await spawn($)
  await step($, 'hello')
  const own: any = await $.tool.call({ tool: 'Bash', tool_use_id: 'x1', agentId: 'a1', command: 'rm -rf /' } as any)
  expect(own.deny).toContain('runs nothing')
  const tool: any = await $.tool.call({ tool: 'mcp__lord__grok', tool_use_id: 'x2', step: 'anything' } as any)
  expect(tool.deny).toContain('runs nothing')
  await $.tool.call({ tool: 'Bash', tool_use_id: 'x3', command: 'ls' } as any)
  expect(w.toolCalls.map((x: any) => x.tool_use_id)).toEqual(['x3'])
})

test('a step still waiting when this code is reloaded is answered by the new copy', async ($, on) => {
  const w = world(on)
  const state = sessionState(on)
  state.put('agent', 'a1', { family: 'gpt', opened: true, cwd: '/work', running: true, continuing: true, row: 'r9' })
  const r: any = await $.tool.call({ tool: 'Bash', tool_use_id: 'r9', agentId: 'a1', command: 'make' } as any)
  expect(r.result.stdout).toBe('row output')
  expect(state.get('agent', 'a1').row).toBeUndefined()
})

test('a Lord agent\'s transcript is never summarized; any other is', async ($, on) => {
  world(on)
  const kept = [{ role: 'user', text: 'summary', toolUses: [] }]
  on('session.compact', () => ({ messages: kept }) as any)
  await spawn($)
  await step($, 'hello')
  const lord: any = await $.session.compact({ agentId: 'a1', trigger: 'auto', messages: kept } as any)
  expect(lord.skip).toContain('own context')
  const main: any = await $.session.compact({ trigger: 'auto', messages: kept } as any)
  expect(main.messages).toHaveLength(1)
})

test('first-line directives in a resumed SendMessage survive the coordinator envelope', async ($, on) => {
  const w = world(on)
  await spawn($)
  await step($, 'The coordinator sent a message while you were working:\n@model next-model\n@readonly\n@net\nSECOND\n\nAddress this before completing your current task.')
  expect(w.requests.find(x => x.path === '/turn')?.body).toEqual({ agentId: 'a1', text: 'SECOND', model: 'next-model', effort: 'xhigh', rows: true })
})

test('vendor tools run through native chunks and their results return before continue', async ($, on) => {
  const input = { subagent_type: 'lord:gemini', prompt: 'child', description: 'child', run_in_background: false }
  const w = world(on, [{ type: 'activity', text: 'working' }, { type: 'tool', callId: 'call1', name: 'Agent', input }])
  await spawn($)
  const { result, chunks } = await step($, 'parent')
  expect(result.stopReason).toBe('tool_use')
  expect(result.toolUses).toEqual([{ name: 'Agent', input }])
  expect(chunks).toContainEqual({ kind: 'thinking', index: 0, text: 'working' })
  expect(chunks).toContainEqual({ kind: 'tool', index: 1, id: 'call1', name: 'Agent' })
  await $.tool.call({ tool: 'Agent', tool_use_id: 'call1', agentId: 'a1', ...input })
  await step($, '', 1)
  expect(w.requests.find(x => x.path === '/tool_result')?.body).toEqual({ agentId: 'a1', callId: 'call1', text: 'child result', isError: false })
  expect(w.requests.some(x => x.path === '/continue')).toBe(true)
  expect(w.modelCalls()).toBe(0)
})

for (const events of [[{ type: 'error', message: 'vendor offline', retryable: false }], [], [{ type: 'nonsense' }]]) {
  test('daemon failure ends visibly without model fallback: ' + JSON.stringify(events), async ($, on) => {
    const w = world(on, events)
    await spawn($)
    const { result, chunks } = await step($, 'hello')
    expect(result.stopReason).toBe('end_turn')
    expect(result.answer).toMatch(/^lord: error: [^\n]+$/)
    expect(chunks.filter(x => x.kind === 'text').length).toBe(1)
    expect(w.modelCalls()).toBe(0)
  })
}

test('ordinary agents retain their own model step', async ($, on) => {
  const w = world(on)
  await spawn($, 'general-purpose')
  expect((await step($, 'hello')).result.answer).toBe('MODEL CALLED')
  expect(w.modelCalls()).toBe(1)
})

test('the slash model override applies without polling, and no status line is set', async ($, on) => {
  const w = world(on)
  await $.session.start({ cwd: '/repo' })
  // Lord sets no status line.
  expect(w.statuses).toEqual([])
  await spawn($)
  await step($, 'hello')
  const changed = await $.command.run({ command: 'lord', args: 'model a1 chosen-model' })
  expect(changed.text).toMatch(/chosen-model/)
  await step($, 'again')
  expect(w.requests.filter(x => x.path === '/turn').at(-1)?.body.model).toBe('chosen-model')
  const listed = await $.command.run({ command: 'lord', args: '' })
  expect(listed.text).toMatch(/a1/)
  const count = w.requests.length
  await w.clock.advance(60000)
  expect(w.requests.length).toBe(count)
})

test('an abandoned vendor turn is interrupted from its completion dispatch', async ($, on) => {
  const w = world(on, [{ type: 'tool', callId: 'pending1', name: 'SendMessage', input: { to: 'other', message: 'hi', summary: 'hi' } }])
  await spawn($)
  await step($, 'pending')
  await $.turn.complete({ agentId: 'a1', turnId: 't1', answer: '', durationMs: 1, isAborted: true, reason: 'aborted' })
  expect(w.requests.filter(x => x.path === '/interrupt').map(x => x.body)).toEqual([{ agentId: 'a1' }])
})

test('a failed connection starts the plugin daemon and retries', async ($, on) => {
  const w = world(on, [{ type: 'done', text: 'connected' }], 2, null)
  await spawn($)
  const response = step($, 'hello')
  await w.clock.advance(1000)
  expect((await response).result.answer).toBe('connected')
  expect(w.processes.length).toBe(1)
  expect(w.processes[0][0]).toBe('/opt/homebrew/bin/python3')
  expect(w.processes[0][1]).toMatch(/\/lordd\.py$/)
  expect(w.processes[0][2]).toBe('--daemon')
  expect(w.modelCalls()).toBe(0)
})

test('a missing daemon gives one visible error after five seconds', async ($, on) => {
  const w = world(on, [], 1000, null)
  await spawn($)
  const response = step($, 'hello')
  await w.clock.advance(5100)
  const { result, chunks } = await response
  expect(result.answer).toMatch(/^lord: error: /)
  expect(chunks.filter(x => x.kind === 'text').length).toBe(1)
  expect(w.processes.length).toBe(1)
  expect(w.modelCalls()).toBe(0)
})

test('SendMessage steers a running vendor and queues a message for an idle one', async ($, on) => {
  const w = world(on, [{ type: 'tool', callId: 'call1', name: 'SendMessage', input: { to: 'other', message: 'hi', summary: 'hi' } }])
  await spawn($)
  await step($, 'running')
  expect(await $.session.send({ to: 'a1', text: 'change course', origin: { kind: 'model' } })).toEqual({ isDelivered: true })
  expect(w.requests.find(x => x.path === '/steer')?.body).toEqual({ agentId: 'a1', text: 'change course' })
  expect(w.deliveries).toEqual([])
  await $.turn.complete({ agentId: 'a1', turnId: 't1', answer: '', durationMs: 1, isAborted: true, reason: 'aborted' })
  await $.session.send({ to: 'a1', text: 'next turn', origin: { kind: 'model' } })
  expect(w.deliveries).toEqual(['next turn'])
})

test('directives in a live message are removed before steering and select the next model', async ($, on) => {
  const w = world(on, [{ type: 'tool', callId: 'call1', name: 'SendMessage', input: { to: 'other', message: 'hi', summary: 'hi' } }])
  await spawn($)
  await step($, 'running')
  await $.session.send({ to: 'a1', text: '@model later\n@readonly\n@net\nchange course', origin: { kind: 'model' } })
  expect(w.requests.find(x => x.path === '/steer')?.body).toEqual({ agentId: 'a1', text: 'change course' })
  expect(w.deliveries).toEqual([])
  await step($, 'next task')
  expect(w.requests.filter(x => x.path === '/turn')[1].body.model).toBe('later')
})

test('a Workflow schema step gets the vendor answer as a StructuredOutput call and then ends', async ($, on) => {
  const w = world(on, [{ type: 'done', text: 'Here: {"word":"hi"}' }])
  await spawn($)
  await step($, 'task')
  const { result, chunks } = await step($, '', 1)
  expect(result.toolUses).toEqual([{ name: 'StructuredOutput', input: { word: 'hi' } }])
  expect(chunks.find(x => x.kind === 'tool')?.name).toBe('StructuredOutput')
  expect(w.requests.some(x => x.path === '/continue')).toBe(false)
  await $.tool.call({ tool: 'StructuredOutput', tool_use_id: 'lord_structured_t1_1', agentId: 'a1', word: 'hi' })
  const before = w.requests.length
  const last = await step($, '', 2)
  expect(last.result.stopReason).toBe('end_turn')
  expect(w.requests.slice(before).filter(x => x.path === '/turn' || x.path === '/continue')).toEqual([])
  expect(w.modelCalls()).toBe(0)
})

test('a prose answer makes the plugin ask the vendor for JSON, and a second prose answer ends visibly', async ($, on) => {
  const w = world(on, [{ type: 'done', text: 'just prose' }])
  await spawn($)
  await step($, 'task')
  w.requests.length = 0
  const { result } = await step($, '', 1)
  expect(w.requests.filter(x => x.path === '/turn').map(x => x.body.text)).toEqual(['Reply with only a JSON object holding your answer, with no other text.'])
  expect(result.stopReason).toBe('end_turn')
  expect(result.answer).toMatch(/lord: error: the vendor returned no JSON object/)
  expect(w.modelCalls()).toBe(0)
})

test('an empty vendor answer ends with a visible error, not a silent success', async ($, on) => {
  world(on, [{ type: 'done', text: '  ' }])
  await spawn($)
  const { result } = await step($, 'task')
  expect(result.answer).toBe('lord: error: the vendor finished with an empty answer')
})

test('@dir lines collect extra writable folders for the vendor session', async ($, on) => {
  const w = world(on)
  await spawn($)
  await step($, '@dir /data/a\n@dir /data/b\nbuild it')
  expect(w.requests.find(x => x.path === '/open')?.body.dirs).toEqual(['/data/a', '/data/b'])
  expect(w.requests.find(x => x.path === '/turn')?.body.text).toBe('build it')
})

test('@cwd names the vendor working folder on the first turn', async ($, on) => {
  const w = world(on)
  await spawn($)
  await step($, '@cwd /pins/r1\nreview')
  expect(w.requests.find(x => x.path === '/open')?.body.cwd).toBe('/pins/r1')
  expect(w.requests.find(x => x.path === '/turn')?.body.text).toBe('review')
})

test('@memory, @hang and @tools reach the daemon as per-agent overrides', async ($, on) => {
  const w = world(on)
  await spawn($)
  await step($, '@memory 24\n@hang 3600\n@wall 7200\n@tools\nbuild')
  const open = w.requests.find(x => x.path === '/open')?.body
  expect([open.memory_gb, open.hang_s, open.wall_s, open.tools]).toEqual([24, 3600, 7200, true])
  expect(w.requests.find(x => x.path === '/turn')?.body.text).toBe('build')
})

test('the done text is the answer; streamed narration is not', async ($, on) => {
  world(on, [{ type: 'text', text: 'Let me look. ' }, { type: 'done', text: 'final answer' }])
  await spawn($)
  expect((await step($, 'task')).result.answer).toBe('final answer')
})

test('an empty final answer is an error even after streamed narration', async ($, on) => {
  world(on, [{ type: 'text', text: 'Let me look. ' }, { type: 'done', text: '' }])
  await spawn($)
  expect((await step($, 'task')).result.answer).toBe('lord: error: the vendor finished with an empty answer')
})

test('a different @cwd after the first turn is refused', async ($, on) => {
  const w = world(on)
  await spawn($)
  await step($, '@cwd /pins/r1\nfirst')
  expect((await step($, '@cwd /pins/r2\nsecond')).result.answer).toMatch(/^lord: error: open: @cwd is fixed at the first turn/)
  expect(w.requests.filter(x => x.path === '/turn').length).toBe(1)
})

test('worktree isolation without a reported worktree path refuses to run in the main tree', async ($, on) => {
  const w = world(on)
  await $.tool.call({ tool: 'Agent', tool_use_id: 'spawn1', subagent_type: 'lord:gpt', isolation: 'worktree', prompt: 'task', description: 'test' } as any)
  await spawn($)
  expect((await step($, 'build')).result.answer).toMatch(/^lord: error: worktree isolation was requested/)
  expect(w.requests.some(x => x.path === '/open')).toBe(false)
})

test('a Python that is missing or too old is skipped for the next one', async ($, on) => {
  const w = world(on, [{ type: 'done', text: 'connected' }], 2, null, { '/opt/homebrew/bin/python3': 3 })
  await spawn($)
  const response = step($, 'hello')
  await w.clock.advance(1000)
  expect((await response).result.answer).toBe('connected')
  expect(w.processes.map(p => p[0])).toEqual(['/opt/homebrew/bin/python3', '/usr/local/bin/python3'])
})

test('python3 on the session PATH is tried after Homebrew and python.org, once each', async ($, on) => {
  const exits = { '/opt/homebrew/bin/python3': 3, '/usr/local/bin/python3': 3 }
  const w = world(on, [{ type: 'done', text: 'connected' }], 2, null, exits, { PATH: '/home/u/.pyenv/shims:relative/bin:/opt/homebrew/bin/:/usr/bin' })
  await spawn($)
  const response = step($, 'hello')
  await w.clock.advance(1000)
  expect((await response).result.answer).toBe('connected')
  expect(w.processes.map(p => p[0])).toEqual(['/opt/homebrew/bin/python3', '/usr/local/bin/python3', '/home/u/.pyenv/shims/python3'])
})

test('/open carries the plugin version, so an older daemon hands over to this one', async ($, on) => {
  const w = world(on)
  on('fs.read', (_, e) => e.path.endsWith('/.claude-plugin/plugin.json') ? { value: JSON.stringify({ name: 'lord', version: '9.8.7' }) } : { deny: 'no such file' })
  await spawn($)
  expect((await step($, 'hello')).result.answer).toBe('fake answer')
  expect(w.requests.find(x => x.path === '/open')?.body.version).toBe('9.8.7')
})

test('@effort sets a vendor level beyond the Agent field and persists', async ($, on) => {
  const w = world(on)
  await spawn($)
  await step($, '@effort ultra\nbuild it')
  expect(w.requests.find(x => x.path === '/open')!.body.effort).toBe('ultra')
  const turn = w.requests.find(x => x.path === '/turn')!.body
  expect(turn.effort).toBe('ultra')
  expect(turn.text).toBe('build it')
})

test('a broken settings file is reported instead of a connection timeout', async ($, on) => {
  const w = world(on, [], 1000, null, { '/opt/homebrew/bin/python3': 2 })
  await spawn($)
  const { result } = await step($, 'hello')
  expect(result.answer).toMatch(/^lord: error: .*config\.json is not valid JSON/)
  expect(w.processes.length).toBe(1)
})

test('an option the daemon refuses is not kept for later turns', async ($, on) => {
  const w = world(on)
  await spawn($)
  expect((await step($, '@dir /bad\nfirst')).result.answer).toMatch(/every @dir must be an existing directory/)
  expect((await step($, 'second')).result.answer).toBe('fake answer')
  expect(w.requests.filter(x => x.path === '/open').at(-1)!.body.dirs).toBeUndefined()
})

test('/lord answers before the daemon has started', async ($, on) => {
  world(on, [], 0, '/down.sock')
  await $.session.start({ cwd: '/repo' } as any)
  expect(await $.command.run({ command: 'lord', args: '' } as any)).toMatchObject({ text: 'lord: no agents' })
})

test('after a reload the daemon keeps an agent\'s @effort level and folder', async ($, on) => {
  const w = world(on)
  await step($, 'continue the work')
  const open = w.requests.find(x => x.path === '/open')!.body
  expect([open.effort, open.cwd]).toEqual(['ultra', '/pinned'])
  expect(w.requests.find(x => x.path === '/turn')!.body.effort).toBe('ultra')
})

test('the first turn\'s @cwd stays the folder for later turns', async ($, on) => {
  const w = world(on)
  await spawn($)
  await $.classic.SubagentStart({ agent_id: 'a1', agent_type: 'lord:gpt', cwd: '/repo' } as any)
  await step($, '@cwd /pins/r1\nfirst')
  await step($, 'second')
  expect((await step($, '@cwd /pins/r1\nthird')).result.answer).toBe('fake answer')
  expect(w.requests.filter(x => x.path === '/open').map(x => x.body.cwd)).toEqual(['/pins/r1', '/pins/r1', '/pins/r1'])
})

test('@nosandbox reaches the daemon and stays on for later turns', async ($, on) => {
  const w = world(on)
  await spawn($)
  await step($, '@nosandbox\nbuild it')
  await step($, 'next')
  expect(w.requests.filter(x => x.path === '/open').map(x => x.body.nosandbox)).toEqual([true, true])
  expect(w.requests.find(x => x.path === '/turn')?.body.text).toBe('build it')
})

const CONFIGS = (path: string) => path === '/test/.claude/lord/config.json' ? { value: JSON.stringify({ grok: { model: 'grok-5' } }) }
  : path.endsWith('/config.json') ? { value: JSON.stringify({ gpt: { model: 'gpt-6.1-sol' }, grok: { model: 'grok-4.7' }, gemini: { model: 'gemini-4-argon' } }) }
  : { deny: 'no such file' }

test('each Lord agent is labelled with the vendor model and effort it will run, and runs as that model', async ($, on) => {
  const w = world(on)
  on('fs.read', (_, e) => CONFIGS(e.path))
  const launch = async (type: string, prompt: string, effort?: string) => {
    await $.tool.call({ tool: 'Agent', tool_use_id: 'L', subagent_type: type, prompt, description: 'Fix it', ...(effort ? { effort } : {}) } as any)
    await $.agent.spawn({ tool_use_id: 'L', prompt, description: 'Fix it', subagentType: type, provider: { plugin: 'lord', tier: 'user' }, parentModel: 'haiku', background: true, fork: false } as any)
    const input = w.spawnInputs.at(-1)
    return { description: input.description, model: input.model }
  }
  expect(await launch('lord:gpt', '@model gpt-6-astra\n@effort xhigh\nwork')).toEqual({ description: 'Astra, xhigh · Fix it', model: 'gpt-6-astra' })
  expect(await launch('lord:gpt', '@model gpt-6-sol\nwork')).toEqual({ description: 'Sol, high · Fix it', model: 'gpt-6-sol' })
  // The bundled default, with the Agent call's effort.
  expect(await launch('lord:gpt', 'work', 'max')).toEqual({ description: 'Sol 6.1, max · Fix it', model: 'gpt-6.1-sol' })
  // The user's config.json wins over the bundled one; grok runs a max request as xhigh, gemini as high.
  expect(await launch('lord:grok', 'work', 'max')).toEqual({ description: 'Grok 5, xhigh · Fix it', model: 'grok-5' })
  expect(await launch('lord:gemini', '@effort max\nwork')).toEqual({ description: 'Argon, high · Fix it', model: 'gemini-4-argon' })
  // The Agent call carries the level the vendor runs, so Claude Code shows it beside the model; ultra stays an @effort line.
  const calls = w.toolCalls
  await launch('lord:gpt', '@effort xhigh\nwork', 'high')
  expect(calls.at(-1).effort).toBe('xhigh')
  await launch('lord:grok', 'work', 'max')
  expect(calls.at(-1).effort).toBe('xhigh')
  expect(await launch('lord:gpt', '@effort ultra\nwork', 'max')).toEqual({ description: 'Sol 6.1, ultra · Fix it', model: 'gpt-6.1-sol' })
  expect(calls.at(-1).effort).toBe('max')
  // Claude's own agents are left alone.
  expect(await launch('general-purpose', 'work', 'max')).toEqual({ description: 'Fix it', model: undefined })
  expect(calls.at(-1).effort).toBe('max')
})

test('a newer installed Lord reloads plugins once no Lord turn is running, once per version', async ($, on) => {
  const w = world(on, [{ type: 'activity', text: 'working' }, { type: 'done', text: 'final answer' }])
  let installed = '9.0.1', root = ''
  on('fs.read', (_, e) => {
    if (e.path.endsWith('/.claude-plugin/plugin.json')) { root = e.path.replace('/.claude-plugin/plugin.json', ''); return { value: JSON.stringify({ name: 'lord', version: '9.0.0' }) } }
    if (e.path.endsWith('/installed_plugins.json')) return { value: JSON.stringify({ version: 2, plugins: { 'lord@x': [{ installPath: root.replace(/\/[^/]+$/, '') + '/' + installed, version: installed }] } }) }
    return { deny: 'no such file' }
  })
  await spawn($)
  await $.turn.start({ agentId: 'a1', turnId: 't1', text: 'hello' })
  const stream = $.turn.step({ agentId: 'a1', turnId: 't1', index: 0, model: 'haiku', messageCount: 1 })
  await stream.next()
  // The Lord turn is mid-stream: no reload.
  await $.turn.complete({ turnId: 'm1', answer: '', isAborted: false } as any)
  await w.clock.advance(1000)
  expect(w.commands).toEqual([])
  while (!(await stream.next()).done) {}
  await $.turn.complete({ turnId: 'm2', answer: '', isAborted: false } as any)
  await w.clock.advance(1000)
  expect(w.commands.map(c => [c.command, c.args])).toEqual([['reload-plugins', '--force']])
  expect(w.toasts).toContain('Lord Claude 9.0.1 installed; reloading plugins')
  // Once per version; an equal version never reloads.
  await $.turn.complete({ turnId: 'm3', answer: '', isAborted: false } as any)
  await w.clock.advance(1000)
  expect(w.commands.length).toBe(1)
})

test('an installed version equal to the loaded one does not reload', async ($, on) => {
  const w = world(on)
  let root = ''
  on('fs.read', (_, e) => {
    if (e.path.endsWith('/.claude-plugin/plugin.json')) { root = e.path.replace('/.claude-plugin/plugin.json', ''); return { value: JSON.stringify({ name: 'lord', version: '9.0.0' }) } }
    if (e.path.endsWith('/installed_plugins.json')) return { value: JSON.stringify({ plugins: { 'lord@x': [{ installPath: root.replace(/\/[^/]+$/, '') + '/9.0.0', version: '9.0.0' }] } }) }
    return { deny: 'no such file' }
  })
  await $.session.start({ cwd: '/repo' })
  await w.clock.advance(1000)
  expect(w.commands).toEqual([])
})

test('a named lord launch is refused, since it would start a Claude teammate instead of the vendor', async ($, on) => {
  const w = world(on)
  const named = await $.tool.call({ tool: 'Agent', tool_use_id: 'n1', subagent_type: 'lord:gpt', name: 'builder', prompt: 'task', description: 'test' } as any)
  expect(named.deny).toMatch(/^lord agents cannot be named/)
  const unnamed = await $.tool.call({ tool: 'Agent', tool_use_id: 'n2', subagent_type: 'lord:gpt', prompt: 'task', description: 'test' } as any)
  expect(unnamed.deny).toBeUndefined()
  // Claude's own named agents are left alone.
  expect((await $.tool.call({ tool: 'Agent', tool_use_id: 'n3', subagent_type: 'general-purpose', name: 'helper', prompt: 'task', description: 'test' } as any)).deny).toBeUndefined()
})

test('a resumed agent keeps the folder it opened in, not the session folder', async ($, on) => {
  const w = world(on)
  await spawn($)
  await $.classic.SubagentStart({ agent_id: 'a1', agent_type: 'lord:gpt', cwd: '/home' } as any)
  await step($, '@cwd /pins/r1\nfirst')
  // A message to an agent whose turn ended resumes it through agent.spawn, with the session's folder.
  await $.agent.spawn({ tool_use_id: 'resume1', prompt: 'continue', description: 'test', subagentType: 'lord:gpt', provider: { plugin: 'lord', tier: 'user' }, parentModel: 'haiku', background: true, fork: false, cwd: '/home', resume: { agentId: 'a1' } } as any)
  await step($, 'continue')
  expect(w.requests.filter(x => x.path === '/open').map(x => x.body.cwd)).toEqual(['/pins/r1', '/pins/r1'])
})

test('a rejected StructuredOutput object is corrected once, then ends with a visible error', async ($, on) => {
  const w = world(on, [{ type: 'done', text: '{"bad":true}' }])
  await spawn($)
  await step($, 'task')
  await step($, '', 1)
  w.tools.answer = { result: {}, isError: true, text: 'word is required', ref: 9 }
  await $.tool.call({ tool: 'StructuredOutput', tool_use_id: 'lord_structured_t1_1', agentId: 'a1', bad: true } as any)
  expect((await step($, '', 2)).result.toolUses[0].name).toBe('StructuredOutput')
  await $.tool.call({ tool: 'StructuredOutput', tool_use_id: 'lord_structured_t1_2', agentId: 'a1', bad: true } as any)
  const last = await step($, '', 3)
  expect(last.result.answer).toMatch(/^lord: error: the vendor's JSON was rejected again: word is required/)
  expect(w.requests.filter(x => x.path === '/turn' && x.body.text.startsWith('That JSON was rejected')).length).toBe(1)
})

test('a known option written wrongly ends the turn with an error instead of dropping later options', async ($, on) => {
  const w = world(on)
  await spawn($)
  const { result } = await step($, '@hang 600s\n@readonly\nreview the repo')
  expect(result.answer).toBe('lord: error: option line not understood: @hang 600s')
  expect(w.requests.some(x => x.path === '/open' || x.path === '/turn')).toBe(false)
})

test('a SubagentStart after a reload does not replace the folder the daemon keeps', async ($, on) => {
  const w = world(on)
  await $.classic.SubagentStart({ agent_id: 'a1', agent_type: 'lord:gpt', cwd: '/repo' } as any)
  await step($, 'continue')
  expect(w.requests.find(x => x.path === '/open')!.body.cwd).toBe('/pinned')
})

test('an agent restored while the daemon was down gets its settings once the daemon starts', async ($, on) => {
  const w = world(on, [{ type: 'done', text: 'connected' }], 2, null)
  const response = step($, 'continue')
  await w.clock.advance(1000)
  expect((await response).result.answer).toBe('connected')
  const open = w.requests.find(x => x.path === '/open')!.body
  expect([open.effort, open.cwd]).toEqual(['ultra', '/pinned'])
})

test('a relative @cwd is under the agent\'s start folder and the session PATH reaches the daemon', async ($, on) => {
  const w = world(on)
  await spawn($)
  await $.classic.SubagentStart({ agent_id: 'a1', agent_type: 'lord:gpt', cwd: '/repo' } as any)
  await step($, '@cwd sub/dir\nwork')
  const open = w.requests.find(x => x.path === '/open')!.body
  expect([open.cwd, open.pin_cwd]).toEqual(['/repo/sub/dir', true])
})

test('after a JSON correction that used a lord tool, an answer with no JSON ends the turn instead of asking again', async ($, on) => {
  const w = world(on, [{ type: 'done', text: '{"bad":true}' }])
  await spawn($)
  await step($, 'task')
  await step($, '', 1)
  w.tools.answer = { result: {}, isError: true, text: 'word is required', ref: 9 }
  await $.tool.call({ tool: 'StructuredOutput', tool_use_id: 'lord_structured_t1_1', agentId: 'a1', bad: true } as any)
  w.events.splice(0, w.events.length, { type: 'tool', callId: 'c9', name: 'SendMessage', input: { to: 'x', message: 'hi', summary: 'hi' } })
  expect((await step($, '', 2)).result.stopReason).toBe('tool_use')
  w.tools.answer = { result: {}, text: 'sent', ref: 9 }
  await $.tool.call({ tool: 'SendMessage', tool_use_id: 'c9', agentId: 'a1', to: 'x', message: 'hi', summary: 'hi' } as any)
  w.events.splice(0, w.events.length, { type: 'done', text: 'prose only' })
  await step($, '', 3)
  const before = w.requests.filter(x => x.path === '/turn').length
  const { result } = await step($, '', 4)
  expect(result.answer).toBe("lord: error: the vendor's corrected answer held no JSON object")
  expect(w.requests.filter(x => x.path === '/turn').length).toBe(before)
})

// the one-correction rule only counts a correction of REJECTED JSON; the "no JSON" correction never sets
// `corrected`, so each time the vendor answers it through a lord tool the engine's next StructuredOutput step asks again.
test('a no-JSON correction that used a lord tool is not asked again', async ($, on) => {
  const w = world(on, [{ type: 'done', text: 'just prose' }])
  await spawn($)
  await step($, 'task')
  w.events.splice(0, w.events.length, { type: 'tool', callId: 'c9', name: 'SendMessage', input: { to: 'x', message: 'hi', summary: 'hi' } })
  expect((await step($, '', 1)).result.stopReason).toBe('tool_use')
  w.tools.answer = { result: {}, text: 'sent', ref: 9 }
  await $.tool.call({ tool: 'SendMessage', tool_use_id: 'c9', agentId: 'a1', to: 'x', message: 'hi', summary: 'hi' } as any)
  w.events.splice(0, w.events.length, { type: 'done', text: 'still prose' })
  await step($, '', 2)
  const asks = () => w.requests.filter(x => x.path === '/turn' && x.body.text.startsWith('Reply with only a JSON object')).length
  const before = asks()
  await step($, '', 3)
  expect(asks()).toBe(before)
})

test('a relative @cwd does not replace a requested worktree that never arrived', async ($, on) => {
  const w = world(on)
  await $.tool.call({ tool: 'Agent', tool_use_id: 'spawn1', subagent_type: 'lord:gpt', isolation: 'worktree', prompt: 'task', description: 'test' } as any)
  await spawn($)
  const { result } = await step($, '@cwd sub\nwork')
  expect(result.answer).toMatch(/worktree isolation was requested/)
  expect(w.requests.some(x => x.path === '/open')).toBe(false)
})

test('/lord model with a model the daemon refuses is not kept for later turns', async ($, on) => {
  const w = world(on)
  await spawn($)
  await step($, 'first')
  await $.command.run({ command: 'lord', args: 'model a1 /bad-model' })
  expect((await step($, 'second')).result.answer).toMatch(/unknown gpt model \/bad-model/)
  w.requests.length = 0
  expect((await step($, 'third')).result.answer).toBe('fake answer')
  expect(w.requests.find(x => x.path === '/turn')!.body.model).toBeUndefined()
})

test('a turn in flight when this code is reloaded continues from the record the earlier copy kept', async ($, on) => {
  const w = world(on, [{ type: 'done', text: 'after the tool' }])
  const state = sessionState(on)
  // The earlier copy yielded a tool call for a1 and kept its record; this copy has never seen a1.
  state.put('agent', 'a1', { family: 'gpt', opened: true, cwd: '/work', running: true, continuing: true, call: 'call7' })
  await $.tool.call({ tool: 'SendMessage', tool_use_id: 'call7', agentId: 'a1', to: 'b', message: 'hi' } as any)
  expect(w.requests.find(x => x.path === '/tool_result')?.body).toEqual({ agentId: 'a1', callId: 'call7', text: 'child result', isError: false })
  const stream = $.turn.step({ agentId: 'a1', turnId: 't1', index: 1, model: 'haiku', messageCount: 3 })
  let item
  while (!(item = await stream.next()).done) {}
  expect(w.requests.filter(x => x.path === '/turn' || x.path === '/continue').map(x => x.path)).toEqual(['/continue'])
  expect(item.value.answer).toBe('after the tool')
  expect(state.get('agent', 'a1')).toMatchObject({ running: false, continuing: false })
})

test('a turn caught by a reload onto a newer version keeps talking to the daemon that opened it', async ($, on) => {
  const w = world(on, [{ type: 'done', text: 'after the tool' }])
  const state = sessionState(on)
  state.put('agent', 'a1', { family: 'gpt', opened: true, cwd: '/work', running: true, continuing: true, call: 'call7', daemon: '/test/.claude/lord/lordd-2.0.17.sock' })
  await $.tool.call({ tool: 'SendMessage', tool_use_id: 'call7', agentId: 'a1', to: 'b', message: 'hi' } as any)
  const stream = $.turn.step({ agentId: 'a1', turnId: 't1', index: 1, model: 'haiku', messageCount: 3 })
  while (!(await stream.next()).done) {}
  expect(w.requests.filter(x => x.path === '/tool_result' || x.path === '/continue').map(x => x.socket)).toEqual(['/test/.claude/lord/lordd-2.0.17.sock', '/test/.claude/lord/lordd-2.0.17.sock'])
})

test('a turn opened before per-version daemons continues on the socket they shared', async ($, on) => {
  const w = world(on, [{ type: 'done', text: 'after the tool' }], 0, null)
  on('fs.read', (_, e) => e.path.endsWith('/.claude-plugin/plugin.json') ? { value: JSON.stringify({ name: 'lord', version: '9.0.0' }) } : { deny: 'absent' })
  const state = sessionState(on)
  state.put('agent', 'a1', { family: 'gpt', opened: true, cwd: '/work', running: true, continuing: true, call: 'call7' })
  await $.tool.call({ tool: 'SendMessage', tool_use_id: 'call7', agentId: 'a1', to: 'b', message: 'hi' } as any)
  const stream = $.turn.step({ agentId: 'a1', turnId: 't1', index: 1, model: 'haiku', messageCount: 3 })
  while (!(await stream.next()).done) {}
  expect(w.requests.filter(x => x.path === '/tool_result' || x.path === '/continue').map(x => x.socket)).toEqual(['/test/.claude/lord/lordd.sock', '/test/.claude/lord/lordd.sock'])
})

test('each plugin version talks to its own daemon socket', async ($, on) => {
  const w = world(on, undefined, 0, null)
  on('fs.read', (_, e) => e.path.endsWith('/.claude-plugin/plugin.json') ? { value: JSON.stringify({ name: 'lord', version: '9.0.0' }) } : { deny: 'absent' })
  await spawn($)
  await step($, 'hello')
  expect(w.requests.find(x => x.path === '/open')?.socket).toBe('/test/.claude/lord/lordd-9.0.0.sock')
  expect(w.requests.find(x => x.path === '/turn')?.socket).toBe('/test/.claude/lord/lordd-9.0.0.sock')
  expect(w.processes).toEqual([])
})

test('a first step after a reload of this code finds the task the earlier copy kept', async ($, on) => {
  const w = world(on)
  const state = sessionState(on)
  await spawn($)
  state.put('start', 't9', { text: 'kept task', model: 'gpt-6-astra' })
  const stream = $.turn.step({ agentId: 'a1', turnId: 't9', index: 0, model: 'haiku', messageCount: 1 })
  while (!(await stream.next()).done) {}
  expect(w.requests.find(x => x.path === '/turn')?.body).toMatchObject({ agentId: 'a1', text: 'kept task', model: 'gpt-6-astra' })
})

test('a record the earlier copy wrote after this copy read it replaces this copy\'s', async ($, on) => {
  const w = world(on)
  const state = sessionState(on)
  await spawn($)
  await step($, 'hello')
  // The earlier copy, still finishing a step, kept a1 as waiting on a tool call.
  state.put('agent', 'a1', { ...state.get('agent', 'a1'), running: true, continuing: true, call: 'call8' })
  await $.tool.call({ tool: 'Agent', tool_use_id: 'call8', agentId: 'a1', subagent_type: 'general-purpose', prompt: 'p', description: 'd' } as any)
  expect(w.requests.find(x => x.path === '/tool_result')?.body).toMatchObject({ agentId: 'a1', callId: 'call8' })
})

test('the reload waits while the main loop or any agent has a turn running', async ($, on) => {
  const w = world(on)
  let root = ''
  on('fs.read', (_, e) => {
    if (e.path.endsWith('/.claude-plugin/plugin.json')) { root = e.path.replace('/.claude-plugin/plugin.json', ''); return { value: JSON.stringify({ name: 'lord', version: '9.0.0' }) } }
    if (e.path.endsWith('/installed_plugins.json')) return { value: JSON.stringify({ version: 2, plugins: { 'lord@x': [{ installPath: root.replace(/\/[^/]+$/, '') + '/9.0.1', version: '9.0.1' }] } }) }
    return { deny: 'no such file' }
  })
  // A queued message starts the next main turn within the second the reload waits.
  await $.turn.complete({ turnId: 'm1', answer: '', isAborted: false } as any)
  await w.clock.advance(500)
  await $.turn.start({ turnId: 'm2', text: 'queued' } as any)
  await w.clock.advance(500)
  expect(w.commands).toEqual([])
  // A Claude agent's turn is still running.
  w.roster.push({ id: 'c1', type: 'general-purpose', status: 'running' })
  await $.turn.complete({ turnId: 'm2', answer: '', isAborted: false } as any)
  await w.clock.advance(1000)
  expect(w.commands).toEqual([])
  w.roster[0].status = 'completed'
  await $.turn.complete({ turnId: 'm3', answer: '', isAborted: false } as any)
  await w.clock.advance(1000)
  expect(w.commands.map(c => [c.command, c.args])).toEqual([['reload-plugins', '--force']])
})

test('a vendor silent on a run woken only by a notification keeps the agent\'s last answer', async ($, on) => {
  const w = world(on, [{ type: 'done', text: 'GPT-DONE' }])
  await spawn($)
  expect((await step($, 'task')).result.answer).toBe('GPT-DONE')
  w.events.splice(0, w.events.length, { type: 'done', text: '' })
  const notice = '[SYSTEM NOTIFICATION - NOT USER INPUT]\nThis is an automated background-task event.\n\n<task-notification>\n<task-id>c1</task-id>\n<status>completed</status>\n<result>PONG</result>\n</task-notification>'
  expect((await step($, notice)).result.answer).toBe('GPT-DONE')
  // A silent vendor on any other run is still an error.
  expect((await step($, 'another task')).result.answer).toBe('lord: error: the vendor finished with an empty answer')
})

// An agent's messages in auto permission mode, where only a SubagentHandback call delivers a report: the API form holds
// the reminders, the rows leave them out.
const REMINDER = '<system-reminder>\nYour final report is delivered through SubagentHandback: when your work is complete, call SubagentHandback({message: <your full report>}). The call ends your run, so make it your last step.\n</system-reminder>'
const ENFORCE = '[handback-send-enforce] Your report has not been delivered. Call SubagentHandback({message: <your full report>}) now; the call ends your run.'
function transcript(on: On, texts: string[]) {
  on('session.messages', (_, e: any) => ({ value: e.as === 'api' ? texts.map(text => ({ role: 'user', content: [{ type: 'text', text }] }))
    : texts.filter(text => !text.startsWith('<system-reminder>') && !text.startsWith('[handback')).map(text => ({ role: 'user', text, toolUses: [] })) }) as any)
  return texts
}

test('in auto mode a finished answer is delivered through SubagentHandback, and the step after it ends quietly', async ($, on) => {
  const w = world(on)
  transcript(on, ['task', REMINDER])
  await spawn($)
  const { result, chunks } = await step($, 'task')
  expect(result).toMatchObject({ stopReason: 'tool_use', toolUses: [{ name: 'SubagentHandback', input: { message: 'fake answer' } }] })
  expect(chunks).toContainEqual({ kind: 'text', index: 0, text: 'fake answer' })
  expect(chunks.find(x => x.kind === 'tool')).toMatchObject({ name: 'SubagentHandback', index: 1 })
  await $.tool.call({ tool: 'SubagentHandback', tool_use_id: 'lord_handback_t1_0', agentId: 'a1', message: 'fake answer' } as any)
  const before = w.requests.length
  const last = await step($, '', 1)
  expect(last.result).toMatchObject({ stopReason: 'end_turn', answer: '', toolUses: [] })
  expect(w.requests.slice(before).filter(x => x.path === '/turn' || x.path === '/continue')).toEqual([])
  expect(w.modelCalls()).toBe(0)
})

test('a handback reminder delivers the last answer instead of asking the vendor for JSON', async ($, on) => {
  const w = world(on)
  const rows = transcript(on, ['task'])
  await spawn($)
  expect((await step($, 'task')).result.answer).toBe('fake answer')
  rows.push(REMINDER, ENFORCE)
  const before = w.requests.length
  const { result, chunks } = await step($, '', 1)
  expect(result.toolUses).toEqual([{ name: 'SubagentHandback', input: { message: 'fake answer' } }])
  expect(chunks.some(x => x.name === 'StructuredOutput')).toBe(false)
  expect(w.requests.slice(before).filter(x => x.path === '/turn' || x.path === '/continue')).toEqual([])
})

test('in auto mode a vendor error is delivered through SubagentHandback', async ($, on) => {
  world(on, [{ type: 'error', message: 'Individual quota reached. Resets in 104h27m0s.' }])
  transcript(on, ['task', REMINDER])
  await spawn($)
  const { result } = await step($, 'task')
  expect(result.toolUses).toEqual([{ name: 'SubagentHandback', input: { message: 'lord: error: Individual quota reached. Resets in 104h27m0s.' } }])
})

test('a delivery the engine refuses falls back to the report as plain text', async ($, on) => {
  const w = world(on)
  transcript(on, ['task', REMINDER])
  await spawn($)
  await step($, 'task')
  w.tools.answer = { result: {}, isError: true, text: 'Nothing was sent: SubagentHandback is not active for this agent.' }
  await $.tool.call({ tool: 'SubagentHandback', tool_use_id: 'lord_handback_t1_0', agentId: 'a1', message: 'fake answer' } as any)
  const { result, chunks } = await step($, '', 1)
  expect(result).toMatchObject({ stopReason: 'end_turn', answer: 'fake answer' })
  expect(chunks).toContainEqual({ kind: 'text', index: 0, text: 'fake answer' })
})

test('a long Lord transcript is shortened in place to the task and its newest steps, with no model call', async ($, on) => {
  world(on)
  let summarized = false
  on('session.compact', () => { summarized = true; return { messages: [] } as any })
  await spawn($)
  await step($, 'hello')
  const pair = (i: number) => [
    { role: 'assistant', text: '', toolUses: [{ tool_use_id: `r${i}`, tool: 'Bash', input: { command: `step ${i}` } }], handle: `a${i}` },
    { role: 'user', text: '', toolUses: [], toolResults: [{ tool_use_id: `r${i}`, text: 'y'.repeat(2000) }], handle: `u${i}` },
  ]
  const messages = [{ role: 'user', text: 'the task', toolUses: [], handle: 'task' }, ...Array.from({ length: 300 }, (_, i) => pair(i)).flat()]
  const result: any = await $.session.compact({ agentId: 'a1', trigger: 'auto', messages } as any)
  expect(summarized).toBe(false)
  expect(result.messages[0].handle).toBe('task')
  expect(result.messages[1].role).toBe('assistant')
  expect(result.messages.at(-1).handle).toBe('u299')
  expect(result.messages.length).toBeLessThan(messages.length)
  expect(JSON.stringify(result.messages).length).toBeLessThan(200_000)
})

test('a run Claude Code ended on its own error leaves the vendor to finish its turn', async ($, on) => {
  const w = world(on, [{ type: 'row', rowId: 'r1', shell: true, title: 'edit' }])
  await spawn($)
  await step($, 'work')
  await $.turn.complete({ agentId: 'a1', turnId: 't1', answer: '', durationMs: 1, isAborted: false, reason: 'error' } as any)
  expect(w.requests.filter(x => x.path === '/interrupt')).toEqual([])
})

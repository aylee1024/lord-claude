import { test, expect, mock } from 'claude-code/testing'
import type { On } from 'claude-code'

function world(on: On, events: unknown[] = [{ type: 'done', text: 'fake answer', model: 'fake-gpt', usage: { input_tokens: 3, output_tokens: 2, cache_read_input_tokens: 1 } }], failures = 0, socket: string | null = '/test/lord.sock', pythonExits: Record<string, number> = {}, env: Record<string, string> = {}) {
  const requests: { path: string; body: any }[] = []
  const statuses: string[] = []
  const processes: string[][] = []
  const deliveries: string[] = []
  let modelCalls = 0
  let opened: string | undefined
  let streamFinished = false
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
      requests.push({ path, body: JSON.parse(e.input ?? '{}') })
      const raw = events.map(x => JSON.stringify(x)).join('\n') + '\n'
      for (let i = 0; i < raw.length; i += 11) yield { stream: 'stdout', text: raw.slice(i, i + 11) }
      streamFinished = true
    }
    return { value: { code: 0, signal: null } }
  })
  // Like the real daemon, an agent spawned in this session is listed only after its /open; without a spawn the
  // test stands for a module reload and the daemon already holds the agent's settings.
  let fresh = false
  on('agent.spawn', (_, e) => { spawned = e.subagentType; fresh = true; return { agentId: 'a1', model: 'haiku' } })
  on('classic.SubagentStart', () => ({}))
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_, e) => ({ text: e.answer }))
  on('session.cwd', () => ({ value: '/repo' }))
  on('ui.status', (_, e) => { statuses.push(e.text ?? ''); return { value: {} } })
  on('command.register', () => ({ value: { name: 'lord' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.send', (_, e) => { deliveries.push(e.text); return { isDelivered: true } })
  on('session.receive', (_, e) => ({ text: e.text }))
  on('agent.list', () => ({ value: [{ id: 'a1', type: spawned }] as any }))
  const tools = { answer: { result: {}, text: 'child result', ref: 9 } as any }
  on('tool.call', () => tools.answer)
  on('http.fetch', (_, e) => {
    const path = e.url.replace('http://lord', '')
    requests.push({ path, body: e.init?.body ? JSON.parse(e.init.body) : undefined })
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
      : path === '/usage' ? { chatgpt: { usedPercent: 47 } }
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
  return { events, tools, requests, statuses, processes, deliveries, clock, modelCalls: () => modelCalls, streamFinished: () => streamFinished }
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
  expect(w.requests.find(x => x.path === '/turn')?.body).toEqual({ agentId: 'a1', text: 'hello', effort: 'xhigh' })
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

test('streamed text is shown live and recorded once as one final text block', async ($, on) => {
  world(on, [{ type: 'text', text: 'hel' }, { type: 'text', text: 'lo ' }, { type: 'activity', text: 'cmd' },
    { type: 'text', text: 'wor' }, { type: 'done', text: 'hello world' }])
  await spawn($)
  const { result, chunks } = await step($, 'hello')
  expect(chunks.filter(x => x.kind === 'text')).toEqual([{ kind: 'text', index: 3, text: 'hello world' }])
  expect(chunks.filter(x => x.kind === 'thinking').map(x => [x.index, x.text])).toEqual([[0, 'hel'], [0, 'lo '], [1, 'cmd'], [2, 'wor']])
  expect(result.answer).toBe('hello world')
})

test('an error after partial output appears on its own line', async ($, on) => {
  world(on, [{ type: 'text', text: 'partial answer' }, { type: 'error', message: 'failed\nnow' }])
  await spawn($)
  const { result } = await step($, 'hello')
  expect(result.answer).toBe('lord: error: failed now')
})

test('first-line directives in a resumed SendMessage survive the coordinator envelope', async ($, on) => {
  const w = world(on)
  await spawn($)
  await step($, 'The coordinator sent a message while you were working:\n@model next-model\n@readonly\n@net\nSECOND\n\nAddress this before completing your current task.')
  expect(w.requests.find(x => x.path === '/turn')?.body).toEqual({ agentId: 'a1', text: 'SECOND', model: 'next-model', effort: 'xhigh' })
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

test('status refresh and slash model override apply without polling', async ($, on) => {
  const w = world(on)
  await $.session.start({ cwd: '/repo' })
  expect(w.statuses).toContain('1 gpt · ChatGPT 47%')
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

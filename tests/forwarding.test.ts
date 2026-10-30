import { test, expect, mock } from 'claude-code/testing'
import type { On } from 'claude-code'

function harness(on: On, family: string, failed = false) {
  const state = new Map<string, { value: any; version: number }>()
  state.set('agent/a', { value: { family, opened: true, cwd: '/repo', nosandbox: family === 'gpt' }, version: 1 })
  const calls: any[] = [], accepted: any[] = [], requests: string[] = []
  mock.env(on, { HOME: '/test', LORD_DAEMON_SOCKET: '/test/lord.sock' }); mock.clock(on)
  on('state.get', (_, e: any) => ({ value: state.get(`${e.key}/${e.id}`) ?? { value: undefined, version: 0 } }) as any)
  on('state.set', (_, e: any) => {
    const version = (state.get(`${e.key}/${e.id}`)?.version ?? 0) + 1
    state.set(`${e.key}/${e.id}`, { value: e.value, version }); return { value: { isSet: true, version } } as any
  })
  on('fs.read', (_, e) => e.path.endsWith('/.claude-plugin/plugin.json') ? { value: JSON.stringify({ version: '2.2.6' }) } : { deny: 'absent' })
  on('agent.list', () => ({ value: [] }))
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('session.messages', () => ({ value: [] }))
  on('session.cwd', () => ({ value: '/repo' }))
  on('tool.call', (_, e) => { calls.push(e); return { result: {}, text: 'PONG', isError: failed } as any })
  on('http.fetch', (_, e) => {
    const path = e.url.replace('http://lord', '')
    if (path === '/tool_result') accepted.push(JSON.parse(e.init!.body!))
    return { value: { ok: true, status: 200, headers: {}, text: JSON.stringify(path === '/agents' ? [] : { ok: true, ...(path === '/health' ? { version: '2.2.6' } : {}) }) } }
  })
  on('turn.step', async function* () { throw new Error('Lord fell through to a model') })
  return { calls, accepted, requests }
}

async function step($: any, index: number) {
  const stream = $.turn.step({ agentId: 'a', turnId: 'turn', index, model: 'haiku', effort: 'high', messageCount: 1 })
  while (true) { const piece = await stream.next(); if (piece.done) return piece.value }
}

const continued = 'Continued in the background while a Lord call runs.'

for (const family of ['gpt', 'gemini']) for (const name of ['Agent', 'SendMessage']) for (const failed of [false, true]) for (const shell of [false, true])
test(`${family} draws a live ${shell ? 'Bash' : 'step'} row then forwards ${name} and carries its ${failed ? 'error' : 'result'}`, async ($, on) => {
  const { calls, accepted, requests } = harness(on, family, failed)
  const input = name === 'Agent' ? { subagent_type: 'general-purpose', prompt: 'ping', description: 'probe', run_in_background: false }
    : { to: 'peer', message: 'ping', summary: 'ping' }
  let continues = 0, finished = false, rowReads = 0
  on('process.spawn', async function* (_, e) {
    const path = e.argv[e.argv.length - 1].replace('http://lord', '')
    requests.push(path)
    let event: any
    if (path === '/turn') event = { type: 'row', rowId: 'open', shell, title: 'vendor work' }
    else if (path === '/row') {
      rowReads++
      expect(finished).toBe(false)
      event = { tool: { callId: 'forward', name, input } }
    } else if (shell && ++continues === 1) event = { type: 'row', rowId: 'lord', shell: false, title: 'tool lord request' }
    else if (!shell && ++continues === 1 || shell && continues === 2) event = { type: 'tool', callId: 'forward', name, input }
    else { finished = true; event = { type: 'done', text: `vendor received ${accepted[0]?.text ?? 'no result'}` } }
    yield { stream: 'stdout', text: JSON.stringify(event) + '\n' }
    return { value: { code: 0, signal: null } }
  })
  await $.turn.start({ agentId: 'a', turnId: 'turn', text: 'work' })
  expect((await step($, 0)).toolUses[0].name).toBe(shell ? 'Bash' : `mcp__lord__${family}`)
  expect(requests).toEqual(['/turn'])
  expect(finished).toBe(false)
  async function row(id: string, bash: boolean) {
    const value: any = await $.tool.call({ tool: bash ? 'Bash' : `mcp__lord__${family}`, tool_use_id: id, agentId: 'a',
      ...(bash ? { command: 'vendor work' } : { step: 'tool lord request' }) } as any)
    expect(value.result).toEqual(bash ? { stdout: continued, stderr: '', interrupted: false } : continued)
    expect(finished).toBe(false)
  }
  await row('open', shell)
  let index = 1
  if (shell) {
    expect((await step($, index++)).toolUses[0].name).toBe(`mcp__lord__${family}`)
    await row('lord', false)
  }
  expect((await step($, index++)).toolUses[0]).toEqual({ name, input })
  await $.tool.call({ tool: name, tool_use_id: 'forward', agentId: 'a', ...input } as any)
  expect(calls).toHaveLength(1)
  expect(calls[0]).toMatchObject({ tool: name, tool_use_id: 'forward', agentId: 'a', ...input })
  expect(accepted).toEqual([{ agentId: 'a', callId: 'forward', text: 'PONG', isError: failed }])
  expect((await step($, index)).answer).toBe('vendor received PONG')
  expect(finished).toBe(true)
  expect(rowReads).toBe(shell ? 2 : 1)
})

for (const family of ['gpt', 'gemini']) for (const shell of [false, true])
test(`${family} draws an ordinary ${shell ? 'Bash' : 'step'} row live and waits for its real output`, async ($, on) => {
  const { calls, requests } = harness(on, family)
  let drawn = false, settled = false, entered!: () => void, release!: () => void
  const waiting = new Promise<void>(resolve => { entered = resolve })
  const finish = new Promise<void>(resolve => { release = resolve })
  on('process.spawn', async function* (_, e) {
    const path = e.argv[e.argv.length - 1].replace('http://lord', '')
    requests.push(path)
    let event: any
    if (path === '/turn') event = { type: 'row', rowId: 'ordinary', shell, title: 'vendor work' }
    else if (path === '/row') {
      entered()
      if (!drawn) throw new Error('row waited before it was drawn')
      await finish
      event = { output: 'real vendor output', stopped: false }
    } else event = { type: 'done', text: 'PONG' }
    yield { stream: 'stdout', text: JSON.stringify(event) + '\n' }
    return { value: { code: 0, signal: null } }
  })
  try {
    await $.turn.start({ agentId: 'a', turnId: 'turn', text: 'work' })
    expect((await step($, 0)).toolUses[0].name).toBe(shell ? 'Bash' : `mcp__lord__${family}`)
    expect(requests).toEqual(['/turn'])
    drawn = true
    const pending = $.tool.call({ tool: shell ? 'Bash' : `mcp__lord__${family}`, tool_use_id: 'ordinary', agentId: 'a',
      ...(shell ? { command: 'vendor work' } : { step: 'vendor work' }) } as any).then(value => { settled = true; return value })
    await waiting
    expect(settled).toBe(false)
    release()
    const value: any = await pending
    expect(value.result).toEqual(shell ? { stdout: 'real vendor output', stderr: '', interrupted: false } : 'real vendor output')
    expect(requests.filter(path => path === '/row')).toHaveLength(1)
    expect(calls).toHaveLength(0)
    expect((await step($, 1)).answer).toBe('PONG')
  } finally { release() }
})

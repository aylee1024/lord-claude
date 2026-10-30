import { test, expect, mock } from 'claude-code/testing'

for (const family of ['gpt', 'gemini']) for (const tool of ['Agent', 'SendMessage']) for (const daemon of ['2.2.5', '2.2.7', 'unknown', 'offline', '2.2.6'])
test(`${family} ${tool} restored after reload checks daemon ${daemon}`, async ($, on) => {
  const state = new Map<string, { value: any; version: number }>()
  state.set('agent/a', { value: { family, opened: true, daemon: '/test/old.sock', call: 'pending' }, version: 1 })
  const calls: any[] = [], requests: any[] = []
  mock.env(on, { HOME: '/test', LORD_DAEMON_SOCKET: '/test/current.sock' }); mock.clock(on)
  on('state.get', (_, e: any) => ({ value: state.get(`${e.key}/${e.id}`) ?? { value: undefined, version: 0 } }) as any)
  on('state.set', (_, e: any) => {
    const version = (state.get(`${e.key}/${e.id}`)?.version ?? 0) + 1
    state.set(`${e.key}/${e.id}`, { value: e.value, version }); return { value: { isSet: true, version } } as any
  })
  on('fs.read', (_, e) => e.path.endsWith('/.claude-plugin/plugin.json') ? { value: JSON.stringify({ version: '2.2.6' }) } : { deny: 'absent' })
  on('agent.list', () => ({ value: [] }))
  on('session.cwd', () => ({ value: '/repo' }))
  on('session.messages', () => ({ value: [] }))
  on('http.fetch', (_, e) => {
    const path = e.url.replace('http://lord', '')
    requests.push({ path, body: e.init?.body ? JSON.parse(e.init.body) : undefined, socket: e.init?.socketPath })
    const value = path === '/agents' ? [] : path === '/health' ? { ok: true, ...(daemon === 'unknown' ? {} : { version: daemon }) } : { ok: true }
    return { value: { ok: path !== '/health' || daemon !== 'offline', status: daemon === 'offline' ? 503 : 200, headers: {}, text: JSON.stringify(value) } }
  })
  on('tool.call', (_, e) => { calls.push(e); return { result: {}, text: 'PONG', isError: false } as any })
  const input = tool === 'Agent' ? { subagent_type: 'lord:gpt', prompt: '@nosandbox\nwork', description: 'probe' }
    : { to: 'peer', message: '@dir /repo\nwork', summary: 'probe' }
  const result: any = await $.tool.call({ tool, tool_use_id: 'pending', agentId: 'a', ...input } as any)
  const accepted = requests.find(r => r.path === '/tool_result')
  expect(accepted.socket).toBe('/test/old.sock')
  expect(accepted.body.agentId).toBe('a')
  expect(accepted.body.callId).toBe('pending')
  if (daemon === '2.2.6') {
    expect(calls).toHaveLength(1)
    expect(!!result.isError).toBe(false)
    expect(accepted.body).toMatchObject({ text: 'PONG', isError: false })
  } else {
    expect(calls).toHaveLength(0)
    expect(result).toEqual({ deny: accepted.body.text })
    expect(accepted.body.isError).toBe(true)
    expect(accepted.body.text).toContain('daemon')
    expect(accepted.body.text).toContain('2.2.6')
    if (!['unknown', 'offline'].includes(daemon)) expect(accepted.body.text).toContain(daemon)
    expect(requests.some(r => r.path === '/open' || r.path === '/steer' || r.path === '/queue')).toBe(false)
  }
  expect(state.get('agent/a')?.value.call).toBeUndefined()
})

for (const family of ['gpt', 'gemini']) for (const tool of ['Agent', 'SendMessage'])
test(`${family} continues its vendor turn after a documented ${tool} refusal`, async ($, on) => {
  const state = new Map<string, { value: any; version: number }>()
  state.set('agent/a', { value: { family, opened: true, continuing: true, daemon: '/test/old.sock', call: 'pending' }, version: 1 })
  const accepted: any[] = [], calls: any[] = [], streams: any[] = []
  mock.env(on, { HOME: '/test', LORD_DAEMON_SOCKET: '/test/current.sock' }); mock.clock(on)
  on('state.get', (_, e: any) => ({ value: state.get(`${e.key}/${e.id}`) ?? { value: undefined, version: 0 } }) as any)
  on('state.set', (_, e: any) => {
    const version = (state.get(`${e.key}/${e.id}`)?.version ?? 0) + 1
    state.set(`${e.key}/${e.id}`, { value: e.value, version }); return { value: { isSet: true, version } } as any
  })
  on('fs.read', (_, e) => e.path.endsWith('/.claude-plugin/plugin.json') ? { value: '{"version":"2.2.6"}' } : { deny: 'absent' })
  on('agent.list', () => ({ value: [] }))
  on('session.messages', () => ({ value: [] }))
  on('session.cwd', () => ({ value: '/repo' }))
  on('http.fetch', (_, e) => {
    const path = e.url.replace('http://lord', '')
    if (path === '/tool_result') {
      expect(e.init?.socketPath).toBe('/test/old.sock')
      accepted.push(JSON.parse(e.init!.body!))
    }
    return { value: { ok: true, status: 200, headers: {}, text: JSON.stringify(path === '/agents' ? [] : { ok: true, version: '2.2.5' }) } }
  })
  on('tool.call', (_, e) => { calls.push(e); return { result: {} } as any })
  on('turn.step', async function* () { throw new Error('Lord fell through to a model') })
  on('process.spawn', async function* (_, e) {
    streams.push(e)
    expect(e.argv[e.argv.length - 1]).toBe('http://lord/continue')
    expect(e.argv).toContain('/test/old.sock')
    expect(accepted).toHaveLength(1)
    expect(accepted[0].isError).toBe(true)
    yield { stream: 'stdout', text: JSON.stringify({ type: 'done', text: `vendor received ${accepted[0].text}` }) + '\n' }
    return { value: { code: 0, signal: null } }
  })
  const input = tool === 'Agent' ? { subagent_type: 'lord:gpt', prompt: '@nosandbox\nwork', description: 'probe' }
    : { to: 'peer', message: 'work', summary: 'probe' }
  const refused = await $.tool.call({ tool, tool_use_id: 'pending', agentId: 'a', ...input } as any)
  expect(calls).toHaveLength(0)
  expect(accepted).toHaveLength(1)
  expect(accepted[0]).toMatchObject({ agentId: 'a', callId: 'pending', isError: true })
  expect(refused).toEqual({ deny: accepted[0].text })
  expect(accepted[0].text).toContain('daemon 2.2.5')
  expect(state.get('agent/a')?.value.call).toBeUndefined()
  const stream = $.turn.step({ agentId: 'a', turnId: 'turn', index: 1, model: 'haiku', effort: 'high', messageCount: 1 })
  let answer: any
  while (true) { const piece = await stream.next(); if (piece.done) { answer = piece.value; break } }
  expect(answer.answer).toBe(`vendor received ${accepted[0].text}`)
  expect(answer.stopReason).toBe('end_turn')
  expect(streams).toHaveLength(1)
  expect(state.get('agent/a')?.value.running).toBe(false)
})

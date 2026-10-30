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
    expect(result.isError).toBe(true)
    expect(accepted.body.isError).toBe(true)
    expect(accepted.body.text).toContain('daemon')
    expect(accepted.body.text).toContain('2.2.6')
    if (!['unknown', 'offline'].includes(daemon)) expect(accepted.body.text).toContain(daemon)
    expect(requests.some(r => r.path === '/open' || r.path === '/steer' || r.path === '/queue')).toBe(false)
  }
  expect(state.get('agent/a')?.value.call).toBeUndefined()
})

import { test, expect, mock } from 'claude-code/testing'

test('r6 a failed first open preserves its ticket, daemon and folders for retry', async ($, on) => {
  const state = new Map<string, { value: any; version: number }>()
  const requests: any[] = [], spawns: any[] = []
  let failed = false
  mock.env(on, { HOME: '/test', LORD_DAEMON_SOCKET: '/test/new.sock' }); mock.clock(on)
  on('state.get', (_, e: any) => ({ value: state.get(`${e.key}/${e.id}`) ?? { value: undefined, version: 0 } }) as any)
  on('state.set', (_, e: any) => {
    const version = (state.get(`${e.key}/${e.id}`)?.version ?? 0) + 1
    state.set(`${e.key}/${e.id}`, { value: e.value, version }); return { value: { isSet: true, version } } as any
  })
  on('fs.read', () => ({ deny: 'absent' }))
  on('agent.list', () => ({ value: [] }))
  on('agent.spawn', (_, e) => { spawns.push(e); return { agentId: 'child', model: 'haiku' } })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('session.cwd', () => ({ value: '/tmp/other' }))
  on('session.messages', () => ({ value: [] }))
  on('http.fetch', (_, e) => {
    const path = e.url.replace('http://lord', '')
    const body = e.init?.body ? JSON.parse(e.init.body) : undefined
    requests.push({ path, body, socket: e.init?.socketPath })
    if (path === '/open' && !failed) {
      failed = true
      return { value: { ok: false, status: 503, headers: {}, text: JSON.stringify({ error: 'temporary start failure' }) } }
    }
    return { value: { ok: true, status: 200, headers: {}, text: JSON.stringify(path === '/agents' ? [] : { ok: true }) } }
  })
  on('process.spawn', async function* () {
    yield { stream: 'stdout', text: JSON.stringify({ type: 'done', text: 'done' }) + '\n' }
    return { value: { code: 0, signal: null } }
  })
  const id = 'a'.repeat(32)
  state.set('agent/sender', { value: { family: 'gpt', call: id, daemon: '/test/old.sock' }, version: 1 })
  await $.agent.spawn({ tool_use_id: id, parentAgentId: 'sender', prompt: '@cwd /tmp/first\n@dir /tmp/extra\nwork', description: 'probe', subagentType: 'lord:gpt' } as any)
  await $.turn.start({ agentId: 'child', turnId: 'first', text: spawns[0].prompt })
  let run = $.turn.step({ agentId: 'child', turnId: 'first', index: 0, model: 'haiku', effort: 'high', messageCount: 1 })
  while (!(await run.next()).done) {}
  await $.turn.start({ agentId: 'child', turnId: 'retry', text: 'retry' })
  run = $.turn.step({ agentId: 'child', turnId: 'retry', index: 0, model: 'haiku', effort: 'high', messageCount: 1 })
  while (!(await run.next()).done) {}
  const opened = requests.filter(r => r.path === '/open')
  expect(opened).toHaveLength(2)
  for (const request of opened) {
    expect(request.body.launch).toBe(id)
    expect(request.body.cwd).toBe('/tmp/first')
    expect(request.body.dirs).toEqual(['/tmp/extra'])
    expect(request.socket).toBe('/test/old.sock')
  }
  expect(state.get(`launch/${id}`)?.value).toBeNull()
})

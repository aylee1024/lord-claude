import { test, expect, mock } from 'claude-code/testing'
import type { On } from 'claude-code'

function world(on: On) {
  const requests: { path: string; body: any; socket?: string }[] = []
  const state = new Map<string, { value: any; version: number }>()
  mock.env(on, { HOME: '/test', LORD_DAEMON_SOCKET: '/test/lord.sock' })
  mock.clock(on)
  on('state.get', (_, e: any) => ({ value: state.get(`${e.key}/${e.id}`) ?? { value: undefined, version: 0 } }) as any)
  on('state.set', (_, e: any) => {
    const key = `${e.key}/${e.id}`, version = (state.get(key)?.version ?? 0) + 1
    state.set(key, { value: e.value, version }); return { value: { isSet: true, version } } as any
  })
  on('fs.read', () => ({ deny: 'absent' }))
  on('session.end', (_, e) => ({ sessionId: e.sessionId }))
  on('session.send', () => ({ isDelivered: true }))
  on('agent.spawn', (_, e) => ({ agentId: e.tool_use_id, model: 'haiku' }))
  on('agent.list', () => ({ value: [] }))
  on('tool.call', () => ({ result: {} }))
  on('http.fetch', (_, e) => {
    const path = e.url.replace('http://lord', ''), body = e.init?.body ? JSON.parse(e.init.body) : undefined
    requests.push({ path, body, socket: e.init?.socketPath })
    return { value: { ok: true, status: 200, headers: {}, text: JSON.stringify(path === '/agents' ? [{ agentId: 'peer', family: 'gpt', state: 'idle', cwd: '/tmp/x', model: 'fake' }] : { ok: true }) } }
  })
  return { requests, state }
}

for (const action of ['lookup', 'launch', 'message', 'tool message']) test(`r4 quit owns ${action} agents precisely`, async ($, on) => {
  const w = world(on)
  await $.command.run({ command: 'lord', args: 'model peer other-model' })
  if (action === 'launch') await $.agent.spawn({ subagentType: 'lord:gpt', tool_use_id: 'own', prompt: 'work' })
  if (action === 'message') await $.session.send({ to: 'peer', text: 'continue' })
  if (action === 'tool message') await $.tool.call({ tool: 'SendMessage', tool_use_id: 'send', to: 'peer', message: 'continue', summary: 'probe' } as any)
  await $.session.end({ sessionId: 'current', reason: 'prompt_input_exit' })
  const ends = w.requests.filter(r => r.path === '/session_end')
  const ids = ends.flatMap(r => r.body.agentIds ?? [])
  expect(ids).toEqual(action === 'lookup' ? [] : action === 'launch' ? ['own'] : ['peer'])
  expect(ends.every(r => r.body.session === undefined)).toBe(true)
})

test('r4 model staging after reload preserves existing cleanup ownership', async ($, on) => {
  const w = world(on)
  w.state.set('agent/peer', { value: { family: 'gpt', owned: true, daemon: '/test/lord.sock' }, version: 1 })
  await $.command.run({ command: 'lord', args: 'model peer other-model' })
  await $.session.end({ sessionId: 'current', reason: 'prompt_input_exit' })
  expect(w.requests.filter(r => r.path === '/session_end').flatMap(r => r.body.agentIds ?? [])).toEqual(['peer'])
})

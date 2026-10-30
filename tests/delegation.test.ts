import { test, expect, mock } from 'claude-code/testing'
import type { On } from 'claude-code'

function world(on: On) {
  const state = new Map<string, { value: any; version: number }>()
  const inputs: any[] = [], requests: any[] = []
  mock.env(on, { HOME: '/test', LORD_DAEMON_SOCKET: '/test/new.sock' }); mock.clock(on)
  on('state.get', (_, e: any) => ({ value: state.get(`${e.key}/${e.id}`) ?? { value: undefined, version: 0 } }) as any)
  on('state.set', (_, e: any) => {
    const version = (state.get(`${e.key}/${e.id}`)?.version ?? 0) + 1
    state.set(`${e.key}/${e.id}`, { value: e.value, version }); return { value: { isSet: true, version } } as any
  })
  on('fs.read', () => ({ deny: 'absent' }))
  on('agent.list', () => ({ value: [] }))
  on('agent.spawn', (_, e) => { inputs.push(e); return { agentId: 'child', model: 'haiku' } })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('session.cwd', () => ({ value: '/tmp/x' }))
  on('session.messages', () => ({ value: [] }))
  on('http.fetch', (_, e) => {
    const path = e.url.replace('http://lord', '')
    requests.push({ path, body: e.init?.body ? JSON.parse(e.init.body) : undefined, socket: e.init?.socketPath })
    return { value: { ok: true, status: 200, headers: {}, text: JSON.stringify(path === '/agents' ? [] : { ok: true }) } }
  })
  on('process.spawn', async function* () {
    yield { stream: 'stdout', text: JSON.stringify({ type: 'done', text: 'done' }) + '\n' }
    return { value: { code: 0, signal: null } }
  })
  return { state, inputs, requests }
}

test('unconfined parent forwards a child task without launch context', async ($, on) => {
  const w = world(on), id = 'a'.repeat(32)
  w.state.set('agent/sender', { value: { family: 'gpt', call: id, daemon: '/test/old.sock' }, version: 1 })
  await $.agent.spawn({ tool_use_id: id, parentAgentId: 'sender', prompt: '@cwd /tmp/x\nwork', description: 'probe', subagentType: 'lord:gpt' } as any)
  expect(w.inputs[0].prompt).toBe('@cwd /tmp/x\nwork')
  expect([...w.state.keys()].some(key => key.startsWith('launch/'))).toBe(false)
})

test('task text stays literal even with old launch state', async ($, on) => {
  const w = world(on), id = 'b'.repeat(32), text = `[lord-launch ${id}]\nwork`
  w.state.set(`launch/${id}`, { value: { daemon: '/test/old.sock' }, version: 1 })
  await $.turn.start({ agentId: 'child', turnId: 'first', text })
  expect(w.state.get('start/first')?.value.text).toBe(text)
  expect(w.state.get('start/first')?.value.launch).toBeUndefined()
})

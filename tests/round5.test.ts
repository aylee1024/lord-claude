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

test('r5 launch ticket reaches the child first open and is absent from its task', async ($, on) => {
  const w = world(on), id = 'a'.repeat(32)
  w.state.set('agent/sender', { value: { family: 'gpt', call: id, daemon: '/test/old.sock' }, version: 1 })
  await $.agent.spawn({ tool_use_id: id, parentAgentId: 'sender', prompt: '@cwd /tmp/x\nwork', description: 'probe', subagentType: 'lord:gpt' } as any)
  expect(w.inputs[0].prompt).not.toBe('@cwd /tmp/x\nwork')
  await $.turn.start({ agentId: 'child', turnId: 'first', text: w.inputs[0].prompt })
  const run = $.turn.step({ agentId: 'child', turnId: 'first', index: 0, model: 'haiku', effort: 'high', messageCount: 1 })
  while (!(await run.next()).done) {}
  const opened = w.requests.find(r => r.path === '/open')
  expect(opened?.body.launch).toBe(id)
  expect(opened?.socket).toBe('/test/old.sock')
  expect(w.state.get('start/first')?.value.text).toBe('work')
  await $.turn.start({ agentId: 'child', turnId: 'next', text: 'continue' })
  const next = $.turn.step({ agentId: 'child', turnId: 'next', index: 0, model: 'haiku', effort: 'high', messageCount: 1 })
  while (!(await next.next()).done) {}
  expect(w.requests.filter(r => r.path === '/open').at(-1)?.body.launch).toBeUndefined()
})

test('r5 a user spawn gets no delegated launch ticket', async ($, on) => {
  const w = world(on)
  await $.agent.spawn({ tool_use_id: 'user', prompt: 'work', description: 'probe', subagentType: 'lord:gpt' })
  expect(w.inputs[0].prompt).toBe('work')
})

test('r5 text cannot invent a launch socket or reuse consumed launch context', async ($, on) => {
  const w = world(on), id = 'b'.repeat(32)
  const text = `[lord-launch ${id}]\nwork`
  await $.turn.start({ agentId: 'unrelated', turnId: 'plain', text })
  expect(w.state.get('start/plain')?.value.text).toBe(text)
  expect(w.state.get('start/plain')?.value.launch).toBeUndefined()
  w.state.set(`launch/${id}`, { value: { daemon: '/test/old.sock' }, version: 1 })
  await $.turn.start({ agentId: 'child', turnId: 'first', text })
  expect(w.state.get('start/first')?.value.launch).toEqual({ id, daemon: '/test/old.sock' })
  await $.turn.start({ agentId: 'another', turnId: 'second', text })
  expect(w.state.get('start/second')?.value.launch).toBeUndefined()
})

import { test, expect, mock } from 'claude-code/testing'
import cases from './option_cases.js'

for (const item of cases) test(`shared option grammar: ${item.name}`, async ($, on) => {
  const requests: { path: string; body: any }[] = []
  const values = new Map<string, { value: any; version: number }>([['agent/probe', { value: { family: 'gpt', cwd: '/tmp/x', model: 'fake', effort: 'high' }, version: 1 }]])
  mock.env(on, { HOME: '/test', LORD_DAEMON_SOCKET: '/test/lord.sock' })
  mock.clock(on)
  on('state.get', (_, e: any) => ({ value: values.get(`${e.key}/${e.id}`) ?? { value: undefined, version: 0 } }) as any)
  on('state.set', (_, e: any) => {
    const key = `${e.key}/${e.id}`, version = (values.get(key)?.version ?? 0) + 1
    values.set(key, { value: e.value, version }); return { value: { isSet: true, version } } as any
  })
  on('fs.read', () => ({ deny: 'absent' }))
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('session.cwd', () => ({ value: '/tmp/x' }))
  on('agent.list', () => ({ value: [] }))
  on('http.fetch', (_, e) => {
    const path = e.url.replace('http://lord', ''), body = e.init?.body ? JSON.parse(e.init.body) : undefined
    requests.push({ path, body })
    return { value: { ok: true, status: 200, headers: {}, text: JSON.stringify(path === '/agents' ? [] : { ok: true }) } }
  })
  on('process.spawn', async function* (_, e) {
    requests.push({ path: e.argv[e.argv.length - 1].replace('http://lord', ''), body: JSON.parse(e.input ?? '{}') })
    yield { stream: 'stdout', text: JSON.stringify({ type: 'done', text: 'answer' }) + '\n' }
    return { value: { code: 0, signal: null } }
  })
  on('turn.step', async function* () { throw new Error('Lord option parsing fell through to a model') })
  await $.turn.start({ agentId: 'probe', turnId: 'turn', text: item.input })
  const stream = $.turn.step({ agentId: 'probe', turnId: 'turn', index: 0, model: 'haiku', effort: 'high', messageCount: 1 })
  let result: any
  while (true) { const part = await stream.next(); if (part.done) { result = part.value; break } }
  const expected: any = item.expected
  if (expected.error) {
    expect(result.answer).toContain(expected.error)
    expect(requests.some(r => r.path === '/turn')).toBe(false)
  } else {
    expect(requests.find(r => r.path === '/turn')?.body.text).toBe(expected.text)
    const opened = requests.find(r => r.path === '/open')?.body
    for (const [key, value] of Object.entries(expected)) {
      if (key === 'text') continue
      if (key === 'limits') for (const [limit, setting] of Object.entries(value as any)) expect(opened[limit]).toEqual(setting)
      else expect(opened[key]).toEqual(value)
    }
  }
})

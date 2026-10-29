import { test, expect, mock } from 'claude-code/testing'
import type { On } from 'claude-code'

function world(on: On) {
  const requests: { path: string; body: any; socket?: string }[] = []
  const files = new Map<string, string>()
  const state = new Map<string, { value: any; version: number }>()
  const replies = { roster: [] as any[], agents: [] as any[], restore: {} as any, transient: false, jobs: [] as any[], record: undefined as any }
  let modelCalls = 0
  mock.env(on, { HOME: '/test', LORD_DAEMON_SOCKET: '/test/lord.sock' })
  mock.clock(on)
  on('state.get', (_, e: any) => ({ value: state.get(`${e.key}/${e.id}`) ?? { value: undefined, version: 0 } }) as any)
  on('state.set', (_, e: any) => {
    const key = `${e.key}/${e.id}`, version = (state.get(key)?.version ?? 0) + 1
    state.set(key, { value: e.value, version })
    return { value: { isSet: true, version } } as any
  })
  on('fs.read', (_, e) => files.has(e.path) ? { value: files.get(e.path)! } : { deny: 'absent' })
  on('agent.list', () => ({ value: replies.roster }))
  on('agent.spawn', (_, e) => ({ agentId: e.tool_use_id, model: 'haiku' }))
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_, e) => ({ text: e.answer }))
  on('session.end', (_, e) => ({ sessionId: e.sessionId }))
  on('session.cwd', () => ({ value: '/repo' }))
  on('session.send', () => ({ isDelivered: true }))
  on('tool.call', () => ({ result: {} }))
  on('http.fetch', (_, e) => {
    const path = e.url.replace('http://lord', ''), body = e.init?.body ? JSON.parse(e.init.body) : undefined
    requests.push({ path, body, socket: e.init?.socketPath })
    if (path === '/restore' && replies.transient)
      return { value: { ok: false, status: 503, headers: {}, text: JSON.stringify({ error: 'record could not be read; retry later' }) } }
    const result = path === '/agents' ? replies.agents : path === '/restore' ? replies.restore
      : path === '/close' ? { ok: true, closed: true, stopped_jobs: replies.jobs.length, jobs: replies.jobs }
      : { ok: true, vendorSession: 'vendor-session' }
    return { value: { ok: true, status: 200, headers: {}, text: JSON.stringify(result) } }
  })
  on('process.spawn', async function* (_, e) {
    const path = e.argv[e.argv.length - 1].replace('http://lord', '')
    requests.push({ path, body: JSON.parse(e.input ?? '{}') })
    yield { stream: 'stdout', text: JSON.stringify({ type: 'done', text: 'answer', record: replies.record }) + '\n' }
    return { value: { code: 0, signal: null } }
  })
  on('turn.step', async function* (_, e) {
    modelCalls++
    return { turnId: e.turnId, index: e.index, answer: 'MODEL CALLED', toolUses: [], stopReason: 'end_turn', usage: null }
  })
  return { requests, files, state, replies, modelCalls: () => modelCalls }
}

async function step($: any, agentId = 'a') {
  await $.turn.start({ agentId, turnId: 'turn', text: 'continue' })
  const stream = $.turn.step({ agentId, turnId: 'turn', index: 0, model: 'haiku', effort: 'high', messageCount: 1 })
  while (true) { const item = await stream.next(); if (item.done) return item.value }
}

test('r2 first-open record with no turns prevents model fallback after restart', async ($, on) => {
  const w = world(on)
  w.files.set('/test/.claude/lord/logs/a.json', JSON.stringify({ family: 'gpt', turns: [], vendorSession: 'vendor-session' }))
  w.replies.roster = [{ id: 'a', type: 'gpt-6.1-sol' }]
  w.replies.restore = { found: true, agent: { family: 'gpt', cwd: '/repo', model: 'fake', effort: 'high' } }
  expect((await step($)).answer).toBe('answer')
  expect(w.requests.find(x => x.path === '/restore')?.body.agentId).toBe('a')
  expect(w.modelCalls()).toBe(0)
})

test('r2 quit sends ids used before clear or resume to their daemons', async ($, on) => {
  const w = world(on)
  for (const [id, daemon] of [['old', '/test/old.sock'], ['current', '/test/lord.sock']])
    w.state.set(`agent/${id}`, { value: { family: 'gpt', daemon }, version: 1 })
  await $.session.send({ to: 'old', text: 'hello' })
  await $.session.end({ sessionId: 'before', reason: 'clear' })
  await $.session.send({ to: 'current', text: 'hello' })
  await $.session.end({ sessionId: 'after', reason: 'resume' })
  expect(w.requests.filter(x => x.path === '/session_end')).toEqual([])
  await $.session.end({ sessionId: 'quit-session', reason: 'prompt_input_exit' })
  const ends = w.requests.filter(x => x.path === '/session_end')
  expect(ends.find(x => x.socket === '/test/old.sock')?.body.agentIds).toEqual(['old'])
  expect(ends.find(x => x.socket === '/test/lord.sock')?.body.agentIds).toEqual(['current'])
})

test('r2 transient record read retries without a sticky refusal', async ($, on) => {
  const w = world(on)
  w.files.set('/test/.claude/lord/logs/a.json', '{}')
  w.replies.transient = true
  expect((await step($)).answer).toContain('retry later')
  expect(w.state.get('agent/a')?.value.resumeError).toBeUndefined()
  w.replies.transient = false
  w.replies.restore = { found: true, agent: { family: 'gpt', cwd: '/repo' } }
  expect((await step($)).answer).toBe('answer')
  expect(w.modelCalls()).toBe(0)
})

test('r2 TaskStop names the jobs killed by that event', async ($, on) => {
  const w = world(on)
  w.replies.agents = [{ agentId: 'a', family: 'gpt', state: 'idle' }]
  w.replies.jobs = [{ pid: 10001, command: 'worker probe', age_s: 2 }]
  const stopped: any = await $.tool.call({ tool: 'TaskStop', tool_use_id: 'stop', task_id: 'a' })
  expect(stopped.result.message).toContain('1 jobs')
  expect(stopped.result.message).toContain('10001 worker probe')
})

test('r2 footer lists five jobs and counts the remainder', async ($, on) => {
  const w = world(on)
  w.replies.roster = [{ id: 'a', type: 'lord:gpt' }]
  w.replies.agents = [{ agentId: 'a', family: 'gpt', cwd: '/repo' }]
  const jobs = Array.from({ length: 8 }, (_, i) => ({ pid: 10000 + i, command: `worker ${i}`, age_s: i }))
  w.replies.record = { ended_how: 'done', model: 'fake', effort: 'high', started: '', ended: '', commands: 0, retries: 0, helpers: [], jobs, killed_jobs: jobs }
  const answer = (await step($)).answer
  expect(answer).toContain('10004 worker 4')
  expect(answer).toContain('and 3 more')
  expect(answer).not.toContain('10005 worker 5')
  expect(answer.match(/\d+ worker \d+ /g)?.length).toBe(5)
  expect(jobs.length).toBe(8)
})

test('r2 footer shares its job budget between live and killed jobs', async ($, on) => {
  const w = world(on)
  w.replies.roster = [{ id: 'a', type: 'lord:gpt' }]
  w.replies.agents = [{ agentId: 'a', family: 'gpt', cwd: '/repo' }]
  const jobs = Array.from({ length: 8 }, (_, i) => ({ pid: 10000 + i, command: `worker ${i}`, age_s: i }))
  w.replies.record = { ended_how: 'done', model: 'fake', effort: 'high', started: '', ended: '', commands: 0, retries: 0, helpers: [], jobs: jobs.slice(0, 2), killed_jobs: jobs }
  const answer = (await step($)).answer
  expect(answer.match(/\d+ worker \d+ /g)?.length).toBe(5)
  expect(answer).toContain('and 5 more')
})

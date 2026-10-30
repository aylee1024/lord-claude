import { test, expect, mock } from 'claude-code/testing'
import type { On } from 'claude-code'

export function world(on: On) {
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

export async function step($: any, agentId = 'a') {
  await $.turn.start({ agentId, turnId: 'turn', text: 'continue' })
  const stream = $.turn.step({ agentId, turnId: 'turn', index: 0, model: 'haiku', effort: 'high', messageCount: 1 })
  while (true) { const item = await stream.next(); if (item.done) return item.value }
}


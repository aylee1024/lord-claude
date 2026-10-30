import { test, expect } from 'claude-code/testing'
import { world, step } from './plugin_helpers'

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

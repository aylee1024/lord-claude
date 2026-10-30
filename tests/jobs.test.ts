import { test, expect } from 'claude-code/testing'
import { world, step } from './plugin_helpers'

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

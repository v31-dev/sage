import { describe, expect, it } from 'vitest'
import { matchesTaskHistoryQuery, type FinishedTask, type TaskHistoryQuery } from '@/services/api'

const task: FinishedTask = {
  task_id: 't1',
  name: 'deploy_application',
  scopes: ['app:demo-web', 'platform'],
  params: {},
  labels: { Application: 'Shop / Web_100%', Worker: 'w1', Volume: 'Åpi' },
  executor: 'app',
  status: 'completed',
  started_at: '2026-01-01T12:00:00',
  created_at: '2026-01-01T12:00:00',
  updated_at: '2026-01-01T12:10:00',
  finished_at: '2026-01-01T12:10:00',
}

describe('a pushed finished task, kept as the history route would return it', () => {
  it.each<[string, TaskHistoryQuery, boolean]>([
    ['no filter', {}, true],
    ['a label substring in another case', { label: 'WEB' }, true],
    ['another label', { label: 'w1' }, true],
    ['a label key', { label: 'Application' }, false],
    ['an absent label', { label: 'api' }, false],
    ['a literal percent and underscore', { label: '_100%' }, true],
    ['a literal wildcard', { label: 'Web*' }, false],
    ['ASCII case within a Unicode label', { label: 'ÅPI' }, true],
    ['another non-ASCII letter', { label: 'åpi' }, false],
    ['its name', { name: 'deploy_application' }, true],
    ['another name', { name: 'sync_workers' }, false],
    ['its executor and status', { executor: 'app', status: 'completed' }, true],
    ['another status', { status: 'failed' }, false],
    ['one of its scopes', { scope: 'platform' }, true],
    ['the parent of one of its scopes', { scope: 'app' }, true],
    ['a scope beginning like one of its own', { scope: 'app:demo' }, false],
    ['a scope with *', { scope: 'app:demo*' }, true],
    ['a scope in another case', { scope: 'APP:Demo-Web' }, true],
    ['a scope whose dot is a dot', { scope: 'app:demo.web' }, false],
    ['a window opening as it finished', { from_ts: '2026-01-01T12:10:00Z' }, true],
    ['a window opening after it finished', { from_ts: '2026-01-01T12:10:01Z' }, false],
    ['a window closing as it was queued', { to_ts: '2026-01-01T12:00:00Z' }, true],
    ['a window closing before it was queued', { to_ts: '2026-01-01T11:59:59Z' }, false],
  ])('with %s: %s', (_case, query, kept) => {
    expect(matchesTaskHistoryQuery(query, task)).toBe(kept)
  })
})

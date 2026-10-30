import { describe, expect, it } from 'vitest'
import { matchesLogQuery, type LogEntry, type LogQuery } from '@/services/api'

const line: LogEntry = {
  id: 1,
  hostname: 'w1',
  ts: '2026-01-01T12:00:00.500000000Z',
  stream: 'stdout',
  message: 'GET /items/42 Done',
}

describe('a pushed line, kept as the logs route would return it', () => {
  it.each<[string, LogQuery, boolean]>([
    ['no filter', {}, true],
    ['its worker', { hostname: 'w1' }, true],
    ['another worker', { hostname: 'w2' }, false],
    ['text anywhere in it, in any case', { search: 'items/42 done' }, true],
    ['text it does not hold', { search: 'POST' }, false],
    ['a window from its time', { from_ts: '2026-01-01T12:00:00.500Z' }, true],
    ['a window from after it', { from_ts: '2026-01-01T12:00:00.501Z' }, false],
    ['a window up to its time, which is left out', { to_ts: '2026-01-01T12:00:00.500Z' }, false],
    ['a window up to after it', { to_ts: '2026-01-01T12:00:00.501Z' }, true],
    ['its worker and text it does not hold', { hostname: 'w1', search: 'POST' }, false],
  ])('with %s: %s', (_case, query, kept) => {
    expect(matchesLogQuery(query, line)).toBe(kept)
  })
})

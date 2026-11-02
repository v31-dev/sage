import { describe, expect, it } from 'vitest'
import { matchesRequestQuery, type RequestQuery, type ServedRequest } from '@/services/api'

const served: ServedRequest = {
  id: 1,
  ts: '2026-01-01T12:00:00.500000000Z',
  worker: 'w1',
  host: 'Web.Example.com',
  method: 'GET',
  path: '/Items/42',
  status: 404,
  duration_ms: 250,
  router: 'demo-web@file',
  service: 'demo-web@file',
  client: '100.64.0.9',
  size: 120,
}

describe('a pushed request, kept as the requests route would return it', () => {
  it.each<[string, RequestQuery, boolean]>([
    ['no filter', {}, true],
    ['its worker', { worker: 'w1' }, true],
    ['another worker', { worker: 'w2' }, false],
    ['its method', { method: 'GET' }, true],
    ['another method', { method: 'POST' }, false],
    ['its status class', { status: '4xx' }, true],
    ['another status class', { status: '2xx' }, false],
    ['text in its host, in any case', { host: 'example.COM' }, true],
    ['text in its path, in any case', { path: 'items/4' }, true],
    ['text in neither', { path: 'admin' }, false],
    ['a window from its start', { from_ts: '2026-01-01T12:00:00.500Z' }, true],
    ['a window up to its start, which is left out', { to_ts: '2026-01-01T12:00:00.500Z' }, false],
  ])('with %s: %s', (_case, query, kept) => {
    expect(matchesRequestQuery(query, served)).toBe(kept)
  })

  it('needs a router only when the query asks for the routed ones', () => {
    const unrouted = { ...served, router: '', service: '' }

    expect(matchesRequestQuery({ routed: true }, served)).toBe(true)
    expect(matchesRequestQuery({ routed: true }, unrouted)).toBe(false)
    expect(matchesRequestQuery({ routed: false }, unrouted)).toBe(true)
  })
})

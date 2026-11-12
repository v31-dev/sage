import type { Container } from '@/services/api'

// A container as the manager sends it: active and current, its runtime running, nothing queued; `overrides` over it.
export function container(overrides: Partial<Container> = {}): Container {
  return {
    id: 'c1',
    worker: { hostname: 'w1', subdomain: 'red' },
    application: { name: 'web', label: 'Web', project: { name: 'demo', label: 'Demo' } },
    view: { headline: 'active', config: 'current', reason: null, detail: null },
    target: { kind: 'present', record: null },
    runtime: {
      knowledge: 'known',
      reading: {
        state: 'running',
        deployment_id: 'd1',
        health: null,
        exit_code: null,
        oom_killed: false,
      },
    },
    live: null,
    routable: true,
    queued: [],
    domains: [],
    events: [],
    created_at: new Date('2026-01-01T00:00:00Z'),
    updated_at: new Date('2026-01-01T00:00:00Z'),
    ...overrides,
  }
}

import type { WorkerCertificate, WorkerListItem, WorkerStatus } from '@/services/api'

export const readyCertificate: WorkerCertificate = {
  covered: true,
  synced: true,
  expiry: null,
  error: null,
}

// A worker as the manager lists it: `status`, reachable and set up, its current report showing its stack running and
// three containers, both certificates ready; `overrides` over it.
export function worker(
  hostname: string,
  status: WorkerStatus = 'online',
  overrides: Partial<WorkerListItem> = {}
): WorkerListItem {
  return {
    hostname,
    subdomain: null,
    view: { status, reason: null, detail: null, ready: status === 'online' },
    ssh: 'reachable',
    setup: 'succeeded',
    report: {
      knowledge: 'known',
      traefik_reachable: true,
      stack_down: [],
      stack: [['traefik', 'running']],
      containers: 3,
    },
    tasks: [],
    address: '100.64.0.2',
    path: null,
    certificates: { worker: readyCertificate, discovery: readyCertificate },
    tunnel: 'healthy',
    tunnel_name: `sage-example.test-${hostname}`,
    metrics: null,
    created_at: new Date('2026-01-01T00:00:00Z'),
    updated_at: new Date('2026-01-01T00:00:00Z'),
    ...overrides,
  }
}

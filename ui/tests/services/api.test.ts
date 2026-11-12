import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import * as api from '@/services/api'

let fetchMock: Mock

function reply(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

// The method, the path, the query parameters and the body of the last call.
function lastCall() {
  const [url, init] = fetchMock.mock.lastCall as [string, RequestInit]
  const parsed = new URL(url, 'http://manager.test')
  return {
    method: init.method,
    path: parsed.pathname,
    params: Object.fromEntries(parsed.searchParams),
    body: init.body === undefined ? undefined : JSON.parse(init.body as string),
  }
}

beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue(reply({}))
  vi.stubGlobal('fetch', fetchMock)
})

describe('the requests a container refuses', () => {
  const container = (live: api.ContainerOperation | null, queued: api.ContainerOperation[] = []) =>
    ({
      live,
      queued: queued.map(operation => ({ operation, task_id: 't1', cancellable: true })),
    }) as api.Container

  it.each([
    ['nothing runs or waits', container(null), false],
    ['a request waits its turn', container(null, ['deploy']), false],
    ['an automatic backup runs', container('backup_preserve'), false],
    ['one of its own operations runs', container('deploy'), true],
    ['a restore runs', container('restore_stopped'), true],
    ['a Delete runs', container('delete'), true],
    ['a Delete waits', container(null, ['delete']), true],
  ])('when %s: %s', (_name, value, refused) => {
    expect(api.refusesRequests(value)).toBe(refused)
  })
})

const APP = '/api/projects/demo/applications/web'

// Each call: what it is given, and the method, path, query and body it sends.
const CALLS: Array<[string, () => Promise<unknown>, string, string, object, unknown]> = [
  ['fetchAppInfo', () => api.fetchAppInfo(), 'GET', '/api', {}, undefined],
  ['fetchRelease', () => api.fetchRelease(), 'GET', '/api/release', {}, undefined],
  ['refreshRelease', () => api.refreshRelease(), 'POST', '/api/release/refresh', {}, undefined],
  ['fetchHomeSummary', () => api.fetchHomeSummary(), 'GET', '/api/summary', {}, undefined],
  ['fetchWorkers', () => api.fetchWorkers(), 'GET', '/api/workers/', {}, undefined],
  ['fetchWorker', () => api.fetchWorker('w1'), 'GET', '/api/workers/w1', {}, undefined],
  [
    'addWorker',
    () => api.addWorker({ host: '100.64.0.2', tag: 'eu1', pool: 'eu' }),
    'POST',
    '/api/workers/',
    {},
    { host: '100.64.0.2', tag: 'eu1', pool: 'eu' },
  ],
  [
    'updateWorker',
    () => api.updateWorker('w1', { tag: 'eu1', pool: 'eu' }),
    'PUT',
    '/api/workers/w1',
    {},
    { tag: 'eu1', pool: 'eu' },
  ],
  ['rebootWorker', () => api.rebootWorker('w1'), 'POST', '/api/workers/w1/reboot', {}, undefined],
  ['resyncWorker', () => api.resyncWorker('w1'), 'POST', '/api/workers/w1/resync', {}, undefined],
  ['deleteWorker', () => api.deleteWorker('w1'), 'DELETE', '/api/workers/w1', {}, undefined],
  ['fetchProjects', () => api.fetchProjects(), 'GET', '/api/projects/', {}, undefined],
  ['fetchProject', () => api.fetchProject('demo'), 'GET', '/api/projects/demo', {}, undefined],
  [
    'createProject',
    () => api.createProject({ label: 'Demo' }),
    'POST',
    '/api/projects/',
    {},
    { label: 'Demo' },
  ],
  [
    'updateProject',
    () => api.updateProject('demo', { env: null }),
    'PUT',
    '/api/projects/demo',
    {},
    { env: null },
  ],
  ['deleteProject', () => api.deleteProject('demo'), 'DELETE', '/api/projects/demo', {}, undefined],
  ['fetchApplication', () => api.fetchApplication('demo', 'web'), 'GET', APP, {}, undefined],
  [
    'createApplication',
    () => api.createApplication('demo', { label: 'Web' }),
    'POST',
    '/api/projects/demo/applications/',
    {},
    { label: 'Web' },
  ],
  [
    'updateApplication',
    () => api.updateApplication('demo', 'web', { image: 'nginx' }),
    'PUT',
    APP,
    {},
    { image: 'nginx' },
  ],
  ['deleteApplication', () => api.deleteApplication('demo', 'web'), 'DELETE', APP, {}, undefined],
  [
    'deployApplication',
    () => api.deployApplication('demo', 'web'),
    'POST',
    `${APP}/deploy`,
    {},
    undefined,
  ],
  [
    'stopApplication',
    () => api.stopApplication('demo', 'web'),
    'POST',
    `${APP}/stop`,
    {},
    undefined,
  ],
  [
    'fetchApplicationMetrics',
    () => api.fetchApplicationMetrics('demo', 'web', '24h'),
    'GET',
    `${APP}/metrics`,
    { period: '24h' },
    undefined,
  ],
  [
    'createContainer',
    () => api.createContainer('demo', 'web', { worker: 'w1' }),
    'POST',
    `${APP}/containers/`,
    {},
    { worker: 'w1' },
  ],
  [
    'deployContainer',
    () => api.deployContainer('demo', 'web', 'c1'),
    'POST',
    `${APP}/containers/c1/deploy`,
    {},
    undefined,
  ],
  [
    'stopContainer',
    () => api.stopContainer('demo', 'web', 'c1'),
    'POST',
    `${APP}/containers/c1/stop`,
    {},
    undefined,
  ],
  [
    'cancelContainerRequest',
    () => api.cancelContainerRequest('demo', 'web', 'c1', 'delete'),
    'POST',
    `${APP}/containers/c1/cancel/delete`,
    {},
    undefined,
  ],
  [
    'deleteContainer',
    () => api.deleteContainer('demo', 'web', 'c1'),
    'DELETE',
    `${APP}/containers/c1`,
    {},
    undefined,
  ],
  [
    'deleteContainer, forced',
    () => api.deleteContainer('demo', 'web', 'c1', true),
    'DELETE',
    `${APP}/containers/c1`,
    { force: 'true' },
    undefined,
  ],
  [
    'createVolume',
    () => api.createVolume('demo', 'web', { name: 'data', path: '/data' }),
    'POST',
    `${APP}/volumes/`,
    {},
    { name: 'data', path: '/data' },
  ],
  [
    'updateVolume',
    () => api.updateVolume('demo', 'web', 'data', { backup_cron: '' }),
    'PUT',
    `${APP}/volumes/data`,
    {},
    { backup_cron: '' },
  ],
  [
    'deleteVolume',
    () => api.deleteVolume('demo', 'web', 'data'),
    'DELETE',
    `${APP}/volumes/data`,
    {},
    undefined,
  ],
  ['fetchDomains', () => api.fetchDomains(), 'GET', '/api/domains/', {}, undefined],
  [
    'createDomain',
    () => api.createDomain({ name: 'web', type: 'public', port: 80, container: 'c1' }),
    'POST',
    '/api/domains/',
    {},
    { name: 'web', type: 'public', port: 80, container: 'c1' },
  ],
  [
    'updateDomain',
    () => api.updateDomain('d1', { port: 8080 }),
    'PUT',
    '/api/domains/d1',
    {},
    { port: 8080 },
  ],
  ['deleteDomain', () => api.deleteDomain('d1'), 'DELETE', '/api/domains/d1', {}, undefined],
  ['fetchSetting', () => api.fetchSetting('s3'), 'GET', '/api/settings/s3', {}, undefined],
  [
    'updateSetting',
    () => api.updateSetting('s3', { bucket: 'b', path: null }),
    'PUT',
    '/api/settings/s3',
    {},
    { value: { bucket: 'b', path: null } },
  ],
  [
    'testSetting',
    () => api.testSetting('notifications', { discord: 'https://hooks.example.test' }),
    'POST',
    '/api/settings/notifications/test',
    {},
    { value: { discord: 'https://hooks.example.test' } },
  ],
  ['pauseSage', () => api.pauseSage(), 'POST', '/api/settings/pause', {}, undefined],
  ['resumeSage', () => api.resumeSage(), 'POST', '/api/settings/resume', {}, undefined],
  [
    'resyncWorkers',
    () => api.resyncWorkers(),
    'POST',
    '/api/settings/resync_workers',
    {},
    undefined,
  ],
  [
    'fetchWorkerMetrics, the hour by default',
    () => api.fetchWorkerMetrics('w1'),
    'GET',
    '/api/workers/w1/metrics',
    { period: '1h' },
    undefined,
  ],
  [
    'fetchWorkerMetrics, a period',
    () => api.fetchWorkerMetrics('w1', '1w'),
    'GET',
    '/api/workers/w1/metrics',
    { period: '1w' },
    undefined,
  ],
  [
    'fetchApplicationLogs, with no filter',
    () => api.fetchApplicationLogs('demo', 'web'),
    'GET',
    '/api/projects/demo/applications/web/logs',
    {},
    undefined,
  ],
  [
    'fetchApplicationLogs, with only the filters that are set',
    () =>
      api.fetchApplicationLogs('demo', 'web', {
        hostname: 'w1',
        search: 'a&b=c d',
        from_ts: '',
        to_ts: '2026-10-24T10:00:00Z',
        after_id: 42,
      }),
    'GET',
    '/api/projects/demo/applications/web/logs',
    { hostname: 'w1', search: 'a&b=c d', to_ts: '2026-10-24T10:00:00Z', after_id: '42' },
    undefined,
  ],
  [
    'fetchRequests, with no filter',
    () => api.fetchRequests(),
    'GET',
    '/api/requests/',
    {},
    undefined,
  ],
  [
    'fetchRequests, with only the filters that are set',
    () =>
      api.fetchRequests({
        worker: 'w1',
        status: '5xx',
        path: '',
        routed: true,
        from_ts: '2026-10-24T10:00:00.000Z',
      }),
    'GET',
    '/api/requests/',
    { worker: 'w1', status: '5xx', routed: 'true', from_ts: '2026-10-24T10:00:00.000Z' },
    undefined,
  ],
  [
    'fetchNotifications, with no window',
    () => api.fetchNotifications(),
    'GET',
    '/api/notifications/',
    {},
    undefined,
  ],
  [
    'fetchNotifications, with a limit and a window',
    () => api.fetchNotifications({ limit: 25, from_ts: '2026-10-24T10:00:00.123456', to_ts: null }),
    'GET',
    '/api/notifications/',
    { limit: '25', from_ts: '2026-10-24T10:00:00.123456' },
    undefined,
  ],
  ['fetchTasks', () => api.fetchTasks(), 'GET', '/api/tasks/', {}, undefined],
  ['fetchTaskHistory', () => api.fetchTaskHistory(), 'GET', '/api/tasks/history', {}, undefined],
  [
    'fetchTaskHistory, filtered, the newest 100',
    () =>
      api.fetchTaskHistory({
        name: 'deploy',
        scope: 'app',
        executor: '',
        from_ts: '2026-01-01T12:00:00.000Z',
        limit: 100,
      }),
    'GET',
    '/api/tasks/history',
    {
      name: 'deploy',
      scope: 'app',
      from_ts: '2026-01-01T12:00:00.000Z',
      limit: '100',
    },
    undefined,
  ],
  [
    'fetchTaskHistoryFilters',
    () => api.fetchTaskHistoryFilters(),
    'GET',
    '/api/tasks/history/filters',
    {},
    undefined,
  ],
  [
    'fetchTaskLogs',
    () => api.fetchTaskLogs('abc12345'),
    'GET',
    '/api/tasks/abc12345/logs',
    {},
    undefined,
  ],
  [
    'fetchTaskLogs, after a line',
    () => api.fetchTaskLogs('abc12345', '', 42),
    'GET',
    '/api/tasks/abc12345/logs',
    { after_id: '42' },
    undefined,
  ],
  [
    'fetchTaskLogs, of a part',
    () => api.fetchTaskLogs('abc12345', 'def67890'),
    'GET',
    '/api/tasks/abc12345/logs',
    { sub_task_id: 'def67890' },
    undefined,
  ],
  [
    'fetchTaskParts',
    () => api.fetchTaskParts('abc12345'),
    'GET',
    '/api/tasks/abc12345/parts',
    {},
    undefined,
  ],
  ['fetchBackups', () => api.fetchBackups(), 'GET', '/api/backups/', {}, undefined],
  [
    'fetchBackupStatus',
    () => api.fetchBackupStatus(),
    'GET',
    '/api/backups/platform_backup_status',
    {},
    undefined,
  ],
  ['createBackup', () => api.createBackup(), 'POST', '/api/backups/', {}, undefined],
  ['discoverBackups', () => api.discoverBackups(), 'POST', '/api/backups/discover', {}, undefined],
  ['deleteBackup', () => api.deleteBackup(7), 'DELETE', '/api/backups/7', {}, undefined],
  ['restoreBackup', () => api.restoreBackup(7), 'POST', '/api/backups/7/restore', {}, undefined],
  [
    'fetchVolumeBackups',
    () => api.fetchVolumeBackups('demo', 'web', 'data'),
    'GET',
    `${APP}/volumes/data/backups/`,
    {},
    undefined,
  ],
  [
    'createVolumeBackup',
    () => api.createVolumeBackup('demo', 'web', 'data'),
    'POST',
    `${APP}/volumes/data/backups/`,
    {},
    undefined,
  ],
  [
    'restoreVolumeBackup',
    () => api.restoreVolumeBackup('demo', 'web', 'data', 7, 'c1', 'logs'),
    'POST',
    `${APP}/volumes/data/backups/7/restore`,
    {},
    { container: 'c1', volume: 'logs' },
  ],
]

describe('every call', () => {
  it.each(CALLS)('%s', async (_name, call, method, path, params, body) => {
    await call()

    expect(lastCall()).toEqual({ method, path, params, body })
  })

  it('names every call the module exports', () => {
    const named = new Set(CALLS.map(([name]) => name.split(',')[0]))
    const calls = Object.entries(api)
      .filter(([, value]) => typeof value === 'function')
      .map(([name]) => name)
      // The downloads are tested below; `refusesRequests` above and a `matches` function (tests/services/api/) call
      // nothing.
      .filter(name => !['downloadBackup', 'downloadVolumeBackup', 'refusesRequests'].includes(name))
      .filter(name => !name.startsWith('matches'))
    expect(calls.filter(name => !named.has(name))).toEqual([])
  })
})

describe('downloading a backup', () => {
  function clicks() {
    const clicked: string[] = []
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement
    ) {
      clicked.push(this.href)
    })
    return clicked
  }

  it.each([
    ['a platform backup', () => api.downloadBackup(7), '/api/backups/7/download'],
    [
      'a volume backup',
      () => api.downloadVolumeBackup('demo', 'web', 'data', 7),
      `${APP}/volumes/data/backups/7/download`,
    ],
  ])('asks for the link of %s and then opens it', async (_name, call, path) => {
    const clicked = clicks()
    fetchMock.mockResolvedValue(reply({ url: 'https://s3.example.test/b.enc' }))

    await call()

    expect([lastCall().method, lastCall().path]).toEqual(['GET', path])
    expect(clicked).toEqual(['https://s3.example.test/b.enc'])
  })

  it('opens nothing when the link cannot be made', async () => {
    const clicked = clicks()
    fetchMock.mockResolvedValue(reply({ detail: 'S3 is not configured.' }, 400))

    await expect(api.downloadBackup(3)).rejects.toThrow('S3 is not configured.')
    expect(clicked).toEqual([])
  })
})

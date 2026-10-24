import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import {
  APPLICATION_BUSY_STATUSES,
  APPLICATION_STOP_ELIGIBLE_STATUSES,
  backupAPI,
  downloadBackup,
  fetchAppInfo,
  fetchHomeSummary,
  fetchLogs,
  fetchNotifications,
  fetchRelease,
  fetchWorkerMetrics,
  getApplicationAPI,
  getContainerAPI,
  getDomainAPI,
  getVolumeAPI,
  getVolumeBackupAPI,
  projectAPI,
  refreshRelease,
  settingsAPI,
  tasksAPI,
  workersAPI,
} from '@/services/api'

let fetchMock: Mock

function reply(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

// The path and the query parameters of the last call, whatever the order or a trailing "?".
function lastCall() {
  const [url, init] = fetchMock.mock.lastCall as [string, RequestInit | undefined]
  const parsed = new URL(url, 'http://manager.test')
  return {
    path: parsed.pathname,
    params: Object.fromEntries(parsed.searchParams),
    init,
  }
}

beforeEach(() => {
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
  vi.spyOn(console, 'error').mockImplementation(() => {})
})

describe('the status lists', () => {
  it('names the statuses in which an application is busy', () => {
    expect(APPLICATION_BUSY_STATUSES).toEqual(['deploying', 'stopping', 'backup', 'restoring'])
  })

  it('lets an application be stopped when it is active or in error', () => {
    expect(APPLICATION_STOP_ELIGIBLE_STATUSES).toEqual(['active', 'error'])
  })
})

describe('the APIs', () => {
  it.each([
    [workersAPI, 'Workers', '/api/workers'],
    [projectAPI, 'Project', '/api/projects'],
    [settingsAPI, 'Settings', '/api/settings'],
    [tasksAPI, 'Task', '/api/tasks'],
    [backupAPI, 'Backup', '/api/backups'],
  ])('%#: points at its path', (api, name, path) => {
    expect([api.name, api.path]).toEqual([name, path])
  })

  it.each([
    ['application', getApplicationAPI('demo'), '/api/projects/demo/applications'],
    ['container', getContainerAPI('demo', 'web'), '/api/projects/demo/applications/web/containers'],
    ['volume', getVolumeAPI('demo', 'web'), '/api/projects/demo/applications/web/volumes'],
    ['domain', getDomainAPI('demo', 'web'), '/api/projects/demo/applications/web/domains'],
    [
      'volume backup',
      getVolumeBackupAPI('demo', 'web', 'data'),
      '/api/projects/demo/applications/web/volumes/data/backups',
    ],
  ])('builds the %s path from the names', (_name, api, path) => {
    expect(api.path).toBe(path)
  })

  it('makes a fresh API for every call', () => {
    expect(getApplicationAPI('a')).not.toBe(getApplicationAPI('a'))
    expect(getApplicationAPI('a').path).not.toBe(getApplicationAPI('b').path)
  })
})

describe('the load delay', () => {
  it.each([
    [undefined, 300],
    ['', 300],
    ['0', 0],
    ['50', 50],
    ['abc', 300],
    ['-5', 300],
    ['Infinity', 300],
  ])('reads VITE_LOAD_DELAY=%j as %j', async (value, delay) => {
    vi.resetModules()
    if (value === undefined) vi.stubEnv('VITE_LOAD_DELAY', '')
    else vi.stubEnv('VITE_LOAD_DELAY', value)

    const module = await import('@/services/api')

    expect(module.projectAPI.load_delay).toBe(delay)
  })
})

describe('fetchAppInfo', () => {
  it('gets the manager info from /api', async () => {
    fetchMock.mockResolvedValue(reply({ version: '1.0.0', state: 'running' }))

    expect(await fetchAppInfo()).toEqual({ version: '1.0.0', state: 'running' })
    expect(lastCall().path).toBe('/api')
  })

  it('throws when the call fails and logs it', async () => {
    fetchMock.mockResolvedValue(reply({ detail: 'x' }, 500))

    await expect(fetchAppInfo()).rejects.toThrow('Failed to fetch app info')
    expect(console.error).toHaveBeenCalled()
  })
})

describe('the release calls', () => {
  it('fetchRelease gets the cached release', async () => {
    fetchMock.mockResolvedValue(reply({ version: '1.2.3', body: 'notes' }))

    expect(await fetchRelease()).toEqual({ version: '1.2.3', body: 'notes' })
    expect(lastCall().path).toBe('/api/release')
  })

  it('fetchRelease throws the detail, or says what failed', async () => {
    fetchMock.mockResolvedValueOnce(reply({ detail: 'No release known yet' }, 404))
    await expect(fetchRelease()).rejects.toThrow('No release known yet')

    fetchMock.mockResolvedValueOnce(reply({}, 500))
    await expect(fetchRelease()).rejects.toThrow('Failed to fetch latest release')
  })

  it('refreshRelease posts to the refresh path', async () => {
    fetchMock.mockResolvedValue(reply({ status: 'OK' }))

    await refreshRelease()

    expect(lastCall().path).toBe('/api/release/refresh')
    expect(lastCall().init).toEqual({ method: 'POST' })
  })

  it('refreshRelease throws the detail, or says what failed', async () => {
    fetchMock.mockResolvedValueOnce(reply({ detail: 'GitHub unreachable' }, 502))
    await expect(refreshRelease()).rejects.toThrow('GitHub unreachable')

    fetchMock.mockResolvedValueOnce(reply({}, 500))
    await expect(refreshRelease()).rejects.toThrow('Failed to refresh release')
  })
})

describe('fetchWorkerMetrics', () => {
  it('asks for the hour by default', async () => {
    fetchMock.mockResolvedValue(reply({ host: [], containers: {}, meta: {} }))

    await fetchWorkerMetrics('w1')

    expect(lastCall().path).toBe('/api/workers/w1/metrics')
    expect(lastCall().params).toEqual({ period: '1h' })
  })

  it.each(['1m', '24h', '1w'] as const)('asks for the period %s', async period => {
    fetchMock.mockResolvedValue(reply({}))

    await fetchWorkerMetrics('w1', period)

    expect(lastCall().params).toEqual({ period })
  })

  it('throws when the call fails', async () => {
    fetchMock.mockResolvedValue(reply({}, 500))
    await expect(fetchWorkerMetrics('w1')).rejects.toThrow('Failed to fetch worker metrics')
  })
})

describe('fetchLogs', () => {
  it('asks for a container of a worker with no parameters by default', async () => {
    fetchMock.mockResolvedValue(reply([]))

    await fetchLogs('w1', 'demo-web')

    expect(lastCall().path).toBe('/api/workers/w1/logs/demo-web')
    expect(lastCall().params).toEqual({})
  })

  it('sends only the filters that are set', async () => {
    fetchMock.mockResolvedValue(reply([]))

    await fetchLogs('w1', 'demo-web', 'error', '', '2026-10-24T10:00:00Z')

    expect(lastCall().params).toEqual({ search: 'error', to_ts: '2026-10-24T10:00:00Z' })
  })

  it('encodes the search text', async () => {
    fetchMock.mockResolvedValue(reply([]))

    await fetchLogs('w1', 'demo-web', 'a&b=c d', '2026-10-24T10:00:00Z', '')

    expect(lastCall().params).toEqual({ search: 'a&b=c d', from_ts: '2026-10-24T10:00:00Z' })
  })

  it('throws the detail, or says what failed', async () => {
    fetchMock.mockResolvedValueOnce(reply({ detail: 'Worker w1 is offline' }, 503))
    await expect(fetchLogs('w1', 'c')).rejects.toThrow('Worker w1 is offline')

    fetchMock.mockResolvedValueOnce(reply({}, 500))
    await expect(fetchLogs('w1', 'c')).rejects.toThrow('Failed to fetch logs')
  })
})

describe('fetchHomeSummary', () => {
  it('gets the summary', async () => {
    fetchMock.mockResolvedValue(reply({ workers_total: 2 }))

    expect(await fetchHomeSummary()).toEqual({ workers_total: 2 })
    expect(lastCall().path).toBe('/api/summary')
  })

  it('throws the detail, or says what failed', async () => {
    fetchMock.mockResolvedValueOnce(reply({ detail: 'Database locked' }, 500))
    await expect(fetchHomeSummary()).rejects.toThrow('Database locked')

    fetchMock.mockResolvedValueOnce(reply({}, 500))
    await expect(fetchHomeSummary()).rejects.toThrow('Failed to fetch home summary')
  })
})

describe('fetchNotifications', () => {
  it('asks for the notifications with no parameters by default', async () => {
    fetchMock.mockResolvedValue(reply([]))

    await fetchNotifications()

    expect(lastCall().path).toBe('/api/notifications/')
    expect(lastCall().params).toEqual({})
  })

  it('sends the limit and the time window it is given', async () => {
    fetchMock.mockResolvedValue(reply([]))

    await fetchNotifications({
      limit: 25,
      from_ts: '2026-10-24T10:00:00.123456',
      to_ts: '2026-10-24T11:00:00',
    })

    expect(lastCall().params).toEqual({
      limit: '25',
      from_ts: '2026-10-24T10:00:00.123456',
      to_ts: '2026-10-24T11:00:00',
    })
  })

  it('leaves out a null window end', async () => {
    fetchMock.mockResolvedValue(reply([]))

    await fetchNotifications({ limit: 10, from_ts: null, to_ts: null })

    expect(lastCall().params).toEqual({ limit: '10' })
  })

  it('throws the detail, or says what failed', async () => {
    fetchMock.mockResolvedValueOnce(reply({ detail: 'Bad window' }, 422))
    await expect(fetchNotifications()).rejects.toThrow('Bad window')

    fetchMock.mockResolvedValueOnce(reply({}, 500))
    await expect(fetchNotifications()).rejects.toThrow('Failed to fetch notifications')
  })
})

describe('downloadBackup', () => {
  it('asks for the download link of the backup and then opens it', async () => {
    const clicked: HTMLAnchorElement[] = []
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement
    ) {
      clicked.push(this)
    })
    const api = { action: vi.fn().mockResolvedValue({ url: 'https://s3.example.test/b.enc' }) }

    await downloadBackup(api, 7)

    expect(api.action).toHaveBeenCalledWith('7/download')
    expect(clicked.map(link => link.href)).toEqual(['https://s3.example.test/b.enc'])
  })

  it('opens nothing when the link cannot be made', async () => {
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const api = { action: vi.fn().mockRejectedValue(new Error('S3 is not configured.')) }

    await expect(downloadBackup(api, '3')).rejects.toThrow('S3 is not configured.')
    expect(click).not.toHaveBeenCalled()
  })
})

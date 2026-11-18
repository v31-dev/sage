import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Home from '@/pages/Home.vue'
import { fetchHomeSummary, type HomeSummary } from '@/services/api'

vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  fetchHomeSummary: vi.fn(),
}))

const summary: HomeSummary = {
  generated_at: '2026-01-01T12:00:00',
  projects: 2,
  applications: 3,
  workers: {
    total: 4,
    online: 1,
    offline: 1,
    checking: 1,
    'not ready': 1,
    adding: 0,
    removing: 0,
    rebooting: 0,
  },
  containers: {
    total: 5,
    active: 1,
    inactive: 1,
    error: 1,
    checking: 1,
    deploying: 1,
    stopping: 0,
    backup: 0,
    restoring: 0,
  },
  domains: { total: 4, synced: 1, pending: 1, syncing: 1, error: 1 },
  deploys: { succeeded: 4, failed: 2 },
  backups: { platform: 2, application: 3, since: 1, latest_at: null },
  notices: { error: 2, warning: 3, events: [] },
}

function mountPage() {
  return mount(Home, {
    global: { stubs: { MetricChart: true, RouterLink: { template: '<a><slot /></a>' } } },
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] })
  vi.mocked(fetchHomeSummary).mockReset().mockResolvedValue(structuredClone(summary))
})
afterEach(() => vi.useRealTimers())

it('shows application counts and each resource state independently', async () => {
  const wrapper = mountPage()
  await flushPromises()
  const text = wrapper.text().replace(/\s+/g, ' ')
  expect(text).toContain('3 apps in 2 projects')
  expect(text).toContain('4 succeeded')
  expect(text).toContain('2 failed')
  for (const state of [
    '1 online',
    '1 offline',
    '1 not ready',
    '1 checking',
    '1 deploying',
    '1 pending',
    '1 syncing',
    '1 sync failed',
  ]) {
    expect(text).toContain(state)
  }
  expect(text).toContain('2 platform')
  expect(text).toContain('3 volume archives')
  expect(text).toContain('2 errors')
  expect(text).toContain('3 warnings')
  expect(text).not.toContain('All running')
  expect(text).not.toContain('undefined')
  expect(text).not.toContain('NaN')
  wrapper.unmount()
})

it('shows an empty inventory without claiming it is all online or running', async () => {
  const empty = structuredClone(summary)
  for (const counts of [empty.workers, empty.containers, empty.domains]) {
    for (const key in counts) counts[key as keyof typeof counts] = 0
  }
  vi.mocked(fetchHomeSummary).mockResolvedValue(empty)
  const wrapper = mountPage()
  await flushPromises()
  expect(wrapper.text()).toContain('No workers')
  expect(wrapper.text()).toContain('No containers')
  expect(wrapper.text()).toContain('No domains')
  expect(wrapper.text()).not.toContain('All online')
  wrapper.unmount()
})

it('keeps one refresh in flight and stops scheduling when it goes away', async () => {
  const wrapper = mountPage()
  await flushPromises()
  let finish!: (value: HomeSummary) => void
  vi.mocked(fetchHomeSummary).mockImplementationOnce(
    () => new Promise(resolve => (finish = resolve))
  )
  await vi.advanceTimersByTimeAsync(60_000)
  expect(fetchHomeSummary).toHaveBeenCalledTimes(2)
  await vi.advanceTimersByTimeAsync(120_000)
  expect(fetchHomeSummary).toHaveBeenCalledTimes(2)
  wrapper.unmount()
  finish(summary)
  await flushPromises()
  await vi.advanceTimersByTimeAsync(120_000)
  expect(fetchHomeSummary).toHaveBeenCalledTimes(2)
})

it('keeps the previous summary after a failed refresh and retries on the next minute', async () => {
  const wrapper = mountPage()
  await flushPromises()
  vi.mocked(fetchHomeSummary).mockRejectedValueOnce(new Error('unreachable'))
  await vi.advanceTimersByTimeAsync(60_000)
  expect(wrapper.text()).toContain('3')
  vi.mocked(fetchHomeSummary).mockResolvedValue({ ...summary, applications: 7 })
  await vi.advanceTimersByTimeAsync(60_000)
  expect(wrapper.text().replace(/\s+/g, ' ')).toContain('7 apps in 2 projects')
  wrapper.unmount()
})

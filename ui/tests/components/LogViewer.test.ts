import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import LogViewer from '@/components/LogViewer.vue'
import { fetchApplicationLogs, type LogEntry } from '@/services/api'

vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  fetchApplicationLogs: vi.fn(),
}))

const line = (id: number, hostname: string, message: string): LogEntry => ({
  id,
  hostname,
  ts: `2026-01-01T12:00:0${id}.000000000Z`,
  stream: 'stdout',
  message,
})

const answer = (lines: LogEntry[], more = false) => ({ lines, more })

async function mountViewer() {
  const wrapper = mount(LogViewer, { props: { project: 'demo', application: 'web' } })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.mocked(fetchApplicationLogs).mockReset()
  vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] })
  vi.setSystemTime(new Date('2026-01-01T12:00:30Z'))
})

afterEach(() => {
  vi.useRealTimers()
})

describe('the application logs', () => {
  it('are read from the application, from five minutes ago, with each line as it was written', async () => {
    vi.mocked(fetchApplicationLogs).mockResolvedValue(
      answer([line(1, 'w1', 'started'), line(2, 'w2', 'Traceback:\n  boom')])
    )

    const wrapper = await mountViewer()

    expect(fetchApplicationLogs).toHaveBeenCalledWith('demo', 'web', {
      hostname: '',
      search: '',
      from_ts: '2026-01-01T11:55:00.000Z',
      to_ts: '',
    })
    expect(wrapper.findAll('tbody pre').map(found => found.element.textContent)).toEqual([
      'started',
      'Traceback:\n  boom',
    ])
  })

  it('are narrowed to a worker by choosing it in a line', async () => {
    vi.mocked(fetchApplicationLogs).mockResolvedValue(answer([line(1, 'w2', 'started')]))
    const wrapper = await mountViewer()

    await wrapper.get('tbody button').trigger('click')
    await flushPromises()

    expect(vi.mocked(fetchApplicationLogs).mock.lastCall![2]).toMatchObject({ hostname: 'w2' })
  })

  it('say when only the newest that match are shown, until a search shows them all', async () => {
    vi.mocked(fetchApplicationLogs).mockResolvedValue(answer([line(1, 'w1', 'a')], true))
    const wrapper = await mountViewer()
    expect(wrapper.text()).toContain('Showing the newest 1000 lines that match.')

    vi.mocked(fetchApplicationLogs).mockResolvedValue(answer([line(1, 'w1', 'a')]))
    await wrapper.get('input[placeholder="Search logs…"]').setValue('a')
    await wrapper
      .findAll('button')
      .find(found => found.text() === 'Search')!
      .trigger('click')
    await flushPromises()

    expect(wrapper.text()).not.toContain('Showing the newest')
  })
})

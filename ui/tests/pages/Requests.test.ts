import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Requests from '@/pages/Requests.vue'
import { formatDate } from '@/lib/utils'
import { TONES } from '@/lib/tone'
import { fetchRequests, type ServedRequest, type WorkerListItem } from '@/services/api'
import { useAppStore } from '@/stores/app'

vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  fetchRequests: vi.fn(),
}))

const inline = { template: '<div><slot /></div>' }
const select = {
  props: ['modelValue'],
  emits: ['update:modelValue'],
  template: '<div data-select><slot /></div>',
}
const option = { props: ['value'], template: '<div data-option>{{ value }}</div>' }
const stubs = {
  Select: select,
  SelectTrigger: inline,
  SelectValue: inline,
  SelectContent: inline,
  SelectItem: option,
}

const TS = '2026-01-01T12:00:00.000000000Z'

function served(id: number, fields: Partial<ServedRequest> = {}): ServedRequest {
  return {
    id,
    ts: TS,
    worker: 'w1',
    host: 'web.example.com',
    method: 'GET',
    path: `/items/${id}`,
    status: 200,
    duration_ms: 12.4,
    router: 'demo-web@file',
    service: 'demo-web@file',
    client: '100.64.0.9',
    size: 1536,
    ...fields,
  }
}

const answer = (requests: ServedRequest[], more = false) => ({ requests, more })

async function mountPage() {
  useAppStore().workers = [{ hostname: 'w1' }, { hostname: 'w2' }] as WorkerListItem[]
  const wrapper = mount(Requests, { global: { stubs } })
  await flushPromises()
  return wrapper
}

type Page = Awaited<ReturnType<typeof mountPage>>

const rows = (wrapper: Page) =>
  wrapper.findAll('tbody tr').map(row => row.findAll('td').map(cell => cell.text()))

const lastQuery = () => vi.mocked(fetchRequests).mock.lastCall![0]

// The dropdowns, in the order of the page: worker, method, status.
async function choose(wrapper: Page, index: number, value: string) {
  wrapper.findAllComponents(select)[index]!.vm.$emit('update:modelValue', value)
  await flushPromises()
}

async function commit(wrapper: Page, id: string, value: string) {
  const field = wrapper.get(`#requests-${id}`)
  await field.setValue(value)
  await field.trigger('change')
  await flushPromises()
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.mocked(fetchRequests).mockReset()
  vi.mocked(fetchRequests).mockResolvedValue(answer([]))
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('the requests', () => {
  it('show each in its columns, newest first as the manager sends them', async () => {
    vi.mocked(fetchRequests).mockResolvedValue(
      answer([served(2, { status: 503, duration_ms: 1250, size: 512 }), served(1)])
    )

    const wrapper = await mountPage()

    expect(rows(wrapper)).toEqual([
      [
        formatDate(TS),
        'w1',
        'GET',
        'web.example.com',
        '/items/2',
        '503',
        '1.25 s',
        '512 B',
        '100.64.0.9',
        'demo-web@file',
      ],
      [
        formatDate(TS),
        'w1',
        'GET',
        'web.example.com',
        '/items/1',
        '200',
        '12 ms',
        '1.5 KiB',
        '100.64.0.9',
        'demo-web@file',
      ],
    ])
    const statuses = wrapper.findAll('tbody tr').map(row => row.findAll('td')[5]!.classes())
    expect(statuses[0]).toContain(TONES.error.text)
    expect(statuses[1]).toContain(TONES.success.text)
  })

  it('say when only the newest are shown', async () => {
    vi.mocked(fetchRequests).mockResolvedValue(answer([served(1), served(2)], true))

    const wrapper = await mountPage()

    expect(wrapper.text()).toContain('Showing the newest 2 requests that match.')
  })

  it('show why they could not be loaded', async () => {
    vi.mocked(fetchRequests).mockRejectedValue(new Error('down'))

    const wrapper = await mountPage()

    expect(wrapper.text()).toContain('down')
  })
})

describe('the filters', () => {
  it('offer the workers and the methods and status classes', async () => {
    const wrapper = await mountPage()

    const options = wrapper
      .findAll('[data-select]')
      .map(found => found.findAll('[data-option]').map(each => each.text()))

    expect(options[0]).toEqual(['any', 'w1', 'w2'])
    expect(options[1]).toContain('POST')
    expect(options[2]).toEqual(['any', '2xx', '3xx', '4xx', '5xx'])
  })

  it('are sent as each is chosen or committed, the times in UTC', async () => {
    const wrapper = await mountPage()
    expect(lastQuery()).toEqual({
      worker: '',
      method: '',
      status: '',
      host: '',
      path: '',
      min_duration_ms: null,
      from_ts: '',
      to_ts: '',
    })

    await choose(wrapper, 0, 'w2')
    await choose(wrapper, 1, 'POST')
    await choose(wrapper, 2, '5xx')
    await commit(wrapper, 'host', ' example.com ')
    await commit(wrapper, 'path', '/api')
    await commit(wrapper, 'slower', '500')
    await commit(wrapper, 'from', '2026-01-01T12:00')
    await commit(wrapper, 'to', '2026-01-01T13:30')

    expect(lastQuery()).toEqual({
      worker: 'w2',
      method: 'POST',
      status: '5xx',
      host: 'example.com',
      path: '/api',
      min_duration_ms: 500,
      from_ts: '2026-01-01T12:00:00.000Z',
      to_ts: '2026-01-01T13:30:00.000Z',
    })
  })
})

describe('loading', () => {
  it('loads again 5 s after each load ends, until the page goes away', async () => {
    const wrapper = await mountPage()
    expect(fetchRequests).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(4_999)
    expect(fetchRequests).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(fetchRequests).toHaveBeenCalledTimes(2)

    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(20_000)
    expect(fetchRequests).toHaveBeenCalledTimes(2)
  })

  it('shows what was asked last, whichever answer comes first', async () => {
    const wrapper = await mountPage()
    let answerFirst!: (value: ReturnType<typeof answer>) => void
    vi.mocked(fetchRequests)
      .mockImplementationOnce(() => new Promise(resolve => (answerFirst = resolve)))
      .mockResolvedValueOnce(answer([served(9, { path: '/newer' })]))

    await choose(wrapper, 2, '4xx')
    await choose(wrapper, 2, '5xx')
    answerFirst(answer([served(8, { path: '/older' })]))
    await flushPromises()

    expect(rows(wrapper).map(row => row[4])).toEqual(['/newer'])
  })
})

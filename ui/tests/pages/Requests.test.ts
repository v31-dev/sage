import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import Requests from '@/pages/Requests.vue'
import { formatDate } from '@/lib/utils'
import { TONES } from '@/lib/tone'
import { fetchRequests, type ServedRequest, type WorkerListItem } from '@/services/api'
import { useAppStore } from '@/stores/app'
import { followTopics, type Followed } from '../helpers/topics'

vi.mock('@/lib/updates', () => ({ watchState: vi.fn(), watchFeed: vi.fn() }))
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

// What the library does each time the socket connects: load, and hand over what came back.
async function connect(feed: Followed) {
  feed.push(await feed.load())
  await flushPromises()
}

async function mountPage(first: ServedRequest[] = [], more = false) {
  useAppStore().workers = [{ hostname: 'w1' }, { hostname: 'w2' }] as WorkerListItem[]
  const topics = followTopics()
  const wrapper = mount(Requests, { global: { stubs } })
  await flushPromises()
  const feed = topics.topic('requests')
  vi.mocked(fetchRequests).mockResolvedValueOnce(answer(first, more))
  await connect(feed)
  return { wrapper, feed }
}

type Page = Awaited<ReturnType<typeof mountPage>>['wrapper']

const rows = (wrapper: Page) =>
  wrapper.findAll('tbody tr').map(row => row.findAll('td').map(cell => cell.text()))

const paths = (wrapper: Page) => rows(wrapper).map(row => row[4])

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
})

describe('the requests', () => {
  it('show each in its columns, newest first as the manager sends them', async () => {
    const { wrapper } = await mountPage([
      served(2, { status: 503, duration_ms: 1250, size: 512 }),
      served(1),
    ])

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
      ],
    ])
    const statuses = wrapper.findAll('tbody tr').map(row => row.findAll('td')[5]!.classes())
    expect(statuses[0]).toContain(TONES.error.text)
    expect(statuses[1]).toContain(TONES.success.text)
  })

  it('say when only the latest are shown', async () => {
    const { wrapper } = await mountPage([served(2), served(1)], true)

    expect(wrapper.text()).toContain('Showing the latest 2 requests that match.')
  })

  it('show why they could not be loaded', async () => {
    const topics = followTopics({ pending: true })
    const wrapper = mount(Requests, { global: { stubs } })
    await flushPromises()

    topics.topic('requests').failed(new Error('down'))
    await flushPromises()

    expect(wrapper.text()).toContain('down')
  })
})

describe('the feed', () => {
  it('is followed, and its requests are merged newest first, each once', async () => {
    const { wrapper, feed } = await mountPage([served(2)])

    feed.push([served(3), served(4)])
    feed.push([served(2), served(1)])
    await flushPromises()

    expect(paths(wrapper)).toEqual(['/items/4', '/items/3', '/items/2', '/items/1'])
  })

  it('is loaded after the newest request held when the socket connects again', async () => {
    const { wrapper, feed } = await mountPage([served(5), served(4)])

    vi.mocked(fetchRequests).mockResolvedValueOnce(answer([served(6)]))
    await connect(feed)

    expect(lastQuery()).toMatchObject({ after_id: 5 })
    expect(paths(wrapper)).toEqual(['/items/6', '/items/5', '/items/4'])
  })

  it.each([
    ['another worker', { worker: 'w2' }],
    ['another method', { method: 'POST' }],
    ['another status class', { status: 404 }],
    ['a host without the text', { host: 'api.other.org' }],
    ['a path without the text', { path: '/health' }],
    ['a quicker answer', { duration_ms: 499 }],
    ['a start before the window', { ts: '2026-01-01T11:59:59.999999999Z' }],
    ['a start at the end of the window', { ts: '2026-01-01T13:30:00.000000000Z' }],
  ])('leaves out a pushed request with %s', async (_case, fields) => {
    const { wrapper, feed } = await mountPage()
    await choose(wrapper, 0, 'w1')
    await choose(wrapper, 1, 'GET')
    await choose(wrapper, 2, '2xx')
    await commit(wrapper, 'host', 'EXAMPLE.com')
    await commit(wrapper, 'path', '/ITEMS')
    await commit(wrapper, 'slower', '500')
    await commit(wrapper, 'from', '2026-01-01T12:00')
    await commit(wrapper, 'to', '2026-01-01T13:30')

    feed.push([served(1, { duration_ms: 500 }), served(2, { duration_ms: 500, ...fields })])
    await flushPromises()

    expect(paths(wrapper)).toEqual(['/items/1'])
  })

  it('stops when the page goes away', async () => {
    const { wrapper, feed } = await mountPage()

    wrapper.unmount()

    expect(feed.stop).toHaveBeenCalledOnce()
  })
})

describe('the filters', () => {
  it('offer the workers and the methods and status classes', async () => {
    const { wrapper } = await mountPage()

    const options = wrapper
      .findAll('[data-select]')
      .map(found => found.findAll('[data-option]').map(each => each.text()))

    expect(options[0]).toEqual(['any', 'w1', 'w2'])
    expect(options[1]).toContain('POST')
    expect(options[2]).toEqual(['any', '2xx', '3xx', '4xx', '5xx'])
  })

  it('are sent as each is chosen or committed, the times in UTC, from the start', async () => {
    const { wrapper } = await mountPage([served(1)])
    expect(lastQuery()).toEqual({
      worker: '',
      method: '',
      status: '',
      host: '',
      path: '',
      min_duration_ms: null,
      from_ts: '',
      to_ts: '',
      after_id: 0,
      limit: 1000,
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
      after_id: 0,
      limit: 1000,
    })
  })

  it('show what was asked last, whichever answer comes first', async () => {
    const { wrapper } = await mountPage()
    let answerFirst!: (value: ReturnType<typeof answer>) => void
    vi.mocked(fetchRequests)
      .mockImplementationOnce(() => new Promise(resolve => (answerFirst = resolve)))
      .mockResolvedValueOnce(answer([served(9, { path: '/newer', status: 503 })]))

    await choose(wrapper, 2, '4xx')
    await choose(wrapper, 2, '5xx')
    answerFirst(answer([served(8, { path: '/older', status: 404 })], true))
    await flushPromises()

    expect(paths(wrapper)).toEqual(['/newer'])
    expect(wrapper.text()).not.toContain('Showing the latest')
  })
})

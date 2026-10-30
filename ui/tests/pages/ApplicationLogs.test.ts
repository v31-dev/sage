import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ApplicationLogs from '@/pages/ApplicationLogs.vue'
import { fetchApplicationLogs, type LogEntry, type WorkerListItem } from '@/services/api'
import { useAppStore } from '@/stores/app'
import { followTopics, type Followed } from '../helpers/topics'

vi.mock('@/lib/updates', () => ({ watchState: vi.fn(), watchFeed: vi.fn() }))
vi.mock('vue-router', () => ({ useRoute: () => ({ params: { projectId: 'demo', appId: 'web' } }) }))
vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  fetchApplicationLogs: vi.fn(),
}))

const line = (id: number, hostname = 'w1', message = `line ${id}`, second = id): LogEntry => ({
  id,
  hostname,
  ts: `2026-01-01T12:00:${String(second).padStart(2, '0')}.000000000Z`,
  stream: 'stdout',
  message,
})

const answer = (lines: LogEntry[], more = false) => ({ lines, more })

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

type Viewer = ReturnType<typeof mount>

const messages = (wrapper: Viewer) =>
  wrapper.findAll('tbody pre').map(found => found.element.textContent)

const lastQuery = () => vi.mocked(fetchApplicationLogs).mock.lastCall![2]

// What the library does each time the socket connects: load, and hand over what came back.
async function connect(feed: Followed) {
  feed.push(await feed.load())
  await flushPromises()
}

async function open(first: LogEntry[] = [], more = false) {
  useAppStore().workers = [{ hostname: 'w1' }, { hostname: 'w2' }] as WorkerListItem[]
  const topics = followTopics()
  const wrapper = mount(ApplicationLogs, { global: { stubs } })
  await flushPromises()
  const feed = topics.topic('application-logs:demo-web')
  vi.mocked(fetchApplicationLogs).mockResolvedValueOnce(answer(first, more))
  await connect(feed)
  return { wrapper, topics, feed }
}

async function search(wrapper: Viewer, text: string) {
  const field = wrapper.get('#logs-search')
  await field.setValue(text)
  await field.trigger('change')
  await flushPromises()
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.mocked(fetchApplicationLogs).mockReset()
  vi.mocked(fetchApplicationLogs).mockResolvedValue(answer([]))
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date('2026-01-01T12:00:30Z'))
})

afterEach(() => {
  vi.useRealTimers()
})

describe('the application logs page', () => {
  it("follow the application's feed, from the newest that match since five minutes ago", async () => {
    const { wrapper, topics } = await open([
      line(1, 'w1', 'started'),
      line(2, 'w2', 'Traceback:\n  boom'),
    ])

    expect(topics.followed.map(followed => followed.topic)).toEqual(['application-logs:demo-web'])
    expect(fetchApplicationLogs).toHaveBeenCalledWith('demo', 'web', {
      hostname: '',
      search: '',
      from_ts: '2026-01-01T11:55:00.000Z',
      to_ts: '',
      after_id: 0,
      limit: 1000,
    })
    expect(messages(wrapper)).toEqual(['started', 'Traceback:\n  boom'])
  })

  it('take the lines pushed, each once, in the order they were stored', async () => {
    const { wrapper, feed } = await open([line(1)])

    feed.push([line(3)])
    feed.push([line(1), line(2)])
    await flushPromises()

    expect(messages(wrapper)).toEqual(['line 1', 'line 2', 'line 3'])
  })

  it('load after the last line held when the socket connects again', async () => {
    const { wrapper, feed } = await open([line(1), line(2)])

    vi.mocked(fetchApplicationLogs).mockResolvedValueOnce(answer([line(3)]))
    await connect(feed)

    expect(lastQuery()).toMatchObject({ after_id: 2 })
    expect(messages(wrapper)).toEqual(['line 1', 'line 2', 'line 3'])
  })

  it('leave out a pushed line from another worker once one is chosen', async () => {
    const { wrapper, feed } = await open([line(1, 'w2')])
    vi.mocked(fetchApplicationLogs).mockResolvedValueOnce(answer([line(1, 'w2')]))
    await wrapper.get('tbody button').trigger('click')
    await flushPromises()
    expect(lastQuery()).toMatchObject({ hostname: 'w2', after_id: 0 })

    feed.push([line(2, 'w1'), line(3, 'w2')])
    await flushPromises()

    expect(messages(wrapper)).toEqual(['line 1', 'line 3'])
  })

  it('offer the workers, and leave out a pushed line from another than the one chosen', async () => {
    const { wrapper, feed } = await open()
    const options = wrapper.get('[data-select]').findAll('[data-option]')
    expect(options.map(found => found.text())).toEqual(['any', 'w1', 'w2'])

    wrapper.findComponent(select).vm.$emit('update:modelValue', 'w2')
    await flushPromises()
    feed.push([line(1, 'w1'), line(2, 'w2')])
    await flushPromises()

    expect(lastQuery()).toMatchObject({ hostname: 'w2', after_id: 0 })
    expect(messages(wrapper)).toEqual(['line 2'])
  })

  it('leave out a pushed line without the text searched, matched anywhere in any case', async () => {
    const { wrapper, feed } = await open()
    await search(wrapper, 'ERROR')

    feed.push([line(1, 'w1', 'an error here'), line(2, 'w1', 'all fine'), line(3, 'w1', 'TERRORS')])
    await flushPromises()

    expect(messages(wrapper)).toEqual(['an error here', 'TERRORS'])
  })

  it('leave out a pushed line written before the window', async () => {
    const { wrapper, feed } = await open()

    feed.push([
      { ...line(1), ts: '2026-01-01T11:54:59.999999999Z' },
      { ...line(2), ts: '2026-01-01T11:55:00.000000000Z' },
    ])
    await flushPromises()

    expect(messages(wrapper)).toEqual(['line 2'])
  })

  it('drop what a load asked for the filters before the search says, its cap included', async () => {
    const { wrapper, feed } = await open()
    let answerFirst!: (value: ReturnType<typeof answer>) => void
    vi.mocked(fetchApplicationLogs)
      .mockImplementationOnce(() => new Promise(resolve => (answerFirst = resolve)))
      .mockResolvedValueOnce(answer([line(2, 'w1', 'new search')]))

    const reconnect = feed.load()
    await search(wrapper, 'new')
    answerFirst(answer([line(1, 'w1', 'new before the search')], true))
    feed.push(await reconnect)
    await flushPromises()

    expect(messages(wrapper)).toEqual(['new search'])
    expect(wrapper.text()).not.toContain('Showing the latest')
  })

  it('say when only the latest that match are shown, until a search shows them all', async () => {
    const { wrapper } = await open([line(1)], true)
    expect(wrapper.text()).toContain('Showing the latest 1000 lines that match.')

    await search(wrapper, 'line')

    expect(wrapper.text()).not.toContain('Showing the latest')
  })

  it('keep the latest 1000, and say so once the oldest are dropped', async () => {
    const { wrapper, feed } = await open(
      Array.from({ length: 1000 }, (_, index) => line(index + 1))
    )
    expect(wrapper.text()).not.toContain('Showing the latest')

    feed.push([line(1001, 'w1', 'line 1001', 59)])
    await flushPromises()

    expect(messages(wrapper)).toHaveLength(1000)
    expect([messages(wrapper)[0], messages(wrapper)[999]]).toEqual(['line 2', 'line 1001'])
    expect(wrapper.text()).toContain('Showing the latest 1000 lines that match.')
  })

  it('draw a marker of what Sage did apart from the lines of the container', async () => {
    const { wrapper } = await open([
      line(1, 'w1', 'listening'),
      { ...line(2, 'w1', 'Deployed (task t1)'), stream: 'sage' },
    ])

    const rows = wrapper.findAll('tbody tr')
    expect(messages(wrapper)).toEqual(['listening', 'Deployed (task t1)'])
    expect(rows.map(row => row.find('[data-marker]').exists())).toEqual([false, true])
    expect(rows[1]!.classes()).toContain('bg-primary/5')
    expect(rows[0]!.classes()).not.toContain('bg-primary/5')
  })

  it('stop following when the page goes away', async () => {
    const { wrapper, feed } = await open()

    wrapper.unmount()

    expect(feed.stop).toHaveBeenCalledOnce()
  })
})

import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'vue-sonner'
import { watchFeed, watchState } from '@/lib/updates'
import Tasks from '@/pages/Tasks/index.vue'
import {
  fetchTaskHistory,
  fetchTaskHistoryFilters,
  fetchTasks,
  type FinishedTask,
  type QueueTask,
  type TaskHistoryPage,
} from '@/services/api'
import { followTopics } from '../helpers/topics'

vi.mock('@/lib/updates', () => ({ watchState: vi.fn(), watchFeed: vi.fn() }))
vi.mock('vue-sonner', () => ({ toast: { error: vi.fn() } }))
vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  fetchTaskHistory: vi.fn(),
  fetchTaskHistoryFilters: vi.fn(),
}))

const table = {
  props: ['rows', 'section'],
  template: '<div data-table>{{ rows.map(row => row.task_id).join(",") }}</div>',
}
const card = { props: ['task'], template: '<div data-card>{{ task.task_id }}</div>' }
const inline = { template: '<div><slot /></div>' }
const select = {
  props: ['modelValue'],
  emits: ['update:modelValue'],
  template: '<div data-select><slot /></div>',
}
const option = { props: ['value'], template: '<div data-option>{{ value }}</div>' }
const stubs = {
  TaskTable: table,
  TaskCard: card,
  Select: select,
  SelectTrigger: inline,
  SelectValue: inline,
  SelectContent: inline,
  SelectItem: option,
}

const at = (minute: number) => `2026-01-01T12:${String(minute).padStart(2, '0')}:00`

function running(id: string): QueueTask {
  return {
    task_id: id,
    name: 'deploy',
    scopes: [],
    params: {},
    executor: 'app',
    status: 'running',
    queued_at: at(0),
    started_at: at(1),
    quiet: false,
  }
}

function finished(id: string, fields: Partial<FinishedTask> = {}): FinishedTask {
  return {
    task_id: id,
    name: 'deploy',
    scopes: ['app:demo-web'],
    params: {},
    executor: 'app',
    status: 'completed',
    created_at: at(0),
    started_at: at(0),
    updated_at: at(10),
    finished_at: at(10),
    ...fields,
  }
}

const page = (tasks: FinishedTask[], total = tasks.length): TaskHistoryPage => ({ tasks, total })

// The page as the library has it once the first page of the history has loaded.
async function mountPage(first: TaskHistoryPage = page([])) {
  const topics = followTopics()
  vi.mocked(fetchTaskHistory).mockResolvedValueOnce(first)
  const wrapper = mount(Tasks, { global: { stubs } })
  const history = topics.topic('task-history')
  await history.load()
  await flushPromises()
  return { wrapper, topics, history }
}

type Page = Awaited<ReturnType<typeof mountPage>>['wrapper']

// The three tables, in the order of the page: running, queued, finished.
const tables = (wrapper: Page) => wrapper.findAll('[data-table]').map(found => found.text())
const shown = (wrapper: Page) => tables(wrapper)[2]

const lastQuery = () => vi.mocked(fetchTaskHistory).mock.lastCall![0]

const button = (wrapper: Page, text: string) =>
  wrapper.findAll('button').find(found => found.text() === text)!

// The dropdowns, in the order of the page: name, executor, status.
async function choose(wrapper: Page, index: number, value: string) {
  wrapper.findAllComponents(select)[index]!.vm.$emit('update:modelValue', value)
  await flushPromises()
}

async function commit(wrapper: Page, id: string, value: string) {
  const field = wrapper.get(`#${id}`)
  await field.setValue(value)
  await field.trigger('change')
  await flushPromises()
}

beforeEach(() => {
  vi.mocked(toast.error).mockReset()
  vi.mocked(fetchTaskHistory).mockReset()
  vi.mocked(fetchTaskHistory).mockResolvedValue(page([]))
  vi.mocked(fetchTaskHistoryFilters).mockResolvedValue({
    names: ['deploy', 'sync_workers'],
    executors: ['app', 'platform'],
    statuses: ['completed', 'failed', 'cancelled'],
  })
})

describe('what the page follows', () => {
  it('follows the queue and the finished tasks, whose load is the first page of the history', async () => {
    const { topics } = await mountPage()

    expect(watchState).toHaveBeenCalledWith('tasks', fetchTasks, expect.anything())
    expect(watchFeed).toHaveBeenCalledWith('task-history', expect.any(Function), expect.anything())
    expect(topics.followed).toHaveLength(2)
    expect(lastQuery()).toEqual({
      name: '',
      executor: '',
      status: '',
      scope: '',
      from_ts: '',
      to_ts: '',
      limit: 50,
      offset: 0,
    })
  })

  it('stops following both when it goes away', async () => {
    const { wrapper, topics } = await mountPage()

    wrapper.unmount()

    expect(topics.topic('tasks').stop).toHaveBeenCalledOnce()
    expect(topics.topic('task-history').stop).toHaveBeenCalledOnce()
  })
})

describe('loading', () => {
  it('shows a spinner until both have loaded', async () => {
    const topics = followTopics({ pending: true })
    const wrapper = mount(Tasks, { global: { stubs } })
    await flushPromises()
    expect(wrapper.text()).toContain('Loading tasks...')

    topics.topic('tasks').loaded()
    await flushPromises()
    expect(wrapper.text()).toContain('Loading tasks...')

    topics.topic('task-history').loaded()
    await flushPromises()
    expect(wrapper.text()).not.toContain('Loading tasks...')
  })

  it('says so when they cannot be loaded, and stops the spinner', async () => {
    const topics = followTopics({ pending: true })
    const wrapper = mount(Tasks, { global: { stubs } })

    topics.topic('tasks').failed(new Error('down'))
    await flushPromises()

    expect(toast.error).toHaveBeenCalledWith('Failed to load tasks')
    expect(wrapper.text()).not.toContain('Loading tasks...')
  })

  it('offers the names, executors and statuses stored, after a choice of any', async () => {
    const { wrapper } = await mountPage()

    const options = wrapper
      .findAll('[data-select]')
      .map(found => found.findAll('[data-option]').map(each => each.text()))

    expect(options).toEqual([
      ['any', 'deploy', 'sync_workers'],
      ['any', 'app', 'platform'],
      ['any', 'completed', 'failed', 'cancelled'],
    ])
  })
})

describe('the queue', () => {
  it('shows the running and the queued tasks as they are', async () => {
    const { wrapper, topics } = await mountPage()

    topics.topic('tasks').push({ running: [running('a')], queued: [running('b'), running('c')] })
    await flushPromises()

    expect(tables(wrapper).slice(0, 2)).toEqual(['a', 'b,c'])
    expect(wrapper.text()).toContain('Running (1)')
    expect(wrapper.text()).toContain('Queued (2)')
  })

  it('shows what the queue holds now, not what it held before', async () => {
    const { wrapper, topics } = await mountPage()
    topics.topic('tasks').push({ running: [running('a')], queued: [] })

    topics.topic('tasks').push({ running: [], queued: [running('b')] })
    await flushPromises()

    expect(tables(wrapper).slice(0, 2)).toEqual(['', 'b'])
  })
})

describe('the finished tasks', () => {
  it('show a page and how many match', async () => {
    const { wrapper } = await mountPage(page([finished('t2'), finished('t1')], 312))

    expect(shown(wrapper)).toBe('t2,t1')
    expect(wrapper.text()).toContain('Finished (312)')
    expect(wrapper.text()).toContain('1–50 of 312')
  })

  it('are loaded from page 1 with each filter chosen or committed', async () => {
    const { wrapper } = await mountPage(page([], 120))
    await button(wrapper, 'Next').trigger('click')
    await flushPromises()

    await choose(wrapper, 0, 'deploy')
    expect(lastQuery()).toMatchObject({ name: 'deploy', offset: 0 })
    await choose(wrapper, 1, 'platform')
    await choose(wrapper, 2, 'failed')
    await commit(wrapper, 'task-scope', ' app:demo-web ')
    await commit(wrapper, 'task-from', '2026-01-01T12:00')
    await commit(wrapper, 'task-to', '2026-01-01T13:30')

    expect(lastQuery()).toEqual({
      name: 'deploy',
      executor: 'platform',
      status: 'failed',
      scope: 'app:demo-web',
      from_ts: '2026-01-01T12:00:00.000Z',
      to_ts: '2026-01-01T13:30:00.000Z',
      limit: 50,
      offset: 0,
    })
  })

  it('page by 50, with Previous and Next only where there is a page', async () => {
    const { wrapper } = await mountPage(page([], 120))
    const disabled = (text: string) => button(wrapper, text).attributes('disabled') !== undefined
    expect([disabled('Previous'), disabled('Next')]).toEqual([true, false])

    vi.mocked(fetchTaskHistory).mockResolvedValue(page([], 120))
    await button(wrapper, 'Next').trigger('click')
    await button(wrapper, 'Next').trigger('click')
    await flushPromises()

    expect(lastQuery()).toMatchObject({ offset: 100 })
    expect(wrapper.text()).toContain('101–120 of 120')
    expect([disabled('Previous'), disabled('Next')]).toEqual([false, true])

    await button(wrapper, 'Previous').trigger('click')
    await flushPromises()
    expect(lastQuery()).toMatchObject({ offset: 50 })
  })

  it('show the page loaded last, whichever answer comes first', async () => {
    const { wrapper } = await mountPage()
    let answerFirst!: (value: TaskHistoryPage) => void
    vi.mocked(fetchTaskHistory)
      .mockImplementationOnce(() => new Promise(resolve => (answerFirst = resolve)))
      .mockResolvedValueOnce(page([finished('newer')]))

    await choose(wrapper, 0, 'deploy')
    await choose(wrapper, 0, 'sync_workers')
    answerFirst(page([finished('older')]))
    await flushPromises()

    expect(shown(wrapper)).toBe('newer')
  })
})

describe('what the manager pushes onto page 1', () => {
  it('is added newest finished first and counted, each task once', async () => {
    const { wrapper, history } = await mountPage(page([finished('t2', { finished_at: at(20) })], 1))

    history.push([finished('t1', { finished_at: at(10) }), finished('t3', { finished_at: at(30) })])
    history.push([finished('t3', { finished_at: at(30) })])
    await flushPromises()

    expect(shown(wrapper)).toBe('t3,t2,t1')
    expect(wrapper.text()).toContain('Finished (3)')
  })

  it('keeps the newest 50', async () => {
    const tasks = Array.from({ length: 50 }, (_, index) =>
      finished(`t${index}`, { finished_at: `2026-01-01T12:00:${String(index).padStart(2, '0')}` })
    ).reverse()
    const { wrapper, history } = await mountPage(page(tasks, 80))

    history.push([finished('newest', { finished_at: at(59) })])
    await flushPromises()

    const ids = shown(wrapper)!.split(',')
    expect(ids).toHaveLength(50)
    expect([ids[0], ids[49]]).toEqual(['newest', 't1'])
    expect(wrapper.text()).toContain('Finished (81)')
  })

  it.each([
    ['another name', { name: 'sync_workers' }],
    ['another executor', { executor: 'platform' }],
    ['another status', { status: 'failed' }],
    ['a sibling scope', { scopes: ['app:demo-web2'] }],
    ['a parent scope', { scopes: ['app'] }],
    ['finished before the window', { finished_at: '2026-01-01T11:59:59' }],
    ['queued after the window', { created_at: '2026-01-01T13:30:01' }],
  ])('is left out when it has %s', async (_case, fields) => {
    const { wrapper, history } = await mountPage()
    await choose(wrapper, 0, 'deploy')
    await choose(wrapper, 1, 'app')
    await choose(wrapper, 2, 'completed')
    await commit(wrapper, 'task-scope', 'app:demo-web')
    await commit(wrapper, 'task-from', '2026-01-01T12:00')
    await commit(wrapper, 'task-to', '2026-01-01T13:30')

    history.push([finished('in'), finished('out', fields)])
    await flushPromises()

    expect(shown(wrapper)).toBe('in')
  })

  it('is let through by a scope that is its parent, and by a window it was active in', async () => {
    const { wrapper, history } = await mountPage()
    await commit(wrapper, 'task-scope', 'app')
    await commit(wrapper, 'task-from', '2026-01-01T12:05')
    await commit(wrapper, 'task-to', '2026-01-01T12:05')

    history.push([finished('across', { scopes: ['platform', 'app:demo-web'] })])
    await flushPromises()

    expect(shown(wrapper)).toBe('across')
  })

  it('is let through when it finished as the window opens, or was queued as it closes', async () => {
    const { wrapper, history } = await mountPage()
    await commit(wrapper, 'task-from', '2026-01-01T12:00')
    await commit(wrapper, 'task-to', '2026-01-01T13:30')

    history.push([
      finished('opens', { created_at: '2026-01-01T11:00:00', finished_at: '2026-01-01T12:00:00' }),
      finished('closes', { created_at: '2026-01-01T13:30:00', finished_at: '2026-01-01T14:00:00' }),
    ])
    await flushPromises()

    expect(shown(wrapper)).toBe('closes,opens')
  })

  it('is kept when it arrives while page 1 loads', async () => {
    const { wrapper, history } = await mountPage()
    let answer!: (value: TaskHistoryPage) => void
    vi.mocked(fetchTaskHistory).mockImplementationOnce(
      () => new Promise(resolve => (answer = resolve))
    )

    await choose(wrapper, 0, 'deploy')
    history.push([finished('pushed', { finished_at: at(30) })])
    answer(page([finished('loaded', { finished_at: at(20) })], 1))
    await flushPromises()

    expect(shown(wrapper)).toBe('pushed,loaded')
    expect(wrapper.text()).toContain('Finished (2)')
  })

  it('changes nothing on a later page', async () => {
    const { wrapper, history } = await mountPage(page([], 120))
    vi.mocked(fetchTaskHistory).mockResolvedValue(page([finished('on-page-2')], 120))
    await button(wrapper, 'Next').trigger('click')
    await flushPromises()

    history.push([finished('pushed', { finished_at: at(30) })])
    await flushPromises()

    expect(shown(wrapper)).toBe('on-page-2')
    expect(wrapper.text()).toContain('Finished (120)')
  })

  it('is loaded again on page 1 when the socket connects again, and not on a later page', async () => {
    const { wrapper, history } = await mountPage(page([], 120))
    vi.mocked(fetchTaskHistory).mockResolvedValue(page([], 120))
    await history.load()
    expect(fetchTaskHistory).toHaveBeenCalledTimes(2)

    await button(wrapper, 'Next').trigger('click')
    await flushPromises()
    await history.load()

    expect(fetchTaskHistory).toHaveBeenCalledTimes(3)
  })
})

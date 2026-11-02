import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { watchState } from '@/lib/updates'
import Upgrade from '@/pages/Settings/Upgrade.vue'
import {
  fetchRelease,
  fetchTasks,
  refreshRelease,
  type AppInfo,
  type QueueTask,
  type Release,
} from '@/services/api'
import { useAppStore } from '@/stores/app'
import { followTopics } from '../helpers/topics'

vi.mock('@/lib/updates', () => ({ watchState: vi.fn(), watchFeed: vi.fn() }))
vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  refreshRelease: vi.fn(),
}))

const refresh = vi.mocked(refreshRelease)

function release(fetchedAt: string | null, overrides: Partial<Release> = {}): Release {
  return {
    version: '1.2.3',
    name: 'Sage 1.2.3',
    body: 'What changed',
    html_url: null,
    published_at: '2026-10-20T10:00:00Z',
    fetched_at: fetchedAt,
    ...overrides,
  }
}

function running(id: string, name = 'deploy'): QueueTask {
  return {
    task_id: id,
    name,
    scopes: [],
    params: {},
    executor: 'app',
    status: 'running',
    queued_at: '2026-01-01T12:00:00',
    started_at: '2026-01-01T12:00:00',
    quiet: false,
  }
}

async function mountPage(state: AppInfo['state'] = 'running', options: { pending?: boolean } = {}) {
  const topics = followTopics(options)
  const store = useAppStore()
  store.info = { version: '1.0.0', latest_version: '1.2.3', state } as AppInfo
  const wrapper = mount(Upgrade)
  await flushPromises()
  return { wrapper, topics }
}

const checkButton = (wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper']) =>
  wrapper.findAll('button').find(button => button.text().includes('Check for updates'))!

beforeEach(() => {
  setActivePinia(createPinia())
  refresh.mockResolvedValue(undefined)
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('what the page follows', () => {
  it('follows the release and the tasks, each loaded from its route', async () => {
    await mountPage()

    expect(watchState).toHaveBeenCalledWith('release', fetchRelease, expect.anything())
    expect(watchState).toHaveBeenCalledWith('tasks', fetchTasks, expect.anything())
  })

  it('stops following both when it goes away', async () => {
    const { wrapper, topics } = await mountPage()

    wrapper.unmount()

    expect(topics.topic('release').stop).toHaveBeenCalledOnce()
    expect(topics.topic('tasks').stop).toHaveBeenCalledOnce()
  })
})

describe('the release notes', () => {
  it('show a placeholder until the release has loaded, and then the release', async () => {
    const { wrapper, topics } = await mountPage('running', { pending: true })
    expect(wrapper.text()).not.toContain('Sage 1.2.3')

    topics.topic('release').push(release('t1'))
    topics.topic('release').loaded()
    await flushPromises()

    expect(wrapper.text()).toContain('Sage 1.2.3')
  })

  it('show the next release when the manager pushes one', async () => {
    const { wrapper, topics } = await mountPage()
    topics.topic('release').push(release('t1'))

    topics.topic('release').push(release('t2', { name: 'Sage 1.3.0' }))
    await flushPromises()

    expect(wrapper.text()).toContain('Sage 1.3.0')
    expect(wrapper.text()).not.toContain('Sage 1.2.3')
  })

  it('say why they could not be loaded', async () => {
    const { wrapper, topics } = await mountPage('running', { pending: true })

    topics.topic('release').failed(new Error('No release known yet'))
    await flushPromises()

    expect(wrapper.text()).toContain('No release known yet')
  })
})

describe('checking for updates', () => {
  it('is over when a release fetched after the click is pushed', async () => {
    const { wrapper, topics } = await mountPage()
    topics.topic('release').push(release('t1'))
    await flushPromises()

    await checkButton(wrapper).trigger('click')
    await flushPromises()
    expect(refresh).toHaveBeenCalledOnce()
    expect(checkButton(wrapper).attributes('disabled')).toBeDefined()

    topics.topic('release').push(release('t2'))
    await flushPromises()
    expect(checkButton(wrapper).attributes('disabled')).toBeUndefined()
    expect(wrapper.text()).not.toContain('timed out')
  })

  it('goes on when the release that is pushed is the one it had', async () => {
    const { wrapper, topics } = await mountPage()
    topics.topic('release').push(release('t1'))
    await flushPromises()
    await checkButton(wrapper).trigger('click')
    await flushPromises()

    topics.topic('release').push(release('t1', { name: 'Sage 1.2.3 again' }))
    await flushPromises()

    expect(checkButton(wrapper).attributes('disabled')).toBeDefined()
  })

  it('takes the first release pushed when it had none', async () => {
    const { wrapper, topics } = await mountPage()
    topics.topic('release').push(release(null))
    await flushPromises()
    await checkButton(wrapper).trigger('click')
    await flushPromises()

    topics.topic('release').push(release('t1'))
    await flushPromises()

    expect(checkButton(wrapper).attributes('disabled')).toBeUndefined()
  })

  it('gives up after a minute', async () => {
    const { wrapper, topics } = await mountPage()
    topics.topic('release').push(release('t1'))
    await flushPromises()
    await checkButton(wrapper).trigger('click')
    await flushPromises()

    await vi.advanceTimersByTimeAsync(59_000)
    expect(checkButton(wrapper).attributes('disabled')).toBeDefined()
    await vi.advanceTimersByTimeAsync(1_000)

    expect(wrapper.text()).toContain('Refresh timed out. Please try again.')
    expect(checkButton(wrapper).attributes('disabled')).toBeUndefined()
  })

  it('says why it failed, and does not time out afterwards', async () => {
    refresh.mockRejectedValue(new Error('Failed to refresh release'))
    const { wrapper } = await mountPage()

    await checkButton(wrapper).trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(61_000)

    expect(wrapper.text()).toContain('Failed to refresh release')
    expect(wrapper.text()).not.toContain('timed out')
    expect(checkButton(wrapper).attributes('disabled')).toBeUndefined()
  })

  it('does not time out once it is over', async () => {
    const { wrapper, topics } = await mountPage()
    topics.topic('release').push(release('t1'))
    await flushPromises()
    await checkButton(wrapper).trigger('click')
    await flushPromises()
    topics.topic('release').push(release('t2'))
    await flushPromises()

    await vi.advanceTimersByTimeAsync(61_000)

    expect(wrapper.text()).not.toContain('timed out')
  })

  it('does not cut a second check short with the timer of the first', async () => {
    const { wrapper, topics } = await mountPage()
    topics.topic('release').push(release('t1'))
    await flushPromises()
    await checkButton(wrapper).trigger('click')
    await flushPromises()
    topics.topic('release').push(release('t2'))
    await flushPromises()
    await vi.advanceTimersByTimeAsync(30_000)

    await checkButton(wrapper).trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(wrapper.text()).not.toContain('timed out')
    expect(checkButton(wrapper).attributes('disabled')).toBeDefined()

    await vi.advanceTimersByTimeAsync(30_000)
    expect(wrapper.text()).toContain('Refresh timed out. Please try again.')
  })

  it('does not cut a check short with the timer of one that failed', async () => {
    refresh.mockRejectedValueOnce(new Error('Failed to refresh release'))
    const { wrapper } = await mountPage()
    await checkButton(wrapper).trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(30_000)

    await checkButton(wrapper).trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(30_000)

    expect(wrapper.text()).not.toContain('timed out')
    expect(checkButton(wrapper).attributes('disabled')).toBeDefined()
  })

  it('leaves no timer behind when the page goes away', async () => {
    const { wrapper } = await mountPage()
    await checkButton(wrapper).trigger('click')
    await flushPromises()

    wrapper.unmount()

    expect(vi.getTimerCount()).toBe(0)
  })
})

describe('preparing to stop', () => {
  it('waits for the running tasks, naming them', async () => {
    const { wrapper, topics } = await mountPage('paused')

    topics
      .topic('tasks')
      .push({ running: [running('a', 'deploy'), running('b', 'backup')], queued: [] })
    await flushPromises()

    expect(wrapper.text()).toContain('Waiting for 2 running task(s) to finish')
    expect(wrapper.text()).toContain('deploy')
    expect(wrapper.text()).toContain('backup')
    expect(wrapper.text()).not.toContain('Safe to stop.')
  })

  it('is safe to stop once none is running, and says what the queued ones will do', async () => {
    const { wrapper, topics } = await mountPage('paused')
    topics.topic('tasks').push({ running: [running('a')], queued: [] })

    topics.topic('tasks').push({ running: [], queued: [running('b'), running('c')] })
    await flushPromises()

    expect(wrapper.text()).toContain('Safe to stop.')
    expect(wrapper.text()).toContain('2 queued task(s) will be cancelled when Sage stops.')
  })

  it('says nothing about tasks while Sage is running', async () => {
    const { wrapper, topics } = await mountPage('running')

    topics.topic('tasks').push({ running: [running('a')], queued: [] })
    await flushPromises()

    expect(wrapper.text()).toContain('Sage is running normally.')
    expect(wrapper.text()).not.toContain('Waiting for')
  })
})

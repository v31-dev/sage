import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import RestoreOverlay from '@/components/layout/RestoreOverlay.vue'
import { watchState } from '@/lib/updates'
import { fetchTasks, type AppInfo, type QueueTask } from '@/services/api'
import { useAppStore } from '@/stores/app'
import { followTopics } from '../helpers/topics'

vi.mock('@/lib/updates', () => ({ watchState: vi.fn(), watchFeed: vi.fn() }))

const open = { props: ['open'], template: '<div v-if="open"><slot /></div>' }
const inline = { template: '<div><slot /></div>' }
const stubs = {
  AlertDialog: open,
  AlertDialogContent: inline,
  AlertDialogHeader: inline,
  AlertDialogTitle: inline,
  AlertDialogDescription: inline,
}

function task(id: string, name: string): QueueTask {
  return {
    task_id: id,
    name,
    scopes: [],
    params: {},
    executor: 'platform',
    status: 'running',
    queued_at: '2026-01-01T12:00:00',
    started_at: '2026-01-01T12:00:00',
    quiet: false,
  }
}

const reload = vi.fn()

function setup(state: AppInfo['state'], restoringFrom: string | null = null) {
  const topics = followTopics()
  const store = useAppStore()
  store.info = { state, restoring_from: restoringFrom } as AppInfo
  const wrapper = mount(RestoreOverlay, { global: { stubs } })
  return { wrapper, topics, store }
}

beforeEach(() => {
  setActivePinia(createPinia())
  reload.mockReset()
  vi.stubGlobal('location', { reload })
})

describe('while Sage is not restoring', () => {
  it('shows nothing and follows no tasks', async () => {
    const { wrapper, topics } = setup('running')
    await flushPromises()

    expect(wrapper.text()).toBe('')
    expect(topics.followed).toHaveLength(0)
    expect(watchState).not.toHaveBeenCalled()
  })
})

describe('while Sage is restoring', () => {
  it('says which backup, and follows the running tasks loaded from their route', async () => {
    const { wrapper, topics } = setup('restoring', '2026-10-20T10:00:00')
    await flushPromises()

    expect(wrapper.text()).toContain('Restoring Sage')
    expect(wrapper.text()).toContain('Restoring the backup of')
    expect(watchState).toHaveBeenCalledWith('tasks', fetchTasks, expect.anything())
    expect(topics.followed).toHaveLength(1)
  })

  it('says it restores a backup when it does not know which', async () => {
    const { wrapper } = setup('restoring')
    await flushPromises()

    expect(wrapper.text()).toContain('Restoring a backup.')
  })

  it('counts the running tasks it waits for, not the restore itself', async () => {
    const { wrapper, topics } = setup('restoring')
    await flushPromises()

    topics.topic('tasks').push({
      running: [task('r', 'restore_database_from_s3'), task('a', 'deploy'), task('b', 'backup')],
      queued: [],
    })
    await flushPromises()
    expect(wrapper.text()).toContain('Waiting for 2 running tasks to finish first.')

    topics
      .topic('tasks')
      .push({ running: [task('r', 'restore_database_from_s3'), task('a', 'deploy')], queued: [] })
    await flushPromises()
    expect(wrapper.text()).toContain('Waiting for 1 running task to finish first.')

    topics.topic('tasks').push({ running: [task('r', 'restore_database_from_s3')], queued: [] })
    await flushPromises()
    expect(wrapper.text()).not.toContain('Waiting for')
  })

  it('follows the running tasks when a restore starts, and stops when it is over', async () => {
    const { topics, store } = setup('running')
    await flushPromises()

    store.info = { ...store.info!, state: 'restoring' }
    await nextTick()
    expect(topics.followed).toHaveLength(1)
    expect(topics.topic('tasks').stop).not.toHaveBeenCalled()

    store.info = { ...store.info!, state: 'running' }
    await nextTick()
    expect(topics.topic('tasks').stop).toHaveBeenCalledOnce()
  })

  it('reloads the page when the restore is over', async () => {
    const { store } = setup('restoring')
    await flushPromises()
    expect(reload).not.toHaveBeenCalled()

    store.info = { ...store.info!, state: 'running' }
    await nextTick()

    expect(reload).toHaveBeenCalledOnce()
  })

  it('does not reload a page that was never restoring', async () => {
    const { store } = setup('running')
    store.info = { ...store.info!, state: 'paused' }
    await nextTick()

    expect(reload).not.toHaveBeenCalled()
  })

  it('stops following the tasks when it goes away', async () => {
    const { wrapper, topics } = setup('restoring')
    await flushPromises()

    wrapper.unmount()

    expect(topics.topic('tasks').stop).toHaveBeenCalledOnce()
  })
})

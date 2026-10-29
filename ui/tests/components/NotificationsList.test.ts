import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import NotificationsList from '@/components/NotificationsList.vue'
import { watchFeed } from '@/lib/updates'
import { fetchNotifications, type Notification } from '@/services/api'

vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  fetchNotifications: vi.fn(),
}))
vi.mock('@/lib/updates', () => ({ watchFeed: vi.fn() }))

const watch = vi.mocked(watchFeed)
const fetchMore = vi.mocked(fetchNotifications)
const LAST_SEEN_KEY = 'lastSeenNotificationId'

const stop = vi.fn()
let push: (rows: Notification[]) => void
let loadNewest: () => Promise<Notification[]>

function notification(id: number): Notification {
  const at = `2026-10-28T10:00:${String(id).padStart(2, '0')}`
  return { id, type: 'info', content: `message ${id}.`, link: null, created_at: at, updated_at: at }
}

const rows = (...ids: number[]) => ids.map(notification)

const inline = { template: '<div><slot /></div>' }

async function mountList() {
  const wrapper = mount(NotificationsList, {
    global: { stubs: { Popover: inline, PopoverTrigger: inline, PopoverContent: inline } },
  })
  await flushPromises()
  return wrapper
}

const dot = (wrapper: Awaited<ReturnType<typeof mountList>>) =>
  wrapper.find('[aria-label="New notifications"]')

async function open(wrapper: Awaited<ReturnType<typeof mountList>>) {
  await wrapper.get('button').trigger('click')
  await flushPromises()
}

const shown = (wrapper: Awaited<ReturnType<typeof mountList>>) =>
  wrapper.text().match(/message \d+(?=\.)/g) ?? []

beforeEach(() => {
  localStorage.clear()
  fetchMore.mockResolvedValue([])
  watch.mockImplementation(((
    _topic: string,
    load: () => Promise<Notification[]>,
    onData: (rows: Notification[]) => void
  ) => {
    loadNewest = load
    push = onData
    return { stop, first: Promise.resolve() }
  }) as unknown as typeof watchFeed)
})

describe('the notifications', () => {
  it('follows the notifications topic and stops when it goes away', async () => {
    const wrapper = await mountList()
    expect(watch).toHaveBeenCalledWith('notifications', expect.anything(), expect.anything())

    wrapper.unmount()

    expect(stop).toHaveBeenCalledOnce()
  })

  it('asks for the newest page while it has none', async () => {
    await mountList()
    fetchMore.mockResolvedValue(rows(2, 1))

    await expect(loadNewest()).resolves.toEqual(rows(2, 1))

    expect(fetchMore).toHaveBeenCalledWith({ limit: 20 })
  })

  it('asks for what came after its newest once it has some', async () => {
    await mountList()
    push(rows(3, 2, 1))

    await loadNewest()

    expect(fetchMore).toHaveBeenCalledWith({ from_ts: notification(3).created_at })
  })

  it('lists what it starts with, newest first', async () => {
    const wrapper = await mountList()
    push(rows(3, 2, 1))

    await open(wrapper)

    expect(shown(wrapper)).toEqual(['message 3', 'message 2', 'message 1'])
  })

  it('says so when there are none', async () => {
    const wrapper = await mountList()
    push([])

    await open(wrapper)

    expect(wrapper.text()).toContain('No notifications')
  })

  it('puts a pushed row on top and does not repeat one it has', async () => {
    const wrapper = await mountList()
    push(rows(2, 1))
    push(rows(3))
    push(rows(3, 2, 1))

    await open(wrapper)

    expect(shown(wrapper)).toEqual(['message 3', 'message 2', 'message 1'])
  })

  it('sorts a row that arrives out of order into place', async () => {
    const wrapper = await mountList()
    push(rows(5, 3, 1))
    push(rows(4))
    push(rows(2))

    await open(wrapper)

    expect(shown(wrapper)).toEqual([
      'message 5',
      'message 4',
      'message 3',
      'message 2',
      'message 1',
    ])
  })

  it('loads older ones after the last it has and ignores the ones it has', async () => {
    const wrapper = await mountList()
    push(rows(3, 2))
    fetchMore.mockResolvedValue(rows(2, 1))
    await open(wrapper)

    await wrapper.get('button[class*="w-full"]').trigger('click')
    await flushPromises()

    expect(fetchMore).toHaveBeenCalledWith({ limit: 20, to_ts: notification(2).created_at })
    expect(shown(wrapper)).toEqual(['message 3', 'message 2', 'message 1'])
  })
})

describe('the marker for new notifications', () => {
  it('is not shown when there are no notifications', async () => {
    const wrapper = await mountList()
    push([])
    await flushPromises()

    expect(dot(wrapper).exists()).toBe(false)
  })

  it('is shown for notifications that were never seen', async () => {
    const wrapper = await mountList()
    push(rows(2, 1))
    await flushPromises()

    expect(dot(wrapper).exists()).toBe(true)
  })

  it('goes away when the bell is opened, and comes back for a newer one', async () => {
    const wrapper = await mountList()
    push(rows(2, 1))
    await flushPromises()

    await open(wrapper)
    expect(dot(wrapper).exists()).toBe(false)

    push(rows(3))
    await flushPromises()
    expect(dot(wrapper).exists()).toBe(true)
  })

  it('is kept as the newest id seen', async () => {
    const wrapper = await mountList()
    push(rows(7, 6))
    await open(wrapper)

    expect(localStorage.getItem(LAST_SEEN_KEY)).toBe('7')
  })

  it('stays away on the next visit until something newer arrives', async () => {
    localStorage.setItem(LAST_SEEN_KEY, '3')
    const wrapper = await mountList()
    push(rows(3, 2, 1))
    await flushPromises()
    expect(dot(wrapper).exists()).toBe(false)

    push(rows(4))
    await flushPromises()
    expect(dot(wrapper).exists()).toBe(true)
  })

  it('does not depend on the clock of the browser', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2020-01-01T00:00:00Z'))
    try {
      const wrapper = await mountList()
      push(rows(2, 1))
      await open(wrapper)
      expect(dot(wrapper).exists()).toBe(false)

      vi.setSystemTime(new Date('2040-01-01T00:00:00Z'))
      push(rows(3))
      await flushPromises()
      expect(dot(wrapper).exists()).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('ignores a stored value that is not an id', async () => {
    localStorage.setItem(LAST_SEEN_KEY, 'soon')
    const wrapper = await mountList()
    push(rows(1))
    await flushPromises()

    expect(dot(wrapper).exists()).toBe(true)
  })
})

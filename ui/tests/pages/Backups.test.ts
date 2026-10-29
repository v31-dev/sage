import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'vue-sonner'
import { watchState } from '@/lib/updates'
import Backups from '@/pages/Backup/index.vue'
import { fetchBackups, type Backup } from '@/services/api'
import { followTopics } from '../helpers/topics'

vi.mock('@/lib/updates', () => ({ watchState: vi.fn(), watchFeed: vi.fn() }))
vi.mock('vue-sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  fetchBackups: vi.fn(),
}))

const item = {
  props: ['backup', 'loadBackups'],
  template: '<button data-backup @click="loadBackups()">{{ backup.s3_path }}</button>',
}

const backup = (id: number) => ({ id, type: 'platform', s3_path: `b${id}` }) as unknown as Backup

async function mountPage() {
  const topics = followTopics({ pending: true })
  const wrapper = mount(Backups, {
    global: {
      stubs: {
        BackupsItem: item,
        CreateBackupButton: true,
        DiscoverBackupsButton: true,
        SectionTitle: true,
      },
    },
  })
  await flushPromises()
  return { wrapper, topics, backups: topics.topic('backups') }
}

const listed = (wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper']) =>
  wrapper.findAll('[data-backup]').map(found => found.text())

beforeEach(() => {
  vi.mocked(fetchBackups).mockReset()
  vi.mocked(toast.error).mockReset()
})

describe('what the page follows', () => {
  it('follows the backups, loaded from their route, and stops when it goes away', async () => {
    const { wrapper, backups } = await mountPage()
    expect(watchState).toHaveBeenCalledWith('backups', expect.any(Function), expect.any(Function))
    vi.mocked(fetchBackups).mockResolvedValue([backup(1)])
    await backups.load()
    expect(fetchBackups).toHaveBeenCalledOnce()

    wrapper.unmount()

    expect(backups.stop).toHaveBeenCalledOnce()
  })
})

describe('loading', () => {
  it('shows a spinner until the backups have loaded, then them, newest first', async () => {
    const { wrapper, backups } = await mountPage()
    expect(wrapper.text()).toContain('Loading backups...')

    backups.push([backup(1), backup(2)])
    backups.loaded()
    await flushPromises()

    expect(wrapper.text()).not.toContain('Loading backups...')
    expect(listed(wrapper)).toEqual(['b2', 'b1'])
  })

  it('says so when they could not be loaded', async () => {
    const { wrapper, backups } = await mountPage()

    backups.failed(new Error('down'))
    await flushPromises()

    expect(toast.error).toHaveBeenCalledWith('Failed to load backups')
    expect(wrapper.text()).not.toContain('Loading backups...')
  })
})

describe('what the manager pushes', () => {
  it('shows each pushed list, newest first, without the spinner', async () => {
    const { wrapper, backups } = await mountPage()
    backups.push([backup(1)])
    backups.loaded()
    await flushPromises()

    backups.push([backup(1), backup(2), backup(3)])
    await flushPromises()

    expect(listed(wrapper)).toEqual(['b3', 'b2', 'b1'])
    expect(wrapper.text()).not.toContain('Loading backups...')
  })

  it('loads the list again at once after a backup is deleted here, without waiting for the push', async () => {
    const { wrapper, backups } = await mountPage()
    backups.push([backup(1), backup(2)])
    backups.loaded()
    await flushPromises()
    vi.mocked(fetchBackups).mockResolvedValue([backup(1)])

    await wrapper.find('[data-backup]').trigger('click')
    await flushPromises()

    expect(listed(wrapper)).toEqual(['b1'])
  })
})

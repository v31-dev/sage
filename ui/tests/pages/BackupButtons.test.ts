import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'vue-sonner'
import { watchState } from '@/lib/updates'
import CreateBackupButton from '@/pages/Backup/CreateBackupButton.vue'
import DiscoverBackupsButton from '@/pages/Backup/DiscoverBackupsButton.vue'
import type { BackupStatus } from '@/services/api'
import { followTopics } from '../helpers/topics'

vi.mock('@/lib/updates', () => ({ watchState: vi.fn(), watchFeed: vi.fn() }))
vi.mock('vue-sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const status = (backup: boolean, discovery: boolean): BackupStatus => ({
  platform_backup_in_progress: backup,
  discovery_in_progress: discovery,
})

function setup(component: typeof CreateBackupButton | typeof DiscoverBackupsButton) {
  const topics = followTopics()
  const backupAPI = { action: vi.fn(), create: vi.fn() }
  const wrapper = mount(component, { props: { backupAPI } })
  return { wrapper, topics, backupAPI, backups: topics.topic('backup-status') }
}

const busy = (wrapper: ReturnType<typeof setup>['wrapper']) =>
  wrapper.find('.animate-spin').exists()
const disabled = (wrapper: ReturnType<typeof setup>['wrapper']) =>
  wrapper.get('button').attributes('disabled') !== undefined

beforeEach(() => {
  vi.mocked(toast.success).mockReset()
  vi.mocked(toast.error).mockReset()
})

describe.each([
  ['Create Backup', CreateBackupButton, 'backup'],
  ['Discover Backups', DiscoverBackupsButton, 'discovery'],
] as const)('the %s button, as to the backup status it follows', (_name, component, which) => {
  const inProgress = (on: boolean) => (which === 'backup' ? status(on, false) : status(false, on))
  const other = (on: boolean) => (which === 'backup' ? status(false, on) : status(on, false))

  it('follows the backup status, loaded from the status route of the backups', async () => {
    const { backups, backupAPI } = setup(component)

    expect(watchState).toHaveBeenCalledWith('backup-status', expect.anything(), expect.anything())
    backupAPI.action.mockResolvedValue(status(false, false))
    await backups.load()

    expect(backupAPI.action).toHaveBeenCalledWith('platform_backup_status')
  })

  it('stops following when it goes away', () => {
    const { wrapper, backups } = setup(component)

    wrapper.unmount()

    expect(backups.stop).toHaveBeenCalledOnce()
  })

  it('shows its own work while it is in progress, wherever it was started', async () => {
    const { wrapper, backups } = setup(component)
    expect([busy(wrapper), disabled(wrapper)]).toEqual([false, false])

    backups.push(inProgress(true))
    await flushPromises()
    expect([busy(wrapper), disabled(wrapper)]).toEqual([true, true])

    backups.push(inProgress(false))
    await flushPromises()
    expect([busy(wrapper), disabled(wrapper)]).toEqual([false, false])
  })

  it('does not show the other work as its own', async () => {
    const { wrapper, backups } = setup(component)

    backups.push(other(true))
    await flushPromises()

    expect([busy(wrapper), disabled(wrapper)]).toEqual([false, false])
  })
})

describe('the Create Backup button', () => {
  it('shows the work at once when clicked, and says it started', async () => {
    const { wrapper, backupAPI } = setup(CreateBackupButton)
    backupAPI.create.mockResolvedValue(undefined)

    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(backupAPI.create).toHaveBeenCalledOnce()
    expect(toast.success).toHaveBeenCalledWith('Backup initiated successfully')
    expect(busy(wrapper)).toBe(true)
  })

  it('gives the button back, saying why, when the backup could not be started', async () => {
    const { wrapper, backupAPI } = setup(CreateBackupButton)
    backupAPI.create.mockRejectedValue(new Error('S3 is not configured.'))

    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(toast.error).toHaveBeenCalledWith('S3 is not configured.')
    expect(disabled(wrapper)).toBe(false)
  })

  it('logs a status that could not be loaded', async () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    const topics = followTopics({ pending: true })
    mount(CreateBackupButton, { props: { backupAPI: {} } })

    topics.topic('backup-status').failed(new Error('down'))
    await flushPromises()

    expect(error).toHaveBeenCalledWith('Error checking backup status:', expect.any(Error))
  })
})

describe('the Discover Backups button', () => {
  it('shows the work at once when clicked, and says it started', async () => {
    const { wrapper, backupAPI } = setup(DiscoverBackupsButton)
    backupAPI.action.mockResolvedValue({})

    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(backupAPI.action).toHaveBeenCalledWith('discover')
    expect(toast.success).toHaveBeenCalledWith('Looking for backups in S3')
    expect(busy(wrapper)).toBe(true)
  })

  it('gives the button back, saying why, when the search could not be started', async () => {
    const { wrapper, backupAPI } = setup(DiscoverBackupsButton)
    backupAPI.action.mockRejectedValue(new Error('S3 is not configured.'))

    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(toast.error).toHaveBeenCalledWith('S3 is not configured.')
    expect(disabled(wrapper)).toBe(false)
  })

  it('goes on when a status without the search arrives before the search is queued', async () => {
    const { wrapper, backupAPI, backups } = setup(DiscoverBackupsButton)
    backupAPI.action.mockResolvedValue({})
    await wrapper.get('button').trigger('click')
    await flushPromises()

    backups.push(status(false, true))
    await flushPromises()
    expect(busy(wrapper)).toBe(true)

    backups.push(status(false, false))
    await flushPromises()
    expect(busy(wrapper)).toBe(false)
  })
})

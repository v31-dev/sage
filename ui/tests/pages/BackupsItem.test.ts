import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'vue-sonner'
import BackupsItem from '@/pages/Backup/BackupsItem.vue'
import { deleteBackup, type Backup } from '@/services/api'

vi.mock('vue-sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  deleteBackup: vi.fn(),
}))

// Confirmed as soon as it is clicked.
const confirmation = {
  props: ['triggerText', 'onConfirm'],
  template: '<button @click="onConfirm()">{{ triggerText }}</button>',
}

const backup = (fields: Partial<Backup> = {}) =>
  ({
    id: 'b1',
    type: 'platform',
    s3_path: 'base/backups/platform/b1.db.enc',
    automatic: true,
    status: 'succeeded',
    reason: null,
    created_at: '2026-01-01T12:00:00',
    ...fields,
  }) as Backup

function mountItem(fields: Partial<Backup> = {}) {
  const loadBackups = vi.fn(async () => {})
  const wrapper = mount(BackupsItem, {
    props: { backup: backup(fields), loadBackups },
    global: { stubs: { ConfirmationButton: confirmation } },
  })
  return { wrapper, loadBackups }
}

const buttons = (wrapper: ReturnType<typeof mountItem>['wrapper']) =>
  wrapper.findAll('button').map(button => button.text())

beforeEach(() => {
  setActivePinia(createPinia())
  vi.mocked(deleteBackup).mockReset()
  vi.mocked(toast.success).mockReset()
})

describe('a platform backup', () => {
  it('that was taken can be downloaded, restored and deleted', () => {
    expect(buttons(mountItem().wrapper)).toEqual(['Download', 'Restore', 'Delete'])
  })

  it('that was not taken says why and can only be deleted, at once', async () => {
    vi.mocked(deleteBackup).mockResolvedValue({ task_id: null })
    const { wrapper, loadBackups } = mountItem({
      status: 'interrupted',
      reason: 'the manager stopped',
    })

    expect(wrapper.text()).toContain('Interrupted')
    expect(wrapper.find('[data-reason]').text()).toBe('the manager stopped')
    expect(buttons(wrapper)).toEqual(['Delete'])

    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(toast.success).toHaveBeenCalledWith('Backup deleted')
    expect(loadBackups).toHaveBeenCalledOnce()
  })

  it('that is queued or being taken shows so, with no action', () => {
    for (const [status, label] of [
      ['queued', 'Queued'],
      ['running', 'In progress'],
    ] as const) {
      const { wrapper } = mountItem({ status })
      expect(wrapper.text()).toContain(label)
      expect(buttons(wrapper)).toEqual([])
    }
  })

  it('says whether the schedule or a request took it', () => {
    expect(mountItem({ automatic: false }).wrapper.text()).toContain('On request')
    expect(mountItem().wrapper.text()).toContain('Scheduled')
  })
})

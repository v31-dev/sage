import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'vue-sonner'
import VolumeRestoreDialog from '@/pages/Application/VolumeRestoreDialog.vue'
import {
  fetchVolumeBackups,
  restoreVolumeBackup,
  type Application,
  type Backup,
  type Container,
  type Volume,
} from '@/services/api'
import { useAppStore } from '@/stores/app'

vi.mock('vue-sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  fetchVolumeBackups: vi.fn(),
  restoreVolumeBackup: vi.fn(),
}))

const inline = { template: '<div><slot /></div>' }
// Opened as it is mounted, as a click on its trigger would.
const dialog = {
  props: ['open'],
  emits: ['update:open'],
  mounted(this: { $emit: (event: string, value: boolean) => void }) {
    this.$emit('update:open', true)
  },
  template: '<div><slot /></div>',
}
const stubs = {
  Dialog: dialog,
  DialogTrigger: inline,
  DialogContent: inline,
  DialogHeader: inline,
  DialogTitle: inline,
  DialogDescription: inline,
  DialogFooter: inline,
  Select: inline,
  SelectTrigger: inline,
  SelectContent: inline,
  SelectValue: inline,
  SelectItem: inline,
}

const application = {
  name: 'web',
  project: { name: 'demo' },
  status: 'inactive',
  containers: [{ worker: { hostname: 'w1' } }],
} as unknown as Application

const backup = { id: 7, s3_path: 'demo/web/w1/data/20260101_120000.tar.gz.enc' } as Backup

const onW1 = { worker: { hostname: 'w1' }, status: 'inactive' } as Container

async function restore() {
  const wrapper = mount(VolumeRestoreDialog, {
    props: { application, volume: { name: 'data' } as Volume },
    global: { stubs },
  })
  await flushPromises()
  const restoreButtons = wrapper.findAll('button').filter(button => button.text() === 'Restore')
  await restoreButtons[restoreButtons.length - 1]!.trigger('click')
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.mocked(fetchVolumeBackups).mockResolvedValue([backup])
  vi.mocked(restoreVolumeBackup).mockReset()
  vi.mocked(toast.success).mockReset()
  vi.mocked(toast.error).mockReset()
})

describe('a volume restore', () => {
  it('says the restore started, and keeps showing it on its worker until the manager says otherwise', async () => {
    vi.mocked(restoreVolumeBackup).mockResolvedValue({})

    const wrapper = await restore()

    expect(restoreVolumeBackup).toHaveBeenCalledWith('demo', 'web', 'data', 7, 'w1')
    expect(toast.success).toHaveBeenCalledWith('Restore initiated for volume data')
    expect(useAppStore().applicationDeployStatus).toBe('restoring')
    expect(useAppStore().containerStatus(onW1)).toBe('restoring')
    expect(wrapper.findComponent(dialog).props('open')).toBe(false)
  })

  it('that is refused says why, and shows the status as it was', async () => {
    vi.mocked(restoreVolumeBackup).mockRejectedValue(
      new Error('Application already has an operation in progress.')
    )

    await restore()

    expect(toast.error).toHaveBeenCalledWith('Application already has an operation in progress.')
    expect(useAppStore().applicationDeployStatus).toBe('inactive')
    expect(useAppStore().containerStatus(onW1)).toBe('inactive')
  })
})

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
  type Volume,
} from '@/services/api'
import { container } from '../helpers/containers'

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

const stopped = container({
  id: 'c1',
  view: { headline: 'inactive', config: 'current', reason: null, detail: null },
})
const running = container({ id: 'c2', worker: { hostname: 'w2', subdomain: 'blue' } })

const application = (containers = [stopped]) =>
  ({
    name: 'web',
    project: { name: 'demo' },
    containers,
    volumes: [
      { id: 'v1', name: 'data' },
      { id: 'v2', name: 'logs' },
    ],
  }) as unknown as Application

const backup = {
  id: 'b7',
  s3_path: 'base/backups/applications/v1/c1/20260101_120000.tar.gz.enc',
  source_volume_name: 'data',
  created_at: '2026-01-01T12:00:00',
} as Backup

async function mountDialog(containers = [stopped]) {
  const loadApplication = vi.fn(async () => {})
  const wrapper = mount(VolumeRestoreDialog, {
    props: {
      application: application(containers),
      volume: { name: 'data' } as Volume,
      loadApplication,
    },
    global: { stubs },
  })
  await flushPromises()
  return { wrapper, loadApplication }
}

const restoreButton = (wrapper: Awaited<ReturnType<typeof mountDialog>>['wrapper']) =>
  wrapper
    .findAll('button')
    .filter(button => button.text() === 'Restore')
    .reverse()[0]!

beforeEach(() => {
  setActivePinia(createPinia())
  vi.mocked(fetchVolumeBackups).mockResolvedValue([backup])
  vi.mocked(restoreVolumeBackup).mockReset()
  vi.mocked(toast.success).mockReset()
  vi.mocked(toast.error).mockReset()
})

describe('a volume restore', () => {
  it('restores the backup into the first container and this volume, then loads the page again', async () => {
    vi.mocked(restoreVolumeBackup).mockResolvedValue({ task_id: 't1' })
    const { wrapper, loadApplication } = await mountDialog()

    await restoreButton(wrapper).trigger('click')
    await flushPromises()

    expect(restoreVolumeBackup).toHaveBeenCalledWith('demo', 'web', 'data', 'b7', 'c1', 'data')
    expect(toast.success).toHaveBeenCalledWith('Restore queued for volume data')
    expect(loadApplication).toHaveBeenCalledOnce()
    expect(wrapper.findComponent(dialog).props('open')).toBe(false)
  })

  it('needs the chosen container stopped, and says so', async () => {
    const { wrapper } = await mountDialog([running])

    expect(restoreButton(wrapper).attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('A restore needs the container stopped.')
  })

  it('that is refused says why, and loads nothing', async () => {
    vi.mocked(restoreVolumeBackup).mockRejectedValue(
      new Error('This container already has an operation in progress.')
    )
    const { wrapper, loadApplication } = await mountDialog()

    await restoreButton(wrapper).trigger('click')
    await flushPromises()

    expect(toast.error).toHaveBeenCalledWith('This container already has an operation in progress.')
    expect(loadApplication).not.toHaveBeenCalled()
  })
})

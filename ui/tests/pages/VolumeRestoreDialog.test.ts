import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'vue-sonner'
import { watchState } from '@/lib/updates'
import VolumeRestoreDialog from '@/pages/Application/VolumeRestoreDialog.vue'
import {
  deleteVolumeBackup,
  fetchVolumeBackups,
  restoreVolumeBackup,
  type Application,
  type Backup,
  type Volume,
} from '@/services/api'
import { container } from '../helpers/containers'
import { followTopics } from '../helpers/topics'

vi.mock('@/lib/updates', () => ({ watchState: vi.fn(), watchFeed: vi.fn() }))
vi.mock('vue-sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  fetchVolumeBackups: vi.fn(),
  restoreVolumeBackup: vi.fn(),
  deleteVolumeBackup: vi.fn(),
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
// Confirmed as soon as it is clicked.
const confirmation = {
  props: ['triggerText', 'onConfirm'],
  template: '<button data-confirm @click="onConfirm()">{{ triggerText }}</button>',
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
  ConfirmationButton: confirmation,
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

const backup = (id: string, fields: Partial<Backup> = {}) =>
  ({
    id,
    s3_path: `base/backups/applications/v1/c1/${id}.tar.gz.enc`,
    source_volume_name: 'data',
    worker_hostname: 'w1',
    automatic: true,
    status: 'succeeded',
    reason: null,
    created_at: '2026-01-01T12:00:00',
    ...fields,
  }) as Backup

async function mountDialog(containers = [stopped], backups = [backup('b7')]) {
  const topics = followTopics()
  vi.mocked(fetchVolumeBackups).mockResolvedValue(backups)
  const loadApplication = vi.fn(async () => {})
  const wrapper = mount(VolumeRestoreDialog, {
    props: {
      application: application(containers),
      volume: { id: 'v1', name: 'data' } as Volume,
      loadApplication,
    },
    global: { stubs },
  })
  await flushPromises()
  const followed = topics.topic('volume-backups:v1')
  followed.push(await followed.load())
  await flushPromises()
  return { wrapper, loadApplication, followed }
}

type Wrapper = Awaited<ReturnType<typeof mountDialog>>['wrapper']

const row = (wrapper: Wrapper, id: string) => wrapper.find(`[data-backup="${id}"]`)
const buttons = (wrapper: Wrapper, id: string) =>
  row(wrapper, id)
    .findAll('button')
    .map(button => button.text())
const button = (wrapper: Wrapper, id: string, text: string) =>
  row(wrapper, id)
    .findAll('button')
    .find(found => found.text() === text)!

async function openRestore(wrapper: Wrapper, id: string) {
  await button(wrapper, id, 'Restore').trigger('click')
  await flushPromises()
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.mocked(fetchVolumeBackups).mockReset()
  vi.mocked(restoreVolumeBackup).mockReset()
  vi.mocked(deleteVolumeBackup).mockReset()
  vi.mocked(toast.success).mockReset()
  vi.mocked(toast.error).mockReset()
})

describe('what the dialog follows', () => {
  it("follows the volume's backups by its id, loaded from their route, and stops when it goes away", async () => {
    const { wrapper, followed } = await mountDialog()

    expect(watchState).toHaveBeenCalledWith(
      'volume-backups:v1',
      expect.any(Function),
      expect.any(Function)
    )
    expect(fetchVolumeBackups).toHaveBeenCalledWith('demo', 'web', 'data')

    wrapper.unmount()
    expect(followed.stop).toHaveBeenCalledOnce()
  })

  it('shows each pushed list, a backup moving on as it is taken', async () => {
    const { wrapper, followed } = await mountDialog(
      [stopped],
      [backup('b8', { status: 'running' })]
    )
    expect(row(wrapper, 'b8').text()).toContain('In progress')

    followed.push([backup('b8')])
    await flushPromises()

    expect(row(wrapper, 'b8').text()).not.toContain('In progress')
    expect(buttons(wrapper, 'b8')).toEqual(['Download', 'Restore', 'Delete'])
  })
})

describe('each backup', () => {
  it('is named by its volume and time, with how it was taken and its worker as details', async () => {
    const { wrapper } = await mountDialog([stopped], [backup('b7', { automatic: false })])

    expect(row(wrapper, 'b7').text()).toContain('Volume data · on request · from w1')
  })

  it('that was taken can be downloaded, restored and deleted', async () => {
    const { wrapper } = await mountDialog()

    expect(buttons(wrapper, 'b7')).toEqual(['Download', 'Restore', 'Delete'])
  })

  it('that was not taken says why and can only be deleted, at once', async () => {
    vi.mocked(deleteVolumeBackup).mockResolvedValue({ task_id: null })
    const failed = backup('b6', { status: 'failed', reason: "its worker can't be reached" })
    const { wrapper } = await mountDialog([stopped], [failed])

    expect(row(wrapper, 'b6').text()).toContain('Failed')
    expect(row(wrapper, 'b6').find('[data-reason]').text()).toBe("its worker can't be reached")
    expect(buttons(wrapper, 'b6')).toEqual(['Delete'])

    await button(wrapper, 'b6', 'Delete').trigger('click')
    await flushPromises()

    expect(deleteVolumeBackup).toHaveBeenCalledWith('demo', 'web', 'data', 'b6')
    expect(toast.success).toHaveBeenCalledWith('Backup deleted')
  })

  it('that is queued or being taken shows so, with no action', async () => {
    const { wrapper } = await mountDialog([stopped], [backup('b5', { status: 'queued' })])

    expect(row(wrapper, 'b5').text()).toContain('Queued')
    expect(buttons(wrapper, 'b5')).toEqual([])
  })
})

describe('a volume restore', () => {
  it('opens on its row, restores into the first container and this volume, then loads the page again', async () => {
    vi.mocked(restoreVolumeBackup).mockResolvedValue({ task_id: 't1' })
    const { wrapper, loadApplication } = await mountDialog()

    await openRestore(wrapper, 'b7')
    await button(wrapper, 'b7', 'Restore here').trigger('click')
    await flushPromises()

    expect(restoreVolumeBackup).toHaveBeenCalledWith('demo', 'web', 'data', 'b7', 'c1', 'data')
    expect(toast.success).toHaveBeenCalledWith('Restore queued for volume data')
    expect(loadApplication).toHaveBeenCalledOnce()
    expect(wrapper.findComponent(dialog).props('open')).toBe(false)
  })

  it('is open on one row at a time, and its Restore closes it again', async () => {
    const { wrapper } = await mountDialog([stopped], [backup('b7'), backup('b8')])

    await openRestore(wrapper, 'b7')
    await openRestore(wrapper, 'b8')

    expect(row(wrapper, 'b7').find('[data-restore]').exists()).toBe(false)
    expect(row(wrapper, 'b8').find('[data-restore]').exists()).toBe(true)

    await openRestore(wrapper, 'b8')
    expect(row(wrapper, 'b8').find('[data-restore]').exists()).toBe(false)
  })

  it('closes when its backup is no longer one that can be restored', async () => {
    const { wrapper, followed } = await mountDialog()
    await openRestore(wrapper, 'b7')

    followed.push([])
    await flushPromises()
    followed.push([backup('b7')])
    await flushPromises()

    expect(row(wrapper, 'b7').find('[data-restore]').exists()).toBe(false)
  })

  it('needs the chosen container stopped, and says so', async () => {
    const { wrapper } = await mountDialog([running])
    await openRestore(wrapper, 'b7')

    expect(button(wrapper, 'b7', 'Restore here').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('A restore needs the container stopped.')
  })

  it('that is refused says why, and loads nothing', async () => {
    vi.mocked(restoreVolumeBackup).mockRejectedValue(
      new Error('This container already has an operation in progress.')
    )
    const { wrapper, loadApplication } = await mountDialog()

    await openRestore(wrapper, 'b7')
    await button(wrapper, 'b7', 'Restore here').trigger('click')
    await flushPromises()

    expect(toast.error).toHaveBeenCalledWith('This container already has an operation in progress.')
    expect(loadApplication).not.toHaveBeenCalled()
  })
})

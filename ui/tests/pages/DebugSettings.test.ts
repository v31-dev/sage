import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import DebugSettings from '@/pages/Settings/DebugSettings.vue'
import {
  downloadDebugFile,
  fetchDebugCapture,
  setDebugStop,
  type DebugCapture,
  type DebugFile,
  type DebugSetting,
} from '@/services/api'
import { useAppStore } from '@/stores/app'

vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  fetchDebugCapture: vi.fn(),
  setDebugStop: vi.fn(),
  downloadDebugFile: vi.fn(),
}))

const capture = (stopAt: string | null, files: DebugFile[] = []): DebugCapture => ({
  stop_at: stopAt,
  files,
})

const file = (name: string, size: number, active = false): DebugFile => ({
  name,
  size,
  modified_at: '2026-01-01T12:30:00',
  active,
})

// The page shows what the app store holds of the capture, which the store follows for the whole app.
async function mountPage(stopAt: string | null, files: DebugFile[] = []) {
  const store = useAppStore()
  store.debugCapture = capture(stopAt, files)
  const wrapper = mount(DebugSettings)
  await flushPromises()
  return { wrapper, store }
}

type Page = Awaited<ReturnType<typeof mountPage>>['wrapper']

const button = (wrapper: Page, text: string) =>
  wrapper.findAll('button').find(found => found.text() === text)

const stopField = (wrapper: Page) =>
  (wrapper.find('input[type="datetime-local"]').element as HTMLInputElement).value

beforeEach(() => {
  setActivePinia(createPinia())
  vi.mocked(fetchDebugCapture).mockReset()
  vi.mocked(setDebugStop).mockReset()
  vi.mocked(setDebugStop).mockResolvedValue({} as DebugSetting)
})

describe('what the store holds', () => {
  it('shows nothing until the capture has loaded, or why it could not be', async () => {
    const store = useAppStore()
    const wrapper = mount(DebugSettings)
    expect(button(wrapper, 'Start')).toBeUndefined()

    store.debugCaptureError = 'Failed to fetch the capture'
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to fetch the capture')
  })

  it('shows a capture that ended elsewhere as off', async () => {
    const { wrapper, store } = await mountPage('2026-01-01T13:00:00')
    expect(wrapper.text()).toContain('Capturing until')

    store.debugCapture = capture(null)
    await flushPromises()

    expect(wrapper.text()).toContain('Off')
    expect(button(wrapper, 'Stop')).toBeUndefined()
  })

  it('keeps a stop time being typed while the same capture is pushed, and shows a new one', async () => {
    const { wrapper, store } = await mountPage('2026-01-01T13:00:00')
    await wrapper.find('input[type="datetime-local"]').setValue('2026-01-01T15:00')

    store.debugCapture = capture('2026-01-01T13:00:00', [
      file('sage-debug-2026-01-01T12-00-00Z.log', 10, true),
    ])
    await flushPromises()
    expect(stopField(wrapper)).toBe('2026-01-01T15:00')

    store.debugCapture = capture('2026-01-01T14:00:00')
    await flushPromises()
    expect(stopField(wrapper)).toBe('2026-01-01T14:00')
  })
})

describe('the capture', () => {
  it('starts until the time chosen, sent in UTC, and shows what it reads back', async () => {
    const { wrapper } = await mountPage(null)
    expect(wrapper.text()).toContain('Off')
    expect(button(wrapper, 'Stop')).toBeUndefined()

    await wrapper.find('input[type="datetime-local"]').setValue('2026-01-01T13:00')
    vi.mocked(fetchDebugCapture).mockResolvedValue(capture('2026-01-01T13:00:00'))
    await button(wrapper, 'Start')!.trigger('click')
    await flushPromises()

    expect(setDebugStop).toHaveBeenCalledWith('2026-01-01T13:00:00.000Z')
    expect(wrapper.text()).toContain('Capturing until')
    expect(button(wrapper, 'Change stop time')).toBeDefined()
  })

  it('shows the stop time of the capture on in the field, and stops it', async () => {
    const { wrapper } = await mountPage('2026-01-01T13:00:00')
    expect(stopField(wrapper)).toBe('2026-01-01T13:00')

    vi.mocked(fetchDebugCapture).mockResolvedValue(capture(null))
    await button(wrapper, 'Stop')!.trigger('click')
    await flushPromises()

    expect(setDebugStop).toHaveBeenCalledWith(null)
    expect(wrapper.text()).toContain('Off')
  })

  it('shows why a stop time was refused', async () => {
    const { wrapper } = await mountPage(null)
    vi.mocked(setDebugStop).mockRejectedValue(new Error('The stop time has passed.'))

    await button(wrapper, 'Start')!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('The stop time has passed.')
  })
})

describe('the files', () => {
  it('lists each with its size, marks the one being written and keeps it from being deleted', async () => {
    const { wrapper } = await mountPage('2026-01-01T13:00:00', [
      file('sage-debug-2026-01-01T12-00-00Z.log', 3 * 1024 * 1024, true),
      file('sage-debug-2025-12-31T00-00-00Z.log', 512),
    ])

    const rows = wrapper.findAll('tbody tr')
    expect(rows.map(row => row.findAll('td').map(cell => cell.text())[1])).toEqual([
      '3.0 MiB',
      '512 B',
    ])
    expect(rows[0]!.text()).toContain('being written')
    expect(rows[1]!.text()).not.toContain('being written')
    const deletes = rows.map(row => row.findAll('button').find(found => found.text() === 'Delete')!)
    expect(deletes.map(found => found.attributes('disabled') !== undefined)).toEqual([true, false])
  })

  it('downloads a file by its name', async () => {
    const { wrapper } = await mountPage(null, [file('sage-debug-2025-12-31T00-00-00Z.log', 512)])

    await button(wrapper, 'Download')!.trigger('click')

    expect(downloadDebugFile).toHaveBeenCalledWith('sage-debug-2025-12-31T00-00-00Z.log')
  })
})

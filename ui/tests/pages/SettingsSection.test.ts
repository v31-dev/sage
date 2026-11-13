import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import SettingsSection from '@/pages/Settings/SettingsSection.vue'
import {
  fetchAppInfo,
  fetchSetting,
  testSetting,
  updateSetting,
  type AppInfo,
  type SettingSectionStatus,
  type SettingState,
  type SettingsStatus,
} from '@/services/api'
import { useAppStore } from '@/stores/app'
import { toast } from 'vue-sonner'
import { followTopics } from '../helpers/topics'

vi.mock('vue-router', () => ({ onBeforeRouteLeave: vi.fn() }))
vi.mock('vue-sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
vi.mock('@/lib/updates', () => ({ watchState: vi.fn(), watchFeed: vi.fn() }))
vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  fetchAppInfo: vi.fn(),
  fetchSetting: vi.fn(),
  testSetting: vi.fn(),
  updateSetting: vi.fn(),
}))

let topics: ReturnType<typeof followTopics>

const fields = [
  { key: 'bucket', label: 'Bucket' },
  { key: 'endpoint', label: 'Endpoint' },
]

const section = (state: SettingState): SettingSectionStatus => ({
  state,
  message: null,
  task_id: null,
  checked_at: null,
  details: null,
  channels: null,
  testing: false,
})

const info = (s3: SettingState) =>
  ({
    version: '1.0.0',
    state: 'running',
    settings: { s3: section(s3) } as SettingsStatus,
  }) as AppInfo

async function mountSection(saved: Record<string, string | null>) {
  vi.mocked(fetchSetting).mockResolvedValue({ key: 's3', value: saved } as never)
  const wrapper = mount(SettingsSection, {
    props: { title: 'S3', description: 'Backups', section: 's3', fields },
  })
  await flushPromises()
  return wrapper
}

async function save(wrapper: Awaited<ReturnType<typeof mountSection>>, values: string[]) {
  const inputs = wrapper.findAll('input')
  for (const [index, value] of values.entries()) await inputs[index]!.setValue(value)
  vi.mocked(updateSetting).mockResolvedValue({
    key: 's3',
    value: { bucket: values[0] || null, endpoint: values[1] || null },
  } as never)
  await wrapper
    .findAll('button')
    .find(button => button.text() === 'Save changes')!
    .trigger('click')
  await flushPromises()
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.mocked(fetchAppInfo).mockReset()
  vi.mocked(testSetting).mockReset()
  vi.mocked(toast.success).mockReset()
  topics = followTopics()
  useAppStore().info = info('ok')
})

const testButton = (wrapper: Awaited<ReturnType<typeof mountSection>>) =>
  wrapper.findAll('button').find(button => button.text() === 'Test')!

const testingIn = (sections: Record<string, boolean>) => {
  const settings = useAppStore().info!.settings!
  for (const [key, testing] of Object.entries(sections))
    settings[key as keyof typeof settings] = { ...section('ok'), testing }
}

describe('testing', () => {
  it("is busy while its section's Test is queued or running, and not for another section's", async () => {
    const wrapper = await mountSection({ bucket: 'backups', endpoint: null })
    testingIn({ notifications: true })
    await flushPromises()
    expect(testButton(wrapper).attributes('disabled')).toBeUndefined()
    testingIn({ s3: true })
    await flushPromises()
    expect(testButton(wrapper).attributes('disabled')).toBeDefined()
    testingIn({ s3: false })
    await flushPromises()
    expect(testButton(wrapper).attributes('disabled')).toBeUndefined()
  })

  it('marks its Test as running once its request is answered, for the next push to confirm', async () => {
    vi.mocked(testSetting).mockResolvedValue({ task_id: 't1' })
    const wrapper = await mountSection({ bucket: 'backups', endpoint: null })
    await testButton(wrapper).trigger('click')
    await flushPromises()
    expect(testSetting).toHaveBeenCalledWith('s3', { bucket: 'backups', endpoint: null })
    expect(useAppStore().info!.settings!.s3.testing).toBe(true)
    expect(testButton(wrapper).attributes('disabled')).toBeDefined()
    testingIn({ s3: false })
    await flushPromises()
    expect(testButton(wrapper).attributes('disabled')).toBeUndefined()
  })

  it("shows the outcome its section's feed pushes: a success as a toast, a failure under the form", async () => {
    const wrapper = await mountSection({ bucket: 'backups', endpoint: null })
    const feed = topics.topic('setting-tests:s3')
    expect(await feed.load()).toEqual([])
    feed.push([{ ok: true, message: 'Bucket reachable and writable.', at: '2026-01-01T12:00:00' }])
    await flushPromises()
    expect(toast.success).toHaveBeenCalledWith('Bucket reachable and writable.')
    feed.push([{ ok: false, message: 'bucket unreachable', at: '2026-01-01T12:01:00' }])
    await flushPromises()
    expect(wrapper.text()).toContain('bucket unreachable')
  })

  it('shows why a Test was refused', async () => {
    vi.mocked(testSetting).mockRejectedValue(
      new Error('A test of this section is already running.')
    )
    const wrapper = await mountSection({ bucket: 'backups', endpoint: null })
    await testButton(wrapper).trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('A test of this section is already running.')
    expect(testButton(wrapper).attributes('disabled')).toBeUndefined()
  })
})

describe('saving', () => {
  it('reads the state back from the manager after a save that empties the section', async () => {
    vi.mocked(fetchAppInfo).mockResolvedValue(info('not_configured'))
    const wrapper = await mountSection({ bucket: 'backups', endpoint: 'https://s3.example.test' })

    await save(wrapper, ['', ''])

    expect(fetchAppInfo).toHaveBeenCalledOnce()
    expect(useAppStore().info?.settings?.s3.state).toBe('not_configured')
  })

  it('shows a section that is still filled in as applying, without reading it back', async () => {
    const wrapper = await mountSection({ bucket: 'backups', endpoint: 'https://s3.example.test' })

    await save(wrapper, ['other', 'https://s3.example.test'])

    expect(fetchAppInfo).not.toHaveBeenCalled()
    expect(useAppStore().info?.settings?.s3.state).toBe('applying')
  })
})

import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import SettingsSection from '@/pages/Settings/SettingsSection.vue'
import {
  fetchAppInfo,
  settingsAPI,
  type AppInfo,
  type SettingSectionStatus,
  type SettingState,
  type SettingsStatus,
} from '@/services/api'
import { useAppStore } from '@/stores/app'

vi.mock('vue-router', () => ({ onBeforeRouteLeave: vi.fn() }))
vi.mock('vue-sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  fetchAppInfo: vi.fn(),
  settingsAPI: { fetchOne: vi.fn(), update: vi.fn(), action: vi.fn() },
}))

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
})

const info = (s3: SettingState) =>
  ({
    version: '1.0.0',
    state: 'running',
    settings: { s3: section(s3) } as SettingsStatus,
  }) as AppInfo

async function mountSection(saved: Record<string, string | null>) {
  vi.mocked(settingsAPI.fetchOne).mockResolvedValue({ key: 's3', value: saved } as never)
  const wrapper = mount(SettingsSection, {
    props: { title: 'S3', description: 'Backups', section: 's3', fields },
  })
  await flushPromises()
  return wrapper
}

async function save(wrapper: Awaited<ReturnType<typeof mountSection>>, values: string[]) {
  const inputs = wrapper.findAll('input')
  for (const [index, value] of values.entries()) await inputs[index]!.setValue(value)
  vi.mocked(settingsAPI.update).mockResolvedValue({
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
  useAppStore().info = info('ok')
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

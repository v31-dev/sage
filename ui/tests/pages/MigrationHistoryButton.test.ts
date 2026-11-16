import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import MigrationHistoryButton from '@/pages/Settings/MigrationHistoryButton.vue'
import { fetchMigrations, type MigrationRun } from '@/services/api'

vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  fetchMigrations: vi.fn(),
}))

const inline = { template: '<div><slot /></div>' }
const stubs = {
  Dialog: inline,
  DialogTrigger: inline,
  DialogContent: inline,
  DialogHeader: inline,
  DialogTitle: inline,
  DialogDescription: inline,
  DialogFooter: inline,
  SelectTrigger: inline,
  SelectContent: inline,
  SelectValue: inline,
  SelectItem: { props: ['value'], template: '<div data-item :data-value="value"><slot /></div>' },
  Select: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<div data-select :data-selected="modelValue"><slot /></div>',
  },
}

const runs: MigrationRun[] = [
  {
    run: 'b',
    trigger: 'boot',
    release: '0.3.0',
    at: '2026-02-01T00:00:00',
    outcome: 'refused',
    files: [
      {
        file: 'data.db',
        from: 1,
        to: 3,
        outcome: 'refused',
        detail: 'its migration step 3 failed (x).',
        at: '2026-02-01T00:00:00',
      },
    ],
  },
  {
    run: 'a',
    trigger: 'restore',
    release: '0.2.0',
    at: '2026-01-01T00:00:00',
    outcome: 'migrated',
    files: [
      {
        file: 'data.db',
        from: 0,
        to: 1,
        outcome: 'migrated',
        detail: null,
        at: '2026-01-01T00:00:00',
      },
    ],
  },
]

async function opened() {
  const wrapper = mount(MigrationHistoryButton, { global: { stubs } })
  await wrapper
    .findAll('button')
    .find(button => button.text().includes('Migration history'))!
    .trigger('click')
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.mocked(fetchMigrations).mockReset()
})

describe('the migration history', () => {
  it('loads when opened and shows the newest run, with why a file was refused', async () => {
    vi.mocked(fetchMigrations).mockResolvedValue(runs)
    const wrapper = await opened()

    expect(fetchMigrations).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-select]').attributes('data-selected')).toBe('b')
    const [file] = wrapper.findAll('[data-file]')
    expect(file!.text()).toContain('data.db')
    expect(file!.text()).toContain('1 → 3')
    expect(file!.text()).toContain('Refused')
    expect(file!.text()).toContain('its migration step 3 failed (x).')
  })

  it('lists every run with its release, how it started and its outcome', async () => {
    vi.mocked(fetchMigrations).mockResolvedValue(runs)
    const wrapper = await opened()

    const items = wrapper.findAll('[data-item]').map(item => item.text())
    expect(items).toHaveLength(2)
    expect(items[0]).toContain('Sage 0.3.0 · At start · Refused')
    expect(items[1]).toContain('Sage 0.2.0 · Platform restore · Updated')
  })

  it('says so when there is none', async () => {
    vi.mocked(fetchMigrations).mockResolvedValue([])
    const wrapper = await opened()
    expect(wrapper.find('[data-empty]').text()).toBe('No migrations yet.')
  })

  it('shows a load that failed', async () => {
    vi.mocked(fetchMigrations).mockRejectedValue(new Error('Request failed'))
    const wrapper = await opened()
    expect(wrapper.find('[data-error]').text()).toBe('Request failed')
  })

  it('shows the newest load when an earlier one answers last', async () => {
    let first!: (value: MigrationRun[]) => void
    vi.mocked(fetchMigrations)
      .mockImplementationOnce(() => new Promise(resolve => (first = resolve)))
      .mockResolvedValueOnce([runs[1]!])
    const wrapper = mount(MigrationHistoryButton, { global: { stubs } })
    const button = wrapper.findAll('button').find(b => b.text().includes('Migration history'))!
    await button.trigger('click')
    await button.trigger('click')
    await flushPromises()
    first(runs)
    await flushPromises()

    expect(wrapper.findAll('[data-item]')).toHaveLength(1)
    expect(wrapper.find('[data-select]').attributes('data-selected')).toBe('a')
  })
})

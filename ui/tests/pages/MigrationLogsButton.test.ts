import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import MigrationLogsButton from '@/pages/Settings/MigrationLogsButton.vue'
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

const ACTION =
  'Worker [w1] - Run command to stop [shop-web] as 0.1.3 ran it.\n\ndocker compose -f /opt/sage/applications/shop-web/docker-compose.yml down'

function migration(run: string, fields: Partial<MigrationRun> = {}): MigrationRun {
  return {
    run,
    trigger: 'boot',
    release: '0.2.0',
    at: '2026-02-01T00:00:00',
    refused: false,
    actions: 1,
    lines: [
      {
        ts: '2026-02-01T00:00:00',
        level: 'INFO',
        message: 'Kept [2] applications with their ids.',
      },
      { ts: '2026-02-01T00:00:01', level: 'CRITICAL', message: ACTION },
    ],
    ...fields,
  }
}

const runs = [
  migration('b', { refused: true, release: '0.3.0' }),
  migration('a', { trigger: 'restore', at: '2026-01-01T00:00:00' }),
]

async function opened() {
  const wrapper = mount(MigrationLogsButton, { global: { stubs } })
  await wrapper
    .findAll('button')
    .find(button => button.text().includes('Migration logs'))!
    .trigger('click')
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.mocked(fetchMigrations).mockReset()
})

describe('the migration logs', () => {
  it('load when opened and show the newest run as a log, its actions last and highlighted', async () => {
    vi.mocked(fetchMigrations).mockResolvedValue(runs)
    const wrapper = await opened()

    expect(fetchMigrations).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-select]').attributes('data-selected')).toBe('b')
    const lines = wrapper.findAll('[data-line]')
    expect(lines.map(line => line.findAll('td')[1]!.text())).toEqual(['INFO', 'CRITICAL'])
    expect(lines[0]!.text()).toContain('Kept [2] applications with their ids.')
    expect(lines[1]!.find('pre').text()).toBe(ACTION)
    expect(lines[1]!.findAll('td')[1]!.classes()).toContain('font-semibold')
  })

  it('list every run with its release, how it started and its outcome', async () => {
    vi.mocked(fetchMigrations).mockResolvedValue(runs)
    const wrapper = await opened()

    const items = wrapper.findAll('[data-item]').map(item => item.text())
    expect(items[0]).toContain('Sage 0.3.0 · At start · Refused')
    expect(items[1]).toMatch(/Sage 0\.2\.0 · Platform restore$/)
  })

  it('open on the run they are given, loading at once', async () => {
    vi.mocked(fetchMigrations).mockResolvedValue(runs)
    const wrapper = mount(MigrationLogsButton, {
      props: { run: 'a', open: true },
      global: { stubs },
    })
    await flushPromises()
    expect(fetchMigrations).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-select]').attributes('data-selected')).toBe('a')
  })

  it('open on the newest when the run they are given is not listed', async () => {
    vi.mocked(fetchMigrations).mockResolvedValue(runs)
    const wrapper = mount(MigrationLogsButton, {
      props: { run: 'gone', open: true },
      global: { stubs },
    })
    await flushPromises()
    expect(wrapper.find('[data-select]').attributes('data-selected')).toBe('b')
  })

  it('note above the log how many steps a run left to do by hand, and nothing for one that left none', async () => {
    vi.mocked(fetchMigrations).mockResolvedValue([
      migration('b', { actions: 3 }),
      migration('a', { actions: 0 }),
    ])
    const wrapper = await opened()
    expect(wrapper.find('[data-actions-note]').text()).toBe(
      "This run left 3 steps to do by hand: they're at the end of the log, in red, in the order to take them."
    )

    vi.mocked(fetchMigrations).mockResolvedValue([migration('a', { actions: 0 })])
    const none = await opened()
    expect(none.find('[data-actions-note]').exists()).toBe(false)
  })

  it('say so when there is none', async () => {
    vi.mocked(fetchMigrations).mockResolvedValue([])
    const wrapper = await opened()
    expect(wrapper.find('[data-empty]').text()).toBe('No migrations yet.')
  })

  it('show a load that failed', async () => {
    vi.mocked(fetchMigrations).mockRejectedValue(new Error('Request failed'))
    const wrapper = await opened()
    expect(wrapper.find('[data-error]').text()).toBe('Request failed')
  })

  it('show the newest load when an earlier one answers last', async () => {
    let first!: (value: MigrationRun[]) => void
    vi.mocked(fetchMigrations)
      .mockImplementationOnce(() => new Promise(resolve => (first = resolve)))
      .mockResolvedValueOnce([runs[1]!])
    const wrapper = mount(MigrationLogsButton, { global: { stubs } })
    const button = wrapper.findAll('button').find(b => b.text().includes('Migration logs'))!
    await button.trigger('click')
    await button.trigger('click')
    await flushPromises()
    first(runs)
    await flushPromises()

    expect(wrapper.findAll('[data-item]')).toHaveLength(1)
    expect(wrapper.find('[data-select]').attributes('data-selected')).toBe('a')
  })
})

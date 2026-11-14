import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'vue-sonner'
import ContainerStatus from '@/components/ContainerStatus.vue'
import { cancelContainerRequest, type Container, type RuntimeReading } from '@/services/api'
import { container } from '../helpers/containers'

vi.mock('vue-sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  cancelContainerRequest: vi.fn(),
}))

const cancel = vi.mocked(cancelContainerRequest)

function mountStatus(overrides: Partial<Container> = {}, reload = vi.fn(async () => {})) {
  return mount(ContainerStatus, { props: { container: container(overrides), reload } })
}

const reading = (overrides: Partial<RuntimeReading> = {}): RuntimeReading => ({
  state: 'running',
  deployment_id: 'd1',
  health: null,
  exit_code: null,
  oom_killed: false,
  ...overrides,
})

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('the state', () => {
  it('shows the headline the manager decided', () => {
    const wrapper = mountStatus({
      view: { headline: 'deploying', config: 'current', reason: null, detail: null },
    })

    expect(wrapper.text()).toContain('deploying')
    expect(wrapper.find('[data-reason]').exists()).toBe(false)
  })

  it.each([
    ['dirty', 'Configuration changed'],
    ['not_deployed', 'Not deployed'],
    ['current', null],
    ['unknown', null],
  ] as const)('says when its configuration is %s', (config, shown) => {
    const badge = mountStatus({
      view: { headline: 'active', config, reason: null, detail: null },
    }).find('[data-config]')

    expect(badge.exists() ? badge.text() : null).toBe(shown)
  })

  it.each([
    ['failed', 'boom', 'The last operation failed: boom'],
    ['unreachable', null, "Its worker can't be reached."],
    ['unexpected_deployment', 'd9', 'It is running a deployment it was not deployed with.'],
    ['something_new', null, 'something_new'],
  ])('says why it is in error: %s', (reason, detail, shown) => {
    const wrapper = mountStatus({ view: { headline: 'error', config: 'current', reason, detail } })

    expect(wrapper.get('[data-reason]').text()).toBe(shown)
  })
})

describe('the runtime', () => {
  it.each([
    ['known', reading(), 'Docker: running'],
    [
      'known',
      reading({ state: 'exited', exit_code: 137, oom_killed: true }),
      'Docker: exited (exit code 137, out of memory)',
    ],
    ['known', reading({ state: 'absent' }), 'Docker: not on its worker'],
    ['stale', reading(), 'Last seen running'],
    [
      'unreachable',
      reading({ state: 'exited', exit_code: 0 }),
      'Worker unreachable; last seen exited (exit code 0)',
    ],
    ['unreachable', null, "Its worker can't be reached; not seen yet"],
    ['unobserved', null, 'Not seen on its worker yet'],
  ] as const)('when it is %s says what was last read', (knowledge, read, shown) => {
    const wrapper = mountStatus({ runtime: { knowledge, reading: read } })

    expect(wrapper.get('[data-runtime]').text()).toBe(shown)
  })

  it.each([
    ['healthy', 'health healthy'],
    ['unhealthy', 'health unhealthy'],
    ['starting', 'health starting'],
  ])('shows Docker’s health %s apart from the headline', (health, label) => {
    const wrapper = mountStatus({ runtime: { knowledge: 'known', reading: reading({ health }) } })

    expect(wrapper.text()).toContain(label)
    expect(wrapper.text()).toContain('active')
  })

  it('shows no health for a container without a check', () => {
    expect(mountStatus().text()).not.toContain('health')
  })

  it('says when it was deployed, from its record', () => {
    const recorded = mountStatus({
      target: {
        kind: 'present',
        record: {
          applied: { deployment_id: 'd1', config_hash: 'h', committed_at: '2026-01-01T00:00:00' },
        },
      },
    })

    expect(recorded.text()).toContain('Deployed')
    expect(mountStatus().text()).not.toContain('Deployed')
  })
})

describe('the requests waiting their turn', () => {
  it('lists each, with a Cancel only for those Cancel takes', () => {
    const wrapper = mountStatus({
      queued: [
        { operation: 'deploy', task_id: 't1', cancellable: true },
        { operation: 'backup_preserve', task_id: 't2', cancellable: false },
      ],
    })
    const rows = wrapper.findAll('[data-queued]')

    expect(rows.map(row => [row.get('span').text(), row.find('button').exists()])).toEqual([
      ['Deploy queued', true],
      ['Automatic backup queued', false],
    ])
  })

  it('cancels the request and loads the page again', async () => {
    cancel.mockResolvedValue({ task_id: 't1' })
    const reload = vi.fn(async () => {})
    const wrapper = mountStatus(
      { queued: [{ operation: 'stop', task_id: 't1', cancellable: true }] },
      reload
    )

    await wrapper.get('[data-queued] button').trigger('click')
    await flushPromises()

    expect(cancel).toHaveBeenCalledWith('demo', 'web', 'c1', 'stop')
    expect(reload).toHaveBeenCalledOnce()
  })

  it('says why a Cancel was refused, and loads nothing', async () => {
    cancel.mockRejectedValue(new Error('The stop of this container has already started.'))
    const reload = vi.fn(async () => {})
    const wrapper = mountStatus(
      { queued: [{ operation: 'stop', task_id: 't1', cancellable: true }] },
      reload
    )

    await wrapper.get('[data-queued] button').trigger('click')
    await flushPromises()

    expect(toast.error).toHaveBeenCalledWith('The stop of this container has already started.')
    expect(reload).not.toHaveBeenCalled()
    expect(wrapper.get('[data-queued] button').attributes('disabled')).toBeUndefined()
  })
})

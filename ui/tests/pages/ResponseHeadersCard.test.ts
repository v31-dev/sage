import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ResponseHeadersCard from '@/pages/Application/ResponseHeadersCard.vue'
import { updateApplication, type Application } from '@/services/api'

vi.mock('vue-sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  updateApplication: vi.fn(),
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
}

const DEFAULTS = {
  'Content-Security-Policy': "default-src 'self'",
  'X-Frame-Options': 'DENY',
  'Permissions-Policy': 'camera=()',
}

const application = (changes: Record<string, string | null> = {}) =>
  ({
    name: 'web',
    project: { name: 'demo', label: 'Demo' },
    response_headers: { defaults: DEFAULTS, changes, own_allowed: ['Content-Security-Policy'] },
  }) as unknown as Application

const loadApplication = vi.fn(async () => {})

function setup(changes: Record<string, string | null> = {}) {
  return mount(ResponseHeadersCard, {
    props: { application: application(changes), loadApplication },
    global: { stubs },
  })
}

type Wrapper = ReturnType<typeof setup>

// A button by its text or its label, in the card or in one of its parts.
const button = (wrapper: Wrapper, text: string, within: Pick<Wrapper, 'findAll'> = wrapper) =>
  within
    .findAll('button')
    .find(found => found.text() === text || found.attributes('aria-label') === text)!

async function open(wrapper: Wrapper) {
  await button(wrapper, 'Edit').trigger('click')
}

async function save(wrapper: Wrapper) {
  await button(wrapper, 'Save').trigger('click')
  await flushPromises()
}

const sent = () => vi.mocked(updateApplication).mock.lastCall?.[2]

beforeEach(() => {
  vi.mocked(updateApplication).mockReset()
  loadApplication.mockClear()
})

describe('the response headers', () => {
  it('show what each header sends: the default, a change, one of the application’s own', () => {
    const wrapper = setup({ 'X-Frame-Options': 'SAMEORIGIN', 'X-App': 'v1' })

    const row = (name: string) => wrapper.get(`[data-row="${name}"]`).text()
    expect(row('Content-Security-Policy')).toContain("default-src 'self'")
    expect(row('Content-Security-Policy')).toContain('Default')
    expect(row('X-Frame-Options')).toContain('SAMEORIGIN')
    expect(row('X-Frame-Options')).toContain('Changed')
    expect(row('X-App')).toContain('v1')
    expect(row('X-App')).toContain('Added')
  })

  it('open with each header’s current value, so an edit starts from it', async () => {
    const wrapper = setup({ 'X-Frame-Options': 'SAMEORIGIN' })
    await open(wrapper)

    const value = (name: string) =>
      (wrapper.get(`[data-header="${name}"] textarea`).element as HTMLTextAreaElement).value
    expect(value('Content-Security-Policy')).toBe("default-src 'self'")
    expect(value('X-Frame-Options')).toBe('SAMEORIGIN')
  })

  it('send only what differs from the defaults', async () => {
    const wrapper = setup()
    await open(wrapper)
    await wrapper
      .get('[data-header="Content-Security-Policy"] textarea')
      .setValue("default-src 'self'; img-src 'self' data: *.openstreetmap.org")
    await save(wrapper)

    expect(sent()).toEqual({
      response_headers: {
        'Content-Security-Policy': "default-src 'self'; img-src 'self' data: *.openstreetmap.org",
      },
    })
    expect(loadApplication).toHaveBeenCalled()
  })

  it('reset one header to its default and keep the others’ changes', async () => {
    const wrapper = setup({
      'X-Frame-Options': 'SAMEORIGIN',
      'Permissions-Policy': 'camera=(self)',
      'X-App': 'v1',
    })
    await open(wrapper)
    const frame = wrapper.get('[data-header="X-Frame-Options"]')
    expect(
      button(wrapper, 'Reset to default', wrapper.get('[data-header="Content-Security-Policy"]'))
    ).toBeUndefined()
    await button(wrapper, 'Reset to default', frame).trigger('click')
    expect((frame.get('textarea').element as HTMLTextAreaElement).value).toBe('DENY')
    await save(wrapper)

    expect(sent()).toEqual({
      response_headers: { 'Permissions-Policy': 'camera=(self)', 'X-App': 'v1' },
    })
  })

  it('send a header left blank as its default', async () => {
    const wrapper = setup({ 'X-Frame-Options': 'SAMEORIGIN' })
    await open(wrapper)
    await wrapper.get('[data-header="X-Frame-Options"] textarea').setValue('  ')
    await save(wrapper)

    expect(sent()).toEqual({ response_headers: null })
  })

  it('add and remove headers of the application’s own, leaving out an empty row', async () => {
    const wrapper = setup({ 'X-Old': 'gone' })
    await open(wrapper)
    await button(wrapper, 'Remove').trigger('click')
    await button(wrapper, 'Add header').trigger('click')
    await button(wrapper, 'Add header').trigger('click')
    const [name, value] = wrapper.findAll('[data-own-header] input')
    await name!.setValue(' X-App-Version ')
    await value!.setValue('1.4.2')
    await save(wrapper)

    expect(sent()).toEqual({ response_headers: { 'X-App-Version': '1.4.2' } })
  })

  it('show a header the application sends itself', () => {
    const wrapper = setup({ 'Content-Security-Policy': null })

    const row = wrapper.get('[data-row="Content-Security-Policy"]').text()
    expect(row).toContain('Sent by the application')
    expect(row).toContain("Application's own")
    expect(row).not.toContain("default-src 'self'")
  })

  it('let only the headers allowed be the application’s own, saved as null', async () => {
    const wrapper = setup()
    await open(wrapper)
    expect(wrapper.find('[data-header="X-Frame-Options"] [role="switch"]').exists()).toBe(false)
    const csp = wrapper.get('[data-header="Content-Security-Policy"]')
    await csp.get('[role="switch"]').trigger('click')
    expect(csp.find('textarea').exists()).toBe(false)
    await save(wrapper)

    expect(sent()).toEqual({ response_headers: { 'Content-Security-Policy': null } })
  })

  it('open with the application’s own on, and reset it to the default', async () => {
    const wrapper = setup({ 'Content-Security-Policy': null, 'X-App': 'v1' })
    await open(wrapper)
    const csp = wrapper.get('[data-header="Content-Security-Policy"]')
    expect(csp.get('[role="switch"]').attributes('aria-checked')).toBe('true')
    await button(wrapper, 'Reset to default', csp).trigger('click')
    expect(csp.get('[role="switch"]').attributes('aria-checked')).toBe('false')
    expect((csp.get('textarea').element as HTMLTextAreaElement).value).toBe("default-src 'self'")
    await save(wrapper)

    expect(sent()).toEqual({ response_headers: { 'X-App': 'v1' } })
  })

  it('show the manager’s refusal and stay open', async () => {
    vi.mocked(updateApplication).mockRejectedValue(
      new Error("Invalid header name: 'X-App Version'.")
    )
    const wrapper = setup()
    await open(wrapper)
    await save(wrapper)

    expect(wrapper.text()).toContain("Invalid header name: 'X-App Version'.")
    expect(loadApplication).not.toHaveBeenCalled()
  })
})

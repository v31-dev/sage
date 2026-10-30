import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import FilterBar from '@/components/FilterBar.vue'

const inline = { template: '<div><slot /></div>' }
const select = {
  props: ['modelValue'],
  emits: ['update:modelValue'],
  template: '<div data-select :data-value="modelValue"><slot /></div>',
}
const option = { props: ['value'], template: '<div data-option>{{ value }}</div>' }
const stubs = {
  Select: select,
  SelectTrigger: inline,
  SelectValue: inline,
  SelectContent: inline,
  SelectItem: option,
}

function setup(filters = { worker: '', search: '' }) {
  return mount(FilterBar, {
    props: {
      id: 'logs',
      modelValue: filters,
      dropdowns: [{ key: 'worker', label: 'Worker', any: 'Any worker', options: ['w1', 'w2'] }],
      fields: [{ key: 'search', label: 'Search', type: 'text' as const }],
    },
    global: { stubs },
  })
}

const emitted = (wrapper: ReturnType<typeof setup>) =>
  (wrapper.emitted('update:modelValue') ?? []).map(([value]) => value)

describe('the filter bar', () => {
  it('offers a dropdown with a choice for any, shown while it has no value', () => {
    const wrapper = setup()

    expect(wrapper.findAll('[data-option]').map(found => found.text())).toEqual(['any', 'w1', 'w2'])
    expect(wrapper.get('[data-select]').attributes('data-value')).toBe('any')
  })

  it('sets a dropdown as it is chosen, and the choice for any as no value', async () => {
    const wrapper = setup({ worker: 'w1', search: '' })

    wrapper.findComponent(select).vm.$emit('update:modelValue', 'w2')
    wrapper.findComponent(select).vm.$emit('update:modelValue', 'any')

    expect(emitted(wrapper)).toEqual([
      { worker: 'w2', search: '' },
      { worker: '', search: '' },
    ])
  })

  it('sets a field once it is committed, trimmed, and not at each key', async () => {
    const wrapper = setup()
    const field = wrapper.get('#logs-search')

    ;(field.element as HTMLInputElement).value = ' error '
    await field.trigger('input')
    expect(emitted(wrapper)).toEqual([])

    await field.trigger('change')
    expect(emitted(wrapper)).toEqual([{ worker: '', search: 'error' }])
  })

  it('changes nothing when a field is committed again with the value it has', async () => {
    const wrapper = setup({ worker: '', search: 'error' })
    const field = wrapper.get('#logs-search')

    ;(field.element as HTMLInputElement).value = 'error'
    await field.trigger('change')

    expect(emitted(wrapper)).toEqual([])
  })
})

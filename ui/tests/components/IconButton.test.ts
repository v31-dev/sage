import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { Edit } from 'lucide-vue-next'
import IconButton from '@/components/IconButton.vue'

function mountButton(props: { destructive?: boolean; disabled?: boolean; loading?: boolean } = {}) {
  const onClick = vi.fn()
  const wrapper = mount(IconButton, {
    props: { icon: Edit, label: 'Edit', ...props },
    attrs: { onClick, class: 'extra' },
    global: { stubs: { TooltipContent: { template: '<div data-tooltip><slot /></div>' } } },
  })
  return { wrapper, onClick }
}

describe('IconButton', () => {
  it('shows the icon alone, named by its label, which is also its tooltip', () => {
    const { wrapper } = mountButton()
    const button = wrapper.get('button')
    expect(button.text()).toBe('')
    expect(button.attributes('aria-label')).toBe('Edit')
    expect(button.find('svg').exists()).toBe(true)
    expect(wrapper.get('[data-tooltip]').text()).toBe('Edit')
  })

  it('passes its attributes to the button, not the wrapper', async () => {
    const { wrapper, onClick } = mountButton()
    expect(wrapper.get('button').classes()).toContain('extra')
    expect(wrapper.get('[data-icon-button]').classes()).not.toContain('extra')
    await wrapper.get('button').trigger('click')
    expect(onClick).toHaveBeenCalledOnce()
  })

  it('keeps its tooltip on the wrapper when disabled, and a click on the wrapper does nothing', async () => {
    const { wrapper, onClick } = mountButton({ disabled: true })
    expect(wrapper.get('button').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-icon-button]').element.contains(wrapper.get('button').element)).toBe(
      true
    )
    expect(wrapper.get('[data-tooltip]').text()).toBe('Edit')
    await wrapper.get('[data-icon-button]').trigger('click')
    expect(onClick).not.toHaveBeenCalled()
  })

  it('tints a destructive icon and shows a spinner while loading', () => {
    expect(mountButton({ destructive: true }).wrapper.get('button').classes()).toContain(
      'text-destructive'
    )
    expect(mountButton().wrapper.get('button').classes()).not.toContain('text-destructive')
    const loading = mountButton({ loading: true }).wrapper.get('button')
    expect(loading.find('.animate-spin').exists()).toBe(true)
  })
})

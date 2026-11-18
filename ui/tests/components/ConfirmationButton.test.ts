import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { Trash } from 'lucide-vue-next'
import ConfirmationButton from '@/components/ConfirmationButton.vue'

function mountButton(props: { iconOnly?: boolean; destructive?: boolean } = {}) {
  return mount(ConfirmationButton, {
    props: {
      triggerText: 'Delete',
      title: 'Delete Domain',
      body: 'Are you sure?',
      icon: Trash,
      onConfirm: vi.fn(async () => {}),
      ...props,
    },
    attachTo: document.body,
  })
}

describe('ConfirmationButton', () => {
  it('shows the icon alone, named by its trigger text', () => {
    const wrapper = mountButton({ iconOnly: true })
    const button = wrapper.get('button')
    expect(button.text()).toBe('')
    expect(button.attributes('aria-label')).toBe('Delete')
    expect(button.find('svg').exists()).toBe(true)
    wrapper.unmount()
  })

  it('tints a destructive icon instead of filling the button', () => {
    const wrapper = mountButton({ iconOnly: true, destructive: true })
    const classes = wrapper.get('button').classes()
    expect(classes).toContain('text-destructive')
    expect(classes).not.toContain('bg-destructive')
    wrapper.unmount()
  })

  it('opens the confirmation from the icon, confirmed by the trigger text', async () => {
    const wrapper = mountButton({ iconOnly: true, destructive: true })
    await wrapper.get('button').trigger('click')
    await flushPromises()
    const confirm = [...document.body.querySelectorAll('button')].find(
      button => button.textContent?.trim() === 'Delete'
    )
    expect(confirm).toBeDefined()
    wrapper.unmount()
  })

  it('keeps the labelled button by default', () => {
    const wrapper = mountButton({ destructive: true })
    const button = wrapper.get('button')
    expect(button.text()).toBe('Delete')
    expect(button.attributes('aria-label')).toBeUndefined()
    wrapper.unmount()
  })
})

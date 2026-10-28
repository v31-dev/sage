import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import SocketStatus from '@/components/layout/SocketStatus.vue'
import { connection } from '@/lib/updates'

function label() {
  return mount(SocketStatus).get('[role="img"]').attributes('aria-label')
}

beforeEach(() => {
  Object.assign(connection, { state: 'connecting', transport: null, heartbeatAt: null })
})

describe('the live updates indicator', () => {
  it('says it is connecting while it connects', () => {
    expect(label()).toBe('Connecting to the manager')
  })

  it('says live updates are on once connected over a WebSocket', () => {
    Object.assign(connection, { state: 'connected', transport: 'websocket' })
    expect(label()).toBe('Live updates are on')
  })

  it('says so when it is connected over the slower fallback', () => {
    Object.assign(connection, { state: 'connected', transport: 'polling' })
    expect(label()).toBe('Live updates are on, over a slower connection')
  })

  it('says it is not connected when it is not', () => {
    Object.assign(connection, { state: 'disconnected' })
    expect(label()).toBe('Not connected to the manager, trying again')
  })

  it('pulses for a heartbeat only while connected', async () => {
    const wrapper = mount(SocketStatus)
    expect(wrapper.find('[aria-hidden="true"]').exists()).toBe(false)

    Object.assign(connection, { state: 'connected', transport: 'websocket', heartbeatAt: 1 })
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[aria-hidden="true"]').exists()).toBe(true)

    Object.assign(connection, { state: 'disconnected' })
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[aria-hidden="true"]').exists()).toBe(false)
  })

  describe('the tooltip', () => {
    async function tooltipText() {
      const wrapper = mount(SocketStatus, { attachTo: document.body })
      await wrapper.get('span.relative').trigger('focus')
      await wrapper.vm.$nextTick()
      const text = document.body.textContent ?? ''
      wrapper.unmount()
      return text
    }

    afterEach(() => {
      document.body.innerHTML = ''
      vi.useRealTimers()
    })

    it('says what the state is, with nothing else before the first heartbeat', async () => {
      Object.assign(connection, { state: 'connected', transport: 'websocket' })
      expect(await tooltipText()).toContain('Live updates are on')
      expect(await tooltipText()).not.toContain('heartbeat')
    })

    it('says when the last heartbeat was, as a sentence', async () => {
      vi.useFakeTimers()
      vi.setSystemTime(new Date('2026-10-28T10:00:10Z'))
      Object.assign(connection, {
        state: 'connected',
        transport: 'websocket',
        heartbeatAt: new Date('2026-10-28T10:00:08Z').getTime(),
      })
      expect(await tooltipText()).toMatch(/Live updates are on\. Last heartbeat just now \(/)
    })
  })
})

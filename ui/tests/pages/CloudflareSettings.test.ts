import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, expect, it, vi } from 'vitest'
import CloudflareSettings from '@/pages/Settings/CloudflareSettings.vue'
import type { AppInfo } from '@/services/api'
import { useAppStore } from '@/stores/app'

vi.mock('vue-router', () => ({ onBeforeRouteLeave: vi.fn() }))

beforeEach(() => {
  setActivePinia(createPinia())
})

it('shows independent certificate names, expiries and maintenance errors', () => {
  useAppStore().info = {
    domain: 'example.test',
    name: 'manager',
    settings: {
      cloudflare: {
        state: 'error',
        details: {
          account_id: null,
          tunnel_id: null,
          tunnel_name: 'platform',
          tunnel_status: 'healthy',
          certificates: [
            {
              id: 'manager',
              names: ['manager.core.example.test'],
              valid: true,
              expiry: null,
              error: null,
            },
            {
              id: 'discovery',
              names: ['discovery.int.example.test'],
              valid: false,
              expiry: null,
              error: 'CA unavailable',
            },
            {
              id: 'worker:w1',
              names: ['*.red.int.example.test'],
              valid: true,
              expiry: '2027-02-02T00:00:00+00:00',
              error: null,
            },
            {
              id: 'worker:gone',
              names: [],
              valid: false,
              expiry: null,
              error: 'Revocation failed',
            },
          ],
        },
      },
    },
  } as AppInfo
  const wrapper = mount(CloudflareSettings, {})
  const text = wrapper.text()
  expect(text).not.toContain('Resync Traefik')
  expect(text).toContain('Manager certificate')
  expect(text).toContain('Discovery certificate')
  expect(text).toContain('Worker w1')
  expect(text).toContain('*.red.int.example.test')
  expect(text).toContain('discovery.int.example.test: not ready')
  expect(text).toContain('CA unavailable')
  expect(text).toContain('Pending retirement')
  expect(text).toContain('Revocation failed')
  expect(wrapper.findAll('[data-certificate-names]')).toHaveLength(4)
})

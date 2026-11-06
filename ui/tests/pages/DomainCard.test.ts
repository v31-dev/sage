import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import DomainCard from '@/pages/Application/DomainCard.vue'
import type { Application, Container, Domain, WorkerListItem } from '@/services/api'
import { useAppStore } from '@/stores/app'

const domain = (overrides: Partial<Domain> = {}) =>
  ({
    id: 1,
    name: 'shop',
    type: 'public',
    port: 80,
    service: null,
    pool: null,
    status: 'synced',
    sync_error: null,
    synced_hash: null,
    synced_at: null,
    host: 'shop.example.test',
    address: 'https://shop.example.test',
    worker: 'w1',
    container: { id: 1, status: 'active', worker: 'w1', application: {} },
    ...overrides,
  }) as Domain

const container = (hostname: string, status: Container['status']) =>
  ({ worker: { hostname }, status }) as unknown as Container

function mountCard(current: Domain, workers: Partial<WorkerListItem>[] = []) {
  useAppStore().workers = workers as WorkerListItem[]
  return mount(DomainCard, {
    props: {
      application: {
        containers: [container('w1', 'active'), container('w2', 'inactive')],
      } as unknown as Application,
      domain: current,
    },
    global: { stubs: { CopyButton: true } },
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
})

describe('a domain on its application page', () => {
  it('links to its own address and shows its worker, the state of its container, its type, service and pool', () => {
    const wrapper = mountCard(domain({ service: 'shop', pool: 'eu' }))

    expect(wrapper.get('a').attributes('href')).toBe('https://shop.example.test')
    const content = wrapper.get('[data-slot=card-content]').element
    expect([...content.children].map(child => child.textContent!.trim())).toEqual([
      'w1',
      'active',
      'synced',
      'Public',
      'Service shop',
      'Pool eu',
    ])
  })

  it('of a TCP domain is its address, not a link', () => {
    const wrapper = mountCard(
      domain({
        type: 'tcp',
        host: 'db.red.int.example.test',
        address: 'db.red.int.example.test:8443',
      })
    )

    expect(wrapper.find('a').exists()).toBe(false)
    expect(wrapper.get('[data-slot=card-title]').text()).toBe('db.red.int.example.test:8443')
  })

  it.each([
    ['pending', 'pending'],
    ['syncing', 'syncing'],
    ['synced', 'synced'],
    ['error', 'sync failed'],
  ] as const)(
    'shows the record sync state %s separately from container health',
    (status, label) => {
      const wrapper = mountCard(
        domain({ status, sync_error: status === 'error' ? 'DNS: unavailable' : null })
      )

      expect(wrapper.text()).toContain('active')
      expect(wrapper.text()).toContain(label)
      if (status === 'error')
        expect(wrapper.get('[title]').attributes('title')).toBe('DNS: unavailable')
    }
  )

  it.each([
    [{ covered: false, synced: false, expiry: null, error: null }, 'waiting for the certificate'],
    [{ covered: true, synced: false, expiry: null, error: null }, 'syncing the certificate'],
  ])(
    'of an internal domain shows the certificate on its worker until it is ready there',
    (certificate, shown) => {
      const workers = [{ hostname: 'w1', certificate }]
      const internal = domain({ type: 'internal', address: 'https://shop.red.int.example.test' })

      expect(mountCard(internal, workers).text()).toContain(shown)
      expect(mountCard(domain(), workers).text()).not.toContain(shown)
      expect(
        mountCard(internal, [
          {
            hostname: 'w1',
            certificate: { covered: true, synced: true, expiry: null, error: null },
          },
        ]).text()
      ).not.toContain('certificate')
    }
  )
})

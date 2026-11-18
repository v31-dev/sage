import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import MigrationNotice from '@/components/layout/MigrationNotice.vue'
import type { AppInfo, MigrationRunSummary } from '@/services/api'
import { useAppStore } from '@/stores/app'

const push = vi.fn()
vi.mock('vue-router', () => ({ useRouter: () => ({ push }) }))

const run = (fields: Partial<MigrationRunSummary> = {}): MigrationRunSummary => ({
  run: 'r1',
  trigger: 'boot',
  release: '0.2.0',
  outcome: 'migrated',
  actions: 3,
  ...fields,
})

async function shown(migration: MigrationRunSummary | null) {
  useAppStore().info = { migration } as AppInfo
  mount(MigrationNotice)
  await flushPromises()
}

beforeEach(() => {
  setActivePinia(createPinia())
  push.mockReset()
  localStorage.clear()
})

describe('after a migration', () => {
  it('that left steps to do by hand, the Upgrade page opens on its run', async () => {
    await shown(run())
    expect(push).toHaveBeenCalledWith({ path: '/upgrade', query: { migration: 'r1' } })
  })

  it('it opens once per run in this browser', async () => {
    await shown(run())
    await shown(run())
    expect(push).toHaveBeenCalledTimes(1)
    await shown(run({ run: 'r2' }))
    expect(push).toHaveBeenLastCalledWith({ path: '/upgrade', query: { migration: 'r2' } })
  })

  it('that left nothing to do, or without one, nothing opens', async () => {
    await shown(run({ actions: 0 }))
    await shown(null)
    expect(push).not.toHaveBeenCalled()
  })
})

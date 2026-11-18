import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, expect, it, vi } from 'vitest'
import Applications from '@/pages/Applications'
import ProjectCard from '@/pages/Applications/ProjectCard.vue'
import { fetchProjects, type ApplicationSummary, type ProjectPage } from '@/services/api'

vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  fetchProjects: vi.fn(),
}))

function application(name: string, active: number, total: number): ApplicationSummary {
  return {
    id: 1,
    name,
    label: name.toUpperCase(),
    description: `${name} notes`,
    type: 'docker',
    image: 'nginx',
    repo: null,
    container_count: total,
    active_count: active,
    created_at: '2026-01-01T12:00:00',
    updated_at: '2026-01-01T12:00:00',
  }
}

function project(name: string, applications: ApplicationSummary[]): ProjectPage {
  return {
    name,
    label: `${name} label`,
    description: `${name} description`,
    env: null,
    application_count: applications.length,
    created_at: '2026-01-01T12:00:00',
    updated_at: '2026-01-01T12:00:00',
    applications,
  }
}

const projects = [
  project('shop', [application('web', 1, 2), application('api', 0, 0)]),
  project('empty', []),
]

function mountPage() {
  return mount(Applications)
}

function deleteButton(card: ReturnType<ReturnType<typeof mountPage>['findComponent']>) {
  return card.findAll('button').find(button => button.text() === 'Delete')!
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.mocked(fetchProjects).mockReset().mockResolvedValue(structuredClone(projects))
})

it('shows each project with its own applications and how many of their containers are active', async () => {
  const wrapper = mountPage()
  await flushPromises()
  const [shop, empty] = wrapper.findAllComponents(ProjectCard).map(card => card.text())

  expect(shop).toContain('shop label')
  expect(shop).toContain('shop description')
  expect(shop).toContain('WEB')
  expect(shop).toContain('1 of 2 active')
  expect(shop).toContain('No containers')
  expect(empty).toContain('No applications')
  expect(empty).not.toContain('WEB')
})

it('allows deleting only a project with no applications', async () => {
  const wrapper = mountPage()
  await flushPromises()
  const [shop, empty] = wrapper.findAllComponents(ProjectCard)

  expect(deleteButton(shop).attributes('disabled')).toBeDefined()
  expect(deleteButton(empty).attributes('disabled')).toBeUndefined()
})

it('keeps the newer list when an older reload answers after it', async () => {
  const wrapper = mountPage()
  await flushPromises()
  let answerOlder: (rows: ProjectPage[]) => void = () => {}
  vi.mocked(fetchProjects)
    .mockReturnValueOnce(new Promise(resolve => (answerOlder = resolve)))
    .mockResolvedValueOnce([project('newer', [])])
  const reload = wrapper.findComponent(ProjectCard).props('loadProjects')

  const older = reload()
  await reload()
  answerOlder([project('older', [])])
  await older
  await flushPromises()

  expect(wrapper.text()).toContain('newer label')
  expect(wrapper.text()).not.toContain('older label')
})

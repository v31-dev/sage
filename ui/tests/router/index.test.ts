import { describe, expect, it, vi } from 'vitest'
import Application from '@/pages/Application'
import ApplicationLogs from '@/pages/ApplicationLogs.vue'
import ApplicationMetrics from '@/pages/ApplicationMetrics.vue'
import Backup from '@/pages/Backup'
import CloudflareSettings from '@/pages/Settings/CloudflareSettings.vue'
import DebugSettings from '@/pages/Settings/DebugSettings.vue'
import Home from '@/pages/Home.vue'
import NotificationsSettings from '@/pages/Settings/NotificationsSettings.vue'
import Project from '@/pages/Project'
import Projects from '@/pages/Projects.vue'
import Requests from '@/pages/Requests.vue'
import S3Settings from '@/pages/Settings/S3Settings.vue'
import System from '@/pages/System.vue'
import TailscaleSettings from '@/pages/Settings/TailscaleSettings.vue'
import Tasks from '@/pages/Tasks'
import Upgrade from '@/pages/Settings/Upgrade.vue'
import Worker from '@/pages/Worker'
import WorkerMetrics from '@/pages/WorkerMetrics.vue'
import Workers from '@/pages/Workers'
// The pages come first on purpose: a page that imported the router would leave its route empty.
import router from '@/router'

describe('the route table', () => {
  const routes = router.getRoutes()

  it('gives every route a name, and each name once', () => {
    const names = routes.map(route => route.name)

    expect(names.every(name => typeof name === 'string' && name.length > 0)).toBe(true)
    expect(new Set(names).size).toBe(names.length)
  })

  it('gives every route a component', () => {
    for (const route of routes) {
      expect(route.components?.default, String(route.name)).toBeTruthy()
    }
  })

  it('has no path twice', () => {
    const paths = routes.map(route => route.path)
    expect(new Set(paths).size).toBe(paths.length)
  })
})

describe('resolving an address', () => {
  it.each([
    ['/', 'Home', {}],
    ['/projects', 'Projects', {}],
    ['/projects/demo', 'Project', { projectId: 'demo' }],
    ['/projects/demo/web', 'Application', { projectId: 'demo', appId: 'web' }],
    ['/projects/demo/web/logs', 'ApplicationLogs', { projectId: 'demo', appId: 'web' }],
    ['/projects/demo/web/metrics', 'ApplicationMetrics', { projectId: 'demo', appId: 'web' }],
    ['/workers', 'Workers', {}],
    ['/workers/w1', 'Worker', { hostname: 'w1' }],
    ['/workers/w1/metrics', 'WorkerMetrics', { hostname: 'w1' }],
    ['/backups', 'Backups', {}],
    ['/tasks', 'Tasks', {}],
    ['/system', 'System', {}],
    ['/requests', 'Requests', {}],
    ['/tailscale', 'TailscaleSettings', {}],
    ['/cloudflare', 'CloudflareSettings', {}],
    ['/s3', 'S3Settings', {}],
    ['/notifications', 'NotificationsSettings', {}],
    ['/upgrade', 'Upgrade', {}],
    ['/debug', 'DebugSettings', {}],
  ])('%s is the page %s', (path, name, params) => {
    const resolved = router.resolve(path)

    expect(resolved.name).toBe(name)
    expect(resolved.params).toEqual(params)
  })

  it.each([
    ['Home', Home],
    ['Projects', Projects],
    ['Project', Project],
    ['Application', Application],
    ['ApplicationLogs', ApplicationLogs],
    ['ApplicationMetrics', ApplicationMetrics],
    ['Requests', Requests],
    ['System', System],
    ['Tasks', Tasks],
    ['Workers', Workers],
    ['Worker', Worker],
    ['WorkerMetrics', WorkerMetrics],
    ['Backups', Backup],
    ['TailscaleSettings', TailscaleSettings],
    ['CloudflareSettings', CloudflareSettings],
    ['S3Settings', S3Settings],
    ['NotificationsSettings', NotificationsSettings],
    ['Upgrade', Upgrade],
    ['DebugSettings', DebugSettings],
  ])('shows the page of its own for the route %s', (name, page) => {
    expect(router.getRoutes().find(route => route.name === name)?.components?.default).toBe(page)
  })

  it('has a route for each of those pages and no other', () => {
    expect(router.getRoutes()).toHaveLength(19)
  })

  it('does not mistake a longer address for a shorter page', () => {
    expect(router.resolve('/projects/demo/web/logs').name).not.toBe('Application')
    expect(router.resolve('/workers/w1/metrics').name).not.toBe('Worker')
  })

  it('matches nothing for an address that is not a page, and the router says so', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})

    expect(router.resolve('/no/such/page').matched).toHaveLength(0)
    expect(router.resolve('/projects/demo/web/logs/extra').matched).toHaveLength(0)

    expect(warn).toHaveBeenCalledTimes(2)
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('"/no/such/page"'))
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('"/projects/demo/web/logs/extra"'))
  })

  it('builds the address of a page from its name and parameters', () => {
    expect(
      router.resolve({ name: 'Application', params: { projectId: 'demo', appId: 'web' } }).path
    ).toBe('/projects/demo/web')
    expect(router.resolve({ name: 'WorkerMetrics', params: { hostname: 'w1' } }).path).toBe(
      '/workers/w1/metrics'
    )
  })
})

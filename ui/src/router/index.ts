import { createRouter, createWebHistory } from 'vue-router'
import Home from '@/pages/Home.vue'
import Projects from '@/pages/Projects.vue'
import Project from '@/pages/Project'
import System from '@/pages/System.vue'
import Application from '@/pages/Application'
import ApplicationLogs from '@/pages/ApplicationLogs.vue'
import ApplicationMetrics from '@/pages/ApplicationMetrics.vue'
import CloudflareSettings from '@/pages/Settings/CloudflareSettings.vue'
import NotificationsSettings from '@/pages/Settings/NotificationsSettings.vue'
import S3Settings from '@/pages/Settings/S3Settings.vue'
import TailscaleSettings from '@/pages/Settings/TailscaleSettings.vue'
import Upgrade from '@/pages/Settings/Upgrade.vue'
import Worker from '@/pages/Worker'
import Workers from '@/pages/Workers'
import WorkerMetrics from '@/pages/WorkerMetrics.vue'
import Requests from '@/pages/Requests.vue'
import Backup from '@/pages/Backup'
import Tasks from '@/pages/Tasks'

const routes = [
  { path: '/', name: 'Home', component: Home },
  { path: '/projects', name: 'Projects', component: Projects },
  { path: '/projects/:projectId', name: 'Project', component: Project },
  { path: '/projects/:projectId/:appId', name: 'Application', component: Application },
  { path: '/projects/:projectId/:appId/logs', name: 'ApplicationLogs', component: ApplicationLogs },
  {
    path: '/projects/:projectId/:appId/metrics',
    name: 'ApplicationMetrics',
    component: ApplicationMetrics,
  },
  { path: '/requests', name: 'Requests', component: Requests },
  { path: '/system', name: 'System', component: System },
  { path: '/tasks', name: 'Tasks', component: Tasks },
  { path: '/workers', name: 'Workers', component: Workers },
  { path: '/workers/:hostname', name: 'Worker', component: Worker },
  { path: '/workers/:hostname/metrics', name: 'WorkerMetrics', component: WorkerMetrics },
  { path: '/backups', name: 'Backups', component: Backup },
  { path: '/tailscale', name: 'TailscaleSettings', component: TailscaleSettings },
  { path: '/cloudflare', name: 'CloudflareSettings', component: CloudflareSettings },
  { path: '/s3', name: 'S3Settings', component: S3Settings },
  {
    path: '/notifications',
    name: 'NotificationsSettings',
    component: NotificationsSettings,
  },
  { path: '/upgrade', name: 'Upgrade', component: Upgrade },
]

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
})

export default router

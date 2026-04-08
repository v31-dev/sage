import { createRouter, createWebHistory } from 'vue-router';
import Home from '@/pages/Home.vue';
import Projects from '@/pages/Projects.vue';
import Project from '@/pages/Project';
import System from '@/pages/System.vue';
import Logs from '@/pages/Logs.vue';
import Application from '@/pages/Application';
import ApplicationLogs from '@/pages/ApplicationLogs.vue';
import ApplicationMetrics from '@/pages/ApplicationMetrics.vue';
import Workers from '@/pages/Workers.vue';
import WorkerMetrics from '@/pages/WorkerMetrics.vue';
import Settings from '@/pages/Settings.vue';
import Requests from '@/pages/Requests.vue';

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
  { path: '/logs', name: 'Logs', component: Logs },
  { path: '/workers', name: 'Workers', component: Workers },
  { path: '/workers/:hostname/metrics', name: 'WorkerMetrics', component: WorkerMetrics },
  { path: '/settings', name: 'Settings', component: Settings },
];

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
});

export default router;

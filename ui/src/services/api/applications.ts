import { request } from '@/lib/api'
import { applicationPath, type Fields } from './_common'
import type { Project } from './projects'
import type { Worker } from './workers'

export interface Application {
  name: string
  label: string
  project: Project
  description: string | null
  type: 'docker' | 'git'
  repo: string | null
  path: string | null
  image: string | null
  env: string | null
  args: string | null
  build_secrets: string | null
  command: string | null
  domains: Domain[]
  containers: Container[]
  volumes: Volume[]
  container_count: number
  status: 'active' | 'inactive' | 'deploying' | 'stopping' | 'backup' | 'restoring' | 'error'
  domains_synced: boolean
  deployed_at: Date | null
  deployed_hash: string | null
  config_dirty?: boolean
  created_at: Date
  updated_at: Date
}

export const APPLICATION_BUSY_STATUSES: Application['status'][] = [
  'deploying',
  'stopping',
  'backup',
  'restoring',
]

export const APPLICATION_STOP_ELIGIBLE_STATUSES: Application['status'][] = ['active', 'error']

export interface Domain {
  name: string
  type: 'internal' | 'public' | 'tcp'
  port: number
  application: Application
  created_at: Date
  updated_at: Date
}

// A container's part of a task: its lines are the task's `application_task_id` and part `container_task_id`.
export interface Event {
  application_task_id: string
  container_task_id: string
  type: 'deploy' | 'stop' | 'delete' | 'backup' | 'restore'
  created_at: Date
}

export interface Container {
  worker: Worker
  status: 'active' | 'inactive' | 'deploying' | 'stopping' | 'backup' | 'restoring' | 'error'
  domain_tag: string | null
  events: Event[]
  created_at: Date
  updated_at: Date
  application: Application
}

export interface Volume {
  name: string
  path: string
  backup_cron: string | null
  application: Application
  created_at: Date
  updated_at: Date
}

export const fetchApplication = (project: string, name: string) =>
  request<Application>('GET', applicationPath(project, name))

export const createApplication = (project: string, data: Fields) =>
  request<Application>('POST', `/projects/${project}/applications/`, { body: data })

export const updateApplication = (project: string, name: string, data: Fields) =>
  request<Application>('PUT', applicationPath(project, name), { body: data })

export const deleteApplication = (project: string, name: string) =>
  request<unknown>('DELETE', applicationPath(project, name))

export const deployApplication = (project: string, name: string) =>
  request<unknown>('POST', `${applicationPath(project, name)}/deploy`)

export const stopApplication = (project: string, name: string) =>
  request<unknown>('POST', `${applicationPath(project, name)}/stop`)

export const createContainer = (project: string, application: string, data: Fields) =>
  request<Container>('POST', `${applicationPath(project, application)}/containers/`, {
    body: data,
  })

export const updateContainer = (
  project: string,
  application: string,
  hostname: string,
  data: Fields
) =>
  request<Container>('PUT', `${applicationPath(project, application)}/containers/${hostname}`, {
    body: data,
  })

// `force` removes the container from Sage without cleaning up after it on an offline worker.
export const deleteContainer = (
  project: string,
  application: string,
  hostname: string,
  force = false
) =>
  request<unknown>('DELETE', `${applicationPath(project, application)}/containers/${hostname}`, {
    query: { force: force || null },
  })

export const createVolume = (project: string, application: string, data: Fields) =>
  request<Volume>('POST', `${applicationPath(project, application)}/volumes/`, { body: data })

export const updateVolume = (project: string, application: string, name: string, data: Fields) =>
  request<Volume>('PUT', `${applicationPath(project, application)}/volumes/${name}`, {
    body: data,
  })

export const deleteVolume = (project: string, application: string, name: string) =>
  request<unknown>('DELETE', `${applicationPath(project, application)}/volumes/${name}`)

// A domain is named by its type and name, `<type>:<name>`.
export const createDomain = (project: string, application: string, data: Fields) =>
  request<Domain>('POST', `${applicationPath(project, application)}/domains/`, { body: data })

export const updateDomain = (project: string, application: string, key: string, data: Fields) =>
  request<Domain>('PUT', `${applicationPath(project, application)}/domains/${key}`, {
    body: data,
  })

export const deleteDomain = (project: string, application: string, key: string) =>
  request<unknown>('DELETE', `${applicationPath(project, application)}/domains/${key}`)

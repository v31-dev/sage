import { request } from '@/lib/api'
import { applicationPath, type Fields } from './_common'
import type { Domain } from './domains'

// What an application runs, and how many of its containers are active; an application has no state of its own.
export interface ApplicationSummary {
  id: number
  name: string
  label: string
  description: string | null
  type: 'docker' | 'git'
  image: string | null
  repo: string | null
  container_count: number
  active_count: number
  created_at: string
  updated_at: string
}

export interface Application extends ApplicationSummary {
  path: string | null
  env: string | null
  args: string | null
  build_secrets: string | null
  command: string | null
  project: { name: string; label: string }
  volumes: Volume[]
  containers: Container[]
}

export type ContainerHeadline =
  | 'active'
  | 'inactive'
  | 'deploying'
  | 'stopping'
  | 'backup'
  | 'restoring'
  | 'error'
  | 'checking'

// What a container's state is, as the manager decides it: the headline, how its configuration compares with what it
// was deployed with, and for an error (or a check under way) why.
export interface ContainerView {
  headline: ContainerHeadline
  config: 'current' | 'dirty' | 'not_deployed' | 'unknown'
  reason: string | null
  detail: string | null
}

// Docker's state of the container as last read (`absent` when the worker has none), its health and how it last exited.
export interface RuntimeReading {
  state: string
  deployment_id: string | null
  health: string | null
  exit_code: number | null
  oom_killed: boolean
}

// How much is known of the runtime now; the reading is current only while `knowledge` is `known`.
export interface Runtime {
  knowledge: 'unobserved' | 'known' | 'stale' | 'unreachable'
  reading: RuntimeReading | null
}

// The operations that show on a container while they run or wait.
export type ContainerOperation =
  | 'deploy'
  | 'stop'
  | 'delete'
  | 'backup_stopped'
  | 'backup_preserve'
  | 'restore_stopped'

// What people call each operation on a container, and the reset of one Sage stopped.
export const OPERATION_LABELS: Record<ContainerOperation | 'reset', string> = {
  deploy: 'Deploy',
  stop: 'Stop',
  delete: 'Delete',
  backup_stopped: 'Backup',
  backup_preserve: 'Automatic backup',
  restore_stopped: 'Restore',
  reset: 'Reset',
}

// A request waiting its turn on the container; `cancellable` when Cancel takes it.
export interface QueuedRequest {
  operation: ContainerOperation
  task_id: string
  cancellable: boolean
}

// A step of an operation on the container; its lines are those of the task `task_id`.
export interface ContainerEvent {
  id: number
  action: string
  transition: string
  operation_id: string | null
  reason: Record<string, unknown> | null
  task_id: string | null
  created_at: string
}

export interface ContainerApplication {
  name: string
  label: string
  project: { name: string; label: string }
}

// A container as a list of containers shows it.
export interface ContainerReference {
  id: string
  worker: { hostname: string; subdomain: string | null }
  application: ContainerApplication
  view: ContainerView
}

// The record of the container's last successful deployment, as last read from its worker.
export interface ContainerTarget {
  kind: string
  record: {
    applied: { deployment_id: string; config_hash: string; committed_at: string } | null
  } | null
}

export interface Container extends ContainerReference {
  target: ContainerTarget
  runtime: Runtime
  live: ContainerOperation | null
  routable: boolean | null
  queued: QueuedRequest[]
  domains: Domain[]
  events: ContainerEvent[]
  created_at: string
  updated_at: string
}

// Whether the container refuses a Deploy, Stop, backup or restore now: one of its own operations runs, or a Delete runs
// or waits. An automatic backup running leaves a request to wait its turn behind it.
export function refusesRequests(container: Container): boolean {
  return (
    (container.live !== null && container.live !== 'backup_preserve') ||
    container.queued.some(request => request.operation === 'delete')
  )
}

export interface Volume {
  id: string
  name: string
  path: string
  backup_cron: string | null
  created_at: string
  updated_at: string
}

export const fetchApplication = (project: string, name: string) =>
  request<Application>('GET', applicationPath(project, name))

export const createApplication = (project: string, data: Fields) =>
  request<Application>('POST', `/projects/${project}/applications/`, { body: data })

export const updateApplication = (project: string, name: string, data: Fields) =>
  request<Application>('PUT', applicationPath(project, name), { body: data })

export const deleteApplication = (project: string, name: string) =>
  request<unknown>('DELETE', applicationPath(project, name))

// Deploy all and Stop all: one task per container.
export const deployApplication = (project: string, name: string) =>
  request<{ task_ids: string[] }>('POST', `${applicationPath(project, name)}/deploy`)

export const stopApplication = (project: string, name: string) =>
  request<{ task_ids: string[] }>('POST', `${applicationPath(project, name)}/stop`)

export const createContainer = (project: string, application: string, data: Fields) =>
  request<Container>('POST', `${applicationPath(project, application)}/containers/`, {
    body: data,
  })

const containerPath = (project: string, application: string, id: string) =>
  `${applicationPath(project, application)}/containers/${id}`

export const deployContainer = (project: string, application: string, id: string) =>
  request<{ task_id: string }>('POST', `${containerPath(project, application, id)}/deploy`)

export const stopContainer = (project: string, application: string, id: string) =>
  request<{ task_id: string }>('POST', `${containerPath(project, application, id)}/stop`)

// Takes the container's queued `operation` out of the queue.
export const cancelContainerRequest = (
  project: string,
  application: string,
  id: string,
  operation: ContainerOperation
) =>
  request<{ task_id: string }>(
    'POST',
    `${containerPath(project, application, id)}/cancel/${operation}`
  )

// `force` removes the container from Sage without cleaning up after it on a worker that can't be reached.
export const deleteContainer = (project: string, application: string, id: string, force = false) =>
  request<{ task_id: string }>('DELETE', containerPath(project, application, id), {
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

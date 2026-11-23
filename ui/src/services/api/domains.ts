import { request } from '@/lib/api'
import type { Fields } from './_common'
import type { ContainerApplication, ContainerReference } from './applications'

export type DomainStatus = 'pending' | 'syncing' | 'synced' | 'error'

// A Domain's state as the manager decides it: whether its route and DNS records are what its configuration gives,
// with the error of its last apply.
export interface DomainView {
  status: DomainStatus
  conforms: boolean | null
  reason: string | null
  error: string | null
}

// A name of one container, deleted with it: public `<name>.<domain>`, or internal or TCP
// `<name>.<subdomain>.int.<domain>` under its container's worker's subdomain.
// Names that share a `service` are listed together at discovery, apart for public and internal; `pool` is a subgroup.
export interface Domain {
  id: string
  name: string
  type: 'public' | 'internal' | 'tcp'
  port: number
  service: string | null
  pool: string | null
  cors_origins: string[] | null
  // None for an internal or TCP one without a worker.
  host: string | null
  address: string | null
  worker: string | null
  container: { id: string; worker: string; application: ContainerApplication }
  // The applications allowed to call an internal or TCP name from their containers, by project and name.
  callers: (ContainerApplication & { id: number })[]
  view: DomainView
  created_at: string
  updated_at: string
}

// The Domains page: every domain, and every container with its state.
export interface DomainsResponse {
  domains: Domain[]
  containers: ContainerReference[]
}

export const fetchDomains = () => request<DomainsResponse>('GET', '/domains/')

export const createDomain = (data: Fields) => request<Domain>('POST', '/domains/', { body: data })

export const updateDomain = (id: string, data: Fields) =>
  request<Domain>('PUT', `/domains/${id}`, { body: data })

export const deleteDomain = (id: string) => request<unknown>('DELETE', `/domains/${id}`)

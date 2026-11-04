import { request } from '@/lib/api'
import type { Fields } from './_common'
import type { Application } from './applications'

// The application a container belongs to, as the Domains page shows it.
export interface ContainerApplication {
  name: string
  label: string
  project: { name: string; label: string }
}

// A name linked to one container: public `<name>.<domain>`, or internal or TCP `<name>.<subdomain>.int.<domain>` under
// its worker's subdomain. Deleting the container unlinks it (`container` is none); an internal one keeps its worker.
// Names that share a `service` are listed together at discovery, apart for public and internal; `pool` is a subgroup.
export interface Domain {
  id: number
  name: string
  type: 'public' | 'internal' | 'tcp'
  port: number
  service: string | null
  pool: string | null
  // None for an internal or TCP one without a worker.
  host: string | null
  address: string | null
  worker: string | null
  container: {
    id: number
    status: Application['status']
    worker: string
    application: ContainerApplication
  } | null
}

// A container a domain can be linked to, with its worker's subdomain.
export interface LinkableContainer {
  id: number
  status: Application['status']
  worker: { hostname: string; subdomain: string | null }
  application: ContainerApplication
}

// The Domains page: every domain, and every container one can be linked to.
export interface DomainsResponse {
  domains: Domain[]
  containers: LinkableContainer[]
}

export const fetchDomains = () => request<DomainsResponse>('GET', '/domains/')

export const createDomain = (data: Fields) => request<Domain>('POST', '/domains/', { body: data })

export const updateDomain = (id: number, data: Fields) =>
  request<Domain>('PUT', `/domains/${id}`, { body: data })

export const deleteDomain = (id: number) => request<unknown>('DELETE', `/domains/${id}`)

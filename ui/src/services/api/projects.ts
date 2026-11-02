import { request } from '@/lib/api'
import type { Fields } from './_common'
import type { Application } from './applications'

export interface Project {
  name: string
  label: string
  description: string | null
  env: string | null
  applications: Application[]
  application_count: number
  created_at: Date
  updated_at: Date
}

export const fetchProjects = () => request<Project[]>('GET', '/projects/')

export const fetchProject = (name: string) => request<Project>('GET', `/projects/${name}`)

export const createProject = (data: Fields) =>
  request<Project>('POST', '/projects/', { body: data })

export const updateProject = (name: string, data: Fields) =>
  request<Project>('PUT', `/projects/${name}`, { body: data })

export const deleteProject = (name: string) => request<unknown>('DELETE', `/projects/${name}`)

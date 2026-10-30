import { request } from '@/lib/api'
import { applicationPath, download } from './_common'

export interface Backup {
  id: number
  s3_path: string
  source_volume_name: string | null
  type: string
  created_at: string
}

export interface BackupStatus {
  platform_backup_in_progress: boolean
  discovery_in_progress: boolean
}

// The platform backups.
export const fetchBackups = () => request<Backup[]>('GET', '/backups/')

export const fetchBackupStatus = () =>
  request<BackupStatus>('GET', '/backups/platform_backup_status')

export const createBackup = () => request<unknown>('POST', '/backups/')

// Lists the backups in S3 that are not listed yet.
export const discoverBackups = () => request<unknown>('POST', '/backups/discover')

export const deleteBackup = (id: number) => request<unknown>('DELETE', `/backups/${id}`)

export const restoreBackup = (id: number) => request<unknown>('POST', `/backups/${id}/restore`)

export const downloadBackup = (id: number) => download(`/backups/${id}/download`)

// The backups of a volume of an application.
const volumeBackupsPath = (project: string, application: string, volume: string) =>
  `${applicationPath(project, application)}/volumes/${volume}/backups`

export const fetchVolumeBackups = (project: string, application: string, volume: string) =>
  request<Backup[]>('GET', `${volumeBackupsPath(project, application, volume)}/`)

export const createVolumeBackup = (project: string, application: string, volume: string) =>
  request<unknown>('POST', `${volumeBackupsPath(project, application, volume)}/`)

export const restoreVolumeBackup = (
  project: string,
  application: string,
  volume: string,
  id: number,
  targetWorker: string
) =>
  request<unknown>('POST', `${volumeBackupsPath(project, application, volume)}/${id}/restore`, {
    body: { target_worker: targetWorker },
  })

export const downloadVolumeBackup = (
  project: string,
  application: string,
  volume: string,
  id: number
) => download(`${volumeBackupsPath(project, application, volume)}/${id}/download`)

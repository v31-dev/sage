import { request } from '@/lib/api'
import { applicationPath, download } from './_common'

// A backup in the configured store (`storage_id`): the platform's, or a volume's (`application`), with the volume's id
// and the name it had when it was taken.
export interface Backup {
  id: number
  type: 'platform' | 'application'
  s3_path: string
  storage_id: string
  source_volume_id: string | null
  source_volume_name: string | null
  application: number | null
  created_at: string
  updated_at: string
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

// A backup of the volume from each of the application's containers, all of them stopped: one task per container.
export const createVolumeBackup = (project: string, application: string, volume: string) =>
  request<{ task_ids: string[] }>('POST', `${volumeBackupsPath(project, application, volume)}/`)

// Restores the backup into a stopped container of the application (`container`, by id), into the volume it was taken
// of or another of the application's (`target`, by name).
export const restoreVolumeBackup = (
  project: string,
  application: string,
  volume: string,
  id: number,
  container: string,
  target: string
) =>
  request<{ task_id: string }>(
    'POST',
    `${volumeBackupsPath(project, application, volume)}/${id}/restore`,
    { body: { container, volume: target } }
  )

export const downloadVolumeBackup = (
  project: string,
  application: string,
  volume: string,
  id: number
) => download(`${volumeBackupsPath(project, application, volume)}/${id}/download`)

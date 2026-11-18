import { request } from '@/lib/api'
import { applicationPath, download } from './_common'

// Where a backup is: waiting or being taken, taken (`succeeded`), or not taken for the row's `reason`.
export type BackupState =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'interrupted'

// A backup in the configured store (`storage_id`): the platform's, or a volume's (`application`), with the volume's id
// and the name it had when it was taken, and the container and worker it was taken from. One per slot: the
// schedule's (`automatic`) or a request's. A platform backup names the release and database version that took it,
// and why it can't be restored, if it can't.
export interface Backup {
  id: string
  type: 'platform' | 'application'
  s3_path: string
  storage_id: string
  source_volume_id: string | null
  source_volume_name: string | null
  container_id: string | null
  worker_hostname: string | null
  application: number | null
  slot_at: string | null
  automatic: boolean
  status: BackupState
  reason: string | null
  created_at: string
  updated_at: string
  release: string | null
  schema_version: number | null
  restore_refusal: string | null
}

export interface BackupStatus {
  platform_backup_in_progress: boolean
  discovery_in_progress: boolean
}

// The platform backups, newest first.
export const fetchBackups = () => request<Backup[]>('GET', '/backups/')

export const fetchBackupStatus = () =>
  request<BackupStatus>('GET', '/backups/platform_backup_status')

export const createBackup = () => request<{ task_id: string }>('POST', '/backups/')

// Lists the backups in S3 that are not listed yet.
export const discoverBackups = () => request<{ task_id: string }>('POST', '/backups/discover')

// A completed backup's deletion is queued; one that was not completed is deleted at once, with no task.
export const deleteBackup = (id: string) =>
  request<{ task_id: string | null }>('DELETE', `/backups/${id}`)

export const restoreBackup = (id: string) =>
  request<{ task_id: string }>('POST', `/backups/${id}/restore`)

export const downloadBackup = (id: string) => download(`/backups/${id}/download`)

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
  id: string,
  container: string,
  target: string
) =>
  request<{ task_id: string }>(
    'POST',
    `${volumeBackupsPath(project, application, volume)}/${id}/restore`,
    { body: { container, volume: target } }
  )

// As `deleteBackup`, for one of the volume's backups.
export const deleteVolumeBackup = (
  project: string,
  application: string,
  volume: string,
  id: string
) =>
  request<{ task_id: string | null }>(
    'DELETE',
    `${volumeBackupsPath(project, application, volume)}/${id}`
  )

export const downloadVolumeBackup = (
  project: string,
  application: string,
  volume: string,
  id: string
) => download(`${volumeBackupsPath(project, application, volume)}/${id}/download`)

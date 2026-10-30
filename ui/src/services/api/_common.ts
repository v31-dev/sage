// What the resources' modules share; not part of the API layer's exports.
import { request } from '@/lib/api'
import { startDownload } from '@/lib/utils'

// The fields a create or an update sends; the manager checks them.
export type Fields = Record<string, unknown>

export const applicationPath = (project: string, application: string) =>
  `/projects/${project}/applications/${application}`

// A short-lived link to the file in S3, which the browser then saves.
export async function download(path: string) {
  const { url } = await request<{ url: string }>('GET', path)
  startDownload(url)
}

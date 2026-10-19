// Saves a backup. The manager hands out a download link that expires within a minute and
// points straight at S3, which sends the file as an attachment. The link opens in a new
// tab so that a failure at S3 never replaces the app.
export async function downloadBackup(
  backupAPI: { action: (action: string) => Promise<unknown> },
  backupId: number | string
) {
  const { url } = (await backupAPI.action(`${backupId}/download`)) as { url: string }
  const link = document.createElement('a')
  link.href = url
  link.target = '_blank'
  link.rel = 'noopener'
  link.click()
}

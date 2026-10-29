export interface APIError extends Error {
  response: Response
  status: number
}

export type Query = Record<string, string | number | boolean | null | undefined>

const API_ROOT = '/api'

// How long every call waits before it is sent: 300 ms unless VITE_LOAD_DELAY says otherwise (0 in the image and the
// tests), so that development shows what a slow network does.
function getLoadDelay() {
  const rawDelay = import.meta.env.VITE_LOAD_DELAY
  const parsedDelay = rawDelay ? Number(rawDelay) : 300

  return Number.isFinite(parsedDelay) && parsedDelay >= 0 ? parsedDelay : 300
}

export const LOAD_DELAY = getLoadDelay()

/**
 * Call the manager's API at `path` (under /api): wait `LOAD_DELAY`, send `query` (leaving out empty values) and `body`
 * (as JSON), and return the JSON of the answer. A failed answer throws an `APIError` with the manager's `detail` as its
 * message, and the status and response.
 */
export async function request<T>(
  method: 'GET' | 'POST' | 'PUT' | 'DELETE',
  path: string,
  { query = {}, body }: { query?: Query; body?: unknown } = {}
): Promise<T> {
  await new Promise(resolve => setTimeout(resolve, LOAD_DELAY))
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== null && value !== undefined && value !== '') params.set(key, String(value))
  }
  const search = params.toString()
  const response = await fetch(
    `${API_ROOT}${path}${search ? `?${search}` : ''}`,
    body === undefined
      ? { method }
      : { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
  )
  const data = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = typeof data?.detail === 'string' ? data.detail : null
    const error = new Error(
      detail ?? `${method} ${API_ROOT}${path} failed (${response.status})`
    ) as APIError
    error.response = response
    error.status = response.status
    throw error
  }
  return data as T
}

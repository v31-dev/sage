import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { request, type APIError } from '@/lib/api'

let fetchMock: Mock

function reply(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function lastCall() {
  const [url, init] = fetchMock.mock.lastCall as [string, RequestInit]
  return { url, init }
}

async function failure(call: Promise<unknown>) {
  try {
    await call
  } catch (error) {
    return error as APIError
  }
  throw new Error('the call did not fail')
}

beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue(reply({}))
  vi.stubGlobal('fetch', fetchMock)
})

describe('the load delay', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllEnvs()
  })

  it.each([
    [undefined, 300],
    ['', 300],
    ['0', 0],
    ['50', 50],
    ['abc', 300],
    ['-5', 300],
    ['Infinity', 300],
  ])('reads VITE_LOAD_DELAY=%j as %j', async (value, delay) => {
    vi.resetModules()
    vi.stubEnv('VITE_LOAD_DELAY', value ?? '')

    const module = await import('@/lib/api')

    expect(module.LOAD_DELAY).toBe(delay)
  })

  it.each(['GET', 'POST', 'PUT', 'DELETE'] as const)(
    'holds a %s back until the delay has passed',
    async method => {
      vi.resetModules()
      vi.stubEnv('VITE_LOAD_DELAY', '500')
      const module = await import('@/lib/api')
      vi.useFakeTimers()

      const pending = module.request(method, '/things')
      await vi.advanceTimersByTimeAsync(499)
      expect(fetchMock).not.toHaveBeenCalled()

      await vi.advanceTimersByTimeAsync(1)
      expect(fetchMock).toHaveBeenCalledOnce()
      await pending
    }
  )
})

describe('what it sends', () => {
  it('calls the path under /api with the method', async () => {
    await request('DELETE', '/projects/demo')

    expect(lastCall().url).toBe('/api/projects/demo')
    expect(lastCall().init.method).toBe('DELETE')
  })

  it('sends the query, leaving out null, undefined and empty values but not false or 0', async () => {
    await request('GET', '/things', {
      query: { a: 'x y', b: null, c: undefined, d: '', e: false, f: 0 },
    })

    const [path, query] = lastCall().url.split('?')
    expect(path).toBe('/api/things')
    expect(Object.fromEntries(new URLSearchParams(query))).toEqual({
      a: 'x y',
      e: 'false',
      f: '0',
    })
  })

  it('adds no question mark when there is no query to send', async () => {
    await request('GET', '/things', { query: { a: null } })

    expect(lastCall().url).toBe('/api/things')
  })

  it('sends a body as JSON', async () => {
    await request('PUT', '/things/1', { body: { label: 'a', note: null } })

    expect(lastCall().init.headers).toEqual({ 'Content-Type': 'application/json' })
    expect(JSON.parse(lastCall().init.body as string)).toEqual({ label: 'a', note: null })
  })

  it('sends no body and no content type without one', async () => {
    await request('POST', '/things/1/go')

    expect(lastCall().init).toEqual({ method: 'POST' })
  })
})

describe('what it returns', () => {
  it('returns the JSON of the answer', async () => {
    fetchMock.mockResolvedValue(reply([{ id: 1 }]))

    await expect(request('GET', '/things')).resolves.toEqual([{ id: 1 }])
  })

  it('returns null for an answer without a body', async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }))

    await expect(request('DELETE', '/things/1')).resolves.toBeNull()
  })

  it("throws the manager's detail, with the status and the response", async () => {
    const answer = reply({ detail: 'Application is already deploying.' }, 409)
    fetchMock.mockResolvedValue(answer)

    const error = await failure(request('POST', '/things/1/go'))

    expect(error.message).toBe('Application is already deploying.')
    expect(error.status).toBe(409)
    expect(error.response).toBe(answer)
  })

  it.each([
    ['no detail', reply({}, 500)],
    ['a detail that is not text', reply({ detail: [{ msg: 'field required' }] }, 422)],
    ['an answer that is not JSON', new Response('Internal Server Error', { status: 500 })],
  ])('says which call failed for %s', async (_case, answer) => {
    fetchMock.mockResolvedValue(answer)

    const error = await failure(request('GET', '/things'))

    expect(error.message).toBe(`GET /api/things failed (${answer.status})`)
    expect(error.status).toBe(answer.status)
  })

  it('passes a network failure on, and logs nothing itself', async () => {
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))

    await expect(request('GET', '/things')).rejects.toThrow('Failed to fetch')
    fetchMock.mockResolvedValue(reply({}, 500))
    await expect(request('GET', '/things')).rejects.toThrow()

    expect(log).not.toHaveBeenCalled()
  })
})

import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { CRUDAPI, type APIError } from '@/lib/api'

interface Widget {
  id: string
  label: string
}

let fetchMock: Mock

function reply(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function lastCall() {
  const [url, init] = fetchMock.mock.lastCall as [string, RequestInit | undefined]
  return { url, init }
}

function widgets(options: Partial<ConstructorParameters<typeof CRUDAPI<Widget>>[0]> = {}) {
  return new CRUDAPI<Widget>({ name: 'Widget', path: '/api/widgets', load_delay: 0, ...options })
}

beforeEach(() => {
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
  vi.spyOn(console, 'error').mockImplementation(() => {})
})

describe('CRUDAPI construction', () => {
  it('fills the path parameters in', () => {
    const api = new CRUDAPI({
      name: 'Volume',
      path: '/api/projects/{project}/applications/{application}/volumes',
      params: { project: 'demo', application: 'web' },
    })

    expect(api.path).toBe('/api/projects/demo/applications/web/volumes')
  })

  it('leaves the path alone without parameters and waits half a second by default', () => {
    const api = new CRUDAPI({ name: 'Widget', path: '/api/widgets/{x}' })

    expect(api.path).toBe('/api/widgets/{x}')
    expect(api.load_delay).toBe(500)
  })
})

describe('the delay before every call', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it.each([
    ['fetchAll', (api: CRUDAPI<Widget>) => api.fetchAll()],
    ['fetchOne', (api: CRUDAPI<Widget>) => api.fetchOne('1')],
    ['create', (api: CRUDAPI<Widget>) => api.create({ label: 'a' })],
    ['update', (api: CRUDAPI<Widget>) => api.update('1', { label: 'a' })],
    ['delete', (api: CRUDAPI<Widget>) => api.delete('1')],
    ['action', (api: CRUDAPI<Widget>) => api.action('go')],
  ])('%s sends nothing until load_delay has passed', async (_name, call) => {
    vi.useFakeTimers()
    fetchMock.mockResolvedValue(reply({}))

    const pending = call(widgets({ load_delay: 500 }))
    await vi.advanceTimersByTimeAsync(499)
    expect(fetchMock).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(1)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    await pending
  })
})

describe('fetchAll', () => {
  it('gets the list from the path with a trailing slash', async () => {
    fetchMock.mockResolvedValue(reply([{ id: '1', label: 'a' }]))

    expect(await widgets().fetchAll()).toEqual([{ id: '1', label: 'a' }])
    expect(lastCall().url).toBe('/api/widgets/')
    expect(lastCall().init).toBeUndefined()
  })

  it('throws the error with the status and response when the call fails', async () => {
    fetchMock.mockResolvedValue(reply({ detail: 'Backend down' }, 503))

    const error: APIError = await widgets()
      .fetchAll()
      .catch(e => e)

    expect(error.message).toBe('Backend down')
    expect(error.status).toBe(503)
    expect(error.response).toBeInstanceOf(Response)
  })

  it('says what it could not fetch when the answer has no detail or is not JSON', async () => {
    fetchMock.mockResolvedValueOnce(reply({}, 500))
    await expect(widgets().fetchAll()).rejects.toThrow('Failed to fetch Widget')

    fetchMock.mockResolvedValueOnce(new Response('<html>bad gateway</html>', { status: 502 }))
    await expect(widgets().fetchAll()).rejects.toThrow('Failed to fetch Widget')
  })

  it('logs the error and passes a network failure on', async () => {
    const failure = new TypeError('Failed to fetch')
    fetchMock.mockRejectedValue(failure)

    await expect(widgets().fetchAll()).rejects.toBe(failure)
    expect(console.error).toHaveBeenCalledWith('Error fetching Widget:', failure)
  })
})

describe('fetchOne', () => {
  it('gets one item by id', async () => {
    fetchMock.mockResolvedValue(reply({ id: '7', label: 'seven' }))

    expect(await widgets().fetchOne('7')).toEqual({ id: '7', label: 'seven' })
    expect(lastCall().url).toBe('/api/widgets/7')
  })

  it('throws an error with the status, and names the id when the answer has no detail', async () => {
    fetchMock.mockResolvedValueOnce(reply({ detail: 'Widget not found' }, 404))
    const missing: APIError = await widgets()
      .fetchOne('9')
      .catch(e => e)
    expect([missing.message, missing.status]).toEqual(['Widget not found', 404])

    fetchMock.mockResolvedValueOnce(reply({}, 500))
    await expect(widgets().fetchOne('9')).rejects.toThrow('Failed to fetch Widget with id 9')
  })
})

describe('create and update', () => {
  it('create posts the data as JSON to the path with a trailing slash', async () => {
    fetchMock.mockResolvedValue(reply({ id: '1', label: 'new' }))

    expect(await widgets().create({ label: 'new' })).toEqual({ id: '1', label: 'new' })
    expect(lastCall().url).toBe('/api/widgets/')
    expect(lastCall().init).toEqual({
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{"label":"new"}',
    })
  })

  it('update puts the data as JSON to the item', async () => {
    fetchMock.mockResolvedValue(reply({ id: '3', label: 'changed' }))

    await widgets().update('3', { label: 'changed' })

    expect(lastCall().url).toBe('/api/widgets/3')
    expect(lastCall().init).toEqual({
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: '{"label":"changed"}',
    })
  })

  it.each([
    ['create', (api: CRUDAPI<Widget>) => api.create({}), 'Failed to create Widget'],
    ['update', (api: CRUDAPI<Widget>) => api.update('1', {}), 'Failed to update Widget'],
  ])('%s throws the detail of the answer, or says what failed', async (_name, call, fallback) => {
    fetchMock.mockResolvedValueOnce(reply({ detail: 'Invalid name' }, 400))
    await expect(call(widgets())).rejects.toThrow('Invalid name')

    fetchMock.mockResolvedValueOnce(reply({}, 500))
    await expect(call(widgets())).rejects.toThrow(fallback)
  })

  it('throws a plain error here, without the status that fetchAll and action keep', async () => {
    fetchMock.mockResolvedValue(reply({ detail: 'Conflict' }, 409))

    const error: APIError = await widgets()
      .create({})
      .catch(e => e)

    expect(error.status).toBeUndefined()
  })
})

describe('delete', () => {
  it('deletes the item', async () => {
    fetchMock.mockResolvedValue(reply({ status: 'deleted', id: '5' }))

    expect(await widgets().delete('5')).toEqual({ status: 'deleted', id: '5' })
    expect(lastCall().url).toBe('/api/widgets/5')
    expect(lastCall().init).toEqual({ method: 'DELETE' })
  })

  it('sends the query parameters, skipping null and undefined but not false or 0', async () => {
    fetchMock.mockResolvedValue(reply({}))

    await widgets().delete('5', { force: false, count: 0, name: 'x y', a: null, b: undefined })

    const { url } = lastCall()
    const [path, query] = url.split('?')
    expect(path).toBe('/api/widgets/5')
    expect(Object.fromEntries(new URLSearchParams(query))).toEqual({
      force: 'false',
      count: '0',
      name: 'x y',
    })
  })

  it('adds no question mark when there is nothing to send', async () => {
    fetchMock.mockResolvedValue(reply({}))

    await widgets().delete('5', { a: null })

    expect(lastCall().url).toBe('/api/widgets/5')
  })

  it('throws the detail of the answer, or says what failed', async () => {
    fetchMock.mockResolvedValueOnce(reply({ detail: 'Application has containers' }, 409))
    await expect(widgets().delete('1')).rejects.toThrow('Application has containers')

    fetchMock.mockResolvedValueOnce(reply({}, 500))
    await expect(widgets().delete('1')).rejects.toThrow('Failed to delete Widget')
  })
})

describe('action', () => {
  it('posts to the path with the action name and an empty body by default', async () => {
    fetchMock.mockResolvedValue(reply({ status: 'OK' }))

    await widgets().action('deploy')

    expect(lastCall().url).toBe('/api/widgets/deploy')
    expect(lastCall().init).toEqual({
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    })
  })

  it('sends the data it is given', async () => {
    fetchMock.mockResolvedValue(reply({}))

    await widgets().action('restore', { label: 'x' })

    expect(lastCall().init!.body).toBe('{"label":"x"}')
  })

  it('returns the answer with the response attached to it', async () => {
    fetchMock.mockResolvedValue(reply({ url: 'https://s3.example.test/x' }, 200))

    const result = (await widgets().action('1/download')) as unknown as {
      url: string
      response: Response
    }

    expect(result.url).toBe('https://s3.example.test/x')
    expect(result.response).toBeInstanceOf(Response)
    expect(result.response.status).toBe(200)
  })

  it('throws an error with the status when the call fails', async () => {
    fetchMock.mockResolvedValue(reply({ detail: 'S3 is not configured.' }, 400))

    const error: APIError = await widgets()
      .action('1/download')
      .catch(e => e)

    expect(error.message).toBe('S3 is not configured.')
    expect(error.status).toBe(400)
  })

  it('names the object when the answer has no detail', async () => {
    fetchMock.mockResolvedValue(reply({}, 500))
    await expect(widgets().action('go')).rejects.toThrow('Failed to create Widget')
  })
})

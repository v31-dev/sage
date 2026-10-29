import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { connection, UpdatesClient, type SocketLike } from '@/lib/updates'

class FakeSocket implements SocketLike {
  connected = false
  sent: Array<[string, ...unknown[]]> = []
  connectCalls = 0
  disconnectCalls = 0
  private listeners: Record<string, Array<(...args: any[]) => void>> = {}
  private managerListeners: Record<string, Array<(...args: any[]) => void>> = {}
  private engineListeners: Record<string, Array<(...args: any[]) => void>> = {}
  private acknowledgements: Array<{ topic: unknown; acknowledge: () => void }> = []
  io = {
    on: (event: string, listener: (...args: any[]) => void) => {
      ;(this.managerListeners[event] ??= []).push(listener)
    },
    engine: {
      transport: { name: 'polling' },
      on: (event: string, listener: (...args: any[]) => void) => {
        ;(this.engineListeners[event] ??= []).push(listener)
      },
    },
  }

  on(event: string, listener: (...args: any[]) => void) {
    ;(this.listeners[event] ??= []).push(listener)
  }
  emit(event: string, ...args: unknown[]) {
    const acknowledge = args.find(arg => typeof arg === 'function') as (() => void) | undefined
    this.sent.push([event, ...args.filter(arg => typeof arg !== 'function')])
    if (acknowledge) this.acknowledgements.push({ topic: args[0], acknowledge })
  }
  connect() {
    this.connectCalls += 1
  }
  disconnect() {
    this.disconnectCalls += 1
    this.connected = false
  }

  // The manager's side.
  fire(event: string, ...args: unknown[]) {
    this.listeners[event]?.forEach(listener => listener(...args))
  }
  serverConnects(transport = 'polling') {
    this.connected = true
    this.io.engine.transport.name = transport
    this.fire('connect')
  }
  /** The manager has the subscriptions it was asked for. */
  acknowledgesSubscriptions() {
    const waiting = this.acknowledgements
    this.acknowledgements = []
    waiting.forEach(({ acknowledge }) => acknowledge())
  }
  upgradesTo(name: string) {
    this.io.engine.transport.name = name
    this.engineListeners['upgrade']?.forEach(listener => listener({ name }))
  }
  pings() {
    this.managerListeners['ping']?.forEach(listener => listener())
  }
  attemptsToReconnect() {
    this.managerListeners['reconnect_attempt']?.forEach(listener => listener(1))
  }
  state(topic: string, data: unknown) {
    this.fire('state', { topic, data })
  }
}

const last = <T>(items: T[]) => items[items.length - 1]!

/** What the socket was asked, without the functions that answer. */
const asked = (socket: FakeSocket) => socket.sent.map(([event, topic]) => [event, topic])

function setup() {
  const sockets: FakeSocket[] = []
  const client = new UpdatesClient(() => {
    const socket = new FakeSocket()
    sockets.push(socket)
    return socket
  })
  return { client, sockets }
}

/** A load that finishes when the test says so. */
function slowLoad<T>() {
  const pending: Array<{ resolve: (data: T) => void; reject: (error: unknown) => void }> = []
  const load = vi.fn(
    () =>
      new Promise<T>((resolve, reject) => {
        pending.push({ resolve, reject })
      })
  )
  return {
    load,
    finish: (data: T, call = pending.length - 1) => pending[call]!.resolve(data),
    pending,
  }
}

function followingState(
  client: UpdatesClient,
  topic: string,
  loadIt: () => Promise<any> = async () => 'loaded'
) {
  const load = vi.fn(loadIt)
  const onData = vi.fn()
  return { ...client.watchState(topic, load, onData), onData, load }
}

/** A state topic of one resource, which the manager can say is gone. */
function followingResource(
  client: UpdatesClient,
  topic: string,
  loadIt: () => Promise<any> = async () => 'loaded'
) {
  const load = vi.fn(loadIt)
  const onData = vi.fn()
  const onNotFound = vi.fn()
  return { ...client.watchState(topic, load, onData, onNotFound), onData, onNotFound, load }
}

function followingFeed(
  client: UpdatesClient,
  topic: string,
  loadIt: () => Promise<any[]> = async () => ['loaded']
) {
  const load = vi.fn(loadIt)
  const onData = vi.fn()
  return { ...client.watchFeed(topic, load, onData), onData, load }
}

const settle = () => vi.advanceTimersByTimeAsync(0)

beforeEach(() => {
  vi.useFakeTimers()
  Object.assign(connection, { state: 'connecting', transport: null, heartbeatAt: null })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('connecting', () => {
  it('opens the connection when the first topic is followed, once for any number of topics', () => {
    const { client, sockets } = setup()
    followingState(client, 'info')
    followingFeed(client, 'notifications')
    expect(sockets).toHaveLength(1)
    expect(sockets[0]!.connectCalls).toBe(1)
    expect(connection.state).toBe('connecting')
  })

  it('subscribes for every watcher when it connects', () => {
    const { client, sockets } = setup()
    followingState(client, 'info')
    followingFeed(client, 'notifications')
    sockets[0]!.serverConnects()
    expect(asked(sockets[0]!)).toEqual([
      ['subscribe', 'info'],
      ['subscribe', 'notifications'],
    ])
  })

  it('subscribes at once to a topic followed after it connected', () => {
    const { client, sockets } = setup()
    followingState(client, 'info')
    sockets[0]!.serverConnects()
    followingFeed(client, 'notifications')
    expect(last(asked(sockets[0]!))).toEqual(['subscribe', 'notifications'])
  })

  it('subscribes again when it reconnects', () => {
    const { client, sockets } = setup()
    followingState(client, 'info')
    followingFeed(client, 'notifications')
    sockets[0]!.serverConnects()
    sockets[0]!.fire('disconnect')
    sockets[0]!.sent.length = 0
    sockets[0]!.serverConnects()
    expect(asked(sockets[0]!)).toEqual([
      ['subscribe', 'info'],
      ['subscribe', 'notifications'],
    ])
  })

  it('does not connect without a watcher', () => {
    const { sockets } = setup()
    expect(sockets).toHaveLength(0)
  })
})

describe('loading', () => {
  it('loads at once, before the socket connects, and gives the page what it loaded', async () => {
    const { client } = setup()
    const info = followingState(client, 'info', async () => ({ state: 'running' }))

    await expect(info.first).resolves.toBeUndefined()

    expect(info.load).toHaveBeenCalledOnce()
    expect(info.onData).toHaveBeenCalledWith({ state: 'running' })
  })

  it('rejects first, and gives the page nothing, when the first load fails', async () => {
    const { client } = setup()
    const info = followingState(client, 'info', async () => {
      throw new Error('Failed to fetch app info')
    })

    await expect(info.first).rejects.toThrow('Failed to fetch app info')

    expect(info.onData).not.toHaveBeenCalled()
  })

  it('loads again once the manager has the subscription, and gives the page that too', async () => {
    const { client, sockets } = setup()
    const loads = ['first', 'second']
    const info = followingState(client, 'info', async () => loads.shift())
    await settle()
    sockets[0]!.serverConnects()
    expect(info.load).toHaveBeenCalledOnce()

    sockets[0]!.acknowledgesSubscriptions()
    await settle()

    expect(info.load).toHaveBeenCalledTimes(2)
    expect(info.onData.mock.calls).toEqual([['first'], ['second']])
  })

  it('loads again after every reconnect, once the manager has the subscription', async () => {
    const { client, sockets } = setup()
    const info = followingState(client, 'info')
    sockets[0]!.serverConnects()
    sockets[0]!.acknowledgesSubscriptions()
    sockets[0]!.fire('disconnect')
    sockets[0]!.serverConnects()
    await settle()
    expect(info.load).toHaveBeenCalledTimes(2)

    sockets[0]!.acknowledgesSubscriptions()
    await settle()

    expect(info.load).toHaveBeenCalledTimes(3)
  })

  it('logs a load that fails after the first and keeps following', async () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { client, sockets } = setup()
    const calls: Array<() => Promise<string>> = [
      async () => 'first',
      async () => {
        throw new Error('down')
      },
    ]
    const info = followingState(client, 'info', () => calls.shift()!())
    await settle()
    sockets[0]!.serverConnects()
    sockets[0]!.acknowledgesSubscriptions()
    await settle()

    expect(error).toHaveBeenCalledWith("Failed to load 'info' again:", expect.any(Error))
    sockets[0]!.state('info', 'pushed')
    expect(last(info.onData.mock.calls)).toEqual(['pushed'])
  })

  it('drops a state that was loaded before a newer one was pushed', async () => {
    const { client, sockets } = setup()
    const { load, finish } = slowLoad<string>()
    const info = followingState(client, 'info', load)
    sockets[0]!.serverConnects()

    sockets[0]!.state('info', 'pushed')
    finish('older')
    await settle()

    expect(info.onData.mock.calls).toEqual([['pushed']])
  })

  it('gives a state that was loaded after the last push', async () => {
    const { client, sockets } = setup()
    const { load, finish } = slowLoad<string>()
    const info = followingState(client, 'info', load)
    sockets[0]!.serverConnects()
    sockets[0]!.state('info', 'pushed')
    sockets[0]!.acknowledgesSubscriptions()

    finish('newer', 1)
    await settle()

    expect(info.onData.mock.calls).toEqual([['pushed'], ['newer']])
  })

  it('gives a feed the rows it loaded even though some were pushed meanwhile', async () => {
    const { client, sockets } = setup()
    const { load, finish } = slowLoad<string[]>()
    const notifications = followingFeed(client, 'notifications', load)
    sockets[0]!.serverConnects()

    sockets[0]!.state('notifications', ['pushed'])
    finish(['missed', 'pushed'])
    await settle()

    expect(notifications.onData.mock.calls).toEqual([[['pushed']], [['missed', 'pushed']]])
  })

  it('does not give a stopped watcher a load that finishes later', async () => {
    const { client } = setup()
    const { load, finish } = slowLoad<string>()
    const info = followingState(client, 'info', load)

    info.stop()
    finish('late')
    await settle()

    expect(info.onData).not.toHaveBeenCalled()
  })
})

describe('what the manager sends', () => {
  it('gives a topic its state to the watchers of that topic only', () => {
    const { client, sockets } = setup()
    const info = followingState(client, 'info')
    const notifications = followingFeed(client, 'notifications')
    sockets[0]!.serverConnects()
    sockets[0]!.state('info', { state: 'running' })

    expect(info.onData).toHaveBeenCalledWith({ state: 'running' })
    expect(notifications.onData).not.toHaveBeenCalled()
  })

  it('gives every new state to the watcher', () => {
    const { client, sockets } = setup()
    const info = followingState(client, 'info')
    sockets[0]!.serverConnects()
    sockets[0]!.state('info', 1)
    sockets[0]!.state('info', 2)
    expect(info.onData.mock.calls).toEqual([[1], [2]])
  })

  it('gives a state to every watcher of the topic', () => {
    const { client, sockets } = setup()
    const first = followingState(client, 'info')
    const second = followingState(client, 'info')
    sockets[0]!.serverConnects()
    sockets[0]!.state('info', 1)
    expect(first.onData).toHaveBeenCalledWith(1)
    expect(second.onData).toHaveBeenCalledWith(1)
  })

  it('tells the watchers of a resource when the manager says it is gone, and no one else', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const { client, sockets } = setup()
    const one = followingResource(client, 'worker:w1')
    const other = followingResource(client, 'worker:w2')
    sockets[0]!.serverConnects()

    sockets[0]!.fire('topic_error', { topic: 'worker:w1', status: 404 })

    expect(one.onNotFound).toHaveBeenCalledOnce()
    expect(other.onNotFound).not.toHaveBeenCalled()
    expect(warn).not.toHaveBeenCalled()
  })

  it('says so when the manager refuses a resource for any other reason', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const { client, sockets } = setup()
    const one = followingResource(client, 'worker:w1')
    sockets[0]!.serverConnects()

    sockets[0]!.fire('topic_error', { topic: 'worker:w1', status: 400 })

    expect(one.onNotFound).not.toHaveBeenCalled()
    expect(warn).toHaveBeenCalledWith("The manager refused the topic 'worker:w1' (400).")
  })

  it('drops a state that was loaded before the manager said the resource is gone', async () => {
    const { client, sockets } = setup()
    const { load, finish } = slowLoad<string>()
    const one = followingResource(client, 'worker:w1', load)
    sockets[0]!.serverConnects()

    sockets[0]!.fire('topic_error', { topic: 'worker:w1', status: 404 })
    finish('older')
    await settle()

    expect(one.onData).not.toHaveBeenCalled()
    expect(one.onNotFound).toHaveBeenCalledOnce()
  })

  it('tells the watcher when the load after it subscribed finds the resource gone, without an error', async () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { client, sockets } = setup()
    const calls: Array<() => Promise<string>> = [
      async () => 'first',
      async () => {
        throw Object.assign(new Error('Worker not found'), { status: 404 })
      },
    ]
    const one = followingResource(client, 'worker:w1', () => calls.shift()!())
    await settle()
    sockets[0]!.serverConnects()
    sockets[0]!.acknowledgesSubscriptions()
    await settle()

    expect(one.onNotFound).toHaveBeenCalledOnce()
    expect(error).not.toHaveBeenCalled()
  })

  it('does not tell a watcher that has stopped', async () => {
    const { client, sockets } = setup()
    const { load, pending } = slowLoad<string>()
    const one = followingResource(client, 'worker:w1', load)
    sockets[0]!.serverConnects()
    sockets[0]!.acknowledgesSubscriptions()
    one.stop()

    pending[1]!.reject(Object.assign(new Error('Worker not found'), { status: 404 }))
    sockets[0]!.fire('topic_error', { topic: 'worker:w1', status: 404 })
    await settle()

    expect(one.onNotFound).not.toHaveBeenCalled()
  })

  it('says so when the manager refuses a topic', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const { client, sockets } = setup()
    followingState(client, 'info')
    sockets[0]!.serverConnects()
    sockets[0]!.fire('topic_error', { topic: 'info', status: 404 })
    expect(warn).toHaveBeenCalledWith("The manager refused the topic 'info' (404).")
  })
})

describe('the state of the connection', () => {
  it('is connected, over the transport in use, once it connects, and follows an upgrade', () => {
    const { client, sockets } = setup()
    followingState(client, 'info')
    sockets[0]!.serverConnects('polling')
    expect(connection).toMatchObject({ state: 'connected', transport: 'polling' })

    sockets[0]!.upgradesTo('websocket')
    expect(connection.transport).toBe('websocket')
  })

  it('is disconnected when it drops, and connecting while it tries again', () => {
    const { client, sockets } = setup()
    followingState(client, 'info')
    sockets[0]!.serverConnects('websocket')
    sockets[0]!.fire('disconnect')
    expect(connection).toMatchObject({ state: 'disconnected', transport: null })

    sockets[0]!.attemptsToReconnect()
    expect(connection.state).toBe('connecting')
  })

  it('is disconnected when it cannot connect, and the page still has what it loaded', async () => {
    const { client, sockets } = setup()
    const info = followingState(client, 'info')
    sockets[0]!.fire('connect_error', new Error('xhr poll error'))

    await expect(info.first).resolves.toBeUndefined()

    expect(connection.state).toBe('disconnected')
    expect(info.onData).toHaveBeenCalledWith('loaded')
  })

  it('notes the time of each heartbeat from the manager', () => {
    vi.setSystemTime(new Date('2026-10-28T10:00:00Z'))
    const { client, sockets } = setup()
    followingState(client, 'info')
    sockets[0]!.serverConnects()
    expect(connection.heartbeatAt).toBeNull()

    sockets[0]!.pings()
    expect(connection.heartbeatAt).toBe(new Date('2026-10-28T10:00:00Z').getTime())
    vi.setSystemTime(new Date('2026-10-28T10:00:10Z'))
    sockets[0]!.pings()
    expect(connection.heartbeatAt).toBe(new Date('2026-10-28T10:00:10Z').getTime())
  })
})

describe('stopping', () => {
  it('unsubscribes when the last watcher of a topic stops, and keeps the connection for the others', () => {
    const { client, sockets } = setup()
    const info = followingState(client, 'info')
    followingFeed(client, 'notifications')
    sockets[0]!.serverConnects()
    info.stop()
    expect(last(asked(sockets[0]!))).toEqual(['unsubscribe', 'info'])
    expect(sockets[0]!.disconnectCalls).toBe(0)
  })

  it('does not unsubscribe while another watcher follows the topic', () => {
    const { client, sockets } = setup()
    const first = followingState(client, 'info')
    followingState(client, 'info')
    sockets[0]!.serverConnects()
    const sent = sockets[0]!.sent.length
    first.stop()
    expect(sockets[0]!.sent).toHaveLength(sent)
  })

  it('stops giving a stopped watcher its states', () => {
    const { client, sockets } = setup()
    const info = followingState(client, 'info')
    followingFeed(client, 'notifications')
    sockets[0]!.serverConnects()
    info.stop()
    info.onData.mockClear()
    sockets[0]!.state('info', 1)
    expect(info.onData).not.toHaveBeenCalled()
  })

  it('closes the connection when the last watcher stops, and opens it again for the next', () => {
    const { client, sockets } = setup()
    const info = followingState(client, 'info')
    sockets[0]!.serverConnects()
    info.stop()
    expect(sockets[0]!.disconnectCalls).toBe(1)
    expect(connection.state).toBe('disconnected')

    followingState(client, 'info')
    expect(sockets).toHaveLength(1)
    expect(sockets[0]!.connectCalls).toBe(2)
    expect(connection.state).toBe('connecting')
  })
})

describe('the connection the app uses', () => {
  it('is made to the manager at its own path, and not opened until a topic is followed', async () => {
    vi.resetModules()
    const fake = new FakeSocket()
    const make = vi.fn(() => fake)
    vi.doMock('socket.io-client', () => ({ io: make }))
    const { watchState } = await import('@/lib/updates')
    expect(make).not.toHaveBeenCalled()

    watchState(
      'info',
      async () => 'loaded',
      () => {}
    )
    expect(make).toHaveBeenCalledWith({ path: '/api/socket.io', autoConnect: false })
    expect(fake.connectCalls).toBe(1)
    vi.doUnmock('socket.io-client')
  })
})

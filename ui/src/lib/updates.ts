// The one Socket.IO connection to the manager, and the topics followed over it. A page loads a topic from the REST route
// for it, and the manager then pushes each change: the whole state of a state topic, or the new rows of a feed. Each time
// the socket connects, the page loads again once the manager has the subscription, which makes up for what was missed
// before it. Reconnecting, and falling back from WebSocket to long-polling, are Socket.IO's.
import { reactive } from 'vue'
import { io } from 'socket.io-client'

export interface SocketLike {
  connected: boolean
  on(event: string, listener: (...args: any[]) => void): unknown
  emit(event: string, ...args: unknown[]): unknown
  connect(): unknown
  disconnect(): unknown
  io: {
    on(event: string, listener: (...args: any[]) => void): unknown
    engine?: {
      transport: { name: string }
      on(event: string, listener: (transport: { name: string }) => void): unknown
    }
  }
}

/** What the connection is doing, for the indicator: `heartbeatAt` is when the manager last pinged. */
export const connection = reactive<{
  state: 'connecting' | 'connected' | 'disconnected'
  transport: string | null
  heartbeatAt: number | null
}>({ state: 'connecting', transport: null, heartbeatAt: null })

interface Watcher {
  topic: string
  load: () => Promise<any>
  onData: (data: any) => void
  onNotFound?: () => void
  // A state that was loaded before one was pushed is older than it, and is dropped.
  dropsOlderLoads: boolean
  pushes: number
}

export class UpdatesClient {
  private createSocket: () => SocketLike
  private socket: SocketLike | null = null
  private open = false
  private watchers = new Set<Watcher>()

  constructor(createSocket: () => SocketLike) {
    this.createSocket = createSocket
  }

  /**
   * Follow a state topic: `load` fetches its state, `onState` receives it, and then each state the manager pushes.
   * `onNotFound` is called when the manager answers 404 instead, pushed or to a load after the first: the resource is
   * gone. `first` settles with the first load, or rejects if it fails.
   */
  watchState<T>(
    topic: string,
    load: () => Promise<T>,
    onState: (state: T) => void,
    onNotFound?: () => void
  ) {
    return this.watch(topic, load, onState, true, onNotFound)
  }

  /**
   * Follow a feed: `load` fetches the rows the page does not have yet, and `onRows` receives them, and then the rows
   * the manager pushes. `first` settles with the first load, or rejects if it fails.
   */
  watchFeed<T>(topic: string, load: () => Promise<T[]>, onRows: (rows: T[]) => void) {
    return this.watch(topic, load, onRows, false)
  }

  private watch(
    topic: string,
    load: () => Promise<any>,
    onData: (data: any) => void,
    dropsOlderLoads: boolean,
    onNotFound?: () => void
  ) {
    const watcher: Watcher = { topic, load, onData, onNotFound, dropsOlderLoads, pushes: 0 }
    this.watchers.add(watcher)
    const socket = this.connect()
    if (socket.connected) this.subscribe(socket, watcher)
    const first = this.refresh(watcher)
    first.catch(() => {})
    return { stop: () => this.unwatch(watcher), first }
  }

  private connect(): SocketLike {
    if (!this.socket) {
      const socket = this.createSocket()
      this.socket = socket
      socket.on('connect', () => this.onConnect(socket))
      socket.on('disconnect', () => this.setState('disconnected'))
      socket.on('connect_error', () => this.setState('disconnected'))
      socket.on('state', (message: { topic: string; data: unknown }) => this.onState(message))
      socket.on('topic_error', (message: { topic: string; status: number }) =>
        this.onTopicError(message)
      )
      socket.io.on('ping', () => (connection.heartbeatAt = Date.now()))
      socket.io.on('reconnect_attempt', () => this.setState('connecting'))
    }
    if (!this.open) {
      this.open = true
      this.setState('connecting')
      this.socket.connect()
    }
    return this.socket
  }

  private onConnect(socket: SocketLike) {
    const engine = socket.io.engine
    connection.state = 'connected'
    connection.transport = engine?.transport.name ?? null
    engine?.on('upgrade', transport => (connection.transport = transport.name))
    for (const watcher of this.watchers) this.subscribe(socket, watcher)
  }

  // The manager answers once it has the subscription; what changed before that is in what the load returns.
  private subscribe(socket: SocketLike, watcher: Watcher) {
    socket.emit('subscribe', watcher.topic, () =>
      this.refresh(watcher).catch(error => {
        if (!this.watchers.has(watcher)) return
        if ((error as { status?: number } | null)?.status === 404 && watcher.onNotFound) {
          watcher.onNotFound()
        } else {
          console.error(`Failed to load '${watcher.topic}' again:`, error)
        }
      })
    )
  }

  private async refresh(watcher: Watcher) {
    const pushes = watcher.pushes
    const data = await watcher.load()
    if (!this.watchers.has(watcher)) return
    if (watcher.dropsOlderLoads && watcher.pushes !== pushes) return
    watcher.onData(data)
  }

  private onState(message: { topic: string; data: unknown }) {
    for (const watcher of this.watchers) {
      if (watcher.topic !== message.topic) continue
      watcher.pushes++
      watcher.onData(message.data)
    }
  }

  private onTopicError(message: { topic: string; status: number }) {
    for (const watcher of this.watchers) {
      if (watcher.topic !== message.topic) continue
      watcher.pushes++
      if (message.status === 404 && watcher.onNotFound) watcher.onNotFound()
      else console.warn(`The manager refused the topic '${message.topic}' (${message.status}).`)
    }
  }

  private unwatch(watcher: Watcher) {
    this.watchers.delete(watcher)
    if (this.watchers.size === 0) {
      this.open = false
      this.socket?.disconnect()
      this.setState('disconnected')
    } else if (
      this.socket?.connected &&
      ![...this.watchers].some(other => other.topic === watcher.topic)
    ) {
      this.socket.emit('unsubscribe', watcher.topic)
    }
  }

  private setState(state: 'connecting' | 'connected' | 'disconnected') {
    connection.state = state
    if (state !== 'connected') connection.transport = null
  }
}

const updates = new UpdatesClient(
  () => io({ path: '/api/socket.io', autoConnect: false }) as unknown as SocketLike
)

export const watchState = <T>(
  topic: string,
  load: () => Promise<T>,
  onState: (state: T) => void,
  onNotFound?: () => void
) => updates.watchState(topic, load, onState, onNotFound)

export const watchFeed = <T>(
  topic: string,
  load: () => Promise<T[]>,
  onRows: (rows: T[]) => void
) => updates.watchFeed(topic, load, onRows)

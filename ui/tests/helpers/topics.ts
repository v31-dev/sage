import { vi } from 'vitest'
import { watchFeed, watchState } from '@/lib/updates'

/** One topic a page followed: what it asked for, the manager's side of it, and whether it stopped. */
export interface Followed {
  topic: string
  load: () => Promise<any>
  /** The manager pushes this to the page. */
  push: (data: any) => void
  stop: ReturnType<typeof vi.fn>
  /** Settle the page's first load. */
  loaded: () => void
  failed: (error: unknown) => void
  /** The manager says the topic's resource is gone. */
  gone: () => void
}

/**
 * Stands in for `watchState` and `watchFeed` (mocked with `vi.mock('@/lib/updates', ...)`): every topic a page
 * follows is recorded. The first load is done at once unless `pending` is set.
 */
export function followTopics({ pending = false } = {}) {
  const followed: Followed[] = []
  const follow = (
    topic: string,
    load: () => Promise<any>,
    push: (data: any) => void,
    onNotFound?: () => void
  ) => {
    let loaded!: () => void
    let failed!: (error: unknown) => void
    const first = new Promise<void>((resolve, reject) => {
      loaded = resolve
      failed = reject
    })
    first.catch(() => {})
    if (!pending) loaded()
    const gone = () => {
      if (!onNotFound) throw new Error(`'${topic}' has no not-found handler`)
      onNotFound()
    }
    const entry: Followed = { topic, load, push, stop: vi.fn(), loaded, failed, gone }
    followed.push(entry)
    return { stop: entry.stop, first }
  }
  vi.mocked(watchState).mockImplementation(follow as unknown as typeof watchState)
  vi.mocked(watchFeed).mockImplementation(follow as unknown as typeof watchFeed)
  return {
    followed,
    topic(name: string) {
      const found = followed.filter(entry => entry.topic === name)
      if (found.length !== 1) throw new Error(`${found.length} watchers of '${name}'`)
      return found[0]!
    },
  }
}

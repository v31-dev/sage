import { onMounted, onUnmounted, ref, shallowRef, watch, type Ref } from 'vue'
import { watchFeed } from '@/lib/updates'

// What a list's route answers a load with: its rows, whether it had more than it sent, and how many match in all.
export interface FeedPage<Row> {
  rows: Row[]
  more?: boolean
  total?: number
}

export interface FeedListOptions<Row, Query> {
  topic: string
  // The order the rows are shown in, and whether it starts with the newest.
  order: (a: Row, b: Row) => number
  newestFirst: boolean
  key: (row: Row) => string | number
  // What the route returns for the filters: the newest that match, or, given the newest row held, what came after it.
  load: (query: Query, newest: Row | undefined) => Promise<FeedPage<Row>>
  // The filters; a change of them starts the list again from a fresh load.
  query?: () => Query
  // Whether a pushed row is one the route returns for the filters; without it, every pushed row is kept.
  matches?: (query: Query, row: Row) => boolean
  // The most rows held: the newest are kept. Without it, every row is.
  ceiling?: number
}

/**
 * A list that follows a feed: what the route returns for the filters at first, then each pushed row the filters let
 * through, merged by key in the list's order, each once. Each time the socket connects, what the route returns given
 * the newest row held is merged in too. A load asked for filters since changed is dropped. `more` says that rows that
 * match are not shown: the route had more than it sent, or the oldest went over the ceiling.
 */
export function useFeedList<Row, Query = void>(options: FeedListOptions<Row, Query>) {
  const rows = shallowRef<Row[]>([]) as Ref<Row[]>
  const more = ref(false)
  const total = ref<number | null>(null)
  const loading = ref(true)
  const error = ref('')
  let generation = 0
  // The keys of the rows pushed while a load is under way, which its total may not count.
  let pushedDuringLoad: Set<string | number> | null = null
  let stop: (() => void) | undefined

  const currentQuery = () => (options.query ? options.query() : (undefined as Query))
  const newest = () => (options.newestFirst ? rows.value[0] : rows.value[rows.value.length - 1])

  // Merges rows the list does not hold yet; returns those it added.
  function merge(incoming: Row[]) {
    const held = new Set(rows.value.map(options.key))
    const fresh = incoming.filter(row => !held.has(options.key(row)))
    if (!fresh.length) return fresh
    const merged = [...rows.value, ...fresh].sort(options.order)
    if (options.ceiling !== undefined && merged.length > options.ceiling) {
      more.value = true
      rows.value = options.newestFirst
        ? merged.slice(0, options.ceiling)
        : merged.slice(-options.ceiling)
    } else {
      rows.value = merged
    }
    return fresh
  }

  async function load(): Promise<Row[]> {
    const asked = generation
    const pushed = new Set<string | number>()
    pushedDuringLoad = pushed
    try {
      const page = await options.load(currentQuery(), newest())
      if (asked !== generation) return []
      merge(page.rows)
      more.value ||= page.more ?? false
      if (page.total !== undefined) {
        const loaded = new Set(page.rows.map(options.key))
        total.value = page.total + [...pushed].filter(key => !loaded.has(key)).length
      }
    } finally {
      if (pushedDuringLoad === pushed) pushedDuringLoad = null
    }
    return []
  }

  function onPushed(pushed: Row[]) {
    const query = currentQuery()
    const fresh = merge(pushed.filter(row => !options.matches || options.matches(query, row)))
    for (const row of fresh) pushedDuringLoad?.add(options.key(row))
    if (total.value !== null) total.value += fresh.length
  }

  // The end of a load the list waits for: its error, unless the filters changed meanwhile.
  async function settle(pending: Promise<unknown>, current: number) {
    try {
      await pending
      if (current === generation) error.value = ''
    } catch (err) {
      if (current === generation)
        error.value = err instanceof Error ? err.message : 'Failed to load'
    }
    if (current === generation) loading.value = false
  }

  async function restart() {
    const current = ++generation
    rows.value = []
    more.value = false
    total.value = null
    loading.value = true
    await settle(load(), current)
  }

  if (options.query) watch(options.query, restart)

  onMounted(async () => {
    const feed = watchFeed(options.topic, load, onPushed)
    stop = feed.stop
    await settle(feed.first, generation)
  })
  onUnmounted(() => stop?.())

  return { rows, more, total, loading, error }
}

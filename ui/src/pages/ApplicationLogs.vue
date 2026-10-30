<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Filter } from 'lucide-vue-next'
import { Card, CardContent } from '@/components/ui/card'
import { Spinner } from '@/components/ui/spinner'
import { TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import FieldHelp from '@/components/FieldHelp.vue'
import FilterBar, { type FilterDropdown, type FilterField } from '@/components/FilterBar.vue'
import { useFeedList } from '@/lib/feedList'
import { formatDate, localTimeToUTC, toLocalTime } from '@/lib/utils'
import { fetchApplicationLogs, matchesLogQuery, type LogEntry, type LogQuery } from '@/services/api'
import { useAppStore } from '@/stores/app'

// An application's logs, from all its containers or one worker's, followed as they are stored.
const route = useRoute()
const project = route.params.projectId as string
const application = route.params.appId as string
const appStore = useAppStore()

const CEILING = 1000
const FIVE_MINUTES = 5 * 60 * 1000

const viewport = ref<HTMLElement | null>(null)
// From and To are local times, as their fields hold them; From starts five minutes back.
const filters = ref({
  worker: '',
  search: '',
  from: toLocalTime(new Date(Date.now() - FIVE_MINUTES)),
  to: '',
})

const dropdowns = computed<FilterDropdown[]>(() => [
  {
    key: 'worker',
    label: 'Worker',
    any: 'Any worker',
    options: (appStore.workers ?? []).map(worker => worker.hostname),
  },
])

const fields: FilterField[] = [
  { key: 'search', label: 'Search', type: 'text', placeholder: 'Text anywhere in a line' },
  { key: 'from', label: 'From', type: 'datetime-local' },
  { key: 'to', label: 'To', type: 'datetime-local' },
]

const query = computed<LogQuery>(() => {
  const { worker, search, from, to } = filters.value
  return { hostname: worker, search, from_ts: localTimeToUTC(from), to_ts: localTimeToUTC(to) }
})

const {
  rows: logs,
  more,
  loading,
  error,
} = useFeedList<LogEntry, LogQuery>({
  topic: `application-logs:${project}-${application}`,
  order: (a, b) => a.id - b.id,
  newestFirst: false,
  key: entry => entry.id,
  query: () => query.value,
  matches: matchesLogQuery,
  ceiling: CEILING,
  load: async (asked, newest) => {
    const page = await fetchApplicationLogs(project, application, {
      ...asked,
      after_id: newest?.id ?? 0,
      limit: CEILING,
    })
    return { rows: page.lines, more: page.more }
  },
})

function filterByWorker(hostname: string) {
  filters.value = { ...filters.value, worker: hostname }
}

function atBottom() {
  const view = viewport.value
  return !view || view.scrollHeight - (view.scrollTop + view.clientHeight) <= 50
}

// The view follows new lines while it is at the bottom (measured before they are shown).
watch(
  () => logs.value[logs.value.length - 1]?.id,
  async () => {
    if (!atBottom() || !viewport.value) return
    await nextTick()
    viewport.value.scrollTop = viewport.value.scrollHeight
  }
)
</script>

<template>
  <main class="flex flex-col flex-1 overflow-hidden">
    <div class="flex-shrink-0 px-0 sm:px-4 py-2 sm:py-4">
      <div class="max-w-7xl mx-auto bg-muted/30 p-2 rounded-lg">
        <FilterBar id="logs" v-model="filters" :dropdowns="dropdowns" :fields="fields" />
      </div>
    </div>

    <div class="flex-1 flex flex-col overflow-hidden px-2 sm:pb-4 sm:px-4">
      <div class="max-w-7xl mx-auto w-full flex flex-col flex-1 overflow-hidden gap-2">
        <p v-if="error" class="text-sm text-destructive">{{ error }}</p>
        <p v-if="more" class="flex items-center gap-1 text-xs text-muted-foreground">
          Showing the latest {{ CEILING }} lines that match.
          <FieldHelp
            :text="`Only the latest ${CEILING} lines are shown. Narrow the search or the window to see older ones.`"
          />
        </p>
        <div v-if="loading" class="flex justify-center py-12">
          <Spinner class="animate-spin" />
        </div>
        <Card v-else-if="logs.length === 0" class="flex-1">
          <CardContent class="flex items-center justify-center py-12">
            <p class="text-muted-foreground">No logs found</p>
          </CardContent>
        </Card>
        <div
          v-else
          ref="viewport"
          class="min-h-0 flex-1 overflow-y-auto space-y-2 md:space-y-0 md:border md:rounded-lg md:bg-card"
        >
          <!-- Desktop table (md+): the time and the worker as wide as they are, the message the rest -->
          <table class="hidden md:table w-full caption-bottom text-sm">
            <TableHeader class="sticky top-0 z-10 bg-muted">
              <TableRow class="hover:bg-muted">
                <TableHead class="pl-4 py-2 text-xs w-px whitespace-nowrap">Timestamp</TableHead>
                <TableHead class="py-2 text-xs w-px whitespace-nowrap">Worker</TableHead>
                <TableHead class="py-2 text-xs">Message</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <TableRow
                v-for="entry in logs"
                :key="entry.id"
                :class="
                  entry.stream === 'stderr'
                    ? 'bg-destructive/5 hover:bg-destructive/10 align-top'
                    : 'hover:bg-muted/30 align-top'
                "
              >
                <TableCell
                  class="pl-4 py-2 text-xs w-px font-mono text-muted-foreground whitespace-nowrap"
                >
                  {{ formatDate(entry.ts) }}
                </TableCell>
                <TableCell class="py-2 text-xs w-px font-mono whitespace-nowrap">
                  <button
                    class="flex items-center gap-0.5 text-violet-600 dark:text-violet-400 hover:underline underline-offset-2"
                    :title="`Filter by worker ${entry.hostname}`"
                    @click="filterByWorker(entry.hostname)"
                  >
                    <Filter class="w-3 h-3 shrink-0" />
                    {{ entry.hostname }}
                  </button>
                </TableCell>
                <TableCell class="py-2 text-xs">
                  <pre class="font-mono break-all whitespace-pre-wrap">{{ entry.message }}</pre>
                </TableCell>
              </TableRow>
            </TableBody>
          </table>

          <!-- Mobile cards (< md) -->
          <Card
            v-for="entry in logs"
            :key="entry.id"
            class="md:hidden"
            :class="entry.stream === 'stderr' ? 'border-destructive/40 bg-destructive/5' : ''"
          >
            <CardContent class="px-3 py-2 space-y-2 text-xs">
              <div class="flex flex-wrap items-center gap-x-3 font-mono text-muted-foreground">
                <span>{{ formatDate(entry.ts) }}</span>
                <button
                  class="flex items-center gap-0.5 text-violet-600 dark:text-violet-400 hover:underline underline-offset-2"
                  :title="`Filter by worker ${entry.hostname}`"
                  @click="filterByWorker(entry.hostname)"
                >
                  <Filter class="w-3 h-3 shrink-0" />{{ entry.hostname }}
                </button>
              </div>
              <pre class="font-mono break-all whitespace-pre-wrap leading-relaxed">{{
                entry.message
              }}</pre>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  </main>
</template>

<script setup lang="ts">
import { ref, computed, nextTick, watch } from 'vue'
import { Search, Filter, CircleMinus } from 'lucide-vue-next'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
} from '@/components/ui/input-group'
import { TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Spinner } from '@/components/ui/spinner'
import FieldHelp from '@/components/FieldHelp.vue'
import { useFeedList } from '@/lib/feedList'
import { formatDate } from '@/lib/utils'

import { fetchApplicationLogs, matchesLogQuery, type LogEntry, type LogQuery } from '@/services/api'

// An application's logs, from all its containers or one worker's, followed as they are stored.
const props = defineProps<{ project: string; application: string }>()

const CEILING = 1000

// From starts five minutes back, in local time as its field holds it.
function fiveMinutesAgo() {
  const now = new Date()
  now.setMinutes(now.getMinutes() - 5)
  const year = now.getFullYear()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  const hours = String(now.getHours()).padStart(2, '0')
  const minutes = String(now.getMinutes()).padStart(2, '0')
  return `${year}-${month}-${day}T${hours}:${minutes}`
}

const inputSearch = ref('')
const activeSearch = ref('')
const inputFromDate = ref(fiveMinutesAgo())
const activeFromDate = ref(inputFromDate.value)
const inputToDate = ref<string>('')
const activeToDate = ref<string>('')
const desktopViewport = ref<HTMLElement | null>(null)
const mobileScroll = ref<HTMLElement | null>(null)
const hostnameFilter = ref('')

const fromUTC = computed(() =>
  activeFromDate.value ? new Date(activeFromDate.value).toISOString() : ''
)
const toUTC = computed(() => (activeToDate.value ? new Date(activeToDate.value).toISOString() : ''))
const query = computed<LogQuery>(() => ({
  hostname: hostnameFilter.value,
  search: activeSearch.value,
  from_ts: fromUTC.value,
  to_ts: toUTC.value,
}))

const {
  rows: logs,
  more,
  loading,
  error,
} = useFeedList<LogEntry, LogQuery>({
  topic: `application-logs:${props.project}-${props.application}`,
  order: (a, b) => a.id - b.id,
  newestFirst: false,
  key: entry => entry.id,
  query: () => query.value,
  matches: matchesLogQuery,
  ceiling: CEILING,
  load: async (filters, newest) => {
    const page = await fetchApplicationLogs(props.project, props.application, {
      ...filters,
      after_id: newest?.id ?? 0,
      limit: CEILING,
    })
    return { rows: page.lines, more: page.more }
  },
})

function isNearBottom(threshold: number = 50): boolean {
  if (desktopViewport.value) {
    const { scrollTop, scrollHeight, clientHeight } = desktopViewport.value
    return scrollHeight - (scrollTop + clientHeight) <= threshold
  }
  if (mobileScroll.value) {
    const { scrollTop, scrollHeight, clientHeight } = mobileScroll.value
    return scrollHeight - (scrollTop + clientHeight) <= threshold
  }
  return true
}

async function scrollToBottom() {
  await nextTick()

  // Use requestAnimationFrame to ensure DOM is painted
  await new Promise(resolve => requestAnimationFrame(resolve))

  // Scroll desktop view
  if (desktopViewport.value) {
    desktopViewport.value.scrollTop = desktopViewport.value.scrollHeight
  }

  // Scroll mobile view
  if (mobileScroll.value) {
    mobileScroll.value.scrollTop = mobileScroll.value.scrollHeight
  }
}

// Follow new lines (if near bottom); the count stays at the cap once it is reached, the last line does not.
watch(
  () => logs.value[logs.value.length - 1]?.id,
  async () => {
    if (isNearBottom()) {
      await scrollToBottom()
    }
  }
)

// The lines this view does not have yet: the newest that match at first, then those after the last one it holds.
// The filters as typed apply at once; the list starts again when they changed.
function applySearch() {
  activeSearch.value = inputSearch.value.trim()
  activeFromDate.value = inputFromDate.value
  activeToDate.value = inputToDate.value
}

function onSearchKeypress(e: KeyboardEvent) {
  if (e.key === 'Enter') applySearch()
}

function clearSearch() {
  inputSearch.value = ''
  applySearch()
}

function clearHostnameFilter() {
  hostnameFilter.value = ''
  applySearch()
}

function filterByWorker(hostname: string) {
  hostnameFilter.value = hostname
  applySearch()
}
</script>

<template>
  <main class="flex flex-col flex-1 overflow-hidden">
    <!-- Controls -->
    <div class="flex-shrink-0 px-0 sm:px-4 py-2 sm:py-4">
      <div class="max-w-7xl mx-auto space-y-3 bg-muted/30 p-2 rounded-lg">
        <div class="grid grid-cols-2 gap-2">
          <div>
            <label class="text-xs font-medium text-muted-foreground mb-1 block">From Date</label>
            <Input
              v-model="inputFromDate"
              type="datetime-local"
              class="text-sm"
              @keyup="onSearchKeypress"
            />
          </div>
          <div>
            <label class="text-xs font-medium text-muted-foreground mb-1 block">To Date</label>
            <Input
              v-model="inputToDate"
              type="datetime-local"
              class="text-sm"
              @keyup="onSearchKeypress"
            />
          </div>
        </div>
        <div class="flex flex-col sm:flex-row gap-2">
          <div class="relative flex-1">
            <Search
              class="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground pointer-events-none"
            />
            <InputGroup>
              <InputGroupInput
                v-model="inputSearch"
                placeholder="Search logs…"
                class="pl-8"
                @keyup="onSearchKeypress"
              />
              <InputGroupAddon v-if="hostnameFilter" align="inline-end">
                <InputGroupButton variant="destructive" @click="clearHostnameFilter">
                  <CircleMinus />
                  {{ hostnameFilter }}
                </InputGroupButton>
              </InputGroupAddon>
            </InputGroup>
          </div>
          <Button
            variant="secondary"
            size="sm"
            @click="applySearch"
            class="whitespace-nowrap"
            :disabled="loading"
          >
            <Spinner class="animate-spin" v-if="loading" />
            Search
          </Button>
          <Button
            v-if="activeSearch"
            variant="ghost"
            size="sm"
            @click="clearSearch"
            :disabled="loading"
            class="whitespace-nowrap"
            >Clear</Button
          >
        </div>
      </div>
    </div>

    <!-- Content -->
    <div class="flex-1 flex flex-col overflow-hidden px-2 sm:pb-4 sm:px-4">
      <div class="max-w-7xl mx-auto w-full flex flex-col flex-1 overflow-hidden gap-2">
        <p v-if="more" class="flex items-center gap-1 text-xs text-muted-foreground">
          Showing the latest {{ CEILING }} lines that match.
          <FieldHelp
            :text="`Only the latest ${CEILING} lines are shown. Narrow the search or the window to see older ones.`"
          />
        </p>
        <Card v-if="logs.length === 0" class="flex-1">
          <CardContent class="flex items-center justify-center py-12">
            <p class="text-muted-foreground">{{ error || 'No logs found' }}</p>
          </CardContent>
        </Card>

        <template v-else>
          <!-- Desktop table (md+): the time and the worker as wide as they are, the message the rest -->
          <div
            ref="desktopViewport"
            class="hidden md:block min-h-0 flex-1 overflow-y-auto border rounded-lg bg-card"
          >
            <table class="w-full caption-bottom text-sm">
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
          </div>

          <!-- Mobile cards (< md) -->
          <div ref="mobileScroll" class="md:hidden overflow-y-auto flex-1 space-y-2">
            <Card
              v-for="entry in logs"
              :key="entry.id"
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
        </template>
      </div>
    </div>
  </main>
</template>

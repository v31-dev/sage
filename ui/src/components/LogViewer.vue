<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
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
import { watchFeed } from '@/lib/updates'
import { formatDate, parseDate } from '@/lib/utils'

import { fetchApplicationLogs, type LogEntry } from '@/services/api'

// An application's logs, from all its containers or one worker's, followed as they are stored.
const props = defineProps<{ project: string; application: string }>()

const MAX_LOGS_ON_UI = 1000

const logs = ref<LogEntry[]>([])
// Lines that match are not shown, since the search was applied: the manager sent the newest, or the oldest were
// dropped here.
const more = ref(false)
const isLoading = ref(true)
const inputSearch = ref('')
const activeSearch = ref('')
const inputFromDate = ref<string>('')
const activeFromDate = ref<string>('')
const inputToDate = ref<string>('')
const activeToDate = ref<string>('')
const desktopViewport = ref<HTMLElement | null>(null)
const mobileScroll = ref<HTMLElement | null>(null)
const error = ref<string>('')
const hostnameFilter = ref('')
let stop: (() => void) | undefined
// Moved on each time the filters are applied, so that a load asked for the filters before is dropped.
let generation = 0

const fromUTC = computed(() =>
  activeFromDate.value ? new Date(activeFromDate.value).toISOString() : ''
)
const toUTC = computed(() => (activeToDate.value ? new Date(activeToDate.value).toISOString() : ''))

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

// The rules of the logs route: the worker, the text anywhere in any case, and the window.
function matches(entry: LogEntry) {
  const search = activeSearch.value.toLowerCase()
  return (
    (!hostnameFilter.value || entry.hostname === hostnameFilter.value) &&
    (!search || entry.message.toLowerCase().includes(search)) &&
    (!fromUTC.value || parseDate(entry.ts) >= new Date(fromUTC.value)) &&
    (!toUTC.value || parseDate(entry.ts) < new Date(toUTC.value))
  )
}

// The lines this view does not have yet: the newest that match at first, then those after the last one it holds.
async function load() {
  const asked = generation
  const result = await fetchApplicationLogs(props.project, props.application, {
    hostname: hostnameFilter.value,
    search: activeSearch.value,
    from_ts: fromUTC.value,
    to_ts: toUTC.value,
    after_id: logs.value[logs.value.length - 1]?.id ?? 0,
  })
  if (asked !== generation) return []
  more.value ||= result.more
  return result.lines
}

// Merged in the order they were stored, each once, when the filters let them through; the newest are kept.
function addLines(rows: LogEntry[]) {
  const known = new Set(logs.value.map(entry => entry.id))
  const fresh = rows.filter(entry => matches(entry) && !known.has(entry.id))
  if (!fresh.length) return
  const merged = [...logs.value, ...fresh].sort((a, b) => a.id - b.id)
  more.value ||= merged.length > MAX_LOGS_ON_UI
  logs.value = merged.slice(-MAX_LOGS_ON_UI)
}

async function applySearch() {
  activeSearch.value = inputSearch.value.trim()
  activeFromDate.value = inputFromDate.value
  activeToDate.value = inputToDate.value
  logs.value = []
  more.value = false
  error.value = ''
  const current = ++generation
  isLoading.value = true
  try {
    addLines(await load())
  } catch (err) {
    if (current === generation)
      error.value = err instanceof Error ? err.message : 'Failed to fetch logs'
  } finally {
    if (current === generation) isLoading.value = false
  }
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

onMounted(async () => {
  // Set inputFromDate to today - 5 min (local timezone)
  const now = new Date()
  now.setMinutes(now.getMinutes() - 5)
  const year = now.getFullYear()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  const hours = String(now.getHours()).padStart(2, '0')
  const minutes = String(now.getMinutes()).padStart(2, '0')
  inputFromDate.value = `${year}-${month}-${day}T${hours}:${minutes}`
  activeFromDate.value = inputFromDate.value

  const feed = watchFeed(`application-logs:${props.project}-${props.application}`, load, addLines)
  stop = feed.stop
  try {
    await feed.first
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Failed to fetch logs'
  } finally {
    isLoading.value = false
  }
})

onUnmounted(() => stop?.())
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
            :disabled="isLoading"
          >
            <Spinner class="animate-spin" v-if="isLoading" />
            Search
          </Button>
          <Button
            v-if="activeSearch"
            variant="ghost"
            size="sm"
            @click="clearSearch"
            :disabled="isLoading"
            class="whitespace-nowrap"
            >Clear</Button
          >
        </div>
      </div>
    </div>

    <!-- Content -->
    <div class="flex-1 flex flex-col overflow-hidden px-2 sm:pb-4 sm:px-4">
      <div class="max-w-7xl mx-auto w-full flex flex-col flex-1 overflow-hidden gap-2">
        <p v-if="more" class="text-xs text-muted-foreground">
          Showing the newest {{ MAX_LOGS_ON_UI }} lines that match. Narrow the search or the time
          range to see the others.
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

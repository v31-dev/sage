<script setup lang="ts">
import { ref, onMounted, onUnmounted, nextTick, watch } from 'vue'
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Spinner } from '@/components/ui/spinner'
import { formatDate } from '@/lib/utils'

import { fetchApplicationLogs, type LogEntry } from '@/services/api'

// An application's logs, from all its containers or one worker's.
const props = defineProps<{ project: string; application: string }>()

const MAX_LOGS_ON_UI = 1000
const POLL_INTERVAL = 5_000

const logs = ref<LogEntry[]>([])
// The manager had more lines than it sent since the search was applied.
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
let pollTimer: ReturnType<typeof setInterval> | null = null
const hostnameFilter = ref('')

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

// Watch logs array and scroll when it changes (if near bottom)
watch(
  () => logs.value.length,
  async () => {
    if (isNearBottom()) {
      await scrollToBottom()
    }
  }
)

async function load() {
  // Don't load if the current query gives an error
  if (error.value != '') {
    return
  }

  isLoading.value = true

  const fromDateUTC = activeFromDate.value ? new Date(activeFromDate.value).toISOString() : ''
  const toDateUTC = activeToDate.value ? new Date(activeToDate.value).toISOString() : ''

  try {
    if (toDateUTC != '' && toDateUTC < new Date().toISOString()) {
      // toDate is in the past - no new logs
    } else {
      const result = await fetchApplicationLogs(props.project, props.application, {
        hostname: hostnameFilter.value,
        search: activeSearch.value,
        from_ts: fromDateUTC,
        to_ts: toDateUTC,
      })
      logs.value.push(...result.lines)
      more.value ||= result.more
      // Keep only last 1000 logs on UI
      if (logs.value.length > MAX_LOGS_ON_UI) {
        logs.value = logs.value.slice(-MAX_LOGS_ON_UI)
      }
    }

    // Update for polling
    if (logs.value.length > 0) {
      const lastLog = logs.value[logs.value.length - 1]
      if (lastLog) {
        activeFromDate.value = lastLog.ts
      }
    }
  } catch (err) {
    if (err instanceof Error) {
      error.value = err.message
    } else {
      error.value = 'Failed to fetch logs'
    }
  } finally {
    isLoading.value = false
  }
}

function applySearch() {
  activeSearch.value = inputSearch.value.trim()
  activeFromDate.value = inputFromDate.value
  activeToDate.value = inputToDate.value
  logs.value = []
  more.value = false
  error.value = ''
  load()
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

  // Load initial logs
  await load()

  // Start polling every X seconds
  pollTimer = setInterval(load, POLL_INTERVAL)
})

onUnmounted(() => {
  if (pollTimer !== null) clearInterval(pollTimer)
})
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
          <!-- Desktop table (md+) -->
          <div class="hidden md:flex md:flex-col min-h-0 flex-1 border rounded-lg bg-card">
            <Table class="table-fixed">
              <TableHeader class="bg-muted/50">
                <TableRow class="hover:bg-muted/50">
                  <TableHead class="pl-4 py-2 text-xs w-40">Timestamp</TableHead>
                  <TableHead class="py-2 text-xs w-24">Worker</TableHead>
                  <TableHead class="py-2 text-xs">Message</TableHead>
                </TableRow>
              </TableHeader>
            </Table>
            <div ref="desktopViewport" class="flex-1 min-h-0 overflow-y-auto">
              <table class="w-full caption-bottom text-sm table-fixed">
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
                      class="pl-4 py-2 text-xs w-40 font-mono text-muted-foreground whitespace-nowrap"
                    >
                      {{ formatDate(entry.ts) }}
                    </TableCell>
                    <TableCell class="py-2 text-xs w-24 font-mono whitespace-nowrap">
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

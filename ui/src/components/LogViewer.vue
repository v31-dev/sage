<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { Search, Filter } from 'lucide-vue-next'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { fetchLogs, type LogEntry } from '@/services/api'


interface Props {
  container: string
  hostname: string
  parseMessage: (raw: string, entry: LogEntry) => {
    ts: string,
    message: string,
    [key: string]: any
  },
  columns: {
    key: string,
    label: string,
    headerClass: string,
    rowClass: string,
    cellClass?: (entry: any) => string,
    filter?: boolean,
    formatter?: (value: any) => string
  }[]
}

const props = withDefaults(defineProps<Props>(), {})

const MAX_LOGS_ON_UI = 1000

const logs = ref<LogEntry[]>([])
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

const parsedLogs = computed(() =>
  logs.value.map(e => ({ entry: e, parsed: props.parseMessage(e.message, e) }))
)

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

// Watch logs array and scroll when it changes
watch(() => logs.value.length, async () => {
  await scrollToBottom()
})

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
      // fetch logs
      logs.value.push(...await fetchLogs(props.hostname, props.container, activeSearch.value, fromDateUTC, toDateUTC))
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

function filterBy(value: string) {
  if (!value) return
  inputSearch.value = value
  applySearch()
}

onMounted(async () => {
  // Set inputFromDate to today (local timezone)
  const now = new Date()
  const year = now.getFullYear()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  const hours = String(now.getHours()).padStart(2, '0')
  const minutes = String(now.getMinutes()).padStart(2, '0')
  inputFromDate.value = `${year}-${month}-${day}T${hours}:${minutes}`
  activeFromDate.value = inputFromDate.value

  // Load initial logs
  await load()

  // Start polling every 5 seconds
  pollTimer = setInterval(load, 5_000)
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
            <Input v-model="inputFromDate" type="datetime-local" class="text-sm" @keyup="onSearchKeypress" />
          </div>
          <div>
            <label class="text-xs font-medium text-muted-foreground mb-1 block">To Date</label>
            <Input v-model="inputToDate" type="datetime-local" class="text-sm" @keyup="onSearchKeypress" />
          </div>
        </div>
        <div class="flex flex-col sm:flex-row gap-2">
          <div class="relative flex-1">
            <Search
              class="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground pointer-events-none" />
            <Input v-model="inputSearch" placeholder="Search logs…" class="pl-8" @keyup="onSearchKeypress" />
          </div>
          <Button variant="secondary" size="sm" @click="applySearch" class="whitespace-nowrap"
            :disabled="isLoading">Search</Button>
          <Button v-if="activeSearch" variant="ghost" size="sm" @click="clearSearch" :disabled="isLoading"
            class="whitespace-nowrap">Clear</Button>
        </div>
      </div>
    </div>

    <!-- Content -->
    <div class="flex-1 flex flex-col overflow-hidden px-2 sm:pb-4 sm:px-4">
      <div class="max-w-7xl mx-auto w-full flex flex-col flex-1 overflow-hidden">
        <Card v-if="logs.length === 0" class="flex-1">
          <CardContent class="flex items-center justify-center py-12">
            <p class="text-muted-foreground">{{ error || 'No logs found' }}</p>
          </CardContent>
        </Card>

        <template v-else>
          <!-- Desktop table (md+) -->
          <div class="hidden md:flex md:flex-col min-h-0 flex-1 border rounded-lg bg-card">
            <Table>
              <TableHeader class="bg-muted/50">
                <TableRow class="hover:bg-muted/50">
                  <TableHead v-for="column in columns" :key="column.key" :class="column.headerClass">{{ column.label }}
                  </TableHead>
                </TableRow>
              </TableHeader>
            </Table>
            <div ref="desktopViewport" class="flex-1 min-h-0 overflow-y-auto">
              <table class="w-full caption-bottom text-sm">
                <TableBody>
                  <TableRow v-for="{ entry, parsed } in parsedLogs" :key="entry.id"
                    :class="entry.stream === 'stderr' ? 'bg-red-50 dark:bg-red-950/30 hover:bg-red-100 dark:hover:bg-red-950/50 align-top' : 'hover:bg-muted/30 align-top'">
                    <TableCell v-for="column in columns" :key="column.key" :class="column.rowClass">
                      <template v-if="column.filter && column.filter == true">
                        <button v-if="parsed[column.key]"
                          class="flex items-center gap-0.5 text-violet-600 dark:text-violet-400 hover:underline underline-offset-2"
                          :title="`Filter by ${column.label} ${parsed[column.key]}`"
                          @click="filterBy(parsed[column.key])">
                          <Filter class="w-3 h-3 shrink-0" />
                          {{ column.formatter ? column.formatter(parsed[column.key]) : (parsed[column.key] || '-') }}
                        </button>
                        <span v-else class="text-muted-foreground">-</span>
                      </template>
                      <template v-else>
                        <span :class="column.cellClass ? column.cellClass(parsed[column.key]) : ''">
                          {{ column.formatter ? column.formatter(parsed[column.key]) : (parsed[column.key] || '-') }}
                        </span>
                      </template>
                    </TableCell>
                  </TableRow>
                </TableBody>
              </table>
            </div>
          </div>

          <!-- Mobile cards (< md) -->
          <div ref="mobileScroll" class="md:hidden overflow-y-auto flex-1 space-y-2">
            <Card v-for="{ entry, parsed } in parsedLogs" :key="entry.id"
              :class="entry.stream === 'stderr' ? 'border-red-300 dark:border-red-800 bg-red-50 dark:bg-red-950/30' : ''">
              <CardContent class="px-3 py-2 space-y-2">
                <div class="grid grid-cols-2 gap-4 text-xs">
                  <template v-for="column in columns" :key="column.key">
                    <template v-if="column.key === 'message'">
                      <div class="col-span-2">
                        <dt class="text-[10px] text-muted-foreground font-medium mb-1">{{ column.label }}</dt>
                        <dd class="font-mono text-xs break-all whitespace-pre-wrap leading-relaxed">{{
                          column.formatter ? column.formatter(parsed[column.key]) : (parsed[column.key] || '-') }}</dd>
                      </div>
                    </template>
                    <template v-else-if="column.filter && column.filter == true">
                      <div>
                        <dt class="text-muted-foreground font-medium text-[10px]">{{ column.label }}</dt>
                        <dd class="font-mono">
                          <button v-if="parsed[column.key]"
                            class="flex items-center gap-0.5 text-violet-600 dark:text-violet-400 hover:underline underline-offset-2"
                            :title="`Filter by ${column.label} ${parsed[column.key]}`"
                            @click="filterBy(parsed[column.key])">
                            <Filter class="w-3 h-3 shrink-0" />{{ column.formatter ?
                              column.formatter(parsed[column.key]) : (parsed[column.key] || '-') }}
                          </button>
                          <span v-else class="text-muted-foreground">-</span>
                        </dd>
                      </div>
                    </template>
                    <template v-else>
                      <div>
                        <dt class="text-muted-foreground font-medium text-[10px]">{{ column.label }}</dt>
                        <dd class="font-mono text-muted-foreground"
                          :class="column.cellClass ? column.cellClass(parsed[column.key]) : ''">{{ column.formatter ?
                            column.formatter(parsed[column.key]) : (parsed[column.key] || '-') }}</dd>
                      </div>
                    </template>
                  </template>
                </div>
              </CardContent>
            </Card>
          </div>
        </template>
      </div>
    </div>
  </main>
</template>
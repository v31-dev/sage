<script setup lang="ts">
import { computed, ref } from 'vue'
import { Card, CardContent } from '@/components/ui/card'
import { Spinner } from '@/components/ui/spinner'
import {
  Table,
  TableBody,
  TableCell,
  TableEmpty,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import FieldHelp from '@/components/FieldHelp.vue'
import FilterBar, { type FilterDropdown, type FilterField } from '@/components/FilterBar.vue'
import { useFeedList } from '@/lib/feedList'
import { formatDate, localTimeToUTC } from '@/lib/utils'
import { TONES, type Tone } from '@/lib/tone'
import {
  fetchRequests,
  matchesRequestQuery,
  type RequestQuery,
  type ServedRequest,
  type StatusClass,
} from '@/services/api'
import { useAppStore } from '@/stores/app'

const CEILING = 1000
const METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']
const STATUS_CLASSES: StatusClass[] = ['2xx', '3xx', '4xx', '5xx']
const STATUS_TONES: Record<string, Tone> = {
  '2': 'success',
  '3': 'info',
  '4': 'warning',
  '5': 'error',
}

const appStore = useAppStore()
// From and To are local times, as their fields hold them.
const filters = ref({
  worker: '',
  method: '',
  status: '',
  host: '',
  path: '',
  slower: '',
  from: '',
  to: '',
})

const dropdowns = computed<FilterDropdown[]>(() => [
  {
    key: 'worker',
    label: 'Worker',
    any: 'Any worker',
    options: (appStore.workers ?? []).map(worker => worker.hostname),
  },
  { key: 'method', label: 'Method', any: 'Any method', options: METHODS },
  { key: 'status', label: 'Status', any: 'Any status', options: STATUS_CLASSES },
])

const fields: FilterField[] = [
  { key: 'host', label: 'Host', type: 'text', placeholder: 'e.g. example.com' },
  { key: 'path', label: 'Path', type: 'text', placeholder: 'e.g. /api' },
  { key: 'slower', label: 'Slower than (ms)', type: 'number', placeholder: 'e.g. 500' },
  { key: 'from', label: 'From', type: 'datetime-local' },
  { key: 'to', label: 'To', type: 'datetime-local' },
]

const query = computed<RequestQuery>(() => {
  const { worker, method, status, host, path, slower, from, to } = filters.value
  return {
    worker,
    method,
    status: status as StatusClass | '',
    host,
    path,
    min_duration_ms: slower === '' ? null : Number(slower),
    from_ts: localTimeToUTC(from),
    to_ts: localTimeToUTC(to),
  }
})

function statusClass(status: number) {
  return TONES[STATUS_TONES[String(status)[0]!] ?? 'neutral'].text
}

function formatDuration(ms: number) {
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(2)} s`
}

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`
  return `${(bytes / 1024 ** 2).toFixed(1)} MiB`
}

const {
  rows: requests,
  more,
  loading,
  error,
} = useFeedList<ServedRequest, RequestQuery>({
  topic: 'requests',
  order: (a, b) => b.id - a.id,
  newestFirst: true,
  key: row => row.id,
  query: () => query.value,
  matches: matchesRequestQuery,
  ceiling: CEILING,
  load: async (filters, newest) => {
    const page = await fetchRequests({ ...filters, after_id: newest?.id ?? 0, limit: CEILING })
    return { rows: page.requests, more: page.more }
  },
})
</script>

<template>
  <main class="flex-1 px-2 sm:px-4 py-4">
    <div class="max-w-7xl mx-auto space-y-4">
      <FilterBar id="requests" v-model="filters" :dropdowns="dropdowns" :fields="fields" />

      <p v-if="error" class="text-sm text-destructive">{{ error }}</p>
      <p v-if="more" class="flex items-center gap-1 text-xs text-muted-foreground">
        Showing the latest {{ requests.length }} requests that match.
        <FieldHelp
          :text="`Only the latest ${CEILING} requests are shown. Narrow the filters or the window to see older ones.`"
        />
      </p>

      <div v-if="loading" class="flex justify-center py-12">
        <Spinner class="animate-spin" />
      </div>
      <template v-else>
        <!-- Desktop table (md+) -->
        <div class="hidden md:block border rounded-lg bg-card">
          <Table>
            <TableHeader class="bg-muted/50">
              <TableRow>
                <TableHead class="pl-4 text-xs">Time</TableHead>
                <TableHead class="text-xs">Worker</TableHead>
                <TableHead class="text-xs">Method</TableHead>
                <TableHead class="text-xs">Host</TableHead>
                <TableHead class="text-xs">Path</TableHead>
                <TableHead class="text-xs">Status</TableHead>
                <TableHead class="text-xs text-right">Duration</TableHead>
                <TableHead class="text-xs text-right">Size</TableHead>
                <TableHead class="text-xs">Client</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <TableEmpty v-if="!requests.length" :colspan="9">No requests found</TableEmpty>
              <TableRow v-for="row in requests" :key="row.id" class="align-top text-xs font-mono">
                <TableCell class="pl-4 whitespace-nowrap text-muted-foreground">
                  {{ formatDate(row.ts) }}
                </TableCell>
                <TableCell class="whitespace-nowrap">{{ row.worker }}</TableCell>
                <TableCell>{{ row.method }}</TableCell>
                <TableCell class="break-all whitespace-normal">{{ row.host }}</TableCell>
                <TableCell class="break-all whitespace-normal">{{ row.path }}</TableCell>
                <TableCell :class="statusClass(row.status)">{{ row.status }}</TableCell>
                <TableCell class="text-right whitespace-nowrap">
                  {{ formatDuration(row.duration_ms) }}
                </TableCell>
                <TableCell class="text-right whitespace-nowrap">{{
                  formatSize(row.size)
                }}</TableCell>
                <TableCell class="whitespace-nowrap">{{ row.client }}</TableCell>
              </TableRow>
            </TableBody>
          </Table>
        </div>

        <!-- Mobile cards (< md) -->
        <div class="md:hidden space-y-2">
          <p v-if="!requests.length" class="py-4 text-center text-sm text-muted-foreground">
            No requests found
          </p>
          <Card v-for="row in requests" :key="row.id">
            <CardContent class="px-3 py-2 space-y-1 text-xs font-mono">
              <div class="flex flex-wrap gap-x-3 text-muted-foreground">
                <span>{{ formatDate(row.ts) }}</span>
                <span>{{ row.worker }}</span>
                <span>{{ row.client }}</span>
              </div>
              <p class="break-all">
                {{ row.method }} {{ row.host }}{{ row.path }}
                <span :class="statusClass(row.status)">{{ row.status }}</span>
              </p>
              <p class="text-muted-foreground">
                {{ formatDuration(row.duration_ms) }} · {{ formatSize(row.size) }}
              </p>
            </CardContent>
          </Card>
        </div>
      </template>
    </div>
  </main>
</template>

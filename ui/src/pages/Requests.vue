<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
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
import { formatDate } from '@/lib/utils'
import { TONES, type Tone } from '@/lib/tone'
import {
  fetchRequests,
  type RequestQuery,
  type ServedRequest,
  type StatusClass,
} from '@/services/api'
import { useAppStore } from '@/stores/app'

const POLL_INTERVAL = 5_000
// A dropdown's choice for no filter, since a select item cannot have an empty value.
const ANY = 'any'
const METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']
const STATUS_CLASSES: StatusClass[] = ['2xx', '3xx', '4xx', '5xx']
const STATUS_TONES: Record<string, Tone> = {
  '2': 'success',
  '3': 'info',
  '4': 'warning',
  '5': 'error',
}

const appStore = useAppStore()
const requests = ref<ServedRequest[]>([])
const more = ref(false)
const loaded = ref(false)
const error = ref('')
// From and To are local times, as their fields hold them.
const filters = reactive({
  worker: ANY,
  method: ANY,
  status: ANY,
  host: '',
  path: '',
  slower: '',
  from: '',
  to: '',
})
let timer: ReturnType<typeof setTimeout> | undefined
let loads = 0
let stopped = false

const dropdowns = computed(() => [
  {
    key: 'worker' as const,
    label: 'Worker',
    any: 'Any worker',
    options: (appStore.workers ?? []).map(worker => worker.hostname),
  },
  { key: 'method' as const, label: 'Method', any: 'Any method', options: METHODS },
  { key: 'status' as const, label: 'Status', any: 'Any status', options: STATUS_CLASSES },
])

const fields = [
  { key: 'host' as const, label: 'Host', type: 'text', placeholder: 'e.g. example.com' },
  { key: 'path' as const, label: 'Path', type: 'text', placeholder: 'e.g. /api' },
  { key: 'slower' as const, label: 'Slower than (ms)', type: 'number', placeholder: 'e.g. 500' },
  { key: 'from' as const, label: 'From', type: 'datetime-local', placeholder: '' },
  { key: 'to' as const, label: 'To', type: 'datetime-local', placeholder: '' },
]

const query = computed<RequestQuery>(() => ({
  worker: filters.worker === ANY ? '' : filters.worker,
  method: filters.method === ANY ? '' : filters.method,
  status: filters.status === ANY ? '' : (filters.status as StatusClass),
  host: filters.host,
  path: filters.path,
  min_duration_ms: filters.slower === '' ? null : Number(filters.slower),
  from_ts: filters.from ? new Date(filters.from).toISOString() : '',
  to_ts: filters.to ? new Date(filters.to).toISOString() : '',
}))

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

// A field's value once it is committed (Enter, or leaving the field), not at each key.
function commit(key: (typeof fields)[number]['key'], event: Event) {
  filters[key] = (event.target as HTMLInputElement).value.trim()
}

// One chain: a load started later wins over one under way, and the next is timed from the end of the last.
async function load() {
  const current = ++loads
  clearTimeout(timer)
  try {
    const result = await fetchRequests(query.value)
    if (current !== loads) return
    requests.value = result.requests
    more.value = result.more
    error.value = ''
  } catch (err) {
    if (current === loads)
      error.value = err instanceof Error ? err.message : 'Failed to load requests'
  } finally {
    if (current === loads) {
      loaded.value = true
      if (!stopped) timer = setTimeout(load, POLL_INTERVAL)
    }
  }
}

watch(query, load)
onMounted(load)
onUnmounted(() => {
  stopped = true
  clearTimeout(timer)
})
</script>

<template>
  <main class="flex-1 px-2 sm:px-4 py-4">
    <div class="max-w-7xl mx-auto space-y-4">
      <div class="grid gap-3 grid-cols-2 sm:grid-cols-4">
        <div v-for="dropdown in dropdowns" :key="dropdown.key" class="space-y-1">
          <Label :for="`requests-${dropdown.key}`" class="text-xs">{{ dropdown.label }}</Label>
          <Select v-model="filters[dropdown.key]">
            <SelectTrigger :id="`requests-${dropdown.key}`" class="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem :value="ANY">{{ dropdown.any }}</SelectItem>
              <SelectItem v-for="option in dropdown.options" :key="option" :value="option">
                {{ option }}
              </SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div v-for="field in fields" :key="field.key" class="space-y-1">
          <Label :for="`requests-${field.key}`" class="text-xs">{{ field.label }}</Label>
          <Input
            :id="`requests-${field.key}`"
            :type="field.type"
            :placeholder="field.placeholder || undefined"
            :min="field.type === 'number' ? 0 : undefined"
            :model-value="filters[field.key]"
            @change="commit(field.key, $event)"
          />
        </div>
      </div>

      <p v-if="error" class="text-sm text-destructive">{{ error }}</p>
      <p v-if="more" class="text-xs text-muted-foreground">
        Showing the newest {{ requests.length }} requests that match. Narrow the filters to see the
        others.
      </p>

      <div v-if="!loaded" class="flex justify-center py-12">
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
                <TableHead class="text-xs">Service</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <TableEmpty v-if="!requests.length" :colspan="10">No requests found</TableEmpty>
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
                <TableCell class="break-all whitespace-normal" :title="row.router">
                  {{ row.service }}
                </TableCell>
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
                {{ formatDuration(row.duration_ms) }} · {{ formatSize(row.size) }} ·
                {{ row.service }}
              </p>
            </CardContent>
          </Card>
        </div>
      </template>
    </div>
  </main>
</template>

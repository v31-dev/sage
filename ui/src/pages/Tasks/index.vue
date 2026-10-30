<script setup lang="ts">
import { ref, computed, reactive, watch, onMounted, onUnmounted } from 'vue'
import { ChevronLeft, ChevronRight } from 'lucide-vue-next'
import { Spinner } from '@/components/ui/spinner'
import { toast } from 'vue-sonner'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import TaskCard from './TaskCard.vue'
import TaskTable from './TaskTable.vue'
import { watchFeed, watchState } from '@/lib/updates'
import {
  fetchTaskHistory,
  fetchTaskHistoryFilters,
  fetchTasks,
  matchesTaskHistoryQuery,
  type FinishedTask,
  type QueueResponse,
  type TaskHistoryFilters,
} from '@/services/api'

const PAGE_SIZE = 50
// A dropdown's choice for no filter, since a select item cannot have an empty value.
const ANY = 'any'

const queue = ref<QueueResponse>({ running: [], queued: [] })
const finished = ref<FinishedTask[]>([])
const total = ref(0)
const offset = ref(0)
const choices = ref<TaskHistoryFilters>({ names: [], executors: [], statuses: [] })
// From and To are local times, as their fields hold them.
const filters = reactive({ name: ANY, executor: ANY, status: ANY, scope: '', from: '', to: '' })
const isLoading = ref(true)
let loads = 0
let pushedDuringLoad: FinishedTask[] | null = null
let stops: Array<() => void> = []

const queueSections = computed(() => [
  { title: 'Running', section: 'running' as const, rows: queue.value.running },
  { title: 'Queued', section: 'queued' as const, rows: queue.value.queued },
])

const dropdowns = computed(() => [
  { key: 'name' as const, label: 'Name', any: 'Any name', options: choices.value.names },
  {
    key: 'executor' as const,
    label: 'Executor',
    any: 'Any executor',
    options: choices.value.executors,
  },
  { key: 'status' as const, label: 'Status', any: 'Any status', options: choices.value.statuses },
])

const query = computed(() => ({
  name: filters.name === ANY ? '' : filters.name,
  executor: filters.executor === ANY ? '' : filters.executor,
  status: filters.status === ANY ? '' : filters.status,
  scope: filters.scope,
  from_ts: filters.from ? new Date(filters.from).toISOString() : '',
  to_ts: filters.to ? new Date(filters.to).toISOString() : '',
}))

const pageLabel = computed(
  () => `${offset.value + 1}–${Math.min(offset.value + PAGE_SIZE, total.value)} of ${total.value}`
)

function newestFinishedFirst(a: FinishedTask, b: FinishedTask) {
  if (a.finished_at !== b.finished_at) return a.finished_at < b.finished_at ? 1 : -1
  return a.task_id < b.task_id ? 1 : -1
}

// What the feed pushes is merged into page 1 when the filters let it through; a later page holds still.
function addFinished(rows: FinishedTask[]) {
  if (offset.value !== 0) return
  pushedDuringLoad?.push(...rows)
  const known = new Set(finished.value.map(task => task.task_id))
  const fresh = rows.filter(
    task => matchesTaskHistoryQuery(query.value, task) && !known.has(task.task_id)
  )
  if (!fresh.length) return
  total.value += fresh.length
  finished.value = [...fresh, ...finished.value].sort(newestFinishedFirst).slice(0, PAGE_SIZE)
}

// TODO(02 step 3C): the route ignores the offset since step 3B, so Prev and Next show the first page; the paging goes.
// The page of the filters and the offset. A load started later wins over one still under way, and what was pushed
// while it loaded is merged into it.
async function loadPage() {
  const load = ++loads
  const pushed: FinishedTask[] = []
  pushedDuringLoad = pushed
  try {
    const page = await fetchTaskHistory({ ...query.value, limit: PAGE_SIZE, offset: offset.value })
    if (load !== loads) return
    pushedDuringLoad = null
    finished.value = page.tasks
    total.value = page.total
    addFinished(pushed)
  } finally {
    if (load === loads) pushedDuringLoad = null
  }
}

function showPage(at: number) {
  offset.value = at
  loadPage().catch(() => toast.error('Failed to load tasks'))
}

// Page 1 is loaded again each time the socket connects, which brings what was missed meanwhile.
async function reloadFirstPage(): Promise<FinishedTask[]> {
  if (offset.value === 0) await loadPage()
  return []
}

// A field's value once it is committed (Enter, or leaving the field), not at each key.
function commit(key: 'scope' | 'from' | 'to', event: Event) {
  filters[key] = (event.target as HTMLInputElement).value.trim()
}

watch(query, () => showPage(0))

onMounted(async () => {
  const watches = [
    watchState('tasks', fetchTasks, next => (queue.value = next)),
    watchFeed('task-history', reloadFirstPage, addFinished),
  ]
  stops = watches.map(watch => watch.stop)
  const loadChoices = fetchTaskHistoryFilters().then(next => (choices.value = next))
  try {
    await Promise.all([loadChoices, ...watches.map(watch => watch.first)])
  } catch {
    toast.error('Failed to load tasks')
  } finally {
    isLoading.value = false
  }
})

onUnmounted(() => stops.forEach(stop => stop()))
</script>

<template>
  <main class="flex-1 px-4 py-4 relative">
    <div class="mx-auto space-y-6 max-w-7xl">
      <div v-if="isLoading" class="absolute inset-0 z-50 flex items-center justify-center">
        <div class="flex flex-col items-center gap-4">
          <Spinner class="animate-spin" />
          <p class="text-sm text-muted-foreground">Loading tasks...</p>
        </div>
      </div>

      <div v-else class="space-y-6">
        <!-- Running and queued tasks (in-memory) -->
        <Card v-for="section in queueSections" :key="section.title">
          <CardHeader>
            <CardTitle>{{ section.title }} ({{ section.rows.length }})</CardTitle>
          </CardHeader>
          <CardContent>
            <!-- Desktop table (md+) -->
            <div class="hidden md:block">
              <TaskTable :rows="section.rows" :section="section.section" />
            </div>

            <!-- Mobile cards (< md) -->
            <div class="md:hidden space-y-2">
              <p v-if="!section.rows.length" class="py-4 text-center text-sm text-muted-foreground">
                No {{ section.section }} tasks
              </p>
              <TaskCard
                v-for="t in section.rows"
                :key="t.task_id"
                :task="t"
                :section="section.section"
              />
            </div>
          </CardContent>
        </Card>

        <!-- Completed, failed and cancelled tasks (from the DB) -->
        <Card>
          <CardHeader>
            <CardTitle>Finished ({{ total }})</CardTitle>
          </CardHeader>
          <CardContent class="space-y-4">
            <div class="grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
              <div v-for="dropdown in dropdowns" :key="dropdown.key" class="space-y-1">
                <Label :for="`task-${dropdown.key}`" class="text-xs">{{ dropdown.label }}</Label>
                <Select v-model="filters[dropdown.key]">
                  <SelectTrigger :id="`task-${dropdown.key}`" class="w-full">
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
              <div class="space-y-1">
                <Label for="task-scope" class="text-xs">Scope</Label>
                <Input
                  id="task-scope"
                  :model-value="filters.scope"
                  placeholder="e.g. app:demo-web or app:demo*"
                  @change="commit('scope', $event)"
                />
              </div>
              <div class="space-y-1">
                <Label for="task-from" class="text-xs">From</Label>
                <Input
                  id="task-from"
                  type="datetime-local"
                  :model-value="filters.from"
                  @change="commit('from', $event)"
                />
              </div>
              <div class="space-y-1">
                <Label for="task-to" class="text-xs">To</Label>
                <Input
                  id="task-to"
                  type="datetime-local"
                  :model-value="filters.to"
                  @change="commit('to', $event)"
                />
              </div>
            </div>

            <!-- Desktop table (md+) -->
            <div class="hidden md:block">
              <TaskTable :rows="finished" section="finished" />
            </div>

            <!-- Mobile cards (< md) -->
            <div class="md:hidden space-y-2">
              <p v-if="!finished.length" class="py-4 text-center text-sm text-muted-foreground">
                No finished tasks
              </p>
              <TaskCard v-for="t in finished" :key="t.task_id" :task="t" section="finished" />
            </div>

            <div v-if="total" class="flex items-center justify-end gap-2 text-sm">
              <span class="text-muted-foreground">{{ pageLabel }}</span>
              <Button
                variant="outline"
                size="sm"
                :disabled="offset === 0"
                @click="showPage(Math.max(0, offset - PAGE_SIZE))"
              >
                <ChevronLeft />Previous
              </Button>
              <Button
                variant="outline"
                size="sm"
                :disabled="offset + PAGE_SIZE >= total"
                @click="showPage(offset + PAGE_SIZE)"
              >
                Next<ChevronRight />
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  </main>
</template>

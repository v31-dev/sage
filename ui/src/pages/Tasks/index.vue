<script setup lang="ts">
import { ref, computed, reactive, watch, onMounted, onUnmounted } from 'vue'
import { Spinner } from '@/components/ui/spinner'
import { toast } from 'vue-sonner'
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
import FieldHelp from '@/components/FieldHelp.vue'
import TaskCard from './TaskCard.vue'
import TaskTable from './TaskTable.vue'
import { useFeedList } from '@/lib/feedList'
import { watchState } from '@/lib/updates'
import {
  fetchTaskHistory,
  fetchTaskHistoryFilters,
  fetchTasks,
  matchesTaskHistoryQuery,
  type FinishedTask,
  type QueueResponse,
  type TaskHistoryFilters,
  type TaskHistoryQuery,
} from '@/services/api'

// The finished tasks: the newest load at first, and the list grows as tasks finish, up to the ceiling.
const FIRST = 100
const CEILING = 1000
// A dropdown's choice for no filter, since a select item cannot have an empty value.
const ANY = 'any'

const queue = ref<QueueResponse>({ running: [], queued: [] })
const choices = ref<TaskHistoryFilters>({ names: [], executors: [], statuses: [] })
// From and To are local times, as their fields hold them.
const filters = reactive({ name: ANY, executor: ANY, status: ANY, scope: '', from: '', to: '' })
const queueLoaded = ref(false)
const failed = ref(false)
let stopQueue: (() => void) | undefined

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

const query = computed<TaskHistoryQuery>(() => ({
  name: filters.name === ANY ? '' : filters.name,
  executor: filters.executor === ANY ? '' : filters.executor,
  status: filters.status === ANY ? '' : filters.status,
  scope: filters.scope,
  from_ts: filters.from ? new Date(filters.from).toISOString() : '',
  to_ts: filters.to ? new Date(filters.to).toISOString() : '',
}))

function newestFinishedFirst(a: FinishedTask, b: FinishedTask) {
  if (a.finished_at !== b.finished_at) return a.finished_at < b.finished_at ? 1 : -1
  return a.task_id < b.task_id ? 1 : -1
}

const {
  rows: finished,
  total,
  loading: finishedLoading,
  error: finishedError,
} = useFeedList<FinishedTask, TaskHistoryQuery>({
  topic: 'task-history',
  order: newestFinishedFirst,
  newestFirst: true,
  key: task => task.task_id,
  query: () => query.value,
  matches: matchesTaskHistoryQuery,
  ceiling: CEILING,
  // The newest that match, also each time the socket connects: what the list holds is merged with them.
  load: async filters => {
    const page = await fetchTaskHistory({ ...filters, limit: FIRST })
    return { rows: page.tasks, total: page.total }
  },
})

// Until the queue and the finished tasks have loaded the first time, or one could not be.
const finishedLoaded = ref(false)
watch(finishedLoading, loading => (finishedLoaded.value ||= !loading))
const isLoading = computed(() => !failed.value && (!queueLoaded.value || !finishedLoaded.value))

watch(finishedError, error => {
  if (!error) return
  failed.value = true
  toast.error('Failed to load tasks')
})

// A field's value once it is committed (Enter, or leaving the field), not at each key.
function commit(key: 'scope' | 'from' | 'to', event: Event) {
  filters[key] = (event.target as HTMLInputElement).value.trim()
}

onMounted(async () => {
  const watched = watchState('tasks', fetchTasks, next => (queue.value = next))
  stopQueue = watched.stop
  fetchTaskHistoryFilters()
    .then(next => (choices.value = next))
    .catch(() => toast.error('Failed to load tasks'))
  try {
    await watched.first
  } catch {
    failed.value = true
    toast.error('Failed to load tasks')
  } finally {
    queueLoaded.value = true
  }
})

onUnmounted(() => stopQueue?.())
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
            <CardTitle>Finished ({{ total ?? 0 }})</CardTitle>
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

            <p
              v-if="total !== null && total > finished.length"
              class="flex items-center gap-1 text-xs text-muted-foreground"
            >
              Showing the latest {{ finished.length }} of {{ total }}.
              <FieldHelp
                :text="`The latest ${FIRST} load at first, and the list grows as tasks finish, up to ${CEILING}. Narrow the filters to see older ones.`"
              />
            </p>

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
          </CardContent>
        </Card>
      </div>
    </div>
  </main>
</template>

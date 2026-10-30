<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { Spinner } from '@/components/ui/spinner'
import { toast } from 'vue-sonner'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import FieldHelp from '@/components/FieldHelp.vue'
import FilterBar, { type FilterDropdown, type FilterField } from '@/components/FilterBar.vue'
import TaskCard from './TaskCard.vue'
import TaskTable from './TaskTable.vue'
import { useFeedList } from '@/lib/feedList'
import { watchState } from '@/lib/updates'
import { localTimeToUTC } from '@/lib/utils'
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

const queue = ref<QueueResponse>({ running: [], queued: [] })
const choices = ref<TaskHistoryFilters>({ names: [], executors: [], statuses: [] })
// From and To are local times, as their fields hold them.
const filters = ref({ name: '', executor: '', status: '', scope: '', from: '', to: '' })
const queueLoaded = ref(false)
const failed = ref(false)
let stopQueue: (() => void) | undefined

const queueSections = computed(() => [
  { title: 'Running', section: 'running' as const, rows: queue.value.running },
  { title: 'Queued', section: 'queued' as const, rows: queue.value.queued },
])

const dropdowns = computed<FilterDropdown[]>(() => [
  { key: 'name', label: 'Name', any: 'Any name', options: choices.value.names },
  { key: 'executor', label: 'Executor', any: 'Any executor', options: choices.value.executors },
  { key: 'status', label: 'Status', any: 'Any status', options: choices.value.statuses },
])

const fields: FilterField[] = [
  { key: 'scope', label: 'Scope', type: 'text', placeholder: 'e.g. app:demo-web or app:demo*' },
  { key: 'from', label: 'From', type: 'datetime-local' },
  { key: 'to', label: 'To', type: 'datetime-local' },
]

const query = computed<TaskHistoryQuery>(() => {
  const { name, executor, status, scope, from, to } = filters.value
  return { name, executor, status, scope, from_ts: localTimeToUTC(from), to_ts: localTimeToUTC(to) }
})

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
            <FilterBar id="task" v-model="filters" :dropdowns="dropdowns" :fields="fields" />

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

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { Spinner } from '@/components/ui/spinner'
import { toast } from 'vue-sonner'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import TaskCard from './TaskCard.vue'
import TaskTable from './TaskTable.vue'
import { watchFeed, watchState } from '@/lib/updates'
import {
  fetchTaskHistory,
  fetchTasks,
  type CompletedTask,
  type QueueResponse,
} from '@/services/api'

const HISTORY_SHOWN = 100

const queue = ref<QueueResponse>({ running: [], queued: [] })
const completed = ref<CompletedTask[]>([])
const isLoading = ref(true)
let stops: Array<() => void> = []

const queueSections = computed(() => [
  { title: 'Running', rows: queue.value.running },
  { title: 'Queued', rows: queue.value.queued },
])

function addCompleted(rows: CompletedTask[]) {
  const known = new Set(completed.value.map(task => task.task_id))
  const fresh = rows.filter(task => !known.has(task.task_id))
  if (fresh.length === 0) return
  completed.value = [...completed.value, ...fresh]
    .sort((a, b) => (a.created_at < b.created_at ? 1 : a.created_at > b.created_at ? -1 : 0))
    .slice(0, HISTORY_SHOWN)
}

onMounted(async () => {
  const watches = [
    watchState('tasks', fetchTasks, next => (queue.value = next)),
    watchFeed('task-history', fetchTaskHistory, addCompleted),
  ]
  stops = watches.map(watch => watch.stop)
  try {
    await Promise.all(watches.map(watch => watch.first))
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
              <TaskTable :rows="section.rows" :emptyLabel="section.title.toLowerCase()" />
            </div>

            <!-- Mobile cards (< md) -->
            <div class="md:hidden space-y-2">
              <p v-if="!section.rows.length" class="py-4 text-center text-sm text-muted-foreground">
                No {{ section.title.toLowerCase() }} tasks
              </p>
              <TaskCard v-for="t in section.rows" :key="t.task_id" :task="t" />
            </div>
          </CardContent>
        </Card>

        <!-- Completed / failed / cancelled tasks (from the DB) -->
        <Card>
          <CardHeader>
            <CardTitle>Completed ({{ completed.length }})</CardTitle>
          </CardHeader>
          <CardContent>
            <!-- Desktop table (md+) -->
            <div class="hidden md:block">
              <TaskTable :rows="completed" emptyLabel="completed" :showTimestamps="true" />
            </div>

            <!-- Mobile cards (< md) -->
            <div class="md:hidden space-y-2">
              <p v-if="!completed.length" class="py-4 text-center text-sm text-muted-foreground">
                No completed tasks
              </p>
              <TaskCard v-for="t in completed" :key="t.task_id" :task="t" :showTimestamps="true" />
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  </main>
</template>

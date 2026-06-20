<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { Spinner } from '@/components/ui/spinner'
import { toast } from 'vue-sonner'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import TaskCard from './TaskCard.vue'
import TaskTable from './TaskTable.vue'
import { tasksAPI, type TasksResponse } from '@/services/api'

const tasks = ref<TasksResponse>({ running: [], queued: [], completed: [] })
const isLoading = ref(false)
let timer: number | undefined

const queueSections = computed(() => [
  { title: 'Running', rows: tasks.value.running },
  { title: 'Queued', rows: tasks.value.queued },
])

async function loadTasks(showSpinner = false) {
  if (showSpinner) isLoading.value = true
  try {
    tasks.value = (await tasksAPI.fetchAll()) as unknown as TasksResponse
  } catch {
    toast.error('Failed to load tasks')
  } finally {
    if (showSpinner) isLoading.value = false
  }
}

onMounted(async () => {
  await loadTasks(true)
  timer = window.setInterval(loadTasks, 3000)
})

onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
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
            <CardTitle>Completed ({{ tasks.completed.length }})</CardTitle>
          </CardHeader>
          <CardContent>
            <!-- Desktop table (md+) -->
            <div class="hidden md:block">
              <TaskTable :rows="tasks.completed" emptyLabel="completed" :showTimestamps="true" />
            </div>

            <!-- Mobile cards (< md) -->
            <div class="md:hidden space-y-2">
              <p
                v-if="!tasks.completed.length"
                class="py-4 text-center text-sm text-muted-foreground"
              >
                No completed tasks
              </p>
              <TaskCard
                v-for="t in tasks.completed"
                :key="t.task_id"
                :task="t"
                :showTimestamps="true"
              />
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  </main>
</template>

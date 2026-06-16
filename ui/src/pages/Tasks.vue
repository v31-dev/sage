<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { Spinner } from '@/components/ui/spinner'
import { toast } from 'vue-sonner'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Table,
  TableBody,
  TableCell,
  TableEmpty,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import TaskLogsButton from '@/components/TaskLogsButton.vue'
import { tasksAPI, type TasksResponse } from '@/services/api'
import { formatDate } from '@/lib/utils'

const tasks = ref<TasksResponse>({ running: [], queued: [], completed: [] })
const isLoading = ref(false)
let timer: number | undefined

const queueSections = computed(() => [
  { title: 'Running', rows: tasks.value.running, cancellable: false },
  { title: 'Queued', rows: tasks.value.queued, cancellable: true },
])

function statusVariant(status: string) {
  if (status === 'completed') return 'default'
  if (status === 'failed') return 'destructive'
  return 'secondary'
}

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

async function cancel(taskId: string) {
  try {
    await tasksAPI.delete(taskId)
    await loadTasks()
  } catch {
    toast.error('Failed to cancel task')
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
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Task ID</TableHead>
                  <TableHead>Name</TableHead>
                  <TableHead>Scopes</TableHead>
                  <TableHead>Executor</TableHead>
                  <TableHead>Params</TableHead>
                  <TableHead v-if="section.cancellable" class="w-20"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                <TableEmpty v-if="!section.rows.length" :colspan="section.cancellable ? 6 : 5">
                  No {{ section.title.toLowerCase() }} tasks
                </TableEmpty>
                <TableRow v-for="t in section.rows" :key="t.task_id">
                  <TableCell><TaskLogsButton :taskId="t.task_id" /></TableCell>
                  <TableCell>{{ t.name }}</TableCell>
                  <TableCell class="font-mono text-xs">{{ t.scopes.join(', ') }}</TableCell>
                  <TableCell>{{ t.executor }}</TableCell>
                  <TableCell class="font-mono text-xs">{{ JSON.stringify(t.params) }}</TableCell>
                  <TableCell v-if="section.cancellable">
                    <Button variant="outline" size="sm" @click="cancel(t.task_id)">Cancel</Button>
                  </TableCell>
                </TableRow>
              </TableBody>
            </Table>
          </CardContent>
        </Card>

        <!-- Completed / failed / cancelled tasks (from the DB) -->
        <Card>
          <CardHeader>
            <CardTitle>Completed ({{ tasks.completed.length }})</CardTitle>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Task ID</TableHead>
                  <TableHead>Name</TableHead>
                  <TableHead>Scopes</TableHead>
                  <TableHead>Executor</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead>Finished</TableHead>
                  <TableHead>Params</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                <TableEmpty v-if="!tasks.completed.length" :colspan="8">
                  No completed tasks
                </TableEmpty>
                <TableRow v-for="t in tasks.completed" :key="t.task_id">
                  <TableCell><TaskLogsButton :taskId="t.task_id" /></TableCell>
                  <TableCell>{{ t.name }}</TableCell>
                  <TableCell class="font-mono text-xs">{{ t.scopes.join(', ') }}</TableCell>
                  <TableCell>{{ t.executor }}</TableCell>
                  <TableCell>
                    <Badge :variant="statusVariant(t.status)">{{ t.status }}</Badge>
                  </TableCell>
                  <TableCell class="text-xs whitespace-nowrap">{{
                    formatDate(t.created_at)
                  }}</TableCell>
                  <TableCell class="text-xs whitespace-nowrap">
                    {{ t.finished_at ? formatDate(t.finished_at) : '—' }}
                  </TableCell>
                  <TableCell class="font-mono text-xs">{{ JSON.stringify(t.params) }}</TableCell>
                </TableRow>
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </div>
    </div>
  </main>
</template>

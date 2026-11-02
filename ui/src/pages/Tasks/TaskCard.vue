<script setup lang="ts">
import { Card, CardContent } from '@/components/ui/card'
import StateIndicator from '@/components/StateIndicator.vue'
import { Label } from '@/components/ui/label'
import TaskLogsButton from './TaskLogsButton.vue'
import { taskTimes, type TaskSection } from './times'
import { type QueueTask, type FinishedTask } from '@/services/api'
import { useAppStore } from '@/stores/app'

const appStore = useAppStore()

const props = defineProps<{
  task: QueueTask | FinishedTask
  section: TaskSection
}>()
</script>

<template>
  <Card class="gap-0 py-0">
    <CardContent class="space-y-3 p-3">
      <div class="space-y-1">
        <Label class="text-muted-foreground text-xs">Task ID</Label>
        <div class="flex items-center justify-between gap-2">
          <TaskLogsButton :task="props.task" />
          <StateIndicator
            v-if="props.section === 'finished'"
            v-bind="appStore.taskIndicator(props.task.status)"
            show-label
          />
        </div>
      </div>

      <div class="grid grid-cols-2 gap-3">
        <div class="space-y-1">
          <Label class="text-muted-foreground text-xs">Name</Label>
          <p class="font-mono text-sm break-all">{{ props.task.name }}</p>
        </div>
        <div class="space-y-1">
          <Label class="text-muted-foreground text-xs">Executor</Label>
          <p class="font-mono text-sm">{{ props.task.executor }}</p>
        </div>

        <div
          v-for="time in taskTimes(props.task, props.section)"
          :key="time.label"
          class="space-y-1"
        >
          <Label class="text-muted-foreground text-xs">{{ time.label }}</Label>
          <p class="font-mono text-sm">{{ time.value }}</p>
        </div>

        <div class="col-span-2 space-y-1">
          <Label class="text-muted-foreground text-xs">Scopes</Label>
          <p class="font-mono text-xs break-all">{{ props.task.scopes.join(', ') }}</p>
        </div>
        <div class="col-span-2 space-y-1">
          <Label class="text-muted-foreground text-xs">Params</Label>
          <p class="font-mono text-xs break-all whitespace-pre-wrap">
            {{ JSON.stringify(props.task.params) }}
          </p>
        </div>
      </div>
    </CardContent>
  </Card>
</template>

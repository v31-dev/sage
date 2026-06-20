<script setup lang="ts">
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Label } from '@/components/ui/label'
import TaskLogsButton from './TaskLogsButton.vue'
import { type QueueTask, type CompletedTask } from '@/services/api'
import { formatDate } from '@/lib/utils'

const props = withDefaults(
  defineProps<{
    task: QueueTask | CompletedTask
    showTimestamps?: boolean
  }>(),
  { showTimestamps: false }
)

function statusVariant(status: string) {
  if (status === 'completed') return 'default'
  if (status === 'failed') return 'destructive'
  return 'secondary'
}

function asCompleted(t: QueueTask | CompletedTask) {
  return t as CompletedTask
}
</script>

<template>
  <Card class="gap-0 py-0">
    <CardContent class="space-y-3 p-3">
      <div class="space-y-1">
        <Label class="text-muted-foreground text-xs">Task ID</Label>
        <div class="flex items-center justify-between gap-2">
          <TaskLogsButton :taskId="props.task.task_id" />
          <Badge v-if="props.showTimestamps" :variant="statusVariant(props.task.status)">
            {{ props.task.status }}
          </Badge>
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

        <template v-if="props.showTimestamps">
          <div class="space-y-1">
            <Label class="text-muted-foreground text-xs">Created</Label>
            <p class="font-mono text-sm">{{ formatDate(asCompleted(props.task).created_at) }}</p>
          </div>
          <div class="space-y-1">
            <Label class="text-muted-foreground text-xs">Finished</Label>
            <p class="font-mono text-sm">
              {{
                asCompleted(props.task).finished_at
                  ? formatDate(asCompleted(props.task).finished_at as string)
                  : '—'
              }}
            </p>
          </div>
        </template>

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

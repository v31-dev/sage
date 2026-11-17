<script setup lang="ts">
import { ref } from 'vue'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { ScrollText } from 'lucide-vue-next'
import TaskLogs from '@/components/TaskLogs.vue'
import KeyValueLines from './KeyValueLines.vue'
import { type FinishedTask, type QueueTask } from '@/services/api'

const props = defineProps<{ task: QueueTask | FinishedTask }>()
const isOpen = ref(false)
</script>

<template>
  <Dialog v-model:open="isOpen">
    <DialogTrigger asChild>
      <button
        :title="`View logs for task ${props.task.task_id}`"
        class="inline-flex items-center gap-0.5 font-mono text-xs text-violet-600 dark:text-violet-400 hover:underline underline-offset-2 cursor-pointer"
      >
        <ScrollText class="w-3 h-3 shrink-0" />
        {{ props.task.task_id }}
      </button>
    </DialogTrigger>
    <DialogContent class="flex flex-col sm:max-w-6xl">
      <DialogHeader>
        <DialogTitle>Task Logs ({{ props.task.task_id }})</DialogTitle>
        <DialogDescription class="font-mono text-xs">{{ props.task.name }}</DialogDescription>
      </DialogHeader>
      <div class="grid grid-cols-1 gap-4 text-xs sm:grid-cols-2">
        <div class="min-w-0 break-words" data-labels>
          <p class="mb-1 font-medium">Labels</p>
          <KeyValueLines :entries="props.task.labels" />
          <p v-if="!Object.keys(props.task.labels).length" class="text-muted-foreground">-</p>
        </div>
        <div class="min-w-0 break-all" data-params>
          <p class="mb-1 font-medium">Params</p>
          <KeyValueLines :entries="props.task.params" />
          <p v-if="!Object.keys(props.task.params).length" class="text-muted-foreground">-</p>
        </div>
      </div>
      <div class="flex min-h-0 flex-1 flex-col">
        <TaskLogs v-if="isOpen" :taskId="props.task.task_id" />
      </div>
    </DialogContent>
  </Dialog>
</template>

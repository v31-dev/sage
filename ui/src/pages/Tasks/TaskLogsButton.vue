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
    <DialogContent class="sm:max-w-6xl">
      <DialogHeader>
        <DialogTitle>Task Logs ({{ props.task.task_id }})</DialogTitle>
        <DialogDescription class="font-mono text-xs break-all">
          {{ props.task.name }} {{ JSON.stringify(props.task.params) }}
        </DialogDescription>
      </DialogHeader>
      <div class="flex flex-col max-h-[60vh]">
        <TaskLogs v-if="isOpen" :taskId="props.task.task_id" />
      </div>
    </DialogContent>
  </Dialog>
</template>

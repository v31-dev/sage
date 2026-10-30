<script setup lang="ts">
import { ref, watch } from 'vue'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { ScrollText } from 'lucide-vue-next'
import TaskLogs from '@/components/TaskLogs.vue'
import { fetchTaskParts, type FinishedTask, type QueueTask, type TaskPart } from '@/services/api'

const props = defineProps<{ task: QueueTask | FinishedTask }>()
const isOpen = ref(false)

const ALL = 'all'
const parts = ref<TaskPart[]>([])
const part = ref(ALL)

const partLabel = (each: TaskPart) => [each.worker, each.type, each.id].filter(Boolean).join(' · ')

// Read each time the dialog opens; without them, it shows all the lines, as for a task without parts.
watch(isOpen, async open => {
  if (!open) return
  part.value = ALL
  parts.value = await fetchTaskParts(props.task.task_id).catch(() => [])
})
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
      <div v-if="parts.length" class="space-y-1">
        <Label for="task-part" class="text-xs">Part</Label>
        <Select v-model="part">
          <SelectTrigger id="task-part" class="w-full sm:w-96">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem :value="ALL">All lines</SelectItem>
            <SelectItem v-for="each in parts" :key="each.id" :value="each.id">
              {{ partLabel(each) }}
            </SelectItem>
          </SelectContent>
        </Select>
      </div>
      <div class="flex flex-col max-h-[60vh]">
        <TaskLogs
          v-if="isOpen"
          :key="part"
          :taskId="props.task.task_id"
          :subTaskId="part === ALL ? '' : part"
          :parts="parts"
        />
      </div>
    </DialogContent>
  </Dialog>
</template>

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
import StateIndicator from '@/components/StateIndicator.vue'
import TaskLogs from '@/components/TaskLogs.vue'
import KeyValueLines from './KeyValueLines.vue'
import { taskTimes, type TaskSection } from './times'
import { type FinishedTask, type QueueTask } from '@/services/api'
import { useAppStore } from '@/stores/app'

const appStore = useAppStore()

const props = defineProps<{ task: QueueTask | FinishedTask; section: TaskSection }>()
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
        <div class="flex flex-wrap items-center justify-center gap-2 sm:justify-start sm:pr-8">
          <DialogTitle class="font-mono">{{ props.task.name }}</DialogTitle>
          <StateIndicator v-bind="appStore.taskIndicator(props.task.status)" show-label />
        </div>
        <DialogDescription class="text-xs" data-subjects>
          <span class="font-mono">{{ props.task.task_id }}</span>
          <span v-for="(value, name) in props.task.labels" :key="name">
            · {{ name }}: <span class="text-foreground">{{ value }}</span>
          </span>
        </DialogDescription>
      </DialogHeader>
      <div
        class="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-1 rounded-lg border bg-card p-4 text-xs sm:grid-cols-2 sm:gap-x-8"
      >
        <dl
          class="contents sm:grid sm:grid-cols-[max-content_minmax(0,1fr)] sm:content-start sm:gap-x-4 sm:gap-y-1"
        >
          <dt class="text-muted-foreground">Executor</dt>
          <dd data-executor>{{ props.task.executor }}</dd>
          <template v-for="time in taskTimes(props.task, props.section)" :key="time.label">
            <dt class="text-muted-foreground">{{ time.label }}</dt>
            <dd data-time>{{ time.value }}</dd>
          </template>
        </dl>
        <dl
          v-if="props.task.scopes.length || Object.keys(props.task.params).length"
          class="contents sm:grid sm:grid-cols-[max-content_minmax(0,1fr)] sm:content-start sm:gap-x-4 sm:gap-y-1"
        >
          <template v-if="props.task.scopes.length">
            <dt class="text-muted-foreground">Scopes</dt>
            <dd class="font-mono break-all" data-scopes>
              <div v-for="scope in props.task.scopes" :key="scope">{{ scope }}</div>
            </dd>
          </template>
          <template v-if="Object.keys(props.task.params).length">
            <dt class="text-muted-foreground">Params</dt>
            <dd class="font-mono break-all" data-params>
              <KeyValueLines :entries="props.task.params" />
            </dd>
          </template>
        </dl>
      </div>
      <div class="flex min-h-0 flex-1 flex-col">
        <TaskLogs v-if="isOpen" :taskId="props.task.task_id" />
      </div>
    </DialogContent>
  </Dialog>
</template>

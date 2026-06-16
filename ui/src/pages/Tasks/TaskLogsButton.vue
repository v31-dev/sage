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
import LogViewer from '@/components/LogViewer.vue'
import { parseManagerLog } from '@/lib/logs'
import { useAppStore } from '@/stores/app'
import { formatDate, levelClass } from '@/lib/utils'

const props = defineProps<{ taskId: string }>()
const appStore = useAppStore()
const isOpen = ref(false)

const columns = [
  {
    key: 'ts',
    label: 'Timestamp',
    headerClass: 'pl-4 py-2 text-xs w-40',
    rowClass: 'pl-4 py-2 text-xs w-40 font-mono text-muted-foreground whitespace-nowrap',
    formatter: formatDate,
  },
  {
    key: 'level',
    label: 'Level',
    headerClass: 'py-2 text-xs w-16',
    rowClass: 'py-2 text-xs w-16 font-mono whitespace-nowrap',
    cellClass: (level: string) => (level ? levelClass(level) : 'text-muted-foreground'),
  },
  {
    key: 'logger',
    label: 'Logger',
    headerClass: 'py-2 text-xs w-36',
    rowClass: 'py-2 text-xs w-36 font-mono text-muted-foreground truncate',
  },
  {
    key: 'message',
    label: 'Message',
    headerClass: 'py-2 text-xs',
    rowClass: 'py-2 text-xs font-mono break-all whitespace-pre-wrap',
  },
]
</script>

<template>
  <Dialog v-model:open="isOpen">
    <DialogTrigger asChild>
      <button class="font-mono text-xs text-primary hover:underline cursor-pointer">
        {{ props.taskId }}
      </button>
    </DialogTrigger>
    <DialogContent class="sm:max-w-6xl">
      <DialogHeader>
        <DialogTitle>Task Logs</DialogTitle>
        <DialogDescription>Manager logs for task {{ props.taskId }}</DialogDescription>
      </DialogHeader>
      <div class="flex flex-col max-h-[60vh]">
        <LogViewer
          v-if="isOpen"
          :hostname="appStore.info?.hostname ?? ''"
          container="sage"
          :search="props.taskId"
          :parseMessage="parseManagerLog"
          :columns="columns"
          :pollInterval="2_000"
          :poll="true"
        />
      </div>
    </DialogContent>
  </Dialog>
</template>

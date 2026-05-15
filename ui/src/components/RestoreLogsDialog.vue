<script setup lang="ts">
import { ref, watch } from 'vue'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'

import LogViewer from '@/components/LogViewer.vue'
import { parseManagerLog } from '@/lib/logs'
import { useAppStore } from '@/stores/app'
import { formatDate, levelClass } from '@/lib/utils'

interface Props {
  taskId: string
  objectTitle: string
}

const props = withDefaults(defineProps<Props>(), {})
const open = ref(false)

// Auto-open when taskId is set
watch(
  () => props.taskId,
  newTaskId => {
    if (newTaskId) {
      open.value = true
    }
  }
)

const appStore = useAppStore()

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
  <Dialog :open="open" @update:open="open = $event">
    <DialogContent class="sm:max-w-6xl">
      <DialogHeader>
        <DialogTitle>Restore Logs</DialogTitle>
        <DialogDescription>
          Detailed logs from the {{ props.objectTitle }} restore operation (Task:
          {{ props.taskId }})
        </DialogDescription>
      </DialogHeader>
      <div class="flex flex-col max-h-[60vh]">
        <LogViewer
          :key="props.taskId"
          :hostname="appStore.info?.hostname ?? ''"
          container="sage"
          :search="props.taskId"
          :parseMessage="parseManagerLog"
          :columns="columns"
        />
      </div>
    </DialogContent>
  </Dialog>
</template>

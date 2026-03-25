<script setup lang="ts">
import LogViewer from '@/components/LogViewer.vue';
import { useAppStore } from '@/stores/app'
import { formatDate, levelClass } from '@/lib/utils'

const appStore = useAppStore()
const LOG_RE = /^\[([^\]]+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+\[([^\]]*)\]\s*([\s\S]*)$/

function parseMessage(raw: string): { [key: string]: any; ts: string; message: string } {
  const m = raw.match(LOG_RE)
  if (!m) return { ts: '', level: '', logger: '', taskId: '', message: raw }
  return {
    ts: m[1]?.split(',')[0]?.trim() ?? '',
    level: m[2]?.trim() ?? '',
    logger: m[3]?.trim() ?? '',
    taskId: m[4]?.trim() ?? '',
    message: m[5]?.trim() ?? '',
  }
}

const columns = [
  {
    key: 'ts',
    label: 'Timestamp',
    headerClass: 'pl-4 py-2 text-xs w-40',
    rowClass: "pl-4 py-2 text-xs w-40 font-mono text-muted-foreground whitespace-nowrap",
    formatter: formatDate
  },
  {
    key: 'level',
    label: 'Level',
    headerClass: 'py-2 text-xs w-16',
    rowClass: "py-2 text-xs w-16 font-mono whitespace-nowrap",
    cellClass: (level: string) => level ? levelClass(level) : 'text-muted-foreground'
  },
  {
    key: 'logger',
    label: 'Logger',
    headerClass: 'py-2 text-xs w-36',
    rowClass: "py-2 text-xs w-36 font-mono text-muted-foreground truncate"
  },
  {
    key: 'taskId',
    label: 'Task ID',
    headerClass: 'py-2 text-xs w-24',
    rowClass: "py-2 text-xs w-24 font-mono",
    filter: true
  },
  {
    key: 'message',
    label: 'Message',
    headerClass: 'py-2 text-xs',
    rowClass: "py-2 text-xs font-mono break-all whitespace-pre-wrap"
  },
]
</script>

<template>
  <LogViewer :hostname="appStore.info?.hostname ?? ''" container="sage" :parseMessage="parseMessage"
    :columns="columns" />
</template>
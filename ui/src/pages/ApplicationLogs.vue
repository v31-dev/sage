<script setup lang="ts">
import { useRoute } from 'vue-router'
import LogViewer from '@/components/LogViewer.vue'
import { formatDate } from '@/lib/utils'

const route = useRoute()
const projectName = route.params.projectId as string
const appName = route.params.appId as string
const containerName = `${projectName}-${appName}`

function parseMessage(
  raw: string,
  entry: any
): { [key: string]: any; ts: string; message: string } {
  try {
    return {
      ts: entry['ts'] ?? '',
      worker: entry['hostname'] ?? '',
      message: raw,
    }
  } catch {
    return { ts: '', message: raw }
  }
}

const columns = [
  {
    key: 'ts',
    label: 'Timestamp',
    headerClass: 'pl-4 py-2 text-xs w-40',
    rowClass: 'pl-4 py-2 text-xs w-40 font-mono text-muted-foreground whitespace-nowrap',
    formatter: formatDate,
  },
  {
    key: 'worker',
    label: 'Worker',
    headerClass: 'py-2 text-xs w-24',
    rowClass: 'py-2 text-xs w-24 font-mono whitespace-nowrap',
    filter: true,
    hostnameFilter: true,
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
  <LogViewer
    hostname="*"
    :container="containerName"
    :parseMessage="parseMessage"
    :columns="columns"
  />
</template>

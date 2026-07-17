<script setup lang="ts">
import {
  Table,
  TableBody,
  TableCell,
  TableEmpty,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import TaskLogsButton from './TaskLogsButton.vue'
import { type QueueTask, type CompletedTask } from '@/services/api'
import { formatDate } from '@/lib/utils'

const props = withDefaults(
  defineProps<{
    rows: (QueueTask | CompletedTask)[]
    emptyLabel: string
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
  <Table>
    <TableHeader class="bg-muted/50">
      <TableRow>
        <TableHead>Task ID</TableHead>
        <TableHead>Name</TableHead>
        <TableHead>Scopes</TableHead>
        <TableHead>Executor</TableHead>
        <template v-if="props.showTimestamps">
          <TableHead>Status</TableHead>
          <TableHead>Created</TableHead>
          <TableHead>Finished</TableHead>
        </template>
        <TableHead>Params</TableHead>
      </TableRow>
    </TableHeader>
    <TableBody>
      <TableEmpty v-if="!props.rows.length" :colspan="props.showTimestamps ? 8 : 5">
        No {{ props.emptyLabel }} tasks
      </TableEmpty>
      <TableRow v-for="t in props.rows" :key="t.task_id" class="align-top">
        <TableCell><TaskLogsButton :taskId="t.task_id" /></TableCell>
        <TableCell class="text-xs">{{ t.name }}</TableCell>
        <TableCell class="max-w-[12rem] font-mono text-xs break-all whitespace-pre-wrap">{{
          t.scopes.join(', ')
        }}</TableCell>
        <TableCell class="text-xs">{{ t.executor }}</TableCell>
        <template v-if="props.showTimestamps">
          <TableCell>
            <Badge
              :variant="statusVariant(t.status)"
              :class="{ warning: t.status === 'cancelled' }"
              >{{ t.status }}</Badge
            >
          </TableCell>
          <TableCell class="text-xs whitespace-nowrap">{{
            formatDate(asCompleted(t).created_at)
          }}</TableCell>
          <TableCell class="text-xs whitespace-nowrap">
            {{
              asCompleted(t).finished_at ? formatDate(asCompleted(t).finished_at as string) : '—'
            }}
          </TableCell>
        </template>
        <TableCell class="max-w-xs font-mono text-xs break-all whitespace-pre-wrap">{{
          JSON.stringify(t.params)
        }}</TableCell>
      </TableRow>
    </TableBody>
  </Table>
</template>

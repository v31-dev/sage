<script setup lang="ts">
import { computed } from 'vue'
import {
  Table,
  TableBody,
  TableCell,
  TableEmpty,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import StateIndicator from '@/components/StateIndicator.vue'
import KeyValueLines from './KeyValueLines.vue'
import TaskLogsButton from './TaskLogsButton.vue'
import { taskTimes, type TaskSection } from './times'
import { type QueueTask, type FinishedTask } from '@/services/api'
import { useAppStore } from '@/stores/app'

const appStore = useAppStore()

const props = defineProps<{
  rows: (QueueTask | FinishedTask)[]
  section: TaskSection
}>()

const finished = computed(() => props.section === 'finished')
</script>

<template>
  <Table>
    <TableHeader class="bg-muted/50">
      <TableRow>
        <TableHead>Task ID</TableHead>
        <TableHead>Name</TableHead>
        <TableHead>Labels</TableHead>
        <TableHead>Scopes</TableHead>
        <TableHead>Executor</TableHead>
        <TableHead v-if="finished">Status</TableHead>
        <TableHead>Times</TableHead>
        <TableHead>Params</TableHead>
      </TableRow>
    </TableHeader>
    <TableBody>
      <TableEmpty v-if="!props.rows.length" :colspan="7 + (finished ? 1 : 0)">
        No {{ props.section }} tasks
      </TableEmpty>
      <TableRow v-for="t in props.rows" :key="t.task_id" class="align-top">
        <TableCell><TaskLogsButton :task="t" /></TableCell>
        <TableCell class="text-xs">{{ t.name }}</TableCell>
        <TableCell class="max-w-xs text-xs whitespace-normal break-words">
          <KeyValueLines :entries="t.labels" />
        </TableCell>
        <TableCell class="max-w-[12rem] font-mono text-xs break-all whitespace-normal" data-scopes>
          <div v-for="scope in t.scopes" :key="scope">{{ scope }}</div>
        </TableCell>
        <TableCell class="text-xs">{{ t.executor }}</TableCell>
        <TableCell v-if="finished">
          <StateIndicator v-bind="appStore.taskIndicator(t.status)" show-label />
        </TableCell>
        <TableCell class="text-xs whitespace-nowrap" data-times>
          <div v-for="time in taskTimes(t, props.section)" :key="time.label">
            <span class="text-muted-foreground">{{ time.label }}:</span> {{ time.value }}
          </div>
        </TableCell>
        <TableCell class="max-w-xs text-xs whitespace-normal break-all" data-params>
          <KeyValueLines :entries="t.params" />
        </TableCell>
      </TableRow>
    </TableBody>
  </Table>
</template>

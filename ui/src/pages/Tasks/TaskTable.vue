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
import TaskLogsButton from './TaskLogsButton.vue'
import { TIME_LABELS, taskTimes, type TaskSection } from './times'
import { type QueueTask, type FinishedTask } from '@/services/api'
import { useAppStore } from '@/stores/app'

const appStore = useAppStore()

const props = defineProps<{
  rows: (QueueTask | FinishedTask)[]
  section: TaskSection
}>()

const labels = computed(() => TIME_LABELS[props.section])
const finished = computed(() => props.section === 'finished')
</script>

<template>
  <Table>
    <TableHeader class="bg-muted/50">
      <TableRow>
        <TableHead>Task ID</TableHead>
        <TableHead>Name</TableHead>
        <TableHead>Scopes</TableHead>
        <TableHead>Executor</TableHead>
        <TableHead v-if="finished">Status</TableHead>
        <TableHead v-for="label in labels" :key="label">{{ label }}</TableHead>
        <TableHead>Params</TableHead>
      </TableRow>
    </TableHeader>
    <TableBody>
      <TableEmpty v-if="!props.rows.length" :colspan="5 + labels.length + (finished ? 1 : 0)">
        No {{ props.section }} tasks
      </TableEmpty>
      <TableRow v-for="t in props.rows" :key="t.task_id" class="align-top">
        <TableCell><TaskLogsButton :task="t" /></TableCell>
        <TableCell class="text-xs">{{ t.name }}</TableCell>
        <TableCell class="max-w-[12rem] font-mono text-xs break-all whitespace-pre-wrap">{{
          t.scopes.join(', ')
        }}</TableCell>
        <TableCell class="text-xs">{{ t.executor }}</TableCell>
        <TableCell v-if="finished">
          <StateIndicator v-bind="appStore.taskIndicator(t.status)" show-label />
        </TableCell>
        <TableCell
          v-for="time in taskTimes(t, props.section)"
          :key="time.label"
          class="text-xs whitespace-nowrap"
          >{{ time.value }}</TableCell
        >
        <TableCell class="max-w-xs font-mono text-xs break-all whitespace-pre-wrap">{{
          JSON.stringify(t.params)
        }}</TableCell>
      </TableRow>
    </TableBody>
  </Table>
</template>

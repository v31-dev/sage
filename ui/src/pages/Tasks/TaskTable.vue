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
import { rowTimes, type TaskSection } from './times'
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
        <TableHead>Task</TableHead>
        <TableHead>Name</TableHead>
        <TableHead>Executor</TableHead>
        <TableHead>Scopes</TableHead>
        <TableHead>Labels</TableHead>
        <TableHead>Times</TableHead>
      </TableRow>
    </TableHeader>
    <TableBody>
      <TableEmpty v-if="!props.rows.length" :colspan="6">No {{ props.section }} tasks</TableEmpty>
      <TableRow v-for="t in props.rows" :key="t.task_id" class="align-top">
        <TableCell>
          <div class="flex items-center gap-1.5">
            <StateIndicator v-if="finished" v-bind="appStore.taskIndicator(t.status)" />
            <TaskLogsButton :task="t" :section="props.section" />
          </div>
        </TableCell>
        <TableCell class="font-mono text-xs">{{ t.name }}</TableCell>
        <TableCell class="text-xs">{{ t.executor }}</TableCell>
        <TableCell class="max-w-[12rem] font-mono text-xs break-all whitespace-normal" data-scopes>
          <div v-for="scope in t.scopes" :key="scope">{{ scope }}</div>
        </TableCell>
        <TableCell class="max-w-xs text-xs whitespace-normal break-words" data-labels>
          <KeyValueLines :entries="t.labels" />
        </TableCell>
        <TableCell class="text-xs whitespace-nowrap" data-times>
          <dl class="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-2">
            <template v-for="time in rowTimes(t, props.section).dates" :key="time.label">
              <dt class="text-muted-foreground">{{ time.label }}</dt>
              <dd>{{ time.value }}</dd>
            </template>
            <dd
              v-if="rowTimes(t, props.section).durations.length"
              class="col-span-2"
              data-durations
            >
              <span v-for="(time, index) in rowTimes(t, props.section).durations" :key="time.label">
                {{ index ? ' · ' : '' }}<span class="text-muted-foreground">{{ time.label }}</span>
                {{ time.value }}
              </span>
            </dd>
          </dl>
        </TableCell>
      </TableRow>
    </TableBody>
  </Table>
</template>

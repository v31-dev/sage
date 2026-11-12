<script setup lang="ts">
import { ref, computed, useAttrs } from 'vue'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'

defineOptions({ inheritAttrs: false })
import { Field, FieldGroup, FieldLabel, FieldSet } from '@/components/ui/field'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'

import TaskLogs from '@/components/TaskLogs.vue'
import { formatDate } from '@/lib/utils'
import { type Container, type ContainerEvent, OPERATION_LABELS } from '@/services/api'
import { Logs } from 'lucide-vue-next'

interface Props {
  container: Container
}

const props = withDefaults(defineProps<Props>(), {})
const attrs = useAttrs()

const isEventLogsDialogOpen = ref(false)
const selectedRun = ref('')

const runOf = (event: ContainerEvent) => `${event.task_id}/${event.part_id ?? ''}`

// Each run of an operation whose task holds its lines, by its newest event (the events come newest first).
const runs = computed(() => {
  const newest = new Map<string, ContainerEvent>()
  for (const event of props.container.events) {
    if (event.task_id && !newest.has(runOf(event))) newest.set(runOf(event), event)
  }
  return [...newest.values()]
})
const selectedEvent = computed(() => runs.value.find(event => runOf(event) === selectedRun.value))

function label(event: ContainerEvent) {
  const action = OPERATION_LABELS[event.action as keyof typeof OPERATION_LABELS] ?? event.action
  return `${formatDate(event.created_at)} - ${action} ${event.transition}`
}

function openEventLogsDialog() {
  selectedRun.value = runs.value[0] ? runOf(runs.value[0]) : ''
  isEventLogsDialogOpen.value = true
}

function closeEventLogsDialog() {
  isEventLogsDialogOpen.value = false
}
</script>

<template>
  <Dialog v-model:open="isEventLogsDialogOpen" :key="props.container.id">
    <DialogTrigger asChild>
      <Button v-bind="attrs" variant="outline" size="sm" @click="openEventLogsDialog">
        <Logs />
        Event Logs
      </Button>
    </DialogTrigger>
    <DialogContent class="sm:max-w-6xl">
      <DialogHeader>
        <DialogTitle>Event Logs</DialogTitle>
        <DialogDescription
          >Event logs for worker {{ props.container.worker.hostname }}</DialogDescription
        >
      </DialogHeader>
      <FieldSet>
        <FieldGroup>
          <Field />
          <Field>
            <FieldLabel for="event-select"> Select Event </FieldLabel>
            <Select v-model="selectedRun" :disabled="selectedRun === ''">
              <SelectTrigger id="event-select">
                <SelectValue placeholder="No events available" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem v-for="event in runs" :key="runOf(event)" :value="runOf(event)">
                  {{ label(event) }}
                </SelectItem>
              </SelectContent>
            </Select>
          </Field>
        </FieldGroup>
      </FieldSet>
      <div class="flex flex-col max-h-[60vh]">
        <TaskLogs
          v-if="selectedEvent?.task_id"
          :key="runOf(selectedEvent)"
          :taskId="selectedEvent.task_id"
          :subTaskId="selectedEvent.part_id ?? undefined"
        />
      </div>
      <DialogFooter>
        <Button size="sm" type="button" variant="outline" @click="closeEventLogsDialog">
          Close
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>

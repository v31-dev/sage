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
import { type Container } from '@/services/api'
import { Logs } from 'lucide-vue-next'

interface Props {
  container: Container
}

const props = withDefaults(defineProps<Props>(), {})
const attrs = useAttrs()

const isEventLogsDialogOpen = ref(false)
const selectedEventId = ref('')
const events = computed(() => [...props.container.events].reverse())
const selectedEvent = computed(() =>
  props.container.events.find(event => event.container_task_id === selectedEventId.value)
)

function openEventLogsDialog(container: Container) {
  selectedEventId.value = ''
  if (container.events && container.events.length > 0) {
    const lastItemIndex = container.events.length - 1
    selectedEventId.value = container.events[lastItemIndex]!.container_task_id
  }
  isEventLogsDialogOpen.value = true
}

function closeEventLogsDialog() {
  isEventLogsDialogOpen.value = false
}
</script>

<template>
  <Dialog v-model:open="isEventLogsDialogOpen" :key="props.container.worker.hostname">
    <DialogTrigger asChild>
      <Button
        v-bind="attrs"
        variant="outline"
        size="sm"
        @click="openEventLogsDialog(props.container)"
      >
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
            <Select v-model="selectedEventId" :disabled="selectedEventId === ''">
              <SelectTrigger id="event-select">
                <SelectValue placeholder="No events available" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem
                  v-for="event in events"
                  :key="event.container_task_id"
                  :value="event.container_task_id"
                >
                  {{ formatDate(event.created_at) }} - {{ event.type }} -
                  {{ event.container_task_id }}
                </SelectItem>
              </SelectContent>
            </Select>
          </Field>
        </FieldGroup>
      </FieldSet>
      <div class="flex flex-col max-h-[60vh]">
        <TaskLogs
          v-if="selectedEvent"
          :key="selectedEvent.container_task_id"
          :taskId="selectedEvent.application_task_id"
          :subTaskId="selectedEvent.container_task_id"
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

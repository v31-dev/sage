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

import { parseManagerLog } from '@/lib/logs'
import { useAppStore } from '@/stores/app'
import LogViewer from '@/components/LogViewer.vue'
import { formatDate, levelClass } from '@/lib/utils'
import { type Container } from '@/services/api'
import { Logs } from 'lucide-vue-next'

interface Props {
  container: Container
}

const props = withDefaults(defineProps<Props>(), {})
const attrs = useAttrs()

const appStore = useAppStore()
const isEventLogsDialogOpen = ref(false)
const selectedEventId = ref('')
const events = computed(() => [...props.container.events].reverse())

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
    key: 'message',
    label: 'Message',
    headerClass: 'py-2 text-xs',
    rowClass: 'py-2 text-xs font-mono break-all whitespace-pre-wrap',
  },
]

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

// Stop polling when not deploying or stopping
const eventLogsPollCondition = computed(() =>
  ['deploying', 'stopping'].includes(appStore.applicationDeployStatus)
)
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
        <LogViewer
          v-if="selectedEventId"
          :key="selectedEventId"
          :hostname="appStore.info?.hostname ?? ''"
          container="sage"
          :search="selectedEventId"
          :parseMessage="parseManagerLog"
          :columns="columns"
          :pollInterval="2_000"
          :poll="eventLogsPollCondition"
          :pollIntervalDelayStop="5_000"
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

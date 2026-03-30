<script setup lang="ts">
import { ref, computed } from 'vue';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button';
import {
  Field,
  FieldGroup,
  FieldLabel,
  FieldSet,
} from '@/components/ui/field';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

import { useAppStore } from '@/stores/app';
import LogViewer from '@/components/LogViewer.vue'
import { formatDate, levelClass } from '@/lib/utils'
import {
  type Container
} from '@/services/api'
import { Logs } from 'lucide-vue-next';


interface Props {
  container: Container
}

const props = withDefaults(defineProps<Props>(), {})

const appStore = useAppStore()
const isDeploymentLogsDialogOpen = ref(false)
const selectedDeploymentId = ref('')
const deployments = computed(() => [...props.container.deployments].reverse())

const LOG_RE = /^\[([^\]]+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+\[([^\]]*)\]\s*([\s\S]*)$/

function parseMessage(raw: string): { [key: string]: any; ts: string; message: string } {
  const m = raw.match(LOG_RE)
  if (!m) return { ts: '', level: '', message: raw }
  return {
    ts: m[1]?.split(',')[0]?.trim() ?? '',
    level: m[2]?.trim() ?? '',
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
    key: 'message',
    label: 'Message',
    headerClass: 'py-2 text-xs',
    rowClass: "py-2 text-xs font-mono break-all whitespace-pre-wrap"
  },
]

function openDeploymentLogsDialog(container: Container) {
  selectedDeploymentId.value = ''
  if (container.deployments && container.deployments.length > 0) {
    const lastItemIndex = container.deployments.length - 1
    selectedDeploymentId.value = container.deployments[lastItemIndex]!.container_task_id
  }
  isDeploymentLogsDialogOpen.value = true
}

function closeDeploymentLogsDialog() {
  isDeploymentLogsDialogOpen.value = false
}

// Stop polling when not deploying or stopping
const deploymentLogsPollCondiiton = computed(() => [
  'deploying',
  'stopping'
].includes(appStore.applicationDeployStatus))
</script>

<template>
  <Dialog v-model:open="isDeploymentLogsDialogOpen" :key="props.container.worker.hostname">
    <DialogTrigger asChild>
      <Button class="w-full" variant="outline" size="sm" @click="openDeploymentLogsDialog(props.container)">
        <Logs />
        Deployment Logs
      </Button>
    </DialogTrigger>
    <DialogContent class="sm:max-w-6xl">
      <DialogHeader>
        <DialogTitle>Deployment Logs</DialogTitle>
        <DialogDescription>Deployment logs for worker {{ props.container.worker.hostname }}</DialogDescription>
      </DialogHeader>
      <FieldSet>
        <FieldGroup>
          <Field />
          <Field>
            <FieldLabel for="deployment-select">
              Select Deployment
            </FieldLabel>
            <Select v-model="selectedDeploymentId" :disabled="selectedDeploymentId === ''">
              <SelectTrigger id="deployment-select">
                <SelectValue placeholder="No deployments available" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem v-for="deployment in deployments" :key="deployment.container_task_id"
                  :value="deployment.container_task_id">
                  {{ formatDate(deployment.created_at) }} - {{ deployment.type }} - {{ deployment.container_task_id }}
                </SelectItem>
              </SelectContent>
            </Select>
          </Field>
        </FieldGroup>
      </FieldSet>
      <div class="flex flex-col max-h-[60vh]">
        <LogViewer v-if="selectedDeploymentId" :key="selectedDeploymentId" :hostname="appStore.info?.hostname ?? ''"
          container="sage" :search="selectedDeploymentId" :parseMessage="parseMessage" :columns="columns"
          :pollInterval="2_000" :poll="deploymentLogsPollCondiiton" :pollIntervalDelayStop="5_000" />
      </div>
      <DialogFooter>
        <Button size="sm" type="button" variant="outline" @click="closeDeploymentLogsDialog">
          Close
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
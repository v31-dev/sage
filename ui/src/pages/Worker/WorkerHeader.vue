<script setup lang="ts">
import { useRouter } from 'vue-router'
import { toast } from 'vue-sonner'
import { Activity, RotateCw, Trash } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'
import { ButtonGroup } from '@/components/ui/button-group'
import {
  Card,
  CardAction,
  CardContent,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Label } from '@/components/ui/label'

import ConfirmationButton from '@/components/ConfirmationButton.vue'
import { workersAPI, type Worker, type WorkerMetricsResponse } from '@/services/api'
import TitleStatus from '@/components/TitleStatus.vue'

interface Props {
  worker: Worker
  workerMetrics: WorkerMetricsResponse
}

const router = useRouter()
const props = withDefaults(defineProps<Props>(), {})

function goToMetrics() {
  router.push(`/workers/${props.worker.hostname}/metrics`)
}

async function onConfirmDelete() {
  await workersAPI.delete(props.worker.hostname)
  toast.success(`Worker ${props.worker.hostname} deleted successfully`)
  router.push('/workers')
}

async function onConfirmRestart() {
  await workersAPI.action(`${props.worker.hostname}/reboot`)
  toast.success(`Worker ${props.worker.hostname} reboot triggered`)
}
</script>

<template>
  <Card>
    <CardHeader class="border-b">
      <CardTitle>
        <TitleStatus
          :title="props.worker.hostname"
          :status="props.worker.online ? 'success' : 'error'"
          :statusText="props.worker.online ? 'online' : 'offline'"
          :size="3"
        />
      </CardTitle>
      <CardAction>
        <ButtonGroup class="space-x-1">
          <ConfirmationButton
            triggerText="Restart"
            title="Restart Worker"
            body="Are you sure you want to restart this worker? It will reboot and briefly go offline."
            :description="props.worker.hostname"
            :icon="RotateCw"
            :onConfirm="onConfirmRestart"
          />
          <ConfirmationButton
            triggerText="Delete"
            title="Delete Worker"
            body="Are you sure you want to delete this worker? This action cannot be undone."
            :description="props.worker.hostname"
            :icon="Trash"
            destructive
            :onConfirm="onConfirmDelete"
          />
        </ButtonGroup>
      </CardAction>
    </CardHeader>

    <CardContent class="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-5">
      <div>
        <Label>IP</Label>
        <p class="text-sm text-muted-foreground font-mono break-all">
          {{ props.workerMetrics.meta.ip || props.worker.ip }}
        </p>
      </div>

      <div>
        <Label>CPU Cores</Label>
        <p class="text-sm text-muted-foreground">
          {{ props.workerMetrics.meta.cpu_cores }}
        </p>
      </div>

      <div>
        <Label>Memory</Label>
        <p class="text-sm text-muted-foreground">{{ props.workerMetrics.meta.mem_total_mb }} MB</p>
      </div>

      <div>
        <Label>Disk</Label>
        <p class="text-sm text-muted-foreground">{{ props.workerMetrics.meta.disk_total_gb }} GB</p>
      </div>

      <div>
        <Label>Application Containers</Label>
        <p class="text-sm text-muted-foreground">
          {{ props.worker.containers.length }}
        </p>
      </div>
    </CardContent>

    <CardFooter class="border-t flex flex-col md:flex-row justify-end items-center gap-2 md:gap-0">
      <ButtonGroup class="space-x-1 w-full md:w-auto flex">
        <Button size="sm" class="flex-1 md:flex-initial" variant="outline" @click="goToMetrics">
          <Activity />Metrics
        </Button>
      </ButtonGroup>
    </CardFooter>
  </Card>
</template>

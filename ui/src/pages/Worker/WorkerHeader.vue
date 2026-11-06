<script setup lang="ts">
import { computed, ref, watch } from 'vue'
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
import { FieldGroup } from '@/components/ui/field'
import { InputGroup, InputGroupAddon, InputGroupInput } from '@/components/ui/input-group'
import { Label } from '@/components/ui/label'

import ConfirmationButton from '@/components/ConfirmationButton.vue'
import CopyButton from '@/components/CopyButton.vue'
import SettingField from '@/components/SettingField.vue'
import { formatDate } from '@/lib/utils'
import {
  deleteWorker,
  rebootWorker,
  type Indicator,
  type TunnelStatus,
  type Worker,
  type WorkerMetricsResponse,
  type WorkerStatus,
} from '@/services/api'
import SectionTitle from '@/components/SectionTitle.vue'
import StateIndicator from '@/components/StateIndicator.vue'
import { useAppStore } from '@/stores/app'
import EditWorkerButton from './EditWorkerButton.vue'

interface Props {
  worker: Worker
  workerMetrics: WorkerMetricsResponse
  loadWorkerStatus: () => Promise<void>
}

const router = useRouter()
const appStore = useAppStore()

const props = withDefaults(defineProps<Props>(), {})

// Shown from the moment Restart is confirmed (undone if the manager refuses) until the next push replaces `worker`
// with the manager's answer.
const requested = ref<WorkerStatus | null>(null)
watch(
  () => props.worker,
  () => (requested.value = null)
)
const status = computed(() => requested.value ?? props.worker.status)
const restartable = computed(() => status.value === 'online' || status.value === 'not ready')
const deleteBody = computed(() =>
  status.value === 'offline'
    ? 'This worker is offline, so Sage cannot clean it up. It is removed from Sage together with its containers; delete the VM yourself if it is not deleted already.'
    : 'Are you sure you want to delete this worker? Sage stops its services and removes its files from the VM; the VM itself is not deleted. Delete its containers first.'
)

const STACK_DESCRIPTIONS: Record<string, string> = {
  traefik: "Routes this worker's names to its containers.",
  vector: "Sends Traefik's and the applications' logs to the manager.",
  telegraf: "Sends the worker's metrics and its containers' state to the manager.",
  cloudflared: "This worker's own tunnel, which carries its public names.",
  'cloudflared-discovery': "The platform's tunnel, run on every worker, for discovery.",
}

interface StackDetail {
  indicator?: Indicator
  value: string
  copy?: string
}

const certificates = computed(() => [
  {
    id: 'worker',
    label: 'Worker certificate',
    description: "For this worker's internal and TCP names.",
    state: props.worker.certificate,
  },
  {
    id: 'discovery',
    label: 'Discovery certificate',
    description: 'For internal discovery, shared by all workers.',
    state: props.worker.discovery_certificate,
  },
])

const stack = computed(() => {
  const details = appStore.info?.settings?.cloudflare.details
  const tunnel = (name: string, status: TunnelStatus | null): StackDetail => ({
    indicator: status ? appStore.tunnelIndicator(status) : undefined,
    value: status ? `${name} · ${status}` : name,
    copy: name,
  })
  const carried: Record<string, StackDetail | undefined> = {
    cloudflared: tunnel(props.worker.tunnel_name, props.worker.tunnel),
    'cloudflared-discovery': details
      ? tunnel(details.tunnel_name, details.tunnel_status)
      : undefined,
  }
  return (props.worker.infra ?? []).map(container => ({
    container,
    detail: carried[container.name],
  }))
})

function goToMetrics() {
  router.push(`/workers/${props.worker.hostname}/metrics`)
}

async function onConfirmDelete() {
  await deleteWorker(props.worker.hostname)
  toast.success(`Worker ${props.worker.hostname} deleted successfully`)
  router.push('/workers')
}

async function onConfirmRestart() {
  requested.value = 'rebooting'
  try {
    await rebootWorker(props.worker.hostname)
  } catch (err) {
    requested.value = null
    throw err
  }
  toast.success(`Worker ${props.worker.hostname} reboot triggered`)
}
</script>

<template>
  <Card>
    <CardHeader class="border-b flex flex-col gap-3 md:flex-row md:justify-between">
      <CardTitle>
        <SectionTitle :title="props.worker.hostname" :size="3">
          <StateIndicator v-bind="appStore.workerIndicator(status)" show-label />
          <StateIndicator
            v-if="props.worker.path"
            class="ml-2"
            v-bind="appStore.pathIndicator(props.worker.path)"
            show-label
          />
        </SectionTitle>
      </CardTitle>
      <CardAction>
        <ButtonGroup class="space-x-1">
          <EditWorkerButton :worker="props.worker" :loadWorkerStatus="props.loadWorkerStatus" />
          <ConfirmationButton
            triggerText="Restart"
            title="Restart Worker"
            body="Are you sure you want to restart this worker? It will reboot and briefly go offline."
            :description="props.worker.hostname"
            :icon="RotateCw"
            :disabled="!restartable"
            :onConfirm="onConfirmRestart"
          />
          <ConfirmationButton
            triggerText="Delete"
            title="Delete Worker"
            :body="deleteBody"
            :description="props.worker.hostname"
            :icon="Trash"
            destructive
            :disabled="status === 'removing' || status === 'adding'"
            :onConfirm="onConfirmDelete"
          />
        </ButtonGroup>
      </CardAction>
    </CardHeader>

    <CardContent class="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-7">
      <div>
        <Label>IP</Label>
        <p class="text-sm text-muted-foreground font-mono break-all">
          {{ props.workerMetrics.meta.ip || props.worker.ip }}
        </p>
      </div>

      <div>
        <Label>Subdomain</Label>
        <p class="text-sm text-muted-foreground font-mono">{{ props.worker.subdomain ?? '-' }}</p>
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

      <div class="md:col-span-2 lg:col-span-7">
        <Label>Certificates</Label>
        <FieldGroup class="mt-3 gap-5">
          <SettingField
            v-for="certificate in certificates"
            :key="certificate.id"
            :id="`certificate-${certificate.id}`"
            :label="certificate.label"
            :description="certificate.description"
            :error="certificate.state?.error ?? undefined"
            :data-certificate="certificate.id"
          >
            <div class="flex flex-col items-start gap-2">
              <template v-if="certificate.state">
                <StateIndicator
                  v-bind="appStore.certificateIndicator(certificate.state)"
                  show-label
                />
                <p
                  v-if="
                    certificate.state.covered &&
                    certificate.state.synced &&
                    certificate.state.expiry
                  "
                  class="text-sm text-muted-foreground"
                >
                  Valid until {{ formatDate(certificate.state.expiry) }}
                </p>
              </template>
              <p v-else class="text-sm text-muted-foreground">No certificate status yet.</p>
            </div>
          </SettingField>
        </FieldGroup>
      </div>

      <div class="md:col-span-2 lg:col-span-7">
        <Label>Stack</Label>
        <FieldGroup v-if="props.worker.infra" class="mt-3 gap-5">
          <SettingField
            v-for="{ container, detail } in stack"
            :key="container.name"
            :id="`stack-${container.name}`"
            :label="container.name"
            :description="STACK_DESCRIPTIONS[container.name]"
            data-stack
          >
            <div class="flex flex-col items-start gap-2">
              <StateIndicator
                v-bind="{ ...appStore.infraIndicator(container), label: container.status }"
                show-label
              />
              <InputGroup v-if="detail">
                <InputGroupAddon v-if="detail.indicator">
                  <StateIndicator v-bind="detail.indicator" />
                </InputGroupAddon>
                <InputGroupInput
                  :id="`stack-${container.name}`"
                  :model-value="detail.value"
                  readonly
                />
                <InputGroupAddon v-if="detail.copy" align="inline-end">
                  <CopyButton compact :text="detail.copy" />
                </InputGroupAddon>
              </InputGroup>
            </div>
          </SettingField>
        </FieldGroup>
        <p v-else class="text-sm text-muted-foreground">No current report from the worker.</p>
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

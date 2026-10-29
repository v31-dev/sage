<script setup lang="ts">
import { useRouter } from 'vue-router'
import { computed } from 'vue'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Label } from '@/components/ui/label'

import { type WorkerListItem, type WorkerMetricSummary } from '@/services/api'
import PercentageBar from '@/components/PercentageBar.vue'
import SectionTitle from '@/components/SectionTitle.vue'
import StateIndicator from '@/components/StateIndicator.vue'
import { useAppStore } from '@/stores/app'
import { TONES } from '@/lib/tone'

interface Props {
  worker: WorkerListItem
}

const router = useRouter()
const appStore = useAppStore()

const props = withDefaults(defineProps<Props>(), {})

const mbps = (kbps: number | null | undefined) =>
  kbps == null ? null : Math.round((kbps / 1000) * 10) / 10

// The worker's newest minute of metrics, from the worker list; nothing while it has none that is recent.
const metrics = computed<WorkerMetricSummary>(() => {
  const minute = props.worker.metrics
  return {
    cpu: minute?.cpu_pct ?? null,
    cores: minute?.cpu_cores ?? null,
    mem: minute?.mem_used_mb ?? null,
    memTotal: minute?.mem_total_mb ?? null,
    disk: minute?.disk_used_gb ?? null,
    diskTotal: minute?.disk_total_gb ?? null,
    netRx: mbps(minute?.net_rx_kbps),
    netTx: mbps(minute?.net_tx_kbps),
  }
})

function goToMetrics(hostname: string) {
  router.push(`/workers/${hostname}`)
}
</script>

<template>
  <Card
    class="flex h-full cursor-pointer flex-col transition-shadow hover:shadow-lg"
    @click="goToMetrics(props.worker.hostname)"
  >
    <CardHeader class="border-b">
      <CardTitle>
        <SectionTitle :title="props.worker.hostname">
          <StateIndicator v-bind="appStore.workerIndicator(props.worker.status)" show-label />
          <StateIndicator
            v-if="props.worker.path"
            class="ml-2"
            v-bind="appStore.pathIndicator(props.worker.path)"
            show-label
          />
        </SectionTitle>
      </CardTitle>
    </CardHeader>

    <CardContent class="space-y-4">
      <div>
        <Label>IP</Label>
        <p class="text-sm text-muted-foreground font-mono break-all">
          {{ props.worker.ip }}
        </p>
      </div>

      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div class="space-y-2">
          <Label>CPU</Label>
          <div v-if="metrics.cpu !== null" class="space-y-1">
            <PercentageBar :value="metrics.cpu" />
            <p class="text-xs text-muted-foreground">{{ metrics.cores }} cores</p>
          </div>
          <p v-else class="text-sm text-muted-foreground">-</p>
        </div>

        <div class="space-y-2">
          <Label>Memory</Label>
          <div v-if="metrics.mem !== null && metrics.memTotal !== null && metrics.memTotal > 0">
            <PercentageBar :value="(metrics.mem / metrics.memTotal) * 100" />
            <p class="text-xs text-muted-foreground">
              {{ Math.round(metrics.mem * 10) / 10 }} / {{ metrics.memTotal }} MB
            </p>
          </div>
          <p v-else class="text-sm text-muted-foreground">-</p>
        </div>

        <div class="space-y-2">
          <Label>Disk</Label>
          <div
            v-if="metrics.disk !== null && metrics.diskTotal !== null && metrics.diskTotal > 0"
            class="space-y-1"
          >
            <PercentageBar :value="(metrics.disk / metrics.diskTotal) * 100" />
            <p class="text-xs text-muted-foreground">
              {{ Math.round(metrics.disk * 10) / 10 }} / {{ metrics.diskTotal }} GB
            </p>
          </div>
          <p v-else class="text-sm text-muted-foreground">-</p>
        </div>

        <div class="space-y-2">
          <Label>Network</Label>
          <div class="space-y-0.5 text-sm">
            <p class="font-mono text-muted-foreground">
              <span :class="TONES.info.text">↓</span>
              {{ metrics.netRx !== null ? metrics.netRx : '-' }} Mbps
            </p>
            <p class="font-mono text-muted-foreground">
              <span class="text-orange-600 dark:text-orange-400">↑</span>
              {{ metrics.netTx !== null ? metrics.netTx : '-' }} Mbps
            </p>
          </div>
        </div>
      </div>

      <div>
        <Label>Containers</Label>
        <p class="text-sm text-muted-foreground">
          {{ props.worker.container_count ?? '-' }}
        </p>
      </div>
    </CardContent>
  </Card>
</template>

<script setup lang="ts">
import { useRouter } from 'vue-router'
import { computed } from 'vue'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Label } from '@/components/ui/label'

import { type Worker, type WorkerMetricSummary } from '@/services/api'
import PercentageBar from '@/components/PercentageBar.vue'
import TitleStatus from '@/components/TitleStatus.vue'

interface Props {
  worker: Worker
  metrics?: WorkerMetricSummary
}

const router = useRouter()
const props = withDefaults(defineProps<Props>(), {})

const emptyMetrics: WorkerMetricSummary = {
  cpu: null,
  cores: null,
  mem: null,
  memTotal: null,
  disk: null,
  diskTotal: null,
  netRx: null,
  netTx: null,
  containers: null,
}

const metrics = computed(() => props.metrics ?? emptyMetrics)

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
        <TitleStatus
          :title="props.worker.hostname"
          :status="props.worker.online ? 'success' : 'error'"
          :statusText="props.worker.online ? 'online' : 'offline'"
        />
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
              <span class="text-blue-600 dark:text-blue-400">↓</span>
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
          {{ metrics.containers !== null ? metrics.containers : '-' }}
        </p>
      </div>
    </CardContent>
  </Card>
</template>

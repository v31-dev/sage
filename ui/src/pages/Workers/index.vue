<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted, watch } from 'vue'

import { Card, CardContent } from '@/components/ui/card'
import { Spinner } from '@/components/ui/spinner'

import {
  fetchWorkerMetrics,
  METRICS_POLL_MS,
  type WorkerMetricsResponse,
  type WorkerMetricSummary,
} from '@/services/api'
import { useAppStore } from '@/stores/app'
import AddWorkerButton from './AddWorkerButton.vue'
import WorkerCard from './WorkerCard.vue'

// The workers are the app store's: it follows the manager's `workers` topic for the whole app.
const appStore = useAppStore()
const workers = computed(() => appStore.workers ?? [])
const isLoading = computed(() => appStore.workers === null && !appStore.workersError)
const workerMetrics = ref<Record<string, WorkerMetricSummary>>({})
let pollInterval: ReturnType<typeof setInterval> | null = null

function summarizeWorkerMetrics(workerData: WorkerMetricsResponse): WorkerMetricSummary {
  const latest = [...workerData.host].reverse().find(row => row.cpu_pct !== null)

  return {
    cpu: latest?.cpu_pct ?? null,
    cores: workerData.meta.cpu_cores,
    mem: latest?.mem_used_mb ?? null,
    memTotal: workerData.meta.mem_total_mb,
    disk: latest?.disk_used_gb ?? null,
    diskTotal: workerData.meta.disk_total_gb,
    netRx:
      latest && latest.net_rx_kbps !== null
        ? Math.round((latest.net_rx_kbps / 1000) * 10) / 10
        : null,
    netTx:
      latest && latest.net_tx_kbps !== null
        ? Math.round((latest.net_tx_kbps / 1000) * 10) / 10
        : null,
  }
}

async function loadWorkerMetrics() {
  if (workers.value.length === 0) {
    workerMetrics.value = {}
    return
  }

  const metricsEntries = await Promise.all(
    workers.value.map(async worker => {
      try {
        const workerData = await fetchWorkerMetrics(worker.hostname, '1m')
        return [worker.hostname, summarizeWorkerMetrics(workerData)] as const
      } catch (err) {
        console.error('Failed to fetch worker metrics for', worker.hostname, err)
        return [
          worker.hostname,
          {
            cpu: null,
            cores: null,
            mem: null,
            memTotal: null,
            disk: null,
            diskTotal: null,
            netRx: null,
            netTx: null,
          },
        ] as const
      }
    })
  )

  workerMetrics.value = Object.fromEntries(metricsEntries)
}

// The metrics of the workers are asked for when there is a worker more or less, and every minute.
watch(() => workers.value.map(worker => worker.hostname).join(','), loadWorkerMetrics, {
  immediate: true,
})

onMounted(() => {
  pollInterval = setInterval(loadWorkerMetrics, METRICS_POLL_MS)
})

onUnmounted(() => {
  if (pollInterval) clearInterval(pollInterval)
})
</script>

<template>
  <main class="flex-1 px-2 sm:px-4 py-4 sm:py-4 relative">
    <div class="max-w-7xl mx-auto">
      <!-- Loading State -->
      <div v-if="isLoading" class="absolute inset-0 z-50 flex items-center justify-center">
        <div class="flex flex-col items-center gap-4">
          <Spinner />
          <p class="text-sm text-muted-foreground">Loading workers...</p>
        </div>
      </div>

      <!-- Content -->
      <div v-else class="space-y-6">
        <div class="flex justify-end">
          <AddWorkerButton />
        </div>

        <Card v-if="appStore.workersError">
          <CardContent class="flex flex-col items-center justify-center py-12">
            <p class="text-muted-foreground text-lg">Failed to load workers</p>
            <p class="text-sm text-muted-foreground">{{ appStore.workersError }}</p>
          </CardContent>
        </Card>

        <!-- Empty State -->
        <Card v-else-if="workers.length === 0">
          <CardContent class="flex flex-col items-center justify-center py-12">
            <p class="text-muted-foreground text-lg">No workers found</p>
            <p class="text-sm text-muted-foreground">
              Add a machine of your tailnet to get started.
            </p>
          </CardContent>
        </Card>

        <!-- Workers Grid -->
        <div v-else class="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          <WorkerCard
            v-for="worker in workers"
            :key="worker.hostname"
            :worker="worker"
            :metrics="workerMetrics[worker.hostname]"
          />
        </div>
      </div>
    </div>
  </main>
</template>

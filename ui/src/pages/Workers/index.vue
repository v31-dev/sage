<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'

import { Card, CardContent } from '@/components/ui/card'
import { Spinner } from '@/components/ui/spinner'

import {
  fetchWorkerMetrics,
  workersAPI,
  type Worker,
  type WorkerMetricsResponse,
  type WorkerMetricSummary,
} from '@/services/api'
import WorkerCard from './WorkerCard.vue'

const workers = ref<Worker[]>([])
const workerMetrics = ref<Record<string, WorkerMetricSummary>>({})
const isLoading = ref(true)
const loadError = ref('')
let pollInterval: ReturnType<typeof setInterval> | null = null

function summarizeWorkerMetrics(workerData: WorkerMetricsResponse): WorkerMetricSummary {
  const latest = workerData.host[workerData.host.length - 1]

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
    containers: Object.keys(workerData.containers ?? {}).length,
  }
}

async function loadWorkers() {
  try {
    loadError.value = ''
    workers.value = (await workersAPI.fetchAll()) as Worker[]
  } catch (err) {
    workers.value = []
    loadError.value = err instanceof Error ? err.message : 'Failed to load workers'
  } finally {
    isLoading.value = false
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
            containers: null,
          },
        ] as const
      }
    })
  )

  workerMetrics.value = Object.fromEntries(metricsEntries)
}

onMounted(async () => {
  await loadWorkers()
  await loadWorkerMetrics()
  pollInterval = setInterval(loadWorkerMetrics, 60_000)
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
        <Card v-if="loadError">
          <CardContent class="flex flex-col items-center justify-center py-12">
            <p class="text-muted-foreground text-lg">Failed to load workers</p>
            <p class="text-sm text-muted-foreground">{{ loadError }}</p>
          </CardContent>
        </Card>

        <!-- Empty State -->
        <Card v-else-if="workers.length === 0">
          <CardContent class="flex flex-col items-center justify-center py-12">
            <p class="text-muted-foreground text-lg">No workers found</p>
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

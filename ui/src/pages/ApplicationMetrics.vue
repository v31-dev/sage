<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Card, CardHeader, CardTitle } from '@/components/ui/card'
import { Spinner } from '@/components/ui/spinner'

import { type ContainerMetricsPoint, type MetricsPeriod } from '@/services/api'
import MetricChart from '@/components/MetricChart.vue'
import { processContainerData } from '@/lib/metrics'
import { getApplicationAPI } from '@/services/api'

const route = useRoute()
const projectName = route.params.projectId as string
const appName = route.params.appId as string
const applicationAPI = getApplicationAPI(projectName)

const period = ref<MetricsPeriod>('1h')
const periods: MetricsPeriod[] = ['1h', '24h', '1w']
const data = ref<Array<Array<ContainerMetricsPoint>>>([])
const isLoading = ref(true)
const loadError = ref('')
let pollInterval: ReturnType<typeof setInterval> | null = null

async function load() {
  try {
    loadError.value = ''
    const nextData: Array<Array<ContainerMetricsPoint>> = []
    const containersData = (await applicationAPI.action(
      `${appName}/metrics?period=${period.value}`
    )) as Array<{ hostname: string; metrics: Array<ContainerMetricsPoint> }>
    for (const containerData of containersData) {
      for (const point of containerData.metrics) {
        point.name = `${point.name} ${containerData.hostname}`
      }
      nextData.push(containerData.metrics)
    }
    data.value = nextData
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : 'Failed to load metrics'
  } finally {
    isLoading.value = false
  }
}

watch(period, () => {
  isLoading.value = true
  load()
})

onMounted(async () => {
  await load()
  pollInterval = setInterval(load, 60_000)
})
onUnmounted(() => {
  if (pollInterval) clearInterval(pollInterval)
})

const containerChartData = computed(() =>
  processContainerData((data.value ?? []) as Array<Array<ContainerMetricsPoint>>)
)
</script>

<template>
  <main class="flex-1 px-2 sm:px-4 py-6 relative">
    <div class="mx-auto space-y-6 max-w-7xl">
      <!-- Loading State -->
      <div v-if="isLoading" class="absolute inset-0 z-50 flex items-center justify-center">
        <div class="flex flex-col items-center gap-4">
          <Spinner />
          <p class="text-sm text-muted-foreground">Loading metrics...</p>
        </div>
      </div>

      <Card v-else-if="loadError && data.length === 0">
        <CardHeader>
          <CardTitle class="text-lg">Failed to load metrics</CardTitle>
          <p class="text-sm text-muted-foreground">{{ loadError }}</p>
        </CardHeader>
      </Card>

      <!-- Content -->
      <div v-else class="space-y-6">
        <!-- Header Card -->
        <Card>
          <CardHeader>
            <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-4">
              <div>
                <CardTitle class="text-lg">{{ appName }}</CardTitle>
              </div>
              <div class="flex gap-1 rounded-md border p-1 text-sm">
                <button
                  v-for="p in periods"
                  :key="p"
                  @click="period = p"
                  :class="[
                    'px-3 py-1 rounded transition-colors whitespace-nowrap',
                    period === p ? 'bg-primary text-primary-foreground' : 'hover:bg-muted',
                  ]"
                >
                  {{ p }}
                </button>
              </div>
            </div>
          </CardHeader>
        </Card>

        <!-- Charts Grid -->
        <div class="grid grid-cols-1 gap-4">
          <MetricChart
            title="CPU Usage"
            type="line"
            :data="
              containerChartData.data.map(d => {
                const { date, ...rest } = d
                return {
                  date,
                  ...Object.fromEntries(
                    Object.entries(rest).filter(
                      ([k]) => k.endsWith('_cpu_pct') || k.endsWith('_cpu_pct_label')
                    )
                  ),
                }
              })
            "
            :yMax="100"
            unit="%"
            :height="'300px'"
            :series="
              containerChartData.colors.map(({ name, color }) => ({
                key: `${name}_cpu_pct`,
                tooltip_label: `${name}_cpu_pct_label`,
                name,
                color,
              }))
            "
          />

          <MetricChart
            title="Memory Usage"
            type="line"
            :data="
              containerChartData.data.map(d => {
                const { date, ...rest } = d
                return {
                  date,
                  ...Object.fromEntries(
                    Object.entries(rest).filter(
                      ([k]) => k.endsWith('_mem_used_mb') || k.endsWith('_mem_used_mb_label')
                    )
                  ),
                }
              })
            "
            :yMax="containerChartData.memMax"
            unit="MB"
            :height="'300px'"
            :series="
              containerChartData.colors.map(({ name, color }) => ({
                key: `${name}_mem_used_mb`,
                tooltip_label: `${name}_mem_used_mb_label`,
                name,
                color,
              }))
            "
          />

          <MetricChart
            title="Network"
            type="line"
            :data="
              containerChartData.data.map(d => {
                const { date, ...rest } = d
                return {
                  date,
                  ...Object.fromEntries(
                    Object.entries(rest).filter(
                      ([k]) =>
                        k.endsWith('_net_rx') ||
                        k.endsWith('_net_rx_label') ||
                        k.endsWith('_net_tx') ||
                        k.endsWith('_net_tx_label')
                    )
                  ),
                }
              })
            "
            :yMax="containerChartData.netMax"
            unit="Mbps"
            :height="'300px'"
            :series="[
              ...containerChartData.colors.map(({ name, color }) => ({
                key: `${name}_net_rx`,
                tooltip_label: `${name}_net_rx_label`,
                name: `${name} Download`,
                color,
              })),
              ...containerChartData.colors.map(({ name, color }) => ({
                key: `${name}_net_tx`,
                tooltip_label: `${name}_net_tx_label`,
                name: `${name} Upload`,
                color,
              })),
            ]"
          />
        </div>
      </div>
    </div>
  </main>
</template>

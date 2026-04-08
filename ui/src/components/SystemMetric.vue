<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { Card, CardHeader, CardTitle } from '@/components/ui/card'
import { Spinner } from '@/components/ui/spinner'

import { 
  fetchWorkerMetrics, 
  type WorkerMetricsResponse, 
  type MetricsPeriod,
  type ContainerMetricsPoint
} from '@/services/api'
import MetricChart from '@/components/MetricChart.vue'
import { processContainerData } from '@/lib/metrics'


const props = defineProps<{ hostname: string }>()

const period = ref<MetricsPeriod>('1h')
const periods: MetricsPeriod[] = ['1h', '24h', '1w']
const data = ref<WorkerMetricsResponse | null>(null)
const isLoading = ref(true)
let pollInterval: ReturnType<typeof setInterval> | null = null

async function load() {
  try {
    data.value = await fetchWorkerMetrics(props.hostname, period.value)
  } catch { }
  finally { isLoading.value = false }
}

watch(period, () => { isLoading.value = true; load() })

onMounted(async () => {
  await load()
  pollInterval = setInterval(load, 60_000)
})
onUnmounted(() => { if (pollInterval) clearInterval(pollInterval) })

const chartData = computed(() => {
  return (data.value?.host ?? []).map(point => {
    const date = new Date(point.ts)
    const cpu = point.cpu_pct
    const mem = point.mem_used_mb
    const cached = point.mem_cached_mb
    const disk = point.disk_used_gb
    const net_rx_mbps = point.net_rx_kbps !== null ? Math.round((point.net_rx_kbps / 1000) * 10) / 10 : null
    const net_tx_mbps = point.net_tx_kbps !== null ? Math.round((point.net_tx_kbps / 1000) * 10) / 10 : null
    const load_1 = point.load_avg_1m !== null ? Math.round(point.load_avg_1m * 100) / 100 : null
    const load_5 = point.load_avg_5m !== null ? Math.round(point.load_avg_5m * 100) / 100 : null
    const load_15 = point.load_avg_15m !== null ? Math.round(point.load_avg_15m * 100) / 100 : null

    return {
      date,
      cpu_pct: cpu,
      cpu_pct_label: cpu !== null ? `${cpu}%` : 'N/A',
      mem_used: mem,
      mem_used_label: mem !== null ? `${mem} MB` : 'N/A',
      mem_cached: cached,
      mem_cached_label: cached !== null ? `${cached} MB` : 'N/A',
      disk_gb: disk,
      disk_gb_label: disk !== null ? `${Math.round(disk * 10) / 10} GB` : 'N/A',
      net_rx: net_rx_mbps,
      net_rx_label: net_rx_mbps !== null ? `${net_rx_mbps} Mbps` : 'N/A',
      net_tx: net_tx_mbps,
      net_tx_label: net_tx_mbps !== null ? `${net_tx_mbps} Mbps` : 'N/A',
      load_1,
      load_1_label: load_1 !== null ? `${load_1}` : 'N/A',
      load_5,
      load_5_label: load_5 !== null ? `${load_5}` : 'N/A',
      load_15,
      load_15_label: load_15 !== null ? `${load_15}` : 'N/A',
    }
  })
})

const containerChartData = computed(() => processContainerData((data.value?.containers ?? []) as Array<Array<ContainerMetricsPoint>>))
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

      <!-- Content -->
      <div v-else class="space-y-6">
        <!-- Header Card -->
        <Card>
          <CardHeader>
            <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-4">
              <div>
                <CardTitle class="text-lg">{{ hostname }}</CardTitle>
                <p class="text-sm text-muted-foreground mt-1">
                  {{ data?.meta?.ip }} • {{ data?.meta?.cpu_cores }} cores • {{ data?.meta?.mem_total_mb }} MB RAM • {{
                    data?.meta?.disk_total_gb }} GB Disk
                </p>
              </div>
              <div class="flex gap-1 rounded-md border p-1 text-sm">
                <button v-for="p in periods" :key="p" @click="period = p"
                  :class="['px-3 py-1 rounded transition-colors whitespace-nowrap', period === p ? 'bg-primary text-primary-foreground' : 'hover:bg-muted']">{{
                    p }}</button>
              </div>
            </div>
          </CardHeader>
        </Card>

        <!-- Charts Grid -->
        <template v-if="data?.host.length">
          <div class="grid grid-cols-1 gap-4">
            <MetricChart title="CPU Usage" type="area"
              :data="chartData.map(({ date, cpu_pct, cpu_pct_label }) => ({ date, cpu_pct, cpu_pct_label }))"
              :yMax="100" unit="%" :height="'300px'"
              :series="[{ key: 'cpu_pct', tooltip_label: 'cpu_pct_label', name: 'CPU', color: '#3b82f6' }]" />

            <MetricChart title="Memory Usage" type="area"
              :data="chartData.map(({ date, mem_used, mem_used_label, mem_cached, mem_cached_label }) => ({ date, mem_used, mem_used_label, mem_cached, mem_cached_label }))"
              :yMax="data.meta.mem_total_mb" unit="MB" :height="'300px'" :series="[
                { key: 'mem_cached', tooltip_label: 'mem_cached_label', name: 'Cached', color: '#047857' },
                { key: 'mem_used', tooltip_label: 'mem_used_label', name: 'Used', color: '#10b981' }
              ]" />

            <MetricChart title="Load Average" type="area"
              :data="chartData.map(({ date, load_1, load_1_label, load_5, load_5_label, load_15, load_15_label }) => ({ date, load_1, load_1_label, load_5, load_5_label, load_15, load_15_label }))"
              :yMax="data.meta.cpu_cores" unit="" :height="'300px'" :series="[
                { key: 'load_1', tooltip_label: 'load_1_label', name: '1min', color: '#ef4444' },
                { key: 'load_5', tooltip_label: 'load_5_label', name: '5min', color: '#f59e0b' },
                { key: 'load_15', tooltip_label: 'load_15_label', name: '15min', color: '#10b981' }
              ]" />

            <MetricChart title="Network" type="area"
              :data="chartData.map(({ date, net_rx, net_rx_label, net_tx, net_tx_label }) => ({ date, net_rx, net_rx_label, net_tx, net_tx_label }))"
              unit="Mbps" :yMax="Math.max(...chartData.map(d => Math.max(d.net_rx ?? 0, d.net_tx ?? 0)), 1)"
              :height="'300px'" :series="[
                { key: 'net_rx', tooltip_label: 'net_rx_label', name: 'Download', color: '#a855f7' },
                { key: 'net_tx', tooltip_label: 'net_tx_label', name: 'Upload', color: '#d946ef' }
              ]" />

            <MetricChart title="Disk Usage" type="area"
              :data="chartData.map(({ date, disk_gb, disk_gb_label }) => ({ date, disk_gb, disk_gb_label }))"
              :yMax="data.meta.disk_total_gb" unit="GB" :height="'300px'"
              :series="[{ key: 'disk_gb', tooltip_label: 'disk_gb_label', name: 'Used', color: '#06b6d4' }]" />

            <MetricChart title="Containers (CPU Usage)" type="line" :data="containerChartData.data.map(d => {
              const { date, ...rest } = d
              return { date, ...Object.fromEntries(Object.entries(rest).filter(([k]) => k.endsWith('_cpu_pct') || k.endsWith('_cpu_pct_label'))) }
            })" :yMax="100" unit="%" :height="'300px'"
              :series="containerChartData.colors.map(({ name, color }) => ({ key: `${name}_cpu_pct`, tooltip_label: `${name}_cpu_pct_label`, name, color }))" />

            <MetricChart title="Containers (Memory Usage)" type="line" :data="containerChartData.data.map(d => {
              const { date, ...rest } = d
              return { date, ...Object.fromEntries(Object.entries(rest).filter(([k]) => k.endsWith('_mem_used_mb') || k.endsWith('_mem_used_mb_label'))) }
            })" :yMax="containerChartData.memMax" unit="MB" :height="'300px'"
              :series="containerChartData.colors.map(({ name, color }) => ({ key: `${name}_mem_used_mb`, tooltip_label: `${name}_mem_used_mb_label`, name, color }))" />

            <MetricChart title="Containers (Network)" type="line" :data="containerChartData.data.map(d => {
              const { date, ...rest } = d
              return { date, ...Object.fromEntries(Object.entries(rest).filter(([k]) => k.endsWith('_net_rx') || k.endsWith('_net_rx_label') || k.endsWith('_net_tx') || k.endsWith('_net_tx_label'))) }
            })" :yMax="containerChartData.netMax" unit="Mbps" :height="'300px'"
              :series="[...containerChartData.colors.map(({ name, color }) => ({ key: `${name}_net_rx`, tooltip_label: `${name}_net_rx_label`, name: `${name} Download`, color })), ...containerChartData.colors.map(({ name, color }) => ({ key: `${name}_net_tx`, tooltip_label: `${name}_net_tx_label`, name: `${name} Upload`, color }))]" />
          </div>
        </template>

        <!-- Empty State -->
        <div v-else class="text-center text-muted-foreground py-16">No metrics collected yet</div>
      </div>
    </div>
  </main>
</template>
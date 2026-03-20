<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { fetchWorkerMetrics, type WorkerMetricsResponse, type MetricsPeriod } from '@/services/api'
import MetricChart from '@/components/MetricChart.vue'

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

const colors = [
  '#3b82f6', '#ef4444', '#10b981', '#f59e0b', '#8b5cf6',
  '#ec4899', '#06b6d4', '#14b8a6', '#f97316', '#6366f1',
  '#84cc16', '#eab308', '#a855f7', '#d946ef', '#0891b2',
  '#059669', '#dc2626', '#ea580c', '#7c3aed', '#db2777'
]

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

const containerMeta = computed<Array<{ name: string; color: string }>>(() => {
  const names = new Set<string>()
  const containerArrays = (data.value?.containers ?? []) as Array<Array<{ name: string }>>
  for (const containerArray of containerArrays) {
    if (containerArray.length > 0 && containerArray[0] && containerArray[0].name) {
        names.add(containerArray[0].name)
    }
  }
  
  return Array.from(names).map((name, index) => {
    const color = colors[index % colors.length]!
    return { name, color }
  })
})

const containerChartData = computed(() => {
  const containerArrays = (data.value?.containers ?? []) as Array<Array<{ ts: string; name: string; date?: Date; cpu_pct: number | null; mem_used_mb: number | null; net_rx_kbps: number | null; net_tx_kbps: number | null }>>
  const result = [] as Record<string, any>[]

  for (const containerArray of containerArrays) {
    for (const point of containerArray) {
      const name = point.name
      const date = new Date(point.ts)
      const cpu = point.cpu_pct
      const mem = point.mem_used_mb
      const net_rx_mbps = point.net_rx_kbps !== null ? Math.round((point.net_rx_kbps / 1000) * 10) / 10 : null
      const net_tx_mbps = point.net_tx_kbps !== null ? Math.round((point.net_tx_kbps / 1000) * 10) / 10 : null

      const pointData = {
        [`${name}_cpu_pct`]: cpu,
        [`${name}_cpu_pct_label`]: cpu !== null ? `${cpu}%` : 'N/A',
        [`${name}_mem_used_mb`]: mem,
        [`${name}_mem_used_mb_label`]: mem !== null ? `${mem} MB` : 'N/A',
        [`${name}_net_rx`]: net_rx_mbps,
        [`${name}_net_rx_label`]: net_rx_mbps !== null ? `${net_rx_mbps} Mbps` : 'N/A',
        [`${name}_net_tx`]: net_tx_mbps,
        [`${name}_net_tx_label`]: net_tx_mbps !== null ? `${net_tx_mbps} Mbps` : 'N/A',
      }

      const record = result.find((r: any) => r.date.getTime() === date.getTime()) 

      if (record) {
        for (const [key, value] of Object.entries(pointData)) {
          record[key] = value
        }
      } else {
         result.push({ date, ...pointData })
      }
    }
  }

  return result
})

const containerMemMax = computed(() => {
  return Math.max(0, ...containerChartData.value.map(point => {
    let max = 0
    for (const key in point) {
      if (key.endsWith('_mem_used_mb')) {
        max = Math.max(max, point[key] || 0)
      }
    }
    return max
  }))
})

const containerNetMax = computed(() => {
  return Math.max(0, ...containerChartData.value.map(point => {
    let max = 0
    for (const key in point) {
      if (key.endsWith('_net_rx') || key.endsWith('_net_tx')) {
        max = Math.max(max, point[key] || 0)
      }
    }
    return max
  }))
})
</script>

<template>
  <main class="flex-1 px-2 sm:px-4 py-6">
    <div class="mx-auto space-y-6 max-w-7xl">

      <!-- Header Card -->
      <Card>
        <CardHeader>
          <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-4">
            <div>
              <CardTitle class="text-2xl">{{ hostname }}</CardTitle>
              <p class="text-sm text-muted-foreground mt-1">
                {{ data?.meta?.ip }} • {{ data?.meta?.cpu_cores }} cores • {{ data?.meta?.mem_total_mb }} MB RAM • {{ data?.meta?.disk_total_gb }} GB Disk
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

      <!-- Loading State -->
      <div v-if="isLoading" class="grid grid-cols-1 gap-4">
        <Card v-for="i in 6" :key="i" class="animate-pulse">
          <CardContent class="pt-6">
            <div class="h-64 bg-muted rounded"></div>
          </CardContent>
        </Card>
      </div>

      <!-- Charts Grid -->
      <template v-else-if="data?.host.length">
        <div class="grid grid-cols-1 gap-4">
          <MetricChart title="CPU Usage" type="area"
            :data="chartData.map(({ date, cpu_pct, cpu_pct_label }) => ({ date, cpu_pct, cpu_pct_label }))"
            :yMax="100" unit="%"
            :height="'300px'"
            :series="[{ key: 'cpu_pct', tooltip_label: 'cpu_pct_label', name: 'CPU', color: '#3b82f6' }]" />

          <MetricChart title="Memory Usage" type="area"
            :data="chartData.map(({ date, mem_used, mem_used_label, mem_cached, mem_cached_label }) => ({ date, mem_used, mem_used_label, mem_cached, mem_cached_label }))"
            :yMax="data.meta.mem_total_mb" unit="MB"
            :height="'300px'"
            :series="[
              { key: 'mem_cached', tooltip_label: 'mem_cached_label', name: 'Cached', color: '#047857' },
              { key: 'mem_used', tooltip_label: 'mem_used_label', name: 'Used', color: '#10b981' }
            ]" />

          <MetricChart title="Load Average" type="area"
            :data="chartData.map(({ date, load_1, load_1_label, load_5, load_5_label, load_15, load_15_label }) => ({ date, load_1, load_1_label, load_5, load_5_label, load_15, load_15_label }))"
            :yMax="data.meta.cpu_cores" unit=""
            :height="'300px'"
            :series="[
              { key: 'load_1', tooltip_label: 'load_1_label', name: '1min', color: '#ef4444' },
              { key: 'load_5', tooltip_label: 'load_5_label', name: '5min', color: '#f59e0b' },
              { key: 'load_15', tooltip_label: 'load_15_label', name: '15min', color: '#10b981' }
            ]" />

          <MetricChart title="Network" type="area"
            :data="chartData.map(({ date, net_rx, net_rx_label, net_tx, net_tx_label }) => ({ date, net_rx, net_rx_label, net_tx, net_tx_label }))"
            unit="Mbps"
            :yMax="Math.max(...chartData.map(d => Math.max(d.net_rx ?? 0, d.net_tx ?? 0)), 1)"
            :height="'300px'"
            :series="[
              { key: 'net_rx', tooltip_label: 'net_rx_label', name: 'Download', color: '#a855f7' },
              { key: 'net_tx', tooltip_label: 'net_tx_label', name: 'Upload', color: '#d946ef' }
            ]" />

          <MetricChart title="Disk Usage" type="area"
            :data="chartData.map(({ date, disk_gb, disk_gb_label }) => ({ date, disk_gb, disk_gb_label }))"
            :yMax="data.meta.disk_total_gb" unit="GB"
            :height="'300px'"
            :series="[{ key: 'disk_gb', tooltip_label: 'disk_gb_label', name: 'Used', color: '#06b6d4' }]" />

          <MetricChart title="Containers (CPU Usage)" type="line"
            :data="containerChartData.map(d => {
              const { date, ...rest } = d
              return { date, ...Object.fromEntries(Object.entries(rest).filter(([k]) => k.endsWith('_cpu_pct') || k.endsWith('_cpu_pct_label'))) }
            })"
            :yMax="100" unit="%"
            :height="'300px'"
            :series="containerMeta.map(({ name, color }) => ({ key: `${name}_cpu_pct`, tooltip_label: `${name}_cpu_pct_label`, name, color }))" />

          <MetricChart title="Containers (Memory Usage)" type="line"
            :data="containerChartData.map(d => {
              const { date, ...rest } = d
              return { date, ...Object.fromEntries(Object.entries(rest).filter(([k]) => k.endsWith('_mem_used_mb') || k.endsWith('_mem_used_mb_label'))) }
            })"
            :yMax="containerMemMax" unit="MB"
            :height="'300px'"
            :series="containerMeta.map(({ name, color }) => ({ key: `${name}_mem_used_mb`, tooltip_label: `${name}_mem_used_mb_label`, name, color }))" />

          <MetricChart title="Containers (Network)" type="line"
            :data="containerChartData.map(d => {
              const { date, ...rest } = d
              return { date, ...Object.fromEntries(Object.entries(rest).filter(([k]) => k.endsWith('_net_rx') || k.endsWith('_net_rx_label') || k.endsWith('_net_tx') || k.endsWith('_net_tx_label'))) }
            })"
            :yMax="containerNetMax" unit="Mbps"
            :height="'300px'"
            :series="[...containerMeta.map(({ name, color }) => ({ key: `${name}_net_rx`, tooltip_label: `${name}_net_rx_label`, name: `${name} Download`, color })), ...containerMeta.map(({ name, color }) => ({ key: `${name}_net_tx`, tooltip_label: `${name}_net_tx_label`, name: `${name} Upload`, color }))]" />
        </div>
      </template>

      <!-- Empty State -->
      <div v-else class="text-center text-muted-foreground py-16">No metrics collected yet</div>
    </div>
  </main>
</template>

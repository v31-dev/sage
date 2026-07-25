<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import {
  Boxes,
  CircleX,
  Globe,
  HardDriveDownload,
  Layers3,
  Rocket,
  Server,
  ShieldAlert,
  TriangleAlert,
} from 'lucide-vue-next'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Item,
  ItemActions,
  ItemContent,
  ItemDescription,
  ItemMedia,
  ItemTitle,
} from '@/components/ui/item'
import { Progress } from '@/components/ui/progress'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Spinner } from '@/components/ui/spinner'
import MetricChart from '@/components/MetricChart.vue'
import { fetchHomeSummary, type HomeSummary, type Notification } from '@/services/api'
import { formatDateStringAgo } from '@/lib/utils'
import { useAppStore } from '@/stores/app'

const HOUR_MS = 3_600_000
const ERROR_COLOR = '#ef4444'
const WARNING_COLOR = '#f59e0b'
const OK_CHIP = 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400'
const BUSY_CHIP = 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400'

const appStore = useAppStore()
const summary = ref<HomeSummary | null>(null)
const isLoading = ref(true)
const loadError = ref('')
let pollInterval: ReturnType<typeof setInterval> | null = null

const timelineSeries = [
  { key: 'errors', tooltip_label: 'errors_label', name: 'Errors', color: ERROR_COLOR },
  { key: 'warnings', tooltip_label: 'warnings_label', name: 'Warnings', color: WARNING_COLOR },
]

const events = computed(() => summary.value?.critical_events_last_24h ?? [])

const appsInTransition = computed(() => {
  const s = summary.value
  if (!s) return 0
  return (
    s.applications_deploying +
    s.applications_stopping +
    s.applications_backup +
    s.applications_restoring
  )
})

const containersInTransition = computed(() => {
  const s = summary.value
  if (!s) return 0
  return (
    s.containers_deploying + s.containers_stopping + s.containers_backup + s.containers_restoring
  )
})

// Clock-aligned local hour buckets, oldest first, empty hours included
const timelineData = computed(() => {
  const now = new Date()
  const latestHour = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate(),
    now.getHours()
  ).getTime()

  const buckets = new Map<number, { errors: number; warnings: number }>()
  for (let i = 23; i >= 0; i--) {
    buckets.set(latestHour - i * HOUR_MS, { errors: 0, warnings: 0 })
  }

  for (const event of events.value) {
    const ts = parseTimestamp(event.created_at)
    const bucket = buckets.get(
      new Date(ts.getFullYear(), ts.getMonth(), ts.getDate(), ts.getHours()).getTime()
    )
    if (!bucket) continue
    if (event.type === 'error') bucket.errors += 1
    else if (event.type === 'warning') bucket.warnings += 1
  }

  return [...buckets.entries()].map(([hour, counts]) => ({
    date: new Date(hour),
    errors: counts.errors,
    errors_label: String(counts.errors),
    warnings: counts.warnings,
    warnings_label: String(counts.warnings),
  }))
})

const timelineMax = computed(() =>
  Math.max(1, ...timelineData.value.map(point => point.errors + point.warnings))
)

async function loadSummary() {
  try {
    loadError.value = ''
    summary.value = await fetchHomeSummary()
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : 'Failed to load home summary'
  } finally {
    isLoading.value = false
  }
}

// Stored naive UTC, so it needs the zone marker before parsing
function parseTimestamp(value: string) {
  return new Date(value.endsWith('Z') ? value : `${value}Z`)
}

function ratio(part: number, total: number) {
  return total > 0 ? (part / total) * 100 : 0
}

function healthBarClass(errorCount: number, transitionCount: number) {
  if (errorCount > 0) return '[&>div]:bg-red-500'
  if (transitionCount > 0) return '[&>div]:bg-amber-500'
  return '[&>div]:bg-emerald-500'
}

function eventIconClass(notificationType: Notification['type']) {
  return notificationType === 'error'
    ? 'bg-red-500/10 text-red-600 dark:text-red-400'
    : 'bg-amber-500/10 text-amber-600 dark:text-amber-400'
}

onMounted(async () => {
  await loadSummary()
  pollInterval = setInterval(loadSummary, 60_000)
})

onUnmounted(() => {
  if (pollInterval) clearInterval(pollInterval)
})
</script>

<template>
  <main class="flex-1 px-4 py-6 relative">
    <div class="max-w-7xl mx-auto">
      <div v-if="isLoading" class="absolute inset-0 z-50 flex items-center justify-center">
        <div class="flex flex-col items-center gap-4">
          <Spinner />
          <p class="text-sm text-muted-foreground">Loading system summary...</p>
        </div>
      </div>

      <div v-else class="space-y-4">
        <Card v-if="loadError && !summary">
          <CardContent class="flex flex-col items-center justify-center py-12">
            <p class="text-muted-foreground text-lg">Failed to load home summary</p>
            <p class="text-sm text-muted-foreground">{{ loadError }}</p>
          </CardContent>
        </Card>

        <Card v-else-if="!summary">
          <CardContent class="flex flex-col items-center justify-center py-12">
            <p class="text-muted-foreground text-lg">No system summary available</p>
          </CardContent>
        </Card>

        <template v-else-if="summary">
          <!-- Header -->
          <Card>
            <CardHeader>
              <CardTitle class="text-2xl sm:text-3xl">System overview</CardTitle>
              <CardDescription>
                <div class="flex items-center gap-2 text-sm text-muted-foreground">
                  <Server class="size-4" />
                  <span>{{ appStore.info?.hostname ?? 'manager' }}</span>
                  <span v-if="appStore.info?.ip">• {{ appStore.info.ip }}</span>
                </div>
              </CardDescription>
            </CardHeader>
          </Card>

          <!-- Summary cards grid -->
          <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <!-- Workers -->
            <RouterLink to="/workers" class="block">
              <Card class="h-full gap-4 transition-colors hover:border-primary/40">
                <CardHeader class="flex flex-row items-center justify-between">
                  <CardTitle class="text-sm font-medium text-muted-foreground">Workers</CardTitle>
                  <Server class="size-4 text-muted-foreground" />
                </CardHeader>
                <CardContent class="space-y-3">
                  <div class="flex items-baseline gap-2">
                    <span class="text-3xl font-semibold tabular-nums">
                      {{ summary.workers_online }}
                    </span>
                    <span class="text-sm text-muted-foreground">
                      of {{ summary.workers_total }} online
                    </span>
                  </div>
                  <Progress
                    class="h-1.5 bg-muted"
                    :class="healthBarClass(summary.workers_offline, 0)"
                    :model-value="ratio(summary.workers_online, summary.workers_total)"
                  />
                  <div class="flex flex-wrap gap-1.5">
                    <Badge v-if="summary.workers_offline" variant="destructive">
                      {{ summary.workers_offline }} offline
                    </Badge>
                    <Badge v-else variant="outline" :class="OK_CHIP">All online</Badge>
                  </div>
                </CardContent>
              </Card>
            </RouterLink>

            <!-- Projects & Apps -->
            <RouterLink to="/projects" class="block">
              <Card class="h-full gap-4 transition-colors hover:border-primary/40">
                <CardHeader class="flex flex-row items-center justify-between">
                  <CardTitle class="text-sm font-medium text-muted-foreground"
                    >Projects &amp; Apps</CardTitle
                  >
                  <Layers3 class="size-4 text-muted-foreground" />
                </CardHeader>
                <CardContent class="space-y-3">
                  <div class="flex items-baseline gap-2">
                    <span class="text-3xl font-semibold tabular-nums">
                      {{ summary.applications_total }}
                    </span>
                    <span class="text-sm text-muted-foreground">
                      apps in {{ summary.projects_total }} projects
                    </span>
                  </div>
                  <Progress
                    class="h-1.5 bg-muted"
                    :class="healthBarClass(summary.applications_error, appsInTransition)"
                    :model-value="ratio(summary.applications_active, summary.applications_total)"
                  />
                  <div class="flex flex-wrap gap-1.5">
                    <Badge variant="outline" :class="OK_CHIP">
                      {{ summary.applications_active }} active
                    </Badge>
                    <Badge v-if="summary.applications_inactive" variant="secondary">
                      {{ summary.applications_inactive }} inactive
                    </Badge>
                    <Badge v-if="summary.applications_error" variant="destructive">
                      {{ summary.applications_error }} error
                    </Badge>
                    <Badge v-if="appsInTransition" variant="outline" :class="BUSY_CHIP">
                      {{ appsInTransition }} in transition
                    </Badge>
                  </div>
                  <div class="flex items-center gap-1.5 text-xs text-muted-foreground">
                    <Rocket class="size-3.5" />
                    <span>{{ summary.deployments_last_24h }} deployments in last 24h</span>
                  </div>
                </CardContent>
              </Card>
            </RouterLink>

            <!-- Containers -->
            <Card class="h-full gap-4">
              <CardHeader class="flex flex-row items-center justify-between">
                <CardTitle class="text-sm font-medium text-muted-foreground">Containers</CardTitle>
                <Boxes class="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent class="space-y-3">
                <div class="flex items-baseline gap-2">
                  <span class="text-3xl font-semibold tabular-nums">
                    {{ summary.containers_active }}
                  </span>
                  <span class="text-sm text-muted-foreground">
                    of {{ summary.containers_total }} running
                  </span>
                </div>
                <Progress
                  class="h-1.5 bg-muted"
                  :class="healthBarClass(summary.containers_error, containersInTransition)"
                  :model-value="ratio(summary.containers_active, summary.containers_total)"
                />
                <div class="flex flex-wrap gap-1.5">
                  <Badge v-if="summary.containers_inactive" variant="secondary">
                    {{ summary.containers_inactive }} inactive
                  </Badge>
                  <Badge v-if="summary.containers_error" variant="destructive">
                    {{ summary.containers_error }} error
                  </Badge>
                  <Badge v-if="containersInTransition" variant="outline" :class="BUSY_CHIP">
                    {{ containersInTransition }} in transition
                  </Badge>
                  <Badge
                    v-if="
                      !summary.containers_inactive &&
                      !summary.containers_error &&
                      !containersInTransition
                    "
                    variant="outline"
                    :class="OK_CHIP"
                  >
                    All running
                  </Badge>
                </div>
              </CardContent>
            </Card>

            <!-- Domains -->
            <Card class="h-full gap-4">
              <CardHeader class="flex flex-row items-center justify-between">
                <CardTitle class="text-sm font-medium text-muted-foreground">Domains</CardTitle>
                <Globe class="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent class="space-y-3">
                <div class="flex items-baseline gap-2">
                  <span class="text-3xl font-semibold tabular-nums">
                    {{ summary.domains_active }}
                  </span>
                  <span class="text-sm text-muted-foreground">
                    of {{ summary.domains_total }} synced
                  </span>
                </div>
                <Progress
                  class="h-1.5 bg-muted"
                  :class="healthBarClass(summary.domains_inactive, 0)"
                  :model-value="ratio(summary.domains_active, summary.domains_total)"
                />
                <div class="flex flex-wrap gap-1.5">
                  <Badge v-if="summary.domains_inactive" variant="destructive">
                    {{ summary.domains_inactive }} unsynced
                  </Badge>
                  <Badge v-else variant="outline" :class="OK_CHIP">All synced</Badge>
                </div>
              </CardContent>
            </Card>

            <!-- Backups -->
            <RouterLink to="/backups" class="block">
              <Card class="h-full gap-4 transition-colors hover:border-primary/40">
                <CardHeader class="flex flex-row items-center justify-between">
                  <CardTitle class="text-sm font-medium text-muted-foreground">Backups</CardTitle>
                  <HardDriveDownload class="size-4 text-muted-foreground" />
                </CardHeader>
                <CardContent class="space-y-3">
                  <div class="flex items-baseline gap-2">
                    <span class="text-3xl font-semibold tabular-nums">
                      {{ summary.backups_total }}
                    </span>
                    <span class="text-sm text-muted-foreground">
                      stored • {{ summary.backups_last_24h }} in last 24h
                    </span>
                  </div>
                  <div class="flex flex-wrap gap-1.5">
                    <Badge variant="secondary">{{ summary.backups_system }} system</Badge>
                    <Badge variant="secondary">
                      {{ summary.backups_application }} application
                    </Badge>
                  </div>
                  <div class="text-xs text-muted-foreground">
                    <span v-if="summary.latest_backup_at">
                      Last backup {{ formatDateStringAgo(summary.latest_backup_at) }}
                    </span>
                    <span v-else>No backups yet</span>
                  </div>
                </CardContent>
              </Card>
            </RouterLink>
          </div>

          <!-- Errors & warnings timeline -->
          <MetricChart
            title="Errors &amp; warnings per hour (last 24h)"
            type="bar"
            unit=""
            height="200px"
            :data="timelineData"
            :series="timelineSeries"
            :y-max="timelineMax"
          />

          <!-- Events -->
          <Card class="gap-0 pb-0">
            <CardHeader class="border-b">
              <div class="flex flex-wrap items-center justify-between gap-2">
                <div class="flex items-center gap-2">
                  <ShieldAlert class="size-4 text-muted-foreground" />
                  <CardTitle class="text-base"
                    >Errors &amp; warnings in the last 24 hours</CardTitle
                  >
                </div>
                <div class="flex flex-wrap gap-1.5">
                  <Badge variant="destructive">
                    {{ summary.critical_error_count_last_24h }} errors
                  </Badge>
                  <Badge variant="outline" :class="BUSY_CHIP">
                    {{ summary.critical_warning_count_last_24h }} warnings
                  </Badge>
                </div>
              </div>
            </CardHeader>
            <CardContent class="px-0">
              <div
                v-if="events.length === 0"
                class="flex min-h-48 items-center justify-center px-6 py-12 text-sm text-muted-foreground"
              >
                No errors or warnings recorded in the last 24 hours.
              </div>
              <ScrollArea v-else :class="events.length > 6 ? 'h-[420px]' : ''">
                <Item
                  v-for="notification in events"
                  :key="notification.id"
                  class="rounded-none border-b border-border px-6 last:border-b-0"
                >
                  <ItemMedia variant="icon" :class="eventIconClass(notification.type)">
                    <CircleX v-if="notification.type === 'error'" />
                    <TriangleAlert v-else />
                  </ItemMedia>
                  <ItemContent>
                    <ItemTitle class="w-full font-normal leading-6 whitespace-normal">
                      {{ notification.content }}
                    </ItemTitle>
                    <ItemDescription>
                      {{ formatDateStringAgo(notification.created_at) }}
                    </ItemDescription>
                  </ItemContent>
                  <ItemActions v-if="notification.link">
                    <Button
                      as="a"
                      variant="outline"
                      size="sm"
                      :href="notification.link"
                      target="_blank"
                      rel="noreferrer"
                    >
                      Open
                    </Button>
                  </ItemActions>
                </Item>
              </ScrollArea>
            </CardContent>
          </Card>
        </template>
      </div>
    </div>
  </main>
</template>

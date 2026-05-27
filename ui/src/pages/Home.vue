<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { Boxes, Globe, HardDriveDownload, Layers3, Server, ShieldAlert } from 'lucide-vue-next'

import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Spinner } from '@/components/ui/spinner'
import { fetchHomeSummary, type HomeSummary, type Notification } from '@/services/api'
import { formatDateStringAgo } from '@/lib/utils'
import { useAppStore } from '@/stores/app'

const appStore = useAppStore()
const summary = ref<HomeSummary | null>(null)
const isLoading = ref(true)
const loadError = ref('')
let pollInterval: ReturnType<typeof setInterval> | null = null

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

function notificationVariant(notificationType: Notification['type']) {
  if (notificationType === 'error') return 'destructive'
  if (notificationType === 'warning') return 'secondary'
  return 'outline'
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
            <Card>
              <CardHeader class="flex flex-row items-center justify-between pb-2">
                <CardTitle class="text-sm font-medium text-muted-foreground">Workers</CardTitle>
                <Server class="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div class="space-y-1.5">
                  <div class="flex items-center justify-between text-sm">
                    <span class="text-muted-foreground">Online</span>
                    <span class="font-medium">{{ summary.workers_online }}</span>
                  </div>
                  <div class="flex items-center justify-between text-sm">
                    <span class="text-muted-foreground">Offline</span>
                    <span class="font-medium">{{ summary.workers_offline }}</span>
                  </div>
                </div>
              </CardContent>
            </Card>

            <!-- Projects & Apps -->
            <Card>
              <CardHeader class="flex flex-row items-center justify-between pb-2">
                <CardTitle class="text-sm font-medium text-muted-foreground"
                  >Projects & Apps</CardTitle
                >
                <Layers3 class="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div class="space-y-1.5">
                  <div class="flex items-center justify-between text-sm">
                    <span class="text-muted-foreground">Projects</span>
                    <span class="font-medium">{{ summary.projects_total }}</span>
                  </div>
                  <div class="flex items-center justify-between text-sm">
                    <span class="text-muted-foreground">Apps</span>
                    <span class="font-medium">{{ summary.applications_total }}</span>
                  </div>
                  <div class="flex items-center justify-between text-sm">
                    <span class="text-muted-foreground">Deployments (24h)</span>
                    <span class="font-medium">{{ summary.deployments_last_24h }}</span>
                  </div>
                  <div class="mt-1.5 space-y-1.5 border-t pt-1.5">
                    <div class="flex items-center justify-between text-sm">
                      <span class="text-muted-foreground">Active</span>
                      <span class="font-medium">{{ summary.applications_active }}</span>
                    </div>
                    <div class="flex items-center justify-between text-sm">
                      <span class="text-muted-foreground">Inactive</span>
                      <span class="font-medium">{{ summary.applications_inactive }}</span>
                    </div>
                    <div class="flex items-center justify-between text-sm">
                      <span class="text-muted-foreground">Error</span>
                      <span class="font-medium">{{ summary.applications_error }}</span>
                    </div>
                    <div class="flex items-center justify-between text-sm">
                      <span class="text-muted-foreground">Deploying</span>
                      <span class="font-medium">{{ summary.applications_deploying }}</span>
                    </div>
                    <div class="flex items-center justify-between text-sm">
                      <span class="text-muted-foreground">Stopping</span>
                      <span class="font-medium">{{ summary.applications_stopping }}</span>
                    </div>
                    <div class="flex items-center justify-between text-sm">
                      <span class="text-muted-foreground">Backup</span>
                      <span class="font-medium">{{ summary.applications_backup }}</span>
                    </div>
                    <div class="flex items-center justify-between text-sm">
                      <span class="text-muted-foreground">Restoring</span>
                      <span class="font-medium">{{ summary.applications_restoring }}</span>
                    </div>
                  </div>
                </div>
              </CardContent>
            </Card>

            <!-- Containers -->
            <Card>
              <CardHeader class="flex flex-row items-center justify-between pb-2">
                <CardTitle class="text-sm font-medium text-muted-foreground">Containers</CardTitle>
                <Boxes class="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div class="space-y-1.5">
                  <div class="flex items-center justify-between text-sm">
                    <span class="text-muted-foreground">Active</span>
                    <span class="font-medium">{{ summary.containers_active }}</span>
                  </div>
                  <div class="flex items-center justify-between text-sm">
                    <span class="text-muted-foreground">Inactive</span>
                    <span class="font-medium">{{ summary.containers_inactive }}</span>
                  </div>
                  <div class="flex items-center justify-between text-sm">
                    <span class="text-muted-foreground">Error</span>
                    <span class="font-medium">{{ summary.containers_error }}</span>
                  </div>
                  <div class="flex items-center justify-between text-sm">
                    <span class="text-muted-foreground">Deploying</span>
                    <span class="font-medium">{{ summary.containers_deploying }}</span>
                  </div>
                  <div class="flex items-center justify-between text-sm">
                    <span class="text-muted-foreground">Stopping</span>
                    <span class="font-medium">{{ summary.containers_stopping }}</span>
                  </div>
                  <div class="flex items-center justify-between text-sm">
                    <span class="text-muted-foreground">Backup</span>
                    <span class="font-medium">{{ summary.containers_backup }}</span>
                  </div>
                  <div class="flex items-center justify-between text-sm">
                    <span class="text-muted-foreground">Restoring</span>
                    <span class="font-medium">{{ summary.containers_restoring }}</span>
                  </div>
                </div>
              </CardContent>
            </Card>

            <!-- Domains -->
            <Card>
              <CardHeader class="flex flex-row items-center justify-between pb-2">
                <CardTitle class="text-sm font-medium text-muted-foreground">Domains</CardTitle>
                <Globe class="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div class="space-y-1.5">
                  <div class="flex items-center justify-between text-sm">
                    <span class="text-muted-foreground">Synced</span>
                    <span class="font-medium">{{ summary.domains_active }}</span>
                  </div>
                  <div class="flex items-center justify-between text-sm">
                    <span class="text-muted-foreground">Unsynced</span>
                    <span class="font-medium">{{ summary.domains_inactive }}</span>
                  </div>
                </div>
              </CardContent>
            </Card>

            <!-- Backups -->
            <Card>
              <CardHeader class="flex flex-row items-center justify-between pb-2">
                <CardTitle class="text-sm font-medium text-muted-foreground">Backups</CardTitle>
                <HardDriveDownload class="size-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div class="space-y-1.5">
                  <div class="flex items-center justify-between text-sm">
                    <span class="text-muted-foreground">System</span>
                    <span class="font-medium">{{ summary.backups_system }}</span>
                  </div>
                  <div class="flex items-center justify-between text-sm">
                    <span class="text-muted-foreground">Application</span>
                    <span class="font-medium">{{ summary.backups_application }}</span>
                  </div>
                </div>
              </CardContent>
            </Card>
          </div>

          <!-- Events -->
          <Card>
            <CardHeader class="border-b">
              <div class="flex items-center gap-2">
                <ShieldAlert class="size-4 text-muted-foreground" />
                <CardTitle class="text-base">Errors &amp; warnings in the last 24 hours</CardTitle>
              </div>
            </CardHeader>
            <CardContent class="p-0">
              <div
                v-if="summary.critical_events_last_24h.length === 0"
                class="flex min-h-48 items-center justify-center px-6 py-12 text-sm text-muted-foreground"
              >
                No errors or warnings recorded in the last 24 hours.
              </div>
              <div v-else class="divide-y">
                <div
                  v-for="notification in summary.critical_events_last_24h"
                  :key="notification.id"
                  class="space-y-2 px-6 py-4"
                >
                  <div class="flex flex-wrap items-center gap-2">
                    <Badge :variant="notificationVariant(notification.type)">
                      {{ notification.type }}
                    </Badge>
                    <span class="text-sm text-muted-foreground">
                      {{ formatDateStringAgo(notification.created_at) }}
                    </span>
                  </div>
                  <p class="text-sm leading-6">{{ notification.content }}</p>
                  <a
                    v-if="notification.link"
                    :href="notification.link"
                    target="_blank"
                    rel="noreferrer"
                    class="text-sm text-primary underline-offset-4 hover:underline"
                  >
                    Open related link
                  </a>
                </div>
              </div>
            </CardContent>
          </Card>
        </template>
      </div>
    </div>
  </main>
</template>

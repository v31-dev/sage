<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/spinner'
import type { APIError } from '@/lib/api'

import {
  workersAPI,
  fetchWorkerMetrics,
  type Worker,
  type WorkerMetricsResponse,
} from '@/services/api'
import TitleStatus from '@/components/TitleStatus.vue'
import WorkerContainerCard from './WorkerContainerCard.vue'
import WorkerHeader from './WorkerHeader.vue'

const route = useRoute()
const router = useRouter()
const hostname = route.params.hostname as string

const worker = ref<Worker | null>(null)
const workerMetrics = ref<WorkerMetricsResponse | null>(null)
const isLoading = ref(true)
const loadError = ref('')
const isNotFound = ref(false)

onMounted(async () => {
  await loadWorker()
})

async function loadWorker() {
  try {
    isLoading.value = true
    loadError.value = ''
    isNotFound.value = false
    const workerData = (await workersAPI.fetchOne(hostname)) as Worker
    worker.value = workerData
    workerMetrics.value = await fetchWorkerMetrics(hostname, '1m')
  } catch (err) {
    console.error('Failed to load worker:', err)
    const status = (err as APIError).status
    if (status === 404) {
      worker.value = null
      workerMetrics.value = null
      isNotFound.value = true
    } else {
      loadError.value = err instanceof Error ? err.message : 'Failed to load worker'
    }
  } finally {
    isLoading.value = false
  }
}

function goBack() {
  router.push('/workers')
}
</script>

<template>
  <main class="flex-1 px-4 py-8 relative">
    <div class="mx-auto space-y-6 max-w-7xl">
      <div v-if="isLoading" class="absolute inset-0 z-50 flex items-center justify-center">
        <div class="flex flex-col items-center gap-4">
          <Spinner />
          <p class="text-sm text-muted-foreground">Loading worker...</p>
        </div>
      </div>

      <Card v-else-if="loadError">
        <CardContent class="flex flex-col items-center justify-center py-12">
          <p class="mb-4 text-lg text-muted-foreground">Failed to load worker</p>
          <p class="text-sm text-muted-foreground">{{ loadError }}</p>
        </CardContent>
      </Card>

      <div v-else-if="worker && workerMetrics" class="space-y-6">
        <WorkerHeader :worker="worker" :workerMetrics="workerMetrics" />

        <div class="space-y-4">
          <div class="flex items-center justify-between">
            <TitleStatus title="Application Containers" :size="4" />
          </div>

          <div v-if="worker.containers.length === 0" class="flex items-center justify-center py-8">
            <Card class="w-full">
              <CardContent class="flex flex-col items-center justify-center py-12">
                <p class="text-muted-foreground text-lg">No containers</p>
              </CardContent>
            </Card>
          </div>

          <div v-else class="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
            <WorkerContainerCard
              v-for="container in worker.containers"
              :key="`${container.application.project.name}-${container.application.name}`"
              :container="container"
              :worker="worker"
              :loadWorker="loadWorker"
            />
          </div>
        </div>
      </div>

      <Card v-else-if="isNotFound">
        <CardContent class="flex flex-col items-center justify-center py-12">
          <p class="mb-4 text-lg text-muted-foreground">Worker {{ hostname }} not found</p>
          <Button size="sm" @click="goBack" variant="outline"> Back to Workers </Button>
        </CardContent>
      </Card>
    </div>
  </main>
</template>

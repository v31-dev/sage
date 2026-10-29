<script setup lang="ts">
import { computed } from 'vue'

import { Card, CardContent } from '@/components/ui/card'
import { Spinner } from '@/components/ui/spinner'

import { useAppStore } from '@/stores/app'
import AddWorkerButton from './AddWorkerButton.vue'
import WorkerCard from './WorkerCard.vue'

// The workers are the app store's: it follows the manager's `workers` topic for the whole app, and each worker
// carries its newest minute of metrics.
const appStore = useAppStore()
const workers = computed(() => appStore.workers ?? [])
const isLoading = computed(() => appStore.workers === null && !appStore.workersError)
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
          <WorkerCard v-for="worker in workers" :key="worker.hostname" :worker="worker" />
        </div>
      </div>
    </div>
  </main>
</template>

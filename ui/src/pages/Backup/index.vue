<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { Spinner } from '@/components/ui/spinner'
import { toast } from 'vue-sonner'
import { Card, CardContent } from '@/components/ui/card'
import { watchState } from '@/lib/updates'

import BackupsItem from './BackupsItem.vue'
import { fetchBackups, type Backup } from '@/services/api'
import CreateBackupButton from './CreateBackupButton.vue'
import DiscoverBackupsButton from './DiscoverBackupsButton.vue'
import SectionTitle from '@/components/SectionTitle.vue'

const backups = ref<Backup[]>([])
const isLoading = ref(true)
const error = ref('')
let stop: (() => void) | undefined

// Newest first.
function showBackups(next: Backup[]) {
  backups.value = [...next].reverse()
}

function showLoadError(err: unknown) {
  error.value = err instanceof Error ? err.message : 'Failed to load backups'
  toast.error('Failed to load backups')
}

// After a backup is deleted here, without waiting for the push.
async function loadBackups() {
  isLoading.value = true
  error.value = ''
  try {
    showBackups(await fetchBackups())
  } catch (err) {
    showLoadError(err)
  } finally {
    isLoading.value = false
  }
}

// The backups, loaded from their route and then pushed by the manager each time a backup is written.
onMounted(async () => {
  const watch = watchState('backups', fetchBackups, showBackups)
  stop = watch.stop
  try {
    await watch.first
  } catch (err) {
    showLoadError(err)
  } finally {
    isLoading.value = false
  }
})
onUnmounted(() => stop?.())
</script>

<template>
  <main class="flex-1 px-4 py-4 relative">
    <div class="mx-auto space-y-6 max-w-7xl">
      <!-- Loading State -->
      <div v-if="isLoading" class="absolute inset-0 z-50 flex items-center justify-center">
        <div class="flex flex-col items-center gap-4">
          <Spinner class="animate-spin" />
          <p class="text-sm text-muted-foreground">Loading backups...</p>
        </div>
      </div>

      <!-- Content -->
      <div v-else class="space-y-6">
        <!-- Header Card -->
        <Card>
          <CardContent class="flex flex-wrap justify-between items-center gap-2">
            <SectionTitle title="Available Backups" :size="4" />
            <div class="flex flex-wrap gap-2">
              <DiscoverBackupsButton />
              <CreateBackupButton />
            </div>
          </CardContent>
        </Card>

        <!-- Empty State Card -->
        <Card v-if="backups.length === 0">
          <CardContent class="flex items-center justify-center py-12">
            <p class="text-sm text-muted-foreground">
              No backups listed. Create one, or discover the ones already in S3.
            </p>
          </CardContent>
        </Card>

        <!-- Backups List -->
        <div v-else class="space-y-2">
          <BackupsItem
            v-for="backup in backups"
            :key="backup.id"
            :backup="backup"
            :loadBackups="loadBackups"
          />
        </div>
      </div>
    </div>
  </main>
</template>

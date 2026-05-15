<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { Spinner } from '@/components/ui/spinner'
import { toast } from 'vue-sonner'
import { Card, CardContent } from '@/components/ui/card'

import { backupAPI } from '@/services/api'
import BackupsItem from './BackupsItem.vue'
import { type Backup } from '@/services/api'
import CreateBackupButton from './CreateBackupButton.vue'
import TitleStatus from '@/components/TitleStatus.vue'

const backups = ref<Backup[]>([])
const isLoading = ref(false)
const error = ref('')

async function loadBackups() {
  isLoading.value = true
  error.value = ''

  try {
    const backupsFromAPI = (await backupAPI.fetchAll()) as Backup[]
    backupsFromAPI.reverse()
    backups.value = backupsFromAPI
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Failed to load backups'
    toast.error('Failed to load backups')
  } finally {
    isLoading.value = false
  }
}

onMounted(async () => {
  await loadBackups()
})
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
          <CardContent class="flex justify-between items-center">
            <TitleStatus title="Available Backups" :size="4" />
            <CreateBackupButton :backupAPI="backupAPI" :loadBackups="loadBackups" />
          </CardContent>
        </Card>

        <!-- Empty State Card -->
        <Card v-if="backups.length === 0">
          <CardContent class="flex items-center justify-center py-12">
            <p class="text-sm text-muted-foreground">
              No backups available yet. Create your first backup to get started.
            </p>
          </CardContent>
        </Card>

        <!-- Backups List -->
        <div v-else class="space-y-2">
          <BackupsItem
            v-for="backup in backups"
            :key="backup.id"
            :backup="backup"
            :backupAPI="backupAPI"
            :loadBackups="loadBackups"
          />
        </div>
      </div>
    </div>
  </main>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { watchState } from '@/lib/updates'
import { createBackup, fetchBackupStatus, type BackupStatus } from '@/services/api'
import { Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/spinner'
import { toast } from 'vue-sonner'
import { CloudBackup } from 'lucide-vue-next'

const isBackupInProgress = ref(false)
let stop: (() => void) | undefined

function showStatus(status: BackupStatus) {
  isBackupInProgress.value = status.platform_backup_in_progress
}

async function onClickCreateBackup() {
  isBackupInProgress.value = true

  try {
    await createBackup()
    toast.success('Backup initiated successfully')
  } catch (err) {
    toast.error(err instanceof Error ? err.message : 'Failed to initiate backup')
    isBackupInProgress.value = false
  }
}

onMounted(() => {
  const status = watchState('backup-status', fetchBackupStatus, showStatus)
  stop = status.stop
  status.first.catch(error => console.error('Error checking backup status:', error))
})

onUnmounted(() => stop?.())
</script>

<template>
  <Button @click="onClickCreateBackup" :disabled="isBackupInProgress">
    <CloudBackup />
    <Spinner v-if="isBackupInProgress" class="animate-spin" />
    Create Backup
  </Button>
</template>

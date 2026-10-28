<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { watchState } from '@/lib/updates'
import type { BackupStatus } from '@/services/api'
import { Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/spinner'
import { toast } from 'vue-sonner'
import { CloudBackup } from 'lucide-vue-next'

interface Props {
  backupAPI: any
  loadBackups: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})

const isBackupInProgress = ref(false)
let stop: (() => void) | undefined

async function showStatus(status: BackupStatus) {
  const wasInProgress = isBackupInProgress.value
  isBackupInProgress.value = status.platform_backup_in_progress
  if (wasInProgress && !status.platform_backup_in_progress) await props.loadBackups()
}

async function onClickCreateBackup() {
  isBackupInProgress.value = true

  try {
    await props.backupAPI.create()
    toast.success('Backup initiated successfully')
  } catch (err) {
    toast.error(err instanceof Error ? err.message : 'Failed to initiate backup')
    isBackupInProgress.value = false
  }
}

onMounted(() => {
  const status = watchState(
    'backup-status',
    () => props.backupAPI.action('platform_backup_status'),
    showStatus
  )
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

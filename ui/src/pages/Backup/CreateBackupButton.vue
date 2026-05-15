<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
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
const pollInterval = 10_000
const pollTimer = ref<number | null>(null)

async function checkBackupStatus() {
  try {
    const status = await props.backupAPI.action('platform_backup_status')
    if (isBackupInProgress.value && status.platform_backup_in_progress === false) {
      await props.loadBackups()
    }
    isBackupInProgress.value = status.platform_backup_in_progress
  } catch (error) {
    console.error('Error checking backup status:', error)
  }
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

onMounted(async () => {
  // Check status immediately on mount
  await checkBackupStatus()

  // Set up polling
  pollTimer.value = window.setInterval(checkBackupStatus, pollInterval)
})

onUnmounted(() => {
  if (pollTimer.value) {
    clearInterval(pollTimer.value)
  }
})
</script>

<template>
  <Button @click="onClickCreateBackup" :disabled="isBackupInProgress">
    <CloudBackup />
    <Spinner v-if="isBackupInProgress" class="animate-spin" />
    Create Backup
  </Button>
</template>

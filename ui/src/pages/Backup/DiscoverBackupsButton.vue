<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { watchState } from '@/lib/updates'
import type { BackupStatus } from '@/services/api'
import { Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/spinner'
import { toast } from 'vue-sonner'
import { ScanSearch } from 'lucide-vue-next'

interface Props {
  backupAPI: any
}

const props = withDefaults(defineProps<Props>(), {})

const isDiscovering = ref(false)
let stop: (() => void) | undefined

function showStatus(status: BackupStatus) {
  isDiscovering.value = status.discovery_in_progress
}

async function onClickDiscover() {
  isDiscovering.value = true
  try {
    await props.backupAPI.action('discover')
    toast.success('Looking for backups in S3')
  } catch (err) {
    toast.error(err instanceof Error ? err.message : 'Failed to look for backups')
    isDiscovering.value = false
  }
}

onMounted(() => {
  const status = watchState(
    'backup-status',
    () => props.backupAPI.action('platform_backup_status'),
    showStatus
  )
  stop = status.stop
  status.first.catch(error => console.error('Error checking discovery status:', error))
})

onUnmounted(() => stop?.())
</script>

<template>
  <Button variant="outline" @click="onClickDiscover" :disabled="isDiscovering">
    <Spinner v-if="isDiscovering" class="animate-spin" />
    <ScanSearch v-else />
    Discover Backups
  </Button>
</template>

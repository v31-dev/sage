<script setup lang="ts">
import { ref, onUnmounted } from 'vue'
import { Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/spinner'
import { toast } from 'vue-sonner'
import { ScanSearch } from 'lucide-vue-next'

interface Props {
  backupAPI: any
  loadBackups: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})

const isDiscovering = ref(false)
const pollInterval = 2_000
const pollTimer = ref<number | null>(null)

function stopPolling() {
  if (pollTimer.value) {
    clearInterval(pollTimer.value)
    pollTimer.value = null
  }
}

async function checkDiscoveryStatus() {
  try {
    const status = await props.backupAPI.action('platform_backup_status')
    if (status.discovery_in_progress === false) {
      stopPolling()
      isDiscovering.value = false
      await props.loadBackups()
    }
  } catch (error) {
    console.error('Error checking discovery status:', error)
  }
}

async function onClickDiscover() {
  isDiscovering.value = true
  try {
    await props.backupAPI.action('discover')
    toast.success('Looking for backups in S3')
    pollTimer.value = window.setInterval(checkDiscoveryStatus, pollInterval)
  } catch (err) {
    toast.error(err instanceof Error ? err.message : 'Failed to look for backups')
    isDiscovering.value = false
  }
}

onUnmounted(stopPolling)
</script>

<template>
  <Button variant="outline" @click="onClickDiscover" :disabled="isDiscovering">
    <Spinner v-if="isDiscovering" class="animate-spin" />
    <ScanSearch v-else />
    Discover Backups
  </Button>
</template>

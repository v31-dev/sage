<script setup lang="ts">
import { computed } from 'vue'
import { toast } from 'vue-sonner'
import { CloudBackup } from 'lucide-vue-next'

import { Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/spinner'
import { type Application, getVolumeBackupAPI, type Volume } from '@/services/api'
import { useAppStore } from '@/stores/app'

interface Props {
  application: Application
  volume: Volume
}

const props = withDefaults(defineProps<Props>(), {})

const appStore = useAppStore()
const volumeBackupAPI = getVolumeBackupAPI(
  props.application.project.name,
  props.application.name,
  props.volume.name
)

const isBackupInProgress = computed(() => {
  return [appStore.applicationDeployStatus, props.application.status].includes('backup')
})

const backupButtonDisabled = computed(() => {
  return [
    appStore.applicationDeployStatus !== 'inactive',
    props.application.status !== 'inactive',
    props.application.container_count === 0,
  ].includes(true)
})

async function onClickCreateBackup() {
  const previousStatus = props.application.status
  appStore.updateApplicationDeployStatus('backup')

  try {
    await volumeBackupAPI.create({})
    toast.success(`Backup initiated for volume ${props.volume.name}`)
  } catch (err) {
    appStore.updateApplicationDeployStatus(previousStatus)
    toast.error(err instanceof Error ? err.message : 'Failed to initiate volume backup')
  }
}
</script>

<template>
  <Button
    size="sm"
    class="flex-1"
    variant="outline"
    @click="onClickCreateBackup"
    :disabled="backupButtonDisabled"
  >
    <Spinner class="animate-spin" v-if="isBackupInProgress" />
    <CloudBackup />Backup
  </Button>
</template>

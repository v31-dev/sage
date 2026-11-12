<script setup lang="ts">
import { computed, ref } from 'vue'
import { toast } from 'vue-sonner'
import { CloudBackup } from 'lucide-vue-next'

import { Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/spinner'
import { type Application, createVolumeBackup, refusesRequests, type Volume } from '@/services/api'

// A backup of the volume from each of the application's containers, which must all be stopped; the page is loaded
// again once they are queued.
const props = defineProps<{
  application: Application
  volume: Volume
  loadApplication: () => Promise<void>
}>()

const isRequesting = ref(false)

const disabled = computed(
  () =>
    isRequesting.value ||
    props.application.containers.length === 0 ||
    props.application.containers.some(
      container => container.view.headline !== 'inactive' || refusesRequests(container)
    )
)

async function onClickCreateBackup() {
  isRequesting.value = true
  try {
    await createVolumeBackup(
      props.application.project.name,
      props.application.name,
      props.volume.name
    )
    toast.success(`Backup queued for volume ${props.volume.name}`)
    await props.loadApplication()
  } catch (err) {
    toast.error(err instanceof Error ? err.message : 'Failed to back up the volume')
  } finally {
    isRequesting.value = false
  }
}
</script>

<template>
  <Button
    size="sm"
    class="flex-1"
    variant="outline"
    @click="onClickCreateBackup"
    :disabled="disabled"
  >
    <Spinner class="animate-spin" v-if="isRequesting" />
    <CloudBackup v-else />Backup
  </Button>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Trash } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'
import { toast } from 'vue-sonner'
import { Spinner } from '@/components/ui/spinner'

import { type Container, getContainerAPI } from '@/services/api'
import { useAppStore } from '@/stores/app'

interface Props {
  container: Container
  containersAPI: ReturnType<typeof getContainerAPI>
  loadApplication: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})
const appStore = useAppStore()

const deleteButtonDisabled = computed(() => {
  return [
    ['stopping', 'deploying'].includes(appStore.applicationDeployStatus),
    ['stopping', 'deploying'].includes(props.container?.status),
  ].includes(true)
})

const deleteButtonSpinner = computed(() => {
  return [
    ['stopping'].includes(appStore.applicationDeployStatus),
    ['stopping'].includes(props.container?.status),
  ].includes(true)
})
async function onClickDeleteContainer() {
  appStore.updateApplicationDeployStatus('stopping')
  try {
    await props.containersAPI.delete(`${props.container.worker.hostname}`)
  } catch (err) {
    appStore.updateApplicationDeployStatus('error')
    toast.error('Failed to delete container ' + (err instanceof Error ? err.message : ''))
  }
}
</script>

<template>
  <Button
    variant="destructive"
    size="sm"
    @click="onClickDeleteContainer"
    :disabled="deleteButtonDisabled"
  >
    <Spinner class="animate-spin" v-if="deleteButtonSpinner" />
    <Trash />
    Delete
  </Button>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Trash } from 'lucide-vue-next'

import ConfirmationButton from '@/components/ConfirmationButton.vue'
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

async function onConfirmDeleteContainer() {
  appStore.updateApplicationDeployStatus('stopping')
  try {
    await props.containersAPI.delete(`${props.container.worker.hostname}`)
  } catch (err) {
    appStore.updateApplicationDeployStatus('error')
    throw err
  }
}
</script>

<template>
  <ConfirmationButton
    triggerText="Delete"
    title="Delete Container"
    body="Are you sure you want to delete this container? This action cannot be undone."
    :description="`Worker: ${props.container.worker.hostname}`"
    :icon="Trash"
    destructive
    :disabled="deleteButtonDisabled"
    :loading="deleteButtonSpinner"
    :onConfirm="onConfirmDeleteContainer"
  />
</template>

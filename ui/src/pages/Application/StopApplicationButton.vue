<script setup lang="ts">
import { computed } from 'vue'
import { StopCircle } from 'lucide-vue-next'

import ConfirmationButton from '@/components/ConfirmationButton.vue'
import {
  type Application,
  type Container,
  refusesRequests,
  stopApplication,
  stopContainer,
} from '@/services/api'

// Stop all, or with `container` that container's Stop; the page is loaded again once it is queued.
const props = defineProps<{
  application: Application
  container?: Container
  loadApplication: () => Promise<void>
}>()

const containers = computed(() =>
  props.container ? [props.container] : props.application.containers
)
const disabled = computed(
  () => containers.value.length === 0 || containers.value.every(refusesRequests)
)

async function onConfirmStop() {
  const { project, name } = props.application
  if (props.container) {
    await stopContainer(project.name, name, props.container.id)
  } else {
    await stopApplication(project.name, name)
  }
  await props.loadApplication()
}
</script>

<template>
  <ConfirmationButton
    :triggerText="props.container ? 'Stop' : 'Stop all'"
    :title="props.container ? 'Stop Container' : 'Stop Application'"
    :body="
      props.container
        ? 'Are you sure you want to stop this container? It will go offline.'
        : 'Are you sure you want to stop this application? Its containers will be stopped and it will go offline.'
    "
    :description="
      props.container ? `Worker: ${props.container.worker.hostname}` : props.application.name
    "
    :icon="StopCircle"
    destructive
    :disabled="disabled"
    triggerClass="flex-1 md:flex-initial"
    :onConfirm="onConfirmStop"
  />
</template>

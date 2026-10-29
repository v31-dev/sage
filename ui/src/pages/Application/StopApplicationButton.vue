<script setup lang="ts">
import { computed } from 'vue'
import { StopCircle } from 'lucide-vue-next'

import ConfirmationButton from '@/components/ConfirmationButton.vue'
import {
  APPLICATION_STOP_ELIGIBLE_STATUSES,
  type Application,
  stopApplication,
} from '@/services/api'
import { useAppStore } from '@/stores/app'

interface Props {
  application: Application
}

const props = withDefaults(defineProps<Props>(), {})

const appStore = useAppStore()

const stopButtonDisabled = computed(() => {
  return [
    !APPLICATION_STOP_ELIGIBLE_STATUSES.includes(appStore.applicationDeployStatus),
    !APPLICATION_STOP_ELIGIBLE_STATUSES.includes(props.application.status),
    props.application?.container_count === 0,
  ].includes(true)
})
const stopButtonSpinner = computed(() => {
  return [
    appStore.applicationDeployStatus === 'stopping',
    props.application?.status === 'stopping',
  ].includes(true)
})

async function onConfirmStop() {
  appStore.updateApplicationDeployStatus(
    'stopping',
    props.application.containers.map(container => container.worker.hostname)
  )
  try {
    await stopApplication(props.application.project.name, props.application.name)
  } catch (err) {
    appStore.updateApplicationDeployStatus('error')
    throw err
  }
}
</script>

<template>
  <ConfirmationButton
    triggerText="Stop"
    title="Stop Application"
    body="Are you sure you want to stop this application? Its containers will be stopped and it will go offline."
    :description="props.application.name"
    :icon="StopCircle"
    destructive
    :disabled="stopButtonDisabled"
    :loading="stopButtonSpinner"
    triggerClass="flex-1 md:flex-initial"
    :onConfirm="onConfirmStop"
  />
</template>

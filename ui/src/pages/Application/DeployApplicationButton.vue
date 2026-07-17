<script setup lang="ts">
import { computed } from 'vue'
import { toast } from 'vue-sonner'
import { Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/spinner'
import { Play } from 'lucide-vue-next'

import { APPLICATION_BUSY_STATUSES, type Application, getApplicationAPI } from '@/services/api'
import { useAppStore } from '@/stores/app'

interface Props {
  application: Application
  applicationAPI: ReturnType<typeof getApplicationAPI>
}

const props = withDefaults(defineProps<Props>(), {})

const appStore = useAppStore()

const deployButtonDisabled = computed(() => {
  return [
    APPLICATION_BUSY_STATUSES.includes(appStore.applicationDeployStatus),
    APPLICATION_BUSY_STATUSES.includes(props.application.status),
    props.application?.container_count === 0,
  ].includes(true)
})
const deployButtonSpinner = computed(() => {
  return [
    appStore.applicationDeployStatus === 'deploying',
    props.application?.status === 'deploying',
  ].includes(true)
})

async function onClickDeployApplication() {
  appStore.updateApplicationDeployStatus('deploying')
  try {
    await props.applicationAPI.action(`${props.application.name}/deploy`)
  } catch (err) {
    // Deployment is a deferred operation so error means the deployment was rejected and not even triggered
    appStore.updateApplicationDeployStatus(props.application.status)
    toast.error('Failed to deploy application ' + (err instanceof Error ? err.message : ''))
  }
}
</script>

<template>
  <Button
    size="sm"
    class="flex-1 md:flex-initial success"
    @click="onClickDeployApplication"
    :disabled="deployButtonDisabled"
  >
    <Spinner class="animate-spin" v-if="deployButtonSpinner" />
    <Play />Deploy
  </Button>
</template>

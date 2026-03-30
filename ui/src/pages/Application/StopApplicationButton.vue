<script setup lang="ts">
import { computed } from 'vue';
import { toast } from 'vue-sonner';
import { Button } from '@/components/ui/button';
import { Spinner } from '@/components/ui/spinner';
import { StopCircle } from 'lucide-vue-next'

import {
  type Application,
  getApplicationAPI,
} from '@/services/api';
import { useAppStore } from '@/stores/app'

interface Props {
  application: Application,
  applicationAPI: ReturnType<typeof getApplicationAPI>
}

const props = withDefaults(defineProps<Props>(), {})

const appStore = useAppStore()

const stopButtonDisabled = computed(() => {
  return [
    appStore.applicationDeployStatus != 'active',
    props.application?.status != 'active',
    props.application?.container_count === 0
  ].includes(true)
})
const stopButtonSpinner = computed(() => {
  return [
    appStore.applicationDeployStatus === 'stopping',
    props.application?.status === 'stopping'
  ].includes(true)
})

async function onClickStopApplication() {
  appStore.updateApplicationDeployStatus('stopping')
  try {
    await props.applicationAPI.action(`${props.application.name}/stop`)
  } catch (err) {
    appStore.updateApplicationDeployStatus('error')
    toast.error('Failed to stop application ' + (err instanceof Error ? err.message : ''))
  }
}
</script>

<template>
  <Button size="sm" class="flex-1 md:flex-initial" variant="destructive" @click="onClickStopApplication" :disabled="stopButtonDisabled">
    <Spinner class="animate-spin" v-if="stopButtonSpinner" />
    <StopCircle />Stop
  </Button>
</template>
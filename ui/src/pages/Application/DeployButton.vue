<script setup lang="ts">
import { computed, ref } from 'vue';
import { toast } from 'vue-sonner';
import { Button } from '@/components/ui/button';
import { Spinner } from '@/components/ui/spinner';
import { Play } from 'lucide-vue-next'

import {
  type Application,
  getApplicationAPI,
} from '@/services/api';


interface Props {
  application: Application,
  applicationAPI: ReturnType<typeof getApplicationAPI>
}

const props = withDefaults(defineProps<Props>(), {})

const isClickedDeploy = ref(false)
const deployButtonDisabled = computed(() => {
  return [
    isClickedDeploy.value,
    props.application?.status === 'deploying',
    props.application?.container_count === 0
  ].includes(true)
})
const deployButtonSpinner = computed(() => {
  return [
    isClickedDeploy.value,
    props.application?.status === 'deploying'
  ].includes(true)
})

async function onClickDeployApplication() {
  isClickedDeploy.value = true
  try {
    await props.applicationAPI.action(`${props.application.name}/deploy`)
  } catch (err) {
    isClickedDeploy.value = false
    toast.error('Failed to deploy application ' + (err instanceof Error ? err.message : ''))
  }
  // Deployment is a background process, polling will handle state updation
}
</script>

<template>
  <Button class="flex-1 md:flex-initial success" @click="onClickDeployApplication" :disabled="deployButtonDisabled">
    <Spinner class="animate-spin" v-if="deployButtonSpinner" />
    <Play />Deploy
  </Button>
</template>
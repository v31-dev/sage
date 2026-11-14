<script setup lang="ts">
import { computed, ref } from 'vue'
import { toast } from 'vue-sonner'
import { Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/spinner'
import { Play } from 'lucide-vue-next'

import {
  type Application,
  type Container,
  deployApplication,
  deployContainer,
  refusesRequests,
} from '@/services/api'

// Deploy all, or with `container` that container's Deploy; the page is loaded again once it is queued.
const props = defineProps<{
  application: Application
  container?: Container
  loadApplication: () => Promise<void>
}>()

const isRequesting = ref(false)

const containers = computed(() =>
  props.container ? [props.container] : props.application.containers
)
const disabled = computed(
  () =>
    isRequesting.value || containers.value.length === 0 || containers.value.every(refusesRequests)
)

async function onClickDeploy() {
  isRequesting.value = true
  try {
    const { project, name } = props.application
    if (props.container) {
      await deployContainer(project.name, name, props.container.id)
    } else {
      await deployApplication(project.name, name)
    }
    await props.loadApplication()
  } catch (err) {
    toast.error(err instanceof Error ? err.message : 'Failed to deploy')
  } finally {
    isRequesting.value = false
  }
}
</script>

<template>
  <Button
    size="sm"
    variant="success"
    class="flex-1 md:flex-initial"
    @click="onClickDeploy"
    :disabled="disabled"
  >
    <Spinner class="animate-spin" v-if="isRequesting" />
    <Play v-else />{{ props.container ? 'Deploy' : 'Deploy all' }}
  </Button>
</template>

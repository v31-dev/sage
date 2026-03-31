<script setup lang="ts">
import { computed } from 'vue'
import {
  Card,
  CardHeader,
  CardTitle,
  CardAction,
  CardFooter
} from '@/components/ui/card'
import { RouterLink } from 'vue-router'
import { Button } from '@/components/ui/button'

import DeploymentLogsButton from './DeploymentLogsButton.vue'
import DeleteContainerButton from './DeleteContainerButton.vue'
import {
  type Container,
  getContainerAPI
} from '@/services/api'


interface Props {
  container: Container,
  containersAPI: ReturnType<typeof getContainerAPI>
  loadApplication: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})

const containerStatusVariant = computed(() => {
  if (props.container.status === 'error')
    return 'destructive'
  else
    return 'ghost'
})

const containerStatusClass = computed(() => {
  if (props.container.status === 'active')
    return 'success'
  else if (['deploying', 'stopping'].includes(props.container.status))
    return 'warning'
  else
    return ''
})
</script>

<template>
  <Card>
    <CardHeader>
      <CardTitle>
        <div class="flex w-full flex-wrap gap-2">
          <Button :class="containerStatusClass" :variant="containerStatusVariant" size="sm" disabled class="uppercase">
            {{ props.container.status }}
          </Button>
        </div>
      </CardTitle>
      <CardAction>
        <DeleteContainerButton :container="props.container" :containersAPI="props.containersAPI"
          :loadApplication="props.loadApplication" />
      </CardAction>
    </CardHeader>
    <CardFooter class="border-t">
      <div class="flex flex-wrap gap-4 w-full">
        <DeploymentLogsButton :container="props.container" />
        <Button as-child variant="outline" size="sm" class="w-full">
          <RouterLink :to="`/workers/${props.container.worker.hostname}`">
            Worker: {{ props.container.worker.hostname }}
          </RouterLink>
        </Button>
      </div>
    </CardFooter>
  </Card>
</template>
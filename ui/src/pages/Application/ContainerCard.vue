<script setup lang="ts">
import { computed } from 'vue'
import { Badge } from '@/components/ui/badge'
import {
  Card,
  CardHeader,
  CardTitle,
  CardAction,
  CardFooter
} from '@/components/ui/card'
import { RouterLink } from 'vue-router'

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

const containerBadgeVariant = computed(() => {
  if (props.container.status === 'error') 
    return 'destructive'
  else
    return 'outline'
})

const containerBadgeClass = computed(() => {
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
          <Badge :class="containerBadgeClass" :variant="containerBadgeVariant">
            Status: {{ props.container.status }}
          </Badge>
          <Badge as-child variant="outline">
            <RouterLink :to="`/workers/${props.container.worker.hostname}`">
              Worker: {{ props.container.worker.hostname }}
            </RouterLink>
          </Badge>
        </div>
      </CardTitle>
      <CardAction>
        <DeleteContainerButton :container="props.container" :containersAPI="props.containersAPI"
          :loadApplication="props.loadApplication" />
      </CardAction>
    </CardHeader>
    <CardFooter class="border-t">
      <DeploymentLogsButton :container="props.container" />
    </CardFooter>
  </Card>
</template>
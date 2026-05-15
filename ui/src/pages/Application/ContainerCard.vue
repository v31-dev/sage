<script setup lang="ts">
import { computed } from 'vue'
import { ArrowRight } from 'lucide-vue-next'
import {
  Card,
  CardHeader,
  CardTitle,
  CardAction,
  CardContent,
  CardFooter,
} from '@/components/ui/card'
import { RouterLink } from 'vue-router'
import { Button } from '@/components/ui/button'
import { ButtonGroup } from '@/components/ui/button-group'
import { Label } from '@/components/ui/label'

import TitleStatus from '@/components/TitleStatus.vue'
import EventLogsButton from './EventLogsButton.vue'
import EditContainerButton from './EditContainerButton.vue'
import DeleteContainerButton from './DeleteContainerButton.vue'
import { APPLICATION_BUSY_STATUSES, type Container, getContainerAPI } from '@/services/api'

interface Props {
  container: Container
  containersAPI: ReturnType<typeof getContainerAPI>
  loadApplication: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})

const isContainerBusy = computed(() => APPLICATION_BUSY_STATUSES.includes(props.container.status))

const containerStatusClass = computed(() => {
  if (props.container.status === 'active') return 'success'
  else if (isContainerBusy.value) return 'warning'
  else if (props.container.status === 'error') return 'error'
  else return 'default'
})

const workerStatusDotClass = computed(() => {
  return props.container.worker.online ? 'bg-green-500' : 'bg-red-500'
})
</script>

<template>
  <Card>
    <CardHeader class="border-b">
      <CardTitle>
        <div class="flex w-full flex-wrap gap-2">
          <TitleStatus
            :status="containerStatusClass"
            :loading="isContainerBusy"
            :statusText="props.container.status"
          />
        </div>
      </CardTitle>
      <CardAction>
        <ButtonGroup class="space-x-1">
          <EditContainerButton
            :container="props.container"
            :containersAPI="props.containersAPI"
            :loadApplication="props.loadApplication"
          />
          <DeleteContainerButton
            :container="props.container"
            :containersAPI="props.containersAPI"
            :loadApplication="props.loadApplication"
          />
        </ButtonGroup>
      </CardAction>
    </CardHeader>
    <CardContent class="space-y-4">
      <div>
        <Label>Domain Tag</Label>
        <p class="text-sm text-muted-foreground break-all">
          {{ props.container.domain_tag ? props.container.domain_tag : 'N/A' }}
        </p>
      </div>
    </CardContent>
    <CardFooter class="border-t flex gap-2">
      <EventLogsButton :container="props.container" />
      <Button as-child variant="outline" size="sm" class="flex-1 min-w-0">
        <RouterLink
          :to="`/workers/${props.container.worker.hostname}`"
          class="w-full min-w-0"
          :title="`Worker: ${props.container.worker.hostname}`"
        >
          <span
            aria-hidden="true"
            class="size-2 rounded-full shrink-0"
            :class="workerStatusDotClass"
          />
          <span class="block truncate">Worker: {{ props.container.worker.hostname }}</span>
          <ArrowRight />
        </RouterLink>
      </Button>
    </CardFooter>
  </Card>
</template>

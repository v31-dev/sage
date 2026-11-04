<script setup lang="ts">
import { computed } from 'vue'
import { ArrowRight } from 'lucide-vue-next'
import { Card, CardHeader, CardTitle, CardAction, CardFooter } from '@/components/ui/card'
import { RouterLink } from 'vue-router'
import { Button } from '@/components/ui/button'

import { useAppStore } from '@/stores/app'
import EventLogsButton from './EventLogsButton.vue'
import DeleteContainerButton from './DeleteContainerButton.vue'
import { type Application, type Container } from '@/services/api'
import StateIndicator from '@/components/StateIndicator.vue'

interface Props {
  application: Application
  container: Container
  loadApplication: () => Promise<void>
}

const appStore = useAppStore()

const props = withDefaults(defineProps<Props>(), {})

// The status of the container's worker in the list of workers; none before the list has loaded.
const workerIndicator = computed(() => {
  const status = appStore.workerStatus(props.container.worker.hostname)
  return status ? appStore.workerIndicator(status) : undefined
})
</script>

<template>
  <Card>
    <CardHeader class="flex items-center justify-between border-b">
      <CardTitle>
        <div class="flex w-full flex-wrap gap-2">
          <StateIndicator
            v-bind="appStore.applicationIndicator(appStore.containerStatus(props.container))"
            show-label
          />
        </div>
      </CardTitle>
      <CardAction>
        <DeleteContainerButton
          :application="props.application"
          :container="props.container"
          :loadApplication="props.loadApplication"
        />
      </CardAction>
    </CardHeader>
    <CardFooter class="flex-col space-y-4">
      <EventLogsButton :container="props.container" class="w-full min-w-0" />
      <Button as-child variant="outline" size="sm" class="w-full min-w-0">
        <RouterLink
          :to="`/workers/${props.container.worker.hostname}`"
          :title="`Worker: ${props.container.worker.hostname}`"
        >
          <StateIndicator
            v-if="workerIndicator"
            dot
            class="size-2"
            :state="workerIndicator.state"
            :label="`Worker ${workerIndicator.label}`"
          />
          <span class="block truncate">Worker: {{ props.container.worker.hostname }}</span>
          <ArrowRight />
        </RouterLink>
      </Button>
    </CardFooter>
  </Card>
</template>

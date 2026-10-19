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

import { useAppStore } from '@/stores/app'
import EventLogsButton from './EventLogsButton.vue'
import EditContainerButton from './EditContainerButton.vue'
import DeleteContainerButton from './DeleteContainerButton.vue'
import { type Container, getContainerAPI } from '@/services/api'
import StateIndicator from '@/components/StateIndicator.vue'

interface Props {
  container: Container
  containersAPI: ReturnType<typeof getContainerAPI>
  loadApplication: () => Promise<void>
}

const appStore = useAppStore()

const props = withDefaults(defineProps<Props>(), {})

const workerIndicator = computed(() => appStore.workerIndicator(props.container.worker.online))
</script>

<template>
  <Card>
    <CardHeader class="flex items-center justify-between border-b">
      <CardTitle>
        <div class="flex w-full flex-wrap gap-2">
          <StateIndicator
            v-bind="appStore.applicationIndicator(props.container.status)"
            show-label
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
    <CardFooter class="border-t flex-col space-y-4">
      <EventLogsButton :container="props.container" class="w-full min-w-0" />
      <Button as-child variant="outline" size="sm" class="w-full min-w-0">
        <RouterLink
          :to="`/workers/${props.container.worker.hostname}`"
          :title="`Worker: ${props.container.worker.hostname}`"
        >
          <StateIndicator
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

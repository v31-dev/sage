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
import {
  APPLICATION_BUSY_STATUSES,
  type Container,
  getContainerAPI,
  statusBadge,
} from '@/services/api'
import StateIndicator from '@/components/StateIndicator.vue'

interface Props {
  container: Container
  containersAPI: ReturnType<typeof getContainerAPI>
  loadApplication: () => Promise<void>
}

const props = withDefaults(defineProps<Props>(), {})

const isContainerBusy = computed(() => APPLICATION_BUSY_STATUSES.includes(props.container.status))

const containerStatusClass = computed(() => statusBadge(props.container.status))
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
            :state="props.container.worker.online ? 'ok' : 'error'"
            :label="props.container.worker.online ? 'Worker online' : 'Worker offline'"
          />
          <span class="block truncate">Worker: {{ props.container.worker.hostname }}</span>
          <ArrowRight />
        </RouterLink>
      </Button>
    </CardFooter>
  </Card>
</template>

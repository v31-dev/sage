<script setup lang="ts">
import { computed } from 'vue'
import {
  Card,
  CardHeader,
  CardTitle,
  CardAction,
  CardContent,
  CardFooter,
} from '@/components/ui/card'
import { RouterLink } from 'vue-router'

import { useAppStore } from '@/stores/app'
import EventLogsButton from './EventLogsButton.vue'
import DeleteContainerButton from './DeleteContainerButton.vue'
import DeployApplicationButton from './DeployApplicationButton.vue'
import StopApplicationButton from './StopApplicationButton.vue'
import { type Application, type Container, type Domain } from '@/services/api'
import ContainerStatus from '@/components/ContainerStatus.vue'
import DomainCard from '@/components/DomainCard.vue'
import DomainDialog from '@/components/DomainDialog.vue'
import SectionTitle from '@/components/SectionTitle.vue'
import StateIndicator from '@/components/StateIndicator.vue'

interface Props {
  application: Application
  container: Container
  loadApplication: () => Promise<void>
}

const appStore = useAppStore()

const props = withDefaults(defineProps<Props>(), {})

const TYPE_ORDER: Record<Domain['type'], number> = { public: 0, internal: 1, tcp: 2 }

// Public names first, then internal and TCP ones, each in the order the manager sends them (by name).
const domains = computed(() =>
  [...props.container.domains].sort((a, b) => TYPE_ORDER[a.type] - TYPE_ORDER[b.type])
)

// The status of the container's worker in the list of workers; none before the list has loaded.
const workerIndicator = computed(() => {
  const status = appStore.workerStatus(props.container.worker.hostname)
  return status ? appStore.workerIndicator(status) : undefined
})
</script>

<template>
  <Card data-container>
    <CardHeader class="flex items-start justify-between border-b">
      <div class="min-w-0 space-y-3">
        <CardTitle class="min-w-0">
          <ContainerStatus :container="props.container" :reload="props.loadApplication" />
        </CardTitle>
        <dl class="grid grid-cols-[max-content_minmax(0,1fr)] items-center gap-x-4 text-sm">
          <dt class="text-muted-foreground">Worker</dt>
          <dd class="flex min-w-0 flex-wrap items-center gap-2">
            <RouterLink
              :to="`/workers/${props.container.worker.hostname}`"
              class="min-w-0 truncate underline decoration-muted-foreground/50 underline-offset-4 hover:decoration-foreground"
              data-worker
            >
              {{ props.container.worker.hostname }}
            </RouterLink>
            <StateIndicator v-if="workerIndicator" v-bind="workerIndicator" show-label />
          </dd>
        </dl>
      </div>
      <CardAction>
        <DeleteContainerButton
          :application="props.application"
          :container="props.container"
          :loadApplication="props.loadApplication"
        />
      </CardAction>
    </CardHeader>
    <CardFooter class="flex-col space-y-4">
      <div class="grid w-full grid-cols-2 gap-1">
        <DeployApplicationButton
          :application="props.application"
          :container="props.container"
          :loadApplication="props.loadApplication"
        />
        <StopApplicationButton
          :application="props.application"
          :container="props.container"
          :loadApplication="props.loadApplication"
        />
      </div>
      <EventLogsButton :container="props.container" class="w-full min-w-0" />
    </CardFooter>
    <CardContent class="space-y-4 border-t pt-6" data-domains>
      <div class="flex items-center justify-between gap-2">
        <SectionTitle title="Domains" />
        <DomainDialog
          :container="{ id: props.container.id, worker: props.container.worker.hostname }"
          :reload="props.loadApplication"
        />
      </div>
      <DomainCard
        v-for="domain in domains"
        :key="domain.id"
        :domain="domain"
        in-container
        :reload="props.loadApplication"
      />
    </CardContent>
  </Card>
</template>

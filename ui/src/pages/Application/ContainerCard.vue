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
import { Badge } from '@/components/ui/badge'

import { useAppStore } from '@/stores/app'
import EventLogsButton from './EventLogsButton.vue'
import DeleteContainerButton from './DeleteContainerButton.vue'
import DeployApplicationButton from './DeployApplicationButton.vue'
import StopApplicationButton from './StopApplicationButton.vue'
import { type Application, type Container, type Domain } from '@/services/api'
import ContainerStatus from '@/components/ContainerStatus.vue'
import DomainAddress from '@/components/DomainAddress.vue'
import StateIndicator from '@/components/StateIndicator.vue'

interface Props {
  application: Application
  container: Container
  loadApplication: () => Promise<void>
}

const appStore = useAppStore()

const props = withDefaults(defineProps<Props>(), {})

const TYPES: Record<Domain['type'], string> = { public: 'Public', internal: 'Internal', tcp: 'TCP' }

const pendingCertificate = computed(() => {
  const certificate = appStore.workerCertificate(props.container.worker.hostname)
  return certificate && !(certificate.covered && certificate.synced)
    ? appStore.certificateIndicator(certificate)
    : undefined
})

// The status of the container's worker in the list of workers; none before the list has loaded.
const workerIndicator = computed(() => {
  const status = appStore.workerStatus(props.container.worker.hostname)
  return status ? appStore.workerIndicator(status) : undefined
})
</script>

<template>
  <Card data-container>
    <CardHeader class="flex items-start justify-between border-b">
      <CardTitle class="min-w-0">
        <ContainerStatus :container="props.container" :reload="props.loadApplication" />
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
    <CardContent v-if="props.container.domains.length" class="border-t pt-6 space-y-4">
      <div v-for="domain in props.container.domains" :key="domain.id" class="space-y-2" data-domain>
        <DomainAddress :domain="domain" />
        <div class="flex flex-wrap items-center gap-2">
          <StateIndicator
            v-if="domain.type !== 'public' && pendingCertificate"
            v-bind="pendingCertificate"
            show-label
          />
          <StateIndicator v-bind="appStore.domainIndicator(domain.view.status)" show-label />
          <Badge variant="outline">{{ TYPES[domain.type] }}</Badge>
          <Badge v-if="domain.service" variant="outline" class="font-mono">
            Service {{ domain.service }}
          </Badge>
          <Badge v-if="domain.pool" variant="outline" class="font-mono">
            Pool {{ domain.pool }}
          </Badge>
        </div>
        <p
          v-if="appStore.domainReason(domain.view)"
          class="text-sm text-muted-foreground"
          data-reason
        >
          {{ appStore.domainReason(domain.view) }}
        </p>
      </div>
    </CardContent>
  </Card>
</template>

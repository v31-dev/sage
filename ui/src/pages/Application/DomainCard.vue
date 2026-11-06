<script setup lang="ts">
import { computed } from 'vue'
import { ExternalLink } from 'lucide-vue-next'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useAppStore } from '@/stores/app'

import type { Domain, Application } from '@/services/api'
import CopyButton from '@/components/CopyButton.vue'
import StateIndicator from '@/components/StateIndicator.vue'

interface Props {
  application: Application
  domain: Domain
}

const props = withDefaults(defineProps<Props>(), {})
const appStore = useAppStore()

const TYPES: Record<Domain['type'], string> = { public: 'Public', internal: 'Internal', tcp: 'TCP' }

const container = computed(() =>
  props.application.containers.find(
    container => container.worker.hostname === props.domain.container?.worker
  )
)

// The owning worker's certificate for an internal or TCP name, shown until it is ready there.
const pendingCertificate = computed(() => {
  if (props.domain.type === 'public' || !props.domain.worker) return undefined
  const certificate = appStore.workerCertificate(props.domain.worker)
  return certificate && !(certificate.covered && certificate.synced)
    ? appStore.certificateIndicator(certificate)
    : undefined
})
</script>

<template>
  <Card data-domain>
    <CardHeader class="flex items-center gap-2">
      <CardTitle class="min-w-0 flex-1">
        <Button
          v-if="props.domain.type === 'tcp'"
          size="sm"
          variant="outline"
          class="w-full min-w-0"
          disabled
        >
          <span class="truncate">{{ props.domain.address }}</span>
        </Button>
        <Button v-else size="sm" variant="outline" class="w-full min-w-0" as-child>
          <a :href="props.domain.address ?? undefined" target="_blank">
            <ExternalLink />
            <span class="truncate">{{ props.domain.address }}</span>
          </a>
        </Button>
      </CardTitle>
      <CopyButton v-if="props.domain.address" :text="props.domain.address" />
    </CardHeader>
    <CardContent class="flex flex-wrap items-center gap-2">
      <Badge variant="secondary" class="font-mono">{{ props.domain.worker }}</Badge>
      <StateIndicator
        v-if="container"
        v-bind="appStore.applicationIndicator(appStore.containerStatus(container))"
        show-label
      />
      <StateIndicator v-if="pendingCertificate" v-bind="pendingCertificate" show-label />
      <StateIndicator
        v-bind="appStore.domainIndicator(props.domain.status)"
        :title="props.domain.sync_error ?? undefined"
        show-label
      />
      <Badge variant="outline">{{ TYPES[props.domain.type] }}</Badge>
      <Badge v-if="props.domain.service" variant="outline" class="font-mono">
        Service {{ props.domain.service }}
      </Badge>
      <Badge v-if="props.domain.pool" variant="outline" class="font-mono">
        Pool {{ props.domain.pool }}
      </Badge>
    </CardContent>
  </Card>
</template>

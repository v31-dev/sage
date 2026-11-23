<script setup lang="ts">
import { computed } from 'vue'
import { toast } from 'vue-sonner'
import { RouterLink } from 'vue-router'
import { Trash } from 'lucide-vue-next'
import { Card, CardContent, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import ConfirmationButton from '@/components/ConfirmationButton.vue'
import DomainAddress from '@/components/DomainAddress.vue'
import DomainDialog from '@/components/DomainDialog.vue'
import StateIndicator from '@/components/StateIndicator.vue'
import { type Domain, type Indicator, deleteDomain } from '@/services/api'
import { useAppStore } from '@/stores/app'

// A domain with its state, its address and its Edit and Delete. On its container's card (`inContainer`) it names its
// type and service, which the Domains page groups by (it names TCP only), and leaves out the application and worker
// that card shows; on the Domains page it links them, with `containerIndicator`, its container's state.
interface Props {
  domain: Domain
  inContainer?: boolean
  containerIndicator?: Indicator
  reload: () => Promise<void>
}

const props = defineProps<Props>()
const appStore = useAppStore()

const TYPES: Record<Domain['type'], string> = { public: 'Public', internal: 'Internal', tcp: 'TCP' }

// None before the list of workers has loaded.
const workerIndicator = computed(() => {
  const status = appStore.workerStatus(props.domain.container.worker)
  return status ? appStore.workerIndicator(status) : undefined
})

// The worker's wildcard certificate, which serves an internal or TCP name.
const certificateIndicator = computed(() => {
  if (props.domain.type === 'public') return undefined
  const certificate = appStore.workerCertificate(props.domain.container.worker)
  return certificate ? appStore.certificateIndicator(certificate) : undefined
})

const link =
  'underline decoration-muted-foreground/50 underline-offset-4 hover:decoration-foreground'

async function onConfirmDelete() {
  await deleteDomain(props.domain.id)
  await props.reload()
  toast.success('Domain deleted')
}
</script>

<template>
  <Card data-domain class="min-w-0 gap-0 py-0">
    <CardHeader class="flex items-center gap-2 border-b py-4 [.border-b]:pb-4">
      <CardTitle class="min-w-0 flex-1 truncate leading-normal" data-host>
        {{ props.domain.host ?? props.domain.name }}
      </CardTitle>
      <StateIndicator v-bind="appStore.domainIndicator(props.domain.view.status)" show-label />
    </CardHeader>
    <CardContent class="flex-1 space-y-3 py-4">
      <dl class="grid grid-cols-[max-content_minmax(0,1fr)] items-center gap-x-4 gap-y-2 text-sm">
        <template v-if="props.inContainer || props.domain.type === 'tcp'">
          <dt class="text-muted-foreground">Type</dt>
          <dd data-type>{{ TYPES[props.domain.type] }}</dd>
        </template>
        <template v-if="props.inContainer && props.domain.service">
          <dt class="text-muted-foreground">Service</dt>
          <dd data-service>{{ props.domain.service }}</dd>
        </template>
        <template v-if="props.domain.pool">
          <dt class="text-muted-foreground">Pool</dt>
          <dd data-pool>{{ props.domain.pool }}</dd>
        </template>
        <template v-if="!props.inContainer">
          <dt class="text-muted-foreground">Application</dt>
          <dd class="flex min-w-0 flex-wrap items-center gap-2">
            <RouterLink
              :to="`/applications/${props.domain.container.application.project.name}/${props.domain.container.application.name}`"
              :class="['min-w-0 truncate', link]"
              data-application
            >
              {{ props.domain.container.application.project.label }} /
              {{ props.domain.container.application.label }}
            </RouterLink>
            <StateIndicator
              v-if="props.containerIndicator"
              v-bind="props.containerIndicator"
              show-label
            />
          </dd>
        </template>
        <dt class="text-muted-foreground">Port</dt>
        <dd data-port>{{ props.domain.port }}</dd>
        <template v-if="props.domain.callers.length">
          <dt class="text-muted-foreground">Callers</dt>
          <dd class="min-w-0" data-callers>
            <template v-for="(caller, index) in props.domain.callers" :key="caller.id">
              <template v-if="index">, </template>
              <RouterLink
                :to="`/applications/${caller.project.name}/${caller.name}`"
                :class="link"
                data-caller
                >{{ caller.project.label }} / {{ caller.label }}</RouterLink
              >
            </template>
          </dd>
        </template>
        <template v-if="!props.inContainer">
          <dt class="text-muted-foreground">Worker</dt>
          <dd class="flex min-w-0 flex-wrap items-center gap-2">
            <RouterLink
              :to="`/workers/${props.domain.container.worker}`"
              :class="['min-w-0 truncate', link]"
              data-worker
            >
              {{ props.domain.container.worker }}
            </RouterLink>
            <StateIndicator v-if="workerIndicator" v-bind="workerIndicator" show-label />
          </dd>
        </template>
        <template v-if="certificateIndicator">
          <dt class="text-muted-foreground">Certificate</dt>
          <dd data-certificate>
            <StateIndicator v-bind="certificateIndicator" show-label />
          </dd>
        </template>
      </dl>
      <p
        v-if="appStore.domainReason(props.domain.view)"
        class="text-sm text-muted-foreground"
        data-reason
      >
        {{ appStore.domainReason(props.domain.view) }}
      </p>
    </CardContent>
    <CardFooter class="gap-1 border-t py-2 pr-3 [.border-t]:pt-2">
      <DomainAddress :domain="props.domain" />
      <div class="ml-auto flex shrink-0 items-center gap-1">
        <DomainDialog
          :container="props.domain.container"
          :domain="props.domain"
          :reload="props.reload"
        />
        <ConfirmationButton
          triggerText="Delete"
          title="Delete Domain"
          body="Are you sure you want to delete this domain? This action cannot be undone."
          :description="props.domain.host ?? props.domain.name"
          :icon="Trash"
          icon-only
          destructive
          :onConfirm="onConfirmDelete"
        />
      </div>
    </CardFooter>
  </Card>
</template>

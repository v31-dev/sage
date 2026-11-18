<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { toast } from 'vue-sonner'
import { RouterLink } from 'vue-router'
import { Compass, ExternalLink, Trash } from 'lucide-vue-next'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Spinner } from '@/components/ui/spinner'
import ConfirmationButton from '@/components/ConfirmationButton.vue'
import DomainAddress from '@/components/DomainAddress.vue'
import StateIndicator from '@/components/StateIndicator.vue'
import { watchState } from '@/lib/updates'
import { type Domain, type DomainsResponse, deleteDomain, fetchDomains } from '@/services/api'
import { useAppStore } from '@/stores/app'
import DomainDialog from './DomainDialog.vue'

const appStore = useAppStore()
const state = ref<DomainsResponse>({ domains: [], containers: [] })
const isLoading = ref(true)
const error = ref('')
let stop: (() => void) | undefined

// A group per namespace and service: Public's first, and in each namespace the domains without a service last.
const groups = computed(() =>
  [
    {
      title: 'Public',
      discovery: 'discovery',
      domains: state.value.domains.filter(domain => domain.type === 'public'),
    },
    {
      title: 'Internal',
      discovery: 'discovery.int',
      domains: state.value.domains.filter(domain => domain.type !== 'public'),
    },
  ].flatMap(namespace =>
    [...new Set(namespace.domains.map(domain => domain.service))]
      .sort((a, b) => (a === null ? 1 : b === null ? -1 : a.localeCompare(b)))
      .map(service => ({
        key: `${namespace.title}/${service ?? ''}`,
        namespace: namespace.title,
        service,
        discoveryUrl:
          service && appStore.info?.domain
            ? `https://${namespace.discovery}.${appStore.info.domain}/?service=${encodeURIComponent(service)}`
            : null,
        domains: namespace.domains.filter(domain => domain.service === service),
      }))
  )
)

// Each linked container's state, from the containers the page holds.
const indicators = computed(() =>
  Object.fromEntries(
    state.value.containers.map(container => [
      container.id,
      appStore.containerIndicator(container.view.headline, container.view.reason),
    ])
  )
)

function show(next: DomainsResponse) {
  state.value = next
  error.value = ''
}

async function load() {
  show(await fetchDomains())
}

async function onConfirmDelete(domain: Domain) {
  await deleteDomain(domain.id)
  await load()
  toast.success('Domain deleted')
}

// The domains, loaded from their route and then pushed by the manager each time one, or its container, changes.
onMounted(async () => {
  const watch = watchState('domains', fetchDomains, show)
  stop = watch.stop
  try {
    await watch.first
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Failed to load domains'
  } finally {
    isLoading.value = false
  }
})
onUnmounted(() => stop?.())
</script>

<template>
  <main class="flex-1 px-2 sm:px-4 py-4 relative">
    <div class="max-w-7xl mx-auto">
      <div v-if="isLoading" class="absolute inset-0 z-50 flex items-center justify-center">
        <div class="flex flex-col items-center gap-4">
          <Spinner />
          <p class="text-sm text-muted-foreground">Loading domains...</p>
        </div>
      </div>

      <div v-else class="space-y-6">
        <div class="flex justify-end">
          <DomainDialog :containers="state.containers" :domains="state.domains" :reload="load" />
        </div>

        <Card v-if="error">
          <CardContent class="flex flex-col items-center justify-center py-12">
            <p class="text-muted-foreground text-lg">Failed to load domains</p>
            <p class="text-sm text-muted-foreground">{{ error }}</p>
          </CardContent>
        </Card>

        <Card v-else-if="groups.length === 0">
          <CardContent class="flex flex-col items-center justify-center py-12">
            <p class="text-muted-foreground text-lg">No domains</p>
            <p class="text-sm text-muted-foreground">Give a container a name to reach it at.</p>
          </CardContent>
        </Card>

        <Card v-for="group in groups" v-else :key="group.key" data-group>
          <CardHeader>
            <CardTitle>
              {{ group.namespace }} ·
              {{ group.service ? `Service ${group.service}` : 'No service' }}
            </CardTitle>
            <CardDescription v-if="!group.service">Not listed at discovery</CardDescription>
            <CardAction v-if="group.discoveryUrl">
              <Button
                as="a"
                :href="group.discoveryUrl"
                target="_blank"
                rel="noreferrer"
                variant="outline"
                size="sm"
                data-discovery
              >
                <Compass />
                Discovery
                <ExternalLink />
              </Button>
            </CardAction>
          </CardHeader>
          <CardContent class="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
            <Card
              v-for="domain in group.domains"
              :key="domain.id"
              data-domain
              class="min-w-0 gap-0 py-0"
            >
              <CardHeader class="flex items-center gap-2 border-b py-4 [.border-b]:pb-4">
                <CardTitle class="min-w-0 flex-1 truncate leading-normal" data-host>
                  {{ domain.host ?? domain.name }}
                </CardTitle>
                <Badge v-if="domain.type === 'tcp'" variant="outline">TCP</Badge>
                <StateIndicator v-bind="appStore.domainIndicator(domain.view.status)" show-label />
              </CardHeader>
              <CardContent class="flex-1 space-y-3 py-4">
                <dl
                  class="grid grid-cols-[max-content_minmax(0,1fr)] items-center gap-x-4 gap-y-2 text-sm"
                >
                  <template v-if="domain.pool">
                    <dt class="text-muted-foreground">Pool</dt>
                    <dd data-pool>{{ domain.pool }}</dd>
                  </template>
                  <dt class="text-muted-foreground">Application</dt>
                  <dd class="flex min-w-0 flex-wrap items-center gap-2">
                    <template v-if="domain.container">
                      <RouterLink
                        :to="`/applications/${domain.container.application.project.name}/${domain.container.application.name}`"
                        class="min-w-0 truncate underline decoration-muted-foreground/50 underline-offset-4 hover:decoration-foreground"
                        data-application
                      >
                        {{ domain.container.application.project.label }} /
                        {{ domain.container.application.label }}
                      </RouterLink>
                      <StateIndicator
                        v-if="indicators[domain.container.id]"
                        v-bind="indicators[domain.container.id]!"
                        show-label
                      />
                    </template>
                    <StateIndicator v-else state="warning" label="not linked" show-label />
                  </dd>
                  <template v-if="domain.container?.worker ?? domain.worker">
                    <dt class="text-muted-foreground">Worker</dt>
                    <dd class="min-w-0">
                      <RouterLink
                        :to="`/workers/${domain.container?.worker ?? domain.worker}`"
                        class="block truncate underline decoration-muted-foreground/50 underline-offset-4 hover:decoration-foreground"
                        data-worker
                      >
                        {{ domain.container?.worker ?? domain.worker }}
                      </RouterLink>
                    </dd>
                  </template>
                </dl>
                <p
                  v-if="appStore.domainReason(domain.view)"
                  class="text-sm text-muted-foreground"
                  data-reason
                >
                  {{ appStore.domainReason(domain.view) }}
                </p>
              </CardContent>
              <CardFooter class="gap-1 border-t py-2 pr-3 [.border-t]:pt-2">
                <DomainAddress :domain="domain" />
                <div class="ml-auto flex shrink-0 items-center gap-1">
                  <DomainDialog
                    :containers="state.containers"
                    :domains="state.domains"
                    :domain="domain"
                    :reload="load"
                  />
                  <ConfirmationButton
                    triggerText="Delete"
                    title="Delete Domain"
                    body="Are you sure you want to delete this domain? This action cannot be undone."
                    :description="domain.host ?? domain.name"
                    :icon="Trash"
                    icon-only
                    destructive
                    :onConfirm="() => onConfirmDelete(domain)"
                  />
                </div>
              </CardFooter>
            </Card>
          </CardContent>
        </Card>
      </div>
    </div>
  </main>
</template>

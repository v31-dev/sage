<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { toast } from 'vue-sonner'
import { ExternalLink, Trash } from 'lucide-vue-next'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Spinner } from '@/components/ui/spinner'
import ConfirmationButton from '@/components/ConfirmationButton.vue'
import CopyButton from '@/components/CopyButton.vue'
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

// Each namespace with its domains, by service and then by pool; the domains without one last.
const namespaces = computed(() =>
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
  ]
    .filter(namespace => namespace.domains.length > 0)
    .map(namespace => ({
      title: namespace.title,
      services: groups(namespace.domains, 'service').map(service => ({
        name: service.name,
        discoveryUrl:
          service.name && appStore.info?.domain
            ? `https://${namespace.discovery}.${appStore.info.domain}/?service=${encodeURIComponent(service.name)}`
            : null,
        pools: groups(service.domains, 'pool'),
      })),
    }))
)

// Each linked container's headline, from the containers the page holds.
const headlines = computed(() =>
  Object.fromEntries(
    state.value.containers.map(container => [container.id, container.view.headline])
  )
)

function groups(domains: Domain[], key: 'service' | 'pool') {
  const names = [...new Set(domains.map(domain => domain[key]))].sort((a, b) =>
    a === null ? 1 : b === null ? -1 : a.localeCompare(b)
  )
  return names.map(name => ({ name, domains: domains.filter(domain => domain[key] === name) }))
}

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

        <Card v-else-if="namespaces.length === 0">
          <CardContent class="flex flex-col items-center justify-center py-12">
            <p class="text-muted-foreground text-lg">No domains</p>
            <p class="text-sm text-muted-foreground">Give a container a name to reach it at.</p>
          </CardContent>
        </Card>

        <Card v-for="namespace in namespaces" v-else :key="namespace.title" data-namespace>
          <CardHeader>
            <CardTitle>{{ namespace.title }}</CardTitle>
          </CardHeader>
          <CardContent class="space-y-6">
            <section v-for="service in namespace.services" :key="service.name ?? ''" data-service>
              <h3 class="mb-2 text-sm font-medium">
                <Button
                  v-if="service.discoveryUrl"
                  as="a"
                  :href="service.discoveryUrl"
                  target="_blank"
                  rel="noreferrer"
                  variant="link"
                  class="h-auto p-0 has-[>svg]:px-0"
                >
                  Service {{ service.name }}
                  <ExternalLink />
                </Button>
                <template v-else>{{
                  service.name ? `Service ${service.name}` : 'No service'
                }}</template>
              </h3>
              <div v-for="pool in service.pools" :key="pool.name ?? ''" class="mb-3 ml-2" data-pool>
                <p class="mb-1 text-xs text-muted-foreground">
                  {{ pool.name ? `Pool ${pool.name}` : 'No pool' }}
                </p>
                <div
                  v-for="domain in pool.domains"
                  :key="domain.id"
                  data-domain
                  class="flex flex-wrap items-center gap-2 border-t py-2"
                >
                  <Button
                    v-if="domain.type === 'tcp' || !domain.address"
                    size="sm"
                    variant="outline"
                    class="min-w-0 flex-1"
                    disabled
                  >
                    <span class="truncate">{{ domain.address ?? domain.name }}</span>
                  </Button>
                  <Button v-else size="sm" variant="outline" class="min-w-0 flex-1" as-child>
                    <a :href="domain.address" target="_blank">
                      <ExternalLink />
                      <span class="truncate">{{ domain.address }}</span>
                    </a>
                  </Button>
                  <CopyButton v-if="domain.address" :text="domain.address" />
                  <Badge v-if="domain.type === 'tcp'" variant="outline">TCP</Badge>
                  <template v-if="domain.container">
                    <Badge variant="secondary">
                      {{ domain.container.application.label }} · {{ domain.container.worker }}
                    </Badge>
                    <StateIndicator
                      v-if="headlines[domain.container.id]"
                      v-bind="appStore.containerIndicator(headlines[domain.container.id]!)"
                      show-label
                    />
                  </template>
                  <StateIndicator v-else state="warning" label="not linked" show-label />
                  <StateIndicator
                    v-bind="appStore.domainIndicator(domain.view.status)"
                    :title="domain.view.error ?? undefined"
                    show-label
                  />
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
                    destructive
                    :onConfirm="() => onConfirmDelete(domain)"
                  />
                </div>
              </div>
            </section>
          </CardContent>
        </Card>
      </div>
    </div>
  </main>
</template>

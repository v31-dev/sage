<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { Compass, ExternalLink } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Spinner } from '@/components/ui/spinner'
import DomainCard from '@/components/DomainCard.vue'
import { watchState } from '@/lib/updates'
import { type DomainsResponse, fetchDomains } from '@/services/api'
import { useAppStore } from '@/stores/app'

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
        <Card v-if="error">
          <CardContent class="flex flex-col items-center justify-center py-12">
            <p class="text-muted-foreground text-lg">Failed to load domains</p>
            <p class="text-sm text-muted-foreground">{{ error }}</p>
          </CardContent>
        </Card>

        <Card v-else-if="groups.length === 0">
          <CardContent class="flex flex-col items-center justify-center py-12">
            <p class="text-muted-foreground text-lg">No domains</p>
            <p class="text-sm text-muted-foreground">
              Add one on a container's card, on its application's page.
            </p>
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
            <DomainCard
              v-for="domain in group.domains"
              :key="domain.id"
              :domain="domain"
              :container-indicator="indicators[domain.container.id]"
              :reload="load"
            />
          </CardContent>
        </Card>
      </div>
    </div>
  </main>
</template>

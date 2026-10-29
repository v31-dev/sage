<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/spinner'
import type { APIError } from '@/lib/api'
import { watchState } from '@/lib/updates'

import {
  getApplicationAPI,
  getContainerAPI,
  getDomainAPI,
  getVolumeAPI,
  type Application,
} from '@/services/api'
import { useAppStore } from '@/stores/app'
import ApplicationHeader from './ApplicationHeader.vue'
import AddContainerButton from './AddContainerButton.vue'
import ContainerCard from './ContainerCard.vue'
import AddDomainButton from './AddDomainButton.vue'
import DomainCard from './DomainCard.vue'
import AddVolumeButton from './AddVolumeButton.vue'
import VolumeCard from './VolumeCard.vue'
import SectionTitle from '@/components/SectionTitle.vue'

const route = useRoute()
const router = useRouter()
const projectName = route.params.projectId as string
const appName = route.params.appId as string
const appStore = useAppStore()

const applicationAPI = getApplicationAPI(projectName)
const volumeAPI = getVolumeAPI(projectName, appName)
const containersAPI = getContainerAPI(projectName, appName)
const domainAPI = getDomainAPI(projectName, appName)
const application = ref<Application | null>(null)
const isLoading = ref(true)
const loadError = ref('')
const isNotFound = ref(false)
let stop: (() => void) | undefined

const fetchApplication = () => applicationAPI.fetchOne(appName) as Promise<Application>

function showLoadError(err: unknown) {
  console.error('Failed to load application:', err)
  application.value = null
  const status = (err as APIError).status
  if (status === 404) {
    isNotFound.value = true
  } else {
    loadError.value = err instanceof Error ? err.message : 'Failed to load application'
  }
}

// The application, loaded from its route and then pushed by the manager each time it changes.
onMounted(async () => {
  const watch = watchState(
    `application:${projectName}/${appName}`,
    fetchApplication,
    showApplication
  )
  stop = watch.stop
  try {
    await watch.first
  } catch (err) {
    showLoadError(err)
  } finally {
    isLoading.value = false
  }
})
onUnmounted(() => stop?.())

// The first state is the page's; a later one changes what shows of the application as it runs, and leaves the rest
// (a volume, a domain or a variable being edited) to the reload after an action.
function showApplication(next: Application) {
  loadError.value = ''
  isNotFound.value = false
  if (application.value) {
    application.value.status = next.status
    application.value.containers = next.containers
    application.value.container_count = next.container_count
    application.value.updated_at = next.updated_at
    application.value.domains_synced = next.domains_synced
    application.value.deployed_at = next.deployed_at
    application.value.config_dirty = next.config_dirty
  } else {
    application.value = next
  }
  appStore.updateApplicationDeployStatus(next.status)
}

// The whole application again, after an action on it.
async function loadApplication() {
  try {
    isLoading.value = true
    loadError.value = ''
    isNotFound.value = false
    application.value = await fetchApplication()
    appStore.updateApplicationDeployStatus(application.value.status)
  } catch (err) {
    showLoadError(err)
  } finally {
    isLoading.value = false
  }
}

function goBack() {
  router.push(`/projects/${projectName}`)
}
</script>

<template>
  <main class="flex-1 px-4 py-8 relative">
    <div class="mx-auto space-y-6 max-w-7xl">
      <!-- Loading State -->
      <div v-if="isLoading" class="absolute inset-0 z-50 flex items-center justify-center">
        <div class="flex flex-col items-center gap-4">
          <Spinner />
          <p class="text-sm text-muted-foreground">Loading application...</p>
        </div>
      </div>

      <!-- Content -->
      <Card v-else-if="loadError">
        <CardContent class="flex flex-col items-center justify-center py-12">
          <p class="text-muted-foreground text-lg mb-4">Failed to load application</p>
          <p class="text-sm text-muted-foreground">{{ loadError }}</p>
        </CardContent>
      </Card>

      <div v-else-if="application" class="space-y-6">
        <!-- Application Header -->
        <ApplicationHeader
          :application="application"
          :applicationAPI="applicationAPI"
          :loadApplication="loadApplication"
        />

        <!-- Volumes -->
        <div class="space-y-4">
          <div class="flex justify-between items-center">
            <SectionTitle
              title="Volumes"
              :size="4"
              tooltip="Application needs to be deployed again if Volume configuration is modified."
            />
            <!-- Add Volume Button -->
            <AddVolumeButton
              :application="application"
              :volumeAPI="volumeAPI"
              :loadApplication="loadApplication"
            />
          </div>

          <!-- Empty Volumes State -->
          <div
            v-if="application.volumes.length === 0"
            class="flex items-center justify-center py-8"
          >
            <Card class="w-full">
              <CardContent class="flex flex-col items-center justify-center py-12">
                <p class="text-muted-foreground text-lg">No volumes</p>
              </CardContent>
            </Card>
          </div>

          <!-- Volumes Grid -->
          <div v-else class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            <VolumeCard
              v-for="volume in application.volumes"
              :key="volume.name"
              :application="application"
              :volume="volume"
              :volumeAPI="volumeAPI"
              :loadApplication="loadApplication"
            />
          </div>
        </div>

        <!-- Domains -->
        <div class="space-y-4">
          <div class="flex justify-between items-center">
            <SectionTitle
              title="Domains"
              :size="4"
              tooltip="Domain configurations are automatically synced."
            />
            <!-- Add Domain Button -->
            <AddDomainButton
              :application="application"
              :domainAPI="domainAPI"
              :loadApplication="loadApplication"
            />
          </div>

          <!-- Empty Domains State -->
          <div
            v-if="application.domains.length === 0"
            class="flex items-center justify-center py-8"
          >
            <Card class="w-full">
              <CardContent class="flex flex-col items-center justify-center py-12">
                <p class="text-muted-foreground text-lg">No domains</p>
              </CardContent>
            </Card>
          </div>

          <!-- Domains Grid -->
          <div v-else class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            <DomainCard
              v-for="domain in application.domains"
              :key="domain.name"
              :application="application"
              :domain="domain"
              :domainAPI="domainAPI"
              :loadApplication="loadApplication"
            />
          </div>
        </div>

        <!-- Containers -->
        <div class="space-y-4">
          <div class="flex justify-between items-center">
            <SectionTitle
              title="Containers"
              :size="4"
              tooltip="Application needs to be deployed again if configuration is modified."
            />
            <!-- Add Container Button -->
            <AddContainerButton
              :application="application"
              :applicationAPI="applicationAPI"
              :containersAPI="containersAPI"
              :loadApplication="loadApplication"
            />
          </div>

          <!-- Empty Containers State -->
          <div
            v-if="application.containers.length === 0"
            class="flex items-center justify-center py-8"
          >
            <Card class="w-full">
              <CardContent class="flex flex-col items-center justify-center py-12">
                <p class="text-muted-foreground text-lg">No containers</p>
              </CardContent>
            </Card>
          </div>

          <!-- Containers Grid -->
          <div v-else class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            <ContainerCard
              v-for="container in application.containers"
              :key="container.worker.hostname"
              :container="container"
              :containersAPI="containersAPI"
              :loadApplication="loadApplication"
              class="flex flex-col"
            />
          </div>
        </div>
      </div>

      <!-- Error/Not Found State -->
      <Card v-else-if="isNotFound">
        <CardContent class="flex flex-col items-center justify-center py-12">
          <p class="text-muted-foreground text-lg mb-4">Application {{ appName }} not found</p>
          <Button size="sm" @click="goBack" variant="outline"> Back to Project </Button>
        </CardContent>
      </Card>
    </div>
  </main>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Card,
  CardContent,
} from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/spinner'

import {
  getApplicationAPI,
  getContainerAPI,
  getDomainAPI,
  type Application,
} from '@/services/api'
import { useAppStore } from '@/stores/app'
import ApplicationHeader from './ApplicationHeader.vue'
import AddContainerButton from './AddContainerButton.vue'
import ContainerCard from './ContainerCard.vue'
import AddDomainButton from './AddDomainButton.vue'
import DomainCard from './DomainCard.vue'

const route = useRoute()
const router = useRouter()
const projectName = route.params.projectId as string
const appName = route.params.appId as string
const appStore = useAppStore()

const applicationAPI = getApplicationAPI(projectName)
const containersAPI = getContainerAPI(projectName, appName)
const domainAPI = getDomainAPI(projectName, appName)
const application = ref<Application | null>(null)
const isLoading = ref(true)
let pollInterval: ReturnType<typeof setInterval> | null = null

onMounted(async () => {
  await loadApplication()
  pollInterval = setInterval(loadApplicationStatus, 5_000)
})
onUnmounted(() => {
  if (pollInterval) clearInterval(pollInterval)
})

async function loadApplication() {
  try {
    isLoading.value = true
    application.value = await applicationAPI.fetchOne(appName) as Application
    appStore.updateApplicationDeployStatus(application.value.status)
  } catch (err) {
    console.error('Failed to load application:', err)
  } finally {
    isLoading.value = false
  }
}

async function loadApplicationStatus() {
  try {
    const updatedApplication = await applicationAPI.fetchOne(appName) as Application
    if (application.value) {
      application.value.status = updatedApplication.status
      application.value.containers = updatedApplication.containers
      application.value.updated_at = updatedApplication.updated_at
      appStore.updateApplicationDeployStatus(updatedApplication.status)
    }
  } catch (err) {
    console.error('Failed to load application status:', err)
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
      <div v-else-if="application" class="space-y-6">
        <!-- Application Header -->
        <ApplicationHeader :application="application" :applicationAPI="applicationAPI"
          :loadApplication="loadApplication" />

        <!-- Domains -->
        <div class="space-y-4">
          <div class="flex justify-between items-center">
            <h2 class="text-xl font-semibold">Domains</h2>
            <!-- Add Domain Button -->
            <AddDomainButton :application="application" :domainAPI="domainAPI" :loadApplication="loadApplication" />
          </div>

          <!-- Empty Domains State -->
          <div v-if="application.domains.length === 0" class="flex items-center justify-center py-8">
            <Card class="w-full">
              <CardContent class="flex flex-col items-center justify-center py-12">
                <p class="text-muted-foreground text-lg">No domains</p>
              </CardContent>
            </Card>
          </div>

          <!-- Domains Grid -->
          <div v-else class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            <DomainCard v-for="domain in application.domains" :key="domain.name" :domain="domain" :domainAPI="domainAPI"
              :loadApplication="loadApplication" class="flex flex-col" />
          </div>
        </div>

        <!-- Containers -->
        <div class="space-y-4">
          <div class="flex justify-between items-center">
            <h2 class="text-xl font-semibold">Containers</h2>
            <!-- Add Container Button -->
            <AddContainerButton :application="application" :applicationAPI="applicationAPI"
              :containersAPI="containersAPI" :loadApplication="loadApplication" />
          </div>

          <!-- Empty Containers State -->
          <div v-if="application.containers.length === 0" class="flex items-center justify-center py-8">
            <Card class="w-full">
              <CardContent class="flex flex-col items-center justify-center py-12">
                <p class="text-muted-foreground text-lg">No containers</p>
              </CardContent>
            </Card>
          </div>

          <!-- Containers Grid -->
          <div v-else class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            <ContainerCard v-for="container in application.containers" :key="container.worker.hostname"
              :container="container" :containersAPI="containersAPI" :loadApplication="loadApplication"
              class="flex flex-col" />
          </div>
        </div>
      </div>

      <!-- Error/Not Found State -->
      <Card v-else>
        <CardContent class="flex flex-col items-center justify-center py-12">
          <p class="text-muted-foreground text-lg mb-4">Application {{ appName }} not found</p>
          <Button size="sm" @click="goBack" variant="outline">
            Back to Project
          </Button>
        </CardContent>
      </Card>
    </div>
  </main>
</template>
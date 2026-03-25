<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Trash,
  StopCircle,
  RefreshCw,
  Logs,
  Activity
} from 'lucide-vue-next'
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  CardFooter
} from '@/components/ui/card'
import { ButtonGroup } from '@/components/ui/button-group'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'

import {
  getApplicationAPI,
  getContainerAPI,
  type Application,
} from '@/services/api'
import DeployButton from './DeployButton.vue'
import DeploymentLogsButton from './DeploymentLogsButton.vue'
import EditApplicationButton from './EditApplicationButton.vue'
import DeleteApplicationButton from './DeleteApplicationButton.vue'
import AddContainerButton from './AddContainerButton.vue'

const route = useRoute()
const router = useRouter()
const projectName = route.params.projectId as string
const appName = route.params.appId as string

const applicationAPI = getApplicationAPI(projectName)
const containersAPI = getContainerAPI(projectName, appName)
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
    }
  } catch (err) {
    console.error('Failed to load application status:', err)
  }
}

function goBack() {
  router.push(`/projects/${projectName}`)
}

// ####################################################################################################
// Delete Container
async function onClickDeleteContainer() {

}
// ####################################################################################################
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
        <Card>
          <CardHeader class="border-b">
            <CardTitle class="text-2xl">{{ application.label }}</CardTitle>
            <CardDescription>{{ application.name }}</CardDescription>
            <CardAction>
              <ButtonGroup class="space-x-1">
                <EditApplicationButton :application="application" :applicationAPI="applicationAPI"
                  :loadApplication="loadApplication" />
                <DeleteApplicationButton :application="application" :applicationAPI="applicationAPI" />
              </ButtonGroup>
            </CardAction>
          </CardHeader>
          <CardContent class="space-y-4">
            <div>
              <Label>Description</Label>
              <p class="text-sm text-muted-foreground">{{ application.description ? application.description : '-' }}</p>
            </div>
            <div>
              <Label>Image</Label>
              <p class="text-sm text-muted-foreground">{{ application.image ? application.image : '-' }}</p>
            </div>
          </CardContent>
          <CardFooter class="border-t flex flex-col md:flex-row justify-between items-center gap-2 md:gap-0">
            <ButtonGroup class="space-x-1 w-full md:w-auto flex">
              <DeployButton :application="application" :applicationAPI="applicationAPI" />
              <Button class="flex-1 md:flex-initial" variant="destructive" :disabled="application.status !== 'active'">
                <StopCircle />Stop
              </Button>
              <Button class="flex-1 md:flex-initial neutral" :disabled="application.status !== 'active'">
                <RefreshCw />Restart
              </Button>
            </ButtonGroup>
            <ButtonGroup class="space-x-1 w-full md:w-auto flex">
              <Button class="flex-1 md:flex-initial" variant="outline">
                <Logs />Logs
              </Button>
              <Button class="flex-1 md:flex-initial" variant="outline">
                <Activity />Metrics
              </Button>
            </ButtonGroup>
          </CardFooter>
        </Card>

        <!-- Containers -->
        <div class="space-y-4">
          <div class="flex justify-between items-center">
            <h2 class="text-xl font-semibold">Containers</h2>
            <!-- Add Container Button -->
            <AddContainerButton :application="application" :applicationAPI="applicationAPI"
              :containersAPI="containersAPI" :projectName="projectName" :loadApplication="loadApplication" />
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
            <Card v-for="container in application.containers" :key="container.worker.hostname" class="flex flex-col">
              <CardHeader>
                <CardTitle>
                  <Badge as-child :variant="container.worker.online ? 'default' : 'destructive'">
                    <RouterLink :to="`/workers/${container.worker.hostname}`">
                      {{ container.worker.hostname }}
                    </RouterLink>
                  </Badge>
                </CardTitle>
                <CardAction>
                  <Button variant="destructive" size="icon-sm" @click="onClickDeleteContainer">
                    <Trash />
                  </Button>
                </CardAction>
              </CardHeader>
              <CardContent class="flex-1 space-y-2">
                <p class="text-sm text-muted-foreground">
                  <span class="font-medium">Status:</span> {{ container.status }}
                </p>
              </CardContent>
              <CardFooter class="border-t">
                <DeploymentLogsButton :container="container" />
              </CardFooter>
            </Card>
          </div>
        </div>
      </div>

      <!-- Error/Not Found State -->
      <Card v-else>
        <CardContent class="flex flex-col items-center justify-center py-12">
          <p class="text-muted-foreground text-lg mb-4">Application {{ appName }} not found</p>
          <Button @click="goBack" variant="outline">
            Back to Project
          </Button>
        </CardContent>
      </Card>
    </div>
  </main>
</template>
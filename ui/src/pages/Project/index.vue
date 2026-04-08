<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/spinner'

import { projectAPI, getApplicationAPI, type Project } from '@/services/api'
import ApplicationCard from './ApplicationCard.vue'
import ProjectHeader from './ProjectHeader.vue'
import AddApplicationButton from './AddApplicationButton.vue'


const route = useRoute()
const router = useRouter()
const projectName = route.params.projectId as string
const applicationAPI = getApplicationAPI(projectName)
const project = ref<Project | null>(null)
const isLoading = ref(true)

onMounted(async () => {
  await loadProject()
})

async function loadProject() {
  try {
    isLoading.value = true
    project.value = await projectAPI.fetchOne(projectName) as Project
  } catch (err) {
    console.error('Failed to load project:', err)
  } finally {
    isLoading.value = false
  }
}

function goBack() {
  router.push('/projects')
}
</script>

<template>
  <main class="flex-1 px-4 py-8 relative">
    <div class="mx-auto space-y-6 max-w-7xl">
      <!-- Loading State -->
      <div v-if="isLoading" class="absolute inset-0 z-50 flex items-center justify-center">
        <div class="flex flex-col items-center gap-4">
          <Spinner />
          <p class="text-sm text-muted-foreground">Loading project...</p>
        </div>
      </div>

      <!-- Content -->
      <div v-else-if="project" class="space-y-6">
        <!-- Project Header -->
        <ProjectHeader :project="project" :projectAPI="projectAPI" :applicationAPI="applicationAPI" :loadProject="loadProject" />

        <!-- Applications -->
        <div class="space-y-4">
          <div class="flex justify-between items-center">
            <h2 class="text-xl font-semibold">Applications</h2>
            <AddApplicationButton :applicationAPI="applicationAPI" :loadProject="loadProject" />
          </div>

          <!-- Empty Applications State -->
          <div v-if="project.applications.length === 0" class="flex items-center justify-center py-8">
            <Card class="w-full">
              <CardContent class="flex flex-col items-center justify-center py-12">
                <p class="text-muted-foreground text-lg">No applications</p>
              </CardContent>
            </Card>
          </div>

          <!-- Applications Grid -->
          <div v-else class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            <ApplicationCard v-for="app in project.applications" :key="app.name" :application="app" :project="project" />
          </div>
        </div>
      </div>

      <!-- Error/Not Found State -->
      <Card v-else>
        <CardContent class="flex flex-col items-center justify-center py-12">
          <p class="text-muted-foreground text-lg mb-4">Project {{ projectName }} not found</p>
          <Button size="sm" @click="goBack" variant="outline">
            Back to Projects
          </Button>
        </CardContent>
      </Card>
    </div>
  </main>
</template>
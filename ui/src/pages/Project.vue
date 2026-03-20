<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import {
  Field,
  FieldError,
  FieldGroup,
  FieldLabel,
  FieldSet,
} from '@/components/ui/field'
import { Textarea } from '@/components/ui/textarea'
import { project as projectService, type Project } from '@/services/api'
import CardFooter from '@/components/ui/card/CardFooter.vue'

const route = useRoute()
const router = useRouter()
const projectName = route.params.projectId as string

const project = ref<Project | null>(null)
const isLoading = ref(true)
const isDialogOpen = ref(false)
const isSubmitting = ref(false)
const errorMessage = ref('')
const successMessage = ref('')

const editFormData = ref({
  description: '',
  env: '',
})

onMounted(async () => {
  await loadProject()
})

async function loadProject() {
  try {
    isLoading.value = true
    project.value = await projectService.fetchOne(projectName) as Project
  } catch (err) {
    console.error('Failed to load project:', err)
  } finally {
    isLoading.value = false
  }
}

function openEditDialog() {
  editFormData.value.description = project.value?.description || ''
  editFormData.value.env = project.value?.env || ''
  errorMessage.value = ''
  successMessage.value = ''
  isDialogOpen.value = true
}

async function handleUpdateEnv() {
  errorMessage.value = ''
  successMessage.value = ''

  try {
    isSubmitting.value = true
    await projectService.update(projectName, { 
      description: editFormData.value.description || null,
      env: editFormData.value.env || null 
    })
    await loadProject()
    isDialogOpen.value = false
    successMessage.value = 'Project updated successfully'
    setTimeout(() => {
      successMessage.value = ''
    }, 3000)
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : 'Failed to update project'
  } finally {
    isSubmitting.value = false
  }
}

function goBack() {
  router.push('/projects')
}
</script>

<template>
  <main class="flex-1 px-4 py-8">
    <div class="mx-auto space-y-6 max-w-7xl">
      <!-- Loading State -->
      <Card v-if="isLoading">
        <CardContent class="pt-6 pb-6">
          <div class="space-y-4">
            <div class="h-8 bg-gray-300 rounded w-1/3"></div>
            <div class="h-4 bg-gray-300 rounded w-1/2"></div>
            <div class="h-32 bg-gray-300 rounded"></div>
          </div>
        </CardContent>
      </Card>

      <!-- Content -->
      <div v-else-if="project" class="space-y-6">
        <!-- Success Message -->
        <div v-if="successMessage" class="p-4 bg-green-50 text-green-700 rounded-md">
          {{ successMessage }}
        </div>

        <!-- Project Card -->
        <Card>
          <CardHeader>
            <div class="flex justify-between items-start">
              <CardTitle class="text-2xl">{{ project.name }}</CardTitle>
              <Dialog v-model:open="isDialogOpen">
                <DialogTrigger asChild>
                  <Button @click="openEditDialog">Edit</Button>
                </DialogTrigger>
                <DialogContent class="sm:max-w-[600px]">
                  <DialogHeader>
                    <DialogTitle>Edit</DialogTitle>
                  </DialogHeader>
                  <FieldSet>
                    <FieldGroup>
                      <Field>
                        <FieldLabel for="description">
                          Description
                        </FieldLabel>
                        <Textarea id="description" v-model="editFormData.description" class="resize-none" placeholder="optional" />
                      </Field>
                      <Field>
                        <FieldLabel for="env">
                          Environment Variables
                        </FieldLabel>
                        <Textarea id="env" v-model="editFormData.env" class="resize-none" placeholder="optional" />
                      </Field>
                      <Field>
                        <FieldError v-if="errorMessage">{{errorMessage}}</FieldError>
                      </Field>
                    </FieldGroup>
                  </FieldSet>
                  <DialogFooter>
                    <Button
                      type="button"
                      variant="outline"
                      @click="isDialogOpen = false"
                      :disabled="isSubmitting"
                    >
                      Cancel
                    </Button>
                    <Button
                      @click="handleUpdateEnv"
                      :disabled="isSubmitting"
                    >
                      {{ isSubmitting ? 'Saving...' : 'Save Changes' }}
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
            </div>
          </CardHeader>
          <CardContent class="space-y-4">
            <div>
              <h3 class="text-sm font-medium text-muted-foreground">{{ project.description }}</h3>
            </div>
          </CardContent>
          <CardFooter class="border-t">
            <div class="pt-4 space-y-2 text-xs text-muted-foreground">
              <p>Updated: {{ new Date(project.updated_at).toLocaleString() }}</p>
            </div>
          </CardFooter>
        </Card>
      </div>

      <!-- Error/Not Found State -->
      <Card v-else>
        <CardContent class="flex flex-col items-center justify-center py-12">
          <p class="text-muted-foreground text-lg mb-4">Project not found</p>
          <Button @click="goBack" variant="outline">
            Back to Projects
          </Button>
        </CardContent>
      </Card>
    </div>
  </main>
</template>

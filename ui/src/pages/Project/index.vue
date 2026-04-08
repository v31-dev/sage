<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Plus } from 'lucide-vue-next'
import { Card, CardContent, CardHeader, CardTitle, CardAction, CardDescription } from '@/components/ui/card'
import { ButtonGroup } from '@/components/ui/button-group'
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
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { toast } from 'vue-sonner'
import { Spinner } from '@/components/ui/spinner'
import { Label } from '@/components/ui/label'

import { projectAPI, getApplicationAPI, type Project } from '@/services/api'
import ApplicationCard from './ApplicationCard.vue'


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

// ####################################################################################################
// Create Application
const isCreateAppDialogOpen = ref(false)
const createAppFormData = ref({
  label: '',
  description: ''
})
const createAppDialogErrorMessage = ref('')
const isClickedCreateAppConfirm = ref(false)

function openCreateAppDialog() {
  createAppFormData.value = { label: '', description: '' }
  createAppDialogErrorMessage.value = ''
  isCreateAppDialogOpen.value = true
}

async function onClickCreateAppConfirm() {
  createAppDialogErrorMessage.value = ''

  if (!createAppFormData.value.label.trim()) {
    createAppDialogErrorMessage.value = 'Application name is required'
    return
  }

  try {
    isClickedCreateAppConfirm.value = true
    await applicationAPI.create({
      label: createAppFormData.value.label,
      description: createAppFormData.value.description || null,
    })
    isCreateAppDialogOpen.value = false
    toast.success(`Application ${createAppFormData.value.label} created successfully`)
    loadProject()
  } catch (err) {
    createAppDialogErrorMessage.value = err instanceof Error ? err.message : 'Failed to create application'
  } finally {
    isClickedCreateAppConfirm.value = false
  }
}
// ####################################################################################################

// ####################################################################################################
// Edit Project
const isEditDialogOpen = ref(false)
const editFormData = ref({
  label: '',
  description: '',
  env: '',
})
const editDialogErrorMessage = ref('')
const isClickedEditConfirm = ref(false)

function openEditDialog() {
  editFormData.value.label = project.value?.label || ''
  editFormData.value.description = project.value?.description || ''
  editFormData.value.env = project.value?.env || ''
  editDialogErrorMessage.value = ''
  isEditDialogOpen.value = true
}

async function handleUpdateProject() {
  editDialogErrorMessage.value = ''

  try {
    isClickedEditConfirm.value = true
    await projectAPI.update(projectName, {
      label: editFormData.value.label || null,
      description: editFormData.value.description || null,
      env: editFormData.value.env || null
    })
    await loadProject()
    isEditDialogOpen.value = false
    toast.success('Project updated successfully')
  } catch (err) {
    editDialogErrorMessage.value = err instanceof Error ? err.message : 'Failed to update project'
  } finally {
    isClickedEditConfirm.value = false
  }
}
// ####################################################################################################

// ####################################################################################################
// Delete Project
const isDeleteDialogOpen = ref(false)
const isClickedDeleteConfirm = ref(false)
const deleteDialogErrorMessage = ref('')

function openDeleteProjectDialog() {
  isDeleteDialogOpen.value = true
  isClickedDeleteConfirm.value = false
  deleteDialogErrorMessage.value = ''
}

async function onClickDeleteProjectConfirm() {
  isClickedDeleteConfirm.value = true

  await new Promise(resolve => setTimeout(resolve, 1000))

  try {
    await projectAPI.delete(projectName)
    isDeleteDialogOpen.value = false
    toast.success(`Project ${projectName} deleted successfully`)
    router.push('/projects')
  } catch (err) {
    deleteDialogErrorMessage.value = err instanceof Error ? err.message : 'Failed to delete project'
  } finally {
    isClickedDeleteConfirm.value = false
  }
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
          <p class="text-sm text-muted-foreground">Loading project...</p>
        </div>
      </div>

      <!-- Content -->
      <div v-else-if="project" class="space-y-6">
        <!-- Project Header -->
        <Card>
          <CardHeader class="border-b">
            <CardTitle class="text-2xl">{{ project.label }}</CardTitle>
            <CardDescription>{{ project.name }}</CardDescription>
            <CardAction>
              <ButtonGroup class="space-x-1">
                <!-- Edit Dialog -->
                <Dialog v-model:open="isEditDialogOpen">
                  <DialogTrigger asChild>
                    <Button size="sm" @click="openEditDialog">Edit</Button>
                  </DialogTrigger>
                  <DialogContent class="sm:max-w-[600px]">
                    <DialogHeader>
                      <DialogTitle>Edit Project</DialogTitle>
                    </DialogHeader>
                    <FieldSet>
                      <FieldGroup>
                        <Field>
                          <FieldLabel for="label">
                            Name
                          </FieldLabel>
                          <Input id="label" v-model="editFormData.label" placeholder="optional" />
                        </Field>
                        <Field>
                          <FieldLabel for="description">
                            Description
                          </FieldLabel>
                          <Textarea id="description" v-model="editFormData.description" class="resize-none"
                            placeholder="optional" />
                        </Field>
                        <Field>
                          <FieldLabel for="env">
                            Environment Variables
                          </FieldLabel>
                          <Textarea id="env" v-model="editFormData.env" class="resize-none" placeholder="optional" />
                        </Field>
                        <Field>
                          <FieldError v-if="editDialogErrorMessage">{{ editDialogErrorMessage }}</FieldError>
                        </Field>
                      </FieldGroup>
                    </FieldSet>
                    <DialogFooter>
                      <Button size="sm" type="button" variant="outline" @click="isEditDialogOpen = false"
                        :disabled="isClickedEditConfirm">
                        Cancel
                      </Button>
                      <Button size="sm" @click="handleUpdateProject" :disabled="isClickedEditConfirm">
                        <Spinner class="animate-spin" v-if="isClickedEditConfirm" />
                        Save
                      </Button>
                    </DialogFooter>
                  </DialogContent>
                </Dialog>
                <!-- Delete Project Dialog -->
                <Dialog v-model:open="isDeleteDialogOpen">
                  <DialogTrigger asChild>
                    <Button size="sm" @click="openDeleteProjectDialog" variant="destructive">Delete</Button>
                  </DialogTrigger>
                  <DialogContent class="sm:max-w-[600px]">
                    <DialogHeader>
                      <DialogTitle>Delete Project</DialogTitle>
                    </DialogHeader>
                    <FieldSet>
                      <FieldGroup>
                        <Field />
                        <Field>
                          <p class="text-sm text-muted-foreground">
                            Are you sure you want to delete this project? This action cannot be undone.
                          </p>
                        </Field>
                        <Field>
                          <FieldError v-if="deleteDialogErrorMessage">{{ deleteDialogErrorMessage }}
                          </FieldError>
                        </Field>
                      </FieldGroup>
                    </FieldSet>
                    <DialogFooter>
                      <Button size="sm" variant="destructive" @click="onClickDeleteProjectConfirm"
                        :disabled="isClickedDeleteConfirm">
                        <Spinner class="animate-spin" v-if="isClickedDeleteConfirm" />
                        Delete
                      </Button>
                    </DialogFooter>
                  </DialogContent>
                </Dialog>
              </ButtonGroup>
            </CardAction>
          </CardHeader>
          <CardContent>
            <div>
              <Label>Description</Label>
              <p class="text-sm text-muted-foreground">{{ project.description ? project.description : '-' }}</p>
            </div>
          </CardContent>
        </Card>

        <!-- Applications -->
        <div class="space-y-4">
          <div class="flex justify-between items-center">
            <h2 class="text-xl font-semibold">Applications</h2>
            <!-- Create Application -->
            <Dialog v-model:open="isCreateAppDialogOpen">
              <DialogTrigger asChild>
                <Button size="sm" @click="openCreateAppDialog" class="gap-2">
                  <Plus :size="20" />
                  New Application
                </Button>
              </DialogTrigger>
              <DialogContent class="sm:max-w-[600px]">
                <DialogHeader>
                  <DialogTitle>Create New Application</DialogTitle>
                </DialogHeader>
                <FieldSet>
                  <FieldGroup>
                    <Field />
                    <Field>
                      <FieldLabel for="app-name">
                        Name
                      </FieldLabel>
                      <Input id="app-name" v-model="createAppFormData.label" placeholder="required" />
                    </Field>
                    <Field>
                      <FieldLabel for="app-description">
                        Description
                      </FieldLabel>
                      <Textarea id="app-description" v-model="createAppFormData.description" class="resize-none"
                        placeholder="optional" />
                    </Field>
                    <Field>
                      <FieldError v-if="createAppDialogErrorMessage">{{ createAppDialogErrorMessage }}</FieldError>
                    </Field>
                  </FieldGroup>
                </FieldSet>
                <DialogFooter>
                  <Button size="sm" type="button" variant="outline" @click="isCreateAppDialogOpen = false"
                    :disabled="isClickedCreateAppConfirm">
                    Cancel
                  </Button>
                  <Button size="sm" @click="onClickCreateAppConfirm" :disabled="isClickedCreateAppConfirm">
                    <Spinner class="animate-spin" v-if="isClickedCreateAppConfirm" />
                    Create
                  </Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>
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
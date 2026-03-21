<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Plus } from 'lucide-vue-next'
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
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { project as projectService, application as applicationService, type Project, type Application } from '@/services/api'
import CardFooter from '@/components/ui/card/CardFooter.vue'

const route = useRoute()
const router = useRouter()
const projectName = route.params.projectId as string

const project = ref<Project | null>(null)
const applications = ref<Application[]>([])
const isLoading = ref(true)
const isEditDialogOpen = ref(false)
const isCreateAppDialogOpen = ref(false)
const isSubmitting = ref(false)
const errorMessage = ref('')
const successMessage = ref('')

const editFormData = ref({
  description: '',
  env: '',
})

const createAppFormData = ref({
  name: '',
  description: '',
  repo: '',
  env: '',
  args: '',
})

const projectApplications = computed(() => 
  applications.value.filter(app => app.project === projectName)
)

onMounted(async () => {
  await loadProject()
  await loadApplications()
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

async function loadApplications() {
  try {
    applications.value = await applicationService.fetchAll() as Application[]
  } catch (err) {
    console.error('Failed to load applications:', err)
  }
}

function openEditDialog() {
  editFormData.value.description = project.value?.description || ''
  editFormData.value.env = project.value?.env || ''
  errorMessage.value = ''
  successMessage.value = ''
  isEditDialogOpen.value = true
}

function openCreateAppDialog() {
  createAppFormData.value = { name: '', description: '', repo: '', env: '', args: '' }
  errorMessage.value = ''
  isCreateAppDialogOpen.value = true
}

async function handleUpdateProject() {
  errorMessage.value = ''
  successMessage.value = ''

  try {
    isSubmitting.value = true
    await projectService.update(projectName, { 
      description: editFormData.value.description || null,
      env: editFormData.value.env || null 
    })
    await loadProject()
    isEditDialogOpen.value = false
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

async function handleCreateApplication() {
  errorMessage.value = ''
  
  if (!createAppFormData.value.name.trim()) {
    errorMessage.value = 'Application name is required'
    return
  }

  try {
    isSubmitting.value = true
    await applicationService.create({
      name: createAppFormData.value.name,
      project: projectName,
      description: createAppFormData.value.description || null,
      repo: createAppFormData.value.repo || null,
      env: createAppFormData.value.env || null,
      args: createAppFormData.value.args || null,
    })
    isCreateAppDialogOpen.value = false
    await loadApplications()
    successMessage.value = 'Application created successfully'
    setTimeout(() => {
      successMessage.value = ''
    }, 3000)
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : 'Failed to create application'
  } finally {
    isSubmitting.value = false
  }
}

function goToApplication(app: Application) {
  router.push(`/projects/${projectName}/${app.name}`)
}

function goBack() {
  router.push('/projects')
}
</script>

<template>
  <main class="flex-1 px-4 py-8">
    <div class="mx-auto space-y-6 max-w-7xl">
      <!-- Loading State -->
      <div v-if="isLoading" class="flex items-center justify-center py-16">
        <div class="animate-spin rounded-full h-12 w-12 border-b-2 border-primary"></div>
      </div>

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
              <Dialog v-model:open="isEditDialogOpen">
                <DialogTrigger asChild>
                  <Button @click="openEditDialog">Edit</Button>
                </DialogTrigger>
                <DialogContent class="sm:max-w-[600px]">
                  <DialogHeader>
                    <DialogTitle>Edit Project</DialogTitle>
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
                      @click="isEditDialogOpen = false"
                      :disabled="isSubmitting"
                    >
                      Cancel
                    </Button>
                    <Button
                      @click="handleUpdateProject"
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

        <!-- Applications Section -->
        <div class="space-y-4">
          <div class="flex justify-between items-center">
            <h2 class="text-xl font-semibold">Applications</h2>
            <Dialog v-model:open="isCreateAppDialogOpen">
              <DialogTrigger asChild>
                <Button @click="openCreateAppDialog" class="gap-2">
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
                    <Field>
                      <FieldLabel for="app-name">
                        Application Name
                      </FieldLabel>
                      <Input id="app-name" v-model="createAppFormData.name" placeholder="required" />
                    </Field>
                    <Field>
                      <FieldLabel for="app-description">
                        Description
                      </FieldLabel>
                      <Textarea id="app-description" v-model="createAppFormData.description" class="resize-none" placeholder="optional" />
                    </Field>
                    <Field>
                      <FieldLabel for="app-repo">
                        Repository URL
                      </FieldLabel>
                      <Input id="app-repo" v-model="createAppFormData.repo" placeholder="optional" />
                    </Field>
                    <Field>
                      <FieldLabel for="app-env">
                        Environment Variables
                      </FieldLabel>
                      <Textarea id="app-env" v-model="createAppFormData.env" class="resize-none" placeholder="optional" />
                    </Field>
                    <Field>
                      <FieldLabel for="app-args">
                        Arguments
                      </FieldLabel>
                      <Textarea id="app-args" v-model="createAppFormData.args" class="resize-none" placeholder="optional" />
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
                    @click="isCreateAppDialogOpen = false"
                    :disabled="isSubmitting"
                  >
                    Cancel
                  </Button>
                  <Button
                    @click="handleCreateApplication"
                    :disabled="isSubmitting"
                  >
                    {{ isSubmitting ? 'Creating...' : 'Create Application' }}
                  </Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>
          </div>

          <!-- Empty Applications State -->
          <div v-if="projectApplications.length === 0" class="flex items-center justify-center py-8">
            <Card class="w-full">
              <CardContent class="flex flex-col items-center justify-center py-12">
                <p class="text-muted-foreground text-lg">No applications yet</p>
              </CardContent>
            </Card>
          </div>

          <!-- Applications Grid -->
          <div v-else class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            <Card
              v-for="app in projectApplications"
              :key="app.name"
              class="cursor-pointer hover:shadow-lg transition-shadow flex flex-col"
              @click="goToApplication(app)"
            >
              <CardHeader>
                <CardTitle class="line-clamp-2">{{ app.name }}</CardTitle>
              </CardHeader>
              <CardContent class="flex-1 space-y-2">
                <p v-if="app.description" class="text-sm text-muted-foreground">{{ app.description }}</p>
                <p v-if="app.repo" class="text-xs text-muted-foreground truncate">{{ app.repo }}</p>
              </CardContent>
              <CardFooter class="border-t">
                <div class="pt-4 text-xs text-muted-foreground">
                  <p>Updated: {{ new Date(app.updated_at).toLocaleString() }}</p>
                </div>
              </CardFooter>
            </Card>
          </div>
        </div>
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
